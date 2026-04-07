from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def test_drawing_parser_flow_doc_covers_current_pipeline_stages() -> None:
    doc_path = Path(__file__).resolve().parents[1] / "docs" / "图纸解析流程.md"

    content = doc_path.read_text(encoding="utf-8")

    assert "SourceBundleBuilder.build()" in content
    assert "DrawingParser.parse(bundle)" in content
    assert "_parse_dxf()" in content
    assert "_parse_dwg()" in content
    assert "_parse_pdf()" in content
    assert "infer_parsed_asset_storeys(parsed)" in content
    assert "BimEngine._source_layouts(parsed_drawing)" in content
