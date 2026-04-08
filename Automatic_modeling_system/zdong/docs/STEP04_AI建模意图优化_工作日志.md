# STEP04 AI 建模意图优化 — 工作日志

**日期**: 2026-04-08
**目标**: 优化 STEP04 建模意图功能，实现「输入文字要求 → AI 补充 → 转化为建模指令 → 传输到 BIM 引擎 → 结合图纸信息完成模型创建」的完整流程。

---

## 完成任务清单

### Task 1: AI 提示词配置文件 ✅
- **文件**: `app/config/ai_prompts.json`
- **内容**: 包含 system_prompt、user_prompt_template、fallback_system_prompt 三套模板
- **要点**:
  - System prompt 约束 LLM 严格按 JSON Schema 输出
  - 定义了 6 种来源分类规则（form_input / text_prompt / parsed_drawing / ai_inferred / template_default / system_default）
  - 包含建筑类型识别规则、来源模式判断逻辑、修改指令格式规范
  - User prompt 模板支持 17 个变量占位符

### Task 2: 扩展建筑知识库配置 ✅
- **文件**: `app/config/intent_defaults.json`
- **内容**: 新增 `knowledge_entries` 节，包含 7 个建筑知识条目
- **条目列表**:
  - `residential_low_rise`: 低层住宅（1-3层），楼梯间核心，户型面积 90-150㎡
  - `residential_mid_rise`: 多层住宅（4-6层），楼梯核心，户型面积 80-120㎡
  - `residential_high_rise`: 高层住宅（7-33层），双电梯+楼梯核心，户型面积 70-140㎡
  - `office_low_rise`: 低层办公（1-3层），层高 3.6m，开敞办公比例 60-80%
  - `office_mid_high_rise`: 中高层办公（4+层），层高 3.9m，幕墙比例 50-70%
  - `modern_facade_style`: 现代风格立面，玻璃/铝材/石材
  - `traditional_facade_style`: 传统风格立面，砖/涂料/木材

### Task 3: 新增 API 端点和 Pipeline 方法 ✅
- **文件**: `app/main.py`, `app/pipeline.py`
- **新增端点**: `POST /api/projects/{project_id}/intent/parse`
- **新增方法**:
  - `ModelingPipeline.parse_intent_only()` — 仅执行意图解析，不触发完整建模管线
  - `_detect_intent_conflicts()` — 检测表单输入与图纸解析结果之间的冲突
  - `_build_clarification_questions()` — 根据低置信度字段生成澄清问题
  - `_build_parsed_summary()` — 生成中文解析摘要文本
- **返回结构**: `AIIntentParseResponse`（包含结构化意图、摘要、冲突、澄清问题、解析耗时）

### Task 4: LLM 客户端抽象层 ✅
- **文件**: `app/llm_client.py`
- **类结构**:
  - `LLMConfig` — 配置模型（支持 OpenAI / Claude / fallback）
  - `OpenAIClient` — 使用 `response_format={"type": "json_object"}` 强制 JSON ���出
  - `ClaudeClient` — 使用 tool_use 机制强制 JSON Schema 输出
  - `FallbackLLMClient` — 多模型自动回退，按顺序尝试
  - `MockLLMClient` — 测试用模拟客户端
  - `create_llm_client()` — 工厂函数，根据配置创建客户端
- **异常体系**: `LLMError` → `LLMTimeoutError` / `LLMOutputValidationError` / `AllModelsFailedError`
- **JSON 解析**: `_parse_json_output()` 兼容 markdown 代码块包裹的 JSON

### Task 5: 前端 Step 04 AI 解析确认界面 ✅
- **文件**: `web/src/types.ts`, `web/src/api.ts`, `web/src/App.tsx`
- **新增类型**:
  - `AIIntentParseResponse` — 完整的解析响应类型
  - `ClarificationQuestion` — 澄清问题
  - `IntentConflict` — 意图冲突
- **新增 API 函数**: `parseIntent()` — 调用意图解析端点
- **前端交互升级**:
  - Step 04 新增「AI 解析意图」按钮
  - 点击后显示结构化解析结果面板
  - 展示：解析摘要、建筑类型/模式、系统假设、冲突检测、待确认问题、修改指令
  - 文本修改时自动清除上一次解析结果

### Task 6: AIIntentProvider 集成 ✅
- **文件**: `app/intent_service.py`
- **新增类**:
  - `PromptBuilder` — 构建 AI 意图解析的提示词（加载 ai_prompts.json 模板）
  - `AIIntentProvider` — 基于 LLM 的意图解析器，失败时自动回退到启发式解析器
  - `IntentProviderSelector` — 根据输入复杂度选择解析策略（复杂/长文本 → AI，简单 → 启发式）
- **修改**: `StructuredIntentTransformer.__init__` 新增 `llm_client` 可选参数，支持 AI 驱动解析
- **回退策略**: AI 解析失败 → 启发式正则解析（保证系统可用性）

---

## 数据模型扩展

### models.py 新增模型
| 模型 | 用途 |
|------|------|
| `ClarificationQuestion` | 澄清问题（字段、问题文本、建议答案、置信度） |
| `IntentConflict` | 意图冲突（字段、文本值、图纸值、解决方案、原因） |
| `AIIntentParseRequest` | AI 解析请求（prompt、building_type、region、form_fields、asset_ids） |
| `AIIntentParseResponse` | AI 解析响应（结构化意图、摘要、冲突、澄清问题、模型名、耗时） |

### StructuredIntentOutput 扩展字段
- `ai_parsed_summary: str` — AI 解析摘要
- `modification_commands: list[dict]` — 修改指令列表

---

## 架构决策

1. **策略模式**：IntentProvider 协议 + HeuristicStructuredIntentProvider / AIIntentProvider 双实现，支持优雅降级
2. **选择器模式**：IntentProviderSelector 根据输入复杂度自动路由到合适的解析器
3. **回退机制**：AI 解析失败时自动回退到正则启发式解析，确保系统始终可用
4. **轻量预览**：`parse_intent_only()` 仅执行解析阶段，不触发完整建模管线，适合前端实时预览

---

## 文件变更清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `app/config/ai_prompts.json` | 新增 | AI 提示词模板配置 |
| `app/config/intent_defaults.json` | 修改 | 新增知识条目 knowledge_entries |
| `app/llm_client.py` | 新增 | LLM 客户端抽象层 |
| `app/intent_service.py` | 修改 | 集成 AI 解析器、选择器、提示词构建器 |
| `app/models.py` | 修改 | 新增 4 个 Pydantic 模型 + 扩展字段 |
| `app/pipeline.py` | 修改 | 新增 parse_intent_only() 及辅助方法 |
| `app/main.py` | 修改 | 新增 POST intent/parse 端点 |
| `web/src/types.ts` | 修改 | 新增前端类型定义 |
| `web/src/api.ts` | 修改 | 新增 parseIntent() API 函数 |
| `web/src/App.tsx` | 修改 | Step 04 升级为 AI 解析预览界面 |
