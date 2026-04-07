from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from zdong.app.drawing_parser import DrawingParser, _AssetParseResult
from zdong.app.models import LayerMapEntry


def test_finalize_asset_quality_marks_unresolved_proxy_entities_on_semantic_layers() -> None:
    parser = DrawingParser()
    result = _AssetParseResult(
        kind="cad",
        layer_map=[
            LayerMapEntry(
                asset_name="proxy-sample.dxf",
                name="WALL",
                semantic_role="wall",
                entity_count=16,
                entity_types=["ACAD_PROXY_ENTITY"],
            ),
            LayerMapEntry(
                asset_name="proxy-sample.dxf",
                name="WINDOW",
                semantic_role="window",
                entity_count=4,
                entity_types=["ACAD_PROXY_ENTITY"],
            ),
        ],
    )

    parser._finalize_asset_quality("proxy-sample.dxf", "cad", result)

    categories = {item.category for item in result.pending_review}
    assert "wall_detection_low_confidence" in categories
    assert "proxy_entities_unresolved" in categories
