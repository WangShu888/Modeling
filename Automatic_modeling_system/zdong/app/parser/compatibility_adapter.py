from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, TypeVar

from ..models import (
    CoordinateReference,
    DimensionEntityRecord,
    DrawingEntityRecord,
    DrawingFragmentRecord,
    GridAxis,
    LayerMapEntry,
    PdfAssetInfo,
    PendingReviewItem,
    ParsedDrawingModel,
    StoreyCandidateRecord,
    TextAnnotationRecord,
)
from .annotation_binder import bind_annotations
from .assembly_engine import AssemblyEngine
from .component_recognizer import extract_component_candidates
from .fragments import append_parse_diagnostics, assign_entity_fragment, build_drawing_fragments
from .validation_engine import ValidationEngine


_MAX_GRID_AXES = 80
_MAX_DIMENSIONS = 120
_MAX_TEXT_ITEMS = 120
_MAX_PENDING_REVIEW = 80

T = TypeVar("T")


def _safe_append(items: List[T], value: T, limit: int) -> bool:
    if limit <= 0:
        return False
    if len(items) >= limit:
        return False
    items.append(value)
    return True


def _dedupe_keep_order(items: Iterable[str]) -> List[str]:
    seen: set[str] = set()
    ordered: List[str] = []
    for item in items:
        if not item:
            continue
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def _format_annotation_summaries(items: List[TextAnnotationRecord], limit: int) -> List[str]:
    formatted = [f"{item.asset_name}: {item.text}" for item in items if item.text]
    return _dedupe_keep_order(formatted)[:limit]


def _entity_source_summary(detected_entities: Iterable[DrawingEntityRecord]) -> Dict[str, int]:
    summary: Dict[str, int] = {}
    for entity in detected_entities:
        key = entity.asset_name or "unknown_asset"
        summary[key] = summary.get(key, 0) + 1
    return summary


def _empty_entity_summary() -> Dict[str, int]:
    return {"lines": 0, "polylines": 0, "blocks": 0, "texts": 0, "dimensions": 0}


def _merge_counts(target: Dict[str, int], source: Dict[str, int]) -> Dict[str, int]:
    for key, value in source.items():
        target[key] = target.get(key, 0) + value
    return target


def _resolve_coordinate_reference(
    current: CoordinateReference,
    incoming: CoordinateReference | None,
    pending_review: List[PendingReviewItem],
    limit: int,
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
            limit,
        )
    return current


def _resolve_north_angle(
    current: float | None,
    incoming: float | None,
    asset_name: str,
    pending_review: List[PendingReviewItem],
    limit: int,
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
            limit,
        )
    return current


def _resolve_units(
    current: str,
    incoming: str | None,
    asset_name: str,
    pending_review: List[PendingReviewItem],
    initialized: bool,
    limit: int,
) -> tuple[str, bool]:
    if incoming is None:
        return current, initialized
    if not initialized:
        return incoming, True
    if incoming != current:
        _safe_append(
            pending_review,
            PendingReviewItem(
                asset_name=asset_name,
                category="unit_conflict",
                reason=f"Detected unit '{incoming}' conflicts with '{current}'.",
                severity="warning",
            ),
            limit,
        )
    return current, initialized


def _ensure_compat_metadata(entity: DrawingEntityRecord) -> None:
    fragment_id = entity.metadata.get("fragment_id") or entity.metadata.get("source_fragment_id")
    fragment_role = entity.metadata.get("fragment_role")
    fragment_storey_key = entity.metadata.get("fragment_storey_key")
    storey_key = entity.metadata.get("storey_key") or fragment_storey_key
    if not storey_key:
        storey_key = "1F"
    entity.metadata.setdefault("fragment_id", fragment_id or f"{entity.asset_name}::fragment::unknown")
    entity.metadata.setdefault("source_fragment_id", entity.metadata["fragment_id"])
    entity.metadata.setdefault("fragment_role", fragment_role or "unknown")
    entity.metadata.setdefault("source_fragment_role", entity.metadata["fragment_role"])
    entity.metadata.setdefault("fragment_storey_key", fragment_storey_key or storey_key)
    entity.metadata.setdefault("storey_key", storey_key)
    entity.metadata.setdefault("source_storey_key", entity.metadata["storey_key"])


def _annotation_anchor(annotation: TextAnnotationRecord) -> list[float] | None:
    if annotation.bbox is None:
        return None
    return [
        (annotation.bbox.min_x + annotation.bbox.max_x) / 2.0,
        (annotation.bbox.min_y + annotation.bbox.max_y) / 2.0,
    ]


def _synthesize_room_label_entities(
    annotations: Iterable[TextAnnotationRecord],
    fragments_by_asset: dict[str, list[DrawingFragmentRecord]],
) -> list[DrawingEntityRecord]:
    room_entities: list[DrawingEntityRecord] = []
    for annotation in annotations:
        if annotation.semantic_tag != "room_label" or annotation.bbox is None:
            continue
        anchor = _annotation_anchor(annotation)
        if anchor is None:
            continue
        entity = DrawingEntityRecord(
            asset_name=annotation.asset_name,
            category="room_label",
            layer=annotation.layer,
            label=annotation.text,
            bbox=annotation.bbox,
            points=[],
            source_ref=annotation.source_ref,
            confidence=0.65,
            metadata={"source_annotation_text": annotation.text},
        )
        assign_entity_fragment(entity, fragments_by_asset.get(annotation.asset_name, []))
        room_entities.append(entity)
    return room_entities


def _entity_identity(entity: DrawingEntityRecord) -> tuple[str, str, str, str]:
    return (
        entity.asset_name,
        entity.category,
        str(entity.source_ref or ""),
        str(entity.label or ""),
    )


def _merge_component_metadata(
    detected_entities: list[DrawingEntityRecord],
    annotations: list[TextAnnotationRecord],
) -> dict[str, int]:
    candidates = bind_annotations(extract_component_candidates(detected_entities), annotations)
    candidate_map = {_entity_identity(candidate): candidate for candidate in candidates}
    component_counts: dict[str, int] = {}
    for entity in detected_entities:
        candidate = candidate_map.get(_entity_identity(entity))
        if candidate is None:
            continue
        entity.metadata.update(candidate.metadata)
        entity.confidence = max(entity.confidence, candidate.confidence)
        component_type = str(entity.metadata.get("component_type") or "")
        if component_type:
            component_counts[component_type] = component_counts.get(component_type, 0) + 1
    return component_counts


@dataclass
class ParserAssetSnapshot:
    asset_name: str
    kind: str
    units: str | None = None
    origin: CoordinateReference | None = None
    north_angle: float | None = None
    recognized_layers: List[str] = field(default_factory=list)
    layer_map: List[LayerMapEntry] = field(default_factory=list)
    grid_map: List[GridAxis] = field(default_factory=list)
    dimension_details: List[DimensionEntityRecord] = field(default_factory=list)
    text_items: List[TextAnnotationRecord] = field(default_factory=list)
    detected_entities: List[DrawingEntityRecord] = field(default_factory=list)
    storey_candidate_details: List[StoreyCandidateRecord] = field(default_factory=list)
    storey_elevations_m: List[float] = field(default_factory=list)
    pending_review: List[PendingReviewItem] = field(default_factory=list)
    entity_summary: Dict[str, int] = field(default_factory=_empty_entity_summary)
    site_boundary_detected: bool = False
    space_boundaries_detected: int = 0
    pdf_assets: List[PdfAssetInfo] = field(default_factory=list)
    unresolved_entities: List[str] = field(default_factory=list)


@dataclass
class ParserCompatibilityContext:
    asset_results: List[ParserAssetSnapshot] = field(default_factory=list)
    bundle_units: str | None = None
    bundle_units_locked: bool = False
    bundle_north_angle: float | None = None
    default_origin: CoordinateReference | None = None


class ParserCompatibilityAdapter:
    def __init__(
        self,
        *,
        axis_limit: int = _MAX_GRID_AXES,
        dimension_limit: int = _MAX_DIMENSIONS,
        text_limit: int = _MAX_TEXT_ITEMS,
        pending_limit: int = _MAX_PENDING_REVIEW,
    ) -> None:
        self.axis_limit = axis_limit
        self.dimension_limit = dimension_limit
        self.text_limit = text_limit
        self.pending_limit = pending_limit

    def adapt(self, context: ParserCompatibilityContext) -> ParsedDrawingModel:
        assets = context.asset_results
        units = context.bundle_units or "mm"
        units_initialized = context.bundle_units_locked
        pending_review: List[PendingReviewItem] = []
        origin = context.default_origin or CoordinateReference()
        parsed_north_angle: float | None = None
        recognized_layers: set[str] = set()
        layer_map: List[LayerMapEntry] = []
        grid_map: List[GridAxis] = []
        dimension_details: List[DimensionEntityRecord] = []
        text_items: List[TextAnnotationRecord] = []
        detected_entities: List[DrawingEntityRecord] = []
        storey_candidate_details: List[StoreyCandidateRecord] = []
        storey_elevations: List[float] = []
        pdf_assets: List[PdfAssetInfo] = []
        unresolved_entities: List[str] = []
        entity_summary: Dict[str, int] = _empty_entity_summary()
        site_boundary_detected = False
        space_boundaries_detected = 0
        asset_kinds: List[str] = []

        for asset in assets:
            asset_kinds.append(asset.kind)
            recognized_layers.update(asset.recognized_layers)
            layer_map.extend(asset.layer_map)
            grid_map.extend(asset.grid_map)
            dimension_details.extend(asset.dimension_details)
            text_items.extend(asset.text_items)
            storey_candidate_details.extend(asset.storey_candidate_details)
            storey_elevations.extend(asset.storey_elevations_m)
            pdf_assets.extend(asset.pdf_assets)
            unresolved_entities.extend(asset.unresolved_entities)
            pending_review.extend(asset.pending_review)
            site_boundary_detected = site_boundary_detected or asset.site_boundary_detected
            space_boundaries_detected += asset.space_boundaries_detected
            _merge_counts(entity_summary, asset.entity_summary)
            detected_entities.extend([entity.model_copy(deep=True) for entity in asset.detected_entities])
            units, units_initialized = _resolve_units(
                units,
                asset.units,
                asset.asset_name,
                pending_review,
                units_initialized,
                self.pending_limit,
            )
            origin = _resolve_coordinate_reference(origin, asset.origin, pending_review, self.pending_limit)
            parsed_north_angle = _resolve_north_angle(
                parsed_north_angle,
                asset.north_angle,
                asset.asset_name,
                pending_review,
                self.pending_limit,
            )

        north_angle = context.bundle_north_angle if context.bundle_north_angle is not None else 0.0
        if context.bundle_north_angle is None and parsed_north_angle is not None:
            north_angle = parsed_north_angle

        grid_map_limited = grid_map[: self.axis_limit]
        dimension_details_limited = dimension_details[: self.dimension_limit]
        text_annotation_summaries = _format_annotation_summaries(text_items, self.text_limit)

        fragments = build_drawing_fragments(detected_entities, text_items, storey_candidate_details)
        fragments_by_asset: dict[str, list[DrawingFragmentRecord]] = {}
        for fragment in fragments:
            fragments_by_asset.setdefault(fragment.asset_name, []).append(fragment)
        room_label_entities = _synthesize_room_label_entities(text_items, fragments_by_asset)
        detected_entities.extend(room_label_entities)
        component_counts = _merge_component_metadata(detected_entities, text_items)
        detected_entities_total = len(detected_entities)
        detected_entities_emitted = detected_entities_total
        detected_entities_dropped = max(0, detected_entities_total - detected_entities_emitted)
        space_candidates_detected = space_boundaries_detected + len(room_label_entities)

        append_parse_diagnostics(
            detected_entities,
            detected_entities_dropped,
            fragments,
            storey_candidate_details,
            pending_review,
        )

        for entity in detected_entities:
            _ensure_compat_metadata(entity)

        assembly_result = AssemblyEngine().assemble(detected_entities, fragments=fragments, grid_axes=grid_map_limited)
        validation_outcome = ValidationEngine().validate(
            assembly_result,
            fragments=fragments,
            grid_axes=grid_map_limited,
        )
        for issue in [*validation_outcome.need_review_list.issues, *validation_outcome.blocked_issue_list.issues]:
            _safe_append(
                pending_review,
                PendingReviewItem(
                    category=f"parser.{issue.category}",
                    reason=issue.reason,
                    severity=issue.severity,
                    source_ref=issue.source_ref,
                ),
                self.pending_limit,
            )

        pending_review = pending_review[: self.pending_limit]
        unresolved_entities = _dedupe_keep_order(unresolved_entities)
        storey_candidates = _dedupe_keep_order([item.name for item in storey_candidate_details])
        storey_elevations_sorted = sorted({round(value, 3) for value in storey_elevations})
        detected_entities_source_summary = _entity_source_summary(detected_entities)
        pdf_modes_detected = _dedupe_keep_order(asset.pdf_type for asset in pdf_assets)

        return ParsedDrawingModel(
            assets_count=len(assets),
            asset_kinds=asset_kinds,
            units=units,
            origin=origin,
            recognized_layers=sorted(recognized_layers),
            layer_map=layer_map,
            grid_map=grid_map_limited,
            grid_lines_detected=len(grid_map_limited),
            dimension_entities=len(dimension_details_limited),
            dimension_details=dimension_details_limited,
            text_annotations=text_annotation_summaries,
            text_annotation_items=text_items,
            detected_entities=detected_entities,
            detected_entities_total=detected_entities_total,
            detected_entities_emitted=detected_entities_emitted,
            detected_entities_dropped=detected_entities_dropped,
            detected_entities_source_summary=detected_entities_source_summary,
            fragments=fragments,
            storey_candidates=storey_candidates,
            storey_candidate_details=storey_candidate_details,
            storey_elevations_m=storey_elevations_sorted,
            north_angle=north_angle,
            pdf_assets=pdf_assets,
            pdf_modes_detected=pdf_modes_detected,
            site_boundary_detected=site_boundary_detected,
            space_boundaries_detected=space_boundaries_detected,
            space_candidates_detected=space_candidates_detected,
            pending_review=pending_review,
            unresolved_entities=unresolved_entities,
            entity_summary=entity_summary,
            parser_analysis={
                "component_counts": component_counts,
                "room_label_entities": len(room_label_entities),
                "assembly": {
                    "storey_manifests": [
                        {
                            "storey_key": manifest.storey_key,
                            "component_count": manifest.component_count,
                            "duplicates": manifest.duplicates,
                            "grid_signature": manifest.grid_signature,
                            "bounding_box": manifest.bounding_box.model_dump(mode="json")
                            if manifest.bounding_box is not None
                            else None,
                        }
                        for manifest in assembly_result.storey_manifests
                    ],
                    "duplicate_signatures": list(assembly_result.duplicate_signatures),
                    "cross_layer_chains": [
                        {
                            "component_type": chain.component_type,
                            "storeys": list(chain.storeys),
                            "anchor": chain.anchor.model_dump(mode="json") if chain.anchor is not None else None,
                            "signature": chain.signature,
                        }
                        for chain in assembly_result.cross_layer_chains
                    ],
                },
                "validation": {
                    "ready": validation_outcome.model_ready_set.ready,
                    "storey_keys": list(validation_outcome.model_ready_set.storey_keys),
                    "grid_signature": validation_outcome.model_ready_set.grid_signature,
                    "cross_layer_ready": validation_outcome.model_ready_set.cross_layer_ready,
                    "need_review": [
                        {"category": issue.category, "reason": issue.reason, "severity": issue.severity}
                        for issue in validation_outcome.need_review_list.issues
                    ],
                    "blocked": [
                        {"category": issue.category, "reason": issue.reason, "severity": issue.severity}
                        for issue in validation_outcome.blocked_issue_list.issues
                    ],
                },
            },
        )
