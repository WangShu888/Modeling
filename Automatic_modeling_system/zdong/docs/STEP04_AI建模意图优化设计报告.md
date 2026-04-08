# STEP04 建模意图优化设计报告

版本：`v1.0`
日期：`2026-04-08`
状态：待评审

---

## 1. 概述

### 1.1 优化目标

将 STEP04「建模意图」模块从当前的正则表达式解析升级为 AI 驱动的智能理解与转化系统，实现以下核心能力：

1. **自然语言理解**：用户输入自由文本，AI 自动提取建筑参数、约束条件和修改意图
2. **智能补充**：AI 基于建筑知识库和上下文，自动补全缺失但非关键的建模参数
3. **结构化转化**：将理解后的意图转化为标准化的建模指令（DesignIntent DSL）
4. **指令融合**：将文本指令与图纸解析信息合并，形成完整的建模指令集
5. **指令下发**：将融合后的指令传输到 BIM 引擎执行建模

### 1.2 当前状态分析

| 维度 | 当前实现 | 目标状态 |
|------|---------|---------|
| 文本解析 | 正则表达式匹配（`_extract_int`, `_extract_float`） | AI 大模型语义理解 |
| 参数提取 | 固定 pattern（如 `(\d+)\s*层`） | 多轮对话 + 上下文推断 |
| 参数补充 | 硬编码模板默认值（`intent_defaults.json`） | AI 基于知识库动态补全 |
| 修改指令 | 仅支持窗替换（`_extract_model_patch_from_prompt`） | 支持全类型构件增删改 |
| 与图纸融合 | 简单优先级覆盖 | AI 理解冲突后智能合并 |
| 用户交互 | 单次文本框提交 | 对话式澄清 + 可编辑确认 |

### 1.3 现有代码落点

- 前端输入：`web/src/App.tsx` — Step 04 建模意图 textarea
- 后端解析：`app/intent_service.py` — `HeuristicStructuredIntentProvider`
- 数据模型：`app/models.py` — `DesignIntent`, `StructuredIntentOutput`
- 配置模板：`app/config/intent_defaults.json`
- 编排入口：`app/pipeline.py` — `ModelingPipeline._run_pipeline()`
- BIM 执行：`app/pipeline.py` — `BimEngine.build()`

---

## 2. 功能设计

### 2.1 整体架构

```
用户文本输入
    │
    ▼
┌─────────────────────┐
│  STEP04-A 文本预处理  │  中文数字归一化、单位统一、拼写修正
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│  STEP04-B AI 意图解析 │  LLM 语义理解 → 结构化参数提取
└────────┬────────────┘
         │
         ▼
┌──────────��──────────┐
│  STEP04-C 智能补充    │  知识库 + 图纸上下文 → 参数补全
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│  STEP04-D 冲突融合    │  文本指令 ∩ 图纸信息 → 统一 DesignIntent
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│  STEP04-E 指令下发    │  DesignIntent → RuleEngine → ModelingPlan → BimEngine
└─────────────────────┘
```

### 2.2 STEP04-A 文本预处理

**职责**：将原始用户输入规范化为 AI 可消费的标准格式。

**处理逻辑**：

1. **中文数字归一化**：复用现有 `_normalize_text()`，将"十二层"→"12层"
2. **单位归一化**：将"米"、"m"、"公尺"统一为"m"；将"平方米"、"㎡"、"平方"统一为"㎡"
3. **冗余信息清理**：去除重复空格、标点规范化
4. **上下文拼接**：将表单字段（层数、层高、用地面积等）拼接到 prompt 上下文中

**输入**：`SourceBundle.prompt` + `SourceBundle.form_fields`
**输出**：标准化后的 prompt + 结构化 form_fields

**实现位置**：`app/intent_service.py` — 增强 `_normalize_text()`

### 2.3 STEP04-B AI 意图解析（核心）

**职责**：使用大语言模型将自然语言转化为结构化参数。

#### 2.3.1 AI 调用架构

```python
class AIIntentProvider:
    """基于 LLM 的意图解析器，替代 HeuristicStructuredIntentProvider"""

    def __init__(self, llm_client, config):
        self.llm = llm_client          # LLM API 客户端
        self.config = config            # intent_defaults.json 配置
        self.schema = StructuredIntentOutput.model_json_schema()

    def build(self, bundle, parsed) -> StructuredIntentOutput:
        # 1. 构建 system prompt（含 JSON Schema 约束）
        # 2. 构建 user prompt（含文本、表单、图纸摘要）
        # 3. 调用 LLM 获取结构化输出
        # 4. 校验输出合规性
        # 5. 回退到 Heuristic 解析器（如果 LLM 失败）
        ...
```

#### 2.3.2 Prompt 设计

**System Prompt 核心要求**：

```
你是一个建筑建模意图解析器。你的任务是将用户的自然语言需求转化为结构化的建模指令。

严格遵循以下 JSON Schema 输出：
{schema}

规则：
1. 只输出 JSON，不输出任何解释文本
2. 明确区分"用户显式要求"、"AI推断"和"模板默认值"
3. 每个字段必须记录 source（来源）和 confidence（置信度 0-1）
4. 缺失但非关键的字段用合理默认值补全，并标记为 assumption
5. 缺失且关键的字段标记为 missing_field
6. 建筑类型必须是 residential 或 office 之一
7. source_mode 根据是否有图纸资产自动判断
```

**User Prompt 模板**：

```
## 用户文本需求
{prompt}

## 表单参数
- 建筑类型: {building_type}
- 地区: {region}
- 层数: {floors}
- 标准层层高: {standard_floor_height_m}m
- 首层层高: {first_floor_height_m}m
- 用地面积: {site_area_sqm}㎡
- 容积率: {far}

## 图纸上下文
- 已上传图纸: {assets_count} 份
- 已识别图层: {recognized_layers}
- 已识别楼层候选: {storey_candidates}
- 已识别构件: 墙体{wall_count} / 门{door_count} / 窗{window_count} / 空间{space_count}
- 用地面积(图纸推断): {parsed_site_area}㎡

## 建筑类型模板
{building_type_profile}

请根据以上信息生成结构化建模意图。
```

#### 2.3.3 输出校验

LLM 输出后必须经过以下校验：

1. **Schema 校验**：Pydantic 模型验证，确保字段完整且类型正确
2. **业务校验**：楼层 ≥ 1、层高 ≥ 2.8m、建筑类型合法
3. **来源标记校验**：每个字段都有 source 和 confidence
4. **回退机制**：如果 LLM 输出无法解析，回退到 `HeuristicStructuredIntentProvider`

### 2.4 STEP04-C 智能补充

**职责**：AI 基于建筑知识自动补全缺失参数。

#### 2.4.1 补全策略

按以下优先级逐层补充：

| 优先级 | 来源 | 示例 | 置信度范围 |
|--------|------|------|-----------|
| P0 | 用户显式输入 | "12层住宅楼" → floors=12 | 1.0 |
| P1 | 图纸提取信息 | 图纸中识别出6层标高 → floors=6 | 0.85 |
| P2 | AI 从文本推断 | "高层住宅" → floors≥18 | 0.7 |
| P3 | 建筑类型模板默认 | 住宅默认层高3.0m | 0.6 |
| P4 | AI 知识库推断 | 现代风格默认幕墙比例 | 0.5 |

#### 2.4.2 知识库结构

扩展现有 `intent_defaults.json`，增加 AI 可查询的知识条目：

```json
{
  "knowledge_entries": {
    "residential_high_rise": {
      "conditions": {"floors": ">=18", "building_type": "residential"},
      "defaults": {
        "core_type": "double_elevator_plus_staircase",
        "units_per_floor": [4, 6, 8],
        "typical_unit_area_sqm": [70, 90, 120, 140],
        "standard_floor_height_m": 3.0,
        "window_to_wall_ratio": [0.25, 0.35]
      }
    },
    "modern_facade_style": {
      "conditions": {"style": "modern"},
      "defaults": {
        "material_palette": ["glass", "aluminum", "stone"],
        "facade_pattern": "horizontal_band",
        "balcony_type": "recessed"
      }
    }
  }
}
```

### 2.5 STEP04-D 冲突融合

**职责**：当文本指令与图纸信息冲突时，智能合并。

#### 2.5.1 融合规则

```python
class IntentFusionService:
    """融合文本意图与图纸解析结果"""

    def fuse(self, text_intent, drawing_model, bundle) -> StructuredIntentOutput:
        # 1. 识别冲突项
        conflicts = self._detect_conflicts(text_intent, drawing_model)

        # 2. 按策略解决冲突
        for conflict in conflicts:
            resolution = self._resolve_conflict(conflict)
            # 用户显式要求 > 图纸信息 > AI推断 > 模板默认
            ...

        # 3. 生成融合结果
        fused = self._merge(text_intent, drawing_model)

        # 4. 记录所有融合决策到 completion_trace
        ...
        return fused
```

#### 2.5.2 冲突处理策略

| 冲突类型 | 策略 | 示例 |
|---------|------|------|
| 楼层数不一致 | 用户文本优先，图纸为参考 | 用户说12层，图纸只有1层平面 → 用12层，复制标准层 |
| 层高不一致 | 用户文本优先 | 用户要求3.6m，图纸标注3.0m → 用3.6m |
| 用地面积不一致 | 图纸优先（更准确） | 用户说8000㎡，图纸测量7200㎡ → 用7200㎡ |
| 构件类型冲突 | 文本修改指令优先 | 用户要求替换窗型 → 在图纸基础上执行替换 |
| 建筑类型不一致 | 用户文本优先 | 用户说"办公楼"，图纸标题"宿舍楼" → 用办公楼 |

### 2.6 STEP04-E 指令下发

**职责**：将融合后的 DesignIntent 下发到 BIM 引擎。

现有 `ModelingPipeline._run_pipeline()` 已实现此链路：

```
DesignIntent → ConfigurableRuleEngine → ConfigurableModelingPlanner
    → ModelingPlan → BimEngine.build() → BimSemanticModel
```

**优化点**：

1. **增量指令支持**：新增 `text_command` 类型策略，支持"仅文本修改，不重新解析图纸"
2. **指令预览**：在执行前向用户展示将要执行的操作列表
3. **局部重生成**：当文本指令仅涉及局部修改时，跳过全量重建

---

## 3. 前端交互设计

### 3.1 当前 STEP04 界面

当前界面只有一个 textarea 和 token 标签展示：

```tsx
// Step 04 建模意图 — 当前实现
<textarea rows={6} value={form.prompt} onChange={...} />
<div className="token-row">{promptTags.map(...)}</div>
```

### 3.2 优化后交互流程

```
┌───────────────────────────────────────────────┐
│  Step 04 建模意图                              │
│                                               │
│  ┌─────────────────────────────────────────┐  │
│  │ 文本需求输入                              │  │
│  │ ┌─────────────────────────────────────┐ │  │
│  │ │ [textarea]                           │ │  │
│  │ │ 在8000平米地块上生成12层住宅楼...      │ │  │
│  │ └─────────────────────────────────────┘ │  │
│  │                        [AI 解析] 按钮    │  │
│  └─────────────────────────────────────────┘  │
│                                               │
│  ┌── AI 解析结果（可编辑确认）─────────────┐  │
│  │                                         │  │
│  │ ┌─ 已提取参数 ─────────────────────┐    │  │
│  │ │ 建筑类型: 住宅    来源: 文本 ✓    │    │  │
│  │ │ 楼层数: 12        来源: 文本 ✓    │    │  │
│  │ │ 层高: 3.0m        来源: 默认 ⚠    │    │  │
│  │ │ 户数/层: 4户      来源: 推断 ⚠    │    │  │
│  │ │ 用地面积: 8000㎡  来源: 文本 ✓    │    │  │
│  │ └──────────────────────────────────┘    │  │
│  │                                         │  │
│  ��� ┌─ AI 补充说明 ────────────────────┐    │  │
│  │ │ → 推断为两梯四户标准层布局          │    │  │
│  │ │ → 现代风格默认采用玻璃+金属立面    │    │  │
│  │ │ → 首层层高建议3.3m（住宅标准）     │    │  │
│  │ └──────────────────────────────────┘    │  │
│  │                                         │  │
│  │ ┌─ 待确认项 ───────────────────────┐    │  │
│  │ │ ⚠ 地区规则集未指定，使用国标默认    │    │  │
│  │ │ ⚠ 窗墙比未指定，使用住宅默认0.30   │    │  │
│  │ └──────────────────────────────────┘    │  │
│  │                                         │  │
│  │            [确认并建模] [重新编辑]        │  │
│  └─────────────────────────────────────────┘  │
└───────────────────────────────────────────────┘
```

### 3.3 新增 API 端点

```python
# AI 意图预解析（不执行建模，仅返回解析结果）
POST /api/projects/{project_id}/intent/parse
Request:
{
    "prompt": "在8000平米地块上生成12层住宅楼...",
    "building_type": "residential",
    "form_fields": { "floors": 12, ... },
    "assets": [...]  // 可选，已有图纸资产
}
Response:
{
    "structured_intent": { ... },    // StructuredIntentOutput
    "parsed_summary": "...",         // AI 生成的自然语言摘要
    "conflicts": [...],              // 与图纸/表单的冲突项
    "clarification_questions": [...]  // 需要用户确认的问题
}
```

---

## 4. 数据模型扩展

### 4.1 新增模型

```python
class AIIntentParseRequest(BaseModel):
    """AI 意图解析请求"""
    prompt: str
    building_type: str | None = None
    region: str | None = None
    form_fields: dict[str, Any] = Field(default_factory=dict)
    asset_ids: list[str] = Field(default_factory=list)

class ClarificationQuestion(BaseModel):
    """澄清问题"""
    field: str
    question: str
    suggested_answers: list[str] = Field(default_factory=list)
    confidence: float = 0.5

class IntentConflict(BaseModel):
    """意图冲突项"""
    field: str
    text_value: Any
    drawing_value: Any
    resolution: str  # "use_text" | "use_drawing" | "ask_user"
    reason: str

class AIIntentParseResponse(BaseModel):
    """AI 意图解析响应"""
    structured_intent: StructuredIntentOutput
    parsed_summary: str
    conflicts: list[IntentConflict] = Field(default_factory=list)
    clarification_questions: list[ClarificationQuestion] = Field(default_factory=list)
    ai_model: str = ""
    parsing_time_ms: int = 0
```

### 4.2 现有模型扩展

在 `StructuredIntentOutput` 中新增字段：

```python
class StructuredIntentOutput(BaseModel):
    # ... 现有字段 ...

    # 新增
    ai_parsed_summary: str = ""           # AI 对用户意图的自然语言摘要
    modification_commands: list[dict] = Field(default_factory=list)
    # 支持的命令类型：
    # {"action": "add", "target": "balcony", "scope": "south_facade", "params": {...}}
    # {"action": "replace", "target": "IfcWindow", "condition": {...}, "new_family": "..."}
    # {"action": "remove", "target": "IfcSpace", "condition": {...}}
    # {"action": "modify", "target": "IfcWall", "condition": {...}, "changes": {...}}
```

---

## 5. 技术实现方案

### 5.1 LLM 选型与集成

| 方案 | 推荐度 | 说明 |
|------|--------|------|
| OpenAI GPT + Structured Output | ★★★★★ | JSON Schema 强约束，输出稳定 |
| Claude + Tool Use | ★★★★★ | 结构化输出质量高，长文本理解强 |
| 本地模型（Qwen/DeepSeek） | ★★★★☆ | 离线可用，需自建 Schema 约束 |
| 多模型回退 | ★★★★★ | 首选 Claude/GPT，失败回退本地模型 |

**推荐方案**：抽象 LLM 接口 + 多模型回退

```python
class LLMClient(Protocol):
    async def structured_output(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: dict,
    ) -> dict: ...

class OpenAIClient:
    def __init__(self, api_key, model="gpt-4o"):
        ...

class ClaudeClient:
    def __init__(self, api_key, model="claude-sonnet-4-20250514"):
        ...

class FallbackLLMClient:
    """多模型回退客户端"""
    def __init__(self, clients: list[LLMClient]):
        self.clients = clients

    async def structured_output(self, system_prompt, user_prompt, schema):
        for client in self.clients:
            try:
                return await client.structured_output(system_prompt, user_prompt, schema)
            except Exception:
                continue
        raise AllModelsFailedError()
```

### 5.2 解析策略选择器

```python
class IntentProviderSelector:
    """根据输入复杂度选择解析策略"""

    def select(self, bundle, parsed) -> IntentProvider:
        prompt = bundle.prompt
        has_drawing = parsed.assets_count > 0
        has_complex_modification = any(
            kw in prompt for kw in ["替换", "更换", "增加", "删除", "修改", "调整"]
        )

        if has_complex_modification or len(prompt) > 50:
            return self.ai_provider      # 复杂需求走 AI
        elif has_drawing and not prompt.strip():
            return self.drawing_provider  # 纯图纸模式走简化解析
        else:
            return self.heuristic_provider  # 简单需求走正则（低延迟）
```

### 5.3 文件变更清单

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `app/intent_service.py` | **重构** | 新增 `AIIntentProvider`，保留 `HeuristicStructuredIntentProvider` 作为回退 |
| `app/llm_client.py` | **新增** | LLM 客户端抽象层（OpenAI / Claude / 回退策略） |
| `app/models.py` | **扩展** | 新增 `AIIntentParseRequest`/`Response`、`ClarificationQuestion`、`IntentConflict` |
| `app/config/intent_defaults.json` | **扩展** | 增加知识库条目、AI 提示词模板 |
| `app/config/ai_prompts.json` | **新增** | AI system prompt 和 user prompt 模板配置 |
| `app/main.py` | **扩展** | 新增 `/api/projects/{id}/intent/parse` 端点 |
| `app/pipeline.py` | **修改** | 新增 `parse_intent_only()` 方法（不执行完整建模链路） |
| `web/src/App.tsx` | **重构** | Step 04 区域改为 AI 解析 + 可编辑确认流程 |
| `web/src/types.ts` | **扩展** | 新增 AI 解析相关 TypeScript 类型 |
| `web/src/api.ts` | **扩展** | 新增 `parseIntent()` API 调用 |

---

## 6. 实施阶段与分工

### 6.1 阶段规划

#### Phase 1：基础 AI 集成（2 周）

**目标**：接入 LLM，实现文本 → 结构化意图的基本链路。

| 任务 | 负责人 | 工作量 | 依赖 |
|------|--------|--------|------|
| T1.1 LLM 客户端抽象层 | AI 工程师 | 2天 | 无 |
| T1.2 AI 提示词设计与调试 | AI 工程师 | 3天 | T1.1 |
| T1.3 AIIntentProvider 实现 | AI 工程师 | 3天 | T1.1, T1.2 |
| T1.4 解析策略选择器 | AI 工程师 | 1天 | T1.3 |
| T1.5 回退机制验证 | AI 工程师 | 1天 | T1.3 |

#### Phase 2：知识库与智能补充（1.5 周）

**目标**：建立建筑知识库，实现参数智能补全。

| 任务 | 负责人 | 工作量 | 依赖 |
|------|--------|--------|------|
| T2.1 建筑知识库结构设计 | BIM 专家 + AI 工程师 | 2天 | 无 |
| T2.2 知识库数据填充 | BIM 专家 | 3天 | T2.1 |
| T2.3 智能补全逻辑实现 | AI 工程师 | 2天 | T2.1, Phase 1 |
| T2.4 补全结果校验 | 后端工程师 | 1天 | T2.3 |

#### Phase 3：冲突融合与指令下发（1.5 周）

**目标**：实现文本与图纸信息的智能融合，完善指令下发链路。

| 任务 | 负责人 | 工作量 | 依赖 |
|------|--------|--------|------|
| T3.1 冲突检测逻辑 | 后端工程师 | 2天 | Phase 1 |
| T3.2 融合策略实现 | AI 工程师 + 后端工程师 | 3天 | T3.1 |
| T3.3 增量指令策略 | 后端工程师 | 2天 | T3.2 |
| T3.4 指令预览 API | 后端工程师 | 1天 | T3.3 |

#### Phase 4：前端交互升级（2 周）

**目标**：实现 AI 解析确认、可编辑参数、冲突展示的前端界面。

| 任务 | 负责人 | 工作量 | 依赖 |
|------|--------|--------|------|
| T4.1 意图解析确认 UI | 前端工程师 | 3天 | Phase 1 API |
| T4.2 可编辑参数面板 | 前端工程师 | 3天 | T4.1 |
| T4.3 冲突展示与解决 UI | 前端工程师 | 2天 | Phase 3 API |
| T4.4 联调与端到端测试 | 全员 | 2天 | T4.1-T4.3 |

### 6.2 团队分工

#### 推荐团队配置（4-5 人）

| 角色 | 人员 | 职责范围 | 对应模块 |
|------|------|---------|---------|
| **AI 工程师**（1人） | 核心开发 | LLM 集成、提示词设计、AI 意图解析、智能补充逻辑 | `llm_client.py`, `intent_service.py` AI 部分 |
| **后端工程师**（1人） | 数据与编排 | 数据模型扩展、冲突融合、API 端点、策略选择器 | `models.py`, `main.py`, `pipeline.py` |
| **前端工程师**（1人） | 交互实现 | Step 04 UI 重构、AI 解析确认面板、冲突展示 | `App.tsx`, `types.ts`, `api.ts` |
| **BIM/建筑专家**（1人） | 知识与验证 | 知识库设计、提示词审核、建筑参数校验、测试用例设计 | `intent_defaults.json`, `ai_prompts.json` |
| **测试工程师**（0.5人） | 质量保障 | 集成测试、端到端测试、回归测试 | 测试用例 |

#### 协作接口

```
前端工程师  ←→  后端工程师  ←→  AI 工程师
     │               │               │
     │    API 端点    │  LLM 接口     │
     │  (JSON 契约)   │ (Prompt 契约) │
     │               │               │
     └───────────────┼───────────────┘
                     │
              BIM/建筑专家
           (知识库 + 校验规则)
```

#### 精简版分工（3 人）

如果团队只有3人，建议如下合并：

| 线路 | 负责人 | 范围 |
|------|--------|------|
| A线 - AI 核心 | AI 工程师 | T1 全部 + T2.2 + T2.3 + T3.2 |
| B线 - 后端支撑 | 后端工程师 | T2.1 + T2.4 + T3 全部 + 模型扩展 + API |
| C线 - 前端交互 | 前端工程师 | T4 全部 + 与 A/B 线联调 |

---

## 7. 质量保障

### 7.1 测试策略

| 测试类型 | 覆盖范围 | 工具 |
|---------|---------|------|
| 单元测试 | LLM 客户端 mock、IntentProvider、融合逻辑 | pytest |
| 集成测试 | 完整解析链路（文本 → DesignIntent） | pytest + LLM mock |
| Prompt 测试 | 回归测试集（50+ 典型用户输入） | 自建 prompt 评测框架 |
| 端到端测试 | 前端提交 → AI 解析 → BIM 建模 → IFC 导出 | Playwright |
| 性能测试 | AI 解析延迟 < 3s，完整链路 < 30s | Locust |

### 7.2 Prompt 质量指标

- **字段提取准确率** ≥ 95%（对标准测试集）
- **Schema 合规率** ≥ 99%（LLM 输出通过 Pydantic 校验的比例）
- **回退触发率** ≤ 5%（需要降级到正则解析的比例）
- **用户确认率** ≥ 80%（用户无需修改 AI 解析结果直接确认的比例）

### 7.3 回归测试用例示例

```python
TEST_CASES = [
    {
        "input": "在8000平方米地块上生成一栋12层住宅楼，两梯四户，现代风格",
        "expected": {
            "building_type": "residential",
            "floors": 12,
            "site_area_sqm": 8000,
            "units_per_floor": 4,
            "style.facade": "modern",
        }
    },
    {
        "input": "6层办公楼，层高3.6米，落地窗，玻璃幕墙",
        "expected": {
            "building_type": "office",
            "floors": 6,
            "standard_floor_height_m": 3.6,
        }
    },
    {
        "input": "根据图纸建模，将所有800x1200的窗替换为落地窗",
        "expected": {
            "source_mode": "cad_to_bim",
            "model_patch.action_type": "replace_family",
            "element_selector.ifc_type": "IfcWindow",
        }
    },
    {
        "input": "南侧加阳台，窗墙比降到0.45",
        "expected": {
            "modification_commands": [
                {"action": "add", "target": "balcony", "scope": "south_facade"},
            ],
        }
    },
]
```

---

## 8. 风险与应对

| 风险 | 影响 | 概率 | 应对 |
|------|------|------|------|
| LLM 输出不稳定，JSON 解析失败 | 建模链路中断 | 中 | 回退到 Heuristic 解析器，保证基本功能可用 |
| LLM 延迟过高（>5s） | 用户体验差 | 低 | 解析策略选择器，简单需求走正则；复杂需求走 AI |
| 建筑知识不足导致补全错误 | 模型参数不合规 | 中 | BIM 专家审核知识库；所有补全标记为 assumption 待确认 |
| Prompt 注入攻击 | 系统安全风险 | 低 | 输入过滤、输出校验、LLM 沙箱隔离 |
| API 成本过高 | 运营成本增加 | 中 | 简单请求走正则，仅复杂请求调用 LLM；本地模型备选 |

---

## 9. 交付里程碑

| 里程碑 | 时间 | 交付物 | 验收标准 |
|--------|------|--------|---------|
| M1 - AI 基础链路 | 第 2 周 | LLM 集成 + AI 意图解析 | 50% 测试用例通过 |
| M2 - 知识库补全 | 第 3.5 周 | 建筑知识库 + 智能补充 | 80% 测试用例通过 |
| M3 - 融合与指令 | 第 5 周 | 冲突融合 + 指令下发 | 95% 测试用例通过 |
| M4 - 前端升级 | 第 7 周 | 新 UI + 端到端联调 | 完整链路可演示 |

---

## 10. 总结

STEP04 建模意图优化的核心是将「正则表达式解析」升级为「AI 驱动的语义理解」。关键设计原则：

1. **渐进式升级**：AI 解析器与现有正则解析器并存，按复杂度自动选择，回退可靠
2. **人机协同**：AI 解析后提供可编辑确认界面，用户始终拥有最终决定权
3. **来源可追溯**：每个字段都记录来源和置信度，补全决策透明可审计
4. **图纸融合**：文本指令与图纸信息智能合并，不是简单覆盖
5. **职责清晰**：AI 负责理解与转化，BIM 引擎负责执行，规则引擎负责校验

建议以 Phase 1 为首要目标，先打通 AI 文本 → DesignIntent 的基本链路，再逐步增强知识库和前端交互。
