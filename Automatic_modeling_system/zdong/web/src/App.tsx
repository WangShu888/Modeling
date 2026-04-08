import {
  startTransition,
  useEffect,
  useMemo,
  useState,
  type FormEvent,
  type KeyboardEvent
} from "react";

import {
  buildArtifactUrl,
  createModelingRequest,
  createProject,
  fetchAssets,
  fetchHealth,
  fetchProjects,
  fetchRequests,
  fetchVersions,
  runSavedRequest,
  sendFeedback,
  uploadAsset
} from "./api";
import {
  formatIssueMessage,
  formatIssueSeverity,
  formatPipelineStatus
} from "./issueCopy";
import type {
  AssetRecord,
  FeedbackTopic,
  ModelingFormState,
  ProjectSummary,
  VersionSnapshot
} from "./types";

const initialForm: ModelingFormState = {
  projectName: "住宅方案自动建模",
  buildingType: "residential",
  region: "CN-SH",
  prompt:
    "在 8000 平方米地块上生成一栋 12 层住宅楼，两梯四户，标准层层高 3.0m。将所有 800x1200 的窗替换为落地窗，输出 IFC。",
  floors: "12",
  standardFloorHeight: "3.0",
  firstFloorHeight: "3.3",
  siteArea: "8000",
  far: "2.5"
};

const buildingTypeLabels: Record<ModelingFormState["buildingType"], string> = {
  residential: "住宅",
  office: "办公"
};

const feedbackTopics: { topic: FeedbackTopic; label: string; description: string }[] = [
  {
    topic: "issue",
    label: "问题",
    description: "建模结果有偏差、规则违规或导出失败"
  },
  {
    topic: "clarification",
    label: "澄清",
    description: "需要补充场地、需求或假设信息"
  },
  {
    topic: "improvement",
    label: "优化",
    description: "希望升级建模策略或输出内容"
  },
  {
    topic: "endorsement",
    label: "认可",
    description: "输出符合期待，提供正向信号"
  }
];

type DetailTab = "parse" | "rules" | "bim" | "export" | "feedback";

const detailTabs: { id: DetailTab; label: string; description: string }[] = [
  { id: "parse", label: "解析", description: "查看输入资料、识别层与假设补全" },
  { id: "rules", label: "规则", description: "集中审查校验问题与阻塞项" },
  { id: "bim", label: "BIM", description: "查看楼层、空间和构件规模" },
  { id: "export", label: "导出", description: "管理 IFC 和验证产物" },
  { id: "feedback", label: "反馈", description: "把审查意见同步给建模代理" }
];

function statusTone(status?: string | null) {
  if (status === "failed" || status === "blocked") {
    return "tone-danger";
  }
  if (status === "pending" || status === "running") {
    return "tone-warning";
  }
  return "tone-success";
}

function severityTone(severity?: string | null) {
  if (severity === "fatal" || severity === "error") {
    return "tone-danger";
  }
  if (severity === "warning") {
    return "tone-warning";
  }
  return "tone-info";
}

function formatDateTime(value?: string | null) {
  if (!value) {
    return "尚无时间记录";
  }

  try {
    return new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit"
    }).format(new Date(value));
  } catch {
    return value;
  }
}

function formatNumber(value?: number | null) {
  if (value == null || Number.isNaN(value)) {
    return "0";
  }
  return new Intl.NumberFormat("zh-CN").format(value);
}

function formatBytes(bytes: number) {
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KB`;
  }
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatConfidence(confidence?: number | null) {
  if (confidence == null) {
    return "待确认";
  }
  return `${Math.round(confidence * 100)}%`;
}

function buildPromptTags(form: ModelingFormState) {
  return [
    buildingTypeLabels[form.buildingType],
    form.floors ? `${form.floors} 层` : null,
    form.siteArea ? `${form.siteArea} ㎡` : null,
    form.far ? `FAR ${form.far}` : null,
    form.prompt.includes("IFC") ? "输出 IFC" : null
  ].filter((tag): tag is string => Boolean(tag));
}

function buildNextStep(version: VersionSnapshot | null) {
  if (!version) {
    return "先建立项目并上传资料，系统会在第一次运行后生成版本审查面板。";
  }

  if (version.validation.status === "failed" || version.validation.status === "blocked") {
    return version.validation.fix_suggestions[0] ?? "先进入规则页处理阻塞项，再重新触发建模。";
  }

  if (!version.export_bundle.export_allowed) {
    return "导出尚未放行，请先核对校验结果与导出受限原因。";
  }

  if (version.design_intent.missing_fields.length > 0) {
    return "补全缺失字段后可得到更稳定的建模结果。";
  }

  return "当前版本可继续迭代，建议先下载产物或发起下一轮模型优化。";
}

function pickHighlightedIssue(version: VersionSnapshot | null) {
  if (!version) {
    return null;
  }

  const validationIssue = version.validation.issues.find(
    (issue) => issue.severity === "fatal" || issue.severity === "error"
  );
  if (validationIssue) {
    return validationIssue;
  }

  return version.validation.issues[0] ?? version.rule_check.issues[0] ?? null;
}

function assetTypeLabel(asset: AssetRecord) {
  return asset.extension ? asset.extension.toUpperCase() : asset.media_type;
}

function buildingTypeLabel(value?: string | null) {
  if (!value) {
    return "未分类";
  }
  if (value === "office") {
    return buildingTypeLabels.office;
  }
  if (value === "residential") {
    return buildingTypeLabels.residential;
  }
  return "未分类";
}

export default function App() {
  const [form, setForm] = useState<ModelingFormState>(initialForm);
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState<string | null>(null);
  const [assets, setAssets] = useState<AssetRecord[]>([]);
  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
  const [versions, setVersions] = useState<VersionSnapshot[]>([]);
  const [selectedVersion, setSelectedVersion] = useState<VersionSnapshot | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [feedbackTopic, setFeedbackTopic] = useState<FeedbackTopic>("issue");
  const [feedbackComment, setFeedbackComment] = useState("");
  const [feedbackStatus, setFeedbackStatus] = useState<string | null>(null);
  const [sendingFeedback, setSendingFeedback] = useState(false);
  const [copiedPath, setCopiedPath] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<DetailTab>("parse");

  const activeProject = useMemo(
    () => projects.find((project) => project.project_id === selectedProjectId) ?? null,
    [projects, selectedProjectId]
  );
  const promptPreview = useMemo(() => form.prompt.slice(0, 160), [form.prompt]);
  const promptTags = useMemo(() => buildPromptTags(form), [form]);

  const highlightedIssue = useMemo(() => pickHighlightedIssue(selectedVersion), [selectedVersion]);
  const versionIssues = selectedVersion?.validation.issues ?? [];
  const ruleIssues = selectedVersion?.rule_check.issues ?? [];
  const exportArtifacts = selectedVersion?.export_bundle.artifacts ?? [];
  const elementEntries = useMemo(
    () =>
      selectedVersion
        ? Object.entries(selectedVersion.bim_model.element_index).sort((left, right) => right[1] - left[1])
        : [],
    [selectedVersion]
  );
  const totalElements = useMemo(
    () => elementEntries.reduce((sum, [, value]) => sum + value, 0),
    [elementEntries]
  );
  const totalSpaces = useMemo(
    () =>
      selectedVersion?.bim_model.storeys.reduce(
        (sum, storey) => sum + storey.spaces.length,
        0
      ) ?? 0,
    [selectedVersion]
  );
  const shownLayers = (selectedVersion?.parsed_drawing.recognized_layers ?? []).slice(0, 10);
  const hiddenLayerCount =
    (selectedVersion?.parsed_drawing.recognized_layers.length ?? 0) - shownLayers.length;
  const canCopyPaths = typeof navigator !== "undefined" && Boolean(navigator.clipboard);
  const nextStep = useMemo(() => buildNextStep(selectedVersion), [selectedVersion]);

  useEffect(() => {
    void refreshProjects().catch((loadError) => {
      setError(loadError instanceof Error ? loadError.message : "加载项目列表失败");
    });
  }, []);

  useEffect(() => {
    if (!selectedProjectId) {
      startTransition(() => {
        setAssets([]);
        setVersions([]);
      });
      return;
    }
    void refreshProjectData(selectedProjectId).catch((loadError) => {
      setError(loadError instanceof Error ? loadError.message : "加载项目数据失败");
    });
  }, [selectedProjectId]);

  useEffect(() => {
    setFeedbackStatus(null);
    setCopiedPath(null);
    setActiveTab(
      selectedVersion &&
        (selectedVersion.validation.status === "failed" ||
          selectedVersion.validation.status === "blocked")
        ? "rules"
        : "parse"
    );
  }, [selectedVersion?.source_bundle.version_id]);

  async function refreshProjects(preferredProjectId?: string) {
    const items = await fetchProjects();
    startTransition(() => {
      setProjects(items);
      const nextProjectId = preferredProjectId ?? selectedProjectId ?? items[0]?.project_id ?? null;
      if (nextProjectId) {
        setSelectedProjectId(nextProjectId);
      }
    });
  }

  async function refreshProjectData(projectId: string) {
    const [assetItems, versionItems, requestItems] = await Promise.all([
      fetchAssets(projectId),
      fetchVersions(projectId),
      fetchRequests(projectId)
    ]);

    startTransition(() => {
      setAssets(assetItems);
      setVersions(versionItems);
      if (!selectedVersion || selectedVersion.project.project_id !== projectId) {
        setSelectedVersion(versionItems[0] ?? null);
      }

      // Populate form from the selected project's latest data
      const project = projects.find(p => p.project_id === projectId)
                   ?? versionItems[0]?.project
                   ?? null;
      const latestVersion = versionItems[0] ?? null;
      const latestRequest = requestItems[0] ?? null;

      if (project) {
        setForm({
          projectName: project.name,
          buildingType: (project.building_type ?? "residential") as ModelingFormState["buildingType"],
          region: project.region ?? "CN-SH",
          floors: latestVersion
            ? String(latestVersion.design_intent.constraints.floors)
            : latestRequest?.floors != null ? String(latestRequest.floors) : initialForm.floors,
          standardFloorHeight: latestVersion
            ? String(latestVersion.design_intent.constraints.standard_floor_height_m)
            : latestRequest?.standard_floor_height_m != null ? String(latestRequest.standard_floor_height_m) : initialForm.standardFloorHeight,
          firstFloorHeight: latestVersion
            ? String(latestVersion.design_intent.constraints.first_floor_height_m)
            : latestRequest?.first_floor_height_m != null ? String(latestRequest.first_floor_height_m) : initialForm.firstFloorHeight,
          siteArea: latestVersion?.design_intent.site.area_sqm != null
            ? String(latestVersion.design_intent.site.area_sqm)
            : latestRequest?.site_area_sqm != null ? String(latestRequest.site_area_sqm) : initialForm.siteArea,
          far: latestVersion?.design_intent.constraints.far != null
            ? String(latestVersion.design_intent.constraints.far)
            : latestRequest?.far != null ? String(latestRequest.far) : initialForm.far,
          prompt: latestRequest?.prompt ?? initialForm.prompt,
        });
      }
    });
  }

  async function ensureProject() {
    if (selectedProjectId) {
      return selectedProjectId;
    }

    const project = await createAndActivateProject();
    await refreshProjects(project.project_id);
    return project.project_id;
  }

  async function createAndActivateProject() {
    return createProject({
      name: form.projectName,
      building_type: form.buildingType,
      region: form.region
    });
  }

  async function handleCreateProject() {
    setLoading(true);
    setError(null);

    try {
      const project = await createAndActivateProject();
      await refreshProjects(project.project_id);
    } catch (submitError) {
      setError(submitError instanceof Error ? submitError.message : "创建项目失败");
    } finally {
      setLoading(false);
    }
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setLoading(true);
    setError(null);

    try {
      await fetchHealth();
      const projectId = await ensureProject();

      const uploadedAssets = await Promise.all(
        pendingFiles.map((file) => uploadAsset(projectId, file))
      );
      const requestAssets = uploadedAssets.length > 0 ? uploadedAssets : assets;
      const request = await createModelingRequest(projectId, {
        prompt: form.prompt,
        building_type: form.buildingType,
        region: form.region,
        floors: form.floors ? Number(form.floors) : undefined,
        standard_floor_height_m: form.standardFloorHeight
          ? Number(form.standardFloorHeight)
          : undefined,
        first_floor_height_m: form.firstFloorHeight ? Number(form.firstFloorHeight) : undefined,
        site_area_sqm: form.siteArea ? Number(form.siteArea) : undefined,
        far: form.far ? Number(form.far) : undefined,
        asset_ids: requestAssets.map((asset) => asset.asset_id)
      });

      const version = await runSavedRequest(projectId, request.request_id);
      await refreshProjects(projectId);
      await refreshProjectData(projectId);

      startTransition(() => {
        setSelectedVersion(version);
        setPendingFiles([]);
      });
    } catch (submitError) {
      setError(submitError instanceof Error ? submitError.message : "请求失败");
    } finally {
      setLoading(false);
    }
  }

  async function handleSendFeedback() {
    if (!selectedVersion) {
      setFeedbackStatus("还没有可反馈的版本，请先生成一次建模版本。");
      return;
    }
    if (!feedbackComment.trim()) {
      setFeedbackStatus("请先简要描述问题、澄清点或优化建议。");
      return;
    }

    setSendingFeedback(true);
    setFeedbackStatus(null);

    try {
      await sendFeedback(
        selectedVersion.project.project_id,
        selectedVersion.source_bundle.version_id,
        {
          topic: feedbackTopic,
          comment: feedbackComment.trim()
        }
      );
      setFeedbackComment("");
      setFeedbackStatus("反馈已记录，下一轮建模会参考这条审查意见。");
    } catch (submitError) {
      setFeedbackStatus(
        submitError instanceof Error
          ? submitError.message
          : "提交反馈失败，请稍后再试"
      );
    } finally {
      setSendingFeedback(false);
    }
  }

  async function copyArtifactPath(path: string) {
    if (!canCopyPaths) {
      return;
    }

    try {
      await navigator.clipboard.writeText(path);
      setCopiedPath(path);
    } catch {
      setCopiedPath(null);
    }
  }

  const headerProjectName = activeProject?.name ?? form.projectName;
  const headerVersionLabel = selectedVersion
    ? `${selectedVersion.source_bundle.version_id} · ${formatPipelineStatus(selectedVersion.validation.status)}`
    : "等待首个版本";

  function focusDetailTab(tabId: DetailTab) {
    const nextButton = document.getElementById(`detail-tab-${tabId}`);
    nextButton?.focus();
  }

  function handleTabKeyDown(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    let nextIndex = index;

    if (event.key === "ArrowRight") {
      nextIndex = (index + 1) % detailTabs.length;
    } else if (event.key === "ArrowLeft") {
      nextIndex = (index - 1 + detailTabs.length) % detailTabs.length;
    } else if (event.key === "Home") {
      nextIndex = 0;
    } else if (event.key === "End") {
      nextIndex = detailTabs.length - 1;
    } else {
      return;
    }

    event.preventDefault();
    const nextTab = detailTabs[nextIndex];
    setActiveTab(nextTab.id);
    focusDetailTab(nextTab.id);
  }

  return (
    <div className="workbench-shell">
      <header className="surface header-shell">
          <div className="header-main">
          <div className="header-title">
            <p className="eyebrow">自动建模</p>
            <h1>自动建模工作台</h1>
            <p className="header-copy">
              从图纸、约束和文本需求生成 BIM 版本，并在同一页面完成规则审查、导出判断与反馈闭环。
            </p>
          </div>

          <div className="header-metrics" aria-label="工作台状态">
            <article className="metric-tile">
              <span className="metric-label">当前项目</span>
              <strong>{headerProjectName}</strong>
              <span className="metric-note">
                {activeProject
                  ? `${buildingTypeLabel(activeProject.building_type)} · ${activeProject.region ?? "未设置地区"}`
                  : "尚未写入项目"}
              </span>
            </article>
            <article className="metric-tile">
              <span className="metric-label">最近版本</span>
              <strong>{headerVersionLabel}</strong>
              <span className="metric-note">
                {selectedVersion
                  ? `请求 ${selectedVersion.source_bundle.request_id}`
                  : "提交一次建模后自动生成"}
              </span>
            </article>
            <article className="metric-tile">
              <span className="metric-label">导出状态</span>
              <strong>
                {selectedVersion
                  ? selectedVersion.export_bundle.export_allowed
                    ? "允许导出"
                    : "导出受限"
                  : "未生成"}
              </strong>
              <span className="metric-note">
                {selectedVersion ? `${exportArtifacts.length} 个文件` : "等待产物"}
              </span>
            </article>
          </div>
        </div>

        <div className="header-footer">
          <article className="header-callout">
            <span className="micro-label">当前输入焦点</span>
            <p>{promptPreview || "输入需求后，这里会显示建模目标摘要。"}</p>
            <div className="token-row">
              {promptTags.map((tag) => (
                <span key={tag} className="token">
                  {tag}
                </span>
              ))}
            </div>
          </article>

          <article className="header-callout">
            <span className="micro-label">下一步建议</span>
            <p>{nextStep}</p>
            <span className={`status-badge ${statusTone(selectedVersion?.validation.status ?? null)}`}>
              {selectedVersion
                ? formatPipelineStatus(selectedVersion.validation.status)
                : "等待首个版本"}
            </span>
          </article>
        </div>
      </header>

      <div className="workbench-body">
        <aside className="rail">
          <section className="surface rail-card rail-card-strong">
            <div className="card-heading">
              <div>
                <p className="eyebrow">项目概览</p>
                <h2>项目上下文</h2>
              </div>
              <span className={`status-badge ${statusTone(selectedVersion?.validation.status ?? null)}`}>
                {selectedVersion ? formatPipelineStatus(selectedVersion.validation.status) : "准备中"}
              </span>
            </div>

            <div className="rail-summary-grid">
              <article className="summary-chip">
                <span>资料数</span>
                <strong>{assets.length}</strong>
              </article>
              <article className="summary-chip">
                <span>版本数</span>
                <strong>{versions.length}</strong>
              </article>
              <article className="summary-chip">
                <span>待上传</span>
                <strong>{pendingFiles.length}</strong>
              </article>
              <article className="summary-chip">
                <span>问题数</span>
                <strong>{versionIssues.length + ruleIssues.length}</strong>
              </article>
            </div>

            <dl className="definition-list">
              <div>
                <dt>项目名称</dt>
                <dd>{activeProject?.name ?? "尚未创建项目"}</dd>
              </div>
              <div>
                <dt>建筑类型</dt>
                <dd>{buildingTypeLabel(activeProject?.building_type ?? form.buildingType)}</dd>
              </div>
              <div>
                <dt>地区</dt>
                <dd>{activeProject?.region ?? form.region}</dd>
              </div>
              <div>
                <dt>项目建立时间</dt>
                <dd>{activeProject ? formatDateTime(activeProject.created_at) : "等待写入"}</dd>
              </div>
            </dl>
          </section>

          <section className="surface rail-card">
            <div className="card-heading">
              <div>
                <p className="eyebrow">版本</p>
                <h2>最近版本</h2>
              </div>
            </div>

            <div className="compact-list">
              {versions.slice(0, 4).map((version) => {
                const active =
                  selectedVersion?.source_bundle.version_id === version.source_bundle.version_id;
                return (
                  <button
                    key={version.source_bundle.version_id}
                    type="button"
                    className={`compact-card ${active ? "compact-card-active" : ""}`}
                    onClick={() => {
                      setSelectedVersion(version);
                      setFeedbackStatus(null);
                    }}
                  >
                    <div>
                      <strong>{version.source_bundle.version_id}</strong>
                      <span>{version.modeling_plan.strategy}</span>
                    </div>
                    <span className={`status-badge ${statusTone(version.validation.status)}`}>
                      {formatPipelineStatus(version.validation.status)}
                    </span>
                  </button>
                );
              })}
              {versions.length === 0 ? <p className="empty-note">提交后，这里会显示最近版本。</p> : null}
            </div>
          </section>

          <section className="surface rail-card">
            <div className="card-heading">
              <div>
                <p className="eyebrow">项目</p>
                <h2>最近项目</h2>
              </div>
            </div>

            <div className="compact-list">
              {projects.slice(0, 5).map((project) => {
                const active = project.project_id === selectedProjectId;
                return (
                  <button
                    key={project.project_id}
                    type="button"
                    className={`compact-card project-card ${active ? "compact-card-active" : ""}`}
                    onClick={() => {
                      setSelectedProjectId(project.project_id);
                      setError(null);
                    }}
                  >
                    <div>
                      <strong>{project.name}</strong>
                      <span>{project.project_id}</span>
                    </div>
                    <span className="compact-meta">
                      {project.latest_version_id ?? "暂无版本"}
                    </span>
                  </button>
                );
              })}
              {projects.length === 0 ? <p className="empty-note">尚未创建项目。</p> : null}
            </div>
          </section>
        </aside>

        <main className="board">
          <section className="surface request-surface">
            <div className="section-banner">
              <div>
                <p className="eyebrow">建模输入</p>
                <h2>发起建模</h2>
                <p className="section-copy">
                  先确定项目和约束，再绑定资料文件，最后用文本把建模意图说清楚。
                </p>
              </div>
              <span className={`status-badge ${loading ? "tone-warning" : "tone-success"}`}>
                {loading ? "处理中" : activeProject ? "可复用项目" : "待创建项目"}
              </span>
            </div>

            <form className="request-stack" onSubmit={handleSubmit}>
              <section className="panel-block">
                <div className="block-heading">
                  <div>
                    <p className="eyebrow">Step 01</p>
                    <h3>项目基础信息</h3>
                  </div>
                  <p>建立项目上下文，定义类型和地区，以便后续策略与规则集继承。</p>
                </div>

                <div className="field-grid">
                  <label className="field">
                    <span className="field-label">项目名称</span>
                    <input
                      value={form.projectName}
                      onChange={(event) => setForm({ ...form, projectName: event.target.value })}
                    />
                  </label>

                  <label className="field">
                    <span className="field-label">建筑类型</span>
                    <select
                      value={form.buildingType}
                      onChange={(event) =>
                        setForm({
                          ...form,
                          buildingType: event.target.value as ModelingFormState["buildingType"]
                        })
                      }
                    >
                      <option value="residential">住宅</option>
                      <option value="office">办公</option>
                    </select>
                  </label>

                  <label className="field">
                    <span className="field-label">地区</span>
                    <input
                      value={form.region}
                      onChange={(event) => setForm({ ...form, region: event.target.value })}
                    />
                  </label>

                  <article className="info-panel">
                    <span className="field-label">当前项目摘要</span>
                    <strong>{activeProject?.name ?? "还未创建项目"}</strong>
                    <p>
                      {activeProject
                        ? `${assets.length} 份资料已绑定，${versions.length} 个版本可复盘。`
                        : "提交建模时会自动建立项目；如果你只想先建档，可单独创建项目。"}
                    </p>
                  </article>
                </div>
              </section>

              <section className="panel-block">
                <div className="block-heading">
                  <div>
                    <p className="eyebrow">Step 02</p>
                    <h3>体量与约束</h3>
                  </div>
                  <p>这些参数会直接进入建模约束与规则检查，是首轮体量判断的基础。</p>
                </div>

                <div className="field-grid">
                  <label className="field">
                    <span className="field-label">层数</span>
                    <div className="input-wrap">
                      <input
                        value={form.floors}
                        onChange={(event) => setForm({ ...form, floors: event.target.value })}
                      />
                      <span className="input-suffix">层</span>
                    </div>
                  </label>

                  <label className="field">
                    <span className="field-label">标准层层高</span>
                    <div className="input-wrap">
                      <input
                        value={form.standardFloorHeight}
                        onChange={(event) =>
                          setForm({ ...form, standardFloorHeight: event.target.value })
                        }
                      />
                      <span className="input-suffix">m</span>
                    </div>
                  </label>

                  <label className="field">
                    <span className="field-label">首层层高</span>
                    <div className="input-wrap">
                      <input
                        value={form.firstFloorHeight}
                        onChange={(event) =>
                          setForm({ ...form, firstFloorHeight: event.target.value })
                        }
                      />
                      <span className="input-suffix">m</span>
                    </div>
                  </label>

                  <label className="field">
                    <span className="field-label">用地面积</span>
                    <div className="input-wrap">
                      <input
                        value={form.siteArea}
                        onChange={(event) => setForm({ ...form, siteArea: event.target.value })}
                      />
                      <span className="input-suffix">㎡</span>
                    </div>
                  </label>

                  <label className="field">
                    <span className="field-label">容积率</span>
                    <div className="input-wrap">
                      <input
                        value={form.far}
                        onChange={(event) => setForm({ ...form, far: event.target.value })}
                      />
                      <span className="input-suffix">FAR</span>
                    </div>
                  </label>
                </div>
              </section>

              <section className="panel-block">
                <div className="block-heading">
                  <div>
                    <p className="eyebrow">Step 03</p>
                    <h3>图纸与资料</h3>
                  </div>
                  <p>优先绑定原始图纸与已有模型，让系统具备更稳定的 CAD / PDF 驱动策略。</p>
                </div>

                <label className="upload-zone">
                  <div>
                    <strong>拖入图纸或点击选择文件</strong>
                    <p>支持 DWG / DXF / PDF / IFC。选择新文件后，本次建模只会使用这批新图纸。</p>
                  </div>
                  <span className="upload-action">选择文件</span>
                  <input
                    className="upload-input"
                    type="file"
                    multiple
                    aria-label="选择图纸文件"
                    onChange={(event) => setPendingFiles(Array.from(event.target.files ?? []))}
                  />
                </label>

                <div className="asset-grid">
                  {pendingFiles.map((file) => (
                    <article key={`${file.name}-${file.size}`} className="asset-card asset-card-pending">
                      <span className="asset-type">待上传</span>
                      <strong>{file.name}</strong>
                      <p>{formatBytes(file.size)}</p>
                    </article>
                  ))}
                  {assets.map((asset) => (
                    <article key={asset.asset_id} className="asset-card">
                      <span className="asset-type">{assetTypeLabel(asset)}</span>
                      <strong>{asset.filename}</strong>
                      <p>{asset.asset_id}</p>
                    </article>
                  ))}
                  {pendingFiles.length === 0 && assets.length === 0 ? (
                    <p className="empty-note">当前项目还没有绑定资料文件。</p>
                  ) : null}
                </div>
              </section>

              <section className="panel-block">
                <div className="block-heading">
                  <div>
                    <p className="eyebrow">Step 04</p>
                    <h3>建模意图</h3>
                  </div>
                  <p>用自然语言定义目标建筑、立面、户型和导出要求，系统会自动抽取结构化意图。</p>
                </div>

                <label className="field field-full">
                  <span className="field-label">文本需求</span>
                  <textarea
                    rows={6}
                    value={form.prompt}
                    onChange={(event) => setForm({ ...form, prompt: event.target.value })}
                  />
                </label>

                <div className="token-row">
                  {promptTags.map((tag) => (
                    <span key={tag} className="token">
                      {tag}
                    </span>
                  ))}
                </div>
              </section>

              <div className="action-bar">
                <button type="button" className="button button-ghost" onClick={() => void handleCreateProject()} disabled={loading}>
                  仅建项目
                </button>
                <button type="submit" className="button button-primary" disabled={loading}>
                  {loading ? "建模执行中..." : "开始自动建模"}
                </button>
                <button
                  type="button"
                  className="button button-ghost"
                  onClick={() => {
                    setForm(initialForm);
                    setPendingFiles([]);
                    setError(null);
                  }}
                >
                  重置表单
                </button>
              </div>
              <p className="helper-note">“开始自动建模”会自动建项目；“仅建项目”只用于提前建档。</p>
            </form>

            {error ? <p className="alert alert-error" aria-live="polite">{error}</p> : null}
          </section>

          <section className="surface review-surface">
            <div className="section-banner">
              <div>
                <p className="eyebrow">结果审查</p>
                <h2>结果审查</h2>
                <p className="section-copy">
                  先判断当前版本是否可交付，再深入查看解析、规则、BIM 和导出细节。
                </p>
              </div>
              <div className="badge-group">
                {selectedVersion ? (
                  <span className={`status-badge ${statusTone(selectedVersion.validation.status)}`}>
                    {formatPipelineStatus(selectedVersion.validation.status)}
                  </span>
                ) : null}
                <span className="status-badge tone-neutral">
                  {selectedVersion ? selectedVersion.source_bundle.version_id : "等待版本"}
                </span>
              </div>
            </div>

            {selectedVersion ? (
              <>
                <section className="overview-hero">
                  <div className="overview-copy">
                    <p className="eyebrow">当前版本</p>
                    <h3>{selectedVersion.source_bundle.version_id}</h3>
                    <p>
                      请求 {selectedVersion.source_bundle.request_id} · 策略 {selectedVersion.modeling_plan.strategy} ·
                      项目建立于 {formatDateTime(selectedVersion.project.created_at)}
                    </p>
                  </div>

                  <div className="overview-summary">
                    <article className="summary-stat">
                      <span>状态</span>
                      <strong>{formatPipelineStatus(selectedVersion.validation.status)}</strong>
                    </article>
                    <article className="summary-stat">
                      <span>导出</span>
                      <strong>{selectedVersion.export_bundle.export_allowed ? "允许" : "受限"}</strong>
                    </article>
                    <article className="summary-stat">
                      <span>问题</span>
                      <strong>{versionIssues.length + ruleIssues.length}</strong>
                    </article>
                  </div>

                  <div className="hero-alert">
                    <span className="micro-label">当前判断</span>
                    <p>
                      {highlightedIssue
                        ? formatIssueMessage(highlightedIssue.message)
                        : "当前版本没有发现需要优先拦截的问题。"}
                    </p>
                  </div>
                </section>

                <div className="metric-grid metric-grid-wide">
                  <article className="metric-card metric-card-primary">
                    <span className="metric-label">版本状态</span>
                    <strong>{formatPipelineStatus(selectedVersion.validation.status)}</strong>
                    <p>{nextStep}</p>
                  </article>
                  <article className="metric-card">
                    <span className="metric-label">规则问题</span>
                    <strong>{formatNumber(ruleIssues.length)}</strong>
                    <p>{formatPipelineStatus(selectedVersion.rule_check.status)}</p>
                  </article>
                  <article className="metric-card">
                    <span className="metric-label">BIM 元素</span>
                    <strong>{formatNumber(totalElements)}</strong>
                    <p>{selectedVersion.bim_model.storeys.length} 个楼层 / {totalSpaces} 个空间</p>
                  </article>
                  <article className="metric-card">
                    <span className="metric-label">导出产物</span>
                    <strong>{formatNumber(exportArtifacts.length)}</strong>
                    <p>{selectedVersion.export_bundle.export_allowed ? "已放行" : "需要复核"}</p>
                  </article>
                </div>

                <section className="detail-section">
                  <div className="subsection-head">
                    <div>
                      <p className="eyebrow">版本历史</p>
                      <h3>版本切换</h3>
                    </div>
                  </div>

                  <div className="version-grid">
                    {versions.slice(0, 5).map((version) => {
                      const active =
                        selectedVersion.source_bundle.version_id === version.source_bundle.version_id;
                      return (
                        <button
                          key={version.source_bundle.version_id}
                          type="button"
                          className={`version-card ${active ? "version-card-active" : ""}`}
                          onClick={() => {
                            setSelectedVersion(version);
                            setFeedbackStatus(null);
                          }}
                        >
                          <div>
                            <strong>{version.source_bundle.version_id}</strong>
                            <span>{version.modeling_plan.strategy}</span>
                          </div>
                          <span className={`status-badge ${statusTone(version.validation.status)}`}>
                            {formatPipelineStatus(version.validation.status)}
                          </span>
                        </button>
                      );
                    })}
                  </div>
                </section>

                <section className="detail-section">
                  <div className="subsection-head">
                    <div>
                      <p className="eyebrow">版本明细</p>
                      <h3>版本详情</h3>
                    </div>
                  </div>

                  <nav className="tab-row" aria-label="结果详情标签页" role="tablist">
                    {detailTabs.map((tab, index) => (
                      <button
                        key={tab.id}
                        id={`detail-tab-${tab.id}`}
                        type="button"
                        role="tab"
                        aria-selected={activeTab === tab.id}
                        aria-controls={`detail-panel-${tab.id}`}
                        tabIndex={activeTab === tab.id ? 0 : -1}
                        className={`tab-button ${activeTab === tab.id ? "tab-button-active" : ""}`}
                        onClick={() => setActiveTab(tab.id)}
                        onKeyDown={(event) => handleTabKeyDown(event, index)}
                      >
                        <strong>{tab.label}</strong>
                        <span>{tab.description}</span>
                      </button>
                    ))}
                  </nav>

                  {activeTab === "parse" ? (
                    <div
                      className="detail-panel"
                      id="detail-panel-parse"
                      role="tabpanel"
                      aria-labelledby="detail-tab-parse"
                      tabIndex={0}
                    >
                      <div className="metric-grid">
                        <article className="metric-card">
                          <span className="metric-label">输入模式</span>
                          <strong>{selectedVersion.design_intent.source_mode}</strong>
                          <p>{selectedVersion.parsed_drawing.assets_count} 份图纸资产</p>
                        </article>
                        <article className="metric-card">
                          <span className="metric-label">已识别图层</span>
                          <strong>{formatNumber(selectedVersion.parsed_drawing.recognized_layers.length)}</strong>
                          <p>{selectedVersion.parsed_drawing.storey_candidates.length} 个楼层候选</p>
                        </article>
                        <article className="metric-card">
                          <span className="metric-label">约束规则集</span>
                          <strong>{selectedVersion.design_intent.constraints.ruleset}</strong>
                          <p>{selectedVersion.design_intent.constraints.floors} 层 · FAR {selectedVersion.design_intent.constraints.far ?? "--"}</p>
                        </article>
                      </div>

                      <section className="detail-card">
                        <div className="subsection-head">
                          <h4>识别图层</h4>
                        </div>
                        <div className="token-row">
                          {shownLayers.map((layer) => (
                            <span key={layer} className="token token-quiet">
                              {layer}
                            </span>
                          ))}
                          {hiddenLayerCount > 0 ? (
                            <span className="token token-quiet">+{hiddenLayerCount} 个图层</span>
                          ) : null}
                        </div>
                      </section>

                      <section className="detail-card">
                        <div className="subsection-head">
                          <h4>假设与待确认</h4>
                        </div>
                        <div className="info-grid">
                          {selectedVersion.design_intent.assumptions.map((assumption) => (
                            <article key={`${assumption.field}-${String(assumption.value)}`} className="info-card">
                              <span className="metric-label">{assumption.field}</span>
                              <strong>{String(assumption.value)}</strong>
                              <p>
                                来源 {assumption.source} · 置信度 {formatConfidence(assumption.confidence)}
                              </p>
                            </article>
                          ))}
                          {selectedVersion.design_intent.missing_fields.map((field) => (
                            <article key={field.field} className="info-card info-card-warning">
                              <span className="metric-label">待补充字段</span>
                              <strong>{field.field}</strong>
                              <p>{field.reason}</p>
                            </article>
                          ))}
                          {selectedVersion.design_intent.assumptions.length === 0 &&
                          selectedVersion.design_intent.missing_fields.length === 0 ? (
                            <p className="empty-note">当前请求没有额外假设或待确认字段。</p>
                          ) : null}
                        </div>
                      </section>
                    </div>
                  ) : null}

                  {activeTab === "rules" ? (
                    <div
                      className="detail-panel"
                      id="detail-panel-rules"
                      role="tabpanel"
                      aria-labelledby="detail-tab-rules"
                      tabIndex={0}
                    >
                      <section className="detail-card">
                        <div className="subsection-head">
                          <h4>校验与阻塞项</h4>
                          <span className={`status-badge ${statusTone(selectedVersion.validation.status)}`}>
                            {formatPipelineStatus(selectedVersion.validation.status)}
                          </span>
                        </div>
                        <div className="issue-list">
                          {versionIssues.map((issue, index) => (
                            <article key={`${issue.message}-${index}`} className="issue-card">
                              <div className="issue-head">
                                <span className={`status-badge ${severityTone(issue.severity)}`}>
                                  {formatIssueSeverity(issue.severity)}
                                </span>
                                <span className="issue-target">{issue.target ?? "全局"}</span>
                              </div>
                              <strong>{formatIssueMessage(issue.message)}</strong>
                            </article>
                          ))}
                          {versionIssues.length === 0 ? (
                            <p className="empty-note">当前没有校验阻塞项。</p>
                          ) : null}
                        </div>
                      </section>

                      <section className="detail-card">
                        <div className="subsection-head">
                          <h4>规则检查</h4>
                          <span className={`status-badge ${statusTone(selectedVersion.rule_check.status)}`}>
                            {formatPipelineStatus(selectedVersion.rule_check.status)}
                          </span>
                        </div>
                        <div className="issue-list">
                          {ruleIssues.map((issue) => (
                            <article key={`${issue.code}-${issue.message}`} className="issue-card">
                              <div className="issue-head">
                                <span className={`status-badge ${severityTone(issue.severity)}`}>
                                  {formatIssueSeverity(issue.severity)}
                                </span>
                                <span className="issue-target">{issue.code}</span>
                              </div>
                              <strong>{formatIssueMessage(issue.message)}</strong>
                            </article>
                          ))}
                          {ruleIssues.length === 0 ? (
                            <p className="empty-note">规则检查没有发现异常。</p>
                          ) : null}
                        </div>
                      </section>

                      <section className="detail-card">
                        <div className="subsection-head">
                          <h4>建议动作</h4>
                        </div>
                        <div className="token-row">
                          {selectedVersion.validation.fix_suggestions.map((suggestion) => (
                            <span key={suggestion} className="token token-warning">
                              {suggestion}
                            </span>
                          ))}
                          {selectedVersion.validation.fix_suggestions.length === 0 ? (
                            <p className="empty-note">当前没有系统建议动作。</p>
                          ) : null}
                        </div>
                      </section>
                    </div>
                  ) : null}

                  {activeTab === "bim" ? (
                    <div
                      className="detail-panel"
                      id="detail-panel-bim"
                      role="tabpanel"
                      aria-labelledby="detail-tab-bim"
                      tabIndex={0}
                    >
                      <section className="detail-card">
                        <div className="subsection-head">
                          <h4>构件分布</h4>
                        </div>
                        <div className="element-list">
                          {elementEntries.slice(0, 8).map(([key, value]) => (
                            <article key={key} className="element-row">
                              <div>
                                <strong>{key}</strong>
                                <span>{((value / Math.max(totalElements, 1)) * 100).toFixed(1)}%</span>
                              </div>
                              <div className="bar-track">
                                <span style={{ width: `${(value / Math.max(elementEntries[0]?.[1] ?? 1, 1)) * 100}%` }} />
                              </div>
                              <b>{formatNumber(value)}</b>
                            </article>
                          ))}
                        </div>
                      </section>

                      <section className="detail-card">
                        <div className="subsection-head">
                          <h4>楼层与空间</h4>
                        </div>
                        <div className="storey-grid">
                          {selectedVersion.bim_model.storeys.map((storey) => (
                            <article key={storey.name} className="storey-card">
                              <div className="storey-head">
                                <strong>{storey.name}</strong>
                                <span>{storey.elevation_m} m</span>
                              </div>
                              <p>{storey.spaces.length} 个空间</p>
                              <div className="token-row">
                                {storey.spaces.slice(0, 4).map((space) => (
                                  <span key={space.name} className="token token-quiet">
                                    {space.name} · {space.area_sqm}㎡
                                  </span>
                                ))}
                              </div>
                            </article>
                          ))}
                        </div>
                      </section>
                    </div>
                  ) : null}

                  {activeTab === "export" ? (
                    <div
                      className="detail-panel"
                      id="detail-panel-export"
                      role="tabpanel"
                      aria-labelledby="detail-tab-export"
                      tabIndex={0}
                    >
                      <section className="detail-card">
                        <div className="subsection-head">
                          <h4>导出总览</h4>
                          <span
                            className={`status-badge ${
                              selectedVersion.export_bundle.export_allowed ? "tone-success" : "tone-danger"
                            }`}
                          >
                            {selectedVersion.export_bundle.export_allowed ? "已放行" : "导出受限"}
                          </span>
                        </div>

                        {selectedVersion.export_bundle.blocked_by.length > 0 ? (
                          <div className="token-row">
                            {selectedVersion.export_bundle.blocked_by.map((reason) => (
                              <span key={reason} className="token token-warning">
                                {reason}
                              </span>
                            ))}
                          </div>
                        ) : null}

                        <div className="artifact-grid">
                          {exportArtifacts.map((artifact) => (
                            <article className="artifact-card" key={artifact.path}>
                              <div className="artifact-head">
                                <div>
                                  <strong>{artifact.name}</strong>
                                  <p>{artifact.media_type}</p>
                                </div>
                                <span className="status-badge tone-neutral">文件</span>
                              </div>

                              <p className="artifact-path">{artifact.path}</p>

                              <div className="artifact-actions">
                                <a
                                  className="button button-secondary"
                                  href={buildArtifactUrl(
                                    selectedVersion.project.project_id,
                                    selectedVersion.source_bundle.version_id,
                                    artifact.name
                                  )}
                                  target="_blank"
                                  rel="noreferrer"
                                >
                                  下载 / 预览
                                </a>
                                {canCopyPaths ? (
                                  <button
                                    type="button"
                                    className="button button-ghost"
                                    onClick={() => void copyArtifactPath(artifact.path)}
                                  >
                                    复制路径
                                  </button>
                                ) : null}
                              </div>

                              {copiedPath === artifact.path ? (
                                <p className="helper-note">路径已复制</p>
                              ) : null}
                            </article>
                          ))}
                        </div>

                        {exportArtifacts.length === 0 ? (
                          <p className="empty-note">当前版本还没有生成可导出的文件。</p>
                        ) : null}
                      </section>
                    </div>
                  ) : null}

                  {activeTab === "feedback" ? (
                    <div
                      className="detail-panel"
                      id="detail-panel-feedback"
                      role="tabpanel"
                      aria-labelledby="detail-tab-feedback"
                      tabIndex={0}
                    >
                      <section className="detail-card">
                        <div className="subsection-head">
                          <h4>提交审查意见</h4>
                          <span className="status-badge tone-info">同步给建模代理</span>
                        </div>

                        <p className="helper-note">
                          当前版本 {selectedVersion.source_bundle.version_id} ·
                          {formatPipelineStatus(selectedVersion.validation.status)}
                        </p>

                        <div className="topic-grid">
                          {feedbackTopics.map((topicEntry) => (
                            <button
                              type="button"
                              key={topicEntry.topic}
                              className={`topic-card ${
                                feedbackTopic === topicEntry.topic ? "topic-card-active" : ""
                              }`}
                              onClick={() => setFeedbackTopic(topicEntry.topic)}
                            >
                              <strong>{topicEntry.label}</strong>
                              <span>{topicEntry.description}</span>
                            </button>
                          ))}
                        </div>

                        <label className="field field-full">
                          <span className="field-label">审查说明</span>
                          <textarea
                            rows={5}
                            placeholder="说明阻塞点、补充信息或下一轮优化要求。"
                            value={feedbackComment}
                            onChange={(event) => setFeedbackComment(event.target.value)}
                          />
                        </label>

                        <div className="action-bar">
                          <button type="button" className="button button-primary" onClick={() => void handleSendFeedback()} disabled={sendingFeedback}>
                            {sendingFeedback ? "发送中..." : "发送反馈"}
                          </button>
                          <button
                            type="button"
                            className="button button-ghost"
                            onClick={() => {
                              setFeedbackComment("");
                              setFeedbackStatus(null);
                            }}
                          >
                            清空
                          </button>
                        </div>

                        {feedbackStatus ? <p className="alert alert-info" aria-live="polite">{feedbackStatus}</p> : null}
                      </section>
                    </div>
                  ) : null}
                </section>
              </>
            ) : (
              <section className="empty-state">
                <p className="eyebrow">Review Pending</p>
                <h3>还没有可审查的版本</h3>
                <p>
                  创建项目、绑定图纸并执行一次自动建模后，这里会切换为版本审查界面，展示规则、BIM 和导出结果。
                </p>
              </section>
            )}
          </section>
        </main>
      </div>
    </div>
  );
}
