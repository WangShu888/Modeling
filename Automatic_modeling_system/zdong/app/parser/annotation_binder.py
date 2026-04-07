from __future__ import annotations

from typing import Iterable, Sequence

from ..models import DrawingEntityRecord, TextAnnotationRecord

ANNOTATION_KEYWORDS = {
    "wall": ("wall", "墙"),
    "door": ("door", "门"),
    "window": ("window", "窗"),
    "room": ("room", "房", "空间"),
}


def _match_annotations(
    component_type: str | None, annotations: Sequence[TextAnnotationRecord]
) -> list[str]:
    hits: list[str] = []
    normalized_type = (component_type or "").lower()
    for annotation in annotations:
        text = annotation.text.lower()
        if normalized_type and normalized_type in text:
            hits.append(annotation.text)
            continue
        for keyword in ANNOTATION_KEYWORDS.get(normalized_type, []):
            if keyword in text:
                hits.append(annotation.text)
                break
    return hits


def bind_annotations(
    candidates: Iterable[DrawingEntityRecord], annotations: Sequence[TextAnnotationRecord]
) -> list[DrawingEntityRecord]:
    bound: list[DrawingEntityRecord] = []
    for candidate in candidates:
        enriched = candidate.model_copy(deep=True)
        component_type = enriched.metadata.get("component_type")
        hits = _match_annotations(component_type, annotations)
        if hits:
            enriched.metadata.setdefault("annotation_notes", []).extend(hits)
            enriched.metadata["annotation_confidence"] = min(
                1.0, enriched.metadata.get("annotation_confidence", 0.0) + len(hits) * 0.1
            )
        bound.append(enriched)
    return bound
