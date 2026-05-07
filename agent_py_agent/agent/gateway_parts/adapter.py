# LLM: Gateway service module; keep file-queue, daemon, HTTP, and audit contracts stable.
# 模块用途: 拆分 gateway 请求队列、守护进程、HTTP 处理和响应渲染逻辑。

from __future__ import annotations

"""translates file-adapter inbox messages into gateway ask requests and outbox replies.

外部聊天工具或 TUI 可以往 adapter inbox 丢 JSON。
这个文件负责取走这些消息，转成 gateway ask，请求完成后再把回复写到 outbox。
它是外部文件协议和内部 gateway 协议之间的转换层。
"""

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .io import read_json_file, write_json_file
from .logging import _report_gateway_side_effect_error
from .paths import AdapterPaths, GatewayPaths
from .recovery import _archive_gateway_request
from .runtime import GatewayAskParams, submit_gateway_ask, wait_for_gateway_response

if TYPE_CHECKING:
    from ..core import SimpleAgent


# LLM: _AdapterMessageContext 属于网关守护进程的类边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 类用途: 集中保存adapter消息上下文字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class _AdapterMessageContext:

    agent: SimpleAgent
    processing_path: Path
    payload: dict
    gateway_paths_obj: GatewayPaths
    adapter_paths_obj: AdapterPaths
    timeout: float


# LLM: _AdapterTimeoutContext 属于网关守护进程的类边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 类用途: 集中保存adapter超时上下文字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class _AdapterTimeoutContext:

    request_id: str
    payload: dict
    request_path: Path
    started_at: float
    timeout: float


# LLM: ProcessFileAdapterOptions 属于网关守护进程的类边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 类用途: 集中保存process文件adapter选项字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class ProcessFileAdapterOptions:
    gateway_paths_obj: GatewayPaths
    adapter_paths_obj: AdapterPaths
    timeout: float
    limit: int = 20


# LLM: _process_single_adapter_message 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 推进单个adapter消息的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
def _process_single_adapter_message(
    context: _AdapterMessageContext,
) -> bool:
    agent = context.agent
    processing_path = context.processing_path
    payload = context.payload
    adapter_paths_obj = context.adapter_paths_obj
    message_id = _adapter_message_id(payload, processing_path)
    prompt = _adapter_message_prompt(payload)
    output_path = _adapter_output_path(adapter_paths_obj, message_id)
    started_at = time.time()
    if not prompt:
        write_json_file(output_path, _empty_prompt_adapter_response(message_id, payload, processing_path, started_at))
        _archive_adapter_message(processing_path, adapter_paths_obj.failed)
        return True
    request_id, request_path, gateway_response = _submit_adapter_gateway_request(
        agent, context.gateway_paths_obj, payload, prompt
    )
    response = wait_for_gateway_response(context.gateway_paths_obj, request_id, context.timeout)
    if not response:
        response = _timeout_adapter_gateway_response(
            _AdapterTimeoutContext(request_id, payload, request_path, started_at, context.timeout)
        )
        _record_late_pending(adapter_paths_obj, request_id, context.timeout)
    adapter_response = _build_adapter_outbox_response(
        {
            "message_id": message_id,
            "payload": payload,
            "processing_path": processing_path,
            "request_id": request_id,
            "request_path": request_path,
            "gateway_response": gateway_response,
            "response": response,
            "started_at": started_at,
        }
    )
    write_json_file(output_path, adapter_response)
    _archive_adapter_message(
        processing_path,
        adapter_paths_obj.done if adapter_response["ok"] else adapter_paths_obj.failed,
    )
    return True


# LLM: _empty_prompt_adapter_response 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理empty提示词adapter响应相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
def _empty_prompt_adapter_response(message_id: str, payload: dict, processing_path: Path, started_at: float) -> dict:
    return {
        "id": message_id,
        "ok": False,
        "status": "failed",
        "error_code": "ADAPTER_EMPTY_PROMPT",
        "error": "adapter message 缺少 prompt/text/message/content 字段。",
        "created_at": payload.get("created_at", 0),
        "started_at": started_at,
        "ended_at": time.time(),
        "source_file": str(processing_path),
    }


# LLM: _submit_adapter_gateway_request 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 发送adapter网关请求请求或消息，并把外部响应转换成内部可处理结果；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def _submit_adapter_gateway_request(
    agent: SimpleAgent,
    gateway_paths_obj: GatewayPaths,
    payload: dict,
    prompt: str,
) -> tuple[str, Path, Path]:
    return submit_gateway_ask(
        gateway_paths_obj,
        params=GatewayAskParams(
            prompt=prompt,
            inject=[str(item) for item in payload.get("inject", [])],
            prompt_files=[str(item) for item in payload.get("prompt_files", [])],
            save=not bool(payload.get("no_save", False)),
            include_prompt=bool(payload.get("include_prompt", False)),
            agent=agent,
        ),
    )


# LLM: _timeout_adapter_gateway_response 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理超时adapter网关响应相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
def _timeout_adapter_gateway_response(context: _AdapterTimeoutContext) -> dict:
    return {
        "id": context.request_id,
        "kind": "ask",
        "ok": False,
        "status": "timeout",
        "error_code": "GATEWAY_TIMEOUT",
        "error": f"timeout after {context.timeout}s",
        "created_at": context.payload.get("created_at", 0),
        "started_at": context.started_at,
        "ended_at": time.time(),
        "response": "",
        "request_file": str(context.request_path),
    }


# LLM: _build_adapter_outbox_response 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 构建adapteroutbox响应所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _build_adapter_outbox_response(context: dict) -> dict:
    message_id = context["message_id"]
    payload = context["payload"]
    response = context["response"]
    return {
        "adapter_message_id": message_id,
        "conversation_id": payload.get("conversation_id", ""),
        "user": payload.get("user", ""),
        "gateway_request_id": context["request_id"],
        "gateway_request_file": str(context["request_path"]),
        "gateway_response_file": str(context["gateway_response"]),
        "source_file": str(context["processing_path"]),
        "ok": bool(response.get("ok", False)),
        "status": response.get("status", "unknown"),
        "error_code": response.get("error_code", ""),
        "response": response.get("response", ""),
        "error": response.get("error", ""),
        "payload": response,
        "created_at": payload.get("created_at", 0),
        "started_at": context["started_at"],
        "ended_at": time.time(),
    }


# LLM: process_file_adapter_once 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 推进文件adapteronce的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
def process_file_adapter_once(
    agent: SimpleAgent,
    *,
    params: ProcessFileAdapterOptions | None = None,
    gateway_paths_obj: GatewayPaths,
    adapter_paths_obj: AdapterPaths,
    timeout: float,
    limit: int = 20,
) -> int:
    options = params or ProcessFileAdapterOptions(gateway_paths_obj, adapter_paths_obj, timeout, limit)
    for path in (
        options.adapter_paths_obj.inbox,
        options.adapter_paths_obj.processing,
        options.adapter_paths_obj.done,
        options.adapter_paths_obj.failed,
        options.adapter_paths_obj.outbox,
    ):
        path.mkdir(parents=True, exist_ok=True)
    processed = 0
    for message_path in sorted(options.adapter_paths_obj.inbox.glob("*.json")):
        if options.limit > 0 and processed >= options.limit:
            break
        processing_path = options.adapter_paths_obj.processing / message_path.name
        try:
            message_path.replace(processing_path)
        except OSError as exc:
            _report_gateway_side_effect_error("adapter_claim_message", message_path.stem, exc)
            continue
        payload = read_json_file(processing_path)
        _process_single_adapter_message(
            _AdapterMessageContext(
                agent,
                processing_path,
                payload,
                options.gateway_paths_obj,
                options.adapter_paths_obj,
                options.timeout,
            )
        )
        processed += 1
    return processed


# LLM: _archive_adapter_message 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 写入adapter消息的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动请求队列、租约文件、进程状态和响应渲染，调用方依赖写入顺序和文件格式。
def _archive_adapter_message(path: Path, target_dir: Path) -> None:

    try:
        _archive_gateway_request(path, target_dir)
    except OSError as exc:
        _report_gateway_side_effect_error("archive_adapter_message", path.stem, exc)


# LLM: _adapter_message_id 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理adapter消息id相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
def _adapter_message_id(payload: dict, path: Path) -> str:

    return str(payload.get("id") or payload.get("message_id") or path.stem).strip() or path.stem


# LLM: _adapter_message_prompt 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理adapter消息提示词相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
def _adapter_message_prompt(payload: dict) -> str:

    for key in ("prompt", "text", "message", "content"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


# LLM: _adapter_output_path 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理adapteroutput路径相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
def _adapter_output_path(paths: AdapterPaths, message_id: str) -> Path:

    return paths.outbox / f"{message_id}.json"


# LLM: _record_late_pending 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 写入latepending的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动请求队列、租约文件、进程状态和响应渲染，调用方依赖写入顺序和文件格式。
def _record_late_pending(paths: AdapterPaths, request_id: str, original_timeout: float) -> None:

    late_path = paths.root / "late_pending.jsonl"
    entry = {
        "request_id": request_id,
        "timeout_at": time.time(),
        "original_timeout": original_timeout,
        "checked": False,
    }
    try:
        with late_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        _report_gateway_side_effect_error("record_late_pending", request_id, exc)


# LLM: check_late_responses 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 校验lateresponses需要的输入和状态，不满足时把错误明确反馈给调用方；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
def check_late_responses(paths: AdapterPaths) -> list[dict]:

    late_path = paths.root / "late_pending.jsonl"
    if not late_path.exists():
        return []
    results: list[dict] = []
    remaining: list[dict] = []
    for entry in _iter_late_pending_entries(late_path):
        updated = _mark_late_response_if_arrived(paths, entry)
        if updated.get("checked"):
            results.append(updated)
        remaining.append(updated)
    _rewrite_unchecked_late_entries(late_path, remaining)
    return results


# LLM: _iter_late_pending_entries 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理迭代latependingentries相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
def _iter_late_pending_entries(late_path: Path) -> list[dict]:
    entries: list[dict] = []
    for line in late_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not entry.get("checked"):
            entries.append(entry)
    return entries


# LLM: _mark_late_response_if_arrived 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 更新late响应ifarrived对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新请求队列、租约文件、进程状态和响应渲染，需避免破坏既有状态机约定。
def _mark_late_response_if_arrived(paths: AdapterPaths, entry: dict) -> dict:
    request_id = entry.get("request_id", "")
    gateway_responses_dir = paths.root.parent / "gateway" / "responses"
    response_path = gateway_responses_dir / f"{request_id}.json"
    if response_path.exists():
        entry["checked"] = True
        entry["late_response_at"] = time.time()
    return entry


# LLM: _rewrite_unchecked_late_entries 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理rewriteuncheckedlateentries相关的数据流，连接当前职责的前后步骤；关键副作用: 会改动请求队列、租约文件、进程状态和响应渲染，调用方依赖写入顺序和文件格式。
def _rewrite_unchecked_late_entries(late_path: Path, entries: list[dict]) -> None:
    lines = [json.dumps(entry, ensure_ascii=False) for entry in entries if not entry.get("checked")]
    content = "\n".join(lines) + ("\n" if lines else "")
    try:
        late_path.write_text(content, encoding="utf-8")
    except OSError as exc:
        _report_gateway_side_effect_error("check_late_responses_cleanup", "", exc)
