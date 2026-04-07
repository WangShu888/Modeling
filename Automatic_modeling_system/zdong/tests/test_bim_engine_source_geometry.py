from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from zdong.app.models import (
    BoundingBox2D,
    Constraints,
    DesignIntent,
    DrawingEntityRecord,
    ParsedDrawingModel,
    PlanStep,
    Point2D,
    ProgramInfo,
    SiteInfo,
    StyleInfo,
    ModelingPlan,
)
from zdong.app.pipeline import BimEngine


def _intent() -> DesignIntent:
    return DesignIntent(
        project_id="proj",
        request_id="req",
        version_id="ver",
        source_mode="cad_to_bim",
        building_type="residential",
        site=SiteInfo(boundary_source="drawing", area_sqm=800.0, north_angle=0.0),
        constraints=Constraints(
            floors=1,
            standard_floor_height_m=3.0,
            first_floor_height_m=3.3,
            ruleset="cn_residential_v1",
            far=2.0,
        ),
        program=ProgramInfo(
            spaces_from_drawings=True,
            units_per_floor=4,
            core_type="single_core",
            first_floor_spaces=["lobby"],
            typical_floor_spaces=["unit"],
        ),
        style=StyleInfo(facade="simple", material_palette=["white"]),
        deliverables=["ifc"],
    )


def _plan() -> ModelingPlan:
    return ModelingPlan(
        strategy="cad_to_bim",
        can_continue=True,
        steps=[PlanStep(name="build_bim", module="bim", description="build")],
    )


def _parsed() -> ParsedDrawingModel:
    return ParsedDrawingModel(
        assets_count=1,
        asset_kinds=["cad"],
        units="mm",
        detected_entities=[
            DrawingEntityRecord(
                asset_name="sample.dxf",
                category="wall_line",
                layer="WALL",
                points=[Point2D(x=1000, y=1000), Point2D(x=5000, y=1000)],
                source_ref="w1",
                confidence=0.8,
            ),
            DrawingEntityRecord(
                asset_name="sample.dxf",
                category="wall_path",
                layer="WALL",
                bbox=BoundingBox2D(min_x=1000, min_y=2000, max_x=3000, max_y=2500),
                points=[
                    Point2D(x=1000, y=2000),
                    Point2D(x=3000, y=2000),
                    Point2D(x=3000, y=2500),
                    Point2D(x=1000, y=2500),
                ],
                source_ref="w2",
                confidence=0.8,
            ),
            DrawingEntityRecord(
                asset_name="sample.dxf",
                category="door_block",
                layer="DOOR",
                points=[Point2D(x=2000, y=1000), Point2D(x=2000, y=2200)],
                source_ref="d1",
                confidence=0.8,
                metadata={"tch_opening_width": 1200.0, "tch_opening_height": 2100.0},
            ),
            DrawingEntityRecord(
                asset_name="sample.dxf",
                category="window_block",
                layer="WINDOW",
                points=[Point2D(x=3500, y=2000), Point2D(x=3500, y=3500)],
                source_ref="win1",
                confidence=0.8,
                metadata={"tch_opening_width": 1500.0, "tch_opening_height": 1500.0},
            ),
        ],
    )


def test_bim_engine_uses_parsed_drawing_entities_for_geometry() -> None:
    model = BimEngine().build(_intent(), _plan(), _parsed())

    storey = model.storeys[0]
    assert model.metadata["geometry_source"] == "parsed_drawing"
    assert model.metadata["source_wall_entities"] == 2
    assert len([element for element in storey.elements if element.ifc_type == "IfcWall"]) == 2
    assert len([element for element in storey.elements if element.ifc_type == "IfcDoor"]) == 1
    assert len([element for element in storey.elements if element.ifc_type == "IfcWindow"]) == 1

    wall = next(element for element in storey.elements if element.properties.get("source_ref") == "w1")
    assert wall.properties["geometry_source"] == "parsed_drawing"
    assert wall.properties["source_storey_key"] == "1F"
    assert wall.properties["source_fragment_id"] == "sample.dxf:1F"
    assert wall.properties["shape_width_m"] == 4.0
    assert wall.properties["local_x_m"] == 2.0
    assert wall.properties["local_y_m"] == 0.0
    assert wall.properties["geometry_anchor"] == "center"

    door = next(element for element in storey.elements if element.properties.get("source_ref") == "d1")
    assert door.properties["rotation_deg"] == 90.0
    assert door.properties["local_x_m"] == 1.0
    assert door.properties["local_y_m"] == 0.6
    assert door.properties["width_mm"] == 1200
    assert door.properties["geometry_anchor"] == "center"

    window = next(element for element in storey.elements if element.properties.get("source_ref") == "win1")
    assert window.properties["rotation_deg"] == 90.0
    assert window.properties["local_x_m"] == 2.5
    assert window.properties["local_y_m"] == 1.75
    assert window.properties["overall_width_mm"] == 1500
    assert window.properties["geometry_anchor"] == "center"

    slab = next(element for element in storey.elements if element.ifc_type == "IfcSlab")
    assert slab.properties["shape_width_m"] >= 4.0
    assert slab.properties["shape_depth_m"] >= 1.5
    assert slab.properties["source_storey_key"] == "1F"
    assert slab.properties["source_fragment_id"] == "sample.dxf:1F"


def test_bim_engine_groups_same_storey_assets_and_orders_storeys() -> None:
    parsed = ParsedDrawingModel(
        assets_count=3,
        asset_kinds=["cad", "cad", "cad"],
        units="mm",
        detected_entities=[
            DrawingEntityRecord(
                asset_name="宿舍楼-1F-建施.dxf",
                category="wall_line",
                layer="WALL",
                points=[Point2D(x=0, y=0), Point2D(x=4000, y=0)],
                source_ref="1f-wall-a",
                confidence=0.8,
            ),
            DrawingEntityRecord(
                asset_name="宿舍楼-1F-结施.dxf",
                category="door_block",
                layer="DOOR",
                points=[Point2D(x=1000, y=0)],
                source_ref="1f-door-b",
                confidence=0.8,
            ),
            DrawingEntityRecord(
                asset_name="宿舍楼-2F-建施.dxf",
                category="wall_line",
                layer="WALL",
                points=[Point2D(x=10000, y=0), Point2D(x=14000, y=0)],
                source_ref="2f-wall-a",
                confidence=0.8,
            ),
            DrawingEntityRecord(
                asset_name="宿舍楼-2F-建施.dxf",
                category="window_block",
                layer="WINDOW",
                points=[Point2D(x=12000, y=0)],
                source_ref="2f-window-a",
                confidence=0.8,
            ),
        ],
    )
    intent = _intent().model_copy(update={"constraints": _intent().constraints.model_copy(update={"floors": 2})})

    model = BimEngine().build(intent, _plan(), parsed)

    assert [storey.name for storey in model.storeys] == ["1F", "2F"]
    assert model.metadata["source_storey_keys"] == ["1F", "2F"]
    assert len(model.metadata["source_fragment_ids"]) == 3
    assert len([element for element in model.storeys[0].elements if element.ifc_type == "IfcWall"]) == 1
    assert len([element for element in model.storeys[0].elements if element.ifc_type == "IfcDoor"]) == 1
    assert len([element for element in model.storeys[1].elements if element.ifc_type == "IfcWall"]) == 1
    assert len([element for element in model.storeys[1].elements if element.ifc_type == "IfcWindow"]) == 1
    assert model.metadata["count_reconciliation"]["IfcWall"]["source"] == 2
    assert model.metadata["count_reconciliation"]["IfcWall"]["modeled"] == 2


def test_bim_engine_reuses_single_parsed_storey_until_requested_floor_count() -> None:
    parsed = ParsedDrawingModel(
        assets_count=2,
        asset_kinds=["cad", "cad"],
        units="mm",
        detected_entities=[
            DrawingEntityRecord(
                asset_name="专用宿舍楼-建施.dxf",
                category="wall_line",
                layer="WALL",
                points=[Point2D(x=0, y=0), Point2D(x=4000, y=0)],
                source_ref="wall-a",
                confidence=0.8,
            ),
            DrawingEntityRecord(
                asset_name="专用宿舍楼-结施.dxf",
                category="door_block",
                layer="DOOR",
                points=[Point2D(x=1000, y=0)],
                source_ref="door-a",
                confidence=0.8,
            ),
        ],
    )
    intent = _intent().model_copy(update={"constraints": _intent().constraints.model_copy(update={"floors": 2})})

    model = BimEngine().build(intent, _plan(), parsed)

    assert [storey.name for storey in model.storeys] == ["1F", "2F"]
    assert len([element for element in model.storeys[0].elements if element.ifc_type == "IfcWall"]) == 1
    assert len([element for element in model.storeys[1].elements if element.ifc_type == "IfcWall"]) == 1
    assert model.metadata["storey_layout_trace"][0]["storey_key"] == "1F"
    assert model.metadata["storey_layout_trace"][0]["source_storey_key"] == "1F"
    assert model.metadata["storey_layout_trace"][0]["replicated"] is False
    assert model.metadata["storey_layout_trace"][0]["source_fragment_ids"]
    assert model.metadata["storey_layout_trace"][1]["storey_key"] == "2F"
    assert model.metadata["storey_layout_trace"][1]["source_storey_key"] == "1F"
    assert model.metadata["storey_layout_trace"][1]["replicated"] is True
    assert model.metadata["storey_layout_trace"][1]["source_fragment_ids"] == model.metadata["storey_layout_trace"][0]["source_fragment_ids"]


def test_bim_engine_prefers_fragment_storey_trace_from_entity_metadata() -> None:
    parsed = ParsedDrawingModel(
        assets_count=1,
        asset_kinds=["cad"],
        units="mm",
        detected_entities=[
            DrawingEntityRecord(
                asset_name="综合图.dxf",
                category="wall_line",
                layer="WALL",
                points=[Point2D(x=0, y=0), Point2D(x=4000, y=0)],
                source_ref="w-meta",
                confidence=0.8,
                metadata={"fragment_id": "frag-2f-a", "fragment_storey_key": "2F", "fragment_role": "plan"},
            ),
        ],
    )
    intent = _intent().model_copy(update={"constraints": _intent().constraints.model_copy(update={"floors": 2})})

    model = BimEngine().build(intent, _plan(), parsed)

    assert model.metadata["source_storey_keys"] == ["2F"]
    assert model.metadata["source_fragment_ids"] == ["frag-2f-a"]
    wall = next(element for element in model.storeys[0].elements if element.ifc_type == "IfcWall")
    assert wall.properties["source_storey_key"] == "2F"
    assert wall.properties["source_fragment_id"] == "frag-2f-a"


def test_bim_engine_uses_room_labels_as_source_spaces_without_template_fallback() -> None:
    parsed = ParsedDrawingModel(
        assets_count=1,
        asset_kinds=["cad"],
        units="mm",
        detected_entities=[
            DrawingEntityRecord(
                asset_name="宿舍楼-1F-建施.dxf",
                category="wall_line",
                layer="WALL",
                points=[Point2D(x=0, y=0), Point2D(x=5000, y=0)],
                source_ref="wall-1f",
                confidence=0.8,
            ),
            DrawingEntityRecord(
                asset_name="宿舍楼-1F-建施.dxf",
                category="room_label",
                layer="TEXT",
                label="宿舍",
                bbox=BoundingBox2D(min_x=500, min_y=500, max_x=3500, max_y=2500),
                source_ref="room-1f",
                confidence=0.8,
                metadata={"fragment_storey_key": "1F", "fragment_id": "frag-1f"},
            ),
        ],
    )
    intent = _intent().model_copy(update={"constraints": _intent().constraints.model_copy(update={"floors": 2})})

    model = BimEngine().build(intent, _plan(), parsed)

    assert [storey.name for storey in model.storeys] == ["1F", "2F"]
    assert [space.name for space in model.storeys[0].spaces] == ["宿舍"]
    assert [space.name for space in model.storeys[1].spaces] == ["宿舍"]
    assert model.metadata["count_reconciliation"]["IfcSpace"]["source"] == 1
    assert model.metadata["count_reconciliation"]["IfcSpace"]["modeled"] == 2
    assert model.metadata["storey_layout_trace"][1]["replicated"] is True
