from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Iterable, List, TypeVar

from ..models import (
    BoundingBox2D,
    DrawingEntityRecord,
    DrawingFragmentRecord,
    PendingReviewItem,
    Point2D,
    StoreyCandidateRecord,
)
from ..storey_inference import infer_storey_key, storey_sort_key

_MAX_PENDING_REVIEW = 80


def _normalize_text(text: str) -> str:
    return " ".join(text.replace("\x00", " ").split()).strip()


def _round_float(value: float | int | None, digits: int = 3) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


T = TypeVar("T")


def _safe_append(items: List[T], value: T, limit: int) -> bool:
    if limit == _MAX_PENDING_REVIEW:
        items.append(value)
        return True
    if len(items) >= limit:
        return False
    items.append(value)
    return True


def explicit_storey_keys(candidates: Iterable[StoreyCandidateRecord]) -> List[str]:
    keys: List[str] = []
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


def _entity_bbox(entity: DrawingEntityRecord) -> BoundingBox2D | None:
    if entity.bbox is not None:
        return entity.bbox
    if entity.points:
        return BoundingBox2D(
            min_x=min(point.x for point in entity.points),
            min_y=min(point.y for point in entity.points),
            max_x=max(point.x for point in entity.points),
            max_y=max(point.y for point in entity.points),
        )
    return None


def _match_fragment_bbox(
    text_index: dict[tuple[str, str], BoundingBox2D],
    *,
    asset_name: str,
    title: str,
) -> BoundingBox2D | None:
    return text_index.get((asset_name, _normalize_text(title)))


def _role_from_fragment_title(asset_name: str, title: str) -> str:
    lowered = title.lower()
    if "剖面" in title or "section" in lowered:
        return "section"
    if "立面" in title or "elevation" in lowered:
        return "facade"
    if "平面" in title or "plan" in lowered or infer_storey_key(title) is not None:
        return "plan"
    return "plan"


def assign_entity_fragment(
    entity: DrawingEntityRecord,
    fragments: List[DrawingFragmentRecord],
) -> None:
    if not fragments or "fragment_id" in entity.metadata:
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
                    (anchor.x, anchor.y),
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
    fragments: List[DrawingFragmentRecord],
    detected_entities: List[DrawingEntityRecord],
) -> None:
    if not fragments:
        return

    geometry_categories = {"wall_line", "wall_path", "door_block", "window_block", "room_boundary"}
    fragments_by_asset: dict[str, List[DrawingFragmentRecord]] = defaultdict(list)
    for fragment in fragments:
        if fragment.fragment_role == "plan":
            fragments_by_asset[fragment.asset_name].append(fragment)

    entities_by_asset: dict[str, List[DrawingEntityRecord]] = defaultdict(list)
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


def build_drawing_fragments(
    detected_entities: List[DrawingEntityRecord],
    text_items: List[TextAnnotationRecord],
    storey_candidate_details: List[StoreyCandidateRecord],
) -> List[DrawingFragmentRecord]:
    text_index: dict[tuple[str, str], BoundingBox2D] = {}
    for item in text_items:
        if item.semantic_tag != "view_marker" or item.bbox is None:
            continue
        text_index.setdefault((item.asset_name, _normalize_text(item.text)), item.bbox)

    candidates_by_asset: dict[str, List[StoreyCandidateRecord]] = defaultdict(list)
    for candidate in storey_candidate_details:
        candidates_by_asset[candidate.asset_name].append(candidate)

    geometry_assets = sorted(
        {
            entity.asset_name
            for entity in detected_entities
            if entity.category
            in {"wall_line", "wall_path", "door_block", "window_block", "room_boundary"}
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
                    bbox=candidate_bbox or _match_fragment_bbox(
                        text_index, asset_name=asset_name, title=title
                    ),
                    source=source,
                )
            )

    _cluster_plan_fragment_bboxes(fragments, detected_entities)

    fragments_by_asset: dict[str, List[DrawingFragmentRecord]] = defaultdict(list)
    for fragment in fragments:
        fragments_by_asset[fragment.asset_name].append(fragment)

    for entity in detected_entities:
        if entity.category not in {"wall_line", "wall_path", "door_block", "window_block", "room_boundary"}:
            continue
        assign_entity_fragment(entity, fragments_by_asset.get(entity.asset_name, []))

    return fragments


def append_parse_diagnostics(
    detected_entities: List[DrawingEntityRecord],
    detected_entities_dropped: int,
    fragments: List[DrawingFragmentRecord],
    storey_candidate_details: List[StoreyCandidateRecord],
    pending_review: List[PendingReviewItem],
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
            storey_keys = explicit_storey_keys(candidates)
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


__all__ = [
    "explicit_storey_keys",
    "build_drawing_fragments",
    "append_parse_diagnostics",
    "assign_entity_fragment",
]
