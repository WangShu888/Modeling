from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from zdong.app.main import create_app
from zdong.app.models import DrawingEntityRecord, LayerMapEntry, ParsedDrawingModel, PendingReviewItem, Point2D
from zdong.app.store import InMemoryStore


def _resolve_real_dxf_path() -> Path:
    candidates = [
        Path("/workspace/自动建模系统/图纸文件/专用宿舍楼-建施.dxf"),
        Path("/workspace/自动建模系统/图纸文件/.jianmo-odafc/530c2504f6484235a6d4e7190f4d8aa3/专用宿舍楼-建施.dxf"),
        Path("/workspace/自动建模系统/新建文件夹/.jianmo-odafc/530c2504f6484235a6d4e7190f4d8aa3/专用宿舍楼-建施.dxf"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate

    matches = sorted(Path("/workspace/自动建模系统").rglob("专用宿舍楼-建施.dxf"))
    if matches:
        return matches[0]
    raise AssertionError("未找到真实 DXF 样本文件：专用宿舍楼-建施.dxf")


REAL_DXF_PATH = _resolve_real_dxf_path()


def _resolve_real_structural_dxf_path() -> Path:
    candidates = [
        Path("/workspace/自动建模系统/图纸文件/专用宿舍楼-结施.dxf"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate

    matches = sorted(Path("/workspace/自动建模系统").rglob("专用宿舍楼-结施.dxf"))
    if matches:
        return matches[0]
    raise AssertionError("未找到真实 DXF 结构样本文件：专用宿舍楼-结施.dxf")


REAL_STRUCTURAL_DXF_PATH = _resolve_real_structural_dxf_path()


def _resolve_real_tch_floor_plan_paths() -> tuple[Path, Path]:
    root = Path("/workspace/自动建模系统/图纸文件")
    floor_1 = root / "一层平面图.dxf"
    floor_2 = root / "二层平面图.dxf"
    if floor_1.is_file() and floor_2.is_file():
        return floor_1, floor_2
    raise AssertionError("未找到真实天正平面图样本文件：一层平面图.dxf / 二层平面图.dxf")


REAL_TCH_FLOOR_1_PATH, REAL_TCH_FLOOR_2_PATH = _resolve_real_tch_floor_plan_paths()


def test_real_dxf_can_complete_delivery_pipeline(tmp_path: Path) -> None:
    assert REAL_DXF_PATH.is_file()

    app = create_app(
        runtime_root=tmp_path / "runtime",
        store=InMemoryStore(),
        export_root=tmp_path / "exports",
        asset_root=tmp_path / "assets",
    )
    client = TestClient(app)

    project_response = client.post(
        "/api/projects",
        json={"name": "DXF Delivery", "building_type": "residential", "region": "CN-SH"},
    )
    assert project_response.status_code == 200
    project_id = project_response.json()["project_id"]

    with REAL_DXF_PATH.open("rb") as handle:
        upload_response = client.post(
            f"/api/projects/{project_id}/assets",
            files={"file": (REAL_DXF_PATH.name, handle, "image/vnd.dxf")},
            data={"description": "真实DXF建筑图"},
        )
    assert upload_response.status_code == 200
    asset_id = upload_response.json()["asset_id"]

    request_response = client.post(
        f"/api/projects/{project_id}/requests",
        json={
            "prompt": "依据上传DXF生成宿舍楼IFC",
            "building_type": "residential",
            "region": "CN-SH",
            "asset_ids": [asset_id],
        },
    )
    assert request_response.status_code == 200
    request_id = request_response.json()["request_id"]

    parse_response = client.post(f"/api/projects/{project_id}/requests/{request_id}/parse")
    assert parse_response.status_code == 200
    parsed = parse_response.json()
    assert parsed["assets_count"] == 1
    assert parsed["asset_kinds"] == ["cad"]
    assert len(parsed["recognized_layers"]) > 0

    run_response = client.post(f"/api/projects/{project_id}/requests/{request_id}/run")
    assert run_response.status_code == 200
    snapshot = run_response.json()

    assert snapshot["rule_check"]["status"] in {"warning", "passed"}
    assert snapshot["validation"]["status"] in {"warning", "passed"}
    assert snapshot["export_bundle"]["export_allowed"] is True
    assert snapshot["bim_model"]["metadata"]["geometry_source"] == "parsed_drawing"
    assert snapshot["bim_model"]["metadata"]["source_wall_entities"] > 50
    assert snapshot["bim_model"]["element_index"]["IfcWall"] > 50
    assert snapshot["bim_model"]["element_index"]["IfcWindow"] >= 7
    assert snapshot["bim_model"]["element_index"]["IfcDoor"] >= 4

    artifact_names = {artifact["name"] for artifact in snapshot["export_bundle"]["artifacts"]}
    assert {"intent.json", "validation.json", "model.ifc", "export-log.json"}.issubset(artifact_names)

    ifc_path = Path(snapshot["export_bundle"]["artifact_dir"]) / "model.ifc"
    ifc_text = ifc_path.read_text(encoding="utf-8")
    assert "IFCPROJECT(" in ifc_text
    assert "IFCSITE(" in ifc_text
    assert "IFCBUILDING(" in ifc_text
    assert "IFCBUILDINGSTOREY(" in ifc_text
    assert "IFCEXTRUDEDAREASOLID(" in ifc_text
    assert "IFCWALL(" in ifc_text
    assert "IFCWINDOW(" in ifc_text


def test_real_structural_dxf_no_longer_blocks_export_on_multi_storey_collapse(tmp_path: Path) -> None:
    assert REAL_STRUCTURAL_DXF_PATH.is_file()

    app = create_app(
        runtime_root=tmp_path / "runtime-struct",
        store=InMemoryStore(),
        export_root=tmp_path / "exports-struct",
        asset_root=tmp_path / "assets-struct",
    )
    client = TestClient(app)

    project_response = client.post(
        "/api/projects",
        json={"name": "DXF Structural Delivery", "building_type": "residential", "region": "CN-SH"},
    )
    assert project_response.status_code == 200
    project_id = project_response.json()["project_id"]

    with REAL_STRUCTURAL_DXF_PATH.open("rb") as handle:
        upload_response = client.post(
            f"/api/projects/{project_id}/assets",
            files={"file": (REAL_STRUCTURAL_DXF_PATH.name, handle, "image/vnd.dxf")},
            data={"description": "真实DXF结构图"},
        )
    assert upload_response.status_code == 200
    asset_id = upload_response.json()["asset_id"]

    request_response = client.post(
        f"/api/projects/{project_id}/requests",
        json={
            "prompt": "依据上传DXF生成宿舍楼IFC",
            "building_type": "residential",
            "region": "CN-SH",
            "asset_ids": [asset_id],
        },
    )
    assert request_response.status_code == 200
    request_id = request_response.json()["request_id"]

    parse_response = client.post(f"/api/projects/{project_id}/requests/{request_id}/parse")
    assert parse_response.status_code == 200
    parsed = parse_response.json()

    run_response = client.post(f"/api/projects/{project_id}/requests/{request_id}/run")
    assert run_response.status_code == 200
    snapshot = run_response.json()

    pending_review_codes = {item["category"] for item in parsed["pending_review"]}
    assert "multi_storey_asset_collapsed" not in pending_review_codes
    assert snapshot["rule_check"]["status"] in {"warning", "passed"}
    assert snapshot["validation"]["status"] in {"warning", "passed"}
    assert snapshot["export_bundle"]["export_allowed"] is True
    assert {"1F", "2F"}.issubset(set(snapshot["bim_model"]["metadata"]["source_storey_keys"]))


def test_real_arch_and_structural_dxf_use_full_parser_capabilities(tmp_path: Path) -> None:
    app = create_app(
        runtime_root=tmp_path / "runtime-combo",
        store=InMemoryStore(),
        export_root=tmp_path / "exports-combo",
        asset_root=tmp_path / "assets-combo",
    )
    client = TestClient(app)

    project_response = client.post(
        "/api/projects",
        json={"name": "DXF Combined Delivery", "building_type": "residential", "region": "CN-SH"},
    )
    assert project_response.status_code == 200
    project_id = project_response.json()["project_id"]

    asset_ids: list[str] = []
    for path, description in (
        (REAL_DXF_PATH, "真实DXF建筑图"),
        (REAL_STRUCTURAL_DXF_PATH, "真实DXF结构图"),
    ):
        with path.open("rb") as handle:
            upload_response = client.post(
                f"/api/projects/{project_id}/assets",
                files={"file": (path.name, handle, "image/vnd.dxf")},
                data={"description": description},
            )
        assert upload_response.status_code == 200
        asset_ids.append(upload_response.json()["asset_id"])

    request_response = client.post(
        f"/api/projects/{project_id}/requests",
        json={
            "prompt": "依据上传DXF生成宿舍楼IFC",
            "building_type": "residential",
            "region": "CN-SH",
            "asset_ids": asset_ids,
        },
    )
    assert request_response.status_code == 200
    request_id = request_response.json()["request_id"]

    parse_response = client.post(f"/api/projects/{project_id}/requests/{request_id}/parse")
    assert parse_response.status_code == 200
    parsed = parse_response.json()
    assert parsed["space_candidates_detected"] > 0

    run_response = client.post(f"/api/projects/{project_id}/requests/{request_id}/run")
    assert run_response.status_code == 200
    snapshot = run_response.json()

    rule_issue_codes = {item["code"] for item in snapshot["rule_check"]["issues"]}
    validation_targets = {item["target"] for item in snapshot["validation"]["issues"]}
    reconciliation = snapshot["bim_model"]["metadata"]["count_reconciliation"]

    assert snapshot["design_intent"]["site"]["area_sqm"] is not None
    assert "drawing.space_boundaries_missing" not in rule_issue_codes
    assert "site.area_missing" not in rule_issue_codes
    assert "parsed_drawing.space_candidates_detected" not in validation_targets
    assert "site.area_sqm" not in validation_targets
    assert reconciliation["IfcWall"]["delta"] == 0
    assert reconciliation["IfcDoor"]["delta"] == 0
    assert reconciliation["IfcWindow"]["delta"] == 0
    assert reconciliation["IfcSpace"]["delta"] == 0
    assert snapshot["export_bundle"]["export_allowed"] is True


def test_real_tch_floor_plans_drive_delivery_pipeline_without_proxy_fallback(tmp_path: Path) -> None:
    app = create_app(
        runtime_root=tmp_path / "runtime-tch",
        store=InMemoryStore(),
        export_root=tmp_path / "exports-tch",
        asset_root=tmp_path / "assets-tch",
    )
    client = TestClient(app)

    project_response = client.post(
        "/api/projects",
        json={"name": "TCH Floor Plans", "building_type": "residential", "region": "CN-SH"},
    )
    assert project_response.status_code == 200
    project_id = project_response.json()["project_id"]

    asset_ids: list[str] = []
    for path in (REAL_TCH_FLOOR_1_PATH, REAL_TCH_FLOOR_2_PATH):
        with path.open("rb") as handle:
            upload_response = client.post(
                f"/api/projects/{project_id}/assets",
                files={"file": (path.name, handle, "image/vnd.dxf")},
                data={"description": path.stem},
            )
        assert upload_response.status_code == 200
        asset_ids.append(upload_response.json()["asset_id"])

    request_response = client.post(
        f"/api/projects/{project_id}/requests",
        json={
            "prompt": "依据上传DXF生成宿舍楼IFC",
            "building_type": "residential",
            "region": "CN-SH",
            "asset_ids": asset_ids,
        },
    )
    assert request_response.status_code == 200
    request_id = request_response.json()["request_id"]

    parse_response = client.post(f"/api/projects/{project_id}/requests/{request_id}/parse")
    assert parse_response.status_code == 200
    parsed = parse_response.json()

    category_counts: dict[str, int] = {}
    for entity in parsed["detected_entities"]:
        category = entity["category"]
        category_counts[category] = category_counts.get(category, 0) + 1

    assert category_counts["wall_line"] == 30
    assert category_counts["window_block"] >= 8
    assert category_counts["door_block"] >= 6
    assert "WALL" in parsed["recognized_layers"]
    assert "COLUMN" in parsed["recognized_layers"]

    run_response = client.post(f"/api/projects/{project_id}/requests/{request_id}/run")
    assert run_response.status_code == 200
    snapshot = run_response.json()

    assert snapshot["bim_model"]["metadata"]["geometry_source"] == "parsed_drawing"
    assert snapshot["bim_model"]["metadata"]["source_wall_entities"] == 30
    assert snapshot["validation"]["status"] in {"warning", "passed"}
    assert snapshot["export_bundle"]["export_allowed"] is True
    assert snapshot["bim_model"]["element_index"]["IfcWall"] == 30
    assert snapshot["bim_model"]["element_index"]["IfcDoor"] >= 6
    assert snapshot["bim_model"]["element_index"]["IfcWindow"] >= 8

    first_storey = snapshot["bim_model"]["storeys"][0]
    wall_by_source = {
        element["properties"].get("source_ref"): element["properties"]
        for element in first_storey["elements"]
        if element["ifc_type"] == "IfcWall"
    }
    opening_props = [
        element["properties"]
        for element in first_storey["elements"]
        if element["ifc_type"] in {"IfcDoor", "IfcWindow"}
    ]

    assert wall_by_source["3F9"]["geometry_anchor"] == "center"
    assert wall_by_source["3F9"]["local_x_m"] > 1.0
    assert wall_by_source["3F9"]["shape_depth_m"] < 0.12
    assert wall_by_source["tch-wall-vertical:53158.1:1"]["local_y_m"] > 1.0
    assert any(abs(float(props["rotation_deg"])) == 90.0 for props in opening_props)
    assert any(props.get("geometry_anchor") == "center" for props in opening_props)


def test_run_blocks_export_when_uploaded_cad_falls_back_to_template_geometry(tmp_path: Path) -> None:
    app = create_app(
        runtime_root=tmp_path / "runtime-proxy",
        store=InMemoryStore(),
        export_root=tmp_path / "exports-proxy",
        asset_root=tmp_path / "assets-proxy",
    )
    app.state.pipeline.drawing_parser.parse = lambda bundle: ParsedDrawingModel(
        assets_count=1,
        asset_kinds=["cad"],
        recognized_layers=["WALL", "WINDOW"],
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
        pending_review=[
            PendingReviewItem(
                asset_name="proxy-sample.dxf",
                category="wall_detection_low_confidence",
                reason="No wall lines were confidently detected; check layer mapping and source drawing conventions.",
                severity="warning",
            ),
            PendingReviewItem(
                asset_name="proxy-sample.dxf",
                category="proxy_entities_unresolved",
                reason="Semantic CAD layers contain ACAD_PROXY_ENTITY objects that were not converted into modelable geometry: WALL, WINDOW.",
                severity="warning",
            ),
        ],
    )
    client = TestClient(app)

    project_response = client.post(
        "/api/projects",
        json={"name": "DXF Proxy Gate", "building_type": "residential", "region": "CN-SH"},
    )
    assert project_response.status_code == 200
    project_id = project_response.json()["project_id"]

    upload_response = client.post(
        f"/api/projects/{project_id}/assets",
        files={"file": ("proxy-sample.dxf", b"0\nEOF\n", "image/vnd.dxf")},
        data={"description": "代理实体样本"},
    )
    assert upload_response.status_code == 200
    asset_id = upload_response.json()["asset_id"]

    request_response = client.post(
        f"/api/projects/{project_id}/requests",
        json={
            "prompt": "依据上传DXF生成宿舍楼IFC",
            "building_type": "residential",
            "region": "CN-SH",
            "asset_ids": [asset_id],
        },
    )
    assert request_response.status_code == 200
    request_id = request_response.json()["request_id"]

    run_response = client.post(f"/api/projects/{project_id}/requests/{request_id}/run")
    assert run_response.status_code == 200
    snapshot = run_response.json()

    artifact_names = {artifact["name"] for artifact in snapshot["export_bundle"]["artifacts"]}
    blocked_messages = snapshot["export_bundle"]["blocked_by"]

    assert snapshot["bim_model"]["metadata"]["geometry_source"] == "template_fallback"
    assert snapshot["bim_model"]["metadata"]["source_wall_entities"] == 0
    assert snapshot["validation"]["status"] == "failed"
    assert snapshot["export_bundle"]["export_allowed"] is False
    assert any("模板生成" in message for message in blocked_messages)
    assert any("ACAD_PROXY_ENTITY" in message for message in blocked_messages)
    assert "model.ifc" not in artifact_names


def test_run_allows_proxy_review_when_alias_geometry_entities_exist(tmp_path: Path) -> None:
    app = create_app(
        runtime_root=tmp_path / "runtime-proxy-alias",
        store=InMemoryStore(),
        export_root=tmp_path / "exports-proxy-alias",
        asset_root=tmp_path / "assets-proxy-alias",
    )
    app.state.pipeline.drawing_parser.parse = lambda bundle: ParsedDrawingModel(
        assets_count=1,
        asset_kinds=["cad"],
        units="mm",
        recognized_layers=["WALL", "WINDOW", "DOOR"],
        detected_entities=[
            DrawingEntityRecord(
                asset_name="proxy-aliased.dxf",
                category="proxy_wall_path",
                layer="WALL",
                points=[
                    Point2D(x=0.0, y=0.0),
                    Point2D(x=6000.0, y=0.0),
                    Point2D(x=6000.0, y=200.0),
                    Point2D(x=0.0, y=200.0),
                ],
                source_ref="wall-proxy-1",
                confidence=0.72,
            ),
            DrawingEntityRecord(
                asset_name="proxy-aliased.dxf",
                category="proxy_window_anchor",
                layer="WINDOW",
                points=[Point2D(x=3000.0, y=200.0)],
                source_ref="window-proxy-1",
                confidence=0.7,
            ),
            DrawingEntityRecord(
                asset_name="proxy-aliased.dxf",
                category="proxy_door_anchor",
                layer="DOOR",
                points=[Point2D(x=1200.0, y=0.0)],
                source_ref="door-proxy-1",
                confidence=0.7,
            ),
        ],
        pending_review=[
            PendingReviewItem(
                asset_name="proxy-aliased.dxf",
                category="proxy_entities_unresolved",
                reason="Proxy graphic fallback extracted partial geometry; unresolved ACAD_PROXY_ENTITY records remain.",
                severity="warning",
            ),
        ],
    )
    client = TestClient(app)

    project_response = client.post(
        "/api/projects",
        json={"name": "DXF Proxy Alias", "building_type": "residential", "region": "CN-SH"},
    )
    assert project_response.status_code == 200
    project_id = project_response.json()["project_id"]

    upload_response = client.post(
        f"/api/projects/{project_id}/assets",
        files={"file": ("proxy-aliased.dxf", b"0\nEOF\n", "image/vnd.dxf")},
        data={"description": "代理图元别名样本"},
    )
    assert upload_response.status_code == 200
    asset_id = upload_response.json()["asset_id"]

    request_response = client.post(
        f"/api/projects/{project_id}/requests",
        json={
            "prompt": "依据上传DXF生成宿舍楼IFC",
            "building_type": "residential",
            "region": "CN-SH",
            "asset_ids": [asset_id],
        },
    )
    assert request_response.status_code == 200
    request_id = request_response.json()["request_id"]

    run_response = client.post(f"/api/projects/{project_id}/requests/{request_id}/run")
    assert run_response.status_code == 200
    snapshot = run_response.json()

    artifact_names = {artifact["name"] for artifact in snapshot["export_bundle"]["artifacts"]}
    formal_targets = {
        issue["target"]
        for issue in snapshot["validation"]["metadata"]["gate_trace"]["formal_blocking_issues"]
    }

    assert snapshot["bim_model"]["metadata"]["geometry_source"] == "parsed_drawing"
    assert snapshot["bim_model"]["metadata"]["source_wall_entities"] == 1
    assert snapshot["validation"]["status"] in {"warning", "passed"}
    assert "formal_gate.drawing.source_geometry_missing" not in formal_targets
    assert snapshot["export_bundle"]["export_allowed"] is True
    assert "model.ifc" in artifact_names
