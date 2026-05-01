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
        message_id = _adapter_message_id(payload, processing_path)
        prompt = _adapter_message_prompt(payload)
        output_path = _adapter_output_path(adapter_paths_obj, message_id)
        started_at = time.time()
        if not prompt:
            response = {
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
            write_json_file(output_path, response)
            _archive_adapter_message(processing_path, adapter_paths_obj.failed)
            processed += 1
            continue

        request_id, request_path, gateway_response = submit_gateway_ask(
            gateway_paths_obj,
            prompt=prompt,
            inject=[str(item) for item in payload.get("inject", [])],
            prompt_files=[str(item) for item in payload.get("prompt_files", [])],
            save=not bool(payload.get("no_save", False)),
            include_prompt=bool(payload.get("include_prompt", False)),
            agent=agent,
        )
        response = wait_for_gateway_response(gateway_paths_obj, request_id, timeout)
        if not response:
            response = {
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
            _record_late_pending(adapter_paths_obj, request_id, timeout)
        adapter_response = {
            "adapter_message_id": message_id,
            "conversation_id": payload.get("conversation_id", ""),
            "user": payload.get("user", ""),
            "gateway_request_id": request_id,
            "gateway_request_file": str(request_path),
            "gateway_response_file": str(gateway_response),
            "source_file": str(processing_path),
            "ok": bool(response.get("ok", False)),
            "status": response.get("status", "unknown"),
            "error_code": response.get("error_code", ""),
            "response": response.get("response", ""),
            "error": response.get("error", ""),
            "payload": response,
            "created_at": payload.get("created_at", 0),
            "started_at": started_at,
            "ended_at": time.time(),
        }
        write_json_file(output_path, adapter_response)
        _archive_adapter_message(
            processing_path,
            adapter_paths_obj.done if adapter_response["ok"] else adapter_paths_obj.failed,
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
    for line in late_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("checked"):
            continue
        request_id = entry.get("request_id", "")
        # Check gateway responses directory for the response
        from .io import gateway_response_path
        from .paths import gateway_paths
        # We need to check the gateway responses directory
        # The paths.root is the adapter root, not gateway root
        # We'll check if a response file exists in the gateway responses
        # For now, we'll use a simple check based on the request_id
        gateway_responses_dir = paths.root.parent / "gateway" / "responses"
        response_path = gateway_responses_dir / f"{request_id}.json"
        if response_path.exists():
            entry["checked"] = True
            entry["late_response_at"] = time.time()
            results.append(entry)
        remaining.append(entry)
    # Rewrite file with only unchecked entries
    try:
        with late_path.open("w", encoding="utf-8") as f:
            for entry in remaining:
                if not entry.get("checked"):
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        _report_gateway_side_effect_error("check_late_responses_cleanup", "", exc)
    return results
