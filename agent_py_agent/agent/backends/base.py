
from __future__ import annotations

"""模型后端适配层。

这一层的作用很像'翻译器'：
- 上层智能体只知道自己要把 prompt 发给模型。
- 下层不同厂商的接口格式却不一样。

所以这里把不同后端都包装成统一接口，避免核心调度器里到处写 if/else。
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..settings.defaults import DEFAULT_MODEL_MAX_TOKENS
from .errors import ProviderResponseError
from .gateway_helpers import GatewayRequest, post_json, post_stream, post_stream_iter
from .model_metadata import ProviderMetadataOptions, discover_provider_model_metadata
from .stream_parsers import StreamCompletion
from .usage_metadata import (
    collect_anthropic_stream_with_completion,
    collect_openai_stream_with_completion,
    openai_stream_payload,
    usage_dict,
)

# 文本响应自动续写(§3 报满不早停):触发的 stop_reason 集合与有界轮数。
# 每轮续写都带完整 max_tokens 额度;3 轮上限足够把"前 40 条有、后 10 条空"这类
# 枚举长输出补齐,又防上游异常时无限拉锯。
_TEXT_CONTINUATION_STOP_REASONS = frozenset({"max_tokens", "length"})
_MAX_TEXT_CONTINUATION_ROUNDS = 3


def _merged_usage(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    """续写各段 usage 合并:数值累加(token 记账不少算),其余字段取后者。"""
    merged = dict(base)
    for key, value in (extra or {}).items():
        current = merged.get(key)
        if isinstance(value, (int, float)) and isinstance(current, (int, float)):
            merged[key] = current + value
        else:
            merged[key] = value
    return merged


def _bounded_output_tokens(configured: int, requested: int | None) -> int:
    if requested is None:
        return configured
    return max(1, min(configured, int(requested)))


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
    # native 流式响应在 message_stop 前 EOF，或 stop_reason∈{max_tokens,length} 且仍有
    # 未闭合的 tool_use 参数缓冲 → True：这次响应（含 tool_use 参数 JSON）疑似被截断。
    # 默认 False；非流式与 text 协议恒 False，且只有 native 恢复/降级逻辑消费它，零回归。
    truncated: bool = False
    # 上游返回的原始 stop_reason(观测/审计用;自动续写后为最后一段的 stop_reason)。
    stop_reason: str = ""

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
    messages: list[dict[str, Any]] | None = None
    response_schema: dict[str, Any] | None = None
    json_object: bool = False
    max_output_tokens: int | None = None


class BaseBackend:
    """所有后端适配器都要实现的基类接口。"""

    name = "base"

    def provider_context_window_tokens(self) -> int:
        """Return provider-advertised context capacity, or zero when unavailable."""
        return 0

    def generate(
        self,
        prompt: str,
        on_chunk: Callable[[str], None] | None = None,
        tools: list[dict[str, Any]] | None = None,
        messages: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        """Generate one assistant response for the supplied prompt.

        ``tools`` carries an Anthropic-style tools schema for native tool_use
        (tool_protocol=native). Backends that do not support it ignore it and
        keep the text protocol; bypass callers omit it for unchanged behavior.

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

    def generate(
        self,
        prompt: str,
        on_chunk: Callable[[str], None] | None = None,
        tools: list[dict[str, Any]] | None = None,
        messages: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        del tools, messages  # echo backend never speaks native tool_use
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
        self._provider_context_window_cache: int | None = None
        self.model_metadata: dict[str, Any] = {}
        self.temperature = float(options.temperature)
        self.stream_enabled = bool(options.stream_enabled)

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

    def request_stream_iter(
        self, path: str, payload: dict[str, Any], headers: dict[str, str]
    ):
        """Send a streaming request and yield data lines as they arrive."""
        yield from post_stream_iter(self._gateway_request(path, payload, headers))

    def _gateway_request(self, path: str, payload: dict[str, Any], headers: dict[str, str]) -> GatewayRequest:
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

    def generate(
        self,
        prompt: str,
        on_chunk: Callable[[str], None] | None = None,
        tools: list[dict[str, Any]] | None = None,
        messages: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        """Call the OpenAI-compatible chat completion endpoint."""
        return self._generate(
            _OpenAIGenerateRequest(
                prompt=prompt,
                on_chunk=on_chunk,
                tools=tools,
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
        if request.tools:
            payload["tools"] = _openai_tools_from_native(request.tools)
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
        return _openai_non_stream_response(obj, backend_name=self.name, tools_requested=bool(request.tools))

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
        raise ProviderResponseError(f"无法解析 OpenAI-compatible 响应: {_response_preview(obj)}") from exc
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
        stop_reason=str(choice.get("finish_reason") or ""),
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
    blocks = [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []
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
            tool_input = arguments if isinstance(arguments, dict) else json.loads(str(arguments or "{}"))
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
        messages: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        """Call the Anthropic-compatible messages endpoint.

        text 协议（``messages`` 为 None）：单条 ``user`` 消息承载整段 prompt，行为不变。
        native 协议（``messages`` 非空）：保留第一轮真实发送的 ``user=prompt``，再接结构化
        IR 翻出的 assistant(tool_use)/user(tool_result) 序列。不能把同一 prompt 在续轮改成
        system；否则历史失去原始 user turn，严格 OpenAI chat template 会拒绝工具结果续轮。
        """
        payload: dict[str, Any] = {
            "model": self.model_name,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        if messages:
            if prompt:
                payload["messages"] = [{"role": "user", "content": prompt}, *messages]
            else:
                payload["messages"] = messages
        else:
            payload["messages"] = [{"role": "user", "content": prompt}]
        if tools:
            payload["tools"] = tools
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "anthropic-version": self.anthropic_version,
        }
        if self.stream_enabled:
            return self._generate_stream(payload, headers, on_chunk=on_chunk)
        return self._generate_non_stream(payload, headers)

    def _generate_non_stream(self, payload: dict[str, Any], headers: dict[str, str]) -> ModelResponse:
        obj: dict[str, Any] = {}
        text = ""
        blocks: list[dict[str, Any]] = []
        for attempt in range(2):
            obj = self.request_json("/v1/messages", payload, headers)
            try:
                text = _anthropic_text_from_response(obj)
                blocks = _anthropic_tool_use_blocks(obj)
            except Exception as exc:
                raise ProviderResponseError(f"无法解析 Anthropic-compatible 响应: {_response_preview(obj)}") from exc
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
            stop_reason=str(obj.get("stop_reason") or ""),
        )

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
            if text or blocks or attempt > 0:
                break
        # 同非流式：只回 tool_use 块、无文本也合法，不报空响应。
        if not text and not blocks:
            raise ProviderResponseError(
                "Anthropic-compatible 流式响应没有文本内容",
                error_code="MODEL_EMPTY_RESPONSE",
            )
        text, usage, blocks, completion = self._continue_truncated_text(
            payload, headers, (text, usage, blocks, completion), on_chunk
        )
        return ModelResponse(
            text=text,
            backend=self.name,
            usage=usage,
            tool_use_blocks=blocks,
            truncated=completion.truncated,
            stop_reason=completion.stop_reason,
        )

    def _continue_truncated_text(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
        state: tuple[str, dict[str, Any], list[dict[str, Any]], StreamCompletion],
        on_chunk: Callable[[str], None] | None,
    ) -> tuple[str, dict[str, Any], list[dict[str, Any]], StreamCompletion]:
        """纯文本响应撞输出上限时自动续写(「报满不早停」的截断层根治)。

        真机实锤:逐条枚举型长输出稳定掉尾部 ~19%——stop_reason=max_tokens 的纯文本
        响应此前不被视为截断,半截答案被当完整交付。触发是纯结构化信号:
        stop_reason∈{max_tokens,length} 且无 tool_use 块且无半截工具参数缓冲
        (后者仍走 native 截断恢复,不在此续)。以已收文本作 assistant 预填续问、
        原文接续拼接;有界轮数;续写请求失败时保留已收文本按原状返回,绝不丢已有产出。
        """
        text, usage, blocks, completion = state
        base_messages = list(payload.get("messages") or [])
        rounds = 0
        while (
            rounds < _MAX_TEXT_CONTINUATION_ROUNDS
            and text
            and not blocks
            and not completion.open_tool_buffer
            and completion.stop_reason in _TEXT_CONTINUATION_STOP_REASONS
        ):
            rounds += 1
            continued = dict(payload)
            continued["messages"] = [*base_messages, {"role": "assistant", "content": text}]
            try:
                more_text, more_usage, blocks, completion = self._stream_text_once(continued, headers, on_chunk)
            except Exception:
                break  # 续写失败不毁已有产出:按已收内容返回(与不续写的现状一致)
            if not more_text and not blocks:
                break
            text += more_text
            usage = _merged_usage(usage, more_usage)
        return text, usage, blocks, completion

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
        "未知模型后端: %s。当前内置 echo / openai_compatible / anthropic_compatible。"
        % name
    )
