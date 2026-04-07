from __future__ import annotations

from zdong.app.models import DrawingEntityRecord, TextAnnotationRecord
from zdong.app.parser.annotation_binder import bind_annotations


def _entity_with_type(component_type: str) -> DrawingEntityRecord:
    return DrawingEntityRecord(
        asset_name="project1",
        category="wall_line",
        metadata={"component_type": component_type},
    )


def _annotation(text: str) -> TextAnnotationRecord:
    return TextAnnotationRecord(asset_name="project1", text=text, semantic_tag="note")


def test_bind_annotations_associates_notes() -> None:
    candidate = _entity_with_type("wall")
    annotations = [
        _annotation("墙体编号A1"),
        _annotation("窗户尺寸20cm"),
    ]
    bound = bind_annotations([candidate], annotations)
    assert len(bound) == 1
    metadata = bound[0].metadata
    assert "annotation_notes" in metadata
    assert metadata["annotation_notes"][0] == "墙体编号A1"
    assert metadata["annotation_confidence"] > 0


def test_bind_annotations_does_nothing_when_no_match() -> None:
    candidate = _entity_with_type("door")
    annotations = [_annotation("楼梯标高")]
    bound = bind_annotations([candidate], annotations)
    assert bound[0].metadata.get("annotation_notes") is None
