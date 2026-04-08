import type {
  AIIntentParseResponse,
  AssetRecord,
  FeedbackPayload,
  ModelingRequestRecord,
  ProjectSummary,
  VersionSnapshot
} from "./types";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  const isFormData = typeof FormData !== "undefined" && init?.body instanceof FormData;

  if (!isFormData && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      headers
    });
  } catch {
    throw new Error("无法连接到本地建模后端（127.0.0.1:3000），请确认后端服务已启动。");
  }

  if (!response.ok) {
    const body = await response.text();
    if (response.status >= 500) {
      throw new Error(
        body && body !== "Internal Server Error"
          ? body
          : "建模后端当前不可用，请检查本地 3000 端口服务是否正在运行。"
      );
    }
    throw new Error(body || `Request failed with ${response.status}`);
  }

  return response.json() as Promise<T>;
}

export async function fetchHealth(): Promise<{ status: string }> {
  return request<{ status: string }>("/health");
}

export async function fetchProjects(): Promise<ProjectSummary[]> {
  return request<ProjectSummary[]>("/api/projects");
}

export async function createProject(input: {
  name: string;
  building_type: string;
  region: string;
}): Promise<ProjectSummary> {
  return request<ProjectSummary>("/api/projects", {
    method: "POST",
    body: JSON.stringify(input)
  });
}

export async function fetchAssets(projectId: string): Promise<AssetRecord[]> {
  return request<AssetRecord[]>(`/api/projects/${projectId}/assets`);
}

export async function uploadAsset(
  projectId: string,
  file: File,
  description = "Web upload asset"
): Promise<AssetRecord> {
  const body = new FormData();
  body.append("file", file);
  body.append("description", description);
  return request<AssetRecord>(`/api/projects/${projectId}/assets`, {
    method: "POST",
    body
  });
}

export async function createModelingRequest(
  projectId: string,
  input: {
    prompt: string;
    building_type: string;
    region: string;
    floors?: number;
    standard_floor_height_m?: number;
    first_floor_height_m?: number;
    site_area_sqm?: number;
    far?: number;
    asset_ids: string[];
  }
): Promise<ModelingRequestRecord> {
  return request<ModelingRequestRecord>(`/api/projects/${projectId}/requests`, {
    method: "POST",
    body: JSON.stringify(input)
  });
}

export async function runSavedRequest(
  projectId: string,
  requestId: string
): Promise<VersionSnapshot> {
  return request<VersionSnapshot>(`/api/projects/${projectId}/requests/${requestId}/run`, {
    method: "POST"
  });
}

export async function fetchVersions(projectId: string): Promise<VersionSnapshot[]> {
  return request<VersionSnapshot[]>(`/api/projects/${projectId}/versions`);
}

export async function fetchRequests(projectId: string): Promise<ModelingRequestRecord[]> {
  return request<ModelingRequestRecord[]>(`/api/projects/${projectId}/requests`);
}

export function buildArtifactUrl(
  projectId: string,
  versionId: string,
  artifactName: string
): string {
  return `/api/projects/${projectId}/versions/${versionId}/artifacts/${encodeURIComponent(
    artifactName
  )}`;
}

export async function sendFeedback(
  projectId: string,
  versionId: string,
  payload: FeedbackPayload
): Promise<{ feedback_id: string; received_at: string }> {
  return request<{ feedback_id: string; received_at: string }>(
    `/api/projects/${projectId}/versions/${versionId}/feedbacks`,
    {
      method: "POST",
      body: JSON.stringify(payload)
    }
  );
}

export async function parseIntent(
  projectId: string,
  input: {
    prompt: string;
    building_type?: string | null;
    region?: string | null;
    form_fields?: Record<string, unknown>;
    asset_ids?: string[];
  }
): Promise<AIIntentParseResponse> {
  return request<AIIntentParseResponse>(`/api/projects/${projectId}/intent/parse`, {
    method: "POST",
    body: JSON.stringify(input)
  });
}
