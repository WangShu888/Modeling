from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import importlib
import math

import json
import re
import uuid
from pathlib import Path

from .drawing_parser import DrawingParser as RealDrawingParser
from .ifc_runtime import detect_ifc_runtime
from .intent_service import StructuredIntentTransformer
from .models import (
    AssetInput,
    AssetRecord,
    Assumption,
    BimElement,
    BimSemanticModel,
    BimSpace,
    BimStorey,
    CompletionTraceItem,
    Constraints,
    DesignIntent,
    ElementSelector,
    ExportArtifact,
    ExportBundle,
    FeedbackCreateRequest,
    FeedbackReceipt,
    MissingField,
    ModelPatch,
    ModelingPlan,
    ModelingRequestCreate,
    ModelingRequestInput,
    ModelingRequestRecord,
    ParsedDrawingModel,
    PlanStep,
    Point2D,
    ProgramInfo,
    ProjectSummary,
    ProjectCreateRequest,
    RuleCheckResult,
    RuleIssue,
    SiteInfo,
    SourceBundle,
    StyleInfo,
    ValidationIssue,
    ValidationReport,
    VersionSnapshot,
)
from .planning import ConfigurableModelingPlanner, ConfigurableRuleEngine
from .storey_inference import infer_asset_view_role, infer_parsed_asset_storeys, storey_display_name, storey_sort_key
from .store import Store


_CHINESE_NUMERALS = {
    "一": "1",
    "二": "2",
    "三": "3",
    "四": "4",
    "五": "5",
    "六": "6",
    "七": "7",
    "八": "8",
    "九": "9",
    "十": "10",
    "两": "2",
}

_IFC_GUID_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_$"
_FORMAL_BLOCKING_PENDING_REVIEW = {"entity_detection_truncated", "multi_storey_asset_collapsed"}
_FORMAL_BLOCKING_RULE_CODES = {f"drawing.review.{item}" for item in _FORMAL_BLOCKING_PENDING_REVIEW}
_RECONCILIATION_TYPE_MAP = {
    "IfcWall": "wall",
    "IfcDoor": "door",
    "IfcWindow": "window",
    "IfcSpace": "space",
}
_SOURCE_ENTITY_CATEGORY_MAP = {
    "wall_line": "wall_line",
    "wall_path": "wall_path",
    "door_block": "door_block",
    "window_block": "window_block",
    "room_boundary": "room_boundary",
    "room_label": "room_label",
}


def _normalize_text(text: str) -> str:
    normalized = text
    for source, target in _CHINESE_NUMERALS.items():
        normalized = normalized.replace(source, target)
    return normalized


def _suffix(filename: str) -> str:
    return Path(filename).suffix.lower()


def _escape_ifc_text(value: str) -> str:
    return value.replace("'", "''")


def _format_ifc_float(value: float) -> str:
    rounded = round(value, 6)
    if abs(rounded) < 1e-9:
        rounded = 0.0
    text = f"{rounded:.6f}".rstrip("0").rstrip(".")
    if "." not in text:
        text += "."
    return text


def _compress_ifc_guid(value: str) -> str:
    try:
        source_uuid = uuid.UUID(str(value))
    except ValueError:
        source_uuid = uuid.uuid5(uuid.NAMESPACE_URL, value)
    number = source_uuid.int
    chars: list[str] = []
    for _ in range(22):
        chars.append(_IFC_GUID_ALPHABET[number % 64])
        number //= 64
    return "".join(reversed(chars))


class _IfcEntityBuffer:
    def __init__(self, start: int = 1) -> None:
        self._next_id = start
        self._lines: list[str] = []

    def add(self, expression: str) -> int:
        entity_id = self._next_id
        self._next_id += 1
        self._lines.append(f"#{entity_id}={expression};")
        return entity_id

    def render(self) -> list[str]:
        return list(self._lines)


def _extract_model_patch_from_prompt(prompt: str) -> tuple[ElementSelector | None, ModelPatch | None]:
    lower_prompt = prompt.lower()
    has_window_target = "窗" in prompt or "window" in lower_prompt
    has_replace_action = False
    for token in ("替换", "更换"):
        if token in prompt:
            has_replace_action = True
            break
    if "replace" in lower_prompt:
        has_replace_action = True

    if not has_window_target or not has_replace_action:
        return None, None

    match = re.search(r"(\d+)\s*[xX×]\s*(\d+)", prompt)
    width = int(match.group(1)) if match else 800
    height = int(match.group(2)) if match else 1200
    target_family = "floor_to_ceiling_window" if "落地窗" in prompt else "updated_window"
    selector = ElementSelector(
        ifc_type="IfcWindow",
        properties={
            "overall_width_mm": width,
            "overall_height_mm": height,
        },
    )
    patch = ModelPatch(
        action_type="replace_family",
        target_family=target_family,
        preserve=["storey", "host_wall", "axis_alignment", "material_style"],
    )
    return selector, patch


class SourceBundleBuilder:
    def __init__(self, store: Store) -> None:
        self.store = store

    def build(
        self,
        project: ProjectSummary,
        payload: ModelingRequestInput,
        *,
        request_id: str | None = None,
        version_id: str | None = None,
        assets: list[AssetRecord] | None = None,
    ) -> SourceBundle:
        request_id = request_id or self.store.next_id("req")
        version_id = version_id or self.store.next_id("ver")
        bundle_assets = assets if assets is not None else [self._asset_record(project.project_id, asset) for asset in payload.assets]
        form_fields = payload.model_dump(
            exclude={"prompt", "assets", "metadata"},
            exclude_none=True,
        )
        form_fields.update(payload.metadata)
        return SourceBundle(
            project_id=project.project_id,
            request_id=request_id,
            version_id=version_id,
            prompt=payload.prompt,
            source_mode_hint=payload.source_mode_hint,
            building_type_hint=payload.building_type or project.building_type,
            region=payload.region or project.region,
            assets=bundle_assets,
            form_fields=form_fields,
        )

    def _asset_record(self, project_id: str, asset: AssetInput) -> AssetRecord:
        return AssetRecord(
            asset_id=self.store.next_id("asset"),
            project_id=project_id,
            filename=asset.filename,
            media_type=asset.media_type,
            description=asset.description,
            path=asset.path,
            extension=_suffix(asset.filename),
        )


class LegacyDrawingParser:
    def parse(self, bundle: SourceBundle) -> ParsedDrawingModel:
        asset_kinds: list[str] = []
        recognized_layers: set[str] = set()
        text_annotations: list[str] = []
        storey_candidates: list[str] = []
        unresolved_entities: list[str] = []
        entity_summary = {
            "lines": 0,
            "polylines": 0,
            "blocks": 0,
            "texts": 0,
            "dimensions": 0,
        }

        for asset in bundle.assets:
            if asset.extension in {".dwg", ".dxf"}:
                asset_kinds.append("cad")
                recognized_layers.update({"A-WALL", "A-DOOR", "A-WIND", "A-AXIS"})
                entity_summary["lines"] += 48
                entity_summary["polylines"] += 18
                entity_summary["blocks"] += 12
                entity_summary["dimensions"] += 8
            elif asset.extension == ".pdf":
                asset_kinds.append("pdf")
                recognized_layers.update({"PDF-VECTOR", "PDF-TEXT"})
                entity_summary["lines"] += 18
                entity_summary["texts"] += 12
                text_annotations.append(f"{asset.filename}: extracted annotations")
            else:
                asset_kinds.append("unknown")
                unresolved_entities.append(f"{asset.filename}: unsupported asset kind")

            descriptor = f"{asset.filename} {asset.description or ''}".lower()
            if any(token in descriptor for token in ("平面", "plan", "floor")):
                storey_candidates.append("standard_floor")
            if any(token in descriptor for token in ("立面", "elevation")):
                storey_candidates.append("facade_reference")
            if any(token in descriptor for token in ("剖面", "section")):
                storey_candidates.append("section_reference")

        if not bundle.assets:
            unresolved_entities.append("No drawing assets were uploaded.")

        north_angle = float(bundle.form_fields.get("north_angle", 0.0))
        units = str(bundle.form_fields.get("unit", "mm"))
        return ParsedDrawingModel(
            assets_count=len(bundle.assets),
            asset_kinds=asset_kinds,
            units=units,
            recognized_layers=sorted(recognized_layers),
            grid_lines_detected=6 if "cad" in asset_kinds else 0,
            dimension_entities=entity_summary["dimensions"],
            text_annotations=text_annotations,
            storey_candidates=storey_candidates,
            north_angle=north_angle,
            unresolved_entities=unresolved_entities,
            entity_summary=entity_summary,
        )


class IntentTransformer:
    def transform(self, bundle: SourceBundle, parsed: ParsedDrawingModel) -> DesignIntent:
        prompt = _normalize_text(bundle.prompt)
        assumptions: list[Assumption] = []
        completion_trace: list[CompletionTraceItem] = []
        missing_fields: list[MissingField] = []

        building_type = self._infer_building_type(prompt, bundle.building_type_hint)
        floors = self._resolve_int(
            value=bundle.form_fields.get("floors"),
            inferred=self._extract_int(prompt, r"(\d+)\s*层"),
            default=1,
            field="constraints.floors",
            source="building_template_default",
            assumptions=assumptions,
            completion_trace=completion_trace,
        )
        standard_floor_height = self._resolve_float(
            value=bundle.form_fields.get("standard_floor_height_m"),
            inferred=self._extract_float(prompt, r"(?:标准层层高|层高)[:：]?\s*([0-9.]+)"),
            default=3.0 if building_type == "residential" else 3.6,
            field="constraints.standard_floor_height_m",
            source=f"{building_type}_template_default",
            assumptions=assumptions,
            completion_trace=completion_trace,
        )
        first_floor_height = self._resolve_float(
            value=bundle.form_fields.get("first_floor_height_m"),
            inferred=self._extract_float(prompt, r"首层层高[:：]?\s*([0-9.]+)"),
            default=max(standard_floor_height, 4.2 if building_type == "office" else 3.3),
            field="constraints.first_floor_height_m",
            source=f"{building_type}_template_default",
            assumptions=assumptions,
            completion_trace=completion_trace,
        )
        site_area = self._resolve_optional_float(
            value=bundle.form_fields.get("site_area_sqm"),
            inferred=self._extract_float(prompt, r"([0-9.]+)\s*(?:平方米|平方|㎡|m2)"),
            field="site.area_sqm",
            completion_trace=completion_trace,
        )
        if site_area is None:
            inferred_site_area = self._infer_site_area_from_parsed(parsed)
            if inferred_site_area is not None:
                site_area = inferred_site_area
                completion_trace.append(
                    CompletionTraceItem(
                        field="site.area_sqm",
                        value=site_area,
                        source="parsed_drawing_inferred_boundary",
                        source_type="parsed_drawing",
                    )
                )
        far = self._resolve_optional_float(
            value=bundle.form_fields.get("far"),
            inferred=self._extract_float(prompt, r"容积率[:：]?\s*([0-9.]+)"),
            field="constraints.far",
            completion_trace=completion_trace,
        )
        units_per_floor = self._resolve_optional_int(
            value=bundle.form_fields.get("units_per_floor"),
            inferred=self._extract_int(prompt, r"(\d+)\s*户"),
            field="program.units_per_floor",
            completion_trace=completion_trace,
        )
        if units_per_floor is None and building_type == "residential":
            units_per_floor = 4
            assumptions.append(
                Assumption(
                    field="program.units_per_floor",
                    value=units_per_floor,
                    source="residential_template_default",
                    confidence=0.6,
                )
            )

        region = bundle.region
        if not region:
            missing_fields.append(
                MissingField(
                    field="region",
                    reason="地区规则集未指定，将使用默认国标规则集。",
                    critical=False,
                )
            )
            completion_trace.append(
                CompletionTraceItem(
                    field="region",
                    value="cn_default",
                    source="system_default",
                )
            )

        ruleset = "cn_residential_v1" if building_type == "residential" else "cn_office_v1"
        completion_trace.append(
            CompletionTraceItem(
                field="constraints.ruleset",
                value=ruleset,
                source="building_type_ruleset",
            )
        )

        source_mode = self._infer_source_mode(bundle, parsed)
        if source_mode == "text_only" and site_area is None:
            missing_fields.append(
                MissingField(
                    field="site.area_sqm",
                    reason="文本建模未提供用地面积，将生成草案级方案。",
                    critical=False,
                )
            )

        selector, patch = _extract_model_patch_from_prompt(prompt)
        style = StyleInfo(
            facade="follow_drawing" if source_mode == "cad_to_bim" else "generated_default",
            material_palette=["concrete", "glass", "aluminum"]
            if building_type == "office"
            else ["paint", "glass", "metal"],
        )
        program = ProgramInfo(
            spaces_from_drawings=parsed.space_candidates_detected > 0 or source_mode == "cad_to_bim",
            units_per_floor=units_per_floor,
            core_type="double_elevator" if building_type == "residential" else "office_core",
            first_floor_spaces=["lobby", "support"] if building_type == "office" else ["lobby"],
            typical_floor_spaces=["unit", "corridor"]
            if building_type == "residential"
            else ["open_office", "meeting_room", "core"],
        )
        return DesignIntent(
            project_id=bundle.project_id,
            request_id=bundle.request_id,
            version_id=bundle.version_id,
            source_mode=source_mode,
            building_type=building_type,
            site=SiteInfo(
                boundary_source=(
                    "drawing_or_uploaded_polygon"
                    if parsed.site_boundary_detected or parsed.assets_count
                    else "unspecified"
                ),
                area_sqm=site_area,
                north_angle=parsed.north_angle,
            ),
            constraints=Constraints(
                floors=floors,
                standard_floor_height_m=standard_floor_height,
                first_floor_height_m=first_floor_height,
                ruleset=ruleset,
                far=far,
            ),
            program=program,
            style=style,
            deliverables=bundle.form_fields.get("output_formats", ["ifc"]),
            missing_fields=missing_fields,
            assumptions=assumptions,
            completion_trace=completion_trace,
            element_selector=selector,
            model_patch=patch,
        )

    def _infer_building_type(self, prompt: str, fallback: str | None) -> str:
        if fallback:
            return fallback
        lowered = prompt.lower()
        if any(token in lowered for token in ("住宅", "宿舍", "公寓", "residential")):
            return "residential"
        if any(token in lowered for token in ("办公", "office")):
            return "office"
        return "residential"

    def _infer_source_mode(self, bundle: SourceBundle, parsed: ParsedDrawingModel) -> str:
        if bundle.source_mode_hint != "auto":
            return "cad_to_bim" if bundle.source_mode_hint == "cad_to_bim" else "text_only"
        return "cad_to_bim" if parsed.assets_count else "text_only"

    def _extract_int(self, text: str, pattern: str) -> int | None:
        match = re.search(pattern, text)
        return int(match.group(1)) if match else None

    def _extract_float(self, text: str, pattern: str) -> float | None:
        match = re.search(pattern, text)
        return float(match.group(1)) if match else None

    def _infer_site_area_from_parsed(self, parsed: ParsedDrawingModel) -> float | None:
        scale = self._parsed_unit_scale(parsed.units)
        best_area_sqm: float | None = None
        for entity in parsed.detected_entities:
            if entity.category not in {"site_boundary", "site_boundary_candidate"}:
                continue
            area = self._entity_area_sqm(entity, scale)
            if area is None or area <= 0:
                continue
            if best_area_sqm is None or area > best_area_sqm:
                best_area_sqm = area
        return round(best_area_sqm, 3) if best_area_sqm is not None else None

    def _entity_area_sqm(self, entity: DrawingEntityRecord, scale: float) -> float | None:
        if len(entity.points) >= 3:
            return abs(self._polygon_area(entity.points)) * (scale**2)
        bbox = entity.bbox
        if bbox is None:
            return None
        width = max((bbox.max_x - bbox.min_x) * scale, 0.0)
        depth = max((bbox.max_y - bbox.min_y) * scale, 0.0)
        return width * depth if width > 0 and depth > 0 else None

    def _polygon_area(self, points: list[Point2D]) -> float:
        if len(points) < 3:
            return 0.0
        area = 0.0
        for index, current in enumerate(points):
            nxt = points[(index + 1) % len(points)]
            area += current.x * nxt.y - nxt.x * current.y
        return area / 2.0

    def _parsed_unit_scale(self, units: str) -> float:
        normalized = (units or "").lower()
        if normalized in {"mm", "millimeter", "millimeters", "4"}:
            return 0.001
        if normalized in {"cm", "centimeter", "centimeters", "5"}:
            return 0.01
        return 1.0

    def _resolve_int(
        self,
        value: int | None,
        inferred: int | None,
        default: int,
        field: str,
        source: str,
        assumptions: list[Assumption],
        completion_trace: list[CompletionTraceItem],
    ) -> int:
        if value is not None:
            completion_trace.append(CompletionTraceItem(field=field, value=value, source="form_input"))
            return int(value)
        if inferred is not None:
            completion_trace.append(CompletionTraceItem(field=field, value=inferred, source="text_prompt"))
            return int(inferred)
        assumptions.append(Assumption(field=field, value=default, source=source))
        completion_trace.append(CompletionTraceItem(field=field, value=default, source=source))
        return default

    def _resolve_float(
        self,
        value: float | None,
        inferred: float | None,
        default: float,
        field: str,
        source: str,
        assumptions: list[Assumption],
        completion_trace: list[CompletionTraceItem],
    ) -> float:
        if value is not None:
            completion_trace.append(CompletionTraceItem(field=field, value=value, source="form_input"))
            return float(value)
        if inferred is not None:
            completion_trace.append(CompletionTraceItem(field=field, value=inferred, source="text_prompt"))
            return float(inferred)
        assumptions.append(Assumption(field=field, value=default, source=source))
        completion_trace.append(CompletionTraceItem(field=field, value=default, source=source))
        return default

    def _resolve_optional_float(
        self,
        value: float | None,
        inferred: float | None,
        field: str,
        completion_trace: list[CompletionTraceItem],
    ) -> float | None:
        if value is not None:
            completion_trace.append(CompletionTraceItem(field=field, value=value, source="form_input"))
            return float(value)
        if inferred is not None:
            completion_trace.append(CompletionTraceItem(field=field, value=inferred, source="text_prompt"))
            return float(inferred)
        return None

    def _resolve_optional_int(
        self,
        value: int | None,
        inferred: int | None,
        field: str,
        completion_trace: list[CompletionTraceItem],
    ) -> int | None:
        if value is not None:
            completion_trace.append(CompletionTraceItem(field=field, value=value, source="form_input"))
            return int(value)
        if inferred is not None:
            completion_trace.append(CompletionTraceItem(field=field, value=inferred, source="text_prompt"))
            return int(inferred)
        return None

    def _extract_model_patch(self, prompt: str) -> tuple[ElementSelector | None, ModelPatch | None]:
        return _extract_model_patch_from_prompt(prompt)


class RuleEngine:
    def evaluate(self, intent: DesignIntent, parsed: ParsedDrawingModel) -> RuleCheckResult:
        issues: list[RuleIssue] = []
        if intent.constraints.floors <= 0:
            issues.append(
                RuleIssue(
                    code="floors.invalid",
                    severity="fatal",
                    message="楼层数必须大于 0。",
                    target="constraints.floors",
                )
            )

        min_floor_height = 2.8 if intent.building_type == "residential" else 3.0
        if intent.constraints.standard_floor_height_m < min_floor_height:
            issues.append(
                RuleIssue(
                    code="floor_height.low",
                    severity="error",
                    message=f"标准层层高低于 {min_floor_height}m 的最低建议值。",
                    target="constraints.standard_floor_height_m",
                )
            )

        if intent.source_mode == "cad_to_bim" and parsed.assets_count == 0:
            issues.append(
                RuleIssue(
                    code="source.assets_missing",
                    severity="fatal",
                    message="图纸驱动模式缺少 CAD/PDF 图纸。",
                    target="assets",
                )
            )

        for item in parsed.pending_review[:5]:
            issues.append(
                RuleIssue(
                    code=f"drawing.review.{item.category}",
                    severity=item.severity,
                    message=item.reason,
                    target=item.source_ref or item.asset_name,
                )
            )

        if intent.source_mode == "cad_to_bim" and parsed.space_candidates_detected == 0:
            issues.append(
                RuleIssue(
                    code="drawing.space_boundaries_missing",
                    severity="warning",
                    message="图纸解析未识别到房间边界，空间生成结果需要人工复核。",
                    target="parsed_drawing.space_boundaries_detected",
                )
            )

        if intent.constraints.far and intent.constraints.far > 5.0:
            issues.append(
                RuleIssue(
                    code="far.high",
                    severity="warning",
                    message="容积率偏高，建议复核场地边界和退线条件。",
                    target="constraints.far",
                )
            )

        if intent.site.area_sqm is None:
            issues.append(
                RuleIssue(
                    code="site.area_missing",
                    severity="warning",
                    message="用地面积缺失，当前结果仅适合作为草案方案。",
                    target="site.area_sqm",
                )
            )

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

        severity_order = {issue.severity for issue in issues}
        status = "passed"
        if {"fatal", "error"} & severity_order:
            status = "failed"
        elif "warning" in severity_order:
            status = "warning"

        total_height = intent.constraints.first_floor_height_m + (
            max(intent.constraints.floors - 1, 0) * intent.constraints.standard_floor_height_m
        )
        return RuleCheckResult(
            status=status,
            issues=issues,
            solver_constraints={
                "max_height_m": round(total_height, 2),
                "storey_count": intent.constraints.floors,
                "building_type": intent.building_type,
            },
        )


class ModelingPlanner:
    def plan(self, intent: DesignIntent, rule_check: RuleCheckResult) -> ModelingPlan:
        if intent.model_patch:
            strategy = "element_batch_replace"
            regeneration_scope = "matched_elements"
        elif intent.source_mode == "cad_to_bim":
            strategy = "cad_to_bim"
            regeneration_scope = "building"
        else:
            strategy = "text_to_massing"
            regeneration_scope = "building"

        steps = [
            PlanStep(
                name="normalize_inputs",
                module="intake",
                description="归档文本、图纸和表单输入，生成 SourceBundle。",
                inputs=["ModelingRequestInput"],
                outputs=["SourceBundle"],
            ),
            PlanStep(
                name="parse_drawings",
                module="drawing_parser",
                description="解析 CAD/PDF 图元并输出统一图纸中间模型。",
                inputs=["SourceBundle"],
                outputs=["ParsedDrawingModel"],
            ),
            PlanStep(
                name="transform_intent",
                module="ai_intent",
                description="将文本和图纸信息转为结构化 DesignIntent。",
                inputs=["SourceBundle", "ParsedDrawingModel"],
                outputs=["DesignIntent"],
            ),
            PlanStep(
                name="evaluate_rules",
                module="rule_engine",
                description="执行硬约束和软约束检查。",
                inputs=["DesignIntent", "ParsedDrawingModel"],
                outputs=["RuleCheckResult"],
            ),
            PlanStep(
                name="build_bim",
                module="bim_engine",
                description="按执行计划创建语义化 BIM 模型。",
                inputs=["DesignIntent", "ParsedDrawingModel", "RuleCheckResult"],
                outputs=["BimSemanticModel"],
            ),
            PlanStep(
                name="validate_and_export",
                module="validation_export",
                description="执行自检并导出 IFC/JSON 产物。",
                inputs=["BimSemanticModel", "DesignIntent", "RuleCheckResult"],
                outputs=["ValidationReport", "ExportBundle"],
            ),
        ]
        return ModelingPlan(
            strategy=strategy,
            can_continue=rule_check.status != "failed",
            steps=steps,
            affected_modules=[
                "drawing_parser",
                "ai_intent",
                "rule_engine",
                "bim_engine",
                "validation_export",
            ],
            regeneration_scope=regeneration_scope,
        )


class BimEngine:
    def build(
        self,
        intent: DesignIntent,
        plan: ModelingPlan,
        parsed_drawing: ParsedDrawingModel | None = None,
    ) -> BimSemanticModel:
        storeys: list[BimStorey] = []
        replacement_count = 0
        source_layouts = self._source_layouts(parsed_drawing)
        source_space_evidence = sum(len(layout["room_entities"]) for layout in source_layouts) if source_layouts else 0
        target_storey_count = max(len(source_layouts), intent.constraints.floors) if source_layouts else intent.constraints.floors
        source_layout_trace: list[dict[str, object]] = []

        for floor_index in range(target_storey_count):
            storey_id = f"storey_{floor_index + 1:02d}"
            source_layout = self._layout_for_floor(source_layouts, floor_index) if source_layouts else None
            storey_key = str(source_layout["storey_key"]) if source_layout is not None else f"{floor_index + 1}F"
            storey_name = str(source_layout["storey_name"]) if source_layout is not None else f"{floor_index + 1}F"
            source_layout_trace.append(
                {
                    "storey_key": storey_key,
                    "source_storey_key": str(source_layout.get("source_storey_key", storey_key)) if source_layout is not None else None,
                    "source_fragment_id": str(source_layout.get("source_fragment_id", "")) if source_layout is not None else "",
                    "source_fragment_ids": list(source_layout.get("source_fragment_ids", [])) if source_layout is not None else [],
                    "source_assets": list(source_layout.get("asset_names", [])) if source_layout is not None else [],
                    "replicated": bool(source_layout.get("replicated")) if source_layout is not None else False,
                }
            )
            elevation = self._storey_elevation(intent, floor_index, storey_key)
            if source_layout is not None:
                spaces = self._make_spaces_from_source(storey_id, source_layout)
                if not spaces and source_space_evidence == 0:
                    spaces = self._make_spaces(intent, storey_id, floor_index)
                elements = self._make_elements_from_source(intent, storey_id, source_layout)
            else:
                spaces = self._make_spaces(intent, storey_id, floor_index)
                elements = self._make_elements(intent, storey_id, floor_index)

            if intent.element_selector and intent.model_patch:
                for element in elements:
                    if element.ifc_type != intent.element_selector.ifc_type:
                        continue
                    width = element.properties.get("overall_width_mm")
                    height = element.properties.get("overall_height_mm")
                    selector_width = intent.element_selector.properties.get("overall_width_mm")
                    selector_height = intent.element_selector.properties.get("overall_height_mm")
                    if width == selector_width and height == selector_height:
                        element.family_name = intent.model_patch.target_family
                        replacement_count += 1

            storeys.append(
                BimStorey(
                    storey_id=storey_id,
                    name=storey_name,
                    elevation_m=round(elevation, 2),
                    spaces=spaces,
                    elements=elements,
                )
            )

        element_index: dict[str, int] = {}
        for storey in storeys:
            for element in storey.elements:
                element_index[element.ifc_type] = element_index.get(element.ifc_type, 0) + 1

        source_entity_totals = {
            "wall": sum(len(layout["wall_entities"]) for layout in source_layouts) if source_layouts else 0,
            "window": sum(len(layout["window_entities"]) for layout in source_layouts) if source_layouts else 0,
            "door": sum(len(layout["door_entities"]) for layout in source_layouts) if source_layouts else 0,
            "space": sum(len(layout["room_entities"]) for layout in source_layouts) if source_layouts else 0,
        }
        modeled_entity_totals = {
            "IfcWall": element_index.get("IfcWall", 0),
            "IfcDoor": element_index.get("IfcDoor", 0),
            "IfcWindow": element_index.get("IfcWindow", 0),
            "IfcSpace": sum(len(storey.spaces) for storey in storeys),
        }
        source_layout_bounds_by_storey = {
            str(layout["storey_key"]): {
                "min_x_m": float(layout.get("bounds_min_x_m", 0.0)),
                "min_y_m": float(layout.get("bounds_min_y_m", 0.0)),
                "max_x_m": float(layout.get("bounds_max_x_m", float(layout.get("footprint_width", 0.0)))),
                "max_y_m": float(layout.get("bounds_max_y_m", float(layout.get("footprint_depth", 0.0)))),
            }
            for layout in source_layouts
        }
        source_fragment_ids: list[str] = []
        for layout in source_layouts:
            for fragment_id in layout.get("source_fragment_ids", []):
                normalized = str(fragment_id)
                if normalized and normalized not in source_fragment_ids:
                    source_fragment_ids.append(normalized)
        count_reconciliation = {
            "IfcWall": {
                "source": source_entity_totals["wall"],
                "modeled": modeled_entity_totals["IfcWall"],
                "delta": modeled_entity_totals["IfcWall"] - source_entity_totals["wall"],
            },
            "IfcDoor": {
                "source": source_entity_totals["door"],
                "modeled": modeled_entity_totals["IfcDoor"],
                "delta": modeled_entity_totals["IfcDoor"] - source_entity_totals["door"],
            },
            "IfcWindow": {
                "source": source_entity_totals["window"],
                "modeled": modeled_entity_totals["IfcWindow"],
                "delta": modeled_entity_totals["IfcWindow"] - source_entity_totals["window"],
            },
            "IfcSpace": {
                "source": source_entity_totals["space"],
                "modeled": modeled_entity_totals["IfcSpace"],
                "delta": modeled_entity_totals["IfcSpace"] - source_entity_totals["space"],
            },
        }

        return BimSemanticModel(
            project_id=intent.project_id,
            version_id=intent.version_id,
            building_type=intent.building_type,
            storeys=storeys,
            element_index=element_index,
            metadata={
                "strategy": plan.strategy,
                "replacement_count": replacement_count,
                "units_per_floor": intent.program.units_per_floor,
                "geometry_source": "parsed_drawing" if source_layouts else "template_fallback",
                "source_wall_entities": source_entity_totals["wall"],
                "source_window_entities": source_entity_totals["window"],
                "source_door_entities": source_entity_totals["door"],
                "source_storey_keys": [str(layout["storey_key"]) for layout in source_layouts] if source_layouts else [],
                "source_fragment_ids": source_fragment_ids,
                "storey_layout_trace": source_layout_trace,
                "source_layout_bounds_by_storey": source_layout_bounds_by_storey,
                "source_entity_totals": source_entity_totals,
                "modeled_entity_totals": modeled_entity_totals,
                "count_reconciliation": count_reconciliation,
            },
        )

    def _layout_for_floor(
        self,
        source_layouts: list[dict[str, object]],
        floor_index: int,
    ) -> dict[str, object] | None:
        if not source_layouts:
            return None
        if floor_index < len(source_layouts):
            layout = dict(source_layouts[floor_index])
            layout.setdefault("source_storey_key", layout["storey_key"])
            layout.setdefault("source_fragment_id", f"{layout['source_storey_key']}:fragment")
            layout.setdefault("source_fragment_ids", [layout["source_fragment_id"]])
            layout["replicated"] = False
            return layout

        template = dict(source_layouts[-1])
        storey_key = f"{floor_index + 1}F"
        template["source_storey_key"] = template["storey_key"]
        template["source_fragment_id"] = template.get("source_fragment_id") or f"{template['source_storey_key']}:fragment"
        template["source_fragment_ids"] = list(template.get("source_fragment_ids", [template["source_fragment_id"]]))
        template["storey_key"] = storey_key
        template["storey_name"] = storey_display_name(storey_key)
        template["replicated"] = True
        return template

    def _source_layouts(self, parsed_drawing: ParsedDrawingModel | None) -> list[dict[str, object]]:
        if parsed_drawing is None:
            return []

        candidate_names_by_asset: dict[str, list[str]] = {}
        for candidate in parsed_drawing.storey_candidate_details:
            candidate_names_by_asset.setdefault(candidate.asset_name, []).append(candidate.name)
        asset_storeys = infer_parsed_asset_storeys(parsed_drawing)
        scale = self._unit_scale(parsed_drawing.units)

        grouped_entities: dict[str, dict[str, list[object]]] = {}
        for entity in parsed_drawing.detected_entities:
            canonical_category = self._canonical_source_entity_category(entity)
            if canonical_category is None:
                continue
            if canonical_category in {"door_block", "window_block"} and not entity.points:
                continue
            if canonical_category in {"wall_line", "wall_path"} and not (entity.points or entity.bbox is not None):
                continue
            if canonical_category == "room_label" and entity.bbox is None and not entity.label:
                continue

            asset_storey_key = asset_storeys.get(entity.asset_name, "1F")
            role = self._entity_role(entity, infer_asset_view_role(entity.asset_name, candidate_names_by_asset.get(entity.asset_name, [])))
            if role in {"section", "facade"}:
                continue
            storey_key = self._entity_storey_key(entity, asset_storey_key)
            bucket = grouped_entities.setdefault(
                storey_key,
                {
                    "wall_entities": [],
                    "window_entities": [],
                    "door_entities": [],
                    "room_entities": [],
                    "asset_names": [],
                    "fragment_ids": [],
                    "source_storey_keys": [],
                },
            )
            if entity.asset_name not in bucket["asset_names"]:
                bucket["asset_names"].append(entity.asset_name)
            entity_storey_key = self._entity_storey_key(entity, asset_storey_key)
            entity_fragment_id = self._entity_fragment_id(entity, f"{entity.asset_name}:{entity_storey_key}")
            if entity_fragment_id not in bucket["fragment_ids"]:
                bucket["fragment_ids"].append(entity_fragment_id)
            if entity_storey_key not in bucket["source_storey_keys"]:
                bucket["source_storey_keys"].append(entity_storey_key)
            if canonical_category in {"wall_line", "wall_path"}:
                bucket["wall_entities"].append(entity)
            elif canonical_category == "window_block":
                bucket["window_entities"].append(entity)
            elif canonical_category == "door_block":
                bucket["door_entities"].append(entity)
            elif canonical_category in {"room_boundary", "room_label"}:
                bucket["room_entities"].append(entity)

        layouts: list[dict[str, object]] = []
        for storey_key in sorted(grouped_entities, key=storey_sort_key):
            grouped = grouped_entities[storey_key]
            wall_entities = list(grouped["wall_entities"])
            if not wall_entities:
                continue
            window_entities = list(grouped["window_entities"])
            door_entities = list(grouped["door_entities"])
            room_entities = list(grouped["room_entities"])
            source_fragment_ids = [str(fragment_id) for fragment_id in grouped["fragment_ids"]]
            source_storey_keys = [str(source_key) for source_key in grouped["source_storey_keys"]]
            source_storey_key = source_storey_keys[0] if source_storey_keys else storey_key
            source_fragment_id = (
                source_fragment_ids[0]
                if source_fragment_ids
                else f"{next(iter(grouped['asset_names']), storey_key)}:{source_storey_key}"
            )
            min_x, min_y, max_x, max_y = self._entity_bounds(
                wall_entities + window_entities + door_entities + room_entities
            )
            if min_x is None or min_y is None or max_x is None or max_y is None:
                continue
            width = max((max_x - min_x) * scale, 1.0)
            depth = max((max_y - min_y) * scale, 1.0)
            layouts.append(
                {
                    "storey_key": storey_key,
                    "source_storey_key": source_storey_key,
                    "storey_name": storey_display_name(storey_key),
                    "scale": scale,
                    "origin_x": min_x,
                    "origin_y": min_y,
                    "footprint_width": width,
                    "footprint_depth": depth,
                    "bounds_min_x_m": 0.0,
                    "bounds_min_y_m": 0.0,
                    "bounds_max_x_m": round(width, 3),
                    "bounds_max_y_m": round(depth, 3),
                    "wall_entities": wall_entities,
                    "window_entities": window_entities,
                    "door_entities": door_entities,
                    "room_entities": room_entities,
                    "asset_names": list(grouped["asset_names"]),
                    "source_fragment_id": source_fragment_id,
                    "source_fragment_ids": source_fragment_ids,
                }
            )
        return layouts

    def _canonical_source_entity_category(self, entity: DrawingEntityRecord) -> str | None:
        category = str(entity.category or "").strip().lower()
        if not category:
            return None
        direct = _SOURCE_ENTITY_CATEGORY_MAP.get(category)
        if direct is not None:
            return direct

        if "wall" in category:
            return "wall_line" if "line" in category else "wall_path"
        if "door" in category:
            return "door_block"
        if "window" in category or "wind" in category:
            return "window_block"
        if "room" in category or "space" in category:
            if "label" in category or "text" in category:
                return "room_label"
            return "room_boundary"

        metadata = entity.metadata if isinstance(entity.metadata, dict) else {}
        semantic_role = str(metadata.get("semantic_role", "")).strip().lower()
        if semantic_role == "wall":
            return "wall_path"
        if semantic_role == "door":
            return "door_block"
        if semantic_role == "window":
            return "window_block"
        if semantic_role in {"room", "space"}:
            return "room_boundary"
        return None

    def _storey_elevation(self, intent: DesignIntent, floor_index: int, storey_key: str) -> float:
        normalized = storey_key.upper()
        if normalized == "RF":
            return intent.constraints.first_floor_height_m + max(intent.constraints.floors - 1, 0) * intent.constraints.standard_floor_height_m
        basement_match = re.fullmatch(r"B(\d+)", normalized)
        if basement_match:
            return -float(basement_match.group(1)) * intent.constraints.standard_floor_height_m
        floor_match = re.fullmatch(r"(\d+)F", normalized)
        if floor_match:
            floor_number = int(floor_match.group(1))
            if floor_number <= 1:
                return 0.0
            return intent.constraints.first_floor_height_m + (floor_number - 2) * intent.constraints.standard_floor_height_m
        if floor_index == 0:
            return 0.0
        return intent.constraints.first_floor_height_m + (floor_index - 1) * intent.constraints.standard_floor_height_m

    def _make_spaces(self, intent: DesignIntent, storey_id: str, floor_index: int) -> list[BimSpace]:
        spaces: list[BimSpace] = []
        if intent.building_type == "residential":
            units = intent.program.units_per_floor or 4
            for unit_index in range(units):
                spaces.append(
                    BimSpace(
                        space_id=f"{storey_id}_unit_{unit_index + 1}",
                        name=f"户型 {unit_index + 1}",
                        category="apartment",
                        area_sqm=95.0,
                    )
                )
            spaces.append(
                BimSpace(
                    space_id=f"{storey_id}_corridor",
                    name="公共走道",
                    category="circulation",
                    area_sqm=42.0,
                )
            )
            return spaces

        default_spaces = [
            ("open_office", "开放办公区", 240.0),
            ("meeting", "会议室", 48.0),
            ("core", "交通核", 60.0),
        ]
        for suffix, name, area in default_spaces:
            spaces.append(
                BimSpace(
                    space_id=f"{storey_id}_{suffix}",
                    name=name,
                    category=suffix,
                    area_sqm=area,
                )
            )
        return spaces

    def _make_spaces_from_source(self, storey_id: str, layout: dict[str, object]) -> list[BimSpace]:
        room_entities = list(layout["room_entities"])
        if not room_entities:
            return []

        scale = float(layout["scale"])
        spaces: list[BimSpace] = []
        for index, entity in enumerate(room_entities, start=1):
            if entity.category == "room_boundary":
                area = self._polygon_area(entity.points) * (scale**2)
                name = entity.label or f"图纸空间 {index}"
            else:
                area = self._entity_bbox_area(entity, scale)
                name = entity.label or f"房间标签 {index}"
            spaces.append(
                BimSpace(
                    space_id=f"{storey_id}_room_{index:03d}",
                    name=name,
                    category="parsed_room",
                    area_sqm=max(round(area, 3), 1.0),
                )
            )
        return spaces

    def _make_elements(self, intent: DesignIntent, storey_id: str, floor_index: int) -> list[BimElement]:
        elements: list[BimElement] = []
        for index in range(8):
            elements.append(
                self._element(
                    intent,
                    storey_id,
                    "IfcWall",
                    f"墙体 {index + 1}",
                    "generic_wall",
                    {"length_m": 6 + (index % 3), "height_m": intent.constraints.standard_floor_height_m},
                )
            )
        elements.append(
            self._element(
                intent,
                storey_id,
                "IfcSlab",
                f"楼板 {floor_index + 1}",
                "generic_slab",
                {"thickness_mm": 120},
            )
        )
        units = intent.program.units_per_floor or 4
        for index in range(units):
            elements.append(
                self._element(
                    intent,
                    storey_id,
                    "IfcDoor",
                    f"户门 {index + 1}",
                    "unit_entry_door",
                    {"width_mm": 1000, "height_mm": 2100},
                )
            )
            elements.append(
                self._element(
                    intent,
                    storey_id,
                    "IfcWindow",
                    f"窗 {index + 1}-A",
                    "standard_window",
                    {"overall_width_mm": 800, "overall_height_mm": 1200},
                )
            )
            elements.append(
                self._element(
                    intent,
                    storey_id,
                    "IfcWindow",
                    f"窗 {index + 1}-B",
                    "secondary_window",
                    {"overall_width_mm": 1200, "overall_height_mm": 1500},
                )
            )
        return elements

    def _make_elements_from_source(
        self,
        intent: DesignIntent,
        storey_id: str,
        layout: dict[str, object],
    ) -> list[BimElement]:
        scale = float(layout["scale"])
        origin_x = float(layout["origin_x"])
        origin_y = float(layout["origin_y"])
        footprint_width = float(layout["footprint_width"])
        footprint_depth = float(layout["footprint_depth"])
        wall_entities = list(layout["wall_entities"])
        window_entities = list(layout["window_entities"])
        door_entities = list(layout["door_entities"])
        source_storey_key = str(layout.get("source_storey_key", layout["storey_key"]))
        source_fragment_id = str(layout.get("source_fragment_id", f"{source_storey_key}:fragment"))

        elements: list[BimElement] = []
        storey_height = max(intent.constraints.standard_floor_height_m, 2.8)
        slab_properties = self._slab_properties(
            wall_entities=wall_entities,
            scale=scale,
            origin_x=origin_x,
            origin_y=origin_y,
            fallback_width=footprint_width,
            fallback_depth=footprint_depth,
            source_storey_key=source_storey_key,
            source_fragment_id=source_fragment_id,
        )

        elements.append(
            self._element(
                intent,
                storey_id,
                "IfcSlab",
                f"图纸楼板 {storey_id[-2:]}",
                "cad_footprint_slab",
                slab_properties,
            )
        )

        for index, entity in enumerate(wall_entities, start=1):
            properties = self._wall_properties(
                entity,
                scale,
                origin_x,
                origin_y,
                storey_height,
                source_storey_key,
                source_fragment_id,
            )
            elements.append(
                self._element(
                    intent,
                    storey_id,
                    "IfcWall",
                    entity.label or f"图纸墙体 {index}",
                    f"cad_{entity.category}",
                    properties,
                )
            )

        for index, entity in enumerate(door_entities, start=1):
            elements.append(
                self._element(
                    intent,
                    storey_id,
                    "IfcDoor",
                    entity.label or f"图纸门 {index}",
                    "cad_door_block",
                    self._opening_properties(
                        entity,
                        wall_entities=wall_entities,
                        scale=scale,
                        origin_x=origin_x,
                        origin_y=origin_y,
                        source_storey_key=source_storey_key,
                        default_fragment_id=source_fragment_id,
                        default_width=1.0,
                        default_depth=0.15,
                        default_height=2.1,
                        default_z=0.0,
                        width_key="width_mm",
                        height_key="height_mm",
                    ),
                )
            )

        for index, entity in enumerate(window_entities, start=1):
            elements.append(
                self._element(
                    intent,
                    storey_id,
                    "IfcWindow",
                    entity.label or f"图纸窗 {index}",
                    "cad_window_block",
                    self._opening_properties(
                        entity,
                        wall_entities=wall_entities,
                        scale=scale,
                        origin_x=origin_x,
                        origin_y=origin_y,
                        source_storey_key=source_storey_key,
                        default_fragment_id=source_fragment_id,
                        default_width=1.5,
                        default_depth=0.12,
                        default_height=1.5,
                        default_z=0.9,
                        width_key="overall_width_mm",
                        height_key="overall_height_mm",
                    ),
                )
            )

        return elements

    def _slab_properties(
        self,
        *,
        wall_entities: list[object],
        scale: float,
        origin_x: float,
        origin_y: float,
        fallback_width: float,
        fallback_depth: float,
        source_storey_key: str,
        source_fragment_id: str,
    ) -> dict[str, int | float | str]:
        # Use the pre-calculated floor dimensions from layout
        # fallback_width and fallback_depth already represent the floor footprint
        # Position the slab at the center of the floor coordinate system
        width = fallback_width
        depth = fallback_depth
        local_x = width / 2.0
        local_y = depth / 2.0

        return {
            "thickness_mm": 120,
            "geometry_source": "parsed_drawing",
            "source_category": "footprint_bbox",
            "source_storey_key": source_storey_key,
            "source_fragment_id": source_fragment_id,
            "geometry_anchor": "center",
            "local_x_m": round(local_x, 3),
            "local_y_m": round(local_y, 3),
            "local_z_m": 0.0,
            "rotation_deg": 0.0,
            "shape_width_m": round(width, 3),
            "shape_depth_m": round(depth, 3),
            "shape_height_m": 0.12,
        }

    def _wall_properties(
        self,
        entity: object,
        scale: float,
        origin_x: float,
        origin_y: float,
        storey_height: float,
        source_storey_key: str,
        default_fragment_id: str,
    ) -> dict[str, int | float | str]:
        metadata = getattr(entity, "metadata", {})
        thickness = 0.2
        if isinstance(metadata, dict):
            raw_width = metadata.get("tch_wall_width")
            try:
                thickness = max(float(raw_width) * scale, 0.08)
            except (TypeError, ValueError):
                thickness = 0.2
        entity_storey_key = self._entity_storey_key(entity, source_storey_key)
        entity_fragment_id = self._entity_fragment_id(entity, default_fragment_id)
        if entity.category == "wall_line" and len(entity.points) >= 2:
            start = entity.points[0]
            end = entity.points[-1]
            midpoint = Point2D(x=(start.x + end.x) / 2.0, y=(start.y + end.y) / 2.0)
            x, y = self._local_point(midpoint, scale, origin_x, origin_y)
            length = max(math.dist((start.x, start.y), (end.x, end.y)) * scale, 0.2)
            rotation = math.degrees(math.atan2(end.y - start.y, end.x - start.x))
            return {
                "length_m": round(length, 3),
                "height_m": round(storey_height, 3),
                "geometry_source": "parsed_drawing",
                "source_category": entity.category,
                "source_ref": entity.source_ref or "",
                "source_storey_key": entity_storey_key,
                "source_fragment_id": entity_fragment_id,
                "geometry_anchor": "center",
                "local_x_m": round(x, 3),
                "local_y_m": round(y, 3),
                "local_z_m": 0.0,
                "rotation_deg": round(rotation, 3),
                "shape_width_m": round(length, 3),
                "shape_depth_m": thickness,
                "shape_height_m": round(storey_height, 3),
            }

        bbox = entity.bbox
        if bbox is None:
            return {
                "length_m": 1.0,
                "height_m": round(storey_height, 3),
                "geometry_source": "parsed_drawing",
                "source_category": entity.category,
                "source_ref": entity.source_ref or "",
                "source_storey_key": entity_storey_key,
                "source_fragment_id": entity_fragment_id,
                "geometry_anchor": "center",
                "local_x_m": 0.0,
                "local_y_m": 0.0,
                "local_z_m": 0.0,
                "rotation_deg": 0.0,
                "shape_width_m": 1.0,
                "shape_depth_m": thickness,
                "shape_height_m": round(storey_height, 3),
            }

        width = max((bbox.max_x - bbox.min_x) * scale, thickness)
        depth = max((bbox.max_y - bbox.min_y) * scale, thickness)
        center_x = (bbox.min_x + bbox.max_x) / 2.0
        center_y = (bbox.min_y + bbox.max_y) / 2.0
        x, y = self._local_xy(center_x, center_y, scale, origin_x, origin_y)
        return {
            "length_m": round(max(width, depth), 3),
            "height_m": round(storey_height, 3),
            "geometry_source": "parsed_drawing",
            "source_category": entity.category,
            "source_ref": entity.source_ref or "",
            "source_storey_key": entity_storey_key,
            "source_fragment_id": entity_fragment_id,
            "geometry_anchor": "center",
            "local_x_m": round(x, 3),
            "local_y_m": round(y, 3),
            "local_z_m": 0.0,
            "rotation_deg": 0.0,
            "shape_width_m": round(width, 3),
            "shape_depth_m": round(depth, 3),
            "shape_height_m": round(storey_height, 3),
        }

    def _opening_properties(
        self,
        entity: object,
        *,
        wall_entities: list[object],
        scale: float,
        origin_x: float,
        origin_y: float,
        source_storey_key: str,
        default_fragment_id: str,
        default_width: float,
        default_depth: float,
        default_height: float,
        default_z: float,
        width_key: str,
        height_key: str,
    ) -> dict[str, int | float | str]:
        metadata = getattr(entity, "metadata", {})
        point_values = list(getattr(entity, "points", []) or [])
        anchor_point: Point2D | None = None
        rotation = 0.0
        width = default_width

        if len(point_values) >= 2:
            start = point_values[0]
            end = point_values[-1]
            anchor_point = Point2D(x=(start.x + end.x) / 2.0, y=(start.y + end.y) / 2.0)
            rotation = math.degrees(math.atan2(end.y - start.y, end.x - start.x))
            width = max(math.dist((start.x, start.y), (end.x, end.y)) * scale, default_width)
        elif point_values:
            anchor_point = point_values[0]
        elif getattr(entity, "bbox", None) is not None:
            bbox = entity.bbox
            anchor_point = Point2D(x=(bbox.min_x + bbox.max_x) / 2.0, y=(bbox.min_y + bbox.max_y) / 2.0)
            width = max((bbox.max_x - bbox.min_x) * scale, default_width)

        host_wall = self._match_opening_to_wall(entity, wall_entities, anchor_point, scale)
        if isinstance(metadata, dict):
            raw_width = metadata.get("tch_opening_width")
            raw_height = metadata.get("tch_opening_height")
            try:
                width = max(float(raw_width) * scale, 0.3)
            except (TypeError, ValueError):
                pass
            try:
                default_height = max(float(raw_height) * scale, 0.5)
            except (TypeError, ValueError):
                pass

        if host_wall is not None:
            anchor_point = host_wall["anchor_point"]
            rotation = float(host_wall["rotation_deg"])
            default_depth = max(default_depth, float(host_wall["thickness_m"]) + 0.02)

        if anchor_point is None:
            x = 0.0
            y = 0.0
        else:
            x, y = self._local_point(anchor_point, scale, origin_x, origin_y)

        entity_storey_key = self._entity_storey_key(entity, source_storey_key)
        entity_fragment_id = self._entity_fragment_id(entity, default_fragment_id)
        return {
            width_key: round(width * 1000),
            height_key: round(default_height * 1000),
            "geometry_source": "parsed_drawing",
            "source_category": entity.category,
            "source_ref": entity.source_ref,
            "source_storey_key": entity_storey_key,
            "source_fragment_id": entity_fragment_id,
            "geometry_anchor": "center",
            "local_x_m": round(x, 3),
            "local_y_m": round(y, 3),
            "local_z_m": round(default_z, 3),
            "rotation_deg": round(rotation, 3),
            "shape_width_m": round(width, 3),
            "shape_depth_m": round(default_depth, 3),
            "shape_height_m": round(default_height, 3),
            "host_wall_ref": host_wall["source_ref"] if host_wall is not None else "",
        }

    def _match_opening_to_wall(
        self,
        entity: object,
        wall_entities: list[object],
        anchor_point: Point2D | None,
        scale: float,
    ) -> dict[str, object] | None:
        if anchor_point is None:
            return None

        metadata = getattr(entity, "metadata", {})
        parent_refs: list[str] = []
        if isinstance(metadata, dict):
            raw_parent_refs = metadata.get("tch_parent_handles")
            if isinstance(raw_parent_refs, list):
                parent_refs = [str(value) for value in raw_parent_refs if value]

        candidates: list[dict[str, object]] = []
        for wall in wall_entities:
            wall_axis = self._wall_axis(wall, scale)
            if wall_axis is None:
                continue
            wall_axis["source_ref"] = getattr(wall, "source_ref", "") or ""
            candidates.append(wall_axis)

        if not candidates:
            return None

        for candidate in candidates:
            if candidate["source_ref"] in parent_refs:
                return self._project_opening_to_wall(anchor_point, candidate)

        scored: list[tuple[float, dict[str, object]]] = []
        for candidate in candidates:
            projection = self._project_opening_to_wall(anchor_point, candidate)
            scored.append((float(projection["distance_m"]), projection))
        if not scored:
            return None
        scored.sort(key=lambda item: item[0])
        return scored[0][1]

    def _wall_axis(self, entity: object, scale: float) -> dict[str, object] | None:
        points = list(getattr(entity, "points", []) or [])
        metadata = getattr(entity, "metadata", {})
        thickness = 0.2
        if isinstance(metadata, dict):
            try:
                thickness = max(float(metadata.get("tch_wall_width")) * scale, 0.08)
            except (TypeError, ValueError):
                thickness = 0.2

        if getattr(entity, "category", "") == "wall_line" and len(points) >= 2:
            start = points[0]
            end = points[-1]
            return {"start": start, "end": end, "thickness_m": thickness}

        bbox = getattr(entity, "bbox", None)
        if bbox is None:
            return None
        width = bbox.max_x - bbox.min_x
        depth = bbox.max_y - bbox.min_y
        if width >= depth:
            start = Point2D(x=bbox.min_x, y=(bbox.min_y + bbox.max_y) / 2.0)
            end = Point2D(x=bbox.max_x, y=(bbox.min_y + bbox.max_y) / 2.0)
        else:
            start = Point2D(x=(bbox.min_x + bbox.max_x) / 2.0, y=bbox.min_y)
            end = Point2D(x=(bbox.min_x + bbox.max_x) / 2.0, y=bbox.max_y)
        return {"start": start, "end": end, "thickness_m": max(thickness, min(width, depth) * scale)}

    def _project_opening_to_wall(
        self,
        anchor_point: Point2D,
        wall_axis: dict[str, object],
    ) -> dict[str, object]:
        start: Point2D = wall_axis["start"]  # type: ignore[assignment]
        end: Point2D = wall_axis["end"]  # type: ignore[assignment]
        dx = end.x - start.x
        dy = end.y - start.y
        length_sq = dx * dx + dy * dy
        if length_sq <= 1e-9:
            projected = start
            distance = math.dist((anchor_point.x, anchor_point.y), (start.x, start.y)) * 0.001
            rotation = 0.0
        else:
            t = ((anchor_point.x - start.x) * dx + (anchor_point.y - start.y) * dy) / length_sq
            clamped_t = min(max(t, 0.0), 1.0)
            projected = Point2D(x=start.x + dx * clamped_t, y=start.y + dy * clamped_t)
            distance = math.dist((anchor_point.x, anchor_point.y), (projected.x, projected.y)) * 0.001
            rotation = math.degrees(math.atan2(dy, dx))
        return {
            "anchor_point": projected,
            "rotation_deg": rotation,
            "thickness_m": wall_axis["thickness_m"],
            "distance_m": distance,
            "source_ref": wall_axis.get("source_ref", ""),
        }

    def _element(
        self,
        intent: DesignIntent,
        storey_id: str,
        ifc_type: str,
        name: str,
        family_name: str,
        properties: dict[str, int | float | str],
    ) -> BimElement:
        source_ref = str(properties.get("source_ref", "") or "")
        source_storey_key = str(properties.get("source_storey_key", "") or "")
        source_fragment_id = str(properties.get("source_fragment_id", "") or "")
        seed = (
            f"{intent.project_id}:{intent.version_id}:{storey_id}:{ifc_type}:{name}:"
            f"{source_storey_key}:{source_fragment_id}:{source_ref}"
        )
        guid = str(uuid.uuid5(uuid.NAMESPACE_URL, seed))
        element_id = f"{storey_id}_{ifc_type.lower()}_{uuid.uuid5(uuid.NAMESPACE_DNS, seed).hex[:8]}"
        return BimElement(
            element_id=element_id,
            guid=guid,
            ifc_type=ifc_type,
            name=name,
            family_name=family_name,
            storey_id=storey_id,
            properties=properties,
        )

    def _entity_storey_key(self, entity: object, fallback: str) -> str:
        metadata = getattr(entity, "metadata", {})
        if isinstance(metadata, dict):
            raw = metadata.get("source_storey_key") or metadata.get("fragment_storey_key") or metadata.get("storey_key")
            if raw:
                return str(raw)
        return fallback

    def _entity_role(self, entity: object, fallback: str) -> str:
        metadata = getattr(entity, "metadata", {})
        if isinstance(metadata, dict):
            raw = metadata.get("source_fragment_role") or metadata.get("fragment_role")
            if raw:
                return str(raw)
        return fallback

    def _entity_fragment_id(self, entity: object, fallback: str) -> str:
        metadata = getattr(entity, "metadata", {})
        if isinstance(metadata, dict):
            raw = metadata.get("source_fragment_id") or metadata.get("fragment_id")
            if raw:
                return str(raw)
        return fallback

    def _unit_scale(self, units: str) -> float:
        normalized = units.strip().lower()
        if normalized in {"m", "meter", "meters", "metre", "metres"}:
            return 1.0
        if normalized in {"cm", "centimeter", "centimeters", "centimetre", "centimetres"}:
            return 0.01
        return 0.001

    def _entity_bounds(self, entities: list[object]) -> tuple[float | None, float | None, float | None, float | None]:
        min_x: float | None = None
        min_y: float | None = None
        max_x: float | None = None
        max_y: float | None = None
        for entity in entities:
            if entity.points:
                for point in entity.points:
                    min_x = point.x if min_x is None else min(min_x, point.x)
                    min_y = point.y if min_y is None else min(min_y, point.y)
                    max_x = point.x if max_x is None else max(max_x, point.x)
                    max_y = point.y if max_y is None else max(max_y, point.y)
            elif entity.bbox is not None:
                min_x = entity.bbox.min_x if min_x is None else min(min_x, entity.bbox.min_x)
                min_y = entity.bbox.min_y if min_y is None else min(min_y, entity.bbox.min_y)
                max_x = entity.bbox.max_x if max_x is None else max(max_x, entity.bbox.max_x)
                max_y = entity.bbox.max_y if max_y is None else max(max_y, entity.bbox.max_y)
        return min_x, min_y, max_x, max_y

    def _local_point(self, point: object, scale: float, origin_x: float, origin_y: float) -> tuple[float, float]:
        return self._local_xy(point.x, point.y, scale, origin_x, origin_y)

    def _local_xy(self, x: float, y: float, scale: float, origin_x: float, origin_y: float) -> tuple[float, float]:
        return (x - origin_x) * scale, (y - origin_y) * scale

    def _polygon_area(self, points: list[object]) -> float:
        if len(points) < 3:
            return 0.0
        area = 0.0
        for index, point in enumerate(points):
            nxt = points[(index + 1) % len(points)]
            area += point.x * nxt.y - nxt.x * point.y
        return abs(area) / 2.0

    def _entity_bbox_area(self, entity: DrawingEntityRecord, scale: float) -> float:
        if entity.bbox is None:
            return 12.0
        width = max((entity.bbox.max_x - entity.bbox.min_x) * scale, 1.0)
        depth = max((entity.bbox.max_y - entity.bbox.min_y) * scale, 1.0)
        if width <= 1.0 and depth <= 1.0:
            return 12.0
        return width * depth


class ValidationService:
    def validate(
        self,
        intent: DesignIntent,
        rule_check: RuleCheckResult,
        model: BimSemanticModel,
    ) -> ValidationReport:
        runtime = detect_ifc_runtime()
        issues: list[ValidationIssue] = []
        affected: list[str] = []
        suggestions: list[str] = []

        for rule_issue in rule_check.issues:
            issues.append(
                ValidationIssue(
                    severity=rule_issue.severity,
                    message=rule_issue.message,
                    target=rule_issue.target,
                )
            )
        issues.extend(self._enforce_formal_delivery_blockers(rule_check))

        expected_storey_count = self._expected_storey_count(intent, model)
        if len(model.storeys) < expected_storey_count:
            issues.append(
                ValidationIssue(
                    severity="error",
                    message="楼层数量少于 DesignIntent 或图纸显式楼层事实。",
                    target="storeys",
                )
            )
            suggestions.append("检查执行计划中的楼层拆解逻辑。")

        issues.extend(self._validate_storey_integrity(model))
        issues.extend(self._validate_guid_uniqueness(model))
        issues.extend(self._validate_required_elements(intent, model))
        source_geometry_issues = self._validate_source_geometry(intent, rule_check, model)
        issues.extend(source_geometry_issues)
        issues.extend(self._validate_count_reconciliation(model))
        issues.extend(self._validate_position_boundaries(model))
        if source_geometry_issues:
            suggestions.append("检查 DXF 是否包含 ACAD_PROXY_ENTITY，并先转换为可解析的墙体/门窗/房间几何后再重新建模。")

        if intent.model_patch:
            replacement_count = int(model.metadata.get("replacement_count", 0))
            if replacement_count == 0:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        message="构件替换请求未命中任何窗构件。",
                        target="model_patch",
                    )
                )
                suggestions.append("复核 ElementSelector 的宽高条件或图纸识别结果。")
            else:
                affected.append("IfcWindow")

        status = "passed"
        severities = {issue.severity for issue in issues}
        if {"fatal", "error"} & severities:
            status = "failed"
        elif "warning" in severities:
            status = "warning"

        if not suggestions and status != "passed":
            suggestions.append("补全缺失字段后重新触发建模流程。")

        severity_counter = Counter(issue.severity for issue in issues)
        severity_counts = {
            level: severity_counter.get(level, 0) for level in ("info", "warning", "error", "fatal")
        }
        blocking_issues = [
            {
                "severity": issue.severity,
                "target": issue.target,
                "message": issue.message,
            }
            for issue in issues
            if issue.severity in {"error", "fatal"}
        ]
        gate_trace = {
            "validation_status": status,
            "severity_counts": severity_counts,
            "blocking_issues": blocking_issues,
            "formal_blocking_issues": [
                item for item in blocking_issues if str(item.get("target", "")).startswith("formal_gate.")
            ],
            "count_reconciliation": model.metadata.get("count_reconciliation", {}),
            "storey_layout_trace": model.metadata.get("storey_layout_trace", []),
            "suggested_actions": list(suggestions),
        }

        return ValidationReport(
            status=status,
            issues=issues,
            affected_elements=affected,
            fix_suggestions=suggestions,
            metadata={
                "validation_engine": runtime.validator,
                "ifc_exporter": runtime.exporter,
                "ifc_schema": runtime.schema,
                "formal_backend_ready": runtime.formal_backend_ready,
                "gate_trace": gate_trace,
            },
        )

    def _expected_storey_count(self, intent: DesignIntent, model: BimSemanticModel) -> int:
        expected = int(intent.constraints.floors)
        source_storey_keys = model.metadata.get("source_storey_keys", [])
        if (
            intent.source_mode == "cad_to_bim"
            and isinstance(source_storey_keys, list)
            and source_storey_keys
        ):
            expected = max(expected, len(source_storey_keys))
        return expected

    def _enforce_formal_delivery_blockers(self, rule_check: RuleCheckResult) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        for rule_issue in rule_check.issues:
            if rule_issue.code not in _FORMAL_BLOCKING_RULE_CODES:
                continue
            if rule_issue.severity in {"error", "fatal"}:
                continue
            issues.append(
                ValidationIssue(
                    severity="error",
                    message=f"检测问题 {rule_issue.code} 未整改完成，正式导出已阻断。",
                    target=f"formal_gate.{rule_issue.code}",
                )
            )
        return issues

    def _validate_source_geometry(
        self,
        intent: DesignIntent,
        rule_check: RuleCheckResult,
        model: BimSemanticModel,
    ) -> list[ValidationIssue]:
        if intent.source_mode != "cad_to_bim":
            return []

        geometry_source = str(model.metadata.get("geometry_source") or "")
        if geometry_source != "template_fallback":
            return []

        if self._source_geometry_entity_total(model) > 0:
            return []

        rule_codes = {issue.code for issue in rule_check.issues}

        issues = [
            ValidationIssue(
                severity="error",
                message="CAD 图纸未提取到主体墙体几何，当前 IFC 已退回模板生成，正式导出已阻断。",
                target="formal_gate.drawing.source_geometry_missing",
            )
        ]
        if "drawing.review.proxy_entities_unresolved" in rule_codes:
            issues.append(
                ValidationIssue(
                    severity="error",
                    message="图纸主体语义层包含未解析的 ACAD_PROXY_ENTITY，需先转换或展开后再重新建模。",
                    target="formal_gate.drawing.proxy_entities_unresolved",
                )
            )
        return issues

    def _validate_storey_integrity(self, model: BimSemanticModel) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        for storey in model.storeys:
            for element in storey.elements:
                if element.storey_id != storey.storey_id:
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            message="构件楼层归属与所在楼层不一致。",
                            target=element.element_id,
                        )
                    )
        return issues

    def _validate_guid_uniqueness(self, model: BimSemanticModel) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        seen: set[str] = set()
        for storey in model.storeys:
            for element in storey.elements:
                if element.guid in seen:
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            message="检测到重复 GUID，导出 IFC 时可能发生对象冲突。",
                            target=element.element_id,
                        )
                    )
                seen.add(element.guid)
        return issues

    def _validate_required_elements(
        self,
        intent: DesignIntent,
        model: BimSemanticModel,
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        required_types = {"IfcWall", "IfcSlab", "IfcDoor", "IfcWindow"}
        if intent.building_type == "residential":
            required_types.add("IfcSpace")
        for ifc_type in sorted(required_types):
            if ifc_type == "IfcSpace":
                space_count = sum(len(storey.spaces) for storey in model.storeys)
                if space_count == 0:
                    issues.append(
                        ValidationIssue(
                            severity="warning",
                            message="模型未生成任何空间对象，Revit 查看前建议复核空间识别逻辑。",
                            target="IfcSpace",
                        )
                    )
                continue
            if model.element_index.get(ifc_type, 0) <= 0:
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        message=f"模型缺少关键构件类型 {ifc_type}。",
                        target=ifc_type,
                    )
                )
        return issues

    def _validate_count_reconciliation(self, model: BimSemanticModel) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        source_totals = model.metadata.get("source_entity_totals", {})
        if not isinstance(source_totals, dict):
            return issues

        for ifc_type, source_key in _RECONCILIATION_TYPE_MAP.items():
            expected = int(source_totals.get(source_key, 0))
            if expected <= 0:
                continue
            if ifc_type == "IfcSpace":
                actual = sum(len(storey.spaces) for storey in model.storeys)
            else:
                actual = int(model.element_index.get(ifc_type, 0))
            if actual < expected:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        message=f"{ifc_type} 构件数量低于解析结果（{actual} < {expected}）。",
                        target=f"reconciliation.{ifc_type}",
                    )
                )
        return issues

    def _validate_position_boundaries(self, model: BimSemanticModel) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        bounds_by_storey = model.metadata.get("source_layout_bounds_by_storey", {})
        if not isinstance(bounds_by_storey, dict):
            return issues

        tolerance = 0.25
        for storey in model.storeys:
            for element in storey.elements:
                if element.properties.get("geometry_source") != "parsed_drawing":
                    continue

                source_storey_key = str(element.properties.get("source_storey_key", storey.name) or storey.name)
                source_fragment_id = str(element.properties.get("source_fragment_id", "") or "")
                if not source_fragment_id:
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            message="图纸来源构件缺失 source_fragment_id，无法追溯片段来源。",
                            target=element.element_id,
                        )
                    )
                    continue

                bounds = bounds_by_storey.get(source_storey_key)
                if not isinstance(bounds, dict):
                    continue

                local_x = self._as_float(element.properties.get("local_x_m"), 0.0)
                local_y = self._as_float(element.properties.get("local_y_m"), 0.0)
                width = max(self._as_float(element.properties.get("shape_width_m"), 0.0), 0.0)
                depth = max(self._as_float(element.properties.get("shape_depth_m"), 0.0), 0.0)
                source_category = str(element.properties.get("source_category", "") or "")
                geometry_anchor = str(element.properties.get("geometry_anchor", "") or "").lower()

                min_x = self._as_float(bounds.get("min_x_m"), 0.0)
                min_y = self._as_float(bounds.get("min_y_m"), 0.0)
                max_x = self._as_float(bounds.get("max_x_m"), 0.0)
                max_y = self._as_float(bounds.get("max_y_m"), 0.0)

                if geometry_anchor == "center":
                    anchor_x = local_x
                    anchor_y = local_y
                    outside_bounds = (
                        anchor_x < min_x - tolerance
                        or anchor_y < min_y - tolerance
                        or anchor_x > max_x + tolerance
                        or anchor_y > max_y + tolerance
                    )
                elif source_category in {"door_block", "window_block"}:
                    anchor_x = local_x + width / 2.0
                    anchor_y = local_y + depth / 2.0
                    outside_bounds = (
                        anchor_x < min_x - tolerance
                        or anchor_y < min_y - tolerance
                        or anchor_x > max_x + tolerance
                        or anchor_y > max_y + tolerance
                    )
                else:
                    outside_bounds = (
                        local_x < min_x - tolerance
                        or local_y < min_y - tolerance
                        or local_x > max_x + tolerance
                        or local_y > max_y + tolerance
                    )

                if outside_bounds:
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            message=(
                                f"构件坐标超出来源片段边界: storey={source_storey_key}, "
                                f"fragment={source_fragment_id}."
                            ),
                            target=element.element_id,
                        )
                    )
        return issues

    def _as_float(self, value: object, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _as_int(self, value: object) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def _source_geometry_entity_total(self, model: BimSemanticModel) -> int:
        source_totals = model.metadata.get("source_entity_totals", {})
        if isinstance(source_totals, dict):
            total = (
                self._as_int(source_totals.get("wall"))
                + self._as_int(source_totals.get("window"))
                + self._as_int(source_totals.get("door"))
                + self._as_int(source_totals.get("space"))
            )
            if total > 0:
                return total
        return (
            self._as_int(model.metadata.get("source_wall_entities"))
            + self._as_int(model.metadata.get("source_window_entities"))
            + self._as_int(model.metadata.get("source_door_entities"))
        )


class ExportService:
    def __init__(self, root: Path) -> None:
        self.root = root

    def export(
        self,
        project: ProjectSummary,
        intent: DesignIntent,
        model: BimSemanticModel,
        validation: ValidationReport,
    ) -> ExportBundle:
        runtime = detect_ifc_runtime()
        artifact_dir = self.root / project.project_id / intent.version_id
        artifact_dir.mkdir(parents=True, exist_ok=True)

        artifacts = [
            self._write_json(artifact_dir / "intent.json", intent.model_dump(mode="json")),
            self._write_json(artifact_dir / "validation.json", validation.model_dump(mode="json")),
            self._write_json(artifact_dir / "model.semantic.json", model.model_dump(mode="json")),
        ]

        gate_trace = validation.metadata.get("gate_trace", {})
        formal_blocking_issues = gate_trace.get("formal_blocking_issues", []) if isinstance(gate_trace, dict) else []
        export_allowed = validation.status != "failed" and not formal_blocking_issues
        blocked_by = [issue.message for issue in validation.issues if issue.severity in {"fatal", "error"}]
        for blocker in formal_blocking_issues:
            if not isinstance(blocker, dict):
                continue
            message = blocker.get("message")
            if message and message not in blocked_by:
                blocked_by.append(str(message))
        if export_allowed:
            ifc_path = artifact_dir / "model.ifc"
            self._write_ifc(ifc_path, self._render_ifc(project, intent, model), runtime)
            artifacts.append(
                ExportArtifact(
                    name="model.ifc",
                    path=str(ifc_path),
                    media_type="application/octet-stream",
                )
            )
            artifacts.append(
                self._write_json(
                    artifact_dir / "export-log.json",
                    {
                        "project_id": project.project_id,
                        "version_id": intent.version_id,
                        "runtime": runtime.exporter,
                        "schema": runtime.schema,
                        "formal_backend_ready": runtime.formal_backend_ready,
                        "artifact_count": len(artifacts) + 1,
                    },
                )
            )

        artifact_count = len(artifacts)
        export_gate_trace = {
            "validation_status": validation.status,
            "export_allowed": export_allowed,
            "blocking_messages": blocked_by,
            "formal_blocking_issues": formal_blocking_issues,
            "artifact_count": artifact_count,
            "ifc_written": export_allowed,
        }

        return ExportBundle(
            export_allowed=export_allowed,
            artifact_dir=str(artifact_dir),
            artifacts=artifacts,
            blocked_by=blocked_by,
            metadata={
                "ifc_exporter": runtime.exporter,
                "ifc_schema": runtime.schema,
                "formal_backend_ready": runtime.formal_backend_ready,
                "gate_trace": export_gate_trace,
            },
        )

    def _write_json(self, path: Path, payload: dict) -> ExportArtifact:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return ExportArtifact(name=path.name, path=str(path), media_type="application/json")

    def _write_ifc(self, path: Path, content: str, runtime: object) -> None:
        if getattr(runtime, "ifcopenshell_available", False):
            try:
                ifcopenshell = importlib.import_module("ifcopenshell")
                model = ifcopenshell.file.from_string(content)
                model.write(path)
                return
            except Exception:
                pass
        path.write_text(content, encoding="utf-8")

    def _render_ifc(self, project: ProjectSummary, intent: DesignIntent, model: BimSemanticModel) -> str:
        writer = _IfcEntityBuffer()
        origin_3d = writer.add("IFCCARTESIANPOINT((0.,0.,0.))")
        origin_2d = writer.add("IFCCARTESIANPOINT((0.,0.))")
        dir_x = writer.add("IFCDIRECTION((1.,0.,0.))")
        dir_y = writer.add("IFCDIRECTION((0.,1.,0.))")
        dir_z = writer.add("IFCDIRECTION((0.,0.,1.))")
        axis_2d = writer.add(f"IFCAXIS2PLACEMENT2D(#{origin_2d},#{dir_x})")
        world_axis = writer.add(f"IFCAXIS2PLACEMENT3D(#{origin_3d},#{dir_z},#{dir_x})")
        context = writer.add(
            f"IFCGEOMETRICREPRESENTATIONCONTEXT($,'Model',3,1.E-05,#{world_axis},#{self._true_north(writer, intent)})"
        )
        body_context = writer.add(
            f"IFCGEOMETRICREPRESENTATIONSUBCONTEXT('Body','Model',*,*,*,*,#{context},$,.MODEL_VIEW.,$)"
        )
        length_unit = writer.add("IFCSIUNIT(*,.LENGTHUNIT.,$,.METRE.)")
        area_unit = writer.add("IFCSIUNIT(*,.AREAUNIT.,$,.SQUARE_METRE.)")
        volume_unit = writer.add("IFCSIUNIT(*,.VOLUMEUNIT.,$,.CUBIC_METRE.)")
        plane_angle_unit = writer.add("IFCSIUNIT(*,.PLANEANGLEUNIT.,$,.RADIAN.)")
        units = writer.add(
            f"IFCUNITASSIGNMENT((#{length_unit},#{area_unit},#{volume_unit},#{plane_angle_unit}))"
        )
        project_id = writer.add(
            "IFCPROJECT("
            f"'{self._ifc_guid(f'{project.project_id}:{intent.version_id}:project')}',"
            "$,"
            f"'{_escape_ifc_text(project.name)}',"
            "$,$,$,$,"
            f"(#{context}),#{units})"
        )

        world_placement = writer.add(f"IFCLOCALPLACEMENT($,#{world_axis})")
        site_placement = self._add_local_placement(writer, world_placement, 0.0, 0.0, 0.0)
        building_placement = self._add_local_placement(writer, site_placement, 0.0, 0.0, 0.0)

        site_id = writer.add(
            "IFCSITE("
            f"'{self._ifc_guid(f'{project.project_id}:{intent.version_id}:site')}',"
            "$,'Site',$,$,"
            f"#{site_placement},$,$,.ELEMENT.,$,$,$,$,$)"
        )
        building_id = writer.add(
            "IFCBUILDING("
            f"'{self._ifc_guid(f'{project.project_id}:{intent.version_id}:building')}',"
            "$,"
            f"'{_escape_ifc_text(project.name)}',"
            "$,$,"
            f"#{building_placement},$,$,.ELEMENT.,0.,0.,$)"
        )

        writer.add(
            "IFCRELAGGREGATES("
            f"'{self._ifc_guid(f'{project.project_id}:{intent.version_id}:project_site')}',"
            "$,'Project Container',$,"
            f"#{project_id},(#{site_id}))"
        )
        writer.add(
            "IFCRELAGGREGATES("
            f"'{self._ifc_guid(f'{project.project_id}:{intent.version_id}:site_building')}',"
            "$,'Site Container',$,"
            f"#{site_id},(#{building_id}))"
        )

        storey_ids: list[int] = []
        for storey in model.storeys:
            storey_placement = self._add_local_placement(
                writer,
                building_placement,
                0.0,
                0.0,
                storey.elevation_m,
            )
            storey_id = writer.add(
                "IFCBUILDINGSTOREY("
                f"'{self._ifc_guid(storey.storey_id)}',"
                "$,"
                f"'{_escape_ifc_text(storey.name)}',"
                "$,$,"
                f"#{storey_placement},$,$,.ELEMENT.,{_format_ifc_float(storey.elevation_m)})"
            )
            storey_ids.append(storey_id)

            related_products: list[int] = []
            for space_index, space in enumerate(storey.spaces):
                placement = self._add_local_placement(
                    writer,
                    storey_placement,
                    *self._space_layout(space_index),
                )
                width, depth, height = self._space_dimensions(space)
                shape = self._add_box_shape(writer, body_context, axis_2d, origin_3d, dir_z, width, depth, height)
                related_products.append(
                    writer.add(
                        "IFCSPACE("
                        f"'{self._ifc_guid(space.space_id)}',"
                        "$,"
                        f"'{_escape_ifc_text(space.name)}',"
                        "$,$,"
                        f"#{placement},#{shape},$,.ELEMENT.,.INTERNAL.,0.)"
                    )
                )

            # First pass: create walls and collect them for opening matching
            wall_ids: dict[str, int] = {}
            counts_by_type: dict[str, int] = {}
            opening_data: list[tuple[BimElement, int, int, int, float, float, float, float]] = []

            for element in storey.elements:
                element_index = counts_by_type.get(element.ifc_type, 0)
                counts_by_type[element.ifc_type] = element_index + 1
                x, y, z, rotation_deg, width, depth, height = self._element_layout(
                    element,
                    element_index,
                    intent,
                )

                if element.ifc_type == "IfcWall":
                    placement = self._add_local_placement(writer, storey_placement, x, y, z, rotation_deg)
                    shape = self._add_box_shape(writer, body_context, axis_2d, origin_3d, dir_z, width, depth, height)
                    wall_id = self._add_ifc_product(writer, element, placement, shape)
                    related_products.append(wall_id)
                    wall_ids[element.element_id] = wall_id

                elif element.ifc_type in ("IfcDoor", "IfcWindow"):
                    # Store opening data for second pass
                    opening_data.append((element, element_index, x, y, z, rotation_deg, width, depth, height))

                elif element.ifc_type == "IfcSlab":
                    # Create slab with corrected positioning
                    placement = self._add_local_placement(writer, storey_placement, x, y, z, rotation_deg)
                    shape = self._add_box_shape(writer, body_context, axis_2d, origin_3d, dir_z, width, depth, height)
                    related_products.append(self._add_ifc_product(writer, element, placement, shape))

            # Second pass: create openings for doors and windows
            for element, element_index, x, y, z, rotation_deg, width, depth, height in opening_data:
                host_wall_ref = str(element.properties.get("host_wall_ref", "") or "")
                host_wall_id = wall_ids.get(host_wall_ref)

                if host_wall_id is None:
                    # No host wall found, create as independent element
                    placement = self._add_local_placement(writer, storey_placement, x, y, z, rotation_deg)
                    shape = self._add_box_shape(writer, body_context, axis_2d, origin_3d, dir_z, width, depth, height)
                    related_products.append(self._add_ifc_product(writer, element, placement, shape))
                    continue

                # Create opening element
                opening_placement = self._add_local_placement(writer, storey_placement, x, y, z, rotation_deg)
                opening_shape = self._add_box_shape(writer, body_context, axis_2d, origin_3d, dir_z, width, depth, height)
                opening_id = writer.add(
                    f"IFCOPENINGELEMENT('{self._ifc_guid(f'{element.element_id}:opening')}',$,'Opening',$,$,#{opening_placement},#{opening_shape},$,.OPENING.)"
                )

                # Create void relationship (wall -> opening)
                writer.add(
                    f"IFCRELVOIDSELEMENT('{self._ifc_guid(f'{element.element_id}:void')}',$,'Opening',$,#{host_wall_id},#{opening_id})"
                )

                # Create door/window
                element_placement = self._add_local_placement(writer, storey_placement, x, y, z, rotation_deg)
                element_shape = self._add_box_shape(writer, body_context, axis_2d, origin_3d, dir_z, width, depth, height)
                element_id = self._add_ifc_product(writer, element, element_placement, element_shape)
                related_products.append(element_id)

                # Create fills relationship (opening -> door/window)
                writer.add(
                    f"IFCRELFILLSELEMENT('{self._ifc_guid(f'{element.element_id}:fill')}',$,'Fills',$,#{opening_id},#{element_id})"
                )

            if related_products:
                refs = ",".join(f"#{product_id}" for product_id in related_products)
                writer.add(
                    "IFCRELCONTAINEDINSPATIALSTRUCTURE("
                    f"'{self._ifc_guid(f'{storey.storey_id}:containment')}',"
                    "$,'Storey Content',$,"
                    f"({refs}),#{storey_id})"
                )

        if storey_ids:
            storey_refs = ",".join(f"#{storey_id}" for storey_id in storey_ids)
            writer.add(
                "IFCRELAGGREGATES("
                f"'{self._ifc_guid(f'{project.project_id}:{intent.version_id}:building_storeys')}',"
                "$,'Building Storeys',$,"
                f"#{building_id},({storey_refs}))"
            )

        timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        lines = [
            "ISO-10303-21;",
            "HEADER;",
            "FILE_DESCRIPTION(('ViewDefinition [CoordinationView]','Codex Jianmo export'),'2;1');",
            f"FILE_NAME('{project.project_id}_{intent.version_id}.ifc','{timestamp}',('Codex'),('OpenAI'),'Codex','Jianmo MVP','');",
            "FILE_SCHEMA(('IFC4'));",
            "ENDSEC;",
            "DATA;",
        ]
        lines.extend(writer.render())
        lines.extend(["ENDSEC;", "END-ISO-10303-21;"])
        return "\n".join(lines)

    def _true_north(self, writer: _IfcEntityBuffer, intent: DesignIntent) -> int:
        north_angle = intent.site.north_angle or 0.0
        radians = math.radians(north_angle)
        x = math.sin(radians)
        y = math.cos(radians)
        return writer.add(f"IFCDIRECTION(({_format_ifc_float(x)},{_format_ifc_float(y)},0.))")

    def _ifc_guid(self, value: str) -> str:
        return _compress_ifc_guid(value)

    def _add_local_placement(
        self,
        writer: _IfcEntityBuffer,
        parent_placement: int,
        x: float,
        y: float,
        z: float,
        rotation_deg: float = 0.0,
    ) -> int:
        radians = math.radians(rotation_deg)
        ref_direction = writer.add(
            f"IFCDIRECTION(({_format_ifc_float(math.cos(radians))},{_format_ifc_float(math.sin(radians))},0.))"
        )
        axis = writer.add(
            "IFCAXIS2PLACEMENT3D("
            f"#{writer.add(f'IFCCARTESIANPOINT(({_format_ifc_float(x)},{_format_ifc_float(y)},{_format_ifc_float(z)}))')},"
            f"#{writer.add('IFCDIRECTION((0.,0.,1.))')},"
            f"#{ref_direction})"
        )
        return writer.add(f"IFCLOCALPLACEMENT(#{parent_placement},#{axis})")

    def _add_box_shape(
        self,
        writer: _IfcEntityBuffer,
        body_context: int,
        axis_2d: int,
        origin_3d: int,
        dir_z: int,
        width: float,
        depth: float,
        height: float,
    ) -> int:
        profile = writer.add(
            "IFCRECTANGLEPROFILEDEF("
            f".AREA.,$,"
            f"#{axis_2d},{_format_ifc_float(width)},{_format_ifc_float(depth)})"
        )
        solid_position = writer.add(f"IFCAXIS2PLACEMENT3D(#{origin_3d},#{dir_z},$)")
        solid = writer.add(
            "IFCEXTRUDEDAREASOLID("
            f"#{profile},#{solid_position},#{dir_z},{_format_ifc_float(height)})"
        )
        shape = writer.add(
            "IFCSHAPEREPRESENTATION("
            f"#{body_context},'Body','SweptSolid',(#{solid}))"
        )
        return writer.add(f"IFCPRODUCTDEFINITIONSHAPE($,$,(#{shape}))")

    def _space_layout(self, index: int) -> tuple[float, float, float]:
        layouts = (
            (4.0, 4.0, 0.0),
            (12.0, 4.0, 0.0),
            (20.0, 4.0, 0.0),
            (6.0, 10.0, 0.0),
            (16.0, 10.0, 0.0),
        )
        return layouts[index % len(layouts)]

    def _space_dimensions(self, space: BimSpace) -> tuple[float, float, float]:
        width = max(4.0, math.sqrt(space.area_sqm))
        depth = max(4.0, space.area_sqm / width)
        height = 2.8
        return round(width, 3), round(depth, 3), height

    def _element_layout(
        self,
        element: BimElement,
        index: int,
        intent: DesignIntent,
    ) -> tuple[float, float, float, float, float, float, float]:
        source_geometry = self._property_geometry(element.properties)
        if source_geometry is not None:
            return source_geometry

        storey_height = max(intent.constraints.standard_floor_height_m, 2.8)
        if element.ifc_type == "IfcSlab":
            return (12.0, 7.0, 0.0, 0.0, 24.0, 14.0, 0.12)

        if element.ifc_type == "IfcWall":
            wall_length = float(element.properties.get("length_m", 6.0))
            wall_thickness = 0.2
            wall_specs = (
                (12.0, 0.1, 0.0, 0.0, 24.0, wall_thickness, storey_height),
                (12.0, 13.9, 0.0, 0.0, 24.0, wall_thickness, storey_height),
                (0.1, 7.0, 0.0, 90.0, 14.0, wall_thickness, storey_height),
                (23.9, 7.0, 0.0, 90.0, 14.0, wall_thickness, storey_height),
                (8.0, 7.0, 0.0, 90.0, 10.0, wall_thickness, storey_height),
                (16.0, 7.0, 0.0, 90.0, 10.0, wall_thickness, storey_height),
                (12.0, 4.0, 0.0, 0.0, 16.0, wall_thickness, storey_height),
                (12.0, 10.0, 0.0, 0.0, 16.0, wall_thickness, storey_height),
            )
            x, y, z, rotation_deg, width, depth, height = wall_specs[index % len(wall_specs)]
            return (x, y, z, rotation_deg, max(width, wall_length), depth, height)

        if element.ifc_type == "IfcDoor":
            slot = index + 1
            x = 4.0 + slot * 4.0
            width = float(element.properties.get("width_mm", 1000)) / 1000.0
            height = float(element.properties.get("height_mm", 2100)) / 1000.0
            return (x, 0.35, 0.0, 0.0, width, 0.15, height)

        if element.ifc_type == "IfcWindow":
            slot = (index // 2) + 1
            is_secondary = "B" in element.name
            x = 3.0 + slot * 5.0
            y = 13.7 if not is_secondary else 0.35
            width = float(element.properties.get("overall_width_mm", 1200)) / 1000.0
            height = float(element.properties.get("overall_height_mm", 1500)) / 1000.0
            return (x, y, 0.9, 0.0, width, 0.12, height)

        return (4.0 + index * 2.0, 6.0, 0.0, 0.0, 1.0, 1.0, 1.0)

    def _property_geometry(
        self,
        properties: dict[str, int | float | str],
    ) -> tuple[float, float, float, float, float, float, float] | None:
        required = (
            "local_x_m",
            "local_y_m",
            "local_z_m",
            "rotation_deg",
            "shape_width_m",
            "shape_depth_m",
            "shape_height_m",
        )
        if not all(key in properties for key in required):
            return None
        return tuple(float(properties[key]) for key in required)  # type: ignore[return-value]

    def _add_ifc_product(
        self,
        writer: _IfcEntityBuffer,
        element: BimElement,
        placement: int,
        shape: int,
    ) -> int:
        name = _escape_ifc_text(element.name)
        tag = _escape_ifc_text(element.element_id)
        guid = self._ifc_guid(element.guid)
        if element.ifc_type == "IfcWall":
            return writer.add(
                f"IFCWALL('{guid}',$,'{name}',$,$,#{placement},#{shape},'{tag}',$)"
            )
        if element.ifc_type == "IfcSlab":
            return writer.add(
                f"IFCSLAB('{guid}',$,'{name}',$,$,#{placement},#{shape},'{tag}',.FLOOR.)"
            )
        if element.ifc_type == "IfcDoor":
            overall_height = _format_ifc_float(float(element.properties.get('height_mm', 2100)) / 1000.0)
            overall_width = _format_ifc_float(float(element.properties.get('width_mm', 1000)) / 1000.0)
            return writer.add(
                "IFCDOOR("
                f"'{guid}',$,'{name}',$,$,#{placement},#{shape},'{tag}',"
                f"{overall_height},{overall_width},.DOOR.,.NOTDEFINED.,$)"
            )
        if element.ifc_type == "IfcWindow":
            overall_height = _format_ifc_float(float(element.properties.get('overall_height_mm', 1500)) / 1000.0)
            overall_width = _format_ifc_float(float(element.properties.get('overall_width_mm', 1200)) / 1000.0)
            return writer.add(
                "IFCWINDOW("
                f"'{guid}',$,'{name}',$,$,#{placement},#{shape},'{tag}',"
                f"{overall_height},{overall_width},.WINDOW.,.NOTDEFINED.,$)"
            )
        return writer.add(
            f"IFCBUILDINGELEMENTPROXY('{guid}',$,'{name}',$,$,#{placement},#{shape},'{tag}',$)"
        )


class FeedbackService:
    def __init__(self, root: Path) -> None:
        self.root = root

    def record(
        self,
        project: ProjectSummary,
        version: VersionSnapshot,
        payload: FeedbackCreateRequest,
    ) -> FeedbackReceipt:
        receipt = FeedbackReceipt(feedback_id=f"fbk_{uuid.uuid4().hex[:12]}")
        feedback_dir = self.root / project.project_id / version.source_bundle.version_id / "feedback"
        feedback_dir.mkdir(parents=True, exist_ok=True)
        feedback_path = feedback_dir / f"{receipt.feedback_id}.json"
        feedback_path.write_text(
            json.dumps(
                {
                    "feedback_id": receipt.feedback_id,
                    "received_at": receipt.received_at,
                    "project_id": project.project_id,
                    "request_id": version.source_bundle.request_id,
                    "version_id": version.source_bundle.version_id,
                    "topic": payload.topic,
                    "comment": payload.comment,
                    "metadata": payload.metadata,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return receipt


class ModelingPipeline:
    def __init__(self, store: Store, export_root: Path) -> None:
        self.store = store
        self.source_builder = SourceBundleBuilder(store)
        self.drawing_parser = RealDrawingParser()
        self.intent_transformer = StructuredIntentTransformer()
        self.rule_engine = ConfigurableRuleEngine()
        self.planner = ConfigurableModelingPlanner()
        self.bim_engine = BimEngine()
        self.validation_service = ValidationService()
        self.export_service = ExportService(export_root)
        self.feedback_service = FeedbackService(export_root)

    def create_project(self, payload: ProjectCreateRequest) -> ProjectSummary:
        return self.store.create_project(payload)

    def list_projects(self) -> list[ProjectSummary]:
        return self.store.list_projects()

    def create_asset(
        self,
        project_id: str,
        *,
        filename: str,
        media_type: str,
        description: str | None,
        path: str,
        extension: str,
        size_bytes: int,
        content_hash: str,
    ) -> AssetRecord:
        return self.store.create_asset(
            project_id,
            filename=filename,
            media_type=media_type,
            description=description,
            path=path,
            extension=extension,
            size_bytes=size_bytes,
            content_hash=content_hash,
        )

    def list_assets(self, project_id: str) -> list[AssetRecord]:
        return self.store.list_assets(project_id)

    def get_asset(self, project_id: str, asset_id: str) -> AssetRecord | None:
        return self.store.get_asset(project_id, asset_id)

    def create_request(
        self,
        project_id: str,
        payload: ModelingRequestCreate,
    ) -> ModelingRequestRecord:
        return self.store.create_request(project_id, payload)

    def list_requests(self, project_id: str) -> list[ModelingRequestRecord]:
        return self.store.list_requests(project_id)

    def get_request(self, project_id: str, request_id: str) -> ModelingRequestRecord | None:
        return self.store.get_request(project_id, request_id)

    def list_versions(self, project_id: str) -> list[VersionSnapshot]:
        return self.store.list_versions(project_id)

    def get_project(self, project_id: str) -> ProjectSummary | None:
        return self.store.get_project(project_id)

    def get_version(self, project_id: str, version_id: str) -> VersionSnapshot | None:
        return self.store.get_version(project_id, version_id)

    def get_export_artifact(
        self,
        project_id: str,
        version_id: str,
        artifact_name: str,
    ) -> ExportArtifact:
        version = self.store.get_version(project_id, version_id)
        if version is None:
            raise KeyError(version_id)

        artifact = next((item for item in version.export_bundle.artifacts if item.name == artifact_name), None)
        if artifact is None:
            raise KeyError(artifact_name)

        artifact_dir = Path(version.export_bundle.artifact_dir).resolve()
        artifact_path = Path(artifact.path).resolve()
        try:
            artifact_path.relative_to(artifact_dir)
        except ValueError as exc:
            raise ValueError(f"Artifact path escapes artifact directory: {artifact.path}") from exc
        if not artifact_path.is_file():
            raise FileNotFoundError(artifact.path)
        return artifact

    def submit_feedback(
        self,
        project_id: str,
        version_id: str,
        payload: FeedbackCreateRequest,
    ) -> FeedbackReceipt:
        project = self.store.get_project(project_id)
        if project is None:
            raise KeyError(project_id)

        version = self.store.get_version(project_id, version_id)
        if version is None:
            raise KeyError(version_id)
        return self.feedback_service.record(project, version, payload)

    def parse_request(self, project_id: str, request_id: str) -> ParsedDrawingModel:
        project = self.store.get_project(project_id)
        if project is None:
            raise KeyError(project_id)

        request = self.store.get_request(project_id, request_id)
        if request is None:
            raise KeyError(request_id)

        assets = self._load_request_assets(project_id, request)
        payload = self._request_to_input(request, assets)
        source_bundle = self.source_builder.build(
            project,
            payload,
            request_id=request.request_id,
            assets=assets,
        )
        return self.drawing_parser.parse(source_bundle)

    def run(self, project_id: str, payload: ModelingRequestInput) -> VersionSnapshot:
        project = self.store.get_project(project_id)
        if project is None:
            raise KeyError(project_id)

        request = self.store.create_request(project_id, self._to_request_create(payload, asset_ids=[]))
        return self._run_pipeline(project, payload, request_id=request.request_id)

    def run_request(self, project_id: str, request_id: str) -> VersionSnapshot:
        project = self.store.get_project(project_id)
        if project is None:
            raise KeyError(project_id)

        request = self.store.get_request(project_id, request_id)
        if request is None:
            raise KeyError(request_id)

        assets = self._load_request_assets(project_id, request)
        payload = self._request_to_input(request, assets)
        return self._run_pipeline(project, payload, request_id=request.request_id, assets=assets)

    def _run_pipeline(
        self,
        project: ProjectSummary,
        payload: ModelingRequestInput,
        *,
        request_id: str,
        assets: list[AssetRecord] | None = None,
    ) -> VersionSnapshot:
        source_bundle = self.source_builder.build(project, payload, request_id=request_id, assets=assets)
        parsed_drawing = self.drawing_parser.parse(source_bundle)
        design_intent = self.intent_transformer.transform(source_bundle, parsed_drawing)
        rule_check = self.rule_engine.evaluate(design_intent, parsed_drawing)
        modeling_plan = self.planner.plan(design_intent, rule_check)
        bim_model = self.bim_engine.build(design_intent, modeling_plan, parsed_drawing)
        validation = self.validation_service.validate(design_intent, rule_check, bim_model)
        export_bundle = self.export_service.export(project, design_intent, bim_model, validation)

        snapshot = VersionSnapshot(
            project=project.model_copy(deep=True),
            source_bundle=source_bundle,
            parsed_drawing=parsed_drawing,
            design_intent=design_intent,
            rule_check=rule_check,
            modeling_plan=modeling_plan,
            bim_model=bim_model,
            validation=validation,
            export_bundle=export_bundle,
        )
        return self.store.save_version(snapshot)

    def _load_request_assets(
        self,
        project_id: str,
        request: ModelingRequestRecord,
    ) -> list[AssetRecord]:
        assets: list[AssetRecord] = []
        for asset_id in request.asset_ids:
            asset = self.store.get_asset(project_id, asset_id)
            if asset is None:
                raise KeyError(asset_id)
            assets.append(asset)
        return assets

    def _request_to_input(
        self,
        request: ModelingRequestRecord,
        assets: list[AssetRecord],
    ) -> ModelingRequestInput:
        payload = request.model_dump(
            exclude={"project_id", "request_id", "created_at", "latest_version_id", "asset_ids"},
            exclude_none=True,
            mode="json",
        )
        payload["assets"] = [
            AssetInput(
                filename=asset.filename,
                media_type=asset.media_type,
                description=asset.description,
                path=asset.path,
            )
            for asset in assets
        ]
        return ModelingRequestInput(**payload)

    def _to_request_create(
        self,
        payload: ModelingRequestInput,
        *,
        asset_ids: list[str],
    ) -> ModelingRequestCreate:
        request_payload = payload.model_dump(
            exclude={"assets"},
            exclude_none=True,
            mode="json",
        )
        request_payload["asset_ids"] = asset_ids
        return ModelingRequestCreate(**request_payload)
