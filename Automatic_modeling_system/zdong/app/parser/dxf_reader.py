from __future__ import annotations

import base64
from dataclasses import dataclass
import math
from typing import Any

from ezdxf import units as ezdxf_units
from ezdxf.document import Drawing
from ezdxf import proxygraphic as ezdxf_proxygraphic

from ..models import (
    BoundingBox2D,
    CoordinateReference,
    DimensionEntityRecord,
    DrawingEntityRecord,
    GridAxis,
    LayerMapEntry,
    PendingReviewItem,
    Point2D,
    StoreyCandidateRecord,
    TextAnnotationRecord,
)
from ..parser.common import (
    bbox_from_points,
    extract_dimension_records_from_text,
    extract_elevations,
    extract_grid_label,
    extract_north_angle,
    guess_semantic_role,
    normalize_text,
    orientation_from_points,
    round_float,
    safe_append,
    to_point,
)
from ..storey_inference import infer_storey_key

_MAX_GRID_AXES = 80
_MAX_DIMENSIONS = 120
_MAX_DETECTED_ENTITIES = 200
_MAX_PENDING_REVIEW = 80
_MAX_PROXY_FALLBACK_WALLS = 120
_MAX_PROXY_FALLBACK_OPENINGS = 120
_TCH_WALL_DECODE_TOLERANCE = 1e-3
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
_MODELED_COMPONENT_CATEGORIES = {
    "wall_line",
    "wall_path",
    "door_block",
    "window_block",
    "room_boundary",
    "room_label",
}


@dataclass
class DxfDocumentResult:
    asset_name: str
    units: str
    origin: CoordinateReference
    north_angle: float | None
    recognized_layers: set[str]
    layer_map: list[LayerMapEntry]
    grid_map: list[GridAxis]
    dimension_details: list[DimensionEntityRecord]
    text_items: list[TextAnnotationRecord]
    detected_entities: list[DrawingEntityRecord]
    storey_candidates: list[StoreyCandidateRecord]
    storey_elevations_m: list[float]
    pending_review: list[PendingReviewItem]
    entity_summary: dict[str, int]
    site_boundary_detected: bool
    space_boundaries_detected: int


@dataclass
class _TchWallSpec:
    layer: str
    source_ref: str | None
    width: float | None
    start: Point2D | None = None
    end: Point2D | None = None
    anchor: Point2D | None = None


class DxfDocumentReader:
    def parse_document(self, doc: Drawing, asset_name: str, descriptor: str) -> DxfDocumentResult:
        layer_stats: dict[str, dict[str, Any]] = {}
        grid_map: list[GridAxis] = []
        dimension_details: list[DimensionEntityRecord] = []
        text_items: list[TextAnnotationRecord] = []
        detected_entities: list[DrawingEntityRecord] = []
        storey_candidates = _descriptor_storey_candidates(asset_name, descriptor)
        storey_elevations: list[float] = []
        pending_review: list[PendingReviewItem] = []
        entity_summary = _empty_entity_summary()
        recognized_layers = {layer.dxf.name for layer in doc.layers}
        north_angle: float | None = None
        site_boundary_detected = False
        space_boundaries_detected = 0
        largest_closed_unknown_boundary: tuple[float, DrawingEntityRecord] | None = None
        layer_line_segments: dict[str, list[tuple[Point2D, Point2D]]] = {}
        generic_line_segments: list[tuple[Point2D, Point2D]] = []
        proxy_entities_total = 0
        proxy_entities_with_virtual = 0
        proxy_role_counts: dict[str, int] = {"wall": 0, "door": 0, "window": 0, "room": 0}
        proxy_role_layers: dict[str, dict[str, int]] = {
            "wall": {},
            "door": {},
            "window": {},
            "room": {},
        }
        tch_wall_specs: list[_TchWallSpec] = []

        for entity in doc.modelspace():
            dxftype = entity.dxftype()
            layer_name = self._resolve_entity_layer(entity)
            role = guess_semantic_role(layer_name)
            handle = str(getattr(entity.dxf, "handle", "") or "")
            source_ref = handle or None
            recognized_layers.add(layer_name)
            layer_stats.setdefault(layer_name, {"count": 0, "types": set()})
            layer_stats[layer_name]["count"] += 1
            layer_stats[layer_name]["types"].add(dxftype)

            if dxftype in {"LINE", "XLINE", "RAY"}:
                entity_summary["lines"] += 1
                start = to_point(getattr(entity.dxf, "start", (0, 0)))
                end = to_point(getattr(entity.dxf, "end", getattr(entity.dxf, "unit_vector", (0, 0))))
                bbox = bbox_from_points([start, end])
                layer_line_segments.setdefault(layer_name, []).append((start, end))
                generic_line_segments.append((start, end))
                orientation = orientation_from_points(start, end)
                if role == "grid":
                    safe_append(
                        grid_map,
                        GridAxis(
                            asset_name=asset_name,
                            orientation=orientation,
                            coordinate=start.x if orientation == "vertical" else start.y,
                            layer=layer_name,
                            source_ref=source_ref,
                            start=start,
                            end=end,
                            confidence=0.7,
                        ),
                        _MAX_GRID_AXES,
                    )
                category = {
                    "wall": "wall_line",
                    "site_boundary": "site_boundary_line",
                    "facade": "facade_line",
                }.get(role)
                if category:
                    _append_detected_entity(
                        detected_entities,
                        DrawingEntityRecord(
                            asset_name=asset_name,
                            category=category,
                            layer=layer_name,
                            bbox=bbox,
                            points=[start, end],
                            source_ref=source_ref,
                            confidence=0.75 if role == "wall" else 0.6,
                        ),
                    )

            elif dxftype in {"LWPOLYLINE", "POLYLINE"}:
                entity_summary["polylines"] += 1
                points = self._polyline_points(entity)
                bbox = bbox_from_points(points)
                closed = self._is_closed_polyline(entity, points)
                if len(points) >= 2:
                    for index in range(len(points) - 1):
                        start = points[index]
                        end = points[index + 1]
                        layer_line_segments.setdefault(layer_name, []).append((start, end))
                        generic_line_segments.append((start, end))
                    if closed and points[0] != points[-1]:
                        layer_line_segments.setdefault(layer_name, []).append((points[-1], points[0]))
                        generic_line_segments.append((points[-1], points[0]))
                if role == "grid" and len(points) >= 2:
                    orientation = orientation_from_points(points[0], points[-1])
                    safe_append(
                        grid_map,
                        GridAxis(
                            asset_name=asset_name,
                            orientation=orientation,
                            coordinate=points[0].x if orientation == "vertical" else points[0].y,
                            layer=layer_name,
                            source_ref=source_ref,
                            start=points[0],
                            end=points[-1],
                            confidence=0.65,
                        ),
                        _MAX_GRID_AXES,
                    )

                if closed and role in {"room", "space"}:
                    space_boundaries_detected += 1
                    _append_detected_entity(
                        detected_entities,
                        DrawingEntityRecord(
                            asset_name=asset_name,
                            category="room_boundary",
                            layer=layer_name,
                            bbox=bbox,
                            points=points,
                            source_ref=source_ref,
                            confidence=0.82,
                        ),
                    )
                elif closed and role == "site_boundary":
                    site_boundary_detected = True
                    _append_detected_entity(
                        detected_entities,
                        DrawingEntityRecord(
                            asset_name=asset_name,
                            category="site_boundary",
                            layer=layer_name,
                            bbox=bbox,
                            points=points,
                            source_ref=source_ref,
                            confidence=0.8,
                        ),
                    )
                elif role == "wall":
                    _append_detected_entity(
                        detected_entities,
                        DrawingEntityRecord(
                            asset_name=asset_name,
                            category="wall_path",
                            layer=layer_name,
                            bbox=bbox,
                            points=points,
                            source_ref=source_ref,
                            confidence=0.76,
                        ),
                    )
                elif closed and bbox is not None:
                    area = max((bbox.max_x - bbox.min_x) * (bbox.max_y - bbox.min_y), 0.0)
                    candidate = DrawingEntityRecord(
                        asset_name=asset_name,
                        category="boundary_candidate",
                        layer=layer_name,
                        bbox=bbox,
                        points=points,
                        source_ref=source_ref,
                        confidence=0.45,
                    )
                    if largest_closed_unknown_boundary is None or area > largest_closed_unknown_boundary[0]:
                        largest_closed_unknown_boundary = (area, candidate)

            elif dxftype == "ACAD_PROXY_ENTITY":
                proxy_entities_total += 1
                proxy_role = "room" if role in {"room", "space"} else role
                if proxy_role in proxy_role_counts:
                    proxy_role_counts[proxy_role] += 1
                    layer_counter = proxy_role_layers[proxy_role]
                    layer_counter[layer_name] = layer_counter.get(layer_name, 0) + 1

                virtual_entities = self._collect_proxy_virtual_entities(entity, doc)
                if virtual_entities:
                    proxy_entities_with_virtual += 1
                for index, virtual_entity in enumerate(virtual_entities):
                    self._consume_proxy_virtual_entity(
                        virtual_entity,
                        fallback_layer=layer_name,
                        fallback_role=proxy_role,
                        asset_name=asset_name,
                        detected_entities=detected_entities,
                        grid_map=grid_map,
                        layer_line_segments=layer_line_segments,
                        generic_line_segments=generic_line_segments,
                        source_ref=f"{source_ref}:proxy:{index}" if source_ref else f"proxy:{index}",
                    )

            elif dxftype == "INSERT":
                entity_summary["blocks"] += 1
                block_name = str(getattr(entity.dxf, "name", "") or "")
                insert_point = to_point(getattr(entity.dxf, "insert", (0, 0)))
                bbox = bbox_from_points([insert_point])
                block_role = guess_semantic_role(f"{block_name} {layer_name}")
                category = {
                    "door": "door_block",
                    "window": "window_block",
                    "grid": "grid_marker",
                }.get(block_role)
                if category:
                    _append_detected_entity(
                        detected_entities,
                        DrawingEntityRecord(
                            asset_name=asset_name,
                            category=category,
                            layer=layer_name,
                            label=block_name or None,
                            bbox=bbox,
                            points=[insert_point],
                            source_ref=source_ref,
                            confidence=0.85 if block_role in {"door", "window"} else 0.6,
                            metadata={"block_name": block_name},
                        ),
                    )
                if block_role == "grid":
                    safe_append(
                        grid_map,
                        GridAxis(
                            asset_name=asset_name,
                            label=block_name or None,
                            orientation="unknown",
                            coordinate=None,
                            layer=layer_name,
                            source_ref=source_ref,
                            start=insert_point,
                            confidence=0.55,
                        ),
                        _MAX_GRID_AXES,
                    )

            elif dxftype == "TCH_WALL":
                wall_spec = self._parse_tch_wall_spec(entity, layer_name=layer_name, source_ref=source_ref)
                if wall_spec is not None:
                    tch_wall_specs.append(wall_spec)

            elif dxftype == "TCH_OPENING":
                entity_summary["blocks"] += 1
                opening_entity = self._parse_tch_opening_entity(
                    entity,
                    asset_name=asset_name,
                    layer_name=layer_name,
                    source_ref=source_ref,
                )
                if opening_entity is not None:
                    _append_detected_entity(detected_entities, opening_entity)

            elif dxftype == "TCH_COLUMN":
                column_entity = self._parse_tch_column_entity(
                    entity,
                    asset_name=asset_name,
                    layer_name=layer_name,
                    source_ref=source_ref,
                )
                if column_entity is not None:
                    _append_detected_entity(detected_entities, column_entity)

            elif dxftype == "TCH_TEXT":
                entity_summary["texts"] += 1
                text = self._extract_tch_text(entity)
                if not text:
                    continue
                insert_point = self._extract_tch_text_point(entity)
                bbox = bbox_from_points([insert_point])
                semantic_tag = _classify_text_semantics(text, role)
                annotation = TextAnnotationRecord(
                    asset_name=asset_name,
                    text=text,
                    semantic_tag=semantic_tag,
                    layer=layer_name,
                    bbox=bbox,
                    source_ref=source_ref,
                )
                _append_text_item(text_items, annotation)

                grid_label = extract_grid_label(text)
                if grid_label:
                    safe_append(
                        grid_map,
                        GridAxis(
                            asset_name=asset_name,
                            label=grid_label,
                            orientation="unknown",
                            coordinate=None,
                            layer=layer_name,
                            source_ref=source_ref,
                            start=insert_point,
                            confidence=0.68,
                        ),
                        _MAX_GRID_AXES,
                    )

                for record in extract_dimension_records_from_text(
                    asset_name, text, layer_name, bbox, source_ref
                ):
                    safe_append(dimension_details, record, _MAX_DIMENSIONS)

                for elevation in extract_elevations(text):
                    storey_elevations.append(elevation)
                    _append_detected_entity(
                        detected_entities,
                        DrawingEntityRecord(
                            asset_name=asset_name,
                            category="elevation_marker",
                            layer=layer_name,
                            label=text,
                            bbox=bbox,
                            points=[insert_point],
                            source_ref=source_ref,
                            confidence=0.72,
                            metadata={"elevation_m": elevation, "source_entity_type": "TCH_TEXT"},
                        ),
                    )

                extracted_north_angle = extract_north_angle(text)
                if extracted_north_angle is not None:
                    north_angle = extracted_north_angle

                if semantic_tag == "view_marker":
                    _append_view_marker_candidates(
                        storey_candidates,
                        asset_name=asset_name,
                        text=text,
                        confidence=0.78,
                        source="drawing_text",
                        bbox=bbox,
                        source_ref=source_ref,
                    )
                elif semantic_tag == "room_label":
                    _append_detected_entity(
                        detected_entities,
                        DrawingEntityRecord(
                            asset_name=asset_name,
                            category="room_label",
                            layer=layer_name,
                            label=text,
                            bbox=bbox,
                            points=[insert_point],
                            source_ref=source_ref,
                            confidence=0.8,
                            metadata={"source_entity_type": "TCH_TEXT"},
                        ),
                    )

            elif dxftype in {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}:
                entity_summary["texts"] += 1
                text = self._extract_dxf_text(entity)
                if not text:
                    continue
                insert_point = to_point(
                    getattr(entity.dxf, "insert", getattr(entity.dxf, "location", (0, 0)))
                )
                bbox = bbox_from_points([insert_point])
                semantic_tag = _classify_text_semantics(text, role)
                annotation = TextAnnotationRecord(
                    asset_name=asset_name,
                    text=text,
                    semantic_tag=semantic_tag,
                    layer=layer_name,
                    bbox=bbox,
                    source_ref=source_ref,
                )
                _append_text_item(text_items, annotation)

                grid_label = extract_grid_label(text)
                if grid_label:
                    safe_append(
                        grid_map,
                        GridAxis(
                            asset_name=asset_name,
                            label=grid_label,
                            orientation="unknown",
                            coordinate=None,
                            layer=layer_name,
                            source_ref=source_ref,
                            start=insert_point,
                            confidence=0.68,
                        ),
                        _MAX_GRID_AXES,
                    )

                for record in extract_dimension_records_from_text(
                    asset_name, text, layer_name, bbox, source_ref
                ):
                    safe_append(dimension_details, record, _MAX_DIMENSIONS)

                for elevation in extract_elevations(text):
                    storey_elevations.append(elevation)
                    _append_detected_entity(
                        detected_entities,
                        DrawingEntityRecord(
                            asset_name=asset_name,
                            category="elevation_marker",
                            layer=layer_name,
                            label=text,
                            bbox=bbox,
                            points=[insert_point],
                            source_ref=source_ref,
                            confidence=0.72,
                            metadata={"elevation_m": elevation},
                        ),
                    )

                extracted_north_angle = extract_north_angle(text)
                if extracted_north_angle is not None:
                    north_angle = extracted_north_angle

                if semantic_tag == "view_marker":
                    _append_view_marker_candidates(
                        storey_candidates,
                        asset_name=asset_name,
                        text=text,
                        confidence=0.78,
                        source="drawing_text",
                        bbox=bbox,
                        source_ref=source_ref,
                    )

            elif "DIMENSION" in dxftype:
                entity_summary["dimensions"] += 1
                bbox = bbox_from_points(
                    [
                        to_point(getattr(entity.dxf, "defpoint2", (0, 0))),
                        to_point(getattr(entity.dxf, "defpoint3", (0, 0))),
                    ]
                )
                value = None
                try:
                    value = round_float(float(entity.get_measurement()), 3)
                except Exception:
                    value = None
                dim_text = str(getattr(entity.dxf, "text", "") or "").strip()
                if dim_text == "<>":
                    dim_text = None
                safe_append(
                    dimension_details,
                    DimensionEntityRecord(
                        asset_name=asset_name,
                        kind="cad_dimension",
                        text=dim_text,
                        value=value,
                        unit=None,
                        layer=layer_name,
                        bbox=bbox,
                        source_ref=source_ref,
                    ),
                    _MAX_DIMENSIONS,
                )

        if tch_wall_specs:
            self._emit_tch_wall_entities(
                asset_name=asset_name,
                wall_specs=tch_wall_specs,
                detected_entities=detected_entities,
                layer_line_segments=layer_line_segments,
                generic_line_segments=generic_line_segments,
            )

        if not site_boundary_detected and largest_closed_unknown_boundary is not None:
            _, candidate = largest_closed_unknown_boundary
            candidate.category = "site_boundary_candidate"
            _append_detected_entity(detected_entities, candidate)
            safe_append(
                pending_review,
                PendingReviewItem(
                    asset_name=asset_name,
                    category="site_boundary_inferred",
                    reason="A large closed polyline was inferred as a possible site boundary and should be confirmed.",
                    source_ref=candidate.source_ref,
                ),
                _MAX_PENDING_REVIEW,
            )

        has_modeled_components = any(
            entity.category in _MODELED_COMPONENT_CATEGORIES for entity in detected_entities
        )
        if proxy_entities_total > 0 and not has_modeled_components:
            generated = self._promote_proxy_entities_from_layer_geometry(
                asset_name=asset_name,
                detected_entities=detected_entities,
                proxy_role_counts=proxy_role_counts,
                proxy_role_layers=proxy_role_layers,
                layer_line_segments=layer_line_segments,
                generic_line_segments=generic_line_segments,
            )
            if generated == 0:
                generated = self._promote_proxy_entities_from_footprint(
                    asset_name=asset_name,
                    detected_entities=detected_entities,
                    proxy_role_counts=proxy_role_counts,
                    doc=doc,
                )

            if generated > 0:
                safe_append(
                    pending_review,
                    PendingReviewItem(
                        asset_name=asset_name,
                        category="proxy_geometry_approximated",
                        reason=(
                            "Proxy entities did not expose usable geometry; synthesized fallback wall/door/window "
                            "candidates from available non-proxy lines and drawing footprint."
                        ),
                        severity="warning",
                    ),
                    _MAX_PENDING_REVIEW,
                )
            else:
                safe_append(
                    pending_review,
                    PendingReviewItem(
                        asset_name=asset_name,
                        category="proxy_geometry_unresolved",
                        reason=(
                            "Proxy entities were detected but no usable virtual geometry or fallback footprint was "
                            "available for reconstruction."
                        ),
                        severity="warning",
                    ),
                    _MAX_PENDING_REVIEW,
                )

        if proxy_entities_total > 0 and proxy_entities_with_virtual == 0:
            safe_append(
                pending_review,
                PendingReviewItem(
                    asset_name=asset_name,
                    category="proxy_virtual_entities_empty",
                    reason="ACAD_PROXY_ENTITY virtual_entities() returned no usable geometry in this drawing.",
                    severity="warning",
                ),
                _MAX_PENDING_REVIEW,
            )

        layer_map = _build_layer_map(asset_name, layer_stats)
        origin = _origin_from_vec3(doc.header.get("$INSBASE", (0.0, 0.0, 0.0)), asset_name, "dxf_insbase")
        units = ezdxf_units.decode(int(doc.header.get("$INSUNITS", 0) or 0))

        return DxfDocumentResult(
            asset_name=asset_name,
            units=units,
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
            pending_review=pending_review,
            entity_summary=entity_summary,
            site_boundary_detected=site_boundary_detected,
            space_boundaries_detected=space_boundaries_detected,
        )

    def _extract_dxf_text(self, entity: Any) -> str | None:
        try:
            if entity.dxftype() == "MTEXT":
                return normalize_text(entity.text)
            return normalize_text(entity.dxf.text)
        except Exception:
            return None

    def _extract_tch_text(self, entity: Any) -> str | None:
        values = self._extended_tag_values(entity, 1)
        for value in values:
            text = normalize_text(str(value))
            if text:
                return text
        return None

    def _extract_tch_text_point(self, entity: Any) -> Point2D:
        values = self._extended_tag_values(entity, 10)
        if values:
            return to_point(values[0])
        return Point2D(x=0.0, y=0.0)

    def _polyline_points(self, entity: Any) -> list[Point2D]:
        try:
            if entity.dxftype() == "LWPOLYLINE":
                return [
                    Point2D(x=round_float(point[0]) or 0.0, y=round_float(point[1]) or 0.0)
                    for point in entity.get_points("xy")
                ]
            return [
                Point2D(
                    x=round_float(vertex.dxf.location.x) or 0.0,
                    y=round_float(vertex.dxf.location.y) or 0.0,
                )
                for vertex in entity.vertices
            ]
        except Exception:
            return []

    def _is_closed_polyline(self, entity: Any, points: list[Point2D]) -> bool:
        if not points:
            return False
        try:
            if entity.dxftype() == "LWPOLYLINE":
                return bool(entity.closed) or (len(points) > 2 and points[0] == points[-1])
            return bool(entity.is_closed) or (len(points) > 2 and points[0] == points[-1])
        except Exception:
            return len(points) > 2 and points[0] == points[-1]

    def _iter_extended_tags(self, entity: Any) -> list[Any]:
        tags = getattr(entity, "xtags", None)
        if tags is None:
            return []
        try:
            return list(tags)
        except Exception:
            return []

    def _extended_tag_values(self, entity: Any, code: int) -> list[Any]:
        return [tag.value for tag in self._iter_extended_tags(entity) if getattr(tag, "code", None) == code]

    def _resolve_entity_layer(self, entity: Any) -> str:
        layer_name = str(getattr(getattr(entity, "dxf", None), "layer", "") or "").strip()
        if layer_name:
            return layer_name
        for value in self._extended_tag_values(entity, 8):
            candidate = str(value or "").strip()
            if candidate:
                return candidate
        return "0"

    def _parse_tch_wall_spec(
        self,
        entity: Any,
        *,
        layer_name: str,
        source_ref: str | None,
    ) -> _TchWallSpec | None:
        encoded_values = self._extended_tag_values(entity, 300)
        if not encoded_values:
            return None
        decoded = self._decode_tch_wall_vector(encoded_values[0])
        if decoded is None:
            return None
        x1, x2, y1, y2 = decoded
        width = None
        width_values = self._extended_tag_values(entity, 42)
        if width_values:
            try:
                width = float(width_values[0])
            except (TypeError, ValueError):
                width = None

        if abs(x1 - x2) <= _TCH_WALL_DECODE_TOLERANCE and abs(y1 - y2) <= _TCH_WALL_DECODE_TOLERANCE:
            return _TchWallSpec(
                layer=layer_name,
                source_ref=source_ref,
                width=width,
                anchor=Point2D(x=round_float(x1) or 0.0, y=round_float(y1) or 0.0),
            )
        return _TchWallSpec(
            layer=layer_name,
            source_ref=source_ref,
            width=width,
            start=Point2D(x=round_float(x1) or 0.0, y=round_float(y1) or 0.0),
            end=Point2D(x=round_float(x2) or 0.0, y=round_float(y2) or 0.0),
        )

    def _decode_tch_wall_vector(self, encoded: Any) -> tuple[float, float, float, float] | None:
        if not encoded:
            return None
        try:
            raw = base64.b64decode(str(encoded))
            decoded = raw.decode("utf-16le")
            values = [float(value) for value in decoded.split(",")[:4]]
        except Exception:
            return None
        if len(values) < 4:
            return None
        return values[0], values[1], values[2], values[3]

    def _emit_tch_wall_entities(
        self,
        *,
        asset_name: str,
        wall_specs: list[_TchWallSpec],
        detected_entities: list[DrawingEntityRecord],
        layer_line_segments: dict[str, list[tuple[Point2D, Point2D]]],
        generic_line_segments: list[tuple[Point2D, Point2D]],
    ) -> None:
        vertical_nodes: dict[float, set[float]] = {}

        for spec in wall_specs:
            if spec.start is None or spec.end is None:
                if spec.anchor is not None:
                    vertical_nodes.setdefault(spec.anchor.x, set()).add(spec.anchor.y)
                continue

            start = spec.start
            end = spec.end
            if start == end:
                vertical_nodes.setdefault(start.x, set()).add(start.y)
                continue

            layer_line_segments.setdefault(spec.layer, []).append((start, end))
            generic_line_segments.append((start, end))
            _append_detected_entity(
                detected_entities,
                DrawingEntityRecord(
                    asset_name=asset_name,
                    category="wall_line",
                    layer=spec.layer,
                    bbox=bbox_from_points([start, end]),
                    points=[start, end],
                    source_ref=spec.source_ref,
                    confidence=0.84,
                    metadata={"source_entity_type": "TCH_WALL", "tch_wall_width": spec.width},
                ),
            )
            if abs(start.x - end.x) <= _TCH_WALL_DECODE_TOLERANCE:
                vertical_nodes.setdefault(start.x, set()).update({start.y, end.y})
            elif abs(start.y - end.y) <= _TCH_WALL_DECODE_TOLERANCE:
                vertical_nodes.setdefault(start.x, set()).add(start.y)
                vertical_nodes.setdefault(end.x, set()).add(end.y)

        for x_coord in sorted(vertical_nodes):
            y_values = sorted(vertical_nodes[x_coord])
            if len(y_values) < 2:
                continue
            for index in range(len(y_values) - 1):
                start = Point2D(x=x_coord, y=y_values[index])
                end = Point2D(x=x_coord, y=y_values[index + 1])
                if start == end:
                    continue
                layer_line_segments.setdefault("WALL", []).append((start, end))
                generic_line_segments.append((start, end))
                _append_detected_entity(
                    detected_entities,
                    DrawingEntityRecord(
                        asset_name=asset_name,
                        category="wall_line",
                        layer="WALL",
                        bbox=bbox_from_points([start, end]),
                        points=[start, end],
                        source_ref=f"tch-wall-vertical:{round_float(x_coord)}:{index + 1}",
                        confidence=0.72,
                        metadata={"source_entity_type": "TCH_WALL", "tch_reconstructed_axis": "vertical"},
                    ),
                )

    def _parse_tch_opening_entity(
        self,
        entity: Any,
        *,
        asset_name: str,
        layer_name: str,
        source_ref: str | None,
    ) -> DrawingEntityRecord | None:
        opening_points = self._extended_tag_values(entity, 10)
        extent_points = self._extended_tag_values(entity, 11)
        points = [to_point(point) for point in opening_points[:1] + extent_points[:1]]
        if not points:
            return None
        category = self._classify_tch_opening_category(entity, layer_name)
        opening_width = self._first_tag_float(entity, 40)
        opening_height = self._first_tag_float(entity, 41)
        metadata = {
            "source_entity_type": "TCH_OPENING",
            "tch_library_ref": next((str(value) for value in self._extended_tag_values(entity, 1) if value), None),
            "tch_family_ref": next((str(value) for value in self._extended_tag_values(entity, 2) if value), None),
            "tch_parent_handles": [str(value) for value in self._extended_tag_values(entity, 330)],
            "tch_opening_width": opening_width,
            "tch_opening_height": opening_height,
        }
        return DrawingEntityRecord(
            asset_name=asset_name,
            category=category,
            layer=layer_name,
            bbox=bbox_from_points(points),
            points=points,
            source_ref=source_ref,
            confidence=0.82,
            metadata=metadata,
        )

    def _classify_tch_opening_category(self, entity: Any, layer_name: str) -> str:
        signals = [
            str(value)
            for code in (1, 2, 7)
            for value in self._extended_tag_values(entity, code)
            if value is not None
        ]
        lowered = " ".join(signals + [layer_name]).lower()
        if any(token in lowered for token in ("dorlib", "door", "门")):
            return "door_block"
        if any(token in lowered for token in ("winlib", "window", "wind", "窗")):
            return "window_block"
        return "window_block" if guess_semantic_role(layer_name) == "window" else "door_block"

    def _parse_tch_column_entity(
        self,
        entity: Any,
        *,
        asset_name: str,
        layer_name: str,
        source_ref: str | None,
    ) -> DrawingEntityRecord | None:
        points = [to_point(point) for point in self._extended_tag_values(entity, 10)]
        if len(points) < 2:
            return None
        return DrawingEntityRecord(
            asset_name=asset_name,
            category="column_path",
            layer=layer_name,
            bbox=bbox_from_points(points),
            points=points,
            source_ref=source_ref,
            confidence=0.8,
            metadata={"source_entity_type": "TCH_COLUMN"},
        )

    def _first_tag_float(self, entity: Any, code: int) -> float | None:
        values = self._extended_tag_values(entity, code)
        if not values:
            return None
        try:
            return float(values[0])
        except (TypeError, ValueError):
            return None

    def _collect_proxy_virtual_entities(self, entity: Any, doc: Drawing) -> list[Any]:
        virtual_entities: list[Any] = []
        try:
            virtual_entities = list(entity.virtual_entities())
        except Exception:
            virtual_entities = []
        if virtual_entities:
            return virtual_entities

        tags = getattr(entity, "acdb_proxy_entity", None)
        if tags is None:
            return []

        for length_code, data_code in ((160, 310), (162, 311), (161, 310)):
            try:
                proxy_bytes = ezdxf_proxygraphic.load_proxy_graphic(
                    tags, length_code=length_code, data_code=data_code
                )
            except Exception:
                continue
            if not proxy_bytes or len(proxy_bytes) <= 8:
                continue
            try:
                parsed = list(ezdxf_proxygraphic.ProxyGraphic(proxy_bytes, doc).virtual_entities())
            except Exception:
                continue
            if parsed:
                return parsed
        return []

    def _consume_proxy_virtual_entity(
        self,
        entity: Any,
        *,
        fallback_layer: str,
        fallback_role: str,
        asset_name: str,
        detected_entities: list[DrawingEntityRecord],
        grid_map: list[GridAxis],
        layer_line_segments: dict[str, list[tuple[Point2D, Point2D]]],
        generic_line_segments: list[tuple[Point2D, Point2D]],
        source_ref: str | None,
    ) -> None:
        dxftype = entity.dxftype()
        layer_name = str(getattr(getattr(entity, "dxf", None), "layer", "") or fallback_layer)
        role = guess_semantic_role(layer_name)
        if role == "unknown":
            role = fallback_role

        if dxftype in {"LINE", "XLINE", "RAY"}:
            start = to_point(getattr(entity.dxf, "start", (0, 0)))
            end = to_point(getattr(entity.dxf, "end", getattr(entity.dxf, "unit_vector", (0, 0))))
            layer_line_segments.setdefault(layer_name, []).append((start, end))
            generic_line_segments.append((start, end))
            if role == "wall":
                _append_detected_entity(
                    detected_entities,
                    DrawingEntityRecord(
                        asset_name=asset_name,
                        category="wall_line",
                        layer=layer_name,
                        bbox=bbox_from_points([start, end]),
                        points=[start, end],
                        source_ref=source_ref,
                        confidence=0.55,
                        metadata={"proxy_virtual": True},
                    ),
                )
            return

        if dxftype in {"LWPOLYLINE", "POLYLINE"}:
            points = self._polyline_points(entity)
            if len(points) >= 2:
                for index in range(len(points) - 1):
                    segment = (points[index], points[index + 1])
                    layer_line_segments.setdefault(layer_name, []).append(segment)
                    generic_line_segments.append(segment)
            closed = self._is_closed_polyline(entity, points)
            if role == "wall":
                _append_detected_entity(
                    detected_entities,
                    DrawingEntityRecord(
                        asset_name=asset_name,
                        category="wall_path",
                        layer=layer_name,
                        bbox=bbox_from_points(points),
                        points=points,
                        source_ref=source_ref,
                        confidence=0.58,
                        metadata={"proxy_virtual": True},
                    ),
                )
            elif closed and role in {"room", "space"}:
                _append_detected_entity(
                    detected_entities,
                    DrawingEntityRecord(
                        asset_name=asset_name,
                        category="room_boundary",
                        layer=layer_name,
                        bbox=bbox_from_points(points),
                        points=points,
                        source_ref=source_ref,
                        confidence=0.55,
                        metadata={"proxy_virtual": True},
                    ),
                )
            return

        if dxftype == "INSERT":
            insert_point = to_point(getattr(entity.dxf, "insert", (0, 0)))
            block_name = str(getattr(entity.dxf, "name", "") or "")
            block_role = guess_semantic_role(f"{block_name} {layer_name}")
            if block_role == "unknown":
                block_role = role
            category = {"door": "door_block", "window": "window_block", "grid": "grid_marker"}.get(block_role)
            if category:
                _append_detected_entity(
                    detected_entities,
                    DrawingEntityRecord(
                        asset_name=asset_name,
                        category=category,
                        layer=layer_name,
                        label=block_name or None,
                        bbox=bbox_from_points([insert_point]),
                        points=[insert_point],
                        source_ref=source_ref,
                        confidence=0.6 if block_role in {"door", "window"} else 0.5,
                        metadata={"proxy_virtual": True, "block_name": block_name},
                    ),
                )
            if block_role == "grid":
                safe_append(
                    grid_map,
                    GridAxis(
                        asset_name=asset_name,
                        label=block_name or None,
                        orientation="unknown",
                        coordinate=None,
                        layer=layer_name,
                        source_ref=source_ref,
                        start=insert_point,
                        confidence=0.5,
                    ),
                    _MAX_GRID_AXES,
                )
            return

        if dxftype in {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}:
            text = self._extract_dxf_text(entity)
            if not text:
                return
            insert_point = to_point(getattr(entity.dxf, "insert", getattr(entity.dxf, "location", (0, 0))))
            semantic_tag = _classify_text_semantics(text, role)
            if semantic_tag == "room_label":
                _append_detected_entity(
                    detected_entities,
                    DrawingEntityRecord(
                        asset_name=asset_name,
                        category="room_label",
                        layer=layer_name,
                        label=text,
                        bbox=bbox_from_points([insert_point]),
                        points=[insert_point],
                        source_ref=source_ref,
                        confidence=0.52,
                        metadata={"proxy_virtual": True},
                    ),
                )

    def _promote_proxy_entities_from_layer_geometry(
        self,
        *,
        asset_name: str,
        detected_entities: list[DrawingEntityRecord],
        proxy_role_counts: dict[str, int],
        proxy_role_layers: dict[str, dict[str, int]],
        layer_line_segments: dict[str, list[tuple[Point2D, Point2D]]],
        generic_line_segments: list[tuple[Point2D, Point2D]],
    ) -> int:
        generated = 0
        wall_count = max(int(proxy_role_counts.get("wall", 0)), 0)
        door_count = max(int(proxy_role_counts.get("door", 0)), 0)
        window_count = max(int(proxy_role_counts.get("window", 0)), 0)
        if wall_count + door_count + window_count <= 0:
            return 0

        wall_segments: list[tuple[Point2D, Point2D]] = []
        for layer_name, _ in sorted(
            proxy_role_layers.get("wall", {}).items(), key=lambda item: item[1], reverse=True
        ):
            wall_segments.extend(layer_line_segments.get(layer_name, []))
        if not wall_segments:
            wall_segments = list(generic_line_segments)
        if not wall_segments:
            return 0

        wall_target = min(max(wall_count, 4 if (door_count or window_count) else 0), _MAX_PROXY_FALLBACK_WALLS)
        for index in range(wall_target):
            start, end = wall_segments[index % len(wall_segments)]
            _append_detected_entity(
                detected_entities,
                DrawingEntityRecord(
                    asset_name=asset_name,
                    category="wall_line",
                    layer="PROXY_FALLBACK",
                    bbox=bbox_from_points([start, end]),
                    points=[start, end],
                    source_ref=f"proxy-fallback-wall:{index + 1}",
                    confidence=0.45,
                    metadata={"proxy_fallback": True, "strategy": "layer_lines"},
                ),
            )
            generated += 1

        opening_segments = list(wall_segments)
        if not opening_segments:
            return generated

        for role, count, category in (
            ("door", door_count, "door_block"),
            ("window", window_count, "window_block"),
        ):
            target = min(count, _MAX_PROXY_FALLBACK_OPENINGS)
            for index in range(target):
                segment = opening_segments[index % len(opening_segments)]
                point = _segment_midpoint(segment[0], segment[1])
                _append_detected_entity(
                    detected_entities,
                    DrawingEntityRecord(
                        asset_name=asset_name,
                        category=category,
                        layer="PROXY_FALLBACK",
                        bbox=bbox_from_points([point]),
                        points=[point],
                        source_ref=f"proxy-fallback-{role}:{index + 1}",
                        confidence=0.42,
                        metadata={"proxy_fallback": True, "strategy": "layer_lines"},
                    ),
                )
                generated += 1

        return generated

    def _promote_proxy_entities_from_footprint(
        self,
        *,
        asset_name: str,
        detected_entities: list[DrawingEntityRecord],
        proxy_role_counts: dict[str, int],
        doc: Drawing,
    ) -> int:
        bbox = self._resolve_proxy_fallback_bbox(detected_entities, doc)
        if bbox is None:
            return 0

        generated = 0
        min_x, max_x = bbox.min_x, bbox.max_x
        min_y, max_y = bbox.min_y, bbox.max_y
        width = max(max_x - min_x, 1.0)
        height = max(max_y - min_y, 1.0)

        wall_count = min(max(int(proxy_role_counts.get("wall", 0)), 4), _MAX_PROXY_FALLBACK_WALLS)
        for index in range(wall_count):
            y = min_y + ((index + 1) * height / (wall_count + 1))
            start = Point2D(x=min_x, y=y)
            end = Point2D(x=max_x, y=y)
            _append_detected_entity(
                detected_entities,
                DrawingEntityRecord(
                    asset_name=asset_name,
                    category="wall_line",
                    layer="PROXY_FALLBACK",
                    bbox=bbox_from_points([start, end]),
                    points=[start, end],
                    source_ref=f"proxy-footprint-wall:{index + 1}",
                    confidence=0.35,
                    metadata={"proxy_fallback": True, "strategy": "footprint"},
                ),
            )
            generated += 1

        for role, category, edge_y in (
            ("door", "door_block", min_y),
            ("window", "window_block", max_y),
        ):
            count = min(max(int(proxy_role_counts.get(role, 0)), 0), _MAX_PROXY_FALLBACK_OPENINGS)
            for index in range(count):
                x = min_x + ((index + 1) * width / (count + 1 if count > 0 else 1))
                point = Point2D(x=x, y=edge_y)
                _append_detected_entity(
                    detected_entities,
                    DrawingEntityRecord(
                        asset_name=asset_name,
                        category=category,
                        layer="PROXY_FALLBACK",
                        bbox=bbox_from_points([point]),
                        points=[point],
                        source_ref=f"proxy-footprint-{role}:{index + 1}",
                        confidence=0.32,
                        metadata={"proxy_fallback": True, "strategy": "footprint"},
                    ),
                )
                generated += 1

        return generated

    def _resolve_proxy_fallback_bbox(
        self, detected_entities: list[DrawingEntityRecord], doc: Drawing
    ) -> BoundingBox2D | None:
        footprint_bbox: BoundingBox2D | None = None
        largest_area = 0.0
        for entity in detected_entities:
            if entity.category not in {"site_boundary", "site_boundary_candidate", "boundary_candidate"}:
                continue
            if entity.bbox is None:
                continue
            area = max((entity.bbox.max_x - entity.bbox.min_x) * (entity.bbox.max_y - entity.bbox.min_y), 0.0)
            if area > largest_area:
                largest_area = area
                footprint_bbox = entity.bbox
        if footprint_bbox is not None:
            return footprint_bbox

        ext_min = doc.header.get("$EXTMIN")
        ext_max = doc.header.get("$EXTMAX")
        try:
            min_x = float(ext_min[0])
            min_y = float(ext_min[1])
            max_x = float(ext_max[0])
            max_y = float(ext_max[1])
        except Exception:
            return None
        if not all(math.isfinite(value) for value in (min_x, min_y, max_x, max_y)):
            return None
        if max_x <= min_x or max_y <= min_y:
            return None
        return BoundingBox2D(min_x=min_x, min_y=min_y, max_x=max_x, max_y=max_y)


def _empty_entity_summary() -> dict[str, int]:
    return {"lines": 0, "polylines": 0, "blocks": 0, "texts": 0, "dimensions": 0}


def _segment_midpoint(start: Point2D, end: Point2D) -> Point2D:
    return Point2D(x=round_float((start.x + end.x) / 2.0) or 0.0, y=round_float((start.y + end.y) / 2.0) or 0.0)


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
    normalized = normalize_text(text)
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


def _build_layer_map(asset_name: str, layer_stats: dict[str, dict[str, Any]]) -> list[LayerMapEntry]:
    entries: list[LayerMapEntry] = []
    for layer_name in sorted(layer_stats):
        stats = layer_stats[layer_name]
        entries.append(
            LayerMapEntry(
                asset_name=asset_name,
                name=layer_name,
                semantic_role=guess_semantic_role(layer_name),
                entity_count=int(stats["count"]),
                entity_types=sorted(str(item) for item in stats["types"]),
            )
        )
    return entries


def _append_detected_entity(
    detected_entities: list[DrawingEntityRecord],
    entity: DrawingEntityRecord,
) -> bool:
    # Keep the full parsed entity set for diagnostics and later reconciliation.
    detected_entities.append(entity)
    return True


def _append_text_item(text_items: list[TextAnnotationRecord], annotation: TextAnnotationRecord) -> bool:
    # Keep full text annotations so room labels and view markers remain available downstream.
    text_items.append(annotation)
    return True


def _origin_from_vec3(value: Any, asset_name: str, source: str) -> CoordinateReference:
    try:
        x, y, z = value
        return CoordinateReference(x=float(x), y=float(y), z=float(z), source_ref=f"{asset_name}:{source}")
    except Exception:
        return CoordinateReference(source_ref=f"{asset_name}:{source}")


def _classify_text_semantics(text: str, layer_role: str | None = None) -> str:
    normalized = normalize_text(text)
    lowered = normalized.lower()
    if extract_north_angle(normalized) is not None:
        return "north_angle"
    if extract_elevations(normalized):
        return "elevation"
    if any(token in lowered for token in ("plan", "section", "elevation", "floor")):
        return "view_marker"
    if any(token in normalized for token in ("\u5e73\u9762", "\u5256\u9762", "\u7acb\u9762", "\u6807\u51c6\u5c42")):
        return "view_marker"
    if extract_dimension_records_from_text("asset", normalized, None, None):
        return "dimension"
    if extract_grid_label(normalized):
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
