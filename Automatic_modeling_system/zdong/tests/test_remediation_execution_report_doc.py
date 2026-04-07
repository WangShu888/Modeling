from __future__ import annotations

from pathlib import Path


def test_remediation_execution_report_covers_execution_review_and_real_sample_result() -> None:
    doc = Path("/workspace/自动建模系统/zdong/docs/2026-04-02_检测问题整改执行与审查报告.md")
    content = doc.read_text(encoding="utf-8")

    required_tokens = [
        "parser_storey_remediation",
        "pipeline_validation_remediation",
        "review_reuse",
        "review_quality",
        "review_efficiency",
        "DrawingFragmentRecord",
        "fragment_storey_key",
        "source_fragment_id",
        "count_reconciliation",
        "formal_blocking_issues",
        "专用宿舍楼-建施.dxf",
        "test_dxf_delivery_pipeline.py",
        "26 passed",
        "ValidationReport",
        "model.ifc",
    ]

    for token in required_tokens:
        assert token in content
