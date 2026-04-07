export type IssueSeverity = "info" | "warning" | "error" | "fatal";

export interface ProjectSummary {
  project_id: string;
  name: string;
  building_type?: string | null;
  region?: string | null;
  created_at: string;
  latest_version_id?: string | null;
}

export interface AssetRecord {
  asset_id: string;
  project_id?: string | null;
  filename: string;
  media_type: string;
  description?: string | null;
  extension: string;
  size_bytes: number;
  content_hash?: string | null;
  created_at: string;
}

export interface ModelingRequestRecord {
  request_id: string;
  project_id: string;
  prompt: string;
  building_type?: string | null;
  source_mode_hint: "auto" | "cad_to_bim" | "text_only";
  region?: string | null;
  floors?: number | null;
  standard_floor_height_m?: number | null;
  first_floor_height_m?: number | null;
  site_area_sqm?: number | null;
  far?: number | null;
  units_per_floor?: number | null;
  asset_ids: string[];
  output_formats: string[];
  metadata: Record<string, unknown>;
  created_at: string;
  latest_version_id?: string | null;
}

export interface RuleIssue {
  code: string;
  severity: IssueSeverity;
  message: string;
  target?: string | null;
}

export interface ValidationIssue {
  severity: IssueSeverity;
  message: string;
  target?: string | null;
}

export type FeedbackTopic = "issue" | "clarification" | "improvement" | "endorsement";

export interface FeedbackPayload {
  topic: FeedbackTopic;
  comment: string;
  metadata?: Record<string, unknown>;
}

export interface ExportArtifact {
  name: string;
  path: string;
  media_type: string;
}

export interface VersionSnapshot {
  project: ProjectSummary;
  source_bundle: {
    request_id: string;
    version_id: string;
    project_id: string;
    assets: Array<{
      asset_id: string;
      filename: string;
      media_type: string;
      description?: string | null;
      extension: string;
    }>;
  };
  parsed_drawing: {
    assets_count: number;
    asset_kinds: string[];
    recognized_layers: string[];
    unresolved_entities: string[];
    storey_candidates: string[];
  };
  design_intent: {
    source_mode: string;
    building_type: string;
    constraints: {
      floors: number;
      standard_floor_height_m: number;
      first_floor_height_m: number;
      ruleset: string;
      far?: number | null;
    };
    site: {
      area_sqm?: number | null;
      north_angle: number;
      boundary_source: string;
    };
    assumptions: Array<{
      field: string;
      value: unknown;
      source: string;
      confidence?: number;
    }>;
    completion_trace: Array<{
      field: string;
      value: unknown;
      source: string;
      source_type?: string | null;
      source_ref?: string | null;
      confidence?: number | null;
    }>;
    missing_fields: Array<{
      field: string;
      reason: string;
      critical: boolean;
    }>;
    model_patch?: {
      action_type: string;
      target_family: string;
      scope?: Record<string, unknown>;
    } | null;
    metadata?: Record<string, unknown>;
  };
  rule_check: {
    status: string;
    issues: RuleIssue[];
    solver_constraints: Record<string, unknown>;
    ruleset_version?: string | null;
    applied_rules?: string[];
    replay_token?: string | null;
    metadata?: Record<string, unknown>;
  };
  modeling_plan: {
    strategy: string;
    regeneration_scope: string;
    can_continue: boolean;
    plan_id?: string | null;
    planner_version?: string | null;
    replay_token?: string | null;
    strategy_reason?: string | null;
    metadata?: Record<string, unknown>;
  };
  bim_model: {
    element_index: Record<string, number>;
    metadata: Record<string, unknown>;
    storeys: Array<{
      name: string;
      elevation_m: number;
      spaces: Array<{ name: string; category: string; area_sqm: number }>;
    }>;
  };
  validation: {
    status: string;
    issues: ValidationIssue[];
    fix_suggestions: string[];
    metadata?: Record<string, unknown>;
  };
  export_bundle: {
    export_allowed: boolean;
    artifact_dir: string;
    artifacts: ExportArtifact[];
    blocked_by: string[];
    metadata?: Record<string, unknown>;
  };
}

export interface ModelingFormState {
  projectName: string;
  buildingType: "residential" | "office";
  region: string;
  prompt: string;
  floors: string;
  standardFloorHeight: string;
  firstFloorHeight: string;
  siteArea: string;
  far: string;
}
