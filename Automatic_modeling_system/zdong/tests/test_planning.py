from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import (
    Constraints,
    DesignIntent,
    MissingField,
    PendingReviewItem,
    ParsedDrawingModel,
    ProgramInfo,
    SiteInfo,
    StyleInfo,
)
from app.planning import ConfigurableModelingPlanner, ConfigurableRuleEngine


def _sample_intent() -> DesignIntent:
    return DesignIntent(
        project_id="proj",
        request_id="req",
        version_id="v1",
        source_mode="cad_to_bim",
        building_type="residential",
        site=SiteInfo(area_sqm=None),
        constraints=Constraints(
            floors=0,
            standard_floor_height_m=2.5,
            first_floor_height_m=2.5,
            ruleset="cn_residential_v1",
            far=7.0,
        ),
        program=ProgramInfo(),
        style=StyleInfo(),
        missing_fields=[
            MissingField(
                field="site.area_sqm",
                reason="未填用地面积",
            )
        ],
    )


def _sample_parsed() -> ParsedDrawingModel:
    return ParsedDrawingModel(
        assets_count=0,
        asset_kinds=[],
        space_boundaries_detected=0,
        space_candidates_detected=0,
        pending_review=[
            PendingReviewItem(
                asset_name="site.dwg",
                category="geometry",
                reason="图纸缺失关键几何",
                severity="warning",
            )
        ],
    )


def test_rule_engine_produces_trace_and_summary() -> None:
    engine = ConfigurableRuleEngine()
    intent = _sample_intent()
    parsed = _sample_parsed()
    result = engine.evaluate(intent, parsed)

    trace = result.metadata["rule_trace"]
    assert len(trace) == 8
    assert trace[0]["rule_id"] == "constraints.floors.min"
    assert result.metadata["issue_summary"]["fatal"] >= 1
    assert result.metadata["rule_catalog"]["constraints.floors.min"].startswith("确保楼层数")
    review_entry = next(entry for entry in trace if entry["rule_id"] == "drawing.pending_review.forward")
    assert review_entry["issue_count"] == 1
    assert "drawing.review.geometry" in review_entry["issues"][0]["code"]


def test_modeling_plan_metadata_supports_explainability_and_replay() -> None:
    engine = ConfigurableRuleEngine()
    intent = _sample_intent()
    parsed = _sample_parsed()
    rule_check = engine.evaluate(intent, parsed)
    planner = ConfigurableModelingPlanner()
    plan = planner.plan(intent, rule_check)

    metadata = plan.metadata
    assert metadata["plan_payload"]["strategy"] == "cad_to_bim"
    assert metadata["plan_steps"][0]["name"] == "normalize_inputs"
    assert metadata["rule_context"]["issue_count"] == len(rule_check.issues)
    assert metadata["rule_context"]["issue_summary"] == rule_check.metadata["issue_summary"]
    assert metadata["rule_context"]["evaluated_rules"] == rule_check.metadata["evaluated_rules"]
    assert metadata["rule_trace"] == rule_check.metadata["rule_trace"]
    assert metadata["rule_catalog"] == rule_check.metadata["rule_catalog"]
    assert metadata["explainability_notes"].startswith("图纸驱动的策略")


def test_rule_engine_escalates_formal_blocking_pending_review_items() -> None:
    engine = ConfigurableRuleEngine()
    intent = _sample_intent().model_copy(
        update={
            "constraints": _sample_intent().constraints.model_copy(update={"floors": 1, "standard_floor_height_m": 3.2}),
            "site": _sample_intent().site.model_copy(update={"area_sqm": 1200.0}),
            "missing_fields": [],
        }
    )
    parsed = ParsedDrawingModel(
        assets_count=1,
        asset_kinds=["cad"],
        space_boundaries_detected=1,
        pending_review=[
            PendingReviewItem(
                asset_name="sample.dxf",
                category="entity_detection_truncated",
                reason="Detected entities were dropped by parser constraints.",
                severity="warning",
            )
        ],
    )

    result = engine.evaluate(intent, parsed)

    issue = next(item for item in result.issues if item.code == "drawing.review.entity_detection_truncated")
    assert issue.severity == "error"
    assert result.status == "failed"


def test_rule_engine_accepts_room_label_space_candidates_for_cad() -> None:
    engine = ConfigurableRuleEngine()
    intent = _sample_intent().model_copy(
        update={
            "constraints": _sample_intent().constraints.model_copy(update={"floors": 1, "standard_floor_height_m": 3.2}),
            "site": _sample_intent().site.model_copy(update={"area_sqm": 1200.0}),
            "missing_fields": [],
        }
    )
    parsed = ParsedDrawingModel(
        assets_count=1,
        asset_kinds=["cad"],
        space_boundaries_detected=0,
        space_candidates_detected=2,
    )

    result = engine.evaluate(intent, parsed)

    assert all(item.code != "drawing.space_boundaries_missing" for item in result.issues)
