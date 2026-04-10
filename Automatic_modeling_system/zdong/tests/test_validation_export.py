from __future__ import annotations

import sys
import re
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ifc_runtime import IfcRuntimeInfo
from app.models import (
    BimElement,
    BimSemanticModel,
    BimStorey,
    Constraints,
    DesignIntent,
    ProgramInfo,
    ProjectSummary,
    RuleCheckResult,
    RuleIssue,
    SiteInfo,
    StyleInfo,
    ValidationIssue,
    ValidationReport,
)
from app.pipeline import ExportService, ValidationService

try:
    import ifcopenshell  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    ifcopenshell = None


def _runtime_info() -> IfcRuntimeInfo:
    return IfcRuntimeInfo(
        exporter="audit_exporter",
        validator="audit_validator",
        schema="IFC4",
        ifcopenshell_available=False,
        ifctester_available=False,
        ifcdiff_available=False,
        formal_backend_ready=False,
        module_statuses=(),
    )


def _make_design_intent(floors: int = 1) -> DesignIntent:
    return DesignIntent(
        project_id="proj-audit",
        request_id="req-audit",
        version_id="ver-audit",
        source_mode="cad_to_bim",
        building_type="residential",
        site=SiteInfo(boundary_source="audit", area_sqm=1200.0, north_angle=5.0),
        constraints=Constraints(
            floors=floors,
            standard_floor_height_m=3.2,
            first_floor_height_m=3.5,
            ruleset="cn_residential_v1",
            far=2.4,
        ),
        program=ProgramInfo(
            spaces_from_drawings=True,
            units_per_floor=2,
            core_type="audit_core",
            first_floor_spaces=["lobby"],
            typical_floor_spaces=["unit", "corridor"],
        ),
        style=StyleInfo(facade="audit", material_palette=["concrete"]),
        deliverables=["ifc"],
    )


def _make_element(ifc_type: str, storey_id: str) -> BimElement:
    guid = str(uuid.uuid4())
    return BimElement(
        element_id=f"{storey_id}_{ifc_type}_{guid[:8]}",
        guid=guid,
        ifc_type=ifc_type,
        name=f"{ifc_type}-audit",
        family_name="audit_family",
        storey_id=storey_id,
        properties={},
    )


def _make_model(intent: DesignIntent, storey_count: int) -> BimSemanticModel:
    types = ("IfcWall", "IfcSlab", "IfcDoor", "IfcWindow")
    storeys: list[BimStorey] = []
    for index in range(storey_count):
        storey_id = f"storey_{index + 1:02d}"
        elements = [_make_element(ifc_type, storey_id) for ifc_type in types]
        storeys.append(
            BimStorey(
                storey_id=storey_id,
                name=f"{index + 1}F",
                elevation_m=index * intent.constraints.standard_floor_height_m,
                elements=elements,
            )
        )
    return BimSemanticModel(
        project_id=intent.project_id,
        version_id=intent.version_id,
        building_type=intent.building_type,
        storeys=storeys,
        element_index={ifc_type: storey_count for ifc_type in types},
        metadata={"strategy": "audit", "units_per_floor": intent.program.units_per_floor},
    )


def test_validation_gate_trace_captures_blockers() -> None:
    intent = _make_design_intent(floors=2)
    model = _make_model(intent, storey_count=1)
    rule_check = RuleCheckResult(
        status="failed",
        issues=[
            RuleIssue(
                code="missing_assets",
                severity="fatal",
                message="缺少必要图纸资产",
                target="assets",
            )
        ],
    )
    validation_service = ValidationService()
    with patch("app.pipeline.detect_ifc_runtime", return_value=_runtime_info()):
        report = validation_service.validate(intent, rule_check, model)

    gate_trace = report.metadata["gate_trace"]
    assert gate_trace["validation_status"] == "failed"
    assert gate_trace["severity_counts"]["fatal"] == 1
    assert gate_trace["severity_counts"]["error"] >= 1
    assert any(item["target"] == "assets" for item in gate_trace["blocking_issues"])
    assert gate_trace["suggested_actions"] == report.fix_suggestions


def test_export_gate_trace_records_blocked_state(tmp_path) -> None:
    intent = _make_design_intent()
    model = _make_model(intent, storey_count=1)
    project = ProjectSummary(project_id="proj-export", name="Export Audit")
    validation = ValidationReport(
        status="failed",
        issues=[
            ValidationIssue(
                severity="error",
                message="导出流程因验证失败被阻断",
                target="validation",
            )
        ],
    )
    export_service = ExportService(tmp_path / "exports")
    with patch("app.pipeline.detect_ifc_runtime", return_value=_runtime_info()):
        bundle = export_service.export(project, intent, model, validation)

    gate_trace = bundle.metadata["gate_trace"]
    assert not bundle.export_allowed
    assert gate_trace["export_allowed"] is False
    assert gate_trace["blocking_messages"] == bundle.blocked_by
    assert gate_trace["ifc_written"] is False
    assert gate_trace["validation_status"] == validation.status
    assert gate_trace["artifact_count"] == len(bundle.artifacts)


def test_export_gate_trace_records_successful_export(tmp_path) -> None:
    intent = _make_design_intent()
    model = _make_model(intent, storey_count=1)
    project = ProjectSummary(project_id="proj-export", name="Export Audit")
    validation = ValidationReport(status="passed")
    export_service = ExportService(tmp_path / "exports-success")
    with patch("app.pipeline.detect_ifc_runtime", return_value=_runtime_info()):
        bundle = export_service.export(project, intent, model, validation)

    gate_trace = bundle.metadata["gate_trace"]
    assert bundle.export_allowed
    assert gate_trace["export_allowed"]
    assert gate_trace["ifc_written"]
    assert gate_trace["blocking_messages"] == []
    assert gate_trace["validation_status"] == validation.status
    assert gate_trace["artifact_count"] == len(bundle.artifacts)
    assert gate_trace["artifact_count"] > 3

    ifc_path = Path(bundle.artifact_dir) / "model.ifc"
    ifc_text = ifc_path.read_text(encoding="utf-8")
    assert "IFCPROJECT(" in ifc_text
    assert "IFCSITE(" in ifc_text
    assert "IFCBUILDING(" in ifc_text
    assert "IFCBUILDINGSTOREY(" in ifc_text
    assert "IFCRELAGGREGATES(" in ifc_text
    assert "IFCRELCONTAINEDINSPATIALSTRUCTURE(" in ifc_text
    assert "IFCLOCALPLACEMENT(" in ifc_text
    assert "IFCPRODUCTDEFINITIONSHAPE(" in ifc_text
    assert "IFCEXTRUDEDAREASOLID(" in ifc_text
    assert "IFCWALL(" in ifc_text
    assert "IFCSLAB(" in ifc_text
    assert "IFCDOOR(" in ifc_text
    assert "IFCWINDOW(" in ifc_text
    assert "IFCSPACE(" not in ifc_text
    assert "IFCPROXY(" not in ifc_text

    guids = re.findall(
        r"IFC(?:PROJECT|SITE|BUILDING|BUILDINGSTOREY|WALL|SLAB|DOOR|WINDOW)\('([^']+)'",
        ifc_text,
    )
    assert guids
    assert all(len(guid) == 22 for guid in guids)
    if ifcopenshell is not None:
        parsed = ifcopenshell.open(str(ifc_path))
        assert len(parsed.by_type("IfcProject")) == 1
        assert len(parsed.by_type("IfcWall")) >= 1
        assert len(parsed.by_type("IfcDoor")) >= 1
        assert len(parsed.by_type("IfcWindow")) >= 1


def test_validation_promotes_formal_blocking_pending_review_to_error() -> None:
    intent = _make_design_intent(floors=1)
    model = _make_model(intent, storey_count=1)
    rule_check = RuleCheckResult(
        status="warning",
        issues=[
            RuleIssue(
                code="drawing.review.entity_detection_truncated",
                severity="warning",
                message="Detected entities reached configured cap.",
                target="drawing",
            )
        ],
    )
    validation_service = ValidationService()
    with patch("app.pipeline.detect_ifc_runtime", return_value=_runtime_info()):
        report = validation_service.validate(intent, rule_check, model)

    gate_trace = report.metadata["gate_trace"]
    assert report.status == "failed"
    assert any(item["target"] == "formal_gate.drawing.review.entity_detection_truncated" for item in gate_trace["blocking_issues"])
    assert gate_trace["formal_blocking_issues"]


def test_validation_reports_count_reconciliation_mismatch() -> None:
    intent = _make_design_intent(floors=1)
    model = _make_model(intent, storey_count=1).model_copy(
        update={
            "element_index": {"IfcWall": 1, "IfcSlab": 1, "IfcDoor": 1, "IfcWindow": 1},
            "metadata": {
                "strategy": "audit",
                "source_entity_totals": {"wall": 2, "door": 1, "window": 1, "space": 1},
                "source_layout_bounds_by_storey": {},
                "count_reconciliation": {},
            },
        }
    )
    rule_check = RuleCheckResult(status="passed", issues=[])
    validation_service = ValidationService()
    with patch("app.pipeline.detect_ifc_runtime", return_value=_runtime_info()):
        report = validation_service.validate(intent, rule_check, model)

    assert any(issue.target == "reconciliation.IfcWall" for issue in report.issues)
    assert report.status == "failed"


def test_validation_reports_position_out_of_bounds() -> None:
    intent = _make_design_intent(floors=1)
    bad_element = _make_element("IfcWall", "storey_01").model_copy(
        update={
            "properties": {
                "geometry_source": "parsed_drawing",
                "source_storey_key": "1F",
                "source_fragment_id": "frag-1f-a",
                "local_x_m": 12.0,
                "local_y_m": 0.0,
                "shape_width_m": 2.0,
                "shape_depth_m": 0.2,
            }
        }
    )
    model = BimSemanticModel(
        project_id=intent.project_id,
        version_id=intent.version_id,
        building_type=intent.building_type,
        storeys=[
            BimStorey(
                storey_id="storey_01",
                name="1F",
                elevation_m=0.0,
                spaces=[],
                elements=[bad_element],
            )
        ],
        element_index={"IfcWall": 1},
        metadata={
            "source_layout_bounds_by_storey": {
                "1F": {"min_x_m": 0.0, "min_y_m": 0.0, "max_x_m": 10.0, "max_y_m": 5.0}
            },
            "source_entity_totals": {"wall": 1, "door": 0, "window": 0, "space": 0},
            "count_reconciliation": {},
        },
    )
    rule_check = RuleCheckResult(status="passed", issues=[])
    validation_service = ValidationService()
    with patch("app.pipeline.detect_ifc_runtime", return_value=_runtime_info()):
        report = validation_service.validate(intent, rule_check, model)

    assert any("边界" in issue.message for issue in report.issues)
    assert report.status == "failed"


def test_validation_blocks_template_fallback_when_cad_has_no_source_geometry() -> None:
    intent = _make_design_intent(floors=1)
    model = _make_model(intent, storey_count=1).model_copy(
        update={
            "metadata": {
                "strategy": "audit",
                "geometry_source": "template_fallback",
                "source_wall_entities": 0,
                "source_window_entities": 0,
                "source_door_entities": 0,
                "source_storey_keys": [],
                "source_entity_totals": {"wall": 0, "door": 0, "window": 0, "space": 0},
                "source_layout_bounds_by_storey": {},
                "count_reconciliation": {},
            }
        }
    )
    rule_check = RuleCheckResult(
        status="warning",
        issues=[
            RuleIssue(
                code="drawing.review.wall_detection_low_confidence",
                severity="warning",
                message="No wall lines were confidently detected; check layer mapping and source drawing conventions.",
                target="sample.dxf",
            ),
            RuleIssue(
                code="drawing.review.proxy_entities_unresolved",
                severity="warning",
                message="Semantic CAD layers contain ACAD_PROXY_ENTITY objects that were not converted into modelable geometry: WALL.",
                target="sample.dxf",
            ),
        ],
    )
    validation_service = ValidationService()
    with patch("app.pipeline.detect_ifc_runtime", return_value=_runtime_info()):
        report = validation_service.validate(intent, rule_check, model)

    gate_trace = report.metadata["gate_trace"]
    formal_targets = {item["target"] for item in gate_trace["formal_blocking_issues"]}
    assert report.status == "failed"
    assert "formal_gate.drawing.source_geometry_missing" in formal_targets
    assert "formal_gate.drawing.proxy_entities_unresolved" in formal_targets
    assert any(issue.target == "formal_gate.drawing.source_geometry_missing" for issue in report.issues)
    assert "ACAD_PROXY_ENTITY" in report.fix_suggestions[0]


def test_validation_does_not_block_proxy_review_when_real_source_geometry_exists() -> None:
    intent = _make_design_intent(floors=1)
    model = _make_model(intent, storey_count=1).model_copy(
        update={
            "metadata": {
                "strategy": "audit",
                "geometry_source": "parsed_drawing",
                "source_wall_entities": 1,
                "source_window_entities": 1,
                "source_door_entities": 1,
                "source_storey_keys": ["1F"],
                "source_entity_totals": {"wall": 1, "door": 1, "window": 1, "space": 1},
                "source_layout_bounds_by_storey": {},
                "count_reconciliation": {},
            }
        }
    )
    rule_check = RuleCheckResult(
        status="warning",
        issues=[
            RuleIssue(
                code="drawing.review.proxy_entities_unresolved",
                severity="warning",
                message="Semantic CAD layers still contain unresolved ACAD_PROXY_ENTITY objects.",
                target="sample.dxf",
            ),
        ],
    )
    validation_service = ValidationService()
    with patch("app.pipeline.detect_ifc_runtime", return_value=_runtime_info()):
        report = validation_service.validate(intent, rule_check, model)

    gate_trace = report.metadata["gate_trace"]
    formal_targets = {item["target"] for item in gate_trace["formal_blocking_issues"]}
    assert report.status == "warning"
    assert "formal_gate.drawing.source_geometry_missing" not in formal_targets
    assert "formal_gate.drawing.proxy_entities_unresolved" not in formal_targets


def test_validation_blocks_template_fallback_without_proxy_signals_when_source_geometry_missing() -> None:
    intent = _make_design_intent(floors=1)
    model = _make_model(intent, storey_count=1).model_copy(
        update={
            "metadata": {
                "strategy": "audit",
                "geometry_source": "template_fallback",
                "source_wall_entities": 0,
                "source_window_entities": 0,
                "source_door_entities": 0,
                "source_storey_keys": [],
                "source_entity_totals": {"wall": 0, "door": 0, "window": 0, "space": 0},
                "source_layout_bounds_by_storey": {},
                "count_reconciliation": {},
            }
        }
    )
    rule_check = RuleCheckResult(status="passed", issues=[])
    validation_service = ValidationService()
    with patch("app.pipeline.detect_ifc_runtime", return_value=_runtime_info()):
        report = validation_service.validate(intent, rule_check, model)

    gate_trace = report.metadata["gate_trace"]
    formal_targets = {item["target"] for item in gate_trace["formal_blocking_issues"]}
    assert report.status == "failed"
    assert "formal_gate.drawing.source_geometry_missing" in formal_targets


def test_validation_allows_edge_aligned_window_anchor_within_bounds() -> None:
    intent = _make_design_intent(floors=1)
    edge_window = _make_element("IfcWindow", "storey_01").model_copy(
        update={
            "properties": {
                "geometry_source": "parsed_drawing",
                "source_category": "window_block",
                "source_storey_key": "1F",
                "source_fragment_id": "frag-1f-a",
                "local_x_m": 9.25,
                "local_y_m": 2.44,
                "shape_width_m": 1.5,
                "shape_depth_m": 0.12,
            }
        }
    )
    model = BimSemanticModel(
        project_id=intent.project_id,
        version_id=intent.version_id,
        building_type=intent.building_type,
        storeys=[
            BimStorey(
                storey_id="storey_01",
                name="1F",
                elevation_m=0.0,
                spaces=[],
                elements=[edge_window],
            )
        ],
        element_index={"IfcWindow": 1},
        metadata={
            "source_layout_bounds_by_storey": {
                "1F": {"min_x_m": 0.0, "min_y_m": 0.0, "max_x_m": 10.0, "max_y_m": 5.0}
            },
            "source_entity_totals": {"wall": 0, "door": 0, "window": 1, "space": 0},
            "count_reconciliation": {},
        },
    )
    rule_check = RuleCheckResult(status="passed", issues=[])
    validation_service = ValidationService()
    with patch("app.pipeline.detect_ifc_runtime", return_value=_runtime_info()):
        report = validation_service.validate(intent, rule_check, model)

    assert not any(issue.target == edge_window.element_id and "边界" in issue.message for issue in report.issues)
    assert report.status == "warning"


def test_export_blocks_template_fallback_source_geometry_formal_gate(tmp_path) -> None:
    intent = _make_design_intent()
    model = _make_model(intent, storey_count=1)
    project = ProjectSummary(project_id="proj-export-template", name="Export Template Gate")
    validation = ValidationReport(
        status="warning",
        issues=[],
        metadata={
            "gate_trace": {
                "formal_blocking_issues": [
                    {
                        "severity": "error",
                        "target": "formal_gate.drawing.source_geometry_missing",
                        "message": "CAD 图纸未提取到主体墙体几何，当前 IFC 已退回模板生成，正式导出已阻断。",
                    }
                ]
            }
        },
    )
    export_service = ExportService(tmp_path / "exports-template")
    with patch("app.pipeline.detect_ifc_runtime", return_value=_runtime_info()):
        bundle = export_service.export(project, intent, model, validation)

    artifact_names = {artifact.name for artifact in bundle.artifacts}
    gate_trace = bundle.metadata["gate_trace"]
    assert not bundle.export_allowed
    assert "model.ifc" not in artifact_names
    assert gate_trace["formal_blocking_issues"][0]["target"] == "formal_gate.drawing.source_geometry_missing"
    assert gate_trace["blocking_messages"] == bundle.blocked_by


def test_export_blocks_when_formal_blockers_present_even_if_status_warning(tmp_path) -> None:
    intent = _make_design_intent()
    model = _make_model(intent, storey_count=1)
    project = ProjectSummary(project_id="proj-export-formal", name="Export Formal Gate")
    validation = ValidationReport(
        status="warning",
        issues=[],
        metadata={
            "gate_trace": {
                "formal_blocking_issues": [
                    {
                        "severity": "error",
                        "target": "formal_gate.drawing.review.multi_storey_asset_collapsed",
                        "message": "multi_storey_asset_collapsed unresolved.",
                    }
                ]
            }
        },
    )
    export_service = ExportService(tmp_path / "exports-formal")
    with patch("app.pipeline.detect_ifc_runtime", return_value=_runtime_info()):
        bundle = export_service.export(project, intent, model, validation)

    gate_trace = bundle.metadata["gate_trace"]
    assert not bundle.export_allowed
    assert gate_trace["formal_blocking_issues"]
    assert "multi_storey_asset_collapsed unresolved." in gate_trace["blocking_messages"]
