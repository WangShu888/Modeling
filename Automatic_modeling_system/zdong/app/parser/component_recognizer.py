from __future__ import annotations

from typing import Iterable, Sequence

from ..models import DrawingEntityRecord

COMPONENT_CATEGORY_MAP: dict[str, str] = {
    "wall_line": "wall",
    "wall_path": "wall",
    "door_block": "door",
    "window_block": "window",
    "room_boundary": "room",
    "room_label": "room",
}

COMPONENT_CONFIDENCE: dict[str, float] = {
    "wall": 0.72,
    "door": 0.6,
    "window": 0.6,
    "room": 0.55,
}


def categorize_component(category: str) -> str | None:
    normalized = category.lower()
    return COMPONENT_CATEGORY_MAP.get(normalized)


def _confidence_for_type(component_type: str) -> float:
    return COMPONENT_CONFIDENCE.get(component_type, 0.5)


def extract_component_candidates(entities: Sequence[DrawingEntityRecord]) -> list[DrawingEntityRecord]:
    candidates: list[DrawingEntityRecord] = []
    for entity in entities:
        component_type = categorize_component(entity.category)
        if component_type is None:
            continue
        candidate = entity.model_copy(deep=True)
        candidate.metadata.setdefault("component_type", component_type)
        candidate.metadata.setdefault("component_source_category", entity.category)
        candidate.metadata.setdefault("component_confidence", _confidence_for_type(component_type))
        candidate.confidence = max(candidate.confidence, _confidence_for_type(component_type))
        candidates.append(candidate)
    return candidates


def filter_by_confidence(entities: Iterable[DrawingEntityRecord], minimum: float) -> list[DrawingEntityRecord]:
    return [entity for entity in entities if entity.confidence >= minimum]
