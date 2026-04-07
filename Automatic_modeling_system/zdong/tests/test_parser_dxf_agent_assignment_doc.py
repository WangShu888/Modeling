from __future__ import annotations

from pathlib import Path


def test_parser_dxf_agent_assignment_doc_covers_scope_contract_and_owners() -> None:
    doc = Path("/workspace/自动建模系统/zdong/docs/2026-04-02_图纸解析DXF代理分工报告_V2.md")
    content = doc.read_text(encoding="utf-8")

    required_tokens = [
        "本轮只处理 `DXF` 解析",
        "app/parser/common.py",
        "app/parser/dxf_reader.py",
        "app/parser/view_storey.py",
        "app/parser/fragments.py",
        "app/parser/grid_recognizer.py",
        "app/parser/component_recognizer.py",
        "app/parser/annotation_binder.py",
        "app/parser/assembly_engine.py",
        "app/parser/validation_engine.py",
        "app/parser/compatibility_adapter.py",
        "Agent-1 DXF 基础代理",
        "Agent-2 视图楼层片段代理",
        "Agent-3 轴网代理",
        "Agent-4 构件语义代理",
        "Agent-5 拼接校核代理",
        "wall_line",
        "source_storey_key",
        "第一批并行",
        "第二批",
    ]

    for token in required_tokens:
        assert token in content
