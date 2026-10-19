# LLM: Responses items/SSE只按结构化type/status解释；降档位置由真实prompt布局声明，不能把历史user误当新指令；未完成参数不得成为工具调用。
# 模块用途: 将Responses转成原响应/工具历史，保留逐消息原阶段来源、候选正文、显式思考摘要和用量。
from __future__ import annotations

import json

from ..prompting_parts.cache_layout import prompt_cache_layout
from .assistant_messages import KNOWN_PHASES, message_metadata, response_text_block
from .errors import ProviderResponseError
from .response_completion import incomplete_response_fields, without_tool_blocks
from .responses_failure import failed_event_error


# LLM: 仅collector签发归一化来源；属性不进dict/JSON/出站payload，不能把缺失event索引变成排序位置。
# 类用途: 在原响应字典旁携带已观察的真实输出索引，不改变供应商字段或外部字典接口。
class _CollectedResponse(dict):
    # LLM: 只由本模块完整/EOF归一化构造；索引与输出一一对应，冲突沿原malformed门拒绝工具。
    # 函数用途: 保存本次收集到的来源索引和结构化身份冲突事实，不发请求或写历史。
    def __init__(self, response: dict, indices: tuple[int | None, ...], *, conflict: bool = False):
        super().__init__(response)
        self.output_indices = indices
        self.output_identity_conflict = conflict


# LLM: 同真实id/index更正一次；冲突身份不静默吞原调用，保全条目后交原malformed门，不按正文去重。
# 函数用途: 在原done集合合并一条完成事件，记录真实索引并返回是否存在结构冲突。
def _record_done_item(rows: list[dict], positions: dict, event: dict) -> bool:
    item = event["item"]
    index = event.get("output_index")
    index = index if type(index) is int and index >= 0 else None
    item_id = item.get("id") if isinstance(item.get("id"), str) and item["id"] else None
    keys = ([('id', item_id)] if item_id else []) + ([('index', index)] if index is not None else [])
    matches = {positions[key] for key in keys if key in positions}
    conflict = len(matches) > 1
    position = next(iter(matches)) if len(matches) == 1 else None
    if position is not None:
        previous = rows[position]
        old = previous["item"]
        conflict = (old.get("type") != item.get("type")
                    or bool(old.get("id") and item_id and old["id"] != item_id)
                    or (previous["index"] is not None and index is not None and previous["index"] != index)
                    or (item.get("type") == "function_call" and old.get("call_id") != item.get("call_id")))
    if position is None or conflict:
        position = len(rows)
        rows.append({"item": item, "index": index})
    else:
        rows[position] = {"item": item, "index": index if index is not None else rows[position]["index"]}
    for key in keys:
        positions[key] = position
    return conflict


# LLM: 仅已知index之间排序；未知条目保原观察锚点且索引仍None，返回载体仅供response_fields一次消费。
# 函数用途: 为终态空output的done回退恢复真实顺序与来源，不推测缺失位置。
def _done_output_response(terminal: dict, rows: list[dict], *, conflict: bool) -> dict:
    known = iter(sorted((row for row in rows if row["index"] is not None), key=lambda row: row["index"]))
    ordered = [next(known) if row["index"] is not None else row for row in rows]
    return _CollectedResponse({**terminal, "output": [row["item"] for row in ordered]},
                              tuple(row["index"] for row in ordered), conflict=conflict)


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


# LLM: 服务端压缩项只认 type=compaction 且 encrypted_content 为字符串；不保存 id、不解读密文、不让它携带工具字段。
# 函数用途: 清洗供应商返回或回放用的压缩项。
def compaction_item(value: object) -> dict:
    if not isinstance(value, dict) or value.get("type") != "compaction" or not isinstance(value.get("encrypted_content"), str):
        return {}
    return {"type": "compaction", "encrypted_content": value["encrypted_content"]}


# LLM: 工具结果依据 tool_use_id 匹配，助手工具依据 canonical IR；任何自然语言都不能生成 function_call。
#   reasoning 项必须紧跟它产出的消息或函数调用，所以助手轮末尾没有后继的 reasoning 项不发送（只影响本次请求）。
# 函数用途: 转换 Responses items，图片在发送边界编码，未支持的视频显式拒绝而不静默丢失。
def message_items(message: dict, model: str) -> list[dict]:
    role, content = message.get("role"), message.get("content")
    if isinstance(content, str):
        return [{"role": role, "content": content}] if content else []
    result = []
    for block in content or []:
        kind = block.get("type")
        if role == "user" and kind in {"image", "video"}:
            from ..conversation.input_media import provider_media_block
            from .errors import ProviderRequestRejectedError

            if kind == "video":
                raise ProviderRequestRejectedError("当前 Responses 适配器未配置视频输入协议，请使用支持视频的 Messages/Chat 接口。")
            source = provider_media_block(block)["source"]
            url = source.get("url") if source["type"] == "url" else f"data:{source['media_type']};base64,{source['data']}"
            result.append({"role": role, "content": [{"type": "input_image", "image_url": url}]})
            continue
        if kind == "text":
            meta = message_metadata(block) if role == "assistant" else {}
            row = {"role": role, "content": str(block.get("text") or "")}
            if meta.get("message_id"):
                row["id"] = meta["message_id"]
            if meta.get("phase") in KNOWN_PHASES:
                row["phase"] = meta["phase"]
            identity = ('index', meta['output_index'], meta.get('message_id')) if meta.get('output_index') is not None else ('id', meta['message_id']) if meta.get('message_id') else None
            if identity is not None and result and result[-1].get("_message_index") == identity:
                result[-1]["content"] += row["content"]
            else:
                result.append({**row, **({"_message_index": identity} if identity is not None else {})})
            continue
        # 服务端压缩项是顶层 input 项（不在消息里），由 CompactionSummary 的适配消息带进来，任何角色都按项回放。
        if kind == "responses_compaction" and (compaction := compaction_item(block.get("item"))):
            result.append(compaction)
            continue
        if role == "assistant" and kind == "responses_reasoning" and block.get("model") == model and (item := reasoning_item(block.get("item"))):
            result.append(item)
            continue
        if role == "assistant" and kind == "tool_use":
            # LLM: arguments 用稳定键序；理由同 openai_chat._openai_function_call——
            # transcript 落盘按字母序重排键，重载后插入序不同，不排序会让重载前后出站字节不同。
            result.append({"type": "function_call", "call_id": block["id"], "name": block["name"],
                           "arguments": json.dumps(block["input"], ensure_ascii=False, sort_keys=True)})
            continue
        if role == "user" and kind == "tool_result":
            result.append({"type": "function_call_output", "call_id": block["tool_use_id"],
                           "output": str(block.get("content") or "")})
    while role == "assistant" and result and result[-1].get("type") == "reasoning":
        result.pop()
    for row in result:
        row.pop("_message_index", None)
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


# LLM: 调用方按真实prompt布局显式标出新volatile尾项；远端trigger请求没有摘要指令，必须在全部历史之后降档。
# 不根据user角色猜缓存边界；只操作副本，Responses发送与压缩前缀测试须同步。
# 函数用途: 在新摘要指令之前或完整历史之后追加降档更新，绝不插入既有历史中间。
def insert_reasoning_update(items: list[dict], update: dict, *, before_volatile_suffix: bool) -> list[dict]:
    position = len(items) - int(bool(items) and before_volatile_suffix)
    return [*items[:position], update, *items[position:]]


# LLM: 展示回调失败不能改变模型完成状态；此函数不生成或执行工具。
# 函数用途: 安全通知 TUI 一个流式增量或思考完成事件。
def _notify(callback, value: str) -> None:
    if callable(callback):
        try:
            callback(value)
        except Exception:
            pass


# LLM: response.completed/incomplete 是终态事实；EOF 与 failed 不冒充完整回复，工具参数只从最终 output 取。
#   error / response.failed 交给 responses_failure.failed_event_error 按服务商错误码分类（上下文超限走压缩恢复等），不再一律报失败。
#   终态 output 为空时改用流里逐条的 response.output_item.done（同一组完整条目）：ChatGPT 订阅接口终态不回带 output
#   （09-30 实测：函数调用只在 output_item.done 里，completed.output 为 []），不按服务商地址分支。终态有 output 仍以它为准。
# 函数用途: 消费SSE/WS共用事件，按真实id/index校正done，非空terminal权威，EOF索引保持未知。
def collect_response(lines, on_chunk, on_thinking) -> dict:
    text, thinking, done_items, done_positions = [], [], [], {}
    done_conflict = False
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
            raise failed_event_error(event)
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
        if kind == "response.output_item.done" and isinstance(event.get("item"), dict):
            done_conflict = _record_done_item(done_items, done_positions, event) or done_conflict
        if kind in {"response.completed", "response.incomplete"}:
            terminal = event.get("response")
            break
    if thinking and not thinking_closed:
        _notify(getattr(on_thinking, "complete", None), "".join(thinking))
    if not isinstance(terminal, dict):
        return _CollectedResponse({"status": "incomplete", "incomplete_details": {"reason": "stream_eof"},
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "".join(text)}]}]}, (None,))
    if not terminal.get("output") and done_items:
        return _done_output_response(terminal, done_items, conflict=done_conflict)
    return terminal


# LLM: text保全体正文；collector私有来源属性只在此消费，非空terminal按原数组位置，缺失index不补猜，冲突工具仍原malformed。
# 函数用途: 解析最终输出为公共 ModelResponse 字段，原生 reasoning 密文永不当正文。
def response_fields(obj: dict, model: str) -> dict:
    status = obj.get("status")
    if status not in {"completed", "incomplete"}:
        raise ProviderResponseError("Responses 未返回有效完成状态。")
    text, blocks, calls = [], [], []
    malformed = isinstance(obj, _CollectedResponse) and obj.output_identity_conflict
    for position, item in enumerate(obj.get("output", [])):
        output_index = obj.output_indices[position] if isinstance(obj, _CollectedResponse) else position
        kind = item.get("type")
        if kind == "message":
            visible = [response_text_block(item, part, output_index, content_index)
                       for content_index, part in enumerate(item.get("content", []))
                       if part.get("type") in {"output_text", "refusal"}]
            text.extend(block["text"] for block in visible)
            blocks.extend(visible)
            continue
        if kind == "reasoning" and (raw := reasoning_item(item)):
            blocks.append({"type": "responses_reasoning", "model": model, "item": raw})
            continue
        if kind == "compaction" and (raw := compaction_item(item)):
            blocks.append({"type": "responses_compaction", "model": model, "item": raw})
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
