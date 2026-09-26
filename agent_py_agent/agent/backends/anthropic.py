# LLM: 此模块独占 Messages 转换；只读出站投影与真实发送共用组包，保持缓存、思考、工具及错误合同，联合 native IR 回归。
# 模块用途: 调用 Messages 接口并规范化流式或完整响应，保留历史、用量和请求局部控制。
from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from ..tooling.runtime_contracts import ToolChoice
from .anthropic_prompt_cache import (
    anthropic_messages_with_optional_cache,
    anthropic_prompt_cache_projection,
)
from .base import BackendOptions, ModelResponse, ProviderRequestOptions
from .errors import (
    ProviderResponseError,
)
from .http import HttpBackend, bounded_output_tokens, request_stream_lines
from .provider_headers import endpoint_parts
from .reasoning_control import reasoning_payload_fields
from .response_completion import (
    has_reasoning_content,
    incomplete_response_fields,
    response_preview,
    truncated_tool_names,
    without_tool_blocks,
)
from .stream_parsers import StreamCompletion
from .tool_protocol_adapter import tools_for_choice
from .usage_metadata import (
    collect_anthropic_stream_with_completion,
    usage_dict,
)


# LLM: Messages 单次请求携带全部局部覆盖；冻结对象不改后端配置，需联合采样、流观察器和结构化摘要回归。
# 类用途: 汇总一轮 Messages 请求的输入、工具和可选控制，避免长参数列表隐藏调用依赖。
@dataclass(frozen=True)
class _AnthropicGenerateRequest:
    prompt: str
    system_instruction: str = ""
    on_chunk: Callable[[str], None] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: ToolChoice | None = None
    messages: list[dict[str, Any]] | None = None
    max_output_tokens: int | None = None
    stream_response: bool | None = None
    temperature: float | None = None
    thinking_disabled: bool = False
    on_thinking_delta: Callable[[str], None] | None = None
    on_tool_input_progress: Callable[[dict[str, object]], None] | None = None
    first_event_timeout_seconds: float | None = None
    # 智能程度档位（low/medium/high/max 或空串），按 self.reasoning_control 换算成 thinking/output_config。
    reasoning_effort: str = ""


# LLM: Messages 的缓存、原生块与流事件由此适配；能力端点使用 HTTP 同源规则，联合思考、工具和 OAuth 测试。
# 类用途: 发送 Messages 协议请求，并统一返回正文、思考、工具调用及用量。
class AnthropicCompatibleBackend(HttpBackend):
    """适配 Anthropic 风格的 `/v1/messages` 接口。"""

    name = "anthropic_compatible"
    supports_tool_input_progress = True
    supports_thinking_completion = True

    # LLM: 探针与 HTTP/OAuth 共用 endpoint_parts，保留代理前缀及完整接口，不重复追加版本，不授予权限。
    # 函数用途: 只读计算 Messages 实际端点，供成功和失败探针记录来源。
    def _tool_endpoint(self) -> str:
        return "".join(endpoint_parts(self.api_base, "/v1/messages"))

    # LLM: Messages 复用公共冻结连接选项，仅附加明确协议版本；需核对工厂及 OAuth 构造调用。
    # 函数用途: 初始化后端与版本头配置，不发网络请求。
    def __init__(
        self,
        options: BackendOptions,
        anthropic_version: str = "2023-06-01",
    ):
        super().__init__(options)
        self.anthropic_version = anthropic_version

    # LLM: Anthropic-compatible 声明工具参数 delta 与 thinking block-stop 两项能力；
    # callback 只投影 typed 展示边界，不能改变 payload、tool choice、历史或响应解析。
    # 函数用途: 调用 Anthropic messages，并可选上报思考终态和工具参数生成进度。
    def generate(
        self,
        prompt: str,
        on_chunk: Callable[[str], None] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: ToolChoice | None = None,
        messages: list[dict[str, Any]] | None = None,
        on_thinking_delta: Callable[[str], None] | None = None,
        on_tool_input_progress: Callable[[dict[str, object]], None] | None = None,
        request_options: ProviderRequestOptions | None = None,
    ) -> ModelResponse:
        """Call the Anthropic-compatible messages endpoint.

        text 协议（``messages`` 为 None）：单条 ``user`` 消息承载整段 prompt，行为不变。
        native 协议（``messages`` 非空）：保留第一轮真实发送的 ``user=prompt``，再接结构化
        IR 翻出的 assistant(tool_use)/user(tool_result) 序列。不能把同一 prompt 在续轮改成
        system；否则历史失去原始 user turn，严格 OpenAI chat template 会拒绝工具结果续轮。
        独立 ``request_options.system_instruction`` 始终走 Anthropic 顶层 system 字段，不改变上述 user 历史。
        思考 observer 的 ``complete`` 只接收完成块正文；工具参数 callback 只接收累计字符计数，均不参与工具执行。
        """
        provider_options = request_options or ProviderRequestOptions()
        return self._generate_request(
            _AnthropicGenerateRequest(
                prompt,
                system_instruction=provider_options.system_instruction,
                on_chunk=on_chunk,
                tools=tools,
                tool_choice=tool_choice,
                messages=messages,
                thinking_disabled=provider_options.thinking_disabled,
                on_thinking_delta=on_thinking_delta,
                on_tool_input_progress=on_tool_input_progress,
                first_event_timeout_seconds=provider_options.first_event_timeout_seconds,
                reasoning_effort=provider_options.reasoning_effort,
            )
        )

    # LLM: Messages 的强制工具仅是不可执行的 schema 信封；保留既有两次请求预算，需同步结构化摘要测试。
    # 函数用途: 请求一个符合 schema 的结构化结果，校验唯一信封并转为 JSON 正文。
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
                _AnthropicGenerateRequest(
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
            _AnthropicGenerateRequest(
                prompt,
                messages=messages,
                max_output_tokens=max_tokens,
            )
        )

    # LLM: 普通 generate 的只读出站投影复用原组包；不读认证、不探针、不记录诊断，也不证明窗口或工具能力。
    # 函数用途: 返回本后端实际发送的 JSON 内容供宿主验证，未知重写实现返回 None，输入与结果不共享可变容器。
    def project_generate_payload(
        self, prompt: str, *, tools: list[dict[str, Any]] | None = None,
        tool_choice: ToolChoice | None = None, messages: list[dict[str, Any]] | None = None,
        request_options: ProviderRequestOptions | None = None,
    ) -> dict[str, Any] | None:
        if (type(self).generate is not AnthropicCompatibleBackend.generate
                or type(self)._generate_request is not AnthropicCompatibleBackend._generate_request
                or type(self)._request_payload is not AnthropicCompatibleBackend._request_payload
                or type(self)._generate_stream is not AnthropicCompatibleBackend._generate_stream
                or type(self)._generate_non_stream is not AnthropicCompatibleBackend._generate_non_stream):
            return None
        options = request_options or ProviderRequestOptions()
        payload = self._request_payload(_AnthropicGenerateRequest(
            prompt=prompt, tools=tools, tool_choice=tool_choice, messages=messages,
            system_instruction=options.system_instruction, thinking_disabled=options.thinking_disabled,
            reasoning_effort=options.reasoning_effort,
        ))
        if self.stream_enabled:
            payload["stream"] = True
        return deepcopy(payload)

    # LLM: 关闭思考优先（兼容 Anthropic 官方 thinking 参数，不识别的端点如 MiniMax 静默忽略）；否则按档位经
    #   reasoning_control 统一换算，预算夹在 max_tokens 以内；都没有时不写任何思考字段。
    # 函数用途: 返回本次 Messages 请求要合入的思考/智能程度字段。
    def _thinking_fields(self, request: _AnthropicGenerateRequest, max_tokens: int) -> dict[str, Any]:
        if request.thinking_disabled:
            return {"thinking": {"type": "disabled"}}
        if request.reasoning_effort:
            return reasoning_payload_fields(self.reasoning_control, request.reasoning_effort, "anthropic", max_tokens)
        return {}

    # LLM: 普通发送、短JSON和只读投影共用组包；媒体按同预算有界读盘及核对哈希，不执行传输或修改canonical引用。
    # 函数用途: 按原缓存、采样和工具合同构造内容，在此将已验证媒体引用展开为临时字节。
    def _request_payload(self, request: _AnthropicGenerateRequest) -> dict[str, Any]:
        from ..conversation.input_media import project_input_media

        payload: dict[str, Any] = {
            "model": self.model_name,
            "max_tokens": bounded_output_tokens(
                self.max_tokens,
                request.max_output_tokens,
            ),
        }
        if request.temperature is not None or self.temperature_explicit:
            payload["temperature"] = (
                self.temperature if request.temperature is None else float(request.temperature)
            )
        if self.top_p is not None:
            payload["top_p"] = self.top_p
        cache_projection = anthropic_prompt_cache_projection(
            system_instruction=request.system_instruction,
            prompt=request.prompt,
            cache_enabled=self.prompt_cache_enabled,
            native_messages=request.messages is not None,
        )
        if cache_projection.system:
            payload["system"] = cache_projection.system
        payload.update(self._thinking_fields(request, payload["max_tokens"]))
        selected_tools = tools_for_choice(request.tools, request.tool_choice)
        payload["messages"], selected_tools = anthropic_messages_with_optional_cache(
            prompt=cache_projection.prompt,
            messages=project_input_media(request.messages, self.input_media_max_bytes),
            tools=selected_tools,
            cache_enabled=self.prompt_cache_enabled,
            stable_user_prefix=cache_projection.stable_user_prefix,
            stable_system_cache_active=(cache_projection.stable_system_cache_active),
            structured_native_layout_active=(cache_projection.structured_native_layout_active),
        )
        from ..conversation.input_media import provider_media_messages

        payload["messages"] = provider_media_messages(payload["messages"])
        if selected_tools:
            from .tool_protocol_adapter import anthropic_tool_choice

            payload["tools"] = selected_tools
            payload["tool_choice"] = anthropic_tool_choice(request.tool_choice or ToolChoice.auto())
        return payload

    # LLM: 真正发送仅消费同源 payload；认证头、网络、流观察器和错误处理继续由原入口唯一执行。
    # 函数用途: 发送 Messages 请求，未设置温度沿用服务端默认，保留摘要覆盖及请求局部时限。
    def _generate_request(self, request: _AnthropicGenerateRequest) -> ModelResponse:
        payload = self._request_payload(request)
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            # 兼容只认 x-api-key 的 Anthropic 兼容端点(如 工具运行时.ai/zen:Authorization: Bearer
            # 返回 403 Missing API key,x-api-key 才通过)。两头发,主流端点都接受。
            "x-api-key": self.api_key,
            "anthropic-version": self.anthropic_version,
        }
        use_stream = (
            self.stream_enabled if request.stream_response is None else request.stream_response
        )
        if use_stream:
            return self._generate_stream(
                payload,
                headers,
                on_chunk=request.on_chunk,
                on_thinking_delta=request.on_thinking_delta,
                on_tool_input_progress=request.on_tool_input_progress,
                first_event_timeout_seconds=request.first_event_timeout_seconds,
            )
        return self._generate_non_stream(payload, headers)

    # LLM: 非流式响应按同一合同保留正文/思考；合法思考不得隐藏重试，坏参数仍整轮零执行。
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
                blocks, malformed, dropped_names = _anthropic_tool_use_blocks(obj)
                assistant_blocks = _anthropic_assistant_content_blocks(obj)
            except Exception as exc:
                raise ProviderResponseError(
                    f"无法解析 Anthropic-compatible 响应: {response_preview(obj)}"
                ) from exc
            incomplete = incomplete_response_fields(
                str(obj.get("stop_reason") or ""),
                incomplete_reason="invalid_tool_arguments" if malformed else "",
            )
            if incomplete:
                return ModelResponse(
                    text=text,
                    backend=self.name,
                    usage=usage_dict(obj.get("usage")),
                    tool_use_blocks=[],
                    assistant_content_blocks=without_tool_blocks(assistant_blocks),
                    truncated_tool_names=truncated_tool_names(dropped_names),
                    **incomplete,
                )
            if (
                text
                or blocks
                or has_reasoning_content(assistant_blocks)
                or attempt > 0
                or not _anthropic_has_thinking_without_text(obj)
            ):
                break
        # 原生 tool_use 下模型可能只回 tool_use 块、没有文本，这种是合法的，不报空响应。
        if not text and not blocks and not has_reasoning_content(assistant_blocks):
            raise ProviderResponseError(
                f"Anthropic-compatible 响应没有文本内容: {response_preview(obj)}",
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

    # LLM: 流式 Anthropic 响应由 StreamCompletion 带回完整有序 assistant 块；thinking delta/block-stop 与
    # 工具参数计数各走显式 observer；仅思考不隐藏重试，EOF/坏参数不执行工具，保留已闭合内容。
    # 函数用途: 收集流式响应、保留内部历史，并按供应商块顺序投影思考和工具参数进度。
    def _generate_stream(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
        on_chunk: Callable[[str], None] | None = None,
        on_thinking_delta: Callable[[str], None] | None = None,
        on_tool_input_progress: Callable[[dict[str, object]], None] | None = None,
        first_event_timeout_seconds: float | None = None,
    ) -> ModelResponse:
        """保留已生成的思考；真正空流才沿既有预算重试一次。"""
        text, usage, blocks, completion = "", {}, [], StreamCompletion()
        for attempt in range(2):
            text, usage, blocks, completion = self._stream_text_once(
                payload,
                headers,
                on_chunk,
                on_thinking_delta=on_thinking_delta,
                on_tool_input_progress=on_tool_input_progress,
                first_event_timeout_seconds=first_event_timeout_seconds,
            )
            incomplete = incomplete_response_fields(
                completion.stop_reason, incomplete_reason=completion.incomplete_reason
            )
            if incomplete:
                return ModelResponse(
                    text=text,
                    backend=self.name,
                    usage=usage,
                    tool_use_blocks=[],
                    assistant_content_blocks=without_tool_blocks(
                        list(completion.assistant_content_blocks)
                    ),
                    truncated_tool_names=truncated_tool_names(completion.tool_names),
                    **incomplete,
                )
            if (
                text
                or blocks
                or has_reasoning_content(list(completion.assistant_content_blocks))
                or attempt > 0
            ):
                break
        # 同非流式：只回 tool_use 块、无文本也合法，不报空响应。
        if (
            not text
            and not blocks
            and not has_reasoning_content(list(completion.assistant_content_blocks))
        ):
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

    # LLM: 单次物理流必须把 delta、block-stop 与参数 observer 一起交给 collector；重试边界
    # 仍由上层 _generate_stream 掌权，不能在此创建工具、展示重放或重试状态。
    # 函数用途: 读取并收集一条 Anthropic SSE 响应，并保留供应商原始块顺序。
    def _stream_text_once(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
        on_chunk: Callable[[str], None] | None,
        *,
        on_thinking_delta: Callable[[str], None] | None = None,
        on_tool_input_progress: Callable[[dict[str, object]], None] | None = None,
        first_event_timeout_seconds: float | None = None,
    ) -> tuple[str, dict[str, Any], list[dict[str, Any]], StreamCompletion]:
        lines = self.request_stream_iter if on_chunk is not None else self.request_stream
        return collect_anthropic_stream_with_completion(
            request_stream_lines(
                lines,
                "/v1/messages",
                payload,
                headers,
                first_event_timeout_seconds,
            ),
            on_chunk=on_chunk,
            on_thinking_delta=on_thinking_delta,
            on_tool_input_progress=on_tool_input_progress,
        )


# LLM: Messages 正文提取不混入思考或工具块；兼容 completion 字段的现有行为须由协议测试约束。
# 函数用途: 从非流式响应提取公开正文，不改写原响应。
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


# LLM: 原生 input 必须是对象；缺失/坏值只报告协议失败，不转成可以执行的空参数。
# 函数用途: 读取 Anthropic 非流式工具块，并返回整轮是否存在损坏输入。
def _anthropic_tool_use_blocks(obj: dict[str, Any]) -> tuple[list[dict[str, Any]], bool, list[str]]:
    parts = obj.get("content", [])
    if not isinstance(parts, list):
        return [], False, []
    blocks: list[dict[str, Any]] = []
    malformed = False
    dropped: list[str] = []
    for part in parts:
        if not isinstance(part, dict) or part.get("type") != "tool_use":
            continue
        tool_input = part.get("input")
        if not isinstance(tool_input, dict):
            malformed = True
            dropped.append(str(part.get("name", "") or ""))
            continue
        blocks.append(
            {
                "id": str(part.get("id", "") or ""),
                "name": str(part.get("name", "") or ""),
                "input": tool_input,
            }
        )
    return blocks, malformed, dropped


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


# LLM: Messages 此谓词只参与既有空内容请求预算；不能把它当作有效思考或任务完成事实。
# 函数用途: 识别响应中是否出现 thinking 字段，供空响应分支区分协议形状。
def _anthropic_has_thinking_without_text(obj: dict[str, Any]) -> bool:
    parts = obj.get("content", [])
    if not isinstance(parts, list):
        return False
    return any(isinstance(part, dict) and "thinking" in part for part in parts)
