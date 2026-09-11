# LLM: Responses items/SSE 只按结构化 type/status 解释；未完成参数不得成为工具调用，原始密文仅供同模型回放。
# 模块用途: 将 Responses API 转成已有模型响应和工具历史合同，保留流式正文、思考摘要和用量。
from __future__ import annotations

import json

from ..prompting_parts.cache_layout import prompt_cache_layout
from .errors import ProviderResponseError
from .response_completion import incomplete_response_fields, without_tool_blocks


# LLM: 仅白名单 reasoning item 可跨轮回放，不保存服务端 response id 或开启远端会话存储。
# 函数用途: 清洗供应商的加密思考块，禁止其携带可执行工具字段。
def reasoning_item(value: object) -> dict:
    if not isinstance(value, dict) or value.get("type") != "reasoning" or not isinstance(value.get("encrypted_content"), str):
        return {}
    result = {"type": "reasoning", "encrypted_content": value["encrypted_content"], "summary": []}
    if isinstance(value.get("id"), str):
        result["id"] = value["id"]
    for item in value.get("summary", []):
        if isinstance(item, dict) and item.get("type") == "summary_text" and isinstance(item.get("text"), str):
            result["summary"].append({"type": "summary_text", "text": item["text"]})
    return result


# LLM: 工具结果依据 tool_use_id 匹配，助手工具依据 canonical IR；任何自然语言都不能生成 function_call。
# 函数用途: 把一条统一原生消息转换为 Responses 的独立 items。
def message_items(message: dict, model: str) -> list[dict]:
    role, content = message.get("role"), message.get("content")
    if isinstance(content, str):
        return [{"role": role, "content": content}] if content else []
    result = []
    for block in content or []:
        kind = block.get("type")
        if kind == "text":
            result.append({"role": role, "content": str(block.get("text") or "")})
            continue
        if role == "assistant" and kind == "responses_reasoning" and block.get("model") == model and (item := reasoning_item(block.get("item"))):
            result.append(item)
            continue
        if role == "assistant" and kind == "tool_use":
            result.append({"type": "function_call", "call_id": block["id"], "name": block["name"],
                           "arguments": json.dumps(block["input"], ensure_ascii=False)})
            continue
        if role == "user" and kind == "tool_result":
            result.append({"type": "function_call_output", "call_id": block["tool_use_id"],
                           "output": str(block.get("content") or "")})
    return result


# LLM: 复用 typed cache layout 的追加顺序；不把每轮动态信息放进 system，不依赖 previous_response_id。
# 函数用途: 保持本机会话为权威，构建无远端存储的完整请求输入。
def input_items(prompt: str, messages: list | None, system: str, model: str) -> list[dict]:
    layout = prompt_cache_layout(prompt)
    result = []
    prefix = "\n\n".join(text for text in (system, layout.stable_prefix if layout else "") if text)
    if prefix:
        result.append({"role": "system", "content": prefix})
    user = layout.stable_user_prefix if layout else prompt
    if user:
        result.append({"role": "user", "content": user})
    for message in messages or []:
        result.extend(message_items(message, model))
    if layout and layout.volatile_suffix:
        result.append({"role": "user", "content": layout.volatile_suffix})
    return result


# LLM: 展示回调失败不能改变模型完成状态；此函数不生成或执行工具。
# 函数用途: 安全通知 TUI 一个流式增量或思考完成事件。
def _notify(callback, value: str) -> None:
    if callable(callback):
        try:
            callback(value)
        except Exception:
            pass


# LLM: response.completed/incomplete 是终态事实；EOF 与 failed 不冒充完整回复，工具参数只从最终 output 取。
# 函数用途: 消费 typed SSE，实时显示正文及 reasoning summary，保留最终用量。
def collect_response(lines, on_chunk, on_thinking) -> dict:
    text, thinking = [], []
    thinking_closed = False
    terminal = None
    for line in lines:
        if not line or line == "[DONE]":
            continue
        try:
            event = json.loads(line)
        except (TypeError, ValueError) as exc:
            raise ProviderResponseError("Responses 流包含无效 JSON。") from exc
        kind = event.get("type")
        if kind in {"error", "response.failed"}:
            raise ProviderResponseError("Responses 服务返回失败事件。")
        if kind in {"response.reasoning_summary_text.delta", "response.reasoning_text.delta"}:
            delta = str(event.get("delta") or "")
            thinking.append(delta)
            _notify(on_thinking, delta)
        starts_output = kind == "response.output_text.delta" or (
            kind == "response.output_item.added" and event.get("item", {}).get("type") in {"message", "function_call"})
        if starts_output and thinking and not thinking_closed:
            _notify(getattr(on_thinking, "complete", None), "".join(thinking))
            thinking_closed = True
        if kind == "response.output_text.delta":
            delta = str(event.get("delta") or "")
            text.append(delta)
            _notify(on_chunk, delta)
        if kind in {"response.completed", "response.incomplete"}:
            terminal = event.get("response")
            break
    if thinking and not thinking_closed:
        _notify(getattr(on_thinking, "complete", None), "".join(thinking))
    if not isinstance(terminal, dict):
        return {"status": "incomplete", "incomplete_details": {"reason": "stream_eof"},
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "".join(text)}]}]}
    return terminal


# LLM: tool JSON 必须完整且是对象；原 incomplete_details.reason 保留，EOF/过滤/坏参数不能伪装为 max_tokens。
# 函数用途: 解析最终输出为公共 ModelResponse 字段，原生 reasoning 密文永不当正文。
def response_fields(obj: dict, model: str) -> dict:
    status = obj.get("status")
    if status not in {"completed", "incomplete"}:
        raise ProviderResponseError("Responses 未返回有效完成状态。")
    text, blocks, calls = [], [], []
    malformed = False
    for item in obj.get("output", []):
        kind = item.get("type")
        if kind == "message":
            values = [str(part.get("text") or part.get("refusal") or "") for part in item.get("content", [])
                      if part.get("type") in {"output_text", "refusal"}]
            text.extend(values)
            blocks.extend({"type": "text", "text": value} for value in values)
            continue
        if kind == "reasoning" and (raw := reasoning_item(item)):
            blocks.append({"type": "responses_reasoning", "model": model, "item": raw})
            continue
        if kind == "function_call":
            call = _function_block(item)
            if call:
                calls.append(call)
                blocks.append(call)
            malformed = malformed or not call
    malformed = malformed or len({call["id"] for call in calls}) != len(calls)
    stop_reason = str((obj.get("incomplete_details") or {}).get("reason") or "") if status == "incomplete" else "end_turn"
    failure = "invalid_tool_arguments" if malformed else (stop_reason or "unknown") if status == "incomplete" else ""
    incomplete = incomplete_response_fields(stop_reason, incomplete_reason=failure)
    return {"text": "".join(text), "usage": obj.get("usage") or {},
            "tool_use_blocks": [] if incomplete else calls,
            "assistant_content_blocks": without_tool_blocks(blocks) if incomplete else blocks,
            **(incomplete or {"truncated": False, "stop_reason": stop_reason})}


# LLM: JSON 和 call_id/name 都合法才产生 canonical 工具块；任何不完整值返回空，不猜补参数。
# 函数用途: 安全解析一个 Responses function_call。
def _function_block(item: dict) -> dict:
    try:
        args = json.loads(item["arguments"])
        if (not isinstance(args, dict) or not isinstance(item.get("call_id"), str) or not item["call_id"]
                or not isinstance(item.get("name"), str) or not item["name"] or item.get("status") not in {None, "completed"}):
            return {}
        return {"type": "tool_use", "id": item["call_id"], "name": item["name"], "input": args}
    except (KeyError, TypeError, ValueError):
        return {}
