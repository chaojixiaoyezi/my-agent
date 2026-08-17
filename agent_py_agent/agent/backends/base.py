from __future__ import annotations

"""模型后端适配层。

这一层的作用很像'翻译器'：
- 上层智能体只知道自己要把 prompt 发给模型。
- 下层不同厂商的接口格式却不一样。

所以这里把不同后端都包装成统一接口，避免核心调度器里到处写 if/else。
"""

import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..settings.defaults import DEFAULT_MODEL_MAX_TOKENS
from ..tooling.runtime_contracts import ProviderToolCapability, ToolChoice
from .errors import ProviderRecoverableError, ProviderResponseError
from .gateway_helpers import GatewayRequest, post_json, post_stream, post_stream_iter
from .model_metadata import ProviderMetadataOptions, discover_provider_model_metadata
from .stream_parsers import StreamCompletion
from .usage_metadata import (
    collect_anthropic_stream_with_completion,
    collect_openai_stream_with_completion,
    openai_stream_payload,
    usage_dict,
)

# 会话运行时/长期助手 keep a length-truncated turn's completed text and continue the
# loop instead of killing the run.  Anthropic-compatible providers expose the
# fact through ``stop_reason=max_tokens``; OpenAI-compatible providers use
# ``finish_reason=length``.  Truncation is no longer a fatal adapter error:
# the adapter returns a ``truncated`` ModelResponse whose text is preserved and
# whose tool blocks are dropped (a truncated tool call cannot be executed
# safely), matching harness assembler behavior; the tool loop then continues
# with the partial text as this turn's assistant message.
_INCOMPLETE_STOP_REASONS = frozenset({"max_tokens", "length"})


# LLM: 截断不再 raise 致命错误（真机 2026-08-16 长任务复刻：deepseek-v4-flash
# 单轮输出上限内写长文件时 stop_reason=max_tokens → 适配器 raise → 整个 run
# 终止 RC=2；harness/轻量运行时 同场景保留文本继续循环，任务不中断）。
# 函数用途: 判断供应商停止原因是否属截断（max_tokens/length）。
def _incomplete_stop_reason(stop_reason: object) -> bool:
    return str(stop_reason or "").strip().lower() in _INCOMPLETE_STOP_REASONS


def _bounded_output_tokens(configured: int, requested: int | None) -> int:
    if requested is None:
        return configured
    return max(1, min(configured, int(requested)))


# LLM: 这是所有模型后端返回给运行时的唯一响应合同；新增内部历史字段时必须保证不会进入用户可见正文，并同步原生工具循环测试。
# 类用途: 统一保存模型正文、用量、工具调用和厂商原生历史块，供后续工具轮继续使用。
@dataclass
class ModelResponse:
    """Normalized model response returned to the agent runtime."""

    text: str
    backend: str
    runtime_status: str = "ok"
    runtime_reason: str = ""
    runtime_source: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    # 原生 tool_use 协议(tool_protocol=native)下，从结构化响应抽出的工具调用块，
    # 每块形如 {"id","name","input"}。文本协议下恒为空，不影响现有行为。
    tool_use_blocks: list[dict[str, Any]] = field(default_factory=list)
    # Anthropic Messages 返回的 assistant content blocks，经输入白名单清洗后保留原顺序。
    # 它只供下一轮原生消息回放（例如 thinking/text/tool_use），不拼进 text，也不投递给用户。
    assistant_content_blocks: list[dict[str, Any]] = field(default_factory=list)
    # native 流式响应在 message_stop 前 EOF，或 stop_reason∈{max_tokens,length} 且仍有
    # 未闭合的 tool_use 参数缓冲 → True：这次响应（含 tool_use 参数 JSON）疑似被截断。
    # 默认 False；非流式与 text 协议恒 False，且只有 native 恢复/降级逻辑消费它，零回归。
    truncated: bool = False
    # 上游返回的原始 stop_reason(观测/审计用;自动续写后为最后一段的 stop_reason)。
    stop_reason: str = ""
    # 传输/流边界在形成完整响应前发现的结构化工具协议错误。这里只能由宿主适配层写入；
    # 模型正文不能设置该字段，也不能借此制造可执行 ToolCall。
    tool_protocol_violations: list[dict[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class BackendOptions:
    """Connection and generation options shared by HTTP model backends."""

    api_base: str
    api_key: str
    model_name: str
    request_timeout: int = 240
    connect_timeout: float = 10.0
    max_tokens: int = DEFAULT_MODEL_MAX_TOKENS
    context_window_tokens: int = 0
    temperature: float = 0.2
    stream_enabled: bool = True


@dataclass(frozen=True)
class _OpenAIGenerateRequest:
    prompt: str
    on_chunk: Callable[[str], None] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: ToolChoice | None = None
    messages: list[dict[str, Any]] | None = None
    response_schema: dict[str, Any] | None = None
    json_object: bool = False
    max_output_tokens: int | None = None


# 原生工具能力探针的最大尝试次数(弱模型偶发无视强制 tool_choice 回散文,
# 单发误判"不支持 native"; 有界重试后仍无结构化调用才判不支持)。
_PROBE_MAX_ATTEMPTS = 3


class BaseBackend:
    """所有后端适配器都要实现的基类接口。"""

    name = "base"

    def provider_context_window_tokens(self) -> int:
        """Return provider-advertised context capacity, or zero when unavailable."""
        return 0

    def probe_tool_capability(self) -> ProviderToolCapability:
        """Return an explicit unsupported fact for backends without native tools."""

        return ProviderToolCapability(
            provider=str(self.name or "base"),
            endpoint=f"local://{self.name or 'base'}",
            model=str(getattr(self, "model_name", "") or ""),
            stream=bool(getattr(self, "stream_enabled", False)),
            native_supported=False,
            evidence="backend_declares_no_native_tool_transport",
            observed_at=_utc_now_iso(),
        )

    def generate(
        self,
        prompt: str,
        on_chunk: Callable[[str], None] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: ToolChoice | None = None,
        messages: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        """Generate one assistant response for the supplied prompt.

        ``tools`` carries the run-fixed provider schema for native tool use.
        A backend without the selected native capability must fail closed;
        protocol selection happens before the run and never falls back here.

        ``messages`` carries a provider-native structured conversation (native
        tool_use IR translated to Anthropic ``messages``). When supplied it
        replaces the single ``{"role":"user","content":prompt}`` turn; ``prompt``
        still feeds the system/task instructions. Text-protocol callers omit it.
        """
        raise NotImplementedError

    def generate_structured(
        self,
        prompt: str,
        *,
        response_schema: dict[str, Any],
        messages: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        """Generate a JSON-shaped response, with prompt-only fallback by default.

        Provider adapters that support a native output schema override this method.
        Keeping the capability separate from ``generate`` preserves compatibility with
        third-party/test backends whose existing method signature is intentionally small.
        """

        del response_schema
        return self.generate(prompt, messages=messages)

    def generate_json(
        self,
        prompt: str,
        *,
        max_tokens: int | None = None,
        messages: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        """Generate one JSON object, with prompt-only fallback by default."""

        del max_tokens
        return self.generate(prompt, messages=messages)


class EchoBackend(BaseBackend):
    """Local deterministic backend used by tests and dry development."""

    name = "echo"

    def probe_tool_capability(self) -> ProviderToolCapability:
        # EXEC-31b(text 删除后): echo 是本地测试后端, 工具循环在本地解析,
        # 声明 native 支持(native 是唯一协议, 不再有 text 降级)。
        return ProviderToolCapability(
            provider=self.name,
            endpoint=f"local://{self.name}",
            model=str(getattr(self, "model_name", "") or ""),
            stream=False,
            native_supported=True,
            evidence="echo_backend_declares_native",
            observed_at=_utc_now_iso(),
        )

    def generate(
        self,
        prompt: str,
        on_chunk: Callable[[str], None] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: ToolChoice | None = None,
        messages: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        del tools, tool_choice, messages  # echo backend never speaks native tool_use
        lines = [line.strip() for line in prompt.splitlines() if line.strip()]
        if "# User Task" in prompt:
            task = prompt.split("# User Task", 1)[-1]
            task = task.split("\n# ", 1)[0].strip()
        else:
            task = lines[-1] if lines else "空任务"

        summary = task[:300]
        text = (
            "这是 echo 后端的本地响应。\n"
            "我已接收任务，并基于当前 prompt 和记忆生成结构化结果。\n\n"
            f"任务摘要：{summary}\n\n"
            "建议步骤：\n"
            "1. 明确目标。\n"
            "2. 检查已有记忆。\n"
            "3. 必要时拆分 subagent。\n"
            "4. 输出可验证结果。"
        )
        return ModelResponse(text=text, backend=self.name)


class HttpBackend(BaseBackend):
    """真实模型后端共用的 HTTP 请求基础逻辑。"""

    def __init__(
        self,
        options: BackendOptions,
    ):
        self.api_base = str(options.api_base).rstrip("/")
        self.api_key = str(options.api_key)
        self.model_name = str(options.model_name)
        self.request_timeout = int(options.request_timeout)
        self.connect_timeout = float(options.connect_timeout)
        self.max_tokens = int(options.max_tokens)
        # 本地配置与供应商事实分开保存；不能再把 fallback 冒充 provider metadata。
        self.configured_context_window_tokens = int(options.context_window_tokens or 0)
        # 兼容既有只读调用方；context-window resolver 会识别 configured_ 字段，绝不把本属性
        # 当成 provider 事实。新代码应读取 configured_context_window_tokens。
        self.context_window_tokens = self.configured_context_window_tokens
        self._provider_tool_capability_cache: ProviderToolCapability | None = None
        self._provider_context_window_cache: int | None = None
        self.model_metadata: dict[str, Any] = {}
        self.temperature = float(options.temperature)
        self.stream_enabled = bool(options.stream_enabled)
        # Streaming HTTP transports enforce request_timeout as an SSE idle
        # timeout.  The tool-loop guard therefore must not also reinterpret it
        # as a total wall-clock limit while valid events keep arriving.
        self.stream_timeout_is_idle = self.stream_enabled

    def probe_tool_capability(self) -> ProviderToolCapability:
        """Run a harmless structured-call probe (bounded retries) and cache only
        a proven-positive result.

        真机 2026-08-17 MiniMax 弱模型: 探针单发时灵时不灵(模型偶尔无视
        强制 tool_choice 回散文)→ 单发探针把"没调用工具"误判成"模型不
        支持 native", 聊天直接报 ToolProtocolSelectionError。修: ①同一
        探针最多重试 _PROBE_MAX_ATTEMPTS 次(每次新 nonce), 一次结构化
        调用即通过; ②失败不缓存——进程存活期间下一轮再探(弱模型抖动
        不永久判死), 只有成功结果进缓存。本地确定性异常(非 provider)
        不重试, 立即落 evidence。
        """
        cached = self._provider_tool_capability_cache
        if cached is not None:
            return cached
        # 2026-08-17 真机: gateway 进程环境缺 AGENT_API_KEY → generate 抛
        # ValueError("api_key 为空") 被探针 except 吞成"不支持 native",
        # 用户看到误导性 ToolProtocolSelectionError。配置错误≠模型能力,
        # 先结构化检查 key 并原样上抛(调用方显示真实原因)。
        if hasattr(self, "api_key") and not str(self.api_key or "").strip():
            raise ValueError(
                "api_key 为空：无法执行工具能力探针（检查 AGENT_API_KEY 环境变量或配置）"
            )
        evidence = ""
        for _attempt in range(1, _PROBE_MAX_ATTEMPTS + 1):
            nonce = secrets.token_hex(8)
            probe_tool = {
                "name": "my_agent_capability_probe",
                "description": "Internal protocol capability probe with no host-side effect.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "nonce": {
                            "type": "string",
                            "description": "Copy the nonce from the user request exactly.",
                        }
                    },
                    "required": ["nonce"],
                    "additionalProperties": False,
                },
            }
            try:
                response = self.generate(
                    (
                        "Call my_agent_capability_probe exactly once with nonce "
                        f"{nonce}. Do not answer in prose."
                    ),
                    tools=[probe_tool],
                    tool_choice=ToolChoice.specific("my_agent_capability_probe"),
                    thinking_disabled=True,
                )
            except ProviderRecoverableError:
                # EXEC-41b: provider 侧失败(429 额度/限流/超时/连接)不是"不支持
                # native"——探针吞掉它会把配额耗尽误报成"模型无原生工具能力",
                # 真机 2026-08-17 双线 quota 恢复前 resume 报
                # ToolProtocolSelectionError(RC=1 且文案误导)。放行给调用方,
                # CLI 走 provider_quota_exhausted_report 等优雅报告路径。
                raise
            except Exception as exc:
                # 本地确定性异常(解析/配置错)重试无意义, 立即落 evidence。
                code = str(getattr(exc, "error_code", "") or "").strip().upper()
                suffix = f":{code}" if code else ""
                evidence = f"live_probe_failed:{type(exc).__name__}{suffix}"
                break
            blocks = list(getattr(response, "tool_use_blocks", None) or ())
            supported = any(
                isinstance(block, dict)
                and str(block.get("id") or "").strip()
                and str(block.get("name") or "") == "my_agent_capability_probe"
                and isinstance(block.get("input"), dict)
                and str(block["input"].get("nonce") or "") == nonce
                for block in blocks
            )
            if supported:
                capability = ProviderToolCapability(
                    provider=str(self.name or type(self).__name__),
                    endpoint=self._tool_endpoint(),
                    model=self.model_name,
                    stream=self.stream_enabled,
                    native_supported=True,
                    evidence="live_probe_returned_structured_tool_call",
                    observed_at=_utc_now_iso(),
                )
                self._provider_tool_capability_cache = capability
                return capability
            evidence = "live_probe_returned_no_valid_structured_tool_call"
        # 重试耗尽仍未证明: 不缓存(下次 turn 再探), 如实报不支持。
        return ProviderToolCapability(
            provider=str(self.name or type(self).__name__),
            endpoint=self._tool_endpoint(),
            model=self.model_name,
            stream=self.stream_enabled,
            native_supported=False,
            evidence=evidence or "live_probe_returned_no_valid_structured_tool_call",
            observed_at=_utc_now_iso(),
        )

    def _tool_endpoint(self) -> str:
        return self.api_base

    def request_json(
        self, path: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:
        """Send a JSON request through the shared gateway helper."""

        if not self.api_key:
            raise ValueError("api_key 为空：请在配置文件中填写 API Key。")

        return post_json(self._gateway_request(path, payload, headers))

    def request_stream(
        self, path: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> list[str]:
        """Send a streaming request and collect all data lines."""
        return post_stream(self._gateway_request(path, payload, headers))

    def request_stream_iter(self, path: str, payload: dict[str, Any], headers: dict[str, str]):
        """Send a streaming request and yield data lines as they arrive."""
        yield from post_stream_iter(self._gateway_request(path, payload, headers))

    def _gateway_request(
        self, path: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> GatewayRequest:
        """Build the immutable gateway request envelope used by all HTTP calls."""
        return GatewayRequest(
            api_base=self.api_base,
            api_key=self.api_key,
            path=path,
            payload=payload,
            headers=headers,
            timeout=self.request_timeout,
            connect_timeout=self.connect_timeout,
        )

    # LLM: 供应商上下文窗口只从模型 metadata API 的结构化字段读取；失败或字段缺失返回 0，
    #   由上层使用本地配置兜底。不得根据模型名在这里硬编码容量。
    # 人类: 每个 backend 实例只探测一次，避免 compact 每轮重复访问 /models。
    def provider_context_window_tokens(self) -> int:
        """发现并缓存供应商模型目录公开的上下文窗口。"""
        if self._provider_context_window_cache is None:
            metadata = discover_provider_model_metadata(
                ProviderMetadataOptions(
                    api_base=self.api_base,
                    api_key=self.api_key,
                    model_name=self.model_name,
                    request_timeout=self.request_timeout,
                    connect_timeout=self.connect_timeout,
                )
            )
            self.model_metadata = metadata.record
            self._provider_context_window_cache = metadata.context_window_tokens
        return self._provider_context_window_cache


class OpenAICompatibleBackend(HttpBackend):
    """适配 OpenAI-compatible `/chat/completions` 接口。"""

    name = "openai_compatible"

    def _tool_endpoint(self) -> str:
        return self.api_base + "/chat/completions"

    def generate(
        self,
        prompt: str,
        on_chunk: Callable[[str], None] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: ToolChoice | None = None,
        messages: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        """Call the OpenAI-compatible chat completion endpoint."""
        return self._generate(
            _OpenAIGenerateRequest(
                prompt=prompt,
                on_chunk=on_chunk,
                tools=tools,
                tool_choice=tool_choice,
                messages=messages,
            )
        )

    def generate_structured(
        self,
        prompt: str,
        *,
        response_schema: dict[str, Any],
        messages: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        """Use the provider-native strict JSON-schema response format."""

        return self._generate(
            _OpenAIGenerateRequest(
                prompt=prompt,
                messages=messages,
                response_schema=response_schema,
            )
        )

    def generate_json(
        self,
        prompt: str,
        *,
        max_tokens: int | None = None,
        messages: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        """Use the provider-native JSON-object response format."""

        return self._generate(
            _OpenAIGenerateRequest(
                prompt=prompt,
                messages=messages,
                json_object=True,
                max_output_tokens=max_tokens,
            )
        )

    def _generate(self, request: _OpenAIGenerateRequest) -> ModelResponse:
        payload = {
            "model": self.model_name,
            "max_tokens": _bounded_output_tokens(self.max_tokens, request.max_output_tokens),
            "temperature": self.temperature,
        }
        if request.messages:
            payload["messages"] = _openai_messages_from_native(
                request.messages,
                initial_user_prompt=request.prompt,
            )
        else:
            payload["messages"] = [{"role": "user", "content": request.prompt}]
        tools = _tools_for_choice(request.tools, request.tool_choice)
        if tools:
            from .tool_protocol_adapter import openai_tool_choice

            payload["tools"] = _openai_tools_from_native(tools)
            payload["tool_choice"] = openai_tool_choice(request.tool_choice or ToolChoice.auto())
        elif request.tool_choice is not None and request.tool_choice.mode == "none":
            payload["tool_choice"] = "none"
        if request.response_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "my_agent_structured_output",
                    "strict": True,
                    "schema": request.response_schema,
                },
            }
        elif request.json_object:
            payload["response_format"] = {"type": "json_object"}
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        if self.stream_enabled:
            return self._generate_stream(payload, headers, on_chunk=request.on_chunk)
        obj = self.request_json("/chat/completions", payload, headers)
        return _openai_non_stream_response(
            obj,
            backend_name=self.name,
            tools_requested=bool(tools),
        )

    def _generate_stream(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
        on_chunk: Callable[[str], None] | None = None,
    ) -> ModelResponse:
        """Parse OpenAI SSE and concatenate delta.content chunks."""
        lines = self.request_stream_iter if on_chunk is not None else self.request_stream
        text, usage, blocks, completion = collect_openai_stream_with_completion(
            lines("/chat/completions", openai_stream_payload(payload), headers),
            on_chunk=on_chunk,
        )
        if _incomplete_stop_reason(completion.stop_reason):
            # 截断：保留已收文本，丢弃工具调用块（截断的工具调用不能安全执行），
            # 标记 truncated 让工具循环下一轮继续（harness assembler 同款）。
            return ModelResponse(
                text=text,
                backend=self.name,
                usage=usage,
                tool_use_blocks=[],
                truncated=True,
                stop_reason=completion.stop_reason,
            )
        if not text and not blocks and payload.get("tools"):
            raise ProviderResponseError(
                "OpenAI-compatible 流式响应没有文本或工具调用",
                error_code="MODEL_EMPTY_RESPONSE",
            )
        return ModelResponse(
            text=text,
            backend=self.name,
            usage=usage,
            tool_use_blocks=blocks,
            truncated=completion.truncated,
            stop_reason=completion.stop_reason,
        )


def _openai_non_stream_response(
    obj: dict[str, Any],
    *,
    backend_name: str,
    tools_requested: bool,
) -> ModelResponse:
    try:
        choice = obj["choices"][0]
        message = choice["message"]
        if "content" not in message and "tool_calls" not in message:
            raise ValueError("message has neither content nor tool_calls")
        text = str(message.get("content") or "")
        blocks, malformed = _openai_tool_use_blocks(message)
    except Exception as exc:
        raise ProviderResponseError(
            f"无法解析 OpenAI-compatible 响应: {_response_preview(obj)}"
        ) from exc
    finish_reason = str(choice.get("finish_reason") or "")
    if _incomplete_stop_reason(finish_reason):
        # 截断：保留文本、丢弃工具块、标记 truncated（工具循环下一轮继续）。
        return ModelResponse(
            text=text,
            backend=backend_name,
            usage=usage_dict(obj.get("usage")),
            tool_use_blocks=[],
            truncated=True,
            stop_reason=finish_reason,
        )
    if not text and not blocks and tools_requested:
        raise ProviderResponseError(
            f"OpenAI-compatible 响应没有文本或工具调用: {_response_preview(obj)}",
            error_code="MODEL_EMPTY_RESPONSE",
        )
    return ModelResponse(
        text=text,
        backend=backend_name,
        usage=usage_dict(obj.get("usage")),
        tool_use_blocks=blocks,
        truncated=malformed,
        stop_reason=finish_reason,
    )


def _openai_tools_from_native(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate the canonical internal schema to OpenAI function tools."""

    translated: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict) or not str(tool.get("name") or ""):
            continue
        translated.append(
            {
                "type": "function",
                "function": {
                    "name": str(tool["name"]),
                    "description": str(tool.get("description") or ""),
                    "parameters": dict(tool.get("input_schema") or {}),
                },
            }
        )
    return translated


def _openai_messages_from_native(
    messages: list[dict[str, Any]],
    *,
    initial_user_prompt: str,
) -> list[dict[str, Any]]:
    """Translate IR while preserving the first-turn user message across tool rounds."""

    translated: list[dict[str, Any]] = []
    if initial_user_prompt:
        translated.append({"role": "user", "content": initial_user_prompt})
    for message in messages:
        translated.extend(_openai_message_from_native(message))
    return translated


def _openai_message_from_native(message: dict[str, Any]) -> list[dict[str, Any]]:
    role = str(message.get("role") or "")
    content = message.get("content")
    if isinstance(content, str):
        return [{"role": role, "content": content}]
    blocks = (
        [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []
    )
    if role == "assistant":
        return _openai_assistant_messages(blocks)
    if role == "user":
        return _openai_user_messages(blocks)
    return []


def _openai_assistant_messages(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    texts = [str(block.get("text") or "") for block in blocks if block.get("type") == "text"]
    calls = [_openai_function_call(block) for block in blocks if block.get("type") == "tool_use"]
    if texts or calls:
        message: dict[str, Any] = {"role": "assistant", "content": "".join(texts) or None}
        if calls:
            message["tool_calls"] = calls
        return [message]
    return []


def _openai_function_call(block: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(block.get("id") or ""),
        "type": "function",
        "function": {
            "name": str(block.get("name") or ""),
            "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
        },
    }


def _openai_user_messages(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    translated: list[dict[str, Any]] = []
    pending_text: list[str] = []

    def flush_text() -> None:
        if pending_text:
            translated.append({"role": "user", "content": "".join(pending_text)})
            pending_text.clear()

    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            pending_text.append(str(block.get("text") or ""))
            continue
        if block.get("type") != "tool_result":
            continue
        flush_text()
        translated.append(
            {
                "role": "tool",
                "tool_call_id": str(block.get("tool_use_id") or ""),
                "content": str(block.get("content") or ""),
            }
        )
    flush_text()
    return translated


def _openai_tool_use_blocks(message: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    raw_calls = message.get("tool_calls")
    if not isinstance(raw_calls, list):
        return [], False
    blocks: list[dict[str, Any]] = []
    malformed = False
    for raw in raw_calls:
        if not isinstance(raw, dict):
            malformed = True
            continue
        function = raw.get("function") if isinstance(raw.get("function"), dict) else {}
        arguments = function.get("arguments")
        try:
            tool_input = (
                arguments if isinstance(arguments, dict) else json.loads(str(arguments or "{}"))
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            tool_input = {}
            malformed = True
        if not isinstance(tool_input, dict):
            tool_input = {}
            malformed = True
        blocks.append(
            {
                "id": str(raw.get("id") or ""),
                "name": str(function.get("name") or ""),
                "input": tool_input,
            }
        )
    return blocks, malformed


class AnthropicCompatibleBackend(HttpBackend):
    """适配 Anthropic 风格的 `/v1/messages` 接口。"""

    name = "anthropic_compatible"

    def _tool_endpoint(self) -> str:
        return self.api_base + "/v1/messages"

    def __init__(
        self,
        options: BackendOptions,
        anthropic_version: str = "2023-06-01",
    ):
        super().__init__(options)
        self.anthropic_version = anthropic_version

    def generate(
        self,
        prompt: str,
        on_chunk: Callable[[str], None] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: ToolChoice | None = None,
        messages: list[dict[str, Any]] | None = None,
        thinking_disabled: bool = False,
    ) -> ModelResponse:
        """Call the Anthropic-compatible messages endpoint.

        text 协议（``messages`` 为 None）：单条 ``user`` 消息承载整段 prompt，行为不变。
        native 协议（``messages`` 非空）：保留第一轮真实发送的 ``user=prompt``，再接结构化
        IR 翻出的 assistant(tool_use)/user(tool_result) 序列。不能把同一 prompt 在续轮改成
        system；否则历史失去原始 user turn，严格 OpenAI chat template 会拒绝工具结果续轮。
        """
        return self._generate_request(
            prompt,
            on_chunk=on_chunk,
            tools=tools,
            tool_choice=tool_choice,
            messages=messages,
            thinking_disabled=thinking_disabled,
        )

    def generate_structured(
        self,
        prompt: str,
        *,
        response_schema: dict[str, Any],
        messages: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        """Force one non-executable tool block as the provider-native JSON envelope."""

        tool_name = "my_agent_structured_output"
        structured_prompt = (
            "MANDATORY OUTPUT CONTRACT: Call the provided my_agent_structured_output tool "
            "exactly once. Do not answer with prose, Markdown, XML, or a textual tool-call "
            "imitation.\n\n" + prompt
        )
        schema_tool = {
            "name": tool_name,
            "description": (
                "Required non-executable response envelope. Call exactly once; never answer in prose."
            ),
            "input_schema": response_schema,
        }
        for attempt in range(2):
            request_prompt = structured_prompt
            if attempt:
                request_prompt += (
                    "\n\nThe previous provider response ignored the mandatory structured channel. "
                    "Retry by calling my_agent_structured_output exactly once and emit no text."
                )
            response = self._generate_request(
                request_prompt,
                tools=[schema_tool],
                tool_choice=ToolChoice.specific(tool_name),
                messages=messages,
                stream_response=False,
                temperature=0.0,
                # LLM: 强制工具信封不需要推理；部分兼容端点(如 工具运行时 zen)在思考模式下拒绝强制 tool_choice，
                # 显式关闭 thinking 可同时满足该约束并节省结构化调用的延迟与 token。
                thinking_disabled=True,
            )
            blocks = [
                block
                for block in response.tool_use_blocks
                if isinstance(block, dict) and str(block.get("name") or "") == tool_name
            ]
            if len(blocks) == 1 and isinstance(blocks[0].get("input"), dict):
                return ModelResponse(
                    text=json.dumps(blocks[0]["input"], ensure_ascii=False),
                    backend=response.backend,
                    usage=dict(response.usage),
                    stop_reason=response.stop_reason,
                )
        raise ProviderResponseError(
            "Anthropic-compatible structured response did not return one valid schema block",
            error_code="MODEL_SCHEMA_INVALID",
        )

    # LLM: Auxiliary JSON calls share the normal Anthropic transport but may lower only this request's output cap.
    # 函数用途: 给聚焦判读等短 JSON 请求设置有界输出，不修改主会话的全局 max_tokens。
    def generate_json(
        self,
        prompt: str,
        *,
        max_tokens: int | None = None,
        messages: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        return self._generate_request(
            prompt,
            messages=messages,
            max_output_tokens=max_tokens,
        )

    # LLM: This is the single Anthropic payload builder for regular and bounded auxiliary calls.
    # 函数用途: 组装并发送 Anthropic-compatible 请求，保证两类入口只在本次输出上限上有差别。
    def _generate_request(
        self,
        prompt: str,
        *,
        on_chunk: Callable[[str], None] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: ToolChoice | None = None,
        messages: list[dict[str, Any]] | None = None,
        max_output_tokens: int | None = None,
        stream_response: bool | None = None,
        temperature: float | None = None,
        thinking_disabled: bool = False,
    ) -> ModelResponse:
        payload: dict[str, Any] = {
            "model": self.model_name,
            "max_tokens": _bounded_output_tokens(
                self.max_tokens,
                max_output_tokens,
            ),
            "temperature": self.temperature if temperature is None else float(temperature),
        }
        if thinking_disabled:
            # LLM: 兼容 Anthropic 官方 thinking 参数；不识别该字段的端点(如 MiniMax)静默忽略。
            payload["thinking"] = {"type": "disabled"}
        if messages:
            if prompt:
                payload["messages"] = [{"role": "user", "content": prompt}, *messages]
            else:
                payload["messages"] = messages
        else:
            payload["messages"] = [{"role": "user", "content": prompt}]
        selected_tools = _tools_for_choice(tools, tool_choice)
        if selected_tools:
            from .tool_protocol_adapter import anthropic_tool_choice

            payload["tools"] = selected_tools
            payload["tool_choice"] = anthropic_tool_choice(tool_choice or ToolChoice.auto())
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            # 兼容只认 x-api-key 的 Anthropic 兼容端点(如 工具运行时.ai/zen:Authorization: Bearer
            # 返回 403 Missing API key,x-api-key 才通过)。两头发,主流端点都接受。
            "x-api-key": self.api_key,
            "anthropic-version": self.anthropic_version,
        }
        use_stream = self.stream_enabled if stream_response is None else stream_response
        if use_stream:
            return self._generate_stream(payload, headers, on_chunk=on_chunk)
        return self._generate_non_stream(payload, headers)

    # LLM: 非流式 Anthropic 响应必须同时保留可见正文、规范化工具调用和可安全回放的完整 assistant 块；三者不能互相替代。
    # 函数用途: 请求一次非流式 Anthropic-compatible 响应，并整理成运行时统一结果。
    def _generate_non_stream(
        self, payload: dict[str, Any], headers: dict[str, str]
    ) -> ModelResponse:
        obj: dict[str, Any] = {}
        text = ""
        blocks: list[dict[str, Any]] = []
        assistant_blocks: list[dict[str, Any]] = []
        for attempt in range(2):
            obj = self.request_json("/v1/messages", payload, headers)
            try:
                text = _anthropic_text_from_response(obj)
                blocks = _anthropic_tool_use_blocks(obj)
                assistant_blocks = _anthropic_assistant_content_blocks(obj)
            except Exception as exc:
                raise ProviderResponseError(
                    f"无法解析 Anthropic-compatible 响应: {_response_preview(obj)}"
                ) from exc
            if _incomplete_stop_reason(obj.get("stop_reason")):
                # 截断：保留文本、丢弃工具块、标记 truncated（工具循环继续）。
                return ModelResponse(
                    text=text,
                    backend=self.name,
                    usage=usage_dict(obj.get("usage")),
                    tool_use_blocks=[],
                    assistant_content_blocks=[],
                    truncated=True,
                    stop_reason=str(obj.get("stop_reason") or ""),
                )
            if text or blocks or attempt > 0 or not _anthropic_has_thinking_without_text(obj):
                break
        # 原生 tool_use 下模型可能只回 tool_use 块、没有文本，这种是合法的，不报空响应。
        if not text and not blocks:
            raise ProviderResponseError(
                f"Anthropic-compatible 响应没有文本内容: {_response_preview(obj)}",
                error_code="MODEL_EMPTY_RESPONSE",
            )
        return ModelResponse(
            text=text,
            backend=self.name,
            usage=usage_dict(obj.get("usage")),
            tool_use_blocks=blocks,
            assistant_content_blocks=assistant_blocks,
            stop_reason=str(obj.get("stop_reason") or ""),
        )

    # LLM: 流式 Anthropic 响应由 StreamCompletion 带回完整有序 assistant 块；thinking 块不得混入 on_chunk 或用户正文。
    # 函数用途: 收集一次流式 Anthropic-compatible 响应，并保留下一轮工具调用所需的内部历史。
    def _generate_stream(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
        on_chunk: Callable[[str], None] | None = None,
    ) -> ModelResponse:
        """Parse Anthropic SSE and retry the same stream path once when no text is visible."""
        text, usage, blocks, completion = "", {}, [], StreamCompletion()
        for attempt in range(2):
            text, usage, blocks, completion = self._stream_text_once(payload, headers, on_chunk)
            if _incomplete_stop_reason(completion.stop_reason):
                # 截断：保留文本、丢弃工具块、标记 truncated（工具循环下一轮继续，
                # harness assembler 同款：截断时丢弃全部 tool-call 块）。
                return ModelResponse(
                    text=text,
                    backend=self.name,
                    usage=usage,
                    tool_use_blocks=[],
                    assistant_content_blocks=[],
                    truncated=True,
                    stop_reason=completion.stop_reason,
                )
            if text or blocks or attempt > 0:
                break
        # 同非流式：只回 tool_use 块、无文本也合法，不报空响应。
        if not text and not blocks:
            raise ProviderResponseError(
                "Anthropic-compatible 流式响应没有文本内容",
                error_code="MODEL_EMPTY_RESPONSE",
            )
        return ModelResponse(
            text=text,
            backend=self.name,
            usage=usage,
            tool_use_blocks=blocks,
            assistant_content_blocks=list(completion.assistant_content_blocks),
            truncated=completion.truncated,
            stop_reason=completion.stop_reason,
        )

    def _stream_text_once(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
        on_chunk: Callable[[str], None] | None,
    ) -> tuple[str, dict[str, Any], list[dict[str, Any]], StreamCompletion]:
        lines = self.request_stream_iter if on_chunk is not None else self.request_stream
        return collect_anthropic_stream_with_completion(
            lines("/v1/messages", payload, headers), on_chunk=on_chunk
        )


def _tools_for_choice(
    tools: list[dict[str, Any]] | None,
    choice: ToolChoice | None,
) -> list[dict[str, Any]]:
    selected = list(tools or ())
    if choice is None or choice.mode == "auto":
        return selected
    if choice.mode == "none":
        return []
    if not selected:
        raise ValueError(f"tool_choice={choice.mode} requires a non-empty tools surface")
    names = {str(tool.get("name") or "").strip() for tool in selected if isinstance(tool, dict)}
    if choice.mode == "specific" and choice.tool_name not in names:
        raise ValueError(f"specific tool_choice is outside provider surface: {choice.tool_name}")
    return selected


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _anthropic_text_from_response(obj: dict[str, Any]) -> str:
    parts = obj.get("content", [])
    text = "".join(
        part.get("text", "")
        for part in parts
        if isinstance(part, dict) and part.get("type") in (None, "text")
    )
    if not text and "completion" in obj:
        text = obj["completion"]
    return str(text or "")


def _anthropic_tool_use_blocks(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract type==tool_use blocks from a non-stream Anthropic response.

    Each block is normalized to ``{"id","name","input"}``; ``input`` defaults
    to an empty dict when absent or malformed.
    """
    parts = obj.get("content", [])
    if not isinstance(parts, list):
        return []
    blocks: list[dict[str, Any]] = []
    for part in parts:
        if not isinstance(part, dict) or part.get("type") != "tool_use":
            continue
        tool_input = part.get("input")
        blocks.append(
            {
                "id": str(part.get("id", "") or ""),
                "name": str(part.get("name", "") or ""),
                "input": tool_input if isinstance(tool_input, dict) else {},
            }
        )
    return blocks


# LLM: 该白名单是 Anthropic 响应块进入下一轮请求的唯一边界；禁止原样回放未来新增的输出专用字段。
# 函数用途: 从非流式响应中按原顺序提取可安全回放的 thinking、text、redacted_thinking 和 tool_use 块。
def _anthropic_assistant_content_blocks(obj: dict[str, Any]) -> list[dict[str, Any]]:
    parts = obj.get("content", [])
    if not isinstance(parts, list):
        return []
    blocks: list[dict[str, Any]] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        block_type = str(part.get("type") or ("text" if "text" in part else ""))
        if block_type == "text":
            blocks.append({"type": "text", "text": str(part.get("text") or "")})
            continue
        if block_type == "thinking":
            block = {"type": "thinking", "thinking": str(part.get("thinking") or "")}
            signature = part.get("signature")
            if isinstance(signature, str) and signature:
                block["signature"] = signature
            blocks.append(block)
            continue
        if block_type == "redacted_thinking":
            data = part.get("data")
            if isinstance(data, str) and data:
                blocks.append({"type": "redacted_thinking", "data": data})
            continue
        if block_type == "tool_use":
            tool_input = part.get("input")
            blocks.append(
                {
                    "type": "tool_use",
                    "id": str(part.get("id") or ""),
                    "name": str(part.get("name") or ""),
                    "input": tool_input if isinstance(tool_input, dict) else {},
                }
            )
    return blocks


def _anthropic_has_thinking_without_text(obj: dict[str, Any]) -> bool:
    parts = obj.get("content", [])
    if not isinstance(parts, list):
        return False
    return any(isinstance(part, dict) and "thinking" in part for part in parts)


def _response_preview(obj: object, *, max_chars: int = 1000) -> str:
    text = str(obj)
    return text if len(text) <= max_chars else text[:max_chars] + "... [truncated]"


def get_backend(name: str, config: Any | None = None) -> BaseBackend:
    """Resolve a configured backend name to a backend adapter instance."""

    if name == "echo":
        return EchoBackend()
    if config is None:
        raise ValueError("真实模型后端需要传入 config。")

    common = BackendOptions(
        api_base=config.api_base,
        api_key=config.api_key,
        model_name=config.model_name,
        request_timeout=config.request_timeout,
        max_tokens=config.max_tokens,
        context_window_tokens=getattr(config, "model_context_window_tokens", 0),
        temperature=float(config.temperature),
        stream_enabled=getattr(config, "stream_enabled", True),
    )

    if name == "openai_compatible":
        return OpenAICompatibleBackend(common)
    if name == "anthropic_compatible":
        return AnthropicCompatibleBackend(
            common,
            anthropic_version=config.anthropic_version,
        )

    raise ValueError(
        "未知模型后端: %s。当前内置 echo / openai_compatible / anthropic_compatible。" % name
    )
