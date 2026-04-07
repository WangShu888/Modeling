from __future__ import annotations

from zdong.app.models import StoreyCandidateRecord
from zdong.app.parser.view_storey import append_view_marker_candidates, classify_text_semantics, descriptor_storey_candidates


def test_descriptor_storey_candidates_prioritizes_floor() -> None:
    candidates = descriptor_storey_candidates("楼编号A", "首层平面图")
    assert any(candidate.name == "standard_floor" for candidate in candidates)


def test_append_view_marker_candidates_differentiates_reference_views() -> None:
    candidates: list[StoreyCandidateRecord] = []
    append_view_marker_candidates(
        candidates,
        asset_name="asset1",
        text="A-A剖面图",
        confidence=0.6,
        source="text",
    )
    assert any(candidate.name == "section_reference" for candidate in candidates)
    append_view_marker_candidates(
        candidates,
        asset_name="asset1",
        text="标准层平面图",
        confidence=0.6,
        source="text",
    )
    assert any(candidate.name == "standard_floor" for candidate in candidates)


def test_classify_text_semantics_recognizes_signals() -> None:
    assert classify_text_semantics("North Angle 30") == "north_angle"
    assert classify_text_semantics("1F 标高 4.2") == "elevation"
    assert classify_text_semantics("平面图") == "view_marker"
    assert classify_text_semantics("卫生间") == "room_label"


def test_classify_text_semantics_rejects_non_room_titles() -> None:
    assert classify_text_semantics("广联达专用宿舍楼") == "generic"
    assert classify_text_semantics("详楼梯配筋图") == "generic"
    assert classify_text_semantics("室内装修做法表") == "generic"
