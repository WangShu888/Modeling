from __future__ import annotations

import re

from .models import ParsedDrawingModel


_CHINESE_NUMERALS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


def _parse_chinese_floor_token(token: str) -> int | None:
    if not token:
        return None
    if token == "十":
        return 10
    if "十" in token:
        left, _, right = token.partition("十")
        tens = _CHINESE_NUMERALS.get(left, 1 if left == "" else 0)
        ones = _CHINESE_NUMERALS.get(right, 0)
        return tens * 10 + ones
    return _CHINESE_NUMERALS.get(token)


def infer_storey_key(text: str | None) -> str | None:
    if not text:
        return None
    normalized = text.strip()
    lowered = normalized.lower()

    if any(token in lowered for token in ("roof", "rf")) or "屋面" in normalized:
        return "RF"

    basement_match = re.search(r"(?:b|地下)\s*([1-9]\d*)", lowered)
    if basement_match:
        return f"B{int(basement_match.group(1))}"

    chinese_basement_match = re.search(r"地下\s*([一二三四五六七八九十\d]+)\s*层", normalized)
    if chinese_basement_match:
        floor = chinese_basement_match.group(1)
        if floor.isdigit():
            return f"B{int(floor)}"
        parsed = _parse_chinese_floor_token(floor)
        if parsed is not None:
            return f"B{parsed}"

    negative_match = re.search(r"负\s*([一二三四五六七八九十\d]+)\s*层", normalized)
    if negative_match:
        floor = negative_match.group(1)
        if floor.isdigit():
            return f"B{int(floor)}"
        parsed = _parse_chinese_floor_token(floor)
        if parsed is not None:
            return f"B{parsed}"

    floor_match = re.search(r"([1-9]\d*)\s*[fF](?![a-z])", normalized)
    if floor_match:
        return f"{int(floor_match.group(1))}F"

    chinese_floor_match = re.search(r"([一二三四五六七八九十\d]+)\s*层", normalized)
    if chinese_floor_match:
        floor = chinese_floor_match.group(1)
        if floor.isdigit():
            return f"{int(floor)}F"
        parsed = _parse_chinese_floor_token(floor)
        if parsed is not None:
            return f"{parsed}F"

    if "首层" in normalized:
        return "1F"

    return None


def infer_asset_view_role(asset_name: str, candidate_names: list[str]) -> str:
    lowered_name = asset_name.lower()
    lowered_candidates = [name.lower() for name in candidate_names]
    if any(token in lowered_name for token in ("section",)) or "剖面" in asset_name:
        return "section"
    if any(token in lowered_name for token in ("elevation",)) or "立面" in asset_name:
        return "facade"
    if any(token in lowered_name for token in ("plan", "floor")) or "平面" in asset_name:
        return "plan"

    scores = {"plan": 0, "section": 0, "facade": 0}
    for raw_name, lowered in zip(candidate_names, lowered_candidates, strict=False):
        if infer_storey_key(raw_name) is not None:
            scores["plan"] += 3
        if lowered == "standard_floor" or "plan" in lowered or "floor" in lowered or "平面" in raw_name:
            scores["plan"] += 1
        if lowered == "section_reference" or "section" in lowered or "剖面" in raw_name:
            scores["section"] += 1
        if lowered == "facade_reference" or "elevation" in lowered or "立面" in raw_name:
            scores["facade"] += 1

    if scores["plan"] > 0 and scores["plan"] >= max(scores["section"], scores["facade"]):
        return "plan"
    if scores["section"] > scores["facade"] and scores["section"] > 0:
        return "section"
    if scores["facade"] > 0:
        return "facade"
    return "unknown"


def _infer_parsed_asset_storeys_legacy(parsed: ParsedDrawingModel) -> dict[str, str]:
    candidate_names_by_asset: dict[str, list[str]] = {}
    for candidate in parsed.storey_candidate_details:
        candidate_names_by_asset.setdefault(candidate.asset_name, []).append(candidate.name)

    asset_names = {
        entity.asset_name
        for entity in parsed.detected_entities
        if entity.category in {"wall_line", "wall_path", "door_block", "window_block", "room_boundary"}
    }
    asset_names.update(candidate_names_by_asset.keys())
    if not asset_names:
        return {}

    inferred: dict[str, str] = {}
    explicit_keys: dict[str, str] = {}
    for asset_name in sorted(asset_names):
        key = infer_storey_key(asset_name)
        if key is None:
            for candidate_name in candidate_names_by_asset.get(asset_name, []):
                key = infer_storey_key(candidate_name)
                if key is not None:
                    break
        if key is not None:
            explicit_keys[asset_name] = key

    if not explicit_keys:
        return {asset_name: "1F" for asset_name in sorted(asset_names)}

    inferred.update(explicit_keys)
    explicit_values = sorted(set(explicit_keys.values()), key=storey_sort_key)
    default_key = explicit_values[0]
    for asset_name in sorted(asset_names):
        if asset_name in inferred:
            continue
        role = infer_asset_view_role(asset_name, candidate_names_by_asset.get(asset_name, []))
        if role in {"section", "facade"} and len(explicit_values) > 1:
            continue
        inferred[asset_name] = default_key if len(explicit_values) == 1 else default_key
    return inferred


def infer_parsed_fragment_storeys(parsed: ParsedDrawingModel) -> dict[str, str]:
    inferred: dict[str, str] = {}
    for fragment in parsed.fragments:
        storey_key = (
            fragment.storey_key
            or infer_storey_key(fragment.fragment_title)
            or infer_storey_key(fragment.asset_name)
            or "1F"
        )
        inferred[fragment.fragment_id] = storey_key
    if inferred:
        return inferred

    legacy_asset_storeys = _infer_parsed_asset_storeys_legacy(parsed)
    for index, (asset_name, storey_key) in enumerate(sorted(legacy_asset_storeys.items()), start=1):
        inferred[f"{asset_name}::fragment::default::{index:02d}"] = storey_key
    return inferred


def infer_parsed_asset_storeys(parsed: ParsedDrawingModel) -> dict[str, str]:
    if parsed.fragments:
        inferred: dict[str, str] = {}
        grouped: dict[str, set[str]] = {}
        for fragment in parsed.fragments:
            role = (fragment.fragment_role or "unknown").lower()
            if role in {"section", "facade"}:
                continue
            storey_key = (
                fragment.storey_key
                or infer_storey_key(fragment.fragment_title)
                or infer_storey_key(fragment.asset_name)
                or "1F"
            )
            grouped.setdefault(fragment.asset_name, set()).add(storey_key)

        for asset_name, storey_keys in grouped.items():
            inferred[asset_name] = sorted(storey_keys, key=storey_sort_key)[0]
        if inferred:
            return inferred

    return _infer_parsed_asset_storeys_legacy(parsed)


def infer_floor_count(parsed: ParsedDrawingModel) -> int | None:
    if parsed.fragments:
        storey_keys = {
            (fragment.storey_key or infer_storey_key(fragment.fragment_title) or infer_storey_key(fragment.asset_name) or "1F")
            for fragment in parsed.fragments
            if (fragment.fragment_role or "unknown").lower() not in {"section", "facade"}
        }
        if storey_keys:
            return len(storey_keys)
    asset_storeys = infer_parsed_asset_storeys(parsed)
    storey_keys = {key for key in asset_storeys.values() if key}
    return len(storey_keys) or None


def storey_sort_key(storey_key: str) -> tuple[int, int]:
    normalized = storey_key.upper()
    if normalized == "RF":
        return (2, 0)
    basement_match = re.fullmatch(r"B(\d+)", normalized)
    if basement_match:
        return (0, -int(basement_match.group(1)))
    floor_match = re.fullmatch(r"(\d+)F", normalized)
    if floor_match:
        return (1, int(floor_match.group(1)))
    return (3, 0)


def storey_display_name(storey_key: str) -> str:
    normalized = storey_key.upper()
    if normalized == "RF":
        return "RF"
    if re.fullmatch(r"B\d+", normalized):
        return normalized
    if re.fullmatch(r"\d+F", normalized):
        return normalized
    return normalized or "1F"
