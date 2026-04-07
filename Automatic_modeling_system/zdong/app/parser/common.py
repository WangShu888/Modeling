from __future__ import annotations

import re
from typing import Any, Iterable, TypeVar

from ..models import BoundingBox2D, DimensionEntityRecord, Point2D

_UNIT_TEXT_RE = re.compile(r"\b(mm|cm|m)\b", re.IGNORECASE)
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
_GRID_LABEL_RE = re.compile(
    r"(?i)(?:\b(?:axis|grid)\b\s*([A-Z0-9-]+)|\b([A-Z]{1,3}-\d{1,3})\b|\b([A-Z]{1,3})\b|\b(\d{1,3})\b)"
)
_GRID_ROLE_PATTERNS = (
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

T = TypeVar("T")


def normalize_text(text: str) -> str:
    return " ".join(text.replace("\x00", " ").split()).strip()


def round_float(value: float | int | None, digits: int = 3) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def safe_float(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_append(items: list[T], value: T, limit: int) -> bool:
    if limit <= 0:
        return False
    if len(items) >= limit:
        return False
    items.append(value)
    return True


def bbox_from_points(points: Iterable[Point2D]) -> BoundingBox2D | None:
    points = list(points)
    if not points:
        return None
    xs = [point.x for point in points]
    ys = [point.y for point in points]
    return BoundingBox2D(min_x=min(xs), min_y=min(ys), max_x=max(xs), max_y=max(ys))


def bbox_from_rect(rect: Any) -> BoundingBox2D | None:
    if rect is None:
        return None
    if hasattr(rect, "x0"):
        return BoundingBox2D(
            min_x=round_float(rect.x0) or 0.0,
            min_y=round_float(rect.y0) or 0.0,
            max_x=round_float(rect.x1) or 0.0,
            max_y=round_float(rect.y1) or 0.0,
        )
    if isinstance(rect, (tuple, list)) and len(rect) >= 4:
        return BoundingBox2D(
            min_x=round_float(rect[0]) or 0.0,
            min_y=round_float(rect[1]) or 0.0,
            max_x=round_float(rect[2]) or 0.0,
            max_y=round_float(rect[3]) or 0.0,
        )
    return None


def to_point(value: Any) -> Point2D:
    if hasattr(value, "x") and hasattr(value, "y"):
        return Point2D(x=round_float(value.x) or 0.0, y=round_float(value.y) or 0.0)
    if isinstance(value, (tuple, list)):
        return Point2D(x=round_float(value[0]) or 0.0, y=round_float(value[1]) or 0.0)
    return Point2D(x=0.0, y=0.0)


def orientation_from_points(start: Point2D, end: Point2D) -> str:
    dx = abs(end.x - start.x)
    dy = abs(end.y - start.y)
    if dx == 0 and dy == 0:
        return "unknown"
    if dx <= dy * 0.2:
        return "vertical"
    if dy <= dx * 0.2:
        return "horizontal"
    return "angled"


def guess_semantic_role(name: str | None) -> str:
    if not name:
        return "unknown"
    lowered = name.lower()
    for role, tokens in _GRID_ROLE_PATTERNS:
        if any(token.lower() in lowered for token in tokens):
            return role
    return "unknown"


def extract_grid_label(text: str) -> str | None:
    normalized = normalize_text(text)
    match = _GRID_LABEL_RE.search(normalized)
    if not match:
        return None
    groups = [value for value in match.groups() if value]
    if not groups:
        return None
    return groups[0].upper()


def extract_north_angle(text: str) -> float | None:
    match = _NORTH_ANGLE_RE.search(normalize_text(text))
    if not match:
        return None
    try:
        return round_float(float(match.group(1)), 2)
    except Exception:
        return None


def extract_elevations(text: str) -> list[float]:
    normalized = normalize_text(text)
    values: list[float] = []
    for match in _ELEVATION_TEXT_RE.finditer(normalized):
        value = float(match.group(1))
        unit = match.group(2) or "m"
        values.append(round_float(_convert_length_to_m(value, unit), 3) or 0.0)
    if not values:
        for match in _SIGNED_ELEVATION_RE.finditer(normalized):
            values.append(round_float(float(match.group(1)), 3) or 0.0)
    return values


def _convert_length_to_m(value: float, unit: str | None) -> float:
    if unit is None or unit.lower() == "m":
        return float(value)
    if unit.lower() == "cm":
        return float(value) / 100.0
    if unit.lower() == "mm":
        return float(value) / 1000.0
    return float(value)


def extract_dimension_records_from_text(
    asset_name: str,
    text: str,
    layer: str | None,
    bbox: BoundingBox2D | None,
    source_ref: str | None = None,
) -> list[DimensionEntityRecord]:
    normalized = normalize_text(text)
    records: list[DimensionEntityRecord] = []
    for match in _DIMENSION_TEXT_RE.finditer(normalized):
        if match.group(1):
            records.append(
                DimensionEntityRecord(
                    asset_name=asset_name,
                    kind="text_dimension",
                    text=match.group(0),
                    value=round_float(float(match.group(1)), 3),
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
