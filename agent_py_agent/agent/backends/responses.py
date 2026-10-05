# LLM: Responses 适配复用 HTTP 主链、原生工具和工作片采样配置；不启用远端存储，不自动换协议。
# 模块用途: 支持 /responses 的显式采样、文本、工具调用、流式摘要、JSON 输出及用量统计。
from __future__ import annotations

from ..conversation.input_media import project_input_media
from .base import ModelResponse
from .http import bounded_output_tokens, request_stream_lines
from .openai_chat import OpenAICompatibleBackend
from .responses_wire import collect_response, input_items, response_fields
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
        from .reasoning_control import responses_reasoning_field

        payload.update(responses_reasoning_field(self.reasoning_control, request.reasoning_effort, self.reasoning_levels,
                                                 disabled=request.thinking_disabled))
        if request.response_schema is not None:
            payload["text"] = {"format": {"type": "json_schema", "name": "my_agent_output", "strict": True, "schema": request.response_schema}}
        elif request.json_object:
            payload["text"] = {"format": {"type": "json_object"}}
        if getattr(self, "auth_ref", {}).get("mode") == "chatgpt":
            payload.pop("max_output_tokens", None)
            payload["instructions"] = "\n\n".join(item["content"] for item in payload["input"] if item.get("role") == "system")
            payload["input"] = [item for item in payload["input"] if item.get("role") != "system"]
        validate_responses_input(payload["input"])
        payload.update(_session_cache_key())
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}
        obj = self._send_responses_request(payload, headers, request)
        return ModelResponse(backend=self.name, **response_fields(obj, self.model_name))

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
#   路由到同一份提示缓存；只在绑定宿主会话时写入，绝不生成随机键。改动须同步 test_responses_cache_key。
# 函数用途: 返回要并进 Responses 请求体的缓存键字段；没有绑定会话时返回空字典。
def _session_cache_key() -> dict:
    from .provider_headers import current_provider_session

    session = current_provider_session()
    return {"prompt_cache_key": session} if session else {}
