"""Response rendering and response-file polling for gateway CLI output.

Human status lines show cumulative context pressure when available, while JSON
mode preserves the raw fields. Client polling also lives here so chat/TUI/gateway
ask share one response-file load-error path and one stat-based "read only when
changed" check.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .io import gateway_response_path, read_json_file_report
from .paths import GatewayPaths
from .request_errors import gateway_response_load_error_response


@dataclass
class GatewayResponsePollState:
    stat_signature: tuple[int, int] | None = None


def current_context_token_estimate(payload: Any) -> int:
    """Return the token estimate humans expect for current context pressure."""
    for key in (
        "current_context_token_estimate",
        "prompt_token_estimate",
        "turn_token_estimate",
        "cumulative_token_estimate",
    ):
        value = _int_value(payload, key)
        if value > 0:
            return value
    return 0


def _int_value(payload: Any, key: str) -> int:
    raw = payload.get(key) if isinstance(payload, dict) else getattr(payload, key, 0)
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return 0


def print_gateway_response(
    payload: dict,
    *,
    json_mode: bool = False,
    show_prompt: bool = False,
    suppress_response: bool = False,
) -> int:
    if json_mode:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if payload.get("ok") else 2

    if show_prompt and payload.get("prompt"):
        print("===== FINAL PROMPT =====")
        print(payload.get("prompt", ""))
        print("===== RESPONSE =====")

    if not suppress_response:
        response = str(payload.get("response", "") or "")
        if response:
            print(response)
        else:
            print(str(payload.get("error", "gateway 请求没有返回内容。") or "gateway 请求没有返回内容。"))

    # Human CLI status uses cumulative context pressure; JSON mode keeps both token fields.
    status_line = (
        f"request_id={payload.get('id', '-')}; "
        f"status={payload.get('status', '-')}; "
        f"backend={payload.get('backend', '-')}; "
        f"tool_rounds={payload.get('tool_rounds', 0)}; "
        f"ctx_tokens≈{current_context_token_estimate(payload)}; "
        f"prompt_tokens≈{payload.get('prompt_token_estimate', 0)}; "
        f"resume_context={1 if payload.get('memory_resume_context_injected') else 0}"
    )
    print(f"\n[{status_line}]")
    return 0 if payload.get("ok") else 2


def read_gateway_response(paths: GatewayPaths, request_id: str) -> dict[str, Any]:
    return read_gateway_response_file(
        gateway_response_path(paths, request_id),
        request_id=request_id,
        context="gateway.response_renderer.response.read",
    )


def read_gateway_response_file(
    response_path,
    *,
    request_id: str | None = None,
    context: str = "gateway.response.read",
) -> dict[str, Any]:
    report = read_json_file_report(response_path, context=context)
    if report.load_error is not None:
        return gateway_response_load_error_response(response_path, report.load_error, request_id=request_id)
    return report.payload


def read_gateway_response_file_when_ready(
    response_path,
    *,
    state: GatewayResponsePollState,
    request_id: str | None = None,
    context: str = "gateway.response.read",
) -> dict[str, Any]:
    path = Path(response_path)
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {}
    except OSError:
        return read_gateway_response_file(path, request_id=request_id, context=context)

    signature = (int(stat.st_mtime_ns), int(stat.st_size))
    if state.stat_signature == signature:
        return {}
    state.stat_signature = signature
    return read_gateway_response_file(path, request_id=request_id, context=context)
