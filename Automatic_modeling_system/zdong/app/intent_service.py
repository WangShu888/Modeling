from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from .models import (
    Assumption,
    CompletionTraceItem,
    Constraints,
    DesignIntent,
    DrawingEntityRecord,
    ElementSelector,
    MissingField,
    ModelPatch,
    ParsedDrawingModel,
    Point2D,
    ProgramInfo,
    SiteInfo,
    SourceBundle,
    StructuredIntentOutput,
    StyleInfo,
)
from .storey_inference import infer_floor_count


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
_INTENT_CONFIG_PATH = Path(__file__).resolve().parent / "config" / "intent_defaults.json"


def _normalize_text(text: str) -> str:
    normalized = text
    for source, target in _CHINESE_NUMERALS.items():
        normalized = normalized.replace(source, target)
    return normalized


def _extract_model_patch_from_prompt(prompt: str) -> tuple[ElementSelector | None, ModelPatch | None]:
    lower_prompt = prompt.lower()
    has_window_target = "窗" in prompt or "window" in lower_prompt
    has_replace_action = any(token in prompt for token in ("替换", "更换")) or "replace" in lower_prompt
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
        scope={"selector_scope": "matched_elements"},
    )
    return selector, patch


@lru_cache(maxsize=4)
def _load_intent_config(path_str: str) -> dict[str, Any]:
    return json.loads(Path(path_str).read_text(encoding="utf-8"))


class IntentProvider(Protocol):
    def build(self, bundle: SourceBundle, parsed: ParsedDrawingModel) -> StructuredIntentOutput: ...


class HeuristicStructuredIntentProvider:
    provider_name = "heuristic_structured_v1"

    def __init__(self, config_path: Path | None = None) -> None:
        self.config_path = config_path or _INTENT_CONFIG_PATH

    @property
    def config(self) -> dict[str, Any]:
        return _load_intent_config(str(self.config_path))

    def build(self, bundle: SourceBundle, parsed: ParsedDrawingModel) -> StructuredIntentOutput:
        prompt = _normalize_text(bundle.prompt)
        assumptions: list[Assumption] = []
        completion_trace: list[CompletionTraceItem] = []
        missing_fields: list[MissingField] = []

        building_type, building_type_source = self._infer_building_type(prompt, bundle.building_type_hint)
        profile = self._profile(building_type)
        completion_trace.append(
            self._trace(
                field="building_type",
                value=building_type,
                source=building_type_source,
                source_type=building_type_source,
                source_ref="bundle.building_type_hint" if building_type_source == "form_input" else "prompt_tokens",
                confidence=1.0 if building_type_source == "form_input" else 0.8,
            )
        )

        source_mode = self._infer_source_mode(bundle, parsed, prompt)
        completion_trace.append(
            self._trace(
                field="source_mode",
                value=source_mode,
                source="parsed_assets" if parsed.assets_count else "system_default",
                source_type="parsed_drawing" if parsed.assets_count else "system_default",
                source_ref="parsed_drawing.assets_count",
                confidence=0.95 if parsed.assets_count else 0.6,
            )
        )

        prompt_floors = self._extract_int(prompt, r"(\d+)\s*层")
        parsed_floor_count = None if prompt_floors is not None else infer_floor_count(parsed)
        floors = self._resolve_int(
            value=bundle.form_fields.get("floors"),
            inferred=prompt_floors if prompt_floors is not None else parsed_floor_count,
            default=1,
            field="constraints.floors",
            source=f"{building_type}_template_default",
            source_ref=f"intent_defaults.{building_type}.default_floor_count",
            assumptions=assumptions,
            completion_trace=completion_trace,
            inferred_source="text_prompt" if prompt_floors is not None else "parsed_drawing",
            inferred_source_type="text_prompt" if prompt_floors is not None else "parsed_drawing",
            inferred_source_ref="prompt.floor_count" if prompt_floors is not None else "parsed_drawing.asset_storeys",
            inferred_confidence=0.85 if prompt_floors is not None else 0.72,
        )
        standard_floor_height = self._resolve_float(
            value=bundle.form_fields.get("standard_floor_height_m"),
            inferred=self._extract_float(prompt, r"(?:标准层层高|层高)[:：]?\s*([0-9.]+)"),
            default=float(profile["default_standard_floor_height_m"]),
            field="constraints.standard_floor_height_m",
            source=f"{building_type}_template_default",
            source_ref=f"intent_defaults.{building_type}.default_standard_floor_height_m",
            assumptions=assumptions,
            completion_trace=completion_trace,
        )
        first_floor_height = self._resolve_float(
            value=bundle.form_fields.get("first_floor_height_m"),
            inferred=self._extract_float(prompt, r"首层层高[:：]?\s*([0-9.]+)"),
            default=float(profile["default_first_floor_height_m"]),
            field="constraints.first_floor_height_m",
            source=f"{building_type}_template_default",
            source_ref=f"intent_defaults.{building_type}.default_first_floor_height_m",
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
                    self._trace(
                        field="site.area_sqm",
                        value=site_area,
                        source="parsed_drawing_inferred_boundary",
                        source_type="parsed_drawing",
                        source_ref="parsed_drawing.detected_entities",
                        confidence=0.7,
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
            inferred=self._extract_int(prompt, r"(\d+)\s*(?:户|units?)"),
            field="program.units_per_floor",
            completion_trace=completion_trace,
        )
        if units_per_floor is None and profile.get("default_units_per_floor") is not None:
            units_per_floor = int(profile["default_units_per_floor"])
            assumptions.append(
                Assumption(
                    field="program.units_per_floor",
                    value=units_per_floor,
                    source=f"{building_type}_template_default",
                    confidence=0.6,
                )
            )
            completion_trace.append(
                self._trace(
                    field="program.units_per_floor",
                    value=units_per_floor,
                    source=f"{building_type}_template_default",
                    source_type="template_default",
                    source_ref=f"intent_defaults.{building_type}.default_units_per_floor",
                    confidence=0.6,
                )
            )

        if not bundle.region:
            missing_fields.append(
                MissingField(
                    field="region",
                    reason="地区规则集未指定，将继续沿用建筑类型默认规则集。",
                    critical=False,
                )
            )
            completion_trace.append(
                self._trace(
                    field="region",
                    value="building_type_default_ruleset",
                    source="system_default",
                    source_type="system_default",
                    source_ref="bundle.region",
                    confidence=0.5,
                )
            )

        ruleset = str(profile["default_ruleset"])
        assumptions.append(
            Assumption(
                field="constraints.ruleset",
                value=ruleset,
                source="building_type_ruleset",
                confidence=0.85,
            )
        )
        completion_trace.append(
            self._trace(
                field="constraints.ruleset",
                value=ruleset,
                source="building_type_ruleset",
                source_type="template_default",
                source_ref=f"intent_defaults.{building_type}.default_ruleset",
                confidence=0.85,
            )
        )

        boundary_source = "drawing_or_uploaded_polygon" if parsed.site_boundary_detected or parsed.assets_count else "unspecified"
        completion_trace.append(
            self._trace(
                field="site.boundary_source",
                value=boundary_source,
                source="parsed_drawing" if parsed.assets_count else "system_default",
                source_type="parsed_drawing" if parsed.assets_count else "system_default",
                source_ref="parsed_drawing.site_boundary_detected",
                confidence=0.8 if parsed.assets_count else 0.6,
            )
        )
        if source_mode == "text_only" and site_area is None:
            missing_fields.append(
                MissingField(
                    field="site.area_sqm",
                    reason="文本建模未提供用地面积，将生成草案级方案。",
                    critical=False,
                )
            )

        north_angle = float(parsed.north_angle)
        completion_trace.append(
            self._trace(
                field="site.north_angle",
                value=north_angle,
                source="parsed_drawing" if parsed.assets_count else "system_default",
                source_type="parsed_drawing" if parsed.assets_count else "system_default",
                source_ref="parsed_drawing.north_angle",
                confidence=0.75 if parsed.assets_count else 0.4,
            )
        )

        spaces_from_drawings = parsed.space_candidates_detected > 0 or source_mode == "cad_to_bim"
        completion_trace.append(
            self._trace(
                field="program.spaces_from_drawings",
                value=spaces_from_drawings,
                source="parsed_drawing" if parsed.assets_count else "system_default",
                source_type="parsed_drawing" if parsed.assets_count else "system_default",
                source_ref="parsed_drawing.space_candidates_detected",
                confidence=0.75 if parsed.assets_count else 0.5,
            )
        )

        selector, patch = _extract_model_patch_from_prompt(prompt)
        if selector and patch:
            completion_trace.append(
                self._trace(
                    field="model_patch",
                    value=patch.target_family,
                    source="text_prompt",
                    source_type="text_prompt",
                    source_ref="window_replace_pattern",
                    confidence=0.9,
                )
            )
            completion_trace.append(
                self._trace(
                    field="element_selector",
                    value=selector.ifc_type,
                    source="text_prompt",
                    source_type="text_prompt",
                    source_ref="window_replace_pattern",
                    confidence=0.85,
                )
            )

        deliverables = list(bundle.form_fields.get("output_formats") or self.config.get("default_deliverables", ["ifc"]))
        completion_trace.append(
            self._trace(
                field="deliverables",
                value=deliverables,
                source="form_input" if bundle.form_fields.get("output_formats") else "template_default",
                source_type="form_input" if bundle.form_fields.get("output_formats") else "template_default",
                source_ref="form_fields.output_formats" if bundle.form_fields.get("output_formats") else "intent_defaults.default_deliverables",
                confidence=1.0 if bundle.form_fields.get("output_formats") else 0.8,
            )
        )

        final_use = "revit_view"
        completion_trace.append(
            self._trace(
                field="final_use",
                value=final_use,
                source="system_default",
                source_type="system_default",
                source_ref="module_requirements.final_use",
                confidence=0.8,
            )
        )

        return StructuredIntentOutput(
            schema_version=str(self.config.get("schema_version", "jianmo.intent.v1")),
            source_mode=source_mode,
            building_type=building_type,
            site=SiteInfo(
                boundary_source=boundary_source,
                area_sqm=site_area,
                north_angle=north_angle,
            ),
            constraints=Constraints(
                floors=floors,
                standard_floor_height_m=standard_floor_height,
                first_floor_height_m=first_floor_height,
                ruleset=ruleset,
                far=far,
            ),
            program=ProgramInfo(
                spaces_from_drawings=spaces_from_drawings,
                units_per_floor=units_per_floor,
                core_type=str(profile["core_type"]),
                first_floor_spaces=list(profile["first_floor_spaces"]),
                typical_floor_spaces=list(profile["typical_floor_spaces"]),
            ),
            style=StyleInfo(
                facade=str(profile["cad_facade"] if source_mode == "cad_to_bim" else profile["text_facade"]),
                material_palette=list(profile["material_palette"]),
            ),
            deliverables=deliverables,
            final_use=final_use,
            missing_fields=missing_fields,
            assumptions=assumptions,
            completion_trace=completion_trace,
            element_selector=selector,
            model_patch=patch,
            metadata={
                "intent_provider": self.provider_name,
                "config_version": self.config.get("config_version"),
                "schema_version": self.config.get("schema_version", "jianmo.intent.v1"),
                "region_context": bundle.region,
                "asset_count": parsed.assets_count,
            },
        )

    def _profile(self, building_type: str) -> dict[str, Any]:
        profiles = self.config.get("building_types", {})
        return dict(profiles.get(building_type) or profiles.get("residential") or {})

    def _infer_building_type(self, prompt: str, fallback: str | None) -> tuple[str, str]:
        if fallback:
            return fallback, "form_input"
        lowered = prompt.lower()
        if any(token in lowered for token in ("住宅", "宿舍", "公寓", "residential")):
            return "residential", "text_prompt"
        if any(token in lowered for token in ("办公", "office")):
            return "office", "text_prompt"
        return "residential", "template_default"

    def _infer_source_mode(self, bundle: SourceBundle, parsed: ParsedDrawingModel, prompt: str) -> str:
        lowered = prompt.lower()
        if bundle.source_mode_hint != "auto":
            return "cad_to_bim" if bundle.source_mode_hint == "cad_to_bim" else "text_only"
        if "无图纸" in prompt or "text_only" in lowered:
            return "text_only"
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
        *,
        value: int | None,
        inferred: int | None,
        default: int,
        field: str,
        source: str,
        source_ref: str,
        assumptions: list[Assumption],
        completion_trace: list[CompletionTraceItem],
        inferred_source: str = "text_prompt",
        inferred_source_type: str = "text_prompt",
        inferred_source_ref: str | None = None,
        inferred_confidence: float = 0.85,
    ) -> int:
        if value is not None:
            completion_trace.append(
                self._trace(
                    field=field,
                    value=int(value),
                    source="form_input",
                    source_type="form_input",
                    source_ref=f"form_fields.{field}",
                    confidence=1.0,
                )
            )
            return int(value)
        if inferred is not None:
            completion_trace.append(
                self._trace(
                    field=field,
                    value=int(inferred),
                    source=inferred_source,
                    source_type=inferred_source_type,
                    source_ref=inferred_source_ref or field,
                    confidence=inferred_confidence,
                )
            )
            return int(inferred)
        assumptions.append(Assumption(field=field, value=default, source=source, confidence=0.6))
        completion_trace.append(
            self._trace(
                field=field,
                value=default,
                source=source,
                source_type="template_default",
                source_ref=source_ref,
                confidence=0.6,
            )
        )
        return default

    def _resolve_float(
        self,
        *,
        value: float | None,
        inferred: float | None,
        default: float,
        field: str,
        source: str,
        source_ref: str,
        assumptions: list[Assumption],
        completion_trace: list[CompletionTraceItem],
    ) -> float:
        if value is not None:
            completion_trace.append(
                self._trace(
                    field=field,
                    value=float(value),
                    source="form_input",
                    source_type="form_input",
                    source_ref=f"form_fields.{field}",
                    confidence=1.0,
                )
            )
            return float(value)
        if inferred is not None:
            completion_trace.append(
                self._trace(
                    field=field,
                    value=float(inferred),
                    source="text_prompt",
                    source_type="text_prompt",
                    source_ref=field,
                    confidence=0.85,
                )
            )
            return float(inferred)
        assumptions.append(Assumption(field=field, value=default, source=source, confidence=0.6))
        completion_trace.append(
            self._trace(
                field=field,
                value=default,
                source=source,
                source_type="template_default",
                source_ref=source_ref,
                confidence=0.6,
            )
        )
        return default

    def _resolve_optional_float(
        self,
        *,
        value: float | None,
        inferred: float | None,
        field: str,
        completion_trace: list[CompletionTraceItem],
    ) -> float | None:
        if value is not None:
            completion_trace.append(
                self._trace(
                    field=field,
                    value=float(value),
                    source="form_input",
                    source_type="form_input",
                    source_ref=f"form_fields.{field}",
                    confidence=1.0,
                )
            )
            return float(value)
        if inferred is not None:
            completion_trace.append(
                self._trace(
                    field=field,
                    value=float(inferred),
                    source="text_prompt",
                    source_type="text_prompt",
                    source_ref=field,
                    confidence=0.85,
                )
            )
            return float(inferred)
        return None

    def _resolve_optional_int(
        self,
        *,
        value: int | None,
        inferred: int | None,
        field: str,
        completion_trace: list[CompletionTraceItem],
    ) -> int | None:
        if value is not None:
            completion_trace.append(
                self._trace(
                    field=field,
                    value=int(value),
                    source="form_input",
                    source_type="form_input",
                    source_ref=f"form_fields.{field}",
                    confidence=1.0,
                )
            )
            return int(value)
        if inferred is not None:
            completion_trace.append(
                self._trace(
                    field=field,
                    value=int(inferred),
                    source="text_prompt",
                    source_type="text_prompt",
                    source_ref=field,
                    confidence=0.85,
                )
            )
            return int(inferred)
        return None

    def _trace(
        self,
        *,
        field: str,
        value: Any,
        source: str,
        source_type: str,
        source_ref: str,
        confidence: float,
    ) -> CompletionTraceItem:
        return CompletionTraceItem(
            field=field,
            value=value,
            source=source,
            source_type=source_type,
            source_ref=source_ref,
            confidence=confidence,
        )


class StructuredIntentTransformer:
    def __init__(self, provider: IntentProvider | None = None) -> None:
        self.provider = provider or HeuristicStructuredIntentProvider()

    def transform(self, bundle: SourceBundle, parsed: ParsedDrawingModel) -> DesignIntent:
        structured = self.provider.build(bundle, parsed)
        return DesignIntent(
            project_id=bundle.project_id,
            request_id=bundle.request_id,
            version_id=bundle.version_id,
            source_mode=structured.source_mode,
            building_type=structured.building_type,
            site=structured.site,
            constraints=structured.constraints,
            program=structured.program,
            style=structured.style,
            deliverables=structured.deliverables,
            final_use=structured.final_use,
            missing_fields=structured.missing_fields,
            assumptions=structured.assumptions,
            completion_trace=structured.completion_trace,
            element_selector=structured.element_selector,
            model_patch=structured.model_patch,
            metadata={
                **structured.metadata,
                "schema_version": structured.schema_version,
            },
        )

    @staticmethod
    def structured_schema() -> dict[str, Any]:
        return StructuredIntentOutput.model_json_schema()
