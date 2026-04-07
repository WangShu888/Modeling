from __future__ import annotations

import importlib.util
import io
import json
import math
import os
import platform
import re
import shutil
import subprocess
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar
from urllib import error as urllib_error
from urllib import request as urllib_request

import ezdxf
import fitz
from ezdxf import units as ezdxf_units
from ezdxf.addons import odafc
from ezdxf.document import Drawing

from .models import (
    AssetRecord,
    BoundingBox2D,
    CoordinateReference,
    DimensionEntityRecord,
    DrawingFragmentRecord,
    DrawingEntityRecord,
    GridAxis,
    LayerMapEntry,
    ParsedDrawingModel,
    PdfAssetInfo,
    PendingReviewItem,
    Point2D,
    SourceBundle,
    StoreyCandidateRecord,
    TextAnnotationRecord,
)
from .parser import (
    DxfDocumentReader,
    ParserAssetSnapshot,
    ParserCompatibilityAdapter,
    ParserCompatibilityContext,
    descriptor_storey_candidates as parser_descriptor_storey_candidates,
)
from .storey_inference import infer_asset_view_role, infer_storey_key, storey_sort_key


_MAX_GRID_AXES = 80
_MAX_DIMENSIONS = 120
_MAX_TEXT_ITEMS = 120
_MAX_DETECTED_ENTITIES = 200
_MAX_PENDING_REVIEW = 80

_GRID_LAYER_RE = re.compile(r"(?:^|[-_.\s])(axis|grid)(?:$|[-_.\s])|\u8f74", re.IGNORECASE)
_GRID_LABEL_RE = re.compile(
    r"(?i)(?:\b(?:axis|grid)\b\s*([A-Z0-9-]+)|\b([A-Z]{1,3}-\d{1,3})\b|\b([A-Z]{1,3})\b|\b(\d{1,3})\b)"
)
_DIMENSION_TEXT_RE = re.compile(
    r"(?i)(\d+(?:\.\d+)?)\s*(mm|cm|m)\b|(\d+)\s*[x\u00d7]\s*(\d+)(?:\s*(mm|cm|m))?"
)
_ELEVATION_TEXT_RE = re.compile(
    r"(?i)(?:\b(?:el|elev|level)\b|[\u6807\u5c42]\u9ad8|[\u6807\u697c]\u5c42)\s*[:=]?\s*([+-]?\d+(?:\.\d+)?)\s*(mm|cm|m)?"
)
_SIGNED_ELEVATION_RE = re.compile(r"(?<!\d)([+-]\d+(?:\.\d{3,4})?)(?!\d)")
_NORTH_ANGLE_RE = re.compile(
    r"(?i)(?:north(?:\s*angle)?|true\s*north|\u5317\u5411)\s*[:=]?\s*([+-]?\d+(?:\.\d+)?)"
)
_UNIT_TEXT_RE = re.compile(r"\b(mm|cm|m)\b", re.IGNORECASE)
_ROLE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("grid", ("grid", "axis", "\u8f74")),
    ("wall", ("wall", "\u5899")),
    ("door", ("door", "\u95e8")),
    ("window", ("window", "wind", "\u7a97")),
    ("room", ("room", "space", "area", "\u623f\u95f4", "\u7a7a\u95f4")),
    ("dimension", ("dim", "\u5c3a\u5bf8", "\u6807\u6ce8")),
    ("text", ("text", "note", "\u6587\u5b57", "\u8bf4\u660e")),
    ("elevation", ("elev", "level", "\u6807\u9ad8")),
    ("site_boundary", ("site", "plot", "boundary", "\u5730\u5757", "\u7ea2\u7ebf", "\u8fb9\u754c")),
    ("facade", ("facade", "elevation", "\u7acb\u9762")),
)
_PROXY_REVIEW_ROLES = {"wall", "door", "window", "room"}
_MODELED_SOURCE_ENTITY_CATEGORIES = {
    "wall_line",
    "wall_path",
    "door_block",
    "window_block",
    "room_boundary",
    "room_label",
}
_DWG_VERSION_MAP = {
    "AC1012": "R13",
    "AC1014": "R14",
    "AC1015": "R2000",
    "AC1018": "R2004",
    "AC1021": "R2007",
    "AC1024": "R2010",
    "AC1027": "R2013",
    "AC1032": "R2018",
}

T = TypeVar("T")


def _empty_entity_summary() -> dict[str, int]:
    return {
        "lines": 0,
        "polylines": 0,
        "blocks": 0,
        "texts": 0,
        "dimensions": 0,
    }


def _merge_counts(target: dict[str, int], source: dict[str, int]) -> None:
    for key, value in source.items():
        target[key] = target.get(key, 0) + value


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def _normalize_text(text: str) -> str:
    return " ".join(text.replace("\x00", " ").split()).strip()


def _round_float(value: float | int | None, digits: int = 3) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _safe_float(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_append(items: list[T], value: T, limit: int) -> bool:
    if limit == _MAX_DETECTED_ENTITIES:
        items.append(value)
        return True
    if len(items) >= limit:
        return False
    items.append(value)
    return True


def _is_modeled_source_category(category: str) -> bool:
    normalized = (category or "").strip().lower()
    if not normalized:
        return False
    if normalized in _MODELED_SOURCE_ENTITY_CATEGORIES:
        return True
    if "wall" in normalized:
        return True
    if "door" in normalized:
        return True
    if "window" in normalized or "wind" in normalized:
        return True
    if "room" in normalized or "space" in normalized:
        return True
    return False


def _bbox_from_points(points: list[Point2D]) -> BoundingBox2D | None:
    if not points:
        return None
    xs = [point.x for point in points]
    ys = [point.y for point in points]
    return BoundingBox2D(min_x=min(xs), min_y=min(ys), max_x=max(xs), max_y=max(ys))


def _bbox_from_rect(rect: Any) -> BoundingBox2D | None:
    if rect is None:
        return None
    if hasattr(rect, "x0"):
        return BoundingBox2D(
            min_x=_round_float(rect.x0) or 0.0,
            min_y=_round_float(rect.y0) or 0.0,
            max_x=_round_float(rect.x1) or 0.0,
            max_y=_round_float(rect.y1) or 0.0,
        )
    if isinstance(rect, (tuple, list)) and len(rect) >= 4:
        return BoundingBox2D(
            min_x=_round_float(rect[0]) or 0.0,
            min_y=_round_float(rect[1]) or 0.0,
            max_x=_round_float(rect[2]) or 0.0,
            max_y=_round_float(rect[3]) or 0.0,
        )
    return None


def _to_point(value: Any) -> Point2D:
    if hasattr(value, "x") and hasattr(value, "y"):
        return Point2D(x=_round_float(value.x) or 0.0, y=_round_float(value.y) or 0.0)
    if isinstance(value, (tuple, list)):
        return Point2D(x=_round_float(value[0]) or 0.0, y=_round_float(value[1]) or 0.0)
    return Point2D(x=0.0, y=0.0)


def _orientation_from_points(start: Point2D, end: Point2D) -> str:
    dx = abs(end.x - start.x)
    dy = abs(end.y - start.y)
    if dx == 0 and dy == 0:
        return "unknown"
    if dx <= dy * 0.2:
        return "vertical"
    if dy <= dx * 0.2:
        return "horizontal"
    return "angled"


def _guess_semantic_role(name: str | None) -> str:
    if not name:
        return "unknown"
    lowered = name.lower()
    for role, tokens in _ROLE_PATTERNS:
        if any(token.lower() in lowered for token in tokens):
            return role
    return "unknown"


def _extract_grid_label(text: str) -> str | None:
    normalized = _normalize_text(text)
    match = _GRID_LABEL_RE.search(normalized)
    if not match:
        return None
    groups = [value for value in match.groups() if value]
    if not groups:
        return None
    return groups[0].upper()


def _extract_north_angle(text: str) -> float | None:
    match = _NORTH_ANGLE_RE.search(_normalize_text(text))
    if not match:
        return None
    return _round_float(float(match.group(1)), 2)


def _extract_units_from_texts(texts: list[str]) -> str | None:
    for text in texts:
        match = _UNIT_TEXT_RE.search(text)
        if match:
            return match.group(1).lower()
    return None


def _convert_length_to_m(value: float, unit: str | None) -> float:
    if unit is None or unit.lower() == "m":
        return float(value)
    if unit.lower() == "cm":
        return float(value) / 100.0
    if unit.lower() == "mm":
        return float(value) / 1000.0
    return float(value)


def _extract_elevations(text: str) -> list[float]:
    normalized = _normalize_text(text)
    values: list[float] = []
    for match in _ELEVATION_TEXT_RE.finditer(normalized):
        value = float(match.group(1))
        unit = match.group(2) or "m"
        values.append(_round_float(_convert_length_to_m(value, unit), 3) or 0.0)
    if not values:
        for match in _SIGNED_ELEVATION_RE.finditer(normalized):
            values.append(_round_float(float(match.group(1)), 3) or 0.0)
    return values


def _extract_dimension_records_from_text(
    asset_name: str,
    text: str,
    layer: str | None,
    bbox: BoundingBox2D | None,
    source_ref: str | None = None,
) -> list[DimensionEntityRecord]:
    normalized = _normalize_text(text)
    records: list[DimensionEntityRecord] = []
    for match in _DIMENSION_TEXT_RE.finditer(normalized):
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
                    source_ref=source_ref,
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
                    source_ref=source_ref,
                )
            )
    return records


def _descriptor_storey_candidates(asset_name: str, descriptor: str) -> list[StoreyCandidateRecord]:
    candidates: list[StoreyCandidateRecord] = []
    lowered = descriptor.lower()
    if any(token in descriptor for token in ("\u6807\u51c6\u5c42", "\u5e73\u9762\u56fe")) or any(
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
    if "\u7acb\u9762" in descriptor or "elevation" in lowered:
        candidates.append(
            StoreyCandidateRecord(
                asset_name=asset_name,
                name="facade_reference",
                source="asset_descriptor",
                confidence=0.85,
            )
        )
    if "\u5256\u9762" in descriptor or "section" in lowered:
        candidates.append(
            StoreyCandidateRecord(
                asset_name=asset_name,
                name="section_reference",
                source="asset_descriptor",
                confidence=0.85,
            )
        )
    return candidates


def _append_view_marker_candidates(
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
        candidates.append(
            StoreyCandidateRecord(
                asset_name=asset_name,
                name="standard_floor",
                source=source,
                confidence=confidence,
                bbox=bbox,
                source_ref=source_ref,
            )
        )

    if not is_callout_reference and ("剖面" in normalized or "section" in lowered):
        candidates.append(
            StoreyCandidateRecord(
                asset_name=asset_name,
                name="section_reference",
                source=source,
                confidence=confidence,
                bbox=bbox,
                source_ref=source_ref,
            )
        )

    if not is_callout_reference and ("立面" in normalized or "elevation" in lowered):
        candidates.append(
            StoreyCandidateRecord(
                asset_name=asset_name,
                name="facade_reference",
                source=source,
                confidence=confidence,
                bbox=bbox,
                source_ref=source_ref,
            )
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


def _classify_text_semantics(text: str, layer_role: str | None = None) -> str:
    normalized = _normalize_text(text)
    lowered = normalized.lower()
    if _extract_north_angle(normalized) is not None:
        return "north_angle"
    if _extract_elevations(normalized):
        return "elevation"
    if any(token in lowered for token in ("plan", "section", "elevation", "floor")):
        return "view_marker"
    if any(token in normalized for token in ("\u5e73\u9762", "\u5256\u9762", "\u7acb\u9762", "\u6807\u51c6\u5c42")):
        return "view_marker"
    if _extract_dimension_records_from_text("asset", normalized, None, None):
        return "dimension"
    if _extract_grid_label(normalized):
        return "grid_label"
    if layer_role in {"room", "space"}:
        return "room_label"
    return "generic"


def _format_annotation_summaries(items: list[TextAnnotationRecord]) -> list[str]:
    formatted = [f"{item.asset_name}: {item.text}" for item in items if item.text]
    return _dedupe_keep_order(formatted)[:_MAX_TEXT_ITEMS]


def _explicit_storey_keys(candidates: list[StoreyCandidateRecord]) -> list[str]:
    keys: list[str] = []
    for candidate in candidates:
        key = infer_storey_key(candidate.name)
        if key is not None and key not in keys:
            keys.append(key)
    return keys


def _entity_anchor(entity: DrawingEntityRecord) -> Point2D | None:
    if entity.points:
        x = sum(point.x for point in entity.points) / len(entity.points)
        y = sum(point.y for point in entity.points) / len(entity.points)
        return Point2D(x=_round_float(x) or 0.0, y=_round_float(y) or 0.0)
    if entity.bbox is not None:
        return Point2D(
            x=_round_float((entity.bbox.min_x + entity.bbox.max_x) / 2.0) or 0.0,
            y=_round_float((entity.bbox.min_y + entity.bbox.max_y) / 2.0) or 0.0,
        )
    return None


def _match_fragment_bbox(
    text_index: dict[tuple[str, str], BoundingBox2D],
    *,
    asset_name: str,
    title: str,
) -> BoundingBox2D | None:
    return text_index.get((asset_name, _normalize_text(title)))


def _entity_bbox(entity: DrawingEntityRecord) -> BoundingBox2D | None:
    if entity.bbox is not None:
        return entity.bbox
    if entity.points:
        return _bbox_from_points(entity.points)
    return None


def _role_from_fragment_title(asset_name: str, title: str) -> str:
    lowered = title.lower()
    if "剖面" in title or "section" in lowered:
        return "section"
    if "立面" in title or "elevation" in lowered:
        return "facade"
    if "平面" in title or "plan" in lowered or infer_storey_key(title) is not None:
        return "plan"
    return infer_asset_view_role(asset_name, [title])


def _entity_source_summary(detected_entities: list[DrawingEntityRecord]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for entity in detected_entities:
        key = entity.asset_name or "unknown_asset"
        summary[key] = summary.get(key, 0) + 1
    return summary


def _assign_entity_fragment(
    entity: DrawingEntityRecord,
    fragments: list[DrawingFragmentRecord],
) -> None:
    if not fragments:
        return
    if "fragment_id" in entity.metadata:
        return

    anchor = _entity_anchor(entity)
    selected = fragments[0]
    if anchor is not None:
        in_bbox = [
            fragment
            for fragment in fragments
            if fragment.bbox is not None
            and fragment.bbox.min_x <= anchor.x <= fragment.bbox.max_x
            and fragment.bbox.min_y <= anchor.y <= fragment.bbox.max_y
        ]
        if in_bbox:
            selected = in_bbox[0]
        else:
            selected = min(
                fragments,
                key=lambda fragment: math.dist(
                    (
                        anchor.x,
                        anchor.y,
                    ),
                    (
                        (fragment.bbox.min_x + fragment.bbox.max_x) / 2.0,
                        (fragment.bbox.min_y + fragment.bbox.max_y) / 2.0,
                    ),
                )
                if fragment.bbox is not None
                else float("inf"),
            )

    entity.metadata.setdefault("fragment_id", selected.fragment_id)
    entity.metadata.setdefault("fragment_title", selected.fragment_title or selected.storey_key)
    entity.metadata.setdefault("fragment_role", selected.fragment_role)
    entity.metadata.setdefault("fragment_storey_key", selected.storey_key)


def _cluster_plan_fragment_bboxes(
    fragments: list[DrawingFragmentRecord],
    detected_entities: list[DrawingEntityRecord],
) -> None:
    if not fragments:
        return

    geometry_categories = {"wall_line", "wall_path", "door_block", "window_block", "room_boundary"}
    fragments_by_asset: dict[str, list[DrawingFragmentRecord]] = defaultdict(list)
    for fragment in fragments:
        if fragment.fragment_role == "plan":
            fragments_by_asset[fragment.asset_name].append(fragment)

    entities_by_asset: dict[str, list[DrawingEntityRecord]] = defaultdict(list)
    for entity in detected_entities:
        if entity.category in geometry_categories:
            entities_by_asset[entity.asset_name].append(entity)

    for asset_name, asset_fragments in fragments_by_asset.items():
        if len(asset_fragments) <= 1:
            continue
        asset_entities = entities_by_asset.get(asset_name, [])
        if len(asset_entities) < len(asset_fragments):
            continue

        fragment_centers: list[tuple[DrawingFragmentRecord, Point2D]] = []
        for fragment in asset_fragments:
            if fragment.bbox is None:
                continue
            fragment_centers.append(
                (
                    fragment,
                    Point2D(
                        x=(fragment.bbox.min_x + fragment.bbox.max_x) / 2.0,
                        y=(fragment.bbox.min_y + fragment.bbox.max_y) / 2.0,
                    ),
                )
            )
        if len(fragment_centers) != len(asset_fragments):
            continue

        span_x = max(center.x for _, center in fragment_centers) - min(center.x for _, center in fragment_centers)
        span_y = max(center.y for _, center in fragment_centers) - min(center.y for _, center in fragment_centers)
        axis = "x" if span_x >= span_y else "y"

        entity_infos: list[tuple[DrawingEntityRecord, Point2D, BoundingBox2D]] = []
        for entity in asset_entities:
            anchor = _entity_anchor(entity)
            bbox = _entity_bbox(entity)
            if anchor is None or bbox is None:
                continue
            entity_infos.append((entity, anchor, bbox))
        if len(entity_infos) < len(asset_fragments):
            continue

        fragment_centers.sort(key=lambda item: item[1].x if axis == "x" else item[1].y)
        entity_infos.sort(key=lambda item: item[1].x if axis == "x" else item[1].y)
        ordered_values = [item[1].x if axis == "x" else item[1].y for item in entity_infos]
        gaps = [
            (ordered_values[index + 1] - ordered_values[index], index)
            for index in range(len(ordered_values) - 1)
        ]
        if len(gaps) < len(asset_fragments) - 1:
            continue

        split_indices = sorted(index for _, index in sorted(gaps, reverse=True)[: len(asset_fragments) - 1])
        groups: list[list[tuple[DrawingEntityRecord, Point2D, BoundingBox2D]]] = []
        start = 0
        for split_index in split_indices:
            groups.append(entity_infos[start : split_index + 1])
            start = split_index + 1
        groups.append(entity_infos[start:])
        if len(groups) != len(asset_fragments) or any(not group for group in groups):
            continue

        for (fragment, _), group in zip(fragment_centers, groups):
            min_x = min(item[2].min_x for item in group)
            min_y = min(item[2].min_y for item in group)
            max_x = max(item[2].max_x for item in group)
            max_y = max(item[2].max_y for item in group)
            span_width = max(max_x - min_x, 1.0)
            span_height = max(max_y - min_y, 1.0)
            padding_x = max(span_width * 0.05, 100.0)
            padding_y = max(span_height * 0.05, 100.0)
            fragment.bbox = BoundingBox2D(
                min_x=_round_float(min_x - padding_x) or min_x - padding_x,
                min_y=_round_float(min_y - padding_y) or min_y - padding_y,
                max_x=_round_float(max_x + padding_x) or max_x + padding_x,
                max_y=_round_float(max_y + padding_y) or max_y + padding_y,
            )


def _build_drawing_fragments(
    detected_entities: list[DrawingEntityRecord],
    text_items: list[TextAnnotationRecord],
    storey_candidate_details: list[StoreyCandidateRecord],
) -> list[DrawingFragmentRecord]:
    text_index: dict[tuple[str, str], BoundingBox2D] = {}
    for item in text_items:
        if item.semantic_tag != "view_marker" or item.bbox is None:
            continue
        text_index.setdefault((item.asset_name, _normalize_text(item.text)), item.bbox)

    candidates_by_asset: dict[str, list[StoreyCandidateRecord]] = defaultdict(list)
    for candidate in storey_candidate_details:
        candidates_by_asset[candidate.asset_name].append(candidate)

    geometry_assets = sorted(
        {
            entity.asset_name
            for entity in detected_entities
            if entity.category in {"wall_line", "wall_path", "door_block", "window_block", "room_boundary"}
        }
    )
    fragments: list[DrawingFragmentRecord] = []

    for asset_name in geometry_assets:
        asset_candidates = candidates_by_asset.get(asset_name, [])
        explicit: list[tuple[str, str, str, BoundingBox2D | None]] = []
        for candidate in asset_candidates:
            storey_key = infer_storey_key(candidate.name)
            if storey_key is None:
                continue
            explicit.append((candidate.name, storey_key, candidate.source, candidate.bbox))

        if not explicit:
            fallback_key = infer_storey_key(asset_name) or "1F"
            explicit.append((asset_name, fallback_key, "asset_fallback", None))

        unique_explicit: list[tuple[str, str, str, BoundingBox2D | None]] = []
        seen: set[tuple[str, str]] = set()
        for title, storey_key, source, bbox in explicit:
            dedupe_key = (title, storey_key)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            unique_explicit.append((title, storey_key, source, bbox))

        unique_explicit.sort(key=lambda item: (storey_sort_key(item[1]), item[0]))
        for index, (title, storey_key, source, candidate_bbox) in enumerate(unique_explicit, start=1):
            role = _role_from_fragment_title(asset_name, title)
            fragment_id = f"{asset_name}::fragment::{storey_key}::{index:02d}"
            fragments.append(
                DrawingFragmentRecord(
                    fragment_id=fragment_id,
                    asset_name=asset_name,
                    fragment_title=title,
                    fragment_role=role,
                    storey_key=storey_key,
                    bbox=candidate_bbox or _match_fragment_bbox(text_index, asset_name=asset_name, title=title),
                    source=source,
                )
            )

    _cluster_plan_fragment_bboxes(fragments, detected_entities)

    fragments_by_asset: dict[str, list[DrawingFragmentRecord]] = defaultdict(list)
    for fragment in fragments:
        fragments_by_asset[fragment.asset_name].append(fragment)

    for entity in detected_entities:
        if entity.category not in {"wall_line", "wall_path", "door_block", "window_block", "room_boundary"}:
            continue
        _assign_entity_fragment(entity, fragments_by_asset.get(entity.asset_name, []))

    return fragments


def _append_parse_diagnostics(
    detected_entities: list[DrawingEntityRecord],
    detected_entities_dropped: int,
    fragments: list[DrawingFragmentRecord],
    storey_candidate_details: list[StoreyCandidateRecord],
    pending_review: list[PendingReviewItem],
) -> None:
    if detected_entities_dropped > 0:
        _safe_append(
            pending_review,
            PendingReviewItem(
                category="entity_detection_truncated",
                reason=(
                    f"{detected_entities_dropped} detected entities were dropped by downstream constraints. "
                    "Review parser/export limits to avoid losing geometry evidence."
                ),
                severity="warning",
            ),
            _MAX_PENDING_REVIEW,
        )

    geometry_assets = {
        fragment.asset_name for fragment in fragments if fragment.fragment_role not in {"section", "facade"}
    }
    if not geometry_assets:
        return

    fragment_keys_by_asset: dict[str, set[str]] = defaultdict(set)
    for fragment in fragments:
        if fragment.fragment_role in {"section", "facade"}:
            continue
        fragment_keys_by_asset[fragment.asset_name].add(fragment.storey_key)

    assigned_keys_by_asset: dict[str, set[str]] = defaultdict(set)
    for entity in detected_entities:
        if entity.category not in {"wall_line", "wall_path", "door_block", "window_block", "room_boundary"}:
            continue
        raw_storey_key = entity.metadata.get("fragment_storey_key") or entity.metadata.get("storey_key")
        if raw_storey_key:
            assigned_keys_by_asset[entity.asset_name].add(str(raw_storey_key))

    for asset_name in sorted(geometry_assets):
        storey_keys = sorted(fragment_keys_by_asset.get(asset_name, set()), key=storey_sort_key)
        if not storey_keys:
            candidates = [item for item in storey_candidate_details if item.asset_name == asset_name]
            storey_keys = _explicit_storey_keys(candidates)
        if len(storey_keys) <= 1:
            continue
        if len(assigned_keys_by_asset.get(asset_name, set())) >= min(2, len(storey_keys)):
            continue
        _safe_append(
            pending_review,
            PendingReviewItem(
                asset_name=asset_name,
                category="multi_storey_asset_collapsed",
                reason=(
                    f"Detected explicit storey candidates {', '.join(storey_keys)} in one asset. "
                    "Detected geometry is still concentrated in too few fragment storeys, so inter-storey "
                    "stitching may remain collapsed until fragment assignment is reliable."
                ),
                severity="warning",
            ),
            _MAX_PENDING_REVIEW,
        )


def _origin_from_vec3(value: Any, asset_name: str, source: str) -> CoordinateReference:
    if hasattr(value, "x") and hasattr(value, "y"):
        return CoordinateReference(
            x=_round_float(value.x) or 0.0,
            y=_round_float(value.y) or 0.0,
            source=source,
            asset_name=asset_name,
            confidence=0.8,
        )
    return CoordinateReference(asset_name=asset_name, source=source, confidence=0.3)


def _resolve_coordinate_reference(
    current: CoordinateReference,
    incoming: CoordinateReference | None,
    pending_review: list[PendingReviewItem],
) -> CoordinateReference:
    if incoming is None:
        return current
    if current.source == "default":
        return incoming
    if abs(current.x - incoming.x) > 1e-6 or abs(current.y - incoming.y) > 1e-6:
        _safe_append(
            pending_review,
            PendingReviewItem(
                asset_name=incoming.asset_name,
                category="origin_conflict",
                reason=(
                    f"Detected origin ({incoming.x}, {incoming.y}) conflicts with "
                    f"({current.x}, {current.y})."
                ),
                severity="warning",
            ),
            _MAX_PENDING_REVIEW,
        )
    return current


def _resolve_north_angle(
    current: float | None,
    incoming: float | None,
    asset_name: str,
    pending_review: list[PendingReviewItem],
) -> float | None:
    if incoming is None:
        return current
    if current is None:
        return incoming
    if abs(current - incoming) > 0.1:
        _safe_append(
            pending_review,
            PendingReviewItem(
                asset_name=asset_name,
                category="north_angle_conflict",
                reason=f"Detected north angle {incoming} conflicts with {current}.",
                severity="warning",
            ),
            _MAX_PENDING_REVIEW,
        )
    return current


@dataclass
class _AssetParseResult:
    kind: str
    units: str | None = None
    origin: CoordinateReference | None = None
    north_angle: float | None = None
    recognized_layers: set[str] = field(default_factory=set)
    layer_map: list[LayerMapEntry] = field(default_factory=list)
    grid_map: list[GridAxis] = field(default_factory=list)
    dimension_details: list[DimensionEntityRecord] = field(default_factory=list)
    text_items: list[TextAnnotationRecord] = field(default_factory=list)
    detected_entities: list[DrawingEntityRecord] = field(default_factory=list)
    storey_candidates: list[StoreyCandidateRecord] = field(default_factory=list)
    storey_elevations_m: list[float] = field(default_factory=list)
    pdf_assets: list[PdfAssetInfo] = field(default_factory=list)
    pending_review: list[PendingReviewItem] = field(default_factory=list)
    unresolved_entities: list[str] = field(default_factory=list)
    entity_summary: dict[str, int] = field(default_factory=_empty_entity_summary)
    site_boundary_detected: bool = False
    space_boundaries_detected: int = 0


class DrawingParser:
    def __init__(self, workspace_root: Path | None = None) -> None:
        self.workspace_root = workspace_root or Path(__file__).resolve().parents[2]
        self._converter_workspace = self.workspace_root / ".jianmo-odafc"

    def parse(self, bundle: SourceBundle) -> ParsedDrawingModel:
        asset_results: list[ParserAssetSnapshot] = []
        for asset in bundle.assets:
            result = self._parse_asset(asset)
            asset_results.append(
                ParserAssetSnapshot(
                    asset_name=asset.filename,
                    kind=result.kind,
                    units=result.units,
                    origin=result.origin,
                    north_angle=result.north_angle,
                    recognized_layers=sorted(result.recognized_layers),
                    layer_map=result.layer_map,
                    grid_map=result.grid_map,
                    dimension_details=result.dimension_details,
                    text_items=result.text_items,
                    detected_entities=result.detected_entities,
                    storey_candidate_details=result.storey_candidates,
                    storey_elevations_m=result.storey_elevations_m,
                    pending_review=result.pending_review,
                    entity_summary=result.entity_summary,
                    site_boundary_detected=result.site_boundary_detected,
                    space_boundaries_detected=result.space_boundaries_detected,
                    pdf_assets=result.pdf_assets,
                    unresolved_entities=result.unresolved_entities,
                )
            )

        parsed = ParserCompatibilityAdapter().adapt(
            ParserCompatibilityContext(
                asset_results=asset_results,
                bundle_units=str(bundle.form_fields.get("unit")) if bundle.form_fields.get("unit") is not None else None,
                bundle_units_locked=bundle.form_fields.get("unit") is not None,
                bundle_north_angle=(
                    _safe_float(bundle.form_fields.get("north_angle"), 0.0)
                    if bundle.form_fields.get("north_angle") is not None
                    else None
                ),
                default_origin=CoordinateReference(),
            )
        )

        if not bundle.assets:
            parsed.unresolved_entities.append("No drawing assets were uploaded.")
            parsed.unresolved_entities = _dedupe_keep_order(parsed.unresolved_entities)

        return parsed

    def _parse_asset(self, asset: AssetRecord) -> _AssetParseResult:
        descriptor = f"{asset.filename} {asset.description or ''}"
        fallback_candidates = parser_descriptor_storey_candidates(asset.filename, descriptor)
        path = self._resolve_asset_path(asset)

        if asset.extension in {".dwg", ".dxf"}:
            kind = "cad"
        elif asset.extension == ".pdf":
            kind = "pdf"
        else:
            return _AssetParseResult(
                kind="unknown",
                storey_candidates=fallback_candidates,
                unresolved_entities=[f"{asset.filename}: unsupported asset kind '{asset.extension or 'unknown'}'."],
            )

        if path is None:
            return _AssetParseResult(
                kind=kind,
                storey_candidates=fallback_candidates,
                pending_review=[
                    PendingReviewItem(
                        asset_name=asset.filename,
                        category="asset_missing",
                        reason="Drawing file was referenced but not found in the workspace.",
                        severity="error",
                    )
                ],
                unresolved_entities=[
                    f"{asset.filename}: file not found. Provide an absolute/relative asset path or place the file in the workspace."
                ],
            )

        if asset.extension == ".dxf":
            result = self._parse_dxf(path, asset.filename, descriptor)
        elif asset.extension == ".dwg":
            result = self._parse_dwg(path, asset.filename, descriptor)
        else:
            result = self._parse_pdf(path, asset.filename, descriptor)

        result.storey_candidates = [*fallback_candidates, *result.storey_candidates]
        self._finalize_asset_quality(asset.filename, kind, result)
        return result

    def _parse_dxf(self, path: Path, asset_name: str, descriptor: str) -> _AssetParseResult:
        try:
            doc = ezdxf.readfile(path)
        except Exception as exc:
            return _AssetParseResult(
                kind="cad",
                pending_review=[
                    PendingReviewItem(
                        asset_name=asset_name,
                        category="dxf_parse_failed",
                        reason=f"DXF parsing failed: {exc}",
                        severity="error",
                    )
                ],
                unresolved_entities=[f"{asset_name}: DXF parsing failed: {exc}"],
            )
        return self._parse_dxf_document(doc, asset_name, descriptor)

    def _parse_dwg(self, path: Path, asset_name: str, descriptor: str) -> _AssetParseResult:
        dwg_version = self._read_dwg_version(path)
        version_note = f"DWG version {dwg_version}" if dwg_version else "DWG version unknown"

        try:
            converted_path = self._convert_dwg_to_dxf(path, dwg_version)
        except FileNotFoundError as exc:
            return _AssetParseResult(
                kind="cad",
                text_items=[
                    TextAnnotationRecord(
                        asset_name=asset_name,
                        text=version_note,
                        semantic_tag="dwg_version",
                    )
                ],
                pending_review=[
                    PendingReviewItem(
                        asset_name=asset_name,
                        category="dwg_converter_missing",
                        reason=str(exc),
                        severity="error",
                    )
                ],
                unresolved_entities=[f"{asset_name}: {exc}"],
            )
        except RuntimeError as exc:
            return _AssetParseResult(
                kind="cad",
                text_items=[
                    TextAnnotationRecord(
                        asset_name=asset_name,
                        text=version_note,
                        semantic_tag="dwg_version",
                    )
                ],
                pending_review=[
                    PendingReviewItem(
                        asset_name=asset_name,
                        category="dwg_conversion_failed",
                        reason=f"DWG conversion failed: {exc}",
                        severity="error",
                    )
                ],
                unresolved_entities=[f"{asset_name}: DWG conversion failed: {exc}"],
            )

        try:
            doc = ezdxf.readfile(converted_path)
        except Exception as exc:
            return _AssetParseResult(
                kind="cad",
                text_items=[
                    TextAnnotationRecord(
                        asset_name=asset_name,
                        text=version_note,
                        semantic_tag="dwg_version",
                    )
                ],
                pending_review=[
                    PendingReviewItem(
                        asset_name=asset_name,
                        category="dwg_converted_dxf_invalid",
                        reason=f"Converted DXF parsing failed: {exc}",
                        severity="error",
                    )
                ],
                unresolved_entities=[f"{asset_name}: converted DXF parsing failed: {exc}"],
            )
        finally:
            shutil.rmtree(converted_path.parent, ignore_errors=True)

        result = self._parse_dxf_document(doc, asset_name, descriptor)
        _safe_append(
            result.text_items,
            TextAnnotationRecord(asset_name=asset_name, text=version_note, semantic_tag="dwg_version"),
            _MAX_TEXT_ITEMS,
        )
        return result

    def _parse_dxf_document(self, doc: Drawing, asset_name: str, descriptor: str) -> _AssetParseResult:
        layer_stats: dict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "types": set()})
        grid_map: list[GridAxis] = []
        dimension_details: list[DimensionEntityRecord] = []
        text_items: list[TextAnnotationRecord] = []
        detected_entities: list[DrawingEntityRecord] = []
        result = DxfDocumentReader().parse_document(doc, asset_name, descriptor)

        return _AssetParseResult(
            kind="cad",
            units=result.units,
            origin=result.origin,
            north_angle=result.north_angle,
            recognized_layers=result.recognized_layers,
            layer_map=result.layer_map,
            grid_map=result.grid_map,
            dimension_details=result.dimension_details,
            text_items=result.text_items,
            detected_entities=result.detected_entities,
            storey_candidates=result.storey_candidates,
            storey_elevations_m=result.storey_elevations_m,
            pending_review=result.pending_review,
            entity_summary=result.entity_summary,
            site_boundary_detected=result.site_boundary_detected,
            space_boundaries_detected=result.space_boundaries_detected,
        )

    def _parse_pdf(self, path: Path, asset_name: str, descriptor: str) -> _AssetParseResult:
        try:
            doc = fitz.open(path)
        except Exception as exc:
            return _AssetParseResult(
                kind="pdf",
                pending_review=[
                    PendingReviewItem(
                        asset_name=asset_name,
                        category="pdf_parse_failed",
                        reason=f"PDF parsing failed: {exc}",
                        severity="error",
                    )
                ],
                unresolved_entities=[f"{asset_name}: PDF parsing failed: {exc}"],
            )

        recognized_layers: set[str] = set()
        layer_stats: dict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "types": set()})
        grid_map: list[GridAxis] = []
        dimension_details: list[DimensionEntityRecord] = []
        text_items: list[TextAnnotationRecord] = []
        detected_entities: list[DrawingEntityRecord] = []
        storey_candidates = _descriptor_storey_candidates(asset_name, descriptor)
        storey_elevations: list[float] = []
        pending_review: list[PendingReviewItem] = []
        entity_summary = _empty_entity_summary()
        north_angle: float | None = None
        space_boundaries_detected = 0
        pdf_page_modes: list[str] = []

        ocr_available = self._ocr_is_available()
        ocr_attempted = False

        try:
            for page in doc:
                block_tuples = page.get_text("blocks")
                drawings = page.get_drawings()
                images = page.get_images(full=True)
                text_count = len([block for block in block_tuples if _normalize_text(str(block[4]))])
                drawing_count = len(drawings)
                image_count = len(images)
                page_mode = self._classify_pdf_page(text_count, drawing_count, image_count)
                pdf_page_modes.append(page_mode)

                if text_count > 0:
                    recognized_layers.add("PDF-TEXT")
                    layer_stats["PDF-TEXT"]["count"] += text_count
                    layer_stats["PDF-TEXT"]["types"].add("TEXT")
                if drawing_count > 0:
                    recognized_layers.add("PDF-VECTOR")
                    layer_stats["PDF-VECTOR"]["count"] += drawing_count
                    layer_stats["PDF-VECTOR"]["types"].add("VECTOR")
                if image_count > 0:
                    recognized_layers.add("PDF-IMAGE")
                    layer_stats["PDF-IMAGE"]["count"] += image_count
                    layer_stats["PDF-IMAGE"]["types"].add("IMAGE")

                for block_index, block in enumerate(block_tuples):
                    text = _normalize_text(str(block[4]))
                    if not text:
                        continue
                    entity_summary["texts"] += 1
                    bbox = _bbox_from_rect(block[:4])
                    annotation = TextAnnotationRecord(
                        asset_name=asset_name,
                        text=f"page {page.number + 1}: {text}",
                        semantic_tag=_classify_text_semantics(text),
                        layer="PDF-TEXT",
                        page_index=page.number,
                        bbox=bbox,
                        source_ref=f"page:{page.number + 1}:block:{block_index}",
                    )
                    _safe_append(text_items, annotation, _MAX_TEXT_ITEMS)

                    grid_label = _extract_grid_label(text)
                    if grid_label:
                        _safe_append(
                            grid_map,
                            GridAxis(
                                asset_name=asset_name,
                                label=grid_label,
                                orientation="unknown",
                                coordinate=None,
                                layer="PDF-TEXT",
                                source_ref=annotation.source_ref,
                                confidence=0.66,
                            ),
                            _MAX_GRID_AXES,
                        )

                    for record in _extract_dimension_records_from_text(
                        asset_name, text, "PDF-TEXT", bbox, annotation.source_ref
                    ):
                        _safe_append(dimension_details, record, _MAX_DIMENSIONS)

                    for elevation in _extract_elevations(text):
                        storey_elevations.append(elevation)

                    extracted_north_angle = _extract_north_angle(text)
                    if extracted_north_angle is not None:
                        north_angle = extracted_north_angle

                    if annotation.semantic_tag == "view_marker":
                        _append_view_marker_candidates(
                            storey_candidates,
                            asset_name=asset_name,
                            text=text,
                            confidence=0.76,
                            source="drawing_text",
                            bbox=bbox,
                            source_ref=annotation.source_ref,
                        )

                for drawing_index, drawing in enumerate(drawings):
                    layer_name = str(drawing.get("layer") or "PDF-VECTOR").strip() or "PDF-VECTOR"
                    role = _guess_semantic_role(layer_name)
                    recognized_layers.add(layer_name)
                    layer_stats[layer_name]["count"] += 1
                    layer_stats[layer_name]["types"].add("VECTOR")
                    for item_index, item in enumerate(drawing.get("items", [])):
                        operator = item[0]
                        source_ref = f"page:{page.number + 1}:drawing:{drawing_index}:item:{item_index}"
                        if operator == "l":
                            entity_summary["lines"] += 1
                            start = _to_point(item[1])
                            end = _to_point(item[2])
                            bbox = _bbox_from_points([start, end])
                            orientation = _orientation_from_points(start, end)
                            if role == "grid":
                                _safe_append(
                                    grid_map,
                                    GridAxis(
                                        asset_name=asset_name,
                                        orientation=orientation,
                                        coordinate=start.x if orientation == "vertical" else start.y,
                                        layer=layer_name,
                                        source_ref=source_ref,
                                        start=start,
                                        end=end,
                                        confidence=0.62,
                                    ),
                                    _MAX_GRID_AXES,
                                )
                            category = {
                                "wall": "wall_line",
                                "facade": "facade_line",
                            }.get(role, "vector_line")
                            _safe_append(
                                detected_entities,
                                DrawingEntityRecord(
                                    asset_name=asset_name,
                                    category=category,
                                    layer=layer_name,
                                    bbox=bbox,
                                    points=[start, end],
                                    source_ref=source_ref,
                                    confidence=0.55 if category == "vector_line" else 0.7,
                                ),
                                _MAX_DETECTED_ENTITIES,
                            )
                        elif operator in {"re", "qu", "c", "v", "y"}:
                            entity_summary["polylines"] += 1
                            bbox = _bbox_from_rect(drawing.get("rect"))
                            category = "block_region" if operator == "re" else "vector_outline"
                            if role in {"room", "space"}:
                                category = "room_boundary"
                                space_boundaries_detected += 1
                            elif role == "site_boundary":
                                category = "site_boundary"
                            _safe_append(
                                detected_entities,
                                DrawingEntityRecord(
                                    asset_name=asset_name,
                                    category=category,
                                    layer=layer_name,
                                    bbox=bbox,
                                    source_ref=source_ref,
                                    confidence=0.58 if category == "vector_outline" else 0.72,
                                ),
                                _MAX_DETECTED_ENTITIES,
                            )

            if any(mode == "scanned" for mode in pdf_page_modes):
                if ocr_available:
                    ocr_attempted = self._run_optional_pdf_ocr(doc, asset_name, text_items, dimension_details)
                else:
                    _safe_append(
                        pending_review,
                        PendingReviewItem(
                            asset_name=asset_name,
                            category="scanned_pdf_requires_ocr",
                            reason="Scanned PDF pages were detected, but OCR is not available in the current environment.",
                            severity="warning",
                        ),
                        _MAX_PENDING_REVIEW,
                    )
        finally:
            doc.close()

        layer_map = self._build_layer_map(asset_name, layer_stats)
        pdf_info = PdfAssetInfo(
            asset_name=asset_name,
            page_count=max(len(pdf_page_modes), 0),
            pdf_type=self._classify_pdf_document(pdf_page_modes),
            vector_page_count=sum(1 for mode in pdf_page_modes if mode == "vector"),
            scanned_page_count=sum(1 for mode in pdf_page_modes if mode == "scanned"),
            hybrid_page_count=sum(1 for mode in pdf_page_modes if mode == "hybrid"),
            image_page_count=sum(1 for mode in pdf_page_modes if mode in {"scanned", "hybrid"}),
            ocr_attempted=ocr_attempted,
            ocr_available=ocr_available,
        )
        origin = CoordinateReference(x=0.0, y=0.0, source="pdf_page_origin", asset_name=asset_name, confidence=0.45)

        return _AssetParseResult(
            kind="pdf",
            units=_extract_units_from_texts([item.text for item in text_items]),
            origin=origin,
            north_angle=north_angle,
            recognized_layers=recognized_layers,
            layer_map=layer_map,
            grid_map=grid_map,
            dimension_details=dimension_details,
            text_items=text_items,
            detected_entities=detected_entities,
            storey_candidates=storey_candidates,
            storey_elevations_m=storey_elevations,
            pdf_assets=[pdf_info],
            pending_review=pending_review,
            entity_summary=entity_summary,
            site_boundary_detected=any(item.category == "site_boundary" for item in detected_entities),
            space_boundaries_detected=space_boundaries_detected,
        )

    def _build_layer_map(
        self,
        asset_name: str,
        layer_stats: dict[str, dict[str, Any]],
    ) -> list[LayerMapEntry]:
        entries: list[LayerMapEntry] = []
        for layer_name in sorted(layer_stats):
            stats = layer_stats[layer_name]
            entries.append(
                LayerMapEntry(
                    asset_name=asset_name,
                    name=layer_name,
                    semantic_role=_guess_semantic_role(layer_name),
                    entity_count=int(stats["count"]),
                    entity_types=sorted(str(item) for item in stats["types"]),
                )
            )
        return entries

    def _finalize_asset_quality(self, asset_name: str, kind: str, result: _AssetParseResult) -> None:
        categories = {entity.category for entity in result.detected_entities}
        has_modeled_source_entities = any(_is_modeled_source_category(category) for category in categories)
        if kind == "cad" and "wall_line" not in categories and "wall_path" not in categories:
            _safe_append(
                result.pending_review,
                PendingReviewItem(
                    asset_name=asset_name,
                    category="wall_detection_low_confidence",
                    reason="No wall lines were confidently detected; check layer mapping and source drawing conventions.",
                    severity="warning",
                ),
                _MAX_PENDING_REVIEW,
            )
        if kind == "cad" and not has_modeled_source_entities:
            unresolved_proxy_layers = [
                entry.name
                for entry in result.layer_map
                if entry.semantic_role in _PROXY_REVIEW_ROLES and "ACAD_PROXY_ENTITY" in entry.entity_types
            ]
            if unresolved_proxy_layers:
                joined_layers = ", ".join(sorted(set(unresolved_proxy_layers))[:5])
                _safe_append(
                    result.pending_review,
                    PendingReviewItem(
                        asset_name=asset_name,
                        category="proxy_entities_unresolved",
                        reason=(
                            "Semantic CAD layers contain ACAD_PROXY_ENTITY objects that were not converted into "
                            f"modelable geometry: {joined_layers}."
                        ),
                        severity="warning",
                    ),
                    _MAX_PENDING_REVIEW,
                )
        if kind == "cad" and not result.grid_map:
            _safe_append(
                result.pending_review,
                PendingReviewItem(
                    asset_name=asset_name,
                    category="grid_not_detected",
                    reason="No grid axes were detected. Multi-view alignment may require manual confirmation.",
                    severity="warning",
                ),
                _MAX_PENDING_REVIEW,
            )
        if kind == "pdf" and result.pdf_assets and result.pdf_assets[0].pdf_type == "scanned":
            _safe_append(
                result.pending_review,
                PendingReviewItem(
                    asset_name=asset_name,
                    category="pdf_scan_review",
                    reason="This PDF appears to be scanned. Parsed geometry and text may be incomplete without OCR.",
                    severity="warning",
                ),
                _MAX_PENDING_REVIEW,
            )

    def _resolve_asset_path(self, asset: AssetRecord) -> Path | None:
        candidates: list[Path] = []
        if asset.path:
            candidates.append(Path(asset.path).expanduser())
        candidates.append(Path(asset.filename))
        candidates.append(self.workspace_root / asset.filename)
        if asset.path and not Path(asset.path).is_absolute():
            candidates.append(self.workspace_root / asset.path)

        seen: set[Path] = set()
        for candidate in candidates:
            normalized = candidate.resolve(strict=False)
            if normalized in seen:
                continue
            seen.add(normalized)
            if normalized.is_file():
                return normalized
        return None

    def _extract_dxf_text(self, entity: object) -> str | None:
        try:
            if entity.dxftype() == "MTEXT":
                return _normalize_text(entity.text)
            return _normalize_text(entity.dxf.text)
        except Exception:
            return None

    def _polyline_points(self, entity: object) -> list[Point2D]:
        try:
            if entity.dxftype() == "LWPOLYLINE":
                return [
                    Point2D(x=_round_float(point[0]) or 0.0, y=_round_float(point[1]) or 0.0)
                    for point in entity.get_points("xy")
                ]
            return [
                Point2D(
                    x=_round_float(vertex.dxf.location.x) or 0.0,
                    y=_round_float(vertex.dxf.location.y) or 0.0,
                )
                for vertex in entity.vertices
            ]
        except Exception:
            return []

    def _is_closed_polyline(self, entity: object, points: list[Point2D]) -> bool:
        if not points:
            return False
        try:
            if entity.dxftype() == "LWPOLYLINE":
                return bool(entity.closed) or (len(points) > 2 and points[0] == points[-1])
            return bool(entity.is_closed) or (len(points) > 2 and points[0] == points[-1])
        except Exception:
            return len(points) > 2 and points[0] == points[-1]

    def _read_dwg_version(self, path: Path) -> str | None:
        try:
            header = path.read_bytes()[:6].decode("ascii", errors="ignore")
        except OSError:
            return None
        if header in _DWG_VERSION_MAP:
            return f"{header} ({_DWG_VERSION_MAP[header]})"
        return header or None

    def _convert_dwg_to_dxf(self, source_path: Path, dwg_version: str | None) -> Path:
        version_code = "ACAD2018"
        if dwg_version:
            version_key = dwg_version.split(" ", 1)[0]
            version_code = odafc.map_version(version_key)

        remote_url = self._remote_converter_url()
        if remote_url:
            return self._convert_dwg_to_dxf_via_remote_service(
                source_path=source_path,
                dwg_version=dwg_version,
                version_code=version_code,
                remote_url=remote_url,
            )

        converter_path = self._find_odafc_executable()
        if converter_path is None:
            raise FileNotFoundError(
                "No DWG converter is configured. Set JIANMO_DWG_CONVERTER_URL for a remote Windows conversion service, "
                "or set JIANMO_ODAFC_PATH / install ODA File Converter locally."
            )

        command_prefix = self._build_odafc_command(converter_path)
        target_dir = self._converter_workspace / uuid.uuid4().hex
        target_dir.mkdir(parents=True, exist_ok=True)

        command = command_prefix + [
            str(source_path.parent),
            str(target_dir),
            version_code,
            "DXF",
            "0",
            "1",
            source_path.name,
        ]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=180,
                creationflags=creationflags,
            )
        except OSError as exc:
            shutil.rmtree(target_dir, ignore_errors=True)
            raise RuntimeError(f"Unable to launch ODA File Converter: {exc}") from exc
        if completed.returncode != 0:
            shutil.rmtree(target_dir, ignore_errors=True)
            stderr = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
            raise RuntimeError(stderr)

        converted_path = target_dir / source_path.with_suffix(".dxf").name
        if not converted_path.is_file():
            shutil.rmtree(target_dir, ignore_errors=True)
            raise RuntimeError("ODA File Converter completed without producing a DXF file.")
        return converted_path

    def _convert_dwg_to_dxf_via_remote_service(
        self,
        source_path: Path,
        dwg_version: str | None,
        version_code: str,
        remote_url: str,
    ) -> Path:
        target_dir = self._converter_workspace / uuid.uuid4().hex
        target_dir.mkdir(parents=True, exist_ok=True)

        headers = {
            "Content-Type": "application/octet-stream",
            "X-Source-Filename": source_path.name,
            "X-Target-Format": "DXF",
            "X-Output-Version": version_code,
        }
        if dwg_version:
            headers["X-DWG-Version"] = dwg_version

        token = os.getenv("JIANMO_DWG_CONVERTER_TOKEN") or os.getenv("JIANMO_WINDOWS_CONVERTER_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"

        request = urllib_request.Request(
            remote_url,
            data=source_path.read_bytes(),
            headers=headers,
            method="POST",
        )

        try:
            with urllib_request.urlopen(request, timeout=self._remote_converter_timeout()) as response:
                payload = response.read()
                content_type = response.headers.get("Content-Type", "")
                output_name = response.headers.get("X-Output-Filename") or source_path.with_suffix(".dxf").name
        except urllib_error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="ignore").strip()
            message = details or exc.reason or f"HTTP {exc.code}"
            shutil.rmtree(target_dir, ignore_errors=True)
            raise RuntimeError(f"Remote DWG conversion service returned HTTP {exc.code}: {message}") from exc
        except urllib_error.URLError as exc:
            shutil.rmtree(target_dir, ignore_errors=True)
            raise RuntimeError(f"Remote DWG conversion service request failed: {exc.reason}") from exc
        except OSError as exc:
            shutil.rmtree(target_dir, ignore_errors=True)
            raise RuntimeError(f"Remote DWG conversion service request failed: {exc}") from exc

        if not payload:
            shutil.rmtree(target_dir, ignore_errors=True)
            raise RuntimeError("Remote DWG conversion service returned an empty response.")

        if "application/json" in content_type.lower():
            try:
                body = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                shutil.rmtree(target_dir, ignore_errors=True)
                raise RuntimeError("Remote DWG conversion service returned invalid JSON.") from exc
            payload_text = str(body.get("dxf_text") or "")
            if not payload_text:
                error_message = body.get("detail") or body.get("error") or "missing dxf_text"
                shutil.rmtree(target_dir, ignore_errors=True)
                raise RuntimeError(f"Remote DWG conversion service returned invalid JSON payload: {error_message}")
            payload = payload_text.encode("utf-8")
            output_name = str(body.get("filename") or output_name)

        converted_path = target_dir / Path(output_name).name
        if converted_path.suffix.lower() != ".dxf":
            converted_path = converted_path.with_suffix(".dxf")
        converted_path.write_bytes(payload)
        return converted_path

    def _remote_converter_url(self) -> str | None:
        for env_name in (
            "JIANMO_DWG_CONVERTER_URL",
            "JIANMO_WINDOWS_CONVERTER_URL",
        ):
            value = os.getenv(env_name)
            if value:
                return value.strip()
        return None

    def _remote_converter_timeout(self) -> float:
        raw_value = os.getenv("JIANMO_DWG_CONVERTER_TIMEOUT", "180")
        try:
            timeout = float(raw_value)
        except ValueError:
            return 180.0
        return timeout if timeout > 0 else 180.0

    def _build_odafc_command(self, converter_path: Path) -> list[str]:
        system = platform.system()
        converter = converter_path.expanduser()

        if system == "Windows":
            return [str(converter)]

        if converter.suffix.lower() == ".exe":
            wine_path = shutil.which("wine")
            if wine_path is None:
                raise RuntimeError(
                    "Found Windows ODA File Converter, but 'wine' is not installed on this system. "
                    "Install wine or point JIANMO_ODAFC_PATH to a native Linux/macOS converter binary."
                )
            base_command = [wine_path, str(converter)]
        else:
            if not os.access(converter, os.X_OK):
                raise RuntimeError(
                    f"ODA File Converter at '{converter}' is not executable. "
                    "Grant execute permission or configure JIANMO_ODAFC_PATH with a runnable binary."
                )
            base_command = [str(converter)]

        if system == "Linux":
            xvfb_run = shutil.which("xvfb-run")
            if xvfb_run:
                return [xvfb_run, "-a", *base_command]
        return base_command

    def _find_odafc_executable(self) -> Path | None:
        env_candidates = [
            os.getenv("JIANMO_ODAFC_PATH"),
            os.getenv("ODA_FILE_CONVERTER_PATH"),
        ]
        for candidate in env_candidates:
            if candidate:
                path = Path(candidate).expanduser()
                if path.is_file():
                    return path

        which_path = shutil.which("ODAFileConverter")
        if which_path:
            return Path(which_path)

        workspace_candidates = [
            self.workspace_root / "tools" / "oda" / "ODAFileConverter.exe",
            self.workspace_root / "ODAFileConverter.exe",
        ]
        for candidate in workspace_candidates:
            if candidate.is_file():
                return candidate

        roots = [
            Path("C:/Program Files/ODA"),
            Path("C:/Program Files (x86)/ODA"),
        ]
        for root in roots:
            if not root.exists():
                continue
            matches = sorted(root.rglob("ODAFileConverter.exe"), reverse=True)
            if matches:
                return matches[0]
        return None

    def _classify_pdf_page(self, text_count: int, drawing_count: int, image_count: int) -> str:
        if image_count > 0 and drawing_count == 0 and text_count == 0:
            return "scanned"
        if image_count > 0 and (drawing_count > 0 or text_count > 0):
            return "hybrid"
        if drawing_count > 0 or text_count > 0:
            return "vector"
        return "unknown"

    def _classify_pdf_document(self, page_modes: list[str]) -> str:
        if not page_modes:
            return "unknown"
        unique_modes = set(page_modes)
        if unique_modes == {"vector"}:
            return "vector"
        if unique_modes == {"scanned"}:
            return "scanned"
        if unique_modes <= {"vector", "hybrid"} and "hybrid" in unique_modes:
            return "hybrid"
        if len(unique_modes) > 1:
            return "hybrid"
        return next(iter(unique_modes))

    def _ocr_is_available(self) -> bool:
        return (
            importlib.util.find_spec("pytesseract") is not None
            and importlib.util.find_spec("PIL") is not None
            and shutil.which("tesseract") is not None
        )

    def _run_optional_pdf_ocr(
        self,
        doc: fitz.Document,
        asset_name: str,
        text_items: list[TextAnnotationRecord],
        dimension_details: list[DimensionEntityRecord],
    ) -> bool:
        if not self._ocr_is_available():
            return False

        import pytesseract
        from PIL import Image

        attempted = False
        for page in doc:
            if len(text_items) >= _MAX_TEXT_ITEMS:
                break
            if page.get_text("text").strip():
                continue
            pixmap = page.get_pixmap(dpi=180)
            image = Image.open(io.BytesIO(pixmap.tobytes("png")))
            ocr_text = _normalize_text(pytesseract.image_to_string(image))
            attempted = True
            if not ocr_text:
                continue
            record = TextAnnotationRecord(
                asset_name=asset_name,
                text=f"page {page.number + 1}: {ocr_text}",
                semantic_tag="ocr_text",
                layer="PDF-OCR",
                page_index=page.number,
                bbox=_bbox_from_rect(page.rect),
                source_ref=f"page:{page.number + 1}:ocr",
            )
            _safe_append(text_items, record, _MAX_TEXT_ITEMS)
            for detail in _extract_dimension_records_from_text(
                asset_name,
                ocr_text,
                "PDF-OCR",
                _bbox_from_rect(page.rect),
                record.source_ref,
            ):
                _safe_append(dimension_details, detail, _MAX_DIMENSIONS)
        return attempted
