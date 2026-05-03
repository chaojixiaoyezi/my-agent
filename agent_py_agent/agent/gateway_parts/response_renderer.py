from __future__ import annotations

"""Response rendering for gateway CLI output.

This module is derived from runtime.py split. It contains response display
functions that were previously in that file.
"""

import json
from typing import Any

from .io import gateway_response_path, read_json_file
from .paths import GatewayPaths


def print_gateway_response(payload: dict, *, json_mode: bool = False, show_prompt: bool = False) -> int:
    """LLM contract: render a gateway response for CLI users.

    Human version:
    默认打印人看的回复；`--json` 打印完整结构，方便脚本或外部工具继续处理。
    """
    if json_mode:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if payload.get("ok") else 2

    if show_prompt and payload.get("prompt"):
        print("===== FINAL PROMPT =====")
        print(payload.get("prompt", ""))
        print("===== RESPONSE =====")

    response = str(payload.get("response", "") or "")
    if response:
        print(response)
    else:
        print(str(payload.get("error", "gateway 请求没有返回内容。") or "gateway 请求没有返回内容。"))

    status_line = (
        f"request_id={payload.get('id', '-')}; "
        f"status={payload.get('status', '-')}; "
        f"backend={payload.get('backend', '-')}; "
        f"tool_rounds={payload.get('tool_rounds', 0)}; "
        f"prompt_tokens≈{payload.get('prompt_token_estimate', 0)}; "
        f"resume_context={1 if payload.get('memory_resume_context_injected') else 0}"
    )
    print(f"\n[{status_line}]")
    return 0 if payload.get("ok") else 2


def read_gateway_response(paths: GatewayPaths, request_id: str) -> dict[str, Any]:
    """Read a gateway response file."""
    return read_json_file(gateway_response_path(paths, request_id))