from pathlib import Path

import ezdxf

from jianmo.app.models import ModelingRequestInput, ProjectCreateRequest
from jianmo.app.pipeline import ModelingPipeline
from jianmo.app.store import InMemoryStore


def make_pipeline(tmp_path: Path) -> ModelingPipeline:
    return ModelingPipeline(store=InMemoryStore(), export_root=tmp_path / "generated")


def make_runtime_dir(name: str) -> Path:
    root = Path.cwd() / "test_runtime" / name
    root.mkdir(parents=True, exist_ok=True)
    return root


def test_text_pipeline_generates_exportable_snapshot() -> None:
    runtime_dir = make_runtime_dir("pipeline_text")
    pipeline = make_pipeline(runtime_dir)
    project = pipeline.create_project(
        ProjectCreateRequest(name="文本建模测试", building_type="residential", region="CN-SH")
    )

    snapshot = pipeline.run(
        project.project_id,
        ModelingRequestInput(
            prompt="生成一栋 12 层住宅楼，两梯四户，标准层层高 3.0m，输出 IFC。",
            building_type="residential",
            region="CN-SH",
            floors=12,
            standard_floor_height_m=3.0,
            first_floor_height_m=3.3,
            site_area_sqm=8000,
            far=2.5,
        ),
    )

    assert snapshot.design_intent.source_mode == "text_only"
    assert snapshot.rule_check.status == "passed"
    assert snapshot.validation.status == "passed"
    assert snapshot.export_bundle.export_allowed is True
    assert any(artifact.name == "model.ifc" for artifact in snapshot.export_bundle.artifacts)


def test_window_replacement_updates_matching_windows() -> None:
    runtime_dir = make_runtime_dir("pipeline_replace")
    drawing_path = runtime_dir / "standard-floor.dxf"
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 4
    doc.layers.add("A-AXIS")
    doc.layers.add("A-WALL")
    doc.layers.add("A-ROOM")
    doc.layers.add("A-WIND")
    msp = doc.modelspace()
    msp.add_line((0, 0), (0, 6000), dxfattribs={"layer": "A-AXIS"})
    msp.add_lwpolyline(
        [(0, 0), (6000, 0), (6000, 4000), (0, 4000), (0, 0)],
        dxfattribs={"layer": "A-WALL"},
    )
    msp.add_lwpolyline(
        [(300, 300), (5700, 300), (5700, 3700), (300, 3700), (300, 300)],
        dxfattribs={"layer": "A-ROOM"},
    )
    block = doc.blocks.new(name="WINDOW_800x1200")
    block.add_line((0, 0), (800, 0))
    msp.add_blockref("WINDOW_800x1200", (1200, 800), dxfattribs={"layer": "A-WIND"})
    doc.saveas(drawing_path)
    pipeline = make_pipeline(runtime_dir)
    project = pipeline.create_project(
        ProjectCreateRequest(name="图纸建模测试", building_type="residential", region="CN-BJ")
    )

    snapshot = pipeline.run(
        project.project_id,
        ModelingRequestInput(
            prompt="根据图纸建模，并将所有 800x1200 的窗替换为落地窗。",
            building_type="residential",
            region="CN-BJ",
            floors=6,
            standard_floor_height_m=3.0,
            first_floor_height_m=3.3,
            site_area_sqm=3000,
            assets=[
                {
                    "filename": "standard-floor.dxf",
                    "media_type": "application/acad",
                    "path": str(drawing_path),
                    "description": "标准层平面图",
                }
            ],
        ),
    )

    assert snapshot.design_intent.source_mode == "cad_to_bim"
    assert snapshot.design_intent.model_patch is not None
    assert snapshot.bim_model.metadata["replacement_count"] > 0
    assert snapshot.rule_check.status in {"passed", "warning"}
    assert snapshot.validation.status in {"passed", "warning"}


def test_structured_intent_and_replay_metadata_are_recorded() -> None:
    runtime_dir = make_runtime_dir("pipeline_structured")
    pipeline = make_pipeline(runtime_dir)
    project = pipeline.create_project(
        ProjectCreateRequest(name="结构化转化测试", building_type="residential", region="CN-SH")
    )

    snapshot = pipeline.run(
        project.project_id,
        ModelingRequestInput(
            prompt="生成一栋 2 层住宅楼，两梯四户，输出 IFC。",
            building_type="residential",
            region="CN-SH",
            floors=2,
        ),
    )

    assert snapshot.design_intent.metadata["intent_provider"] == "heuristic_structured_v1"
    assert snapshot.design_intent.metadata["schema_version"] == "jianmo.intent.v1"
    assert any(
        item.field == "constraints.standard_floor_height_m" and item.source_type == "template_default"
        for item in snapshot.design_intent.completion_trace
    )
    assert snapshot.rule_check.ruleset_version == "rules.2026-03-31"
    assert "constraints.standard_floor_height.min" in snapshot.rule_check.applied_rules
    assert snapshot.rule_check.replay_token
    assert snapshot.modeling_plan.planner_version == "planner.2026-03-31"
    assert snapshot.modeling_plan.plan_id
    assert snapshot.modeling_plan.replay_token


def test_validation_and_export_capture_ifc_runtime_metadata() -> None:
    runtime_dir = make_runtime_dir("pipeline_export_runtime")
    pipeline = make_pipeline(runtime_dir)
    project = pipeline.create_project(
        ProjectCreateRequest(name="IFC 运行时测试", building_type="office", region="CN-SZ")
    )

    snapshot = pipeline.run(
        project.project_id,
        ModelingRequestInput(
            prompt="生成一栋 8 层办公楼，首层层高 4.5m，标准层层高 3.9m，输出 IFC。",
            building_type="office",
            region="CN-SZ",
            floors=8,
            standard_floor_height_m=3.9,
            first_floor_height_m=4.5,
            site_area_sqm=5600,
        ),
    )

    artifact_names = {artifact.name for artifact in snapshot.export_bundle.artifacts}
    assert snapshot.validation.metadata["ifc_exporter"] == "text_ifc_fallback"
    assert snapshot.export_bundle.metadata["ifc_exporter"] == "text_ifc_fallback"
    assert snapshot.export_bundle.metadata["ifc_schema"] == "IFC4"
    assert "model.semantic.json" in artifact_names
    assert "export-log.json" in artifact_names
