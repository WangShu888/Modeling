from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from zdong.app.intent_service import StructuredIntentTransformer
from zdong.app.models import BoundingBox2D, DrawingEntityRecord, ParsedDrawingModel, Point2D, SourceBundle


def _build_bundle(prompt: str, building_type_hint: str | None = None) -> SourceBundle:
    return SourceBundle(
        project_id="proj",
        request_id="req",
        version_id="v1",
        prompt=prompt,
        source_mode_hint="auto",
        building_type_hint=building_type_hint,
    )


def _parsed_model() -> ParsedDrawingModel:
    return ParsedDrawingModel(assets_count=0)


def test_ruleset_and_units_assumptions_exist_when_region_missing() -> None:
    bundle = _build_bundle(prompt="请交付一个办公楼草案", building_type_hint="office")
    parsed = _parsed_model()
    intent = StructuredIntentTransformer().transform(bundle, parsed)

    assert any(entry.field == "region" for entry in intent.missing_fields)

    ruleset_assumption = next(
        entry for entry in intent.assumptions if entry.field == "constraints.ruleset"
    )
    assert ruleset_assumption.source == "building_type_ruleset"
    assert ruleset_assumption.value == "cn_office_v1"

    units_assumption = next(
        entry for entry in intent.assumptions if entry.field == "program.units_per_floor"
    )
    assert units_assumption.value == 8
    assert units_assumption.source == "office_template_default"


def test_model_patch_completion_trace_records_selector() -> None:
    prompt = "请替换窗 1800x1500，升级视野"
    bundle = _build_bundle(prompt=prompt, building_type_hint="residential")
    parsed = _parsed_model()
    intent = StructuredIntentTransformer().transform(bundle, parsed)

    assert intent.model_patch is not None

    patch_trace = next(entry for entry in intent.completion_trace if entry.field == "model_patch")
    assert patch_trace.value == intent.model_patch.target_family

    selector_trace = next(entry for entry in intent.completion_trace if entry.field == "element_selector")
    assert selector_trace.value == intent.element_selector.ifc_type


def test_floor_count_can_be_inferred_from_uploaded_floor_assets() -> None:
    bundle = _build_bundle(prompt="依据上传图纸生成宿舍楼模型", building_type_hint="residential")
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
    intent = StructuredIntentTransformer().transform(bundle, parsed)

    assert intent.constraints.floors == 2
    floor_trace = next(entry for entry in intent.completion_trace if entry.field == "constraints.floors")
    assert floor_trace.source_type == "parsed_drawing"


def test_site_area_can_be_inferred_from_parsed_site_boundary() -> None:
    bundle = _build_bundle(prompt="依据上传图纸生成宿舍楼模型", building_type_hint="residential")
    parsed = ParsedDrawingModel(
        assets_count=1,
        asset_kinds=["cad"],
        units="mm",
        detected_entities=[
            DrawingEntityRecord(
                asset_name="总平面.dxf",
                category="site_boundary",
                points=[
                    Point2D(x=0, y=0),
                    Point2D(x=50000, y=0),
                    Point2D(x=50000, y=30000),
                    Point2D(x=0, y=30000),
                ],
                confidence=0.8,
            )
        ],
    )

    intent = StructuredIntentTransformer().transform(bundle, parsed)

    assert intent.site.area_sqm == 1500.0
    site_area_trace = next(entry for entry in intent.completion_trace if entry.field == "site.area_sqm")
    assert site_area_trace.source == "parsed_drawing_inferred_boundary"
    assert site_area_trace.source_type == "parsed_drawing"


def test_spaces_from_drawings_uses_room_label_candidates() -> None:
    bundle = _build_bundle(prompt="依据上传图纸生成宿舍楼模型", building_type_hint="residential")
    parsed = ParsedDrawingModel(
        assets_count=1,
        asset_kinds=["cad"],
        space_candidates_detected=3,
        text_annotation_items=[],
        detected_entities=[
            DrawingEntityRecord(
                asset_name="平面图.dxf",
                category="room_label",
                label="卫生间",
                bbox=BoundingBox2D(min_x=0, min_y=0, max_x=3000, max_y=2000),
                confidence=0.8,
            )
        ],
    )

    intent = StructuredIntentTransformer().transform(bundle, parsed)

    assert intent.program.spaces_from_drawings is True
    trace = next(entry for entry in intent.completion_trace if entry.field == "program.spaces_from_drawings")
    assert trace.source_ref == "parsed_drawing.space_candidates_detected"
