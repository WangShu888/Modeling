from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


SourceMode = Literal["cad_to_bim", "text_only", "element_batch_replace"]
RuleStatus = Literal["passed", "warning", "failed"]
ValidationStatus = Literal["passed", "warning", "failed"]
IssueSeverity = Literal["info", "warning", "error", "fatal"]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProjectCreateRequest(BaseModel):
    name: str
    building_type: str | None = None
    region: str | None = None


class ProjectSummary(BaseModel):
    project_id: str
    name: str
    building_type: str | None = None
    region: str | None = None
    created_at: str = Field(default_factory=utc_now_iso)
    latest_version_id: str | None = None


class AssetInput(BaseModel):
    filename: str
    media_type: str = "application/octet-stream"
    description: str | None = None
    path: str | None = Field(default=None, exclude=True)


class AssetRecord(AssetInput):
    asset_id: str
    project_id: str | None = None
    extension: str
    size_bytes: int = 0
    content_hash: str | None = None
    created_at: str = Field(default_factory=utc_now_iso)


class ModelingRequestBase(BaseModel):
    prompt: str
    building_type: str | None = None
    source_mode_hint: Literal["auto", "cad_to_bim", "text_only"] = "auto"
    region: str | None = None
    floors: int | None = None
    standard_floor_height_m: float | None = None
    first_floor_height_m: float | None = None
    site_area_sqm: float | None = None
    far: float | None = None
    units_per_floor: int | None = None
    assets: list[AssetInput] = Field(default_factory=list)
    output_formats: list[str] = Field(
        default_factory=lambda: ["ifc", "validation.json", "intent.json"]
    )
    metadata: dict[str, Any] = Field(default_factory=dict)


class ModelingRequestInput(ModelingRequestBase):
    assets: list[AssetInput] = Field(default_factory=list)


class ModelingRequestCreate(ModelingRequestBase):
    asset_ids: list[str] = Field(default_factory=list)


class ModelingRequestRecord(ModelingRequestCreate):
    project_id: str
    request_id: str
    created_at: str = Field(default_factory=utc_now_iso)
    latest_version_id: str | None = None


class SourceBundle(BaseModel):
    project_id: str
    request_id: str
    version_id: str
    prompt: str
    source_mode_hint: str
    building_type_hint: str | None = None
    region: str | None = None
    assets: list[AssetRecord] = Field(default_factory=list)
    form_fields: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now_iso)


class Point2D(BaseModel):
    x: float
    y: float


class BoundingBox2D(BaseModel):
    min_x: float
    min_y: float
    max_x: float
    max_y: float


class CoordinateReference(BaseModel):
    x: float = 0.0
    y: float = 0.0
    source: str = "default"
    asset_name: str | None = None
    confidence: float = 0.0


class LayerMapEntry(BaseModel):
    asset_name: str
    name: str
    semantic_role: str = "unknown"
    entity_count: int = 0
    entity_types: list[str] = Field(default_factory=list)


class GridAxis(BaseModel):
    asset_name: str
    label: str | None = None
    orientation: str = "unknown"
    coordinate: float | None = None
    layer: str | None = None
    source_ref: str | None = None
    start: Point2D | None = None
    end: Point2D | None = None
    confidence: float = 0.5


class DimensionEntityRecord(BaseModel):
    asset_name: str
    kind: str = "linear"
    text: str | None = None
    value: float | None = None
    unit: str | None = None
    layer: str | None = None
    bbox: BoundingBox2D | None = None
    source_ref: str | None = None


class TextAnnotationRecord(BaseModel):
    asset_name: str
    text: str
    semantic_tag: str = "generic"
    layer: str | None = None
    page_index: int | None = None
    bbox: BoundingBox2D | None = None
    source_ref: str | None = None


class DrawingEntityRecord(BaseModel):
    asset_name: str
    category: str
    layer: str | None = None
    label: str | None = None
    bbox: BoundingBox2D | None = None
    points: list[Point2D] = Field(default_factory=list)
    source_ref: str | None = None
    confidence: float = 0.5
    metadata: dict[str, Any] = Field(default_factory=dict)


class StoreyCandidateRecord(BaseModel):
    asset_name: str
    name: str
    source: str
    confidence: float = 0.5
    elevation_m: float | None = None
    bbox: BoundingBox2D | None = None
    source_ref: str | None = None


class DrawingFragmentRecord(BaseModel):
    fragment_id: str
    asset_name: str
    fragment_title: str | None = None
    fragment_role: str = "unknown"
    storey_key: str = "1F"
    bbox: BoundingBox2D | None = None
    source: str = "inferred"


class PdfAssetInfo(BaseModel):
    asset_name: str
    page_count: int = 0
    pdf_type: str = "unknown"
    vector_page_count: int = 0
    scanned_page_count: int = 0
    hybrid_page_count: int = 0
    image_page_count: int = 0
    ocr_attempted: bool = False
    ocr_available: bool = False


class PendingReviewItem(BaseModel):
    asset_name: str | None = None
    category: str
    reason: str
    source_ref: str | None = None
    severity: IssueSeverity = "warning"


class ParsedDrawingModel(BaseModel):
    assets_count: int
    asset_kinds: list[str] = Field(default_factory=list)
    units: str = "mm"
    origin: CoordinateReference = Field(default_factory=CoordinateReference)
    recognized_layers: list[str] = Field(default_factory=list)
    layer_map: list[LayerMapEntry] = Field(default_factory=list)
    grid_map: list[GridAxis] = Field(default_factory=list)
    grid_lines_detected: int = 0
    dimension_entities: int = 0
    dimension_details: list[DimensionEntityRecord] = Field(default_factory=list)
    text_annotations: list[str] = Field(default_factory=list)
    text_annotation_items: list[TextAnnotationRecord] = Field(default_factory=list)
    detected_entities: list[DrawingEntityRecord] = Field(default_factory=list)
    detected_entities_total: int = 0
    detected_entities_emitted: int = 0
    detected_entities_dropped: int = 0
    detected_entities_source_summary: dict[str, int] = Field(default_factory=dict)
    fragments: list[DrawingFragmentRecord] = Field(default_factory=list)
    storey_candidates: list[str] = Field(default_factory=list)
    storey_candidate_details: list[StoreyCandidateRecord] = Field(default_factory=list)
    storey_elevations_m: list[float] = Field(default_factory=list)
    north_angle: float = 0.0
    pdf_assets: list[PdfAssetInfo] = Field(default_factory=list)
    pdf_modes_detected: list[str] = Field(default_factory=list)
    site_boundary_detected: bool = False
    space_boundaries_detected: int = 0
    space_candidates_detected: int = 0
    pending_review: list[PendingReviewItem] = Field(default_factory=list)
    unresolved_entities: list[str] = Field(default_factory=list)
    entity_summary: dict[str, int] = Field(default_factory=dict)
    parser_analysis: dict[str, Any] = Field(default_factory=dict)


class MissingField(BaseModel):
    field: str
    reason: str
    critical: bool = False


class Assumption(BaseModel):
    field: str
    value: Any
    source: str
    confidence: float = 0.7


class CompletionTraceItem(BaseModel):
    field: str
    value: Any
    source: str
    source_type: str | None = None
    source_ref: str | None = None
    confidence: float | None = None


class SiteInfo(BaseModel):
    boundary_source: str = "unspecified"
    area_sqm: float | None = None
    north_angle: float = 0.0


class Constraints(BaseModel):
    floors: int
    standard_floor_height_m: float
    first_floor_height_m: float
    ruleset: str
    far: float | None = None


class ProgramInfo(BaseModel):
    spaces_from_drawings: bool = False
    units_per_floor: int | None = None
    core_type: str | None = None
    first_floor_spaces: list[str] = Field(default_factory=list)
    typical_floor_spaces: list[str] = Field(default_factory=list)


class StyleInfo(BaseModel):
    facade: str = "default"
    material_palette: list[str] = Field(default_factory=list)


class ElementSelector(BaseModel):
    ifc_type: str
    properties: dict[str, Any]


class ModelPatch(BaseModel):
    action_type: str
    target_family: str
    preserve: list[str] = Field(default_factory=list)
    scope: dict[str, Any] = Field(default_factory=dict)


class StructuredIntentOutput(BaseModel):
    schema_version: str = "jianmo.intent.v1"
    source_mode: SourceMode
    building_type: str
    site: SiteInfo
    constraints: Constraints
    program: ProgramInfo
    style: StyleInfo
    deliverables: list[str] = Field(default_factory=list)
    final_use: str = "revit_view"
    missing_fields: list[MissingField] = Field(default_factory=list)
    assumptions: list[Assumption] = Field(default_factory=list)
    completion_trace: list[CompletionTraceItem] = Field(default_factory=list)
    element_selector: ElementSelector | None = None
    model_patch: ModelPatch | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DesignIntent(BaseModel):
    project_id: str
    request_id: str
    version_id: str
    source_mode: SourceMode
    building_type: str
    site: SiteInfo
    constraints: Constraints
    program: ProgramInfo
    style: StyleInfo
    deliverables: list[str] = Field(default_factory=list)
    final_use: str = "revit_view"
    missing_fields: list[MissingField] = Field(default_factory=list)
    assumptions: list[Assumption] = Field(default_factory=list)
    completion_trace: list[CompletionTraceItem] = Field(default_factory=list)
    element_selector: ElementSelector | None = None
    model_patch: ModelPatch | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RuleIssue(BaseModel):
    code: str
    severity: IssueSeverity
    message: str
    target: str | None = None


class RuleCheckResult(BaseModel):
    status: RuleStatus
    issues: list[RuleIssue] = Field(default_factory=list)
    solver_constraints: dict[str, Any] = Field(default_factory=dict)
    ruleset_version: str | None = None
    applied_rules: list[str] = Field(default_factory=list)
    replay_token: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PlanStep(BaseModel):
    name: str
    module: str
    description: str
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)


class ModelingPlan(BaseModel):
    strategy: str
    can_continue: bool
    steps: list[PlanStep] = Field(default_factory=list)
    affected_modules: list[str] = Field(default_factory=list)
    regeneration_scope: str = "project"
    plan_id: str | None = None
    planner_version: str | None = None
    replay_token: str | None = None
    strategy_reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class BimSpace(BaseModel):
    space_id: str
    name: str
    category: str
    area_sqm: float


class BimElement(BaseModel):
    element_id: str
    guid: str
    ifc_type: str
    name: str
    family_name: str
    storey_id: str
    properties: dict[str, Any] = Field(default_factory=dict)


class BimStorey(BaseModel):
    storey_id: str
    name: str
    elevation_m: float
    spaces: list[BimSpace] = Field(default_factory=list)
    elements: list[BimElement] = Field(default_factory=list)


class BimSemanticModel(BaseModel):
    project_id: str
    version_id: str
    building_type: str
    storeys: list[BimStorey] = Field(default_factory=list)
    element_index: dict[str, int] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ValidationIssue(BaseModel):
    severity: IssueSeverity
    message: str
    target: str | None = None


FeedbackTopic = Literal["issue", "clarification", "improvement", "endorsement"]


class FeedbackCreateRequest(BaseModel):
    topic: FeedbackTopic
    comment: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class FeedbackReceipt(BaseModel):
    feedback_id: str
    received_at: str = Field(default_factory=utc_now_iso)


class ValidationReport(BaseModel):
    status: ValidationStatus
    issues: list[ValidationIssue] = Field(default_factory=list)
    affected_elements: list[str] = Field(default_factory=list)
    fix_suggestions: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExportArtifact(BaseModel):
    name: str
    path: str
    media_type: str


class ExportBundle(BaseModel):
    export_allowed: bool
    artifact_dir: str
    artifacts: list[ExportArtifact] = Field(default_factory=list)
    blocked_by: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class VersionSnapshot(BaseModel):
    project: ProjectSummary
    source_bundle: SourceBundle
    parsed_drawing: ParsedDrawingModel
    design_intent: DesignIntent
    rule_check: RuleCheckResult
    modeling_plan: ModelingPlan
    bim_model: BimSemanticModel
    validation: ValidationReport
    export_bundle: ExportBundle
    created_at: str = Field(default_factory=utc_now_iso)
