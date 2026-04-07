from __future__ import annotations

import re
from typing import Iterable

import ezdxf
from ezdxf.document import Drawing

from ..models import GridAxis, Point2D


_GRID_LAYER_RE = re.compile(r"(?:^|[-_.\s])(axis|grid)(?:$|[-_.\s])|\u8f74", re.IGNORECASE)
_GRID_LABEL_RE = re.compile(
    r"(?i)(?:\b(?:axis|grid)\b\s*([A-Z0-9-]+)|\b([A-Z]{1,3}-\d{1,3})\b|\b([A-Z]{1,3})\b|\b(\d{1,3})\b)"
)


def _to_point(value: Iterable[float] | None) -> Point2D:
    if value is None:
        return Point2D(x=0.0, y=0.0)
    coords = list(value)
    if not coords:
        return Point2D(x=0.0, y=0.0)
    return Point2D(x=float(coords[0]), y=float(coords[1]) if len(coords) > 1 else 0.0)


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


def _guess_semantic_role(text: str | None) -> str:
    if not text:
        return "unknown"
    lowered = text.lower()
    if "grid" in lowered or "axis" in lowered or "轴" in lowered:
        return "grid"
    return "unknown"


def _extract_grid_label(text: str) -> str | None:
    match = _GRID_LABEL_RE.search(text)
    if not match:
        return None
    groups = [group for group in match.groups() if group]
    if not groups:
        return None
    return groups[0].upper()


def _is_grid_layer(layer_name: str | None) -> bool:
    if not layer_name:
        return False
    return bool(_GRID_LAYER_RE.search(layer_name))


def _safe_append_axes(axes: list[GridAxis], axis: GridAxis, limit: int) -> None:
    if len(axes) >= limit:
        return
    axes.append(axis)


class GridRecognizer:
    def __init__(self, axis_limit: int = 80) -> None:
        self.axis_limit = axis_limit

    def extract(self, doc: Drawing, asset_name: str) -> list[GridAxis]:
        axes: list[GridAxis] = []
        for entity in doc.modelspace():
            self._process_entity(asset_name, entity, axes)
            if len(axes) >= self.axis_limit:
                break
        return axes

    def _process_entity(self, asset_name: str, entity: ezdxf.entities.DXFGraphic, axes: list[GridAxis]) -> None:
        dxftype = entity.dxftype()
        layer_name = str(getattr(entity.dxf, "layer", "") or "0")
        source_ref = str(getattr(entity.dxf, "handle", "")) or None

        if dxftype in {"LINE", "XLINE", "RAY"} and _is_grid_layer(layer_name):
            self._append_axis_from_points(
                asset_name,
                layer_name,
                source_ref,
                self._line_endpoints(entity),
                axes,
            )
            return

        if dxftype in {"LWPOLYLINE", "POLYLINE"} and _is_grid_layer(layer_name):
            points = self._polyline_points(entity)
            if len(points) >= 2:
                self._append_axis_from_points(asset_name, layer_name, source_ref, (points[0], points[-1]), axes)
            return

        if dxftype == "INSERT":
            block_name = str(getattr(entity.dxf, "name", ""))
            role = _guess_semantic_role(f"{block_name} {layer_name}")
            if role == "grid":
                insert_point = _to_point(getattr(entity.dxf, "insert", (0, 0)))
                axis = GridAxis(
                    asset_name=asset_name,
                    label=block_name or None,
                    orientation="unknown",
                    coordinate=None,
                    layer=layer_name,
                    source_ref=source_ref,
                    start=insert_point,
                    confidence=0.55,
                )
                _safe_append_axes(axes, axis, self.axis_limit)
            return

        if dxftype in {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}:
            text = self._extract_text(entity)
            if not text:
                return
            label = _extract_grid_label(text)
            if label:
                insert_point = _to_point(getattr(entity.dxf, "insert", getattr(entity.dxf, "location", (0, 0))))
                axis = GridAxis(
                    asset_name=asset_name,
                    label=label,
                    orientation="unknown",
                    coordinate=None,
                    layer=layer_name,
                    source_ref=source_ref,
                    start=insert_point,
                    confidence=0.68,
                )
                _safe_append_axes(axes, axis, self.axis_limit)

    def _line_endpoints(self, entity: ezdxf.entities.DXFGraphic) -> tuple[Point2D, Point2D]:
        start = _to_point(getattr(entity.dxf, "start", (0, 0)))
        end_candidate = getattr(entity.dxf, "end", None) or getattr(entity.dxf, "unit_vector", (0, 0))
        end = _to_point(end_candidate)
        return start, end

    def _polyline_points(self, entity: ezdxf.entities.DXFGraphic) -> list[Point2D]:
        raw_points: list[tuple[float, ...]] = []
        if hasattr(entity, "points"):
            raw_points = list(entity.points())
        elif hasattr(entity, "vertices"):
            raw_points = [(vertex.dxf.location.x, vertex.dxf.location.y) for vertex in entity.vertices()]
        return [_to_point(point) for point in raw_points]

    def _append_axis_from_points(
        self,
        asset_name: str,
        layer_name: str,
        source_ref: str | None,
        pts: tuple[Point2D, Point2D],
        axes: list[GridAxis],
    ) -> None:
        start, end = pts
        orientation = _orientation_from_points(start, end)
        coordinate = start.x if orientation == "vertical" else start.y if orientation == "horizontal" else None
        axis = GridAxis(
            asset_name=asset_name,
            orientation=orientation,
            coordinate=coordinate,
            layer=layer_name,
            source_ref=source_ref,
            start=start,
            end=end,
            confidence=0.7,
        )
        _safe_append_axes(axes, axis, self.axis_limit)

    def _extract_text(self, entity: ezdxf.entities.DXFGraphic) -> str | None:
        if hasattr(entity, "plain_text"):
            return entity.plain_text() or None
        text = getattr(entity.dxf, "text", None)
        if text:
            return str(text)
        return None
