from __future__ import annotations

"""LLM: translates file-adapter inbox messages into gateway ask requests and outbox replies.

给人看的解释：
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


# LLM: adapter internals stay bundled so file-protocol fields do not widen gateway call sites.
@dataclass(frozen=True)
class _AdapterMessageContext:

    agent: SimpleAgent
    processing_path: Path
    payload: dict
    gateway_paths_obj: GatewayPaths
    adapter_paths_obj: AdapterPaths
    timeout: float


@dataclass(frozen=True)
class _AdapterTimeoutContext:

    request_id: str
    payload: dict
    request_path: Path
    started_at: float
    timeout: float


@dataclass(frozen=True)
class ProcessFileAdapterOptions:
    gateway_paths_obj: GatewayPaths
    adapter_paths_obj: AdapterPaths
    timeout: float
    limit: int = 20


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


def _archive_adapter_message(path: Path, target_dir: Path) -> None:

    try:
        _archive_gateway_request(path, target_dir)
    except OSError as exc:
        _report_gateway_side_effect_error("archive_adapter_message", path.stem, exc)


def _adapter_message_id(payload: dict, path: Path) -> str:

    return str(payload.get("id") or payload.get("message_id") or path.stem).strip() or path.stem


def _adapter_message_prompt(payload: dict) -> str:

    for key in ("prompt", "text", "message", "content"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def _adapter_output_path(paths: AdapterPaths, message_id: str) -> Path:

    return paths.outbox / f"{message_id}.json"


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


def _mark_late_response_if_arrived(paths: AdapterPaths, entry: dict) -> dict:
    request_id = entry.get("request_id", "")
    gateway_responses_dir = paths.root.parent / "gateway" / "responses"
    response_path = gateway_responses_dir / f"{request_id}.json"
    if response_path.exists():
        entry["checked"] = True
        entry["late_response_at"] = time.time()
    return entry


def _rewrite_unchecked_late_entries(late_path: Path, entries: list[dict]) -> None:
    lines = [json.dumps(entry, ensure_ascii=False) for entry in entries if not entry.get("checked")]
    content = "\n".join(lines) + ("\n" if lines else "")
    try:
        late_path.write_text(content, encoding="utf-8")
    except OSError as exc:
        _report_gateway_side_effect_error("check_late_responses_cleanup", "", exc)
