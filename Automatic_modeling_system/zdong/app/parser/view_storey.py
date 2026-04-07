from __future__ import annotations

import re
from typing import List

from ..models import BoundingBox2D, DimensionEntityRecord, StoreyCandidateRecord, TextAnnotationRecord
from ..storey_inference import infer_storey_key

_DIMENSION_TEXT_RE = re.compile(
    r"(?i)(\d+(?:\.\d+)?)\s*(mm|cm|m)\b|(\d+)\s*[x×]\s*(\d+)(?:\s*(mm|cm|m))?"
)
_NORTH_ANGLE_RE = re.compile(r"(?i)(?:north(?:\s*angle)?|true\s*north|北向)\s*[:=]?\s*([+-]?\d+(?:\.\d+)?)")
_ELEVATION_TEXT_RE = re.compile(
    r"(?i)(?:\b(?:el|elev|level)\b|[标高][高层])\s*[:=]?\s*([+-]?\d+(?:\.\d+)?)\s*(mm|cm|m)?"
)
_GRID_LABEL_RE = re.compile(
    r"(?i)(?:\b(?:axis|grid)\b\s*([A-Z0-9-]+)|\b([A-Z]{1,3}-\d{1,3})\b|\b([A-Z]{1,3})\b|\b(\d{1,3})\b)"
)
_ROOM_LABEL_KEYWORDS = (
    "室",
    "厅",
    "卫",
    "厨",
    "门厅",
    "走道",
    "楼梯",
    "宿舍",
    "办公室",
    "值班",
    "管理",
    "盥洗",
)


def _normalize_text(text: str) -> str:
    return " ".join(text.replace("\x00", " ").split()).strip()


def _round_float(value: float | int | None, digits: int = 3) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def descriptor_storey_candidates(asset_name: str, descriptor: str) -> list[StoreyCandidateRecord]:
    candidates: list[StoreyCandidateRecord] = []
    lowered = descriptor.lower()
    if any(token in descriptor for token in ("标准层", "平面图")) or any(
        token in lowered for token in ("plan", "floor")
    ):
        candidates.append(
            StoreyCandidateRecord(
                asset_name=asset_name,
                name="standard_floor",
                source="asset_descriptor",
                confidence=0.85,
            )
        )
    if "立面" in descriptor or "elevation" in lowered:
        candidates.append(
            StoreyCandidateRecord(
                asset_name=asset_name,
                name="facade_reference",
                source="asset_descriptor",
                confidence=0.85,
            )
        )
    if "剖面" in descriptor or "section" in lowered:
        candidates.append(
            StoreyCandidateRecord(
                asset_name=asset_name,
                name="section_reference",
                source="asset_descriptor",
                confidence=0.85,
            )
        )
    return candidates


def append_view_marker_candidates(
    candidates: list[StoreyCandidateRecord],
    *,
    asset_name: str,
    text: str,
    confidence: float,
    source: str,
    bbox: BoundingBox2D | None = None,
    source_ref: str | None = None,
) -> None:
    normalized = _normalize_text(text)
    lowered = normalized.lower()
    view_index = min(
        [
            index
            for index in (
                normalized.find("平面"),
                normalized.find("剖面"),
                normalized.find("立面"),
                lowered.find("plan"),
                lowered.find("section"),
                lowered.find("elevation"),
            )
            if index >= 0
        ],
        default=-1,
    )
    prefix = normalized[:view_index].strip() if view_index >= 0 else ""
    has_meaningful_prefix = any(char.isalnum() or "\u4e00" <= char <= "\u9fff" for char in prefix)
    is_callout_reference = bool(prefix) and not has_meaningful_prefix

    if any(token in normalized for token in ("平面", "标准层")) or any(
        token in lowered for token in ("plan", "floor")
    ):
        _append_slot_candidate(
            candidates, asset_name, "standard_floor", source, confidence, bbox, source_ref
        )

    if not is_callout_reference and ("剖面" in normalized or "section" in lowered):
        _append_slot_candidate(
            candidates, asset_name, "section_reference", source, confidence, bbox, source_ref
        )

    if not is_callout_reference and ("立面" in normalized or "elevation" in lowered):
        _append_slot_candidate(
            candidates, asset_name, "facade_reference", source, confidence, bbox, source_ref
        )

    inferred_storey = infer_storey_key(normalized)
    if inferred_storey is not None:
        candidates.append(
            StoreyCandidateRecord(
                asset_name=asset_name,
                name=normalized,
                source=source,
                confidence=min(confidence + 0.05, 0.95),
                bbox=bbox,
                source_ref=source_ref,
            )
        )


def _append_slot_candidate(
    candidates: list[StoreyCandidateRecord],
    asset_name: str,
    name: str,
    source: str,
    confidence: float,
    bbox: BoundingBox2D | None,
    source_ref: str | None,
) -> None:
    candidates.append(
        StoreyCandidateRecord(
            asset_name=asset_name,
            name=name,
            source=source,
            confidence=confidence,
            bbox=bbox,
            source_ref=source_ref,
        )
    )


def classify_text_semantics(text: str, layer_role: str | None = None) -> str:
    normalized = _normalize_text(text)
    lowered = normalized.lower()
    if _extract_north_angle(normalized) is not None:
        return "north_angle"
    if _extract_elevations(normalized):
        return "elevation"
    if any(token in lowered for token in ("plan", "section", "elevation", "floor")):
        return "view_marker"
    if any(token in normalized for token in ("平面", "剖面", "立面", "标准层")):
        return "view_marker"
    if _extract_dimension_records_from_text("asset", normalized, None, None):
        return "dimension"
    if _extract_grid_label(normalized):
        return "grid_label"
    if _looks_like_room_label(normalized):
        return "room_label"
    if layer_role in {"room", "space"}:
        return "room_label"
    return "generic"


def _looks_like_room_label(text: str) -> bool:
    if not text or len(text) > 8:
        return False
    if any(char.isdigit() for char in text):
        return False
    if any(token in text for token in ("，", ",", "。", "；", "：", "(", ")", "（", "）", "、")):
        return False
    if any(
        token in text
        for token in (
            "平面",
            "剖面",
            "立面",
            "详图",
            "节点",
            "雨水",
            "排水",
            "做法",
            "配筋",
            "宿舍楼",
            "广联达",
            "室外",
            "屋面",
            "屋顶",
            "地坪",
            "散水",
            "台阶",
            "现浇",
            "隔墙",
            "梁",
            "板",
            "屋顶层",
        )
    ):
        return False
    if text.endswith("层") and "楼梯间" not in text:
        return False
    return any(keyword in text for keyword in _ROOM_LABEL_KEYWORDS)


def _extract_dimension_records_from_text(
    asset_name: str,
    text: str,
    layer: str | None,
    bbox: BoundingBox2D | None,
) -> list[DimensionEntityRecord]:
    records: list[DimensionEntityRecord] = []
    for match in _DIMENSION_TEXT_RE.finditer(text):
        if match.group(1):
            records.append(
                DimensionEntityRecord(
                    asset_name=asset_name,
                    kind="text_dimension",
                    text=match.group(0),
                    value=_round_float(float(match.group(1)), 3),
                    unit=(match.group(2) or "").lower() or None,
                    layer=layer,
                    bbox=bbox,
                    source_ref=None,
                )
            )
        else:
            records.append(
                DimensionEntityRecord(
                    asset_name=asset_name,
                    kind="size_pair",
                    text=match.group(0),
                    value=None,
                    unit=(match.group(5) or "").lower() or None,
                    layer=layer,
                    bbox=bbox,
                    source_ref=None,
                )
            )
    return records


def _extract_north_angle(text: str) -> float | None:
    match = _NORTH_ANGLE_RE.search(_normalize_text(text))
    if not match:
        return None
    return _round_float(float(match.group(1)), 2)


def _extract_elevations(text: str) -> list[float]:
    normalized = _normalize_text(text)
    elevations: list[float] = []
    for match in _ELEVATION_TEXT_RE.finditer(normalized):
        if match.group(1):
            elevations.append(_round_float(float(match.group(1))) or 0.0)
    return elevations


def _extract_grid_label(text: str) -> str | None:
    normalized = _normalize_text(text)
    match = _GRID_LABEL_RE.search(normalized)
    if not match:
        return None
    groups = [value for value in match.groups() if value]
    return groups[0].upper() if groups else None


__all__ = [
    "descriptor_storey_candidates",
    "append_view_marker_candidates",
    "classify_text_semantics",
]
