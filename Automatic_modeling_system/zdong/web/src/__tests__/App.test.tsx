import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../App";

const projects = [
  {
    project_id: "proj_001",
    name: "住宅方案自动建模",
    building_type: "residential",
    region: "CN-SH",
    created_at: "2026-04-02T06:54:32.304215+00:00",
    latest_version_id: "ver_001"
  }
];

const assets = [
  {
    asset_id: "asset_001",
    project_id: "proj_001",
    filename: "bldg.dwg",
    media_type: "application/dwg",
    description: "现场图纸",
    extension: "dwg",
    size_bytes: 1024,
    content_hash: null,
    created_at: "2026-04-02T07:00:00Z"
  }
];

const failedVersion = {
  project: projects[0],
  source_bundle: {
    request_id: "req_001",
    version_id: "ver_001",
    project_id: "proj_001",
    assets: []
  },
  parsed_drawing: {
    assets_count: 2,
    asset_kinds: ["dwg"],
    recognized_layers: ["LAYER1", "LAYER2"],
    unresolved_entities: [],
    storey_candidates: ["1F"]
  },
  design_intent: {
    source_mode: "cad_to_bim",
    building_type: "residential",
    constraints: {
      floors: 12,
      standard_floor_height_m: 3,
      first_floor_height_m: 3.3,
      ruleset: "zh_residential_v1",
      far: 2.5
    },
    site: {
      north_angle: 0,
      boundary_source: "cad",
      area_sqm: 8000
    },
    assumptions: [
      {
        field: "materials",
        value: "concrete",
        source: "default"
      }
    ],
    completion_trace: [],
    missing_fields: [
      {
        field: "north_angle",
        reason: "需要场地原点",
        critical: false
      }
    ]
  },
  rule_check: {
    status: "failed",
    issues: [
      {
        code: "rule_01",
        severity: "error",
        message: "Test rule violation"
      }
    ],
    solver_constraints: {}
  },
  modeling_plan: {
    strategy: "cad_to_bim",
    regeneration_scope: "building",
    can_continue: false
  },
  bim_model: {
    element_index: { Wall: 120 },
    metadata: {},
    storeys: [
      {
        name: "1F",
        elevation_m: 0,
        spaces: []
      }
    ]
  },
  validation: {
    status: "failed",
    issues: [],
    fix_suggestions: []
  },
  export_bundle: {
    export_allowed: false,
    artifact_dir: "/tmp/export",
    artifacts: [
      {
        name: "intent.json",
        path: "/tmp/export/intent.json",
        media_type: "application/json"
      }
    ],
    blocked_by: []
  }
};

const successfulVersion = {
  ...failedVersion,
  source_bundle: {
    ...failedVersion.source_bundle,
    request_id: "req_002",
    version_id: "ver_002"
  },
  rule_check: {
    ...failedVersion.rule_check,
    status: "success",
    issues: []
  },
  modeling_plan: {
    ...failedVersion.modeling_plan,
    can_continue: true
  },
  validation: {
    status: "success",
    issues: [],
    fix_suggestions: []
  },
  export_bundle: {
    ...failedVersion.export_bundle,
    export_allowed: true
  },
  design_intent: {
    ...failedVersion.design_intent,
    missing_fields: []
  }
};

const makeResponse = (payload: unknown) =>
  Promise.resolve(
    new Response(JSON.stringify(payload), {
      status: 200,
      headers: {
        "Content-Type": "application/json"
      }
    })
  );

let fetchMock: ReturnType<typeof vi.fn>;
function installDefaultFetchMock() {
  fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url =
      typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;

    if (url === "/api/projects" && !init?.method) {
      return makeResponse(projects);
    }
    if (url === "/health") {
      return makeResponse({ status: "ok" });
    }
    if (url.endsWith("/assets") && !init?.method) {
      return makeResponse(assets);
    }
    if (url.endsWith("/versions")) {
      return makeResponse([failedVersion, successfulVersion]);
    }
    if (url.endsWith("/feedbacks") && init?.method === "POST") {
      return makeResponse({
        feedback_id: "fb_001",
        received_at: "2026-04-02T08:00:00Z"
      });
    }

    return makeResponse({});
  });

  Object.defineProperty(globalThis, "fetch", {
    configurable: true,
    value: fetchMock
  });
}

describe("App", () => {
  beforeEach(() => {
    installDefaultFetchMock();
    Object.defineProperty(globalThis.navigator, "clipboard", {
      configurable: true,
      value: {
        writeText: async () => undefined
      }
    });
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("renders header and input controls", async () => {
    render(<App />);
    expect(await screen.findByRole("heading", { name: "自动建模工作台" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "发起建模" })).toBeInTheDocument();
    expect(screen.getByLabelText("项目名称")).toHaveValue("住宅方案自动建模");
    expect(screen.getByRole("button", { name: "开始自动建模" })).toBeInTheDocument();
    expect(
      screen.getByText("“开始自动建模”会自动建项目；“仅建项目”只用于提前建档。")
    ).toBeInTheDocument();

    await waitFor(() => {
      const calledWithProjects = fetchMock.mock.calls.some((call) => call[0] === "/api/projects");
      expect(calledWithProjects).toBe(true);
    });
  });

  it("shows version summary and export cards", async () => {
    const user = userEvent.setup();
    render(<App />);

    expect(await screen.findByRole("heading", { name: "结果审查" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "版本详情" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /规则/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("校验与阻塞项")).toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: /导出/ }));

    expect(screen.getByRole("tab", { name: /导出/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tabpanel", { name: /导出/ })).toBeInTheDocument();
    expect(await screen.findByText("intent.json")).toBeInTheDocument();
  });

  it("switches a failed version back to parse view when a successful version is selected", async () => {
    const user = userEvent.setup();
    render(<App />);

    expect(await screen.findByRole("tab", { name: /规则/ })).toHaveAttribute("aria-selected", "true");

    await user.click(screen.getAllByRole("button", { name: /ver_002/ })[1]);

    await waitFor(() => {
      expect(screen.getByRole("tab", { name: /解析/ })).toHaveAttribute("aria-selected", "true");
    });
    expect(screen.getByRole("tabpanel", { name: /解析/ })).toBeInTheDocument();
    expect(screen.getByText("识别图层")).toBeInTheDocument();
  });

  it("supports file selection and feedback submission", async () => {
    const user = userEvent.setup();
    render(<App />);

    const uploadInput = screen.getByLabelText("选择图纸文件");
    const file = new File(["dwg"], "tower.dwg", { type: "application/dwg" });

    await user.upload(uploadInput, file);

    expect(await screen.findByText("tower.dwg")).toBeInTheDocument();
    expect(
      screen.getByText((_, element) =>
        Boolean(element?.classList.contains("asset-type") && element.textContent === "待上传")
      )
    ).toBeInTheDocument();

    await user.click(await screen.findByRole("tab", { name: /导出/ }));
    expect(screen.getByRole("button", { name: "复制路径" })).toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: /反馈/ }));
    await user.click(screen.getByRole("button", { name: "发送反馈" }));
    expect(
      await screen.findByText("请先简要描述问题、澄清点或优化建议。")
    ).toBeInTheDocument();

    await user.type(screen.getByLabelText("审查说明"), "请补充北向信息并重新校验导出。");
    await user.click(screen.getByRole("button", { name: "发送反馈" }));

    expect(
      await screen.findByText("反馈已记录，下一轮建模会参考这条审查意见。")
    ).toBeInTheDocument();

    const feedbackRequest = fetchMock.mock.calls.find(
      (call) =>
        call[0] === "/api/projects/proj_001/versions/ver_001/feedbacks" &&
        call[1]?.method === "POST"
    );
    expect(feedbackRequest).toBeDefined();
    expect(JSON.parse(String(feedbackRequest?.[1]?.body))).toMatchObject({
      topic: "issue",
      comment: "请补充北向信息并重新校验导出。"
    });
  });

  it("uses only newly uploaded assets when starting a new modeling run", async () => {
    const user = userEvent.setup();
    const uploadedAsset = {
      asset_id: "asset_999",
      project_id: "proj_001",
      filename: "replacement.dxf",
      media_type: "image/vnd.dxf",
      description: "新图纸",
      extension: "dxf",
      size_bytes: 2048,
      content_hash: null,
      created_at: "2026-04-03T01:00:00Z"
    };
    const requestRecord = {
      request_id: "req_999",
      project_id: "proj_001",
      prompt: "依据上传DXF生成宿舍楼IFC",
      building_type: "residential",
      source_mode_hint: "auto",
      region: "CN-SH",
      floors: 12,
      standard_floor_height_m: 3,
      first_floor_height_m: 3.3,
      site_area_sqm: 8000,
      far: 2.5,
      units_per_floor: null,
      asset_ids: [uploadedAsset.asset_id],
      output_formats: ["ifc", "validation.json", "intent.json"],
      metadata: {},
      created_at: "2026-04-03T01:00:00Z",
      latest_version_id: null
    };
    const runVersion = {
      ...successfulVersion,
      source_bundle: {
        ...successfulVersion.source_bundle,
        request_id: requestRecord.request_id,
        version_id: "ver_999",
        assets: [uploadedAsset]
      }
    };

    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;

      if (url === "/api/projects" && !init?.method) {
        return makeResponse(projects);
      }
      if (url === "/health") {
        return makeResponse({ status: "ok" });
      }
      if (url.endsWith("/assets") && !init?.method) {
        return makeResponse(assets);
      }
      if (url.endsWith("/versions")) {
        return makeResponse([failedVersion, successfulVersion]);
      }
      if (url === "/api/projects/proj_001/assets" && init?.method === "POST") {
        return makeResponse(uploadedAsset);
      }
      if (url === "/api/projects/proj_001/requests" && init?.method === "POST") {
        return makeResponse(requestRecord);
      }
      if (url === "/api/projects/proj_001/requests/req_999/run" && init?.method === "POST") {
        return makeResponse(runVersion);
      }
      if (url.endsWith("/feedbacks") && init?.method === "POST") {
        return makeResponse({
          feedback_id: "fb_001",
          received_at: "2026-04-02T08:00:00Z"
        });
      }

      return makeResponse({});
    });

    render(<App />);

    const uploadInput = await screen.findByLabelText("选择图纸文件");
    const file = new File(["dxf"], "replacement.dxf", { type: "image/vnd.dxf" });
    await user.upload(uploadInput, file);

    await user.click(screen.getByRole("button", { name: "开始自动建模" }));

    await waitFor(() => {
      const createRequestCall = fetchMock.mock.calls.find(
        (call) => call[0] === "/api/projects/proj_001/requests" && call[1]?.method === "POST"
      );
      expect(createRequestCall).toBeDefined();
      expect(JSON.parse(String(createRequestCall?.[1]?.body))).toMatchObject({
        asset_ids: [uploadedAsset.asset_id]
      });
    });
  });

  it("shows a clear backend unavailable message when health check fails", async () => {
    const user = userEvent.setup();

    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;

      if (url === "/api/projects" && !init?.method) {
        return makeResponse(projects);
      }
      if (url.endsWith("/assets") && !init?.method) {
        return makeResponse(assets);
      }
      if (url.endsWith("/versions")) {
        return makeResponse([failedVersion, successfulVersion]);
      }
      if (url === "/health") {
        return Promise.resolve(new Response("Internal Server Error", { status: 500 }));
      }

      return makeResponse({});
    });

    render(<App />);

    await user.click(await screen.findByRole("button", { name: "开始自动建模" }));

    expect(
      await screen.findByText("建模后端当前不可用，请检查本地 3000 端口服务是否正在运行。")
    ).toBeInTheDocument();
  });
});
