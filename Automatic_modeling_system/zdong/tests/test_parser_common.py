from __future__ import annotations

from zdong.app.models import Point2D
from zdong.app.parser import common


def test_normalize_text_collapses_whitespace() -> None:
    raw = "  foo  \nbar\x00baz   "
    assert common.normalize_text(raw) == "foo bar baz"


def test_bbox_from_points_handles_empty_input() -> None:
    assert common.bbox_from_points([]) is None
    bbox = common.bbox_from_points([Point2D(x=0, y=1), Point2D(x=2, y=3)])
    assert bbox.min_x == 0 and bbox.max_x == 2
    assert bbox.min_y == 1 and bbox.max_y == 3


def test_safe_append_enforces_limits() -> None:
    items: list[int] = []
    assert common.safe_append(items, 1, limit=1)
    assert not common.safe_append(items, 2, limit=1)


def test_extract_dimension_records_from_text_parses_values() -> None:
    records = common.extract_dimension_records_from_text("asset", "1200mm x 800mm", "A", None)
    assert records
    first = records[0]
    assert first.value == 1200.0
