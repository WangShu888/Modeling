from __future__ import annotations

from pathlib import Path


def test_new_drawing_detection_report_covers_proxy_entity_root_cause() -> None:
    doc = Path("/workspace/自动建模系统/zdong/docs/2026-04-03_新图纸未驱动模型检测报告.md")
    content = doc.read_text(encoding="utf-8")

    required_tokens = [
        "proj_0023 / ver_0038",
        "proj_0026 / ver_0040",
        "proj_0021 / ver_0036",
        "ACAD_PROXY_ENTITY",
        "template_fallback",
        "geometry_source = \"template_fallback\"",
        "wall_detection_low_confidence",
        "drawing.source_geometry_missing",
        "drawing.proxy_entities_unresolved",
        "一层平面图（不带家具）.dxf",
        "二层平面图（不带家具）.dxf",
        "site_boundary_candidate",
        "source_storey_keys = []",
    ]

    for token in required_tokens:
        assert token in content
