from __future__ import annotations

from zdong.app.models import DrawingEntityRecord
from zdong.app.parser.component_recognizer import (
    extract_component_candidates,
    filter_by_confidence,
)


def _entity(category: str, confidence: float | None = None) -> DrawingEntityRecord:
    kwargs = {"asset_name": "project1", "category": category}
    if confidence is not None:
        kwargs["confidence"] = confidence
    return DrawingEntityRecord(**kwargs)


def test_extract_component_candidates_labels_supported_categories() -> None:
    entities = [
        _entity("wall_line"),
        _entity("door_block"),
        _entity("window_block"),
        _entity("room_boundary"),
        _entity("misc_unknown"),
    ]
    candidates = extract_component_candidates(entities)
    assert len(candidates) == 4
    types = {candidate.metadata["component_type"] for candidate in candidates}
    assert types == {"wall", "door", "window", "room"}
    for candidate in candidates:
        assert candidate.metadata["component_source_category"] in {
            "wall_line",
            "door_block",
            "window_block",
            "room_boundary",
        }
        assert candidate.confidence >= 0.55


def test_filter_by_confidence_honors_threshold() -> None:
    candidates = [
        _entity("wall_line", confidence=0.4),
        _entity("wall_line", confidence=0.75),
    ]
    filtered = filter_by_confidence(candidates, minimum=0.5)
    assert len(filtered) == 1
    assert filtered[0].confidence >= 0.75
