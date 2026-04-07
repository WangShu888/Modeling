from __future__ import annotations

from zdong.app.models import (
    BoundingBox2D,
    DrawingEntityRecord,
    PendingReviewItem,
    Point2D,
    StoreyCandidateRecord,
    TextAnnotationRecord,
)
from zdong.app.parser.fragments import append_parse_diagnostics, build_drawing_fragments


def _make_wall_entity(asset_name: str, points: list[Point2D]) -> DrawingEntityRecord:
    return DrawingEntityRecord(
        asset_name=asset_name,
        category="wall_line",
        layer="A-WALL",
        label=None,
        bbox=None,
        points=points,
        source_ref=None,
        confidence=0.8,
        metadata={},
    )


def test_build_drawing_fragments_assigns_entities_to_fragments() -> None:
    text_items = [
        TextAnnotationRecord(
            asset_name="asset1",
            text="1F 平面",
            semantic_tag="view_marker",
            layer=None,
            bbox=BoundingBox2D(min_x=0, min_y=0, max_x=100, max_y=100),
            page_index=None,
            source_ref=None,
        ),
        TextAnnotationRecord(
            asset_name="asset1",
            text="2F 平面",
            semantic_tag="view_marker",
            layer=None,
            bbox=BoundingBox2D(min_x=200, min_y=0, max_x=300, max_y=100),
            page_index=None,
            source_ref=None,
        ),
    ]
    storey_candidates = [
        StoreyCandidateRecord(asset_name="asset1", name="1F", source="test", confidence=0.9),
        StoreyCandidateRecord(asset_name="asset1", name="2F", source="test", confidence=0.9),
    ]
    entities = [
        _make_wall_entity("asset1", [Point2D(x=10, y=10), Point2D(x=90, y=10)]),
        _make_wall_entity("asset1", [Point2D(x=210, y=10), Point2D(x=290, y=10)]),
    ]

    fragments = build_drawing_fragments(entities, text_items, storey_candidates)
    assert len(fragments) >= 2
    for entity in entities:
        assert "fragment_storey_key" in entity.metadata
        assert entity.metadata["fragment_storey_key"] in {"1F", "2F"}


def test_append_parse_diagnostics_reports_truncation_and_multi_storey() -> None:
    text_items = [
        TextAnnotationRecord(
            asset_name="assetX",
            text="1F 平面",
            semantic_tag="view_marker",
            layer=None,
            bbox=BoundingBox2D(min_x=0, min_y=0, max_x=50, max_y=50),
            page_index=None,
            source_ref=None,
        ),
        TextAnnotationRecord(
            asset_name="assetX",
            text="2F 平面",
            semantic_tag="view_marker",
            layer=None,
            bbox=BoundingBox2D(min_x=60, min_y=0, max_x=110, max_y=50),
            page_index=None,
            source_ref=None,
        ),
    ]
    storey_candidates = [
        StoreyCandidateRecord(asset_name="assetX", name="1F", source="test", confidence=0.8),
        StoreyCandidateRecord(asset_name="assetX", name="2F", source="test", confidence=0.8),
    ]
    entities = [
        _make_wall_entity("assetX", [Point2D(x=5, y=5), Point2D(x=45, y=5)]),
        _make_wall_entity("assetX", [Point2D(x=65, y=5), Point2D(x=105, y=5)]),
    ]

    fragments = build_drawing_fragments(entities, text_items, storey_candidates)
    pending_review: list[PendingReviewItem] = []
    append_parse_diagnostics(entities, detected_entities_dropped=2, fragments=fragments, storey_candidate_details=storey_candidates, pending_review=pending_review)
    assert any(item.category == "entity_detection_truncated" for item in pending_review)

    # collapse metadata to a single storey to trigger multi-storey warning
    for entity in entities:
        entity.metadata["fragment_storey_key"] = "1F"
    append_parse_diagnostics(entities, detected_entities_dropped=0, fragments=fragments, storey_candidate_details=storey_candidates, pending_review=pending_review)
    assert any(item.category == "multi_storey_asset_collapsed" for item in pending_review)
