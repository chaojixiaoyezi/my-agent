"""Response rendering and canonical-terminal polling for gateway CLI output.

Human status lines show cumulative context pressure when available, while JSON
mode preserves the raw fields. Client polling also lives here so chat/TUI/gateway
ask share one response-file load-error path and one stat-based "read only when
changed" check.

Stat-based change detection compares the full file identity
(dev/inode/size/mtime/ctime) and distrusts any file whose mtime is inside the
coarse 2-second trust window: a same-tick atomic replace must never be mistaken
for "unchanged" (see ``common/cache_freshness``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..common.cache_freshness import cache_entry_trustworthy, cache_stat_signature
from ..runtime_errors import DataCorruptionError, runtime_error_report
from .io import (
    GatewayJsonReadReport,
    gateway_response_path,
    read_json_file_report,
    validated_gateway_request_fingerprint,
)
from .paths import GatewayPaths
from .request_errors import gateway_client_error_message, gateway_response_load_error_response


# LLM: 轮询去重的唯一状态：stat_signature 是上次已读回的完整文件代次指纹，
#   类型为 cache_freshness.cache_stat_signature 的五元组；None 表示这个文件还没读过。
# 类用途: 保存客户端轮询一个响应/终态文件时“上次读到哪一代”的事实，供去重判断使用。
@dataclass
class GatewayResponsePollState:
    stat_signature: tuple[int, int, int, int, int] | None = None


def project_gateway_stream_chunk(payload: dict[str, Any]) -> tuple[str, bool]:
    """Project one typed stream row and identify terminal response text.

    Provider deltas are tentative and never reach this boundary.  Commentary
    may be shown while work continues, but only an explicit ``assistant_final``
    event (or a legacy untyped row) can prove that the final response was
    already streamed and therefore suppress response-file rendering.
    Unknown typed rows fail closed instead of exposing internal protocol text.
    """
    text = str(payload.get("text", "") or "")
    kind = str(payload.get("kind", "") or "").strip()
    if not text:
        return "", False
    if not kind:
        return text, True
    if kind == "assistant_final":
        return text, True
    if kind == "assistant_commentary":
        return text, False
    level = str(payload.get("verbose_level") or "off").strip().lower()
    if kind == "tool_progress":
        return (text, False) if level in {"on", "full"} else ("", False)
    if kind == "runtime_progress":
        return (text, False) if level == "full" else ("", False)
    return "", False


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


# LLM: A user stop is a typed control result, not an assistant message; UI layers
# must suppress it without matching localized response prose.
# 中文说明：用户停止属于结构化控制结果，不是助手回复；界面层只能依据状态字段静默处理，
# 不能匹配“当前任务已停止”之类的自然语言。
def is_silent_user_stop(payload: Any) -> bool:
    """Return whether a typed user-stop result must stay out of chat history."""
    if isinstance(payload, dict):
        status = str(payload.get("status", "") or "").strip().lower()
        error_code = str(payload.get("error_code", "") or "").strip().upper()
        return status == "interrupted" and error_code == "INTERRUPTED"
    runtime_status = str(getattr(payload, "runtime_status", "") or "").strip().lower()
    runtime_reason = str(getattr(payload, "runtime_reason", "") or "").strip().lower()
    return runtime_status == "cancelled" and runtime_reason == "user_stop"


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

    if not suppress_response and not is_silent_user_stop(payload):
        response = str(payload.get("response", "") or "")
        if response:
            print(response)
        else:
            print(
                str(
                    payload.get("user_error")
                    or gateway_client_error_message(payload.get("error_code"))
                )
            )

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


# LLM: 去重判定 = 完整文件代次指纹相同且这一代已离开粗 mtime 信任窗口。窗口内一律重读并当“变了”返回，
#   否则同时间片的原子替换会被漏掉；调用方（CLI 轮询循环）对同内容重复只做覆盖，不会重复展示。
# 函数用途: 未处理的响应文件刚建好或刚换过时读回结构化响应，其余情况返回空字典表示“这轮不用读”。
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

    signature = cache_stat_signature(stat)
    if state.stat_signature == signature and cache_entry_trustworthy(stat.st_mtime_ns):
        return {}
    state.stat_signature = signature
    return read_gateway_response_file(path, request_id=request_id, context=context)


# LLM: This is the shared canonical-envelope validator for HTTP, worker, CLI, TUI, and projectors.
# It verifies schema, filename/inner ids, and every present immutable request fingerprint.
# 函数用途: 读取并校验 Gateway 唯一终态归档，返回带结构化错误的报告。
def read_gateway_terminal_envelope_report(
    terminal_path: Path,
    *,
    request_id: str | None = None,
    context: str = "gateway.terminal.read",
) -> GatewayJsonReadReport:
    path = Path(terminal_path)
    if not path.exists():
        return GatewayJsonReadReport({})
    report = read_json_file_report(path, context=context)
    expected_id = str(request_id or path.stem).strip()
    if report.load_error is not None:
        return report
    payload = report.payload
    terminal_response = payload.get("terminal_response") if isinstance(payload, dict) else None
    actual_id = str(payload.get("id") or "").strip() if isinstance(payload, dict) else ""
    response_id = (
        str(terminal_response.get("id") or "").strip()
        if isinstance(terminal_response, dict)
        else ""
    )
    try:
        valid = (
            payload.get("schema_version") == "gateway_terminal_request.v1"
            and bool(expected_id)
            and actual_id == expected_id
            and isinstance(terminal_response, dict)
            and bool(terminal_response)
            and response_id == expected_id
        )
        if not valid:
            raise DataCorruptionError("gateway canonical terminal response is invalid")
        validated_gateway_request_fingerprint(payload, expected_id)
    except DataCorruptionError as exc:
        return GatewayJsonReadReport(
            {},
            runtime_error_report(exc, context=context),
        )
    return GatewayJsonReadReport(dict(payload))


# LLM: Only a validated canonical envelope may be unwrapped as a response. Load/corruption errors
# remain typed client failures rather than being mistaken for missing or successful completion.
# 函数用途: 从通过校验的唯一终态归档取出完整最终答复。
def read_gateway_terminal_response_file(
    terminal_path: Path,
    *,
    request_id: str | None = None,
    context: str = "gateway.terminal.read",
) -> dict[str, Any]:
    path = Path(terminal_path)
    expected_id = str(request_id or path.stem).strip()
    report = read_gateway_terminal_envelope_report(
        path,
        request_id=expected_id,
        context=context,
    )
    if report.load_error is not None:
        return gateway_response_load_error_response(
            path,
            report.load_error,
            request_id=expected_id,
        )
    terminal_response = report.payload.get("terminal_response")
    return dict(terminal_response) if isinstance(terminal_response, dict) else {}


# LLM: Stat caching is only an IO optimization; a changed canonical terminal file is still fully
# schema/identity validated before the caller can observe completion. Same identity is only trusted
# once the file left the coarse mtime window, so a same-tick atomic replace is re-read.
# 函数用途: 只在唯一终态归档新建或真的换过代时读取完整答复；窗口内重复读取作为幂等覆盖，不重复收口。
def read_gateway_terminal_response_file_when_ready(
    terminal_path: Path,
    *,
    state: GatewayResponsePollState,
    request_id: str | None = None,
    context: str = "gateway.terminal.read",
) -> dict[str, Any]:
    path = Path(terminal_path)
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {}
    except OSError:
        return read_gateway_terminal_response_file(
            path,
            request_id=request_id,
            context=context,
        )
    signature = cache_stat_signature(stat)
    if state.stat_signature == signature and cache_entry_trustworthy(stat.st_mtime_ns):
        return {}
    state.stat_signature = signature
    return read_gateway_terminal_response_file(
        path,
        request_id=request_id,
        context=context,
    )
