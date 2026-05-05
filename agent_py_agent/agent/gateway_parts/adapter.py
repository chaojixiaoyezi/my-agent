from __future__ import annotations

"""LLM: translates file-adapter inbox messages into gateway ask requests and outbox replies.

给人看的解释：
外部聊天工具或 TUI 可以往 adapter inbox 丢 JSON。
这个文件负责取走这些消息，转成 gateway ask，请求完成后再把回复写到 outbox。
它是外部文件协议和内部 gateway 协议之间的转换层。
"""

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .io import read_json_file, write_json_file
from .logging import _report_gateway_side_effect_error
from .paths import AdapterPaths, GatewayPaths
from .recovery import _archive_gateway_request
from .runtime import submit_gateway_ask, wait_for_gateway_response

if TYPE_CHECKING:
    from ..core import SimpleAgent


def _process_single_adapter_message(
    agent: SimpleAgent,
    processing_path: Path,
    payload: dict,
    gateway_paths_obj: GatewayPaths,
    adapter_paths_obj: AdapterPaths,
    timeout: float,
) -> bool:
    """Process a single claimed adapter message. Returns True if processed."""
    message_id = _adapter_message_id(payload, processing_path)
    prompt = _adapter_message_prompt(payload)
    output_path = _adapter_output_path(adapter_paths_obj, message_id)
    started_at = time.time()
    if not prompt:
        write_json_file(output_path, _empty_prompt_adapter_response(message_id, payload, processing_path, started_at))
        _archive_adapter_message(processing_path, adapter_paths_obj.failed)
        return True
    request_id, request_path, gateway_response = _submit_adapter_gateway_request(
        agent, gateway_paths_obj, payload, prompt
    )
    response = wait_for_gateway_response(gateway_paths_obj, request_id, timeout)
    if not response:
        response = _timeout_adapter_gateway_response(request_id, payload, request_path, started_at, timeout)
        _record_late_pending(adapter_paths_obj, request_id, timeout)
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
        prompt=prompt,
        inject=[str(item) for item in payload.get("inject", [])],
        prompt_files=[str(item) for item in payload.get("prompt_files", [])],
        save=not bool(payload.get("no_save", False)),
        include_prompt=bool(payload.get("include_prompt", False)),
        agent=agent,
    )


def _timeout_adapter_gateway_response(
    request_id: str,
    payload: dict,
    request_path: Path,
    started_at: float,
    timeout: float,
) -> dict:
    return {
        "id": request_id,
        "kind": "ask",
        "ok": False,
        "status": "timeout",
        "error_code": "GATEWAY_TIMEOUT",
        "error": f"timeout after {timeout}s",
        "created_at": payload.get("created_at", 0),
        "started_at": started_at,
        "ended_at": time.time(),
        "response": "",
        "request_file": str(request_path),
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
    gateway_paths_obj: GatewayPaths,
    adapter_paths_obj: AdapterPaths,
    timeout: float,
    limit: int = 20,
) -> int:
    """LLM contract: process one bounded batch of adapter inbox messages.

    Human version:
    外部工具把 JSON 消息放进 adapter inbox。这里每次拿一批，转成 gateway ask，
    等结果，再把回复写进 outbox。limit 防止一次循环无限处理。
    """

    for path in (
        adapter_paths_obj.inbox,
        adapter_paths_obj.processing,
        adapter_paths_obj.done,
        adapter_paths_obj.failed,
        adapter_paths_obj.outbox,
    ):
        path.mkdir(parents=True, exist_ok=True)
    processed = 0
    for message_path in sorted(adapter_paths_obj.inbox.glob("*.json")):
        if limit > 0 and processed >= limit:
            break
        processing_path = adapter_paths_obj.processing / message_path.name
        try:
            message_path.replace(processing_path)
        except OSError as exc:
            _report_gateway_side_effect_error("adapter_claim_message", message_path.stem, exc)
            continue
        payload = read_json_file(processing_path)
        _process_single_adapter_message(
            agent,
            processing_path,
            payload,
            gateway_paths_obj,
            adapter_paths_obj,
            timeout,
        )
        processed += 1
    return processed


def _archive_adapter_message(path: Path, target_dir: Path) -> None:
    """LLM contract: archive an adapter message and report archive failures.

    Human version:
    adapter 消息处理完要归档。归档失败不是业务结果失败，但不能静默吞掉，所以会打到 stderr。
    """

    try:
        _archive_gateway_request(path, target_dir)
    except OSError as exc:
        _report_gateway_side_effect_error("archive_adapter_message", path.stem, exc)


def _adapter_message_id(payload: dict, path: Path) -> str:
    """LLM contract: pick a stable adapter message id from payload or filename.

    Human version:
    外部工具可能叫 `id`，也可能叫 `message_id`。都没有时用文件名，保证 outbox 一定有稳定名字。
    """

    return str(payload.get("id") or payload.get("message_id") or path.stem).strip() or path.stem


def _adapter_message_prompt(payload: dict) -> str:
    """LLM contract: normalize adapter payload text into a prompt string.

    Human version:
    外部系统字段名不一定统一，所以按 `prompt/text/message/content` 这个顺序找第一段非空文本。
    """

    for key in ("prompt", "text", "message", "content"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def _adapter_output_path(paths: AdapterPaths, message_id: str) -> Path:
    """LLM contract: map adapter message id to outbox response path.

    Human version:
    每条 adapter 输入消息都会对应 `outbox/<message_id>.json`，外部程序按这个文件取回复。
    """

    return paths.outbox / f"{message_id}.json"


def _record_late_pending(paths: AdapterPaths, request_id: str, original_timeout: float) -> None:
    """Record a timed-out request so late responses can be detected later."""

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
    """Scan late_pending.jsonl and check if responses have arrived in gateway responses.

    Human version:
    调用方超时后，响应可能迟到。这里扫描 late_pending.jsonl，检查 gateway responses 目录是否有对应响应。
    找到的响应会被标记为已检查，未找到的保留在索引中。
    """

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
