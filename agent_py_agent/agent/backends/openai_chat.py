# LLM: 此模块独占 Chat Completions 转换；只读出站投影与真实发送共用组包；八档换算和声明过滤归 reasoning_control，
#   本模块只传同次输出上限与冻结声明，保持历史、思考、工具、采样与用量合同。
# 模块用途: 调用 Chat 接口，转换消息和流式结果；网络、回调及显式启用的私有诊断写入均在既有边界内。
from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from ..prompting_parts.cache_layout import prompt_cache_layout
from ..tooling.runtime_contracts import ToolChoice
from .base import ModelResponse, ProviderRequestOptions
from .errors import (
    ProviderResponseError,
)
from .http import HttpBackend, bounded_output_tokens, request_stream_lines
from .provider_headers import endpoint_parts
from .reasoning_control import ReasoningPayloadLimits, reasoning_payload_fields
from .response_completion import (
    has_reasoning_content,
    incomplete_response_fields,
    response_preview,
    truncated_tool_names,
    without_tool_blocks,
)
from .stream_parsers import StreamCompletion
from .structured_output_mode import json_object_prompt
from .tool_protocol_adapter import tools_for_choice
from .usage_metadata import (
    collect_openai_stream_with_completion,
    openai_stream_payload,
    usage_dict,
)


# LLM: cabfix：流式观察回调与两个超时事实合成一个数据类，避免 _generate_stream 参数表
#   随每个新观察点增长（code-size 参数门禁也会拦）。
# 类用途: 携带一次 OpenAI 流式调用要用的观察回调和超时选项。
@dataclass(frozen=True)
class _StreamObservers:
    on_chunk: Callable[[str], None] | None = None
    on_thinking_delta: Callable[[str], None] | None = None
    on_tool_input_progress: Callable[[dict[str, object]], None] | None = None
    first_event_timeout_seconds: float | None = None
    total_deadline_seconds: float | None = None


# LLM: OpenAI 请求对象按 typed 字段保留 system 和请求级思考控制；子适配器只发送其协议支持的字段。
# 类用途: 汇总一次 OpenAI-compatible 调用的规则、用户输入、工具、思考开关和输出格式选项。
@dataclass(frozen=True)
class _OpenAIGenerateRequest:
    prompt: str
    system_instruction: str = ""
    on_chunk: Callable[[str], None] | None = None
    on_thinking_delta: Callable[[str], None] | None = None
    on_tool_input_progress: Callable[[dict[str, object]], None] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: ToolChoice | None = None
    messages: list[dict[str, Any]] | None = None
    response_schema: dict[str, Any] | None = None
    json_object: bool = False
    thinking_disabled: bool = False
    max_output_tokens: int | None = None
    # 压缩降档申请（configuration_update 项的档位），只有 Responses 适配器消费；默认空串不插项。
    reasoning_update_effort: str = ""
    first_event_timeout_seconds: float | None = None
    # cabfix：调用方给的本次请求绝对墙钟上界（秒）；None=不设总期限，主模型路径保持原语义。
    total_deadline_seconds: float | None = None
    # 智能程度用户档位或空串；off 已折为开关，两个新档按协议与声明换算，不能直接透传。
    reasoning_effort: str = ""


# LLM: Chat 出站诊断只在显式环境开关下写入；payload 含任务正文，调用方必须使用仓库外私有路径。
# 函数用途: 按需追加完整请求 payload 供协议排错，不保存认证头；写入失败不影响模型请求。
def dump_provider_payload(payload: dict[str, Any], *, path: str) -> None:
    import os

    target = str(os.environ.get("MY_AGENT_PROVIDER_DUMP") or "").strip()
    if not target:
        return
    try:
        record = {
            "path": path,
            "model": payload.get("model"),
            "thinking": payload.get("thinking"),
            "reasoning_effort": payload.get("reasoning_effort"),
            "has_tools": "tools" in payload,
            "tool_choice": payload.get("tool_choice"),
            "message_count": len(payload.get("messages") or []),
            "payload": payload,
        }
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        # 诊断绝不反噬主链路：任何写入失败都只放弃本次 dump。
        return


# LLM: Chat 适配器独占消息转换，能力端点使用 HTTP 同源规则；Responses 只复用稳定生成入口，联合协议及 OAuth 回归。
# 类用途: 把统一生成请求转换为 Chat Completions 网络调用并返回规范化响应。
class OpenAICompatibleBackend(HttpBackend):
    """适配 OpenAI-compatible `/chat/completions` 接口。"""

    name = "openai_compatible"
    # OpenAI-compatible chat 流没有统一 block-stop，但 reasoning_content 在首段正文或
    # 流结束时有确定边界；适配器会在该边界调用同一 typed complete observer。
    supports_thinking_completion = True
    supports_tool_input_progress = True

    # LLM: 探针与 HTTP/OAuth 共用 endpoint_parts，配置完整接口时不重复追加路径，不改变实际传输。
    # 函数用途: 只读计算 Chat Completions 实际端点，供成功和失败探针记录来源。
    def _tool_endpoint(self) -> str:
        return "".join(endpoint_parts(self.api_base, "/chat/completions"))

    # LLM: OpenAI-compatible 传输必须保持 system -> 规范会话/当前 user -> 原生工具历史的追加顺序；
    # reasoning 和工具参数计数是展示边界；请求开关与观察器必须转交组包器，不能丢失或影响执行。
    # 函数用途: 调用 chat/completions，传递宿主规则、本次思考开关和流式展示，不改变后续请求。
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
        """Call the OpenAI-compatible chat completion endpoint."""
        provider_options = request_options or ProviderRequestOptions()
        return self._generate(
            _OpenAIGenerateRequest(
                prompt=prompt,
                system_instruction=provider_options.system_instruction,
                on_chunk=on_chunk,
                on_thinking_delta=on_thinking_delta,
                on_tool_input_progress=on_tool_input_progress,
                tools=tools,
                tool_choice=tool_choice,
                messages=messages,
                thinking_disabled=provider_options.thinking_disabled,
                max_output_tokens=provider_options.max_output_tokens,
                reasoning_update_effort=provider_options.reasoning_update_effort,
                first_event_timeout_seconds=provider_options.first_event_timeout_seconds,
                total_deadline_seconds=provider_options.total_deadline_seconds,
                reasoning_effort=provider_options.reasoning_effort,
            )
        )

    # LLM: Chat 结构化输出仍经过同一请求组装与响应解析；schema 只约束输出，不授予工具权限。方式随 profile 冻结
    #   （self.structured_output，见 structured_output_mode.py）：native 发严格 json_schema；json_object 用供应商 JSON
    #   对象模式，并把 schema 写进提示，是否合规由调用方的严格解析判断。同步 test_structured_output_mode.py。
    # 函数用途: 发起结构化输出的模型请求，按模型声明选择 JSON Schema 或 JSON 对象格式。
    def generate_structured(
        self,
        prompt: str,
        *,
        response_schema: dict[str, Any],
        messages: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        """Use strict JSON schema, or JSON-object mode with the schema in the prompt when declared."""

        json_object = self.structured_output == "json_object"
        return self._generate(
            _OpenAIGenerateRequest(
                prompt=json_object_prompt(prompt, response_schema) if json_object else prompt,
                messages=messages,
                response_schema=None if json_object else response_schema,
                json_object=json_object,
            )
        )

    # LLM: Chat JSON 模式的输出限制属于本次请求；需同步摘要调用方，不能修改共享后端预算。
    # 函数用途: 发起 JSON 对象格式请求，并传递可选的本轮输出上限。
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

    # LLM: 普通 generate 投影复用 Chat 唯一组包；Responses 或自定义重写不能借继承冒充该发送语义。
    # 函数用途: 只读返回实际出站 JSON，不探针、不写诊断、不发网络，未知适配器返回 None。
    def project_generate_payload(
        self, prompt: str, *, tools: list[dict[str, Any]] | None = None,
        tool_choice: ToolChoice | None = None, messages: list[dict[str, Any]] | None = None,
        request_options: ProviderRequestOptions | None = None,
    ) -> dict[str, Any] | None:
        if (type(self).generate is not OpenAICompatibleBackend.generate
                or type(self)._generate is not OpenAICompatibleBackend._generate
                or type(self)._request_payload is not OpenAICompatibleBackend._request_payload
                or type(self)._generate_stream is not OpenAICompatibleBackend._generate_stream):
            return None
        options = request_options or ProviderRequestOptions()
        payload = self._request_payload(_OpenAIGenerateRequest(
            prompt=prompt, tools=tools, tool_choice=tool_choice, messages=messages,
            system_instruction=options.system_instruction, thinking_disabled=options.thinking_disabled,
            reasoning_effort=options.reasoning_effort,
        ))
        if self.stream_enabled:
            payload = openai_stream_payload(payload)
            payload["stream"] = True
        return deepcopy(payload)

    # LLM: 发送和预览共用唯一组包；媒体按预算读文件并校验hash后编码，不修改canonical历史或推断模态容量。
    #   历史先经 wire_contract.repair_native_messages 修整副本，转换后由 validate_chat_messages 复核，不合规在本地抛错不发送。
    #   智能程度只交唯一换算点，传冻结档位声明与本次输出上限；联合 test_reasoning_effort 和采样、工具历史测试。
    # 函数用途: 构造Chat历史、工具和输出格式，保留原思考优先级与声明过滤；媒体输入包含有界文件读取。
    def _request_payload(self, request: _OpenAIGenerateRequest) -> dict[str, Any]:
        from .sampling import chat_sampling_fields

        payload = {
            "model": self.model_name,
            "max_tokens": bounded_output_tokens(self.max_tokens, request.max_output_tokens),
        }
        payload.update(chat_sampling_fields(self.api_base, self.model_name, top_p=self.top_p,
            temperature=self.temperature, temperature_explicit=self.temperature_explicit,
            thinking_disabled=request.thinking_disabled))
        if request.thinking_disabled:
            # 参考 轻量运行时 的 thinkingFormat 适配：官方与 Zen/Go 都默认开思考，强制工具探针须显式关闭；
            # 不是通用 OpenAI 字段，因此只在已核对端点或显式声明了思考控制方式的模型上写入。
            _disable_thinking_for(payload, api_base=self.api_base)
            if self.reasoning_control in {"effort", "budget"}:
                payload["thinking"] = {"type": "disabled"}
        from ..conversation.input_media import project_input_media
        from .wire_contract import repair_native_messages, validate_chat_messages

        if request.messages is not None:
            payload["messages"] = _openai_messages_from_native(
                project_input_media(repair_native_messages(request.messages), self.input_media_max_bytes),
                initial_user_prompt=request.prompt,
                system_instruction=request.system_instruction,
            )
        else:
            payload["messages"] = _openai_messages_from_native(
                [],
                initial_user_prompt=request.prompt,
                system_instruction=request.system_instruction,
            )
        validate_chat_messages(payload["messages"])
        tools = tools_for_choice(request.tools, request.tool_choice)
        if tools:
            from .tool_protocol_adapter import openai_tool_choice

            payload["tools"] = _openai_tools_from_native(tools)
            payload["tool_choice"] = openai_tool_choice(request.tool_choice or ToolChoice.auto())
            if _requires_thinking_disabled(
                payload["messages"], api_base=self.api_base, model_name=self.model_name,
            ):
                _disable_thinking_for(payload, api_base=self.api_base)
        elif request.tool_choice is not None and request.tool_choice.mode == "none":
            payload["tool_choice"] = "none"
        if request.reasoning_effort and payload.get("thinking") != {"type": "disabled"}:
            # LLM: 档位放在工具历史检查之后：DeepSeek 缺 reasoning_content 被迫关思考时不能再带档位。
            payload.update(reasoning_payload_fields(
                self.reasoning_control, request.reasoning_effort, "openai",
                ReasoningPayloadLimits(payload["max_tokens"], self.reasoning_levels),
            ))
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
        return payload

    # LLM: 发送只消费同源 payload；认证、私有诊断、网络和响应解析仍在原边界发生，预览不触发它们。
    # 函数用途: 发起一次 Chat 请求，沿用原流式或非流式响应处理。
    def _generate(self, request: _OpenAIGenerateRequest) -> ModelResponse:
        payload = self._request_payload(request)
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        dump_provider_payload(payload, path="/chat/completions")
        if self.stream_enabled:
            return self._generate_stream(
                payload,
                headers,
                _StreamObservers(
                    on_chunk=request.on_chunk,
                    on_thinking_delta=request.on_thinking_delta,
                    on_tool_input_progress=request.on_tool_input_progress,
                    first_event_timeout_seconds=request.first_event_timeout_seconds,
                    total_deadline_seconds=request.total_deadline_seconds,
                ),
            )
        if request.total_deadline_seconds is None:
            obj = self.request_json("/chat/completions", payload, headers)
        else:
            obj = self.request_json(
                "/chat/completions", payload, headers,
                total_deadline_seconds=request.total_deadline_seconds,
            )
        return _openai_non_stream_response(
            obj,
            backend_name=self.name,
            tools_requested=bool(payload.get("tools")),
        )

    # LLM: reasoning_content 与正文必须走不同观察器，并在正文/工具或流结束前封口思考块；
    # 工具参数开始立即封口思考并发布计数；不完整响应零工具执行，仅思考也保留原内容和用量。
    # 函数用途: 解析 OpenAI SSE，同时展示正文、思考与长工具参数的实时准备进度。
    def _generate_stream(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
        observers: _StreamObservers,
    ) -> ModelResponse:
        """Parse OpenAI SSE and concatenate delta.content chunks."""
        on_chunk = observers.on_chunk
        lines = self.request_stream_iter if on_chunk is not None else self.request_stream
        text, usage, blocks, completion = collect_openai_stream_with_completion(
            request_stream_lines(
                lines,
                (   "/chat/completions", openai_stream_payload(payload), headers,
                    observers.first_event_timeout_seconds, observers.total_deadline_seconds, ),
            ),
            on_chunk=on_chunk,
            on_thinking_delta=observers.on_thinking_delta,
            on_tool_input_progress=observers.on_tool_input_progress,
        )
        assistant_blocks = _openai_assistant_content_blocks(
            reasoning=_openai_reasoning_from_completion(completion),
            text=text,
            tool_blocks=blocks,
        )
        incomplete = incomplete_response_fields(completion.stop_reason, incomplete_reason=completion.incomplete_reason)
        if incomplete:
            return ModelResponse(
                text=text,
                backend=self.name,
                usage=usage,
                tool_use_blocks=[],
                assistant_content_blocks=without_tool_blocks(assistant_blocks),
                truncated_tool_names=truncated_tool_names(completion.tool_names),
                **incomplete,
            )
        if not text and not blocks and not has_reasoning_content(assistant_blocks) and payload.get("tools"):
            raise ProviderResponseError(
                "OpenAI-compatible 流式响应没有文本或工具调用",
                error_code="MODEL_EMPTY_RESPONSE",
            )
        return ModelResponse(
            text=text,
            backend=self.name,
            usage=usage,
            tool_use_blocks=blocks,
            assistant_content_blocks=assistant_blocks,
            truncated=completion.truncated,
            stop_reason=completion.stop_reason,
        )


# LLM: 保留 reasoning_content 的显式存在性；仅思考不是空响应，坏参数/截断整轮零执行。
# 函数用途: 解析非流式回复，生成和 SSE 相同的正文、工具及原生思考历史，供跨轮回放。
def _openai_non_stream_response(
    obj: dict[str, Any],
    *,
    backend_name: str,
    tools_requested: bool,
) -> ModelResponse:
    try:
        choice = obj["choices"][0]
        message = choice["message"]
        if not any(
            field in message for field in ("content", "tool_calls", "reasoning_content")
        ):
            raise ValueError("message has neither content, reasoning, nor tool_calls")
        text = str(message.get("content") or "")
        reasoning = str(message.get("reasoning_content") or "") if "reasoning_content" in message else None
        blocks, malformed, dropped_names = _openai_tool_use_blocks(message)
    except Exception as exc:
        raise ProviderResponseError(
            f"无法解析 OpenAI-compatible 响应: {response_preview(obj)}"
        ) from exc
    finish_reason = str(choice.get("finish_reason") or "")
    incomplete = incomplete_response_fields(finish_reason, incomplete_reason="invalid_tool_arguments" if malformed else "")
    if incomplete:
        return ModelResponse(
            text=text,
            backend=backend_name,
            usage=usage_dict(obj.get("usage")),
            tool_use_blocks=[],
            assistant_content_blocks=_openai_assistant_content_blocks(
                reasoning=reasoning,
                text=text,
                tool_blocks=[],
            ),
            truncated_tool_names=truncated_tool_names(dropped_names),
            **incomplete,
        )
    if not text and not blocks and not (reasoning or "").strip() and tools_requested:
        raise ProviderResponseError(
            f"OpenAI-compatible 响应没有文本或工具调用: {response_preview(obj)}",
            error_code="MODEL_EMPTY_RESPONSE",
        )
    return ModelResponse(
        text=text,
        backend=backend_name,
        usage=usage_dict(obj.get("usage")),
        tool_use_blocks=blocks,
        assistant_content_blocks=_openai_assistant_content_blocks(
            reasoning=reasoning,
            text=text,
            tool_blocks=blocks,
        ),
        truncated=malformed,
        stop_reason=finish_reason,
    )


# LLM: 已知思考方言只要求“最后一条 user 之后”的 assistant 都带 reasoning（DeepSeek 官网 2026-10-04 实测：这一段里
#   缺了就 400，纯文本消息也一样；最后一条 user 之前的不带也接受）。只按这一段判断：以前查全部历史，一条没思考的旧答复
#   就会让之后每次请求都关思考，而关思考是另一个缓存分区，整段上下文按未命中重算，模型也从此不再思考。
#   空串不补造历史，调用方只对已核对端点显式关闭思考；改这里要同步 test_backends_openai_native_tool_use 的思考用例。
# 函数用途: 判断本次出站历史能否满足思考模式（只看最后一条 user 之后的 assistant），不满足则显式请求关闭思考。
def _thinking_mode_supported(messages: list[dict[str, Any]]) -> bool:
    last_user = max((index for index, message in enumerate(messages) if message.get("role") == "user"), default=-1)
    for message in messages[last_user + 1:]:
        if message.get("role") != "assistant":
            continue
        if not str(message.get("reasoning_content") or "").strip():
            return False
    return True


# LLM: 只适配已核对的接口/模型组合(DeepSeek 官方 OpenAI 方言与 工具运行时 Zen/Go)；未知网关保持原消息。
# 历史无法满足思考模式时显式关闭，而不是伪造字段值冒充思考原文。
# 函数用途: 返回本次是否必须显式关闭思考，供 payload 组装使用。
def _requires_thinking_disabled(
    messages: list[dict[str, Any]], *, api_base: str, model_name: str,
) -> bool:
    endpoint = urlsplit(api_base)
    known_endpoint = endpoint.hostname == "api.deepseek.com" or (
        endpoint.hostname == "opencode.ai" and endpoint.path.startswith("/zen/")
    )
    if not known_endpoint or not model_name.lower().startswith("deepseek-"):
        return False
    return not _thinking_mode_supported(messages)


# LLM: thinking 是 DeepSeek/Zen 方言的显式关闭开关，不是通用 OpenAI 字段；只写给已核对端点。
# 函数用途: 在已知 DeepSeek 方言端点上写入关闭思考的请求字段；未知网关不写。
def _disable_thinking_for(payload: dict[str, Any], *, api_base: str) -> None:
    endpoint = urlsplit(api_base)
    known_endpoint = endpoint.hostname == "api.deepseek.com" or (
        endpoint.hostname == "opencode.ai" and endpoint.path.startswith("/zen/")
    )
    if known_endpoint:
        payload["thinking"] = {"type": "disabled"}


# LLM: Chat schema 投影只转换规范工具元数据；不能增加能力，需同步工具选择及请求快照测试。
# 函数用途: 将已有工具目录转换成供应商 function tools 格式。
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


# LLM: Plain prompts preserve legacy ordering. Typed cache layouts must mirror 会话运行时
# append-only order so local OpenAI-compatible KV caches can reuse history without cache_control.
# 函数用途: 把统一原生消息转换成 OpenAI 顺序；typed prompt 使用稳定 system→规范消息→动态事实。
def _openai_messages_from_native(
    messages: list[dict[str, Any]],
    *,
    initial_user_prompt: str,
    system_instruction: str = "",
) -> list[dict[str, Any]]:
    """Translate IR while preserving the first-turn user message across tool rounds."""

    layout = prompt_cache_layout(initial_user_prompt)
    if layout is not None:
        return _openai_messages_from_cache_layout(
            messages,
            stable_system_prefix=layout.stable_prefix,
            stable_user_prefix=layout.stable_user_prefix,
            volatile_suffix=layout.volatile_suffix,
            system_instruction=system_instruction,
        )
    translated: list[dict[str, Any]] = []
    if system_instruction:
        translated.append({"role": "system", "content": system_instruction})
    translated.append({"role": "user", "content": initial_user_prompt})
    for message in messages:
        translated.extend(_openai_message_from_native(message))
    return translated


# LLM: The system prefix and caller-supplied chronological messages are byte-stable until a real
# Compact boundary; request-varying facts append last. Never parse headings to create this order.
# 函数用途: 为没有显式 cache_control 的 OpenAI 兼容端点构造可做 token 前缀复用的消息序列。
def _openai_messages_from_cache_layout(
    messages: list[dict[str, Any]],
    *,
    stable_system_prefix: str,
    stable_user_prefix: str,
    volatile_suffix: str,
    system_instruction: str,
) -> list[dict[str, Any]]:
    translated: list[dict[str, Any]] = []
    system_text = "\n\n".join(
        text
        for text in (str(system_instruction or ""), str(stable_system_prefix or ""))
        if text
    )
    if system_text:
        translated.append({"role": "system", "content": system_text})
    if stable_user_prefix:
        translated.append({"role": "user", "content": str(stable_user_prefix)})
    for message in messages:
        translated.extend(_openai_message_from_native(message))
    return _append_openai_volatile_user_text(translated, volatile_suffix)


# LLM: Merge only with an existing trailing user message; tool and assistant ordering must remain
# untouched so provider tool-call pairing stays valid. 带图片或视频的 user 消息内容是 typed 数组，动态事实作为文本部件
# 追加在数组末尾；绝不能把数组转成字符串（会丢图并把 base64 当正文发出，9a 复审 P4）。
# 函数用途: 把本轮变化事实放到 OpenAI 消息尾部，并避免无意义的连续 user 消息。
def _append_openai_volatile_user_text(
    messages: list[dict[str, Any]],
    volatile_suffix: str,
) -> list[dict[str, Any]]:
    text = str(volatile_suffix or "")
    if not text:
        return messages
    if not messages or messages[-1].get("role") != "user":
        return [*messages, {"role": "user", "content": text}]
    prepared = list(messages)
    tail = prepared[-1]
    prior = tail.get("content")
    if isinstance(prior, list):
        prepared[-1] = {**tail, "content": [*prior, {"type": "text", "text": text}]}
        return prepared
    prior_text = str(prior or "")
    prepared[-1] = {**tail, "content": f"{prior_text}\n\n{text}" if prior_text else text}
    return prepared


# LLM: Chat 历史转换只接受结构化角色与内容；assistant 和 tool-result 顺序由专属转换函数保持。
# 函数用途: 将一条规范历史记录展开为 Chat 消息，不修改原始记录。
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


# LLM: 所有非空真实 thinking 都随 assistant 回放，普通答复也须保留；空块不伪造 reasoning 字段。
# 普通文本不制造厂商扩展字段。改动须同步检查流式/非流式解析和跨轮 native history。
#   Chat Completions 的 assistant 消息必须带正文或 tool_calls（DeepSeek 等上游对两者皆空直接 400）；
#   只有思考、或正文拼起来为空且没有工具调用的块（如模型只回了思考的空答复）不回放，只影响本次请求，
#   规范 native history 本身不改写。
# 函数用途: 把规范化 assistant 块恢复成 Chat Completions 消息，避免插话或最终答复后的工具请求丢失思考字段；
#   没有可见正文也没有工具调用时返回空列表，防止一条空 assistant 让之后每次请求都被上游拒绝。
def _openai_assistant_messages(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    text = "".join(str(block.get("text") or "") for block in blocks if block.get("type") == "text")
    # 空思考块必须按"没有思考"处理：写空串会变成上游拒绝的 reasoning_content=""，
    # 也会让"历史是否满足思考模式"的判定失真。
    reasoning = [
        thought
        for thought in (str(block.get("thinking") or "") for block in blocks if block.get("type") == "thinking")
        if thought.strip()
    ]
    calls = [_openai_function_call(block) for block in blocks if block.get("type") == "tool_use"]
    if text or calls:
        message: dict[str, Any] = {"role": "assistant", "content": text or None}
        if calls:
            message["tool_calls"] = calls
        if reasoning:
            # 普通答复同样属于 reasoning 历史，不能按是否调工具丢弃。
            message["reasoning_content"] = "".join(reasoning)
        return [message]
    return []


# LLM: OpenAI-compatible reasoning/text/tool_calls 进入 canonical IR 前只能经过这一白名单，
# 未知 message 字段不得原样回放到后续请求。
# 函数用途: 把回复整理成有序块，None 表示未返回思考字段，空字符串表示返回了空思考；不混淆两者。
def _openai_assistant_content_blocks(
    *,
    reasoning: str | None,
    text: str,
    tool_blocks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    if reasoning is not None:
        blocks.append({"type": "thinking", "thinking": reasoning})
    if text:
        blocks.append({"type": "text", "text": text})
    blocks.extend(
        {
            "type": "tool_use",
            "id": str(block.get("id") or ""),
            "name": str(block.get("name") or ""),
            "input": dict(block.get("input") or {}),
        }
        for block in tool_blocks
    )
    return blocks


# LLM: StreamCompletion 只允许携带白名单 assistant blocks；本帮助函数只读取 thinking，
# 不接受正文、工具参数或未知 provider 扩展字段。
# 函数用途: 取得完整思考文本，无思考块时返回 None，让普通模型保持原协议。
def _openai_reasoning_from_completion(completion: StreamCompletion) -> str | None:
    parts = [
        str(block.get("thinking") or "")
        for block in completion.assistant_content_blocks
        if isinstance(block, dict) and block.get("type") == "thinking"
    ]
    return "".join(parts) if parts else None


# LLM: Chat 工具调用回放保持原调用 ID、工具名和 JSON 参数；需同步工具结果配对测试。
#   参数 JSON 必须用稳定键序（sort_keys=True）：同一调用在本进程内是 dict 插入序，
#   落盘 transcript 时 store_messages 会按字母序重排键（append_private_jsonl_records 的
#   sort_keys=True），重新加载后插入序不同；不排序会让「重载前后同一历史」出站字节不同，
#   使 DeepSeek 等按前缀匹配的 KV 缓存从该条 tool_call 起整段失效。排序与磁盘序一致，
#   两个来源因此可复现同一字节。
# 函数用途: 将一个规范工具调用块转换为供应商 function call，参数序列化与磁盘重放一致。
def _openai_function_call(block: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(block.get("id") or ""),
        "type": "function",
        "function": {
            "name": str(block.get("name") or ""),
            "arguments": json.dumps(
                block.get("input") or {}, ensure_ascii=False, sort_keys=True,
            ),
        },
    }


# LLM: Chat 转换保留同一 user 消息内的文字/媒体顺序；媒体到发送边界才编码，不丢附件或跨 tool_result 合并。
# 函数用途: 把用户内容块展开为 Chat user/tool 消息，支持图片和视频 URL 内容块。
def _openai_user_messages(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from ..conversation.input_media import provider_media_block

    translated: list[dict[str, Any]] = []
    pending: list[dict] = []

    # LLM: 纯文本沿用旧 wire 形态，含媒体时保留 typed content 数组；不修改原历史。
    # 函数用途: 将连续的用户内容追加成一条消息，并清空局部缓冲。
    def flush() -> None:
        if pending:
            content = "".join(item["text"] for item in pending) if all(item["type"] == "text" for item in pending) else list(pending)
            translated.append({"role": "user", "content": content})
            pending.clear()

    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            pending.append({"type": "text", "text": str(block.get("text") or "")})
        elif kind in {"image", "video"}:
            source = provider_media_block(block)["source"]
            url = source.get("url") if source["type"] == "url" else f"data:{source['media_type']};base64,{source['data']}"
            key = f"{kind}_url"
            pending.append({"type": key, key: {"url": url}})
        elif kind == "tool_result":
            flush()
            translated.append({"role": "tool", "tool_call_id": str(block.get("tool_use_id") or ""),
                               "content": str(block.get("content") or "")})
    flush()
    return translated


# LLM: JSON 必须是完整对象；坏块不上报为空参数调用，错误标记由响应层执行整轮零工具边界。
# 函数用途: 提取非流式工具参数，区分真实空对象与损坏/缺失参数。
def _openai_tool_use_blocks(message: dict[str, Any]) -> tuple[list[dict[str, Any]], bool, list[str]]:
    raw_calls = message.get("tool_calls")
    if not isinstance(raw_calls, list):
        return [], False, []
    blocks: list[dict[str, Any]] = []
    malformed = False
    dropped: list[str] = []
    for raw in raw_calls:
        if not isinstance(raw, dict):
            malformed = True
            continue
        function = raw.get("function") if isinstance(raw.get("function"), dict) else {}
        arguments = function.get("arguments")
        try:
            tool_input = (
                arguments if isinstance(arguments, dict) else json.loads(arguments)
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            malformed = True
            dropped.append(str(function.get("name") or ""))
            continue
        if not isinstance(tool_input, dict):
            malformed = True
            dropped.append(str(function.get("name") or ""))
            continue
        blocks.append(
            {
                "id": str(raw.get("id") or ""),
                "name": str(function.get("name") or ""),
                "input": tool_input,
            }
        )
    return blocks, malformed, dropped
