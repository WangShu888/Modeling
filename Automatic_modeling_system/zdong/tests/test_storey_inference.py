from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from zdong.app.models import (
    DrawingFragmentRecord,
    DrawingEntityRecord,
    ParsedDrawingModel,
    Point2D,
    StoreyCandidateRecord,
)
from zdong.app.storey_inference import (
    infer_asset_view_role,
    infer_floor_count,
    infer_parsed_fragment_storeys,
    infer_storey_key,
)


def test_infer_storey_key_supports_common_floor_patterns() -> None:
    assert infer_storey_key("宿舍楼-1F-建施.dxf") == "1F"
    assert infer_storey_key("住宅-地下二层平面图.dxf") == "B2"
    assert infer_storey_key("办公楼-RF-屋面图.dxf") == "RF"


def test_infer_floor_count_uses_uploaded_asset_names() -> None:
    parsed = ParsedDrawingModel(
        assets_count=2,
        asset_kinds=["cad", "cad"],
        detected_entities=[
            DrawingEntityRecord(
                asset_name="宿舍楼-1F-建施.dxf",
                category="wall_line",
                points=[Point2D(x=0, y=0), Point2D(x=1000, y=0)],
                confidence=0.8,
            ),
            DrawingEntityRecord(
                asset_name="宿舍楼-2F-建施.dxf",
                category="wall_line",
                points=[Point2D(x=0, y=0), Point2D(x=1000, y=0)],
                confidence=0.8,
            ),
        ],
    )

    assert infer_floor_count(parsed) == 2


def test_infer_asset_view_role_uses_chinese_candidate_markers() -> None:
    assert infer_asset_view_role("图纸.dxf", ["南立面图"]) == "facade"
    assert infer_asset_view_role("图纸.dxf", ["1-1剖面图"]) == "section"
    assert infer_asset_view_role("图纸.dxf", ["二层平面图"]) == "plan"


def test_infer_floor_count_uses_storey_candidate_text() -> None:
    parsed = ParsedDrawingModel(
        assets_count=2,
        asset_kinds=["cad", "cad"],
        detected_entities=[
            DrawingEntityRecord(
                asset_name="建施.dxf",
                category="wall_line",
                points=[Point2D(x=0, y=0), Point2D(x=1000, y=0)],
                confidence=0.8,
            ),
            DrawingEntityRecord(
                asset_name="结施.dxf",
                category="wall_line",
                points=[Point2D(x=0, y=0), Point2D(x=1000, y=0)],
                confidence=0.8,
            ),
        ],
        storey_candidate_details=[
            StoreyCandidateRecord(asset_name="建施.dxf", name="一层平面图", source="drawing_text"),
            StoreyCandidateRecord(asset_name="结施.dxf", name="二层平面图", source="drawing_text"),
        ],
    )

    assert infer_floor_count(parsed) == 2


def test_infer_parsed_fragment_storeys_uses_fragment_level_model() -> None:
    parsed = ParsedDrawingModel(
        assets_count=1,
        asset_kinds=["cad"],
        fragments=[
            DrawingFragmentRecord(
                fragment_id="asset-a::fragment::1F::01",
                asset_name="asset-a.dxf",
                fragment_title="一层平面图",
                fragment_role="plan",
                storey_key="1F",
            ),
            DrawingFragmentRecord(
                fragment_id="asset-a::fragment::2F::02",
                asset_name="asset-a.dxf",
                fragment_title="二层平面图",
                fragment_role="plan",
                storey_key="2F",
            ),
            DrawingFragmentRecord(
                fragment_id="asset-a::fragment::section::03",
                asset_name="asset-a.dxf",
                fragment_title="1-1剖面图",
                fragment_role="section",
                storey_key="1F",
            ),
        ],
    )

    fragment_storeys = infer_parsed_fragment_storeys(parsed)
    assert fragment_storeys["asset-a::fragment::1F::01"] == "1F"
    assert fragment_storeys["asset-a::fragment::2F::02"] == "2F"
    assert infer_floor_count(parsed) == 2
