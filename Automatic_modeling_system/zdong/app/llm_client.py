"""LLM 客户端抽象层 — 统一 OpenAI / Claude 调用接口，支持自动回退。

提供统一的 structured_output 接口，屏蔽不同 LLM 厂商 API 差异，
支持同步 / 异步调用，以及多模型自动回退策略。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 异常类
# ---------------------------------------------------------------------------

class LLMError(Exception):
    """LLM 调用基础异常."""


class LLMTimeoutError(LLMError):
    """LLM 调用超时."""


class LLMOutputValidationError(LLMError):
    """LLM 输出 JSON 验证失败."""


class AllModelsFailedError(LLMError):
    """所有回退模型均调用失败."""

    def __init__(self, errors: list[Exception] | None = None) -> None:
        self.errors = errors or []
        detail = "; ".join(str(e) for e in self.errors) if self.errors else "all models failed"
        super().__init__(detail)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

class LLMConfig(BaseModel):
    """LLM 客户端配置."""

    provider: str = "openai"  # "openai" | "claude" | "fallback"
    api_key: str | None = None
    model: str = "gpt-4o"
    temperature: float = 0.1
    max_tokens: int = 4096
    timeout_seconds: int = 30

    # Claude 备选配置
    claude_api_key: str | None = None
    claude_model: str = "claude-sonnet-4-20250514"

    # 回退配置
    fallback_enabled: bool = True


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class LLMClient(Protocol):
    """LLM 客户端协议 — 所有实现必须提供同步 / 异步 structured_output."""

    def structured_output(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        """同步调用 LLM 并返回符合 *schema* 的 JSON dict."""
        ...

    async def astructured_output(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        """异步调用 LLM 并返回符合 *schema* 的 JSON dict."""
        ...


# ---------------------------------------------------------------------------
# JSON 解析工具
# ---------------------------------------------------------------------------

def _parse_json_output(raw: str) -> dict[str, Any]:
    """从 LLM 原始回复中提取 JSON dict，兼容 markdown 代码块包裹."""
    text = raw.strip()
    # 尝试提取 markdown 代码块中的 JSON
    if "```" in text:
        start = text.find("```")
        end = text.find("```", start + 3)
        if end != -1:
            chunk = text[start + 3 : end]
            # 去掉可能的语言标注，如 ```json
            if "\n" in chunk:
                chunk = chunk.split("\n", 1)[1]
            text = chunk.strip()
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMOutputValidationError(
            f"JSON 解析失败: {exc}\n原始内容: {raw[:500]}"
        ) from exc
    if not isinstance(result, dict):
        raise LLMOutputValidationError(
            f"期望 dict，实际得到 {type(result).__name__}"
        )
    return result


# ---------------------------------------------------------------------------
# OpenAI 实现
# ---------------------------------------------------------------------------

class OpenAIClient:
    """基于 openai 库的 LLM 客户端，使用 response_format 强制 JSON 输出."""

    def __init__(self, config: LLMConfig) -> None:
        try:
            import openai  # noqa: F811
        except ImportError as exc:
            raise LLMError("openai 库未安装，请执行 `pip install openai`") from exc

        if not config.api_key:
            raise LLMError("OpenAI api_key 未配置")

        self._client = openai.OpenAI(
            api_key=config.api_key,
            timeout=config.timeout_seconds,
        )
        self._async_client = openai.AsyncOpenAI(
            api_key=config.api_key,
            timeout=config.timeout_seconds,
        )
        self.model = config.model
        self.temperature = config.temperature
        self.max_tokens = config.max_tokens
        self.timeout_seconds = config.timeout_seconds

    def _build_messages(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
    ) -> list[dict[str, str]]:
        schema_hint = (
            f"\n\n请严格按照以下 JSON Schema 输出（不要输出任何其他内容）:\n"
            f"{json.dumps(schema, ensure_ascii=False, indent=2)}"
        )
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt + schema_hint},
        ]

    def structured_output(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        messages = self._build_messages(system_prompt, user_prompt, schema)
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            if "timeout" in str(exc).lower():
                raise LLMTimeoutError(f"OpenAI 调用超时 ({self.timeout_seconds}s)") from exc
            raise LLMError(f"OpenAI 调用失败: {exc}") from exc

        content = resp.choices[0].message.content or ""
        return _parse_json_output(content)

    async def astructured_output(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        messages = self._build_messages(system_prompt, user_prompt, schema)
        try:
            resp = await self._async_client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            if "timeout" in str(exc).lower():
                raise LLMTimeoutError(f"OpenAI 调用超时 ({self.timeout_seconds}s)") from exc
            raise LLMError(f"OpenAI 调用失败: {exc}") from exc

        content = resp.choices[0].message.content or ""
        return _parse_json_output(content)


# ---------------------------------------------------------------------------
# Claude 实现
# ---------------------------------------------------------------------------

class ClaudeClient:
    """基于 anthropic 库的 LLM 客户端，通过 tool_use 强制 JSON Schema 输出."""

    def __init__(self, config: LLMConfig) -> None:
        try:
            import anthropic  # noqa: F811
        except ImportError as exc:
            raise LLMError("anthropic 库未安装，请执行 `pip install anthropic`") from exc

        api_key = config.claude_api_key or config.api_key
        if not api_key:
            raise LLMError("Claude api_key 未配置")

        self._client = anthropic.Anthropic(api_key=api_key, timeout=config.timeout_seconds)
        self._async_client = anthropic.AsyncAnthropic(api_key=api_key, timeout=config.timeout_seconds)
        self.model = config.claude_model
        self.temperature = config.temperature
        self.max_tokens = config.max_tokens
        self.timeout_seconds = config.timeout_seconds

    @staticmethod
    def _extract_tool_json(content_blocks: list[Any]) -> dict[str, Any]:
        """从 Claude tool_use content block 中提取 JSON dict."""
        for block in content_blocks:
            if block.type == "tool_use":
                return block.input  # type: ignore[return-value]
        raise LLMOutputValidationError("Claude 响应中未找到 tool_use block")

    def _build_tool(self, schema: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": "structured_output",
            "description": "以 JSON 格式输出结构化结果",
            "input_schema": schema,
        }

    def structured_output(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        tool = self._build_tool(schema)
        try:
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
                tools=[tool],
                tool_choice={"type": "tool", "name": "structured_output"},
                temperature=self.temperature,
            )
        except Exception as exc:
            if "timeout" in str(exc).lower():
                raise LLMTimeoutError(f"Claude 调用超时 ({self.timeout_seconds}s)") from exc
            raise LLMError(f"Claude 调用失败: {exc}") from exc

        return self._extract_tool_json(resp.content)

    async def astructured_output(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        tool = self._build_tool(schema)
        try:
            resp = await self._async_client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
                tools=[tool],
                tool_choice={"type": "tool", "name": "structured_output"},
                temperature=self.temperature,
            )
        except Exception as exc:
            if "timeout" in str(exc).lower():
                raise LLMTimeoutError(f"Claude 调用超时 ({self.timeout_seconds}s)") from exc
            raise LLMError(f"Claude 调用失败: {exc}") from exc

        return self._extract_tool_json(resp.content)


# ---------------------------------------------------------------------------
# 回退实现
# ---------------------------------------------------------------------------

class FallbackLLMClient:
    """多模型回退客户端 — 按顺序尝试，第一个成功即返回."""

    def __init__(self, clients: list[LLMClient]) -> None:
        if not clients:
            raise LLMError("FallbackLLMClient 至少需要一个客户端实例")
        self._clients = clients
        self._stats: dict[int, dict[str, int]] = {
            i: {"success": 0, "fail": 0} for i in range(len(clients))
        }

    def _record_success(self, idx: int) -> None:
        self._stats[idx]["success"] += 1

    def _record_fail(self, idx: int) -> None:
        self._stats[idx]["fail"] += 1

    def structured_output(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        errors: list[Exception] = []
        for idx, client in enumerate(self._clients):
            try:
                result = client.structured_output(system_prompt, user_prompt, schema)
                self._record_success(idx)
                logger.debug("FallbackLLMClient: 客户端 #%d 成功", idx)
                return result
            except Exception as exc:
                self._record_fail(idx)
                errors.append(exc)
                logger.debug("FallbackLLMClient: 客户端 #%d 失败 — %s", idx, exc)
        raise AllModelsFailedError(errors)

    async def astructured_output(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        errors: list[Exception] = []
        for idx, client in enumerate(self._clients):
            try:
                result = await client.astructured_output(system_prompt, user_prompt, schema)
                self._record_success(idx)
                logger.debug("FallbackLLMClient: 客户端 #%d 异步成功", idx)
                return result
            except Exception as exc:
                self._record_fail(idx)
                errors.append(exc)
                logger.debug("FallbackLLMClient: 客户端 #%d 异步失败 — %s", idx, exc)
        raise AllModelsFailedError(errors)


# ---------------------------------------------------------------------------
# Mock 客户端（测试用）
# ---------------------------------------------------------------------------

class MockLLMClient:
    """Mock 客户端，用于单元测试."""

    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self._response = response or {}

    def structured_output(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        return self._response

    async def astructured_output(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        return self._response


# ---------------------------------------------------------------------------
# 工厂函数
# ---------------------------------------------------------------------------

def create_llm_client(config: LLMConfig | None = None) -> LLMClient:
    """根据配置创建合适的 LLM 客户端.

    - provider="openai"  → OpenAIClient
    - provider="claude"  → ClaudeClient
    - provider="fallback" 或 fallback_enabled=True → FallbackLLMClient

    当 fallback_enabled=True 且 provider 不是 "fallback" 时，
    会以主客户端为第一选择、另一家为备选，自动构建回退链。
    无可用 key 时回退到 MockLLMClient。
    """
    if config is None:
        config = LLMConfig()

    provider = config.provider
    clients: list[LLMClient] = []

    if provider in ("openai", "fallback") and config.api_key:
        try:
            clients.append(OpenAIClient(config))
        except LLMError:
            logger.debug("OpenAI 客户端创建失败，跳过")

    if provider in ("claude", "fallback") and (config.claude_api_key or config.api_key):
        try:
            clients.append(ClaudeClient(config))
        except LLMError:
            logger.debug("Claude 客户端创建失败，跳过")

    if not clients:
        return MockLLMClient()

    if len(clients) == 1:
        return clients[0]

    return FallbackLLMClient(clients)
