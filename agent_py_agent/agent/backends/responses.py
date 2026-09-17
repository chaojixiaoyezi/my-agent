# LLM: Responses 适配复用 HTTP 主链、原生工具和工作片采样配置；不启用远端存储，不自动换协议。
# 模块用途: 支持 /responses 的显式采样、文本、工具调用、流式摘要、JSON 输出及用量统计。
from __future__ import annotations

from .base import ModelResponse, OpenAICompatibleBackend, _bounded_output_tokens, _tools_for_choice
from .responses_wire import collect_response, input_items, response_fields


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
    # 函数用途: 用工作片冻结的私有配置及 top_p 发送一次请求，转成上层通用模型结果。
    def _generate(self, request) -> ModelResponse:
        payload = {"model": self.model_name, "store": False, "include": ["reasoning.encrypted_content"],
                   "max_output_tokens": _bounded_output_tokens(self.max_tokens, request.max_output_tokens),
                   "input": input_items(request.prompt, request.messages, request.system_instruction, self.model_name)}
        if self.temperature_explicit:
            payload["temperature"] = self.temperature
        if self.top_p is not None:
            payload["top_p"] = self.top_p
        tools = _tools_for_choice(request.tools, request.tool_choice)
        if tools:
            payload["tools"] = [{"type": "function", "name": tool["name"], "description": tool.get("description", ""),
                                 "parameters": tool["input_schema"], "strict": False} for tool in tools]
            choice = request.tool_choice
            from ..tooling.runtime_contracts import ToolChoice
            from .tool_protocol_adapter import openai_tool_choice

            value = openai_tool_choice(choice or ToolChoice.auto())
            payload["tool_choice"] = {"type": "function", "name": value["function"]["name"]} if isinstance(value, dict) else value
        if request.response_schema is not None:
            payload["text"] = {"format": {"type": "json_schema", "name": "my_agent_output", "strict": True, "schema": request.response_schema}}
        elif request.json_object:
            payload["text"] = {"format": {"type": "json_object"}}
        if getattr(self, "auth_ref", {}).get("mode") == "chatgpt":
            payload.pop("max_output_tokens", None)
            payload["instructions"] = "\n\n".join(item["content"] for item in payload["input"] if item.get("role") == "system")
            payload["input"] = [item for item in payload["input"] if item.get("role") != "system"]
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}
        if self.stream_enabled:
            payload["stream"] = True
            lines = self.request_stream_iter("/responses", payload, headers, first_event_timeout_seconds=request.first_event_timeout_seconds)
            try:
                obj = collect_response(lines, request.on_chunk, request.on_thinking_delta)
            finally:
                lines.close()
        else:
            obj = self.request_json("/responses", payload, headers)
        return ModelResponse(backend=self.name, **response_fields(obj, self.model_name))
