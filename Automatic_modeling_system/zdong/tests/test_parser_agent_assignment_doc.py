from __future__ import annotations

from pathlib import Path


def test_parser_agent_assignment_doc_covers_owners_dependencies_and_gates() -> None:
    doc = Path("/workspace/自动建模系统/zdong/docs/2026-04-02_图纸解析代理分工报告_V1.md")
    content = doc.read_text(encoding="utf-8")

    required_tokens = [
        "app/drawing_parser.py",
        "app/models.py",
        "app/pipeline.py",
        "app/parser/view_classifier.py",
        "app/parser/floor_splitter.py",
        "app/parser/grid_recognizer.py",
        "app/parser/component_recognizer.py",
        "app/parser/annotation_binder.py",
        "app/parser/assembly_engine.py",
        "app/parser/validation_engine.py",
        "app/parser/compatibility_adapter.py",
        "Agent-0 主代理 / 集成代理",
        "Agent-1 入口治理代理",
        "Agent-2 轴网骨架代理",
        "Agent-3 语义识别代理",
        "Agent-4 拼接与校核代理",
        "Agent-5 兼容适配代理",
        "第一批并行启动",
        "第二批启动",
        "必须补对应测试",
    ]

    for token in required_tokens:
        assert token in content
