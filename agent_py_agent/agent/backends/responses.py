# LLM: Responses 适配复用 HTTP 主链、原生工具和工作片采样配置；不启用远端存储，不自动换协议。
# 模块用途: 支持 /responses 的显式采样、文本、工具调用、流式摘要、JSON 输出及用量统计。
from __future__ import annotations

from ..conversation.input_media import project_input_media
from .base import ModelResponse
from .http import bounded_output_tokens, request_stream_lines
from .openai_chat import OpenAICompatibleBackend
from .responses_wire import collect_response, input_items, insert_reasoning_update, response_fields
from .tool_protocol_adapter import tools_for_choice
from .wire_contract import repair_native_messages, validate_responses_input


# LLM: 继承公开生成签名以保持 Compact/原生调用方一致，仅覆盖协议组装和解析。
# 类用途: 适配支持 OpenAI Responses 的供应商。
class OpenAIResponsesBackend(OpenAICompatibleBackend):
    name = "openai_responses"
    # 此适配器尚未投影 Responses 工具参数增量，不能继承 Chat 已实现能力的标志。
    supports_tool_input_progress = False

    # LLM: capability 记录使用真实规范接口，不推断模型家族。
    # 函数用途: 返回能力探针使用的接口地址。
    def _tool_endpoint(self) -> str:
        from .provider_headers import endpoint_parts

        return "".join(endpoint_parts(self.api_base, "/responses"))

    # LLM: Responses 只发送显式采样；订阅登录固定使用流式、不发 max_output_tokens，system 移到 instructions。
    #   智能程度按 reasoning_control=effort 写 reasoning.effort（档位按模型声明的 reasoning_levels 对应，见 reasoning_control）。
    #   历史先经 wire_contract.repair_native_messages 修整副本，最终 input 由 validate_responses_input 复核，不合规在本地抛错不发送。
    # 函数用途: 用工作片冻结的私有配置及 top_p 发送一次请求，转成上层通用模型结果。
    def _generate(self, request) -> ModelResponse:
        messages = project_input_media(repair_native_messages(request.messages), self.input_media_max_bytes)
        payload = {"model": self.model_name, "store": False, "include": ["reasoning.encrypted_content"],
                   "max_output_tokens": bounded_output_tokens(self.max_tokens, request.max_output_tokens),
                   "input": input_items(request.prompt, messages, request.system_instruction, self.model_name)}
        if self.temperature_explicit:
            payload["temperature"] = self.temperature
        if self.top_p is not None:
            payload["top_p"] = self.top_p
        tools = tools_for_choice(request.tools, request.tool_choice)
        if tools:
            payload["tools"] = [{"type": "function", "name": tool["name"], "description": tool.get("description", ""),
                                 "parameters": tool["input_schema"], "strict": False} for tool in tools]
            choice = request.tool_choice
            from ..tooling.runtime_contracts import ToolChoice
            from .tool_protocol_adapter import openai_tool_choice

            value = openai_tool_choice(choice or ToolChoice.auto())
            payload["tool_choice"] = {"type": "function", "name": value["function"]["name"]} if isinstance(value, dict) else value
        from .reasoning_control import responses_reasoning_field, responses_reasoning_update_item

        payload.update(responses_reasoning_field(self.reasoning_control, request.reasoning_effort, self.reasoning_levels,
                                                 disabled=request.thinking_disabled))
        # 压缩降档：请求级 reasoning.effort 不动（前缀不失配），只在 input 末尾追加 configuration_update 项。
        update = (responses_reasoning_update_item(self.reasoning_control, request.reasoning_update_effort,
                                                  payload.get("reasoning"), self.reasoning_levels)
                  if request.reasoning_update_effort and self.supports_reasoning_update_items() else None)
        if update is not None:
            payload["input"] = insert_reasoning_update(payload["input"], update)
        if request.response_schema is not None:
            payload["text"] = {"format": {"type": "json_schema", "name": "my_agent_output", "strict": True, "schema": request.response_schema}}
        elif request.json_object:
            payload["text"] = {"format": {"type": "json_object"}}
        subscription = getattr(self, "auth_ref", {}).get("mode") == "chatgpt"
        if subscription:
            payload.pop("max_output_tokens", None)
            payload["instructions"] = "\n\n".join(item["content"] for item in payload["input"] if item.get("role") == "system")
            payload["input"] = [item for item in payload["input"] if item.get("role") != "system"]
        validate_responses_input(payload["input"])
        # 服务端压缩：触发项放在 input 最末（压缩链保证此时没有压缩指令这条动态 user 消息），前缀逐字不变。
        if getattr(request, "compaction_trigger", False):
            payload["input"] = [*payload["input"], {"type": "compaction_trigger"}]
        payload.update(_session_cache_key(subscription))
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}",
                   **_subscription_affinity_headers(subscription)}
        obj = self._send_responses_request(payload, headers, request)
        return ModelResponse(backend=self.name, **response_fields(obj, self.model_name))

    # LLM: 只按结构化事实判断：档案显式 on/off 优先；auto 按已核对的模型名前缀 gpt-6（OpenAI 文档：GPT-6 及以后
    #   支持用 configuration_update 项改档位且前缀不变）。不是封闭枚举：新模型可在档案里显式声明 on。
    # 函数用途: 这个模型能不能用 configuration_update 项改思考档位。
    def supports_reasoning_update_items(self) -> bool:
        declared = str(getattr(self, "reasoning_update_items", "auto") or "auto").strip().lower()
        if declared in {"on", "off"}:
            return declared == "on"
        return str(self.model_name or "").lower().startswith("gpt-6")

    # LLM: 服务端压缩能力只按结构化事实判断：订阅登录（auth_ref.mode == chatgpt，即官方 Codex 同一后端）已真机核实接受
    #   compaction_trigger 项并返回 compaction 项；API Key 的 Responses 端点未核实，先不声明。改动同步 conversation/compact_remote。
    # 函数用途: 这个后端能不能让服务端替我们压缩历史。
    def supports_remote_compaction(self) -> bool:
        return str((getattr(self, "auth_ref", None) or {}).get("mode") or "") == "chatgpt"

    # LLM: 压缩项只有同一协议、同一端点的后端能读（真机核实 sol/astra 可互读）；检查点里存这份范围，
    #   compact_summary_view 按它判断兼容，不兼容的后端把该检查点当透明、从归档原文重新压缩。
    # 函数用途: 返回服务端压缩项的适用范围（协议 + 端点），不含凭据。
    def provider_compaction_scope(self) -> dict[str, str]:
        return {"protocol": "openai_responses", "endpoint": str(self.api_base or "")}

    # LLM: 流式与 Chat 后端同一个分派入口 request_stream_lines，按被调方签名传首包预算与绝对期限（cabfix 起
    #   HttpBackend 收 options）；直接写旧关键字会让所有 Responses 流式请求 TypeError（17k Linux 车道实测）。
    #   非流式只在有绝对期限时才带关键字，旧替身的三参 request_json 不受影响。同步 test_responses_backend。
    # 函数用途: 把组装好的 Responses 请求发出去（流式边收边转观察者），返回供应商的完整响应对象。
    def _send_responses_request(self, payload: dict, headers: dict, request) -> dict:
        if self.stream_enabled:
            payload["stream"] = True
            lines = request_stream_lines(self.request_stream_iter, (
                "/responses", payload, headers, request.first_event_timeout_seconds, request.total_deadline_seconds,
            ))
            try:
                return collect_response(lines, request.on_chunk, request.on_thinking_delta)
            finally:
                lines.close()
        if request.total_deadline_seconds is None:
            return self.request_json("/responses", payload, headers)
        return self.request_json("/responses", payload, headers, total_deadline_seconds=request.total_deadline_seconds)


# LLM: 参考官方 Codex：同一会话的请求带稳定缓存键（宿主绑定的 owner+thread 摘要，不含凭据），让服务商把同一会话
#   路由到同一份提示缓存；只在绑定宿主会话时写入，绝不生成随机键。订阅登录用与 session-id 头相同的 UUID 形态
#   （官方根代理两者相同），其它服务商保持原摘要。改动须同步 test_responses_cache_key。
# 函数用途: 返回要并进 Responses 请求体的缓存键字段；没有绑定会话时返回空字典。
def _session_cache_key(subscription: bool = False) -> dict:
    from .provider_headers import chatgpt_session_uuid, current_provider_session

    session = chatgpt_session_uuid() if subscription else current_provider_session()
    return {"prompt_cache_key": session} if session else {}


# LLM: 只给订阅登录加：ChatGPT 后端按 session-id 头决定缓存亲和（官方 Codex 每个请求都带），值与 prompt_cache_key 相同；
#   未绑定会话或非订阅登录返回空字典，不改其它服务商的请求头。WebSocket 握手沿用同一份请求头。
# 函数用途: 返回订阅接口的缓存亲和请求头。
def _subscription_affinity_headers(subscription: bool) -> dict:
    from .provider_headers import CHATGPT_SESSION_HEADER_PROTOCOL, chatgpt_session_uuid

    session = chatgpt_session_uuid() if subscription else ""
    return {CHATGPT_SESSION_HEADER_PROTOCOL: session} if session else {}
