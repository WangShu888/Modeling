from __future__ import annotations

from pathlib import Path


def test_remediation_assignment_doc_covers_detected_issues_and_targets() -> None:
    doc = Path("/workspace/自动建模系统/zdong/docs/2026-04-02_检测问题整改分工方案.md")
    content = doc.read_text(encoding="utf-8")

    required_tokens = [
        "entity_detection_truncated",
        "multi_storey_asset_collapsed",
        "drawing.space_boundaries_missing",
        "site_boundary_inferred",
        "site.area_missing",
        "app/drawing_parser.py",
        "app/storey_inference.py",
        "app/pipeline.py",
        "source_storey_keys",
        "storey_layout_trace",
        "model.ifc",
        "构件数量与位置",
        "楼层按顺序拼接",
    ]

    for token in required_tokens:
        assert token in content
