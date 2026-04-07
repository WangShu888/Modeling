from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field

from .models import DesignIntent, ModelingPlan, ParsedDrawingModel, PlanStep, RuleCheckResult, RuleIssue


_RULE_CONFIG_PATH = Path(__file__).resolve().parent / "config" / "rules.default.json"
_PLANNER_CONFIG_PATH = Path(__file__).resolve().parent / "config" / "planner.default.json"

_SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2, "fatal": 3}
_FORMAL_BLOCKING_PENDING_REVIEW = {"entity_detection_truncated", "multi_storey_asset_collapsed"}


class SharedRuleConfig(BaseModel):
    max_far_warning: float = 5.0
    cad_requires_assets: bool = True
    cad_missing_space_boundaries_severity: str = "warning"
    site_area_missing_severity: str = "warning"


class RulesetConfig(BaseModel):
    building_type: str
    min_floor_count: int = 1
    min_standard_floor_height_m: float
    min_first_floor_height_m: float


class RuleConfigBundle(BaseModel):
    version: str
    shared: SharedRuleConfig
    rulesets: dict[str, RulesetConfig] = Field(default_factory=dict)
    rule_descriptions: dict[str, str] = Field(default_factory=dict)


class PlannerStepTemplate(BaseModel):
    name: str
    module: str
    description: str
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)


class StrategyTemplate(BaseModel):
    regeneration_scope: str
    strategy_reason: str
    affected_modules: list[str] = Field(default_factory=list)
    steps: list[PlannerStepTemplate] = Field(default_factory=list)
    explainability_notes: str | None = None


class PlannerConfigBundle(BaseModel):
    version: str
    strategies: dict[str, StrategyTemplate] = Field(default_factory=dict)


@lru_cache(maxsize=4)
def _load_rule_bundle(path_str: str) -> RuleConfigBundle:
    return RuleConfigBundle.model_validate_json(Path(path_str).read_text(encoding="utf-8"))


@lru_cache(maxsize=4)
def _load_planner_bundle(path_str: str) -> PlannerConfigBundle:
    return PlannerConfigBundle.model_validate_json(Path(path_str).read_text(encoding="utf-8"))


def _stable_hash(payload: Any) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class ConfigurableRuleEngine:
    def __init__(self, config_path: Path | None = None) -> None:
        self.config_path = config_path or _RULE_CONFIG_PATH

    @property
    def config(self) -> RuleConfigBundle:
        return _load_rule_bundle(str(self.config_path))

    def evaluate(self, intent: DesignIntent, parsed: ParsedDrawingModel) -> RuleCheckResult:
        issues: list[RuleIssue] = []
        applied_rules: list[str] = []
        shared = self.config.shared
        profile = self.config.rulesets.get(intent.constraints.ruleset) or self.config.rulesets.get(
            "cn_residential_v1" if intent.building_type == "residential" else "cn_office_v1"
        )
        if profile is None:
            raise KeyError(f"Unknown ruleset: {intent.constraints.ruleset}")

        rule_trace: list[dict[str, Any]] = []

        def _record_rule(rule_id: str, action: Callable[[], None]) -> None:
            start_index = len(issues)
            applied_rules.append(rule_id)
            action()
            new_issues = issues[start_index:]
            highest_severity = "passed"
            if new_issues:
                highest_issue = max(new_issues, key=lambda issue: _SEVERITY_RANK[issue.severity])
                highest_severity = highest_issue.severity
            rule_trace.append(
                {
                    "rule_id": rule_id,
                    "description": self.config.rule_descriptions.get(rule_id),
                    "issue_count": len(new_issues),
                    "highest_severity": highest_severity,
                    "issues": [issue.model_dump(mode="json") for issue in new_issues],
                }
            )

        def _assets_rule() -> None:
            if shared.cad_requires_assets and intent.source_mode == "cad_to_bim" and parsed.assets_count == 0:
                issues.append(
                    RuleIssue(
                        code="source.assets_missing",
                        severity="fatal",
                        message="图纸驱动模式缺少 CAD/PDF 图纸。",
                        target="assets",
                    )
                )

        def _pending_review_rule() -> None:
            for item in parsed.pending_review[:5]:
                severity = "error" if item.category in _FORMAL_BLOCKING_PENDING_REVIEW else item.severity
                issues.append(
                    RuleIssue(
                        code=f"drawing.review.{item.category}",
                        severity=severity,
                        message=item.reason,
                        target=item.source_ref or item.asset_name,
                    )
                )

        def _space_boundaries_rule() -> None:
            if intent.source_mode == "cad_to_bim" and parsed.space_candidates_detected == 0:
                issues.append(
                    RuleIssue(
                        code="drawing.space_boundaries_missing",
                        severity=shared.cad_missing_space_boundaries_severity,
                        message="图纸解析未识别到房间边界，空间生成结果需要人工复核。",
                        target="parsed_drawing.space_candidates_detected",
                    )
                )

        def _far_rule() -> None:
            if intent.constraints.far and intent.constraints.far > shared.max_far_warning:
                issues.append(
                    RuleIssue(
                        code="far.high",
                        severity="warning",
                        message="容积率偏高，建议复核场地边界和退线条件。",
                        target="constraints.far",
                    )
                )

        def _site_area_rule() -> None:
            if intent.site.area_sqm is None:
                issues.append(
                    RuleIssue(
                        code="site.area_missing",
                        severity=shared.site_area_missing_severity,
                        message="用地面积缺失，当前结果仅适合作为草案方案。",
                        target="site.area_sqm",
                    )
                )

        def _missing_fields_rule() -> None:
            if intent.missing_fields:
                issues.extend(
                    RuleIssue(
                        code=f"missing.{item.field}",
                        severity="warning",
                        message=item.reason,
                        target=item.field,
                    )
                    for item in intent.missing_fields
                )

        _record_rule("constraints.floors.min", lambda: self._apply_min_floor_rule(intent, issues, profile))
        _record_rule(
            "constraints.standard_floor_height.min",
            lambda: self._apply_floor_height_rule(intent, issues, profile),
        )
        _record_rule(
            "constraints.first_floor_height.min",
            lambda: self._apply_first_floor_height_rule(intent, issues, profile),
        )
        _record_rule("source.assets.required_for_cad", _assets_rule)
        _record_rule("drawing.pending_review.forward", _pending_review_rule)
        _record_rule("drawing.space_boundaries.required_for_cad", _space_boundaries_rule)
        _record_rule("constraints.far.warning_threshold", _far_rule)
        _record_rule("site.area.required_for_delivery", _site_area_rule)
        _record_rule("intent.missing_fields.forward", _missing_fields_rule)

        severity_order = {issue.severity for issue in issues}
        status = "passed"
        if {"fatal", "error"} & severity_order:
            status = "failed"
        elif "warning" in severity_order:
            status = "warning"

        severity_summary = {level: 0 for level in _SEVERITY_RANK}
        for issue in issues:
            severity_summary[issue.severity] += 1
        severity_summary["total"] = len(issues)

        total_height = intent.constraints.first_floor_height_m + (
            max(intent.constraints.floors - 1, 0) * intent.constraints.standard_floor_height_m
        )
        replay_token = _stable_hash(
            {
                "config_version": self.config.version,
                "ruleset": intent.constraints.ruleset,
                "intent": intent.model_dump(mode="json"),
                "parsed_summary": {
                    "assets_count": parsed.assets_count,
                    "space_boundaries_detected": parsed.space_boundaries_detected,
                    "space_candidates_detected": parsed.space_candidates_detected,
                    "pending_review": [item.model_dump(mode="json") for item in parsed.pending_review[:5]],
                },
            }
        )
        return RuleCheckResult(
            status=status,
            issues=issues,
            solver_constraints={
                "max_height_m": round(total_height, 2),
                "storey_count": intent.constraints.floors,
                "building_type": intent.building_type,
                "ruleset": intent.constraints.ruleset,
            },
            ruleset_version=self.config.version,
            applied_rules=sorted(set(applied_rules)),
            replay_token=replay_token,
            metadata={
                "config_version": self.config.version,
                "ruleset": intent.constraints.ruleset,
                "triggered_rule_codes": [issue.code for issue in issues],
                "issue_summary": severity_summary,
                "rule_trace": rule_trace,
                "rule_catalog": self.config.rule_descriptions,
                "evaluated_rules": [entry["rule_id"] for entry in rule_trace],
            },
        )

    def _apply_min_floor_rule(
        self,
        intent: DesignIntent,
        issues: list[RuleIssue],
        profile: RulesetConfig,
    ) -> None:
        if intent.constraints.floors < profile.min_floor_count:
            issues.append(
                RuleIssue(
                    code="floors.invalid",
                    severity="fatal",
                    message=f"楼层数必须大于等于 {profile.min_floor_count}。",
                    target="constraints.floors",
                )
            )

    def _apply_floor_height_rule(
        self,
        intent: DesignIntent,
        issues: list[RuleIssue],
        profile: RulesetConfig,
    ) -> None:
        if intent.constraints.standard_floor_height_m < profile.min_standard_floor_height_m:
            issues.append(
                RuleIssue(
                    code="floor_height.low",
                    severity="error",
                    message=f"标准层层高低于 {profile.min_standard_floor_height_m}m 的最低建议值。",
                    target="constraints.standard_floor_height_m",
                )
            )

    def _apply_first_floor_height_rule(
        self,
        intent: DesignIntent,
        issues: list[RuleIssue],
        profile: RulesetConfig,
    ) -> None:
        if intent.constraints.first_floor_height_m < profile.min_first_floor_height_m:
            issues.append(
                RuleIssue(
                    code="first_floor_height.low",
                    severity="warning",
                    message=f"首层层高低于 {profile.min_first_floor_height_m}m 的建议值。",
                    target="constraints.first_floor_height_m",
                )
            )


class ConfigurableModelingPlanner:
    def __init__(self, config_path: Path | None = None) -> None:
        self.config_path = config_path or _PLANNER_CONFIG_PATH

    @property
    def config(self) -> PlannerConfigBundle:
        return _load_planner_bundle(str(self.config_path))

    def plan(self, intent: DesignIntent, rule_check: RuleCheckResult) -> ModelingPlan:
        if intent.model_patch:
            strategy = "element_batch_replace"
        elif intent.source_mode == "cad_to_bim":
            strategy = "cad_to_bim"
        else:
            strategy = "text_to_massing"

        template = self.config.strategies[strategy]
        steps = [
            PlanStep(
                name=step.name,
                module=step.module,
                description=step.description,
                inputs=list(step.inputs),
                outputs=list(step.outputs),
            )
            for step in template.steps
        ]
        plan_payload = {
            "planner_version": self.config.version,
            "strategy": strategy,
            "request_id": intent.request_id,
            "version_id": intent.version_id,
            "rule_replay_token": rule_check.replay_token,
            "step_names": [step.name for step in steps],
        }
        plan_hash = _stable_hash(plan_payload)
        plan_id = f"plan_{plan_hash[:12]}"
        plan_metadata: dict[str, Any] = {
            "config_version": self.config.version,
            "selected_template": strategy,
            "rule_replay_token": rule_check.replay_token,
            "plan_payload": plan_payload,
            "plan_steps": [
                {
                    "name": step.name,
                    "module": step.module,
                    "description": step.description,
                    "inputs": list(step.inputs),
                    "outputs": list(step.outputs),
                }
                for step in steps
            ],
            "rule_context": {
                "status": rule_check.status,
                "issue_count": len(rule_check.issues),
                "applied_rule_count": len(rule_check.applied_rules),
            },
        }
        if template.explainability_notes:
            plan_metadata["explainability_notes"] = template.explainability_notes

        rule_metadata = rule_check.metadata or {}
        if "issue_summary" in rule_metadata:
            plan_metadata["rule_context"]["issue_summary"] = rule_metadata["issue_summary"]
        if "rule_trace" in rule_metadata:
            plan_metadata["rule_trace"] = rule_metadata["rule_trace"]
        if "rule_catalog" in rule_metadata:
            plan_metadata["rule_catalog"] = rule_metadata["rule_catalog"]
        if "evaluated_rules" in rule_metadata:
            plan_metadata["rule_context"]["evaluated_rules"] = rule_metadata["evaluated_rules"]

        return ModelingPlan(
            strategy=strategy,
            can_continue=rule_check.status != "failed",
            steps=steps,
            affected_modules=list(template.affected_modules),
            regeneration_scope=template.regeneration_scope,
            plan_id=plan_id,
            planner_version=self.config.version,
            replay_token=plan_hash,
            strategy_reason=template.strategy_reason,
            metadata=plan_metadata,
        )
