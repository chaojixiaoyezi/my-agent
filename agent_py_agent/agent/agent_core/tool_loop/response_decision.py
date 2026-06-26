
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from ...backends import ModelResponse
from ...tooling.content_recovery_mode import (
    LongContentRecoveryRequest,
    long_content_recovery_block_context,
    long_content_recovery_context,
    long_content_recovery_payload_too_large,
    long_content_recovery_state_from_records,
)
from .._runtime_params import ToolLoopExecuteParams
from ..run_task_workspace_writer import current_run_task_workspace_root
from ..tool_guard.call_guardrail import tool_guardrail_records
from ..tool_guard.exploration_fuse import (
    exploration_fuse_context,
    has_pending_exploration_fuse,
    has_required_exploration_fuse,
)
from ..tool_guard.local_progress import (
    has_required_local_progress_guard,
    local_progress_guard_context,
)
from ..tool_guard.unresolved_runtime_issue import (
    has_unresolved_runtime_issues,
    unresolved_runtime_issue_context,
)
from .text_tool_call_promotion import promote_text_tool_calls_if_native

_PROTECTED_TOOL_MARKERS = (
    "[tool-record",
    "[tool-output-record",
    "[/tool-call]",
)


@dataclass(frozen=True)
class ToolLoopRepairCounters:
    __test__: ClassVar[bool] = False

    protected_marker_repairs: int = 0
    local_progress_redirects: int = 0
    exploration_fuse_redirects: int = 0
    unresolved_runtime_issue_redirects: int = 0


def _inc_protected_marker(counters: ToolLoopRepairCounters) -> ToolLoopRepairCounters:
    return ToolLoopRepairCounters(
        protected_marker_repairs=counters.protected_marker_repairs + 1,
        local_progress_redirects=counters.local_progress_redirects,
        exploration_fuse_redirects=counters.exploration_fuse_redirects,
        unresolved_runtime_issue_redirects=counters.unresolved_runtime_issue_redirects,
    )


def _inc_exploration_fuse(counters: ToolLoopRepairCounters) -> ToolLoopRepairCounters:
    return ToolLoopRepairCounters(
        protected_marker_repairs=counters.protected_marker_repairs,
        local_progress_redirects=counters.local_progress_redirects,
        exploration_fuse_redirects=counters.exploration_fuse_redirects + 1,
        unresolved_runtime_issue_redirects=counters.unresolved_runtime_issue_redirects,
    )


def _inc_unresolved_runtime_issue(counters: ToolLoopRepairCounters) -> ToolLoopRepairCounters:
    return ToolLoopRepairCounters(
        protected_marker_repairs=counters.protected_marker_repairs,
        local_progress_redirects=counters.local_progress_redirects,
        exploration_fuse_redirects=counters.exploration_fuse_redirects,
        unresolved_runtime_issue_redirects=counters.unresolved_runtime_issue_redirects + 1,
    )


@dataclass(frozen=True)
class ToolLoopResponseDecisionRequest:
    __test__: ClassVar[bool] = False

    agent: object
    params: ToolLoopExecuteParams
    response: object
    counters: ToolLoopRepairCounters


@dataclass(frozen=True)
class ToolLoopResponseDecision:
    __test__: ClassVar[bool] = False

    action: str
    response: object
    calls: list[dict[str, object]]
    counters: ToolLoopRepairCounters


@dataclass(frozen=True)
class ExplorationFuseDecision:
    __test__: ClassVar[bool] = False

    action: str
    response: object
    calls: list[dict[str, object]]
    counters: ToolLoopRepairCounters


@dataclass(frozen=True)
class ExplorationFuseDecisionRequest:
    __test__: ClassVar[bool] = False

    agent: object
    params: object
    response: object
    counters: ToolLoopRepairCounters
    calls: list[dict[str, object]]


@dataclass(frozen=True)
class UnresolvedRuntimeIssueDecision:
    __test__: ClassVar[bool] = False

    action: str
    response: object
    calls: list[dict[str, object]]
    counters: ToolLoopRepairCounters


@dataclass(frozen=True)
class UnresolvedRuntimeIssueDecisionRequest:
    __test__: ClassVar[bool] = False

    agent: object
    params: object
    response: object
    counters: ToolLoopRepairCounters


@dataclass(frozen=True)
class _NoToolCallsRequest:
    agent: object
    params: ToolLoopExecuteParams
    response: object
    counters: ToolLoopRepairCounters
    has_protected_marker: bool


def tool_loop_response_decision(
    request: ToolLoopResponseDecisionRequest,
) -> ToolLoopResponseDecision:
    has_protected_marker = contains_protected_tool_marker(request.response.text)
    if has_protected_marker:
        request.params.tool_context.append(
            protected_tool_marker_repair_context(native=_native_tool_use_active(request.agent))
        )

    if not request.agent.config.enable_tools:
        final = _disabled_tools_response(request.response, has_protected_marker)
        return ToolLoopResponseDecision("break", final, [], request.counters)

    calls = _tool_calls_from_response(request)
    if calls:
        return _tool_calls_decision(request, calls)

    return _no_tool_calls_decision(
        _NoToolCallsRequest(
            request.agent,
            request.params,
            request.response,
            request.counters,
            has_protected_marker,
        )
    )


def _tool_calls_from_response(
    request: ToolLoopResponseDecisionRequest,
) -> list[dict[str, object]]:
    """Resolve this turn's tool calls, preferring native tool_use blocks.

    tool_protocol=native: the backend already emitted structured
    ``tool_use_blocks`` ({id,name,input}); flatten each to the existing
    ``{"tool": name, **input}`` dict so all downstream gates/execution stay
    unchanged. Otherwise fall back to parsing the [TOOL_CALL] text protocol.

    Step 5 修复网：native 下若本轮**没有**结构化 block 但模型把调用漏成了正文
    ``[TOOL_CALL]`` 文本，文本解析结果要过一道严格门控的提升闸（见
    ``promote_text_tool_calls_if_native``）才放行——只在全部调用都精确命中已注册工具时
    才当真实调用，避免把模型正文里的散文/拼错块误当工具调用。text 协议不进这道闸。
    """
    blocks = getattr(request.response, "tool_use_blocks", None)
    if blocks:
        return [_flatten_tool_use_block(block) for block in blocks]
    parsed = request.agent.tools.parse_tool_calls(request.response.text)
    if not _native_tool_use_active(request.agent):
        return parsed
    return promote_text_tool_calls_if_native(request.agent, request.response, parsed)


def _native_tool_use_active(agent: object) -> bool:
    from ..native_tool_protocol import native_tool_use_active

    return native_tool_use_active(agent)


def _flatten_tool_use_block(block: dict[str, object]) -> dict[str, object]:
    tool_input = block.get("input")
    flattened: dict[str, object] = dict(tool_input) if isinstance(tool_input, dict) else {}
    # tool name must win even if the model put a stray "tool" key in input.
    flattened["tool"] = str(block.get("name", "") or "")
    call_id = str(block.get("id", "") or "")
    if call_id:
        flattened.setdefault("call_id", call_id)
    return flattened


def contains_protected_tool_marker(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in _PROTECTED_TOOL_MARKERS)


def sanitize_protected_tool_marker_response(
    response: ModelResponse, *, native: bool = False
) -> ModelResponse:
    if not contains_protected_tool_marker(response.text):
        return response
    # native 下系统执行的是结构化 tool_use，不是文本 [TOOL_CALL] 块；说成「只执行真实
    # [TOOL_CALL] 块」会把 native 模型往回引到已废弃的文本协议（弱模型有训练惯性）。
    execution_note = (
        "系统只会执行结构化工具调用（tool_use）。"
        if native
        else "系统只会执行真实 [TOOL_CALL] 块。"
    )
    return ModelResponse(
        text=(
            "[assistant-response-omitted]\n"
            "模型回复包含系统内部的 tool-record/tool-output-record 标记，"
            f"该回复正文不进入后续 live prompt；{execution_note}"
        ),
        backend=response.backend,
    )


def protected_tool_marker_repair_context(*, native: bool = False) -> str:
    # native 下纠偏措辞要指向结构化工具调用，不能教模型再写文本 [TOOL_CALL]（治根护栏：
    # 原生协议禁止退回文本协议，纠错提示更不能反向把它带回去）。
    reissue = (
        "请改用结构化工具调用（tool_use）请求工具，或只基于已经存在的真实工具回执总结。"
        if native
        else "请改用真实 `[TOOL_CALL]...[/TOOL_CALL]` 请求工具，或只基于已经存在的真实工具回执总结。"
    )
    return (
        "[tool-system]\n"
        "上一轮模型回复包含系统内部的 `[tool-record]` / `[tool-output-record]` 标记。"
        "这些标记只能由工具循环在真实工具执行后写入，模型不能自行书写、复制或假装工具成功。"
        f"{reissue}"
    )


def protected_tool_marker_block_response(backend: str) -> ModelResponse:
    return ModelResponse(
        text=(
            "系统已阻止本轮结果：模型输出了系统内部的工具记录标记，"
            "但没有提供可执行的真实工具调用或可信的真实工具回执。"
            "当前不能把这次回复视为完成；请重新发起真实工具调用，"
            "或读取现有 task/subagent 状态后再汇报。"
        ),
        backend=backend,
        runtime_status="blocked",
        runtime_reason="PROTECTED_TOOL_MARKER",
    )


def _disabled_tools_response(response, has_protected_marker: bool):
    if not has_protected_marker:
        return response
    return protected_tool_marker_block_response(response.backend)


def _tool_calls_decision(
    request: ToolLoopResponseDecisionRequest,
    calls: list[dict[str, object]],
) -> ToolLoopResponseDecision:
    native_truncated_write = _native_truncated_write_decision(request, calls)
    if native_truncated_write is not None:
        return native_truncated_write
    long_content_recovery = _long_content_recovery_tool_call_decision(request, calls)
    if long_content_recovery is not None:
        return long_content_recovery
    target_coverage_rework = _target_coverage_rework_tool_call_decision(request, calls)
    if target_coverage_rework is not None:
        return target_coverage_rework
    local_progress_tools = _local_progress_tool_call_decision(request, calls)
    if local_progress_tools is not None:
        return local_progress_tools
    exploration_fuse = exploration_fuse_tool_call_decision(_exploration_request(request, calls))
    if exploration_fuse is not None:
        return _exploration_decision(exploration_fuse)
    clean_response = sanitize_protected_tool_marker_response(
        request.response, native=_native_tool_use_active(request.agent)
    )
    return ToolLoopResponseDecision("run_tools", clean_response, calls, request.counters)


# native 长 content 写被 max_tokens/SSE 截断 → 参数清空 → 同一截断空参 write_file 连续失败
# 这么多次即认定死循环（反复重生成又截断），打硬出口而非无限重试。建议 3：给模型 1~2 次
# 分块纠偏机会后仍截断就停，带证据让 run 出口合同走 closeout/REWORK。
_NATIVE_TRUNCATED_WRITE_LOOP_LIMIT = 3
_TRUNCATED_WRITE_FAILURE_CLASS = "code:TOOL_PARAMETER_REQUIRED"


def _native_truncated_write_decision(
    request: ToolLoopResponseDecisionRequest,
    calls: list[dict[str, object]],
) -> ToolLoopResponseDecision | None:
    """P0-2:native 写长文档被截断成空参时，激活长内容恢复（分块写），连续 N 次即硬 break。

    只在 native 协议 + 本轮响应疑似截断（``response.truncated``）+ 存在缺参的 write_file 调用
    时介入。正常多轮写大文档（未截断、参数完整）一律不进此分支，零误伤；text 协议另有
    write_abort 路径，也不进。
    """
    if not _native_tool_use_active(request.agent):
        return None
    if not bool(getattr(request.response, "truncated", False)):
        return None
    if not _has_truncated_empty_write(calls):
        return None
    prior_failures = _consecutive_truncated_write_failures(request.agent)
    if prior_failures + 1 >= _NATIVE_TRUNCATED_WRITE_LOOP_LIMIT:
        return ToolLoopResponseDecision(
            "break",
            _native_truncated_write_loop_break_response(request.response.backend, prior_failures + 1),
            [],
            request.counters,
        )
    request.params.tool_context.append(_native_truncated_write_recovery_context(calls))
    return ToolLoopResponseDecision("continue", None, [], request.counters)


def _has_truncated_empty_write(calls: list[dict[str, object]]) -> bool:
    for call in calls:
        if _call_tool(call) != "write_file":
            continue
        has_path = bool(str(call.get("path") or "").strip())
        has_content = call.get("content") is not None or call.get("data_base64") is not None
        if not has_path or not has_content:
            return True
    return False


def _consecutive_truncated_write_failures(agent: object) -> int:
    """从 guardrail records 尾部数连续的 write_file + TOOL_PARAMETER_REQUIRED 失败。

    复用既有 ``_tool_call_guardrail_records``（已按 tool_name/args_hash/failure_class 结构化）；
    一旦尾部出现非该类记录（例如一次成功 write_file）即中断计数 → 正常写入会自然清零，
    不会把历史失败累计到无关任务上。
    """
    count = 0
    for record in reversed(tool_guardrail_records(agent)):
        if str(record.get("tool_name") or "") != "write_file":
            break
        if record.get("failed") is not True:
            break
        if str(record.get("failure_class") or "") != _TRUNCATED_WRITE_FAILURE_CLASS:
            break
        count += 1
    return count


def _native_truncated_write_recovery_context(calls: list[dict[str, object]]) -> str:
    payload = next(
        (call for call in calls if _call_tool(call) == "write_file"),
        {"tool": "write_file"},
    )
    base = long_content_recovery_context(
        LongContentRecoveryRequest(
            payload=payload,
            result_tool="write_file",
            result_ok=False,
            output="",
            result_error_code="TOOL_PARAMETER_REQUIRED",
            truncated=True,
        )
    )
    header = (
        "[tool-system]\n"
        "上一轮 write_file 的参数 JSON 在流式生成时被截断（疑似 max_tokens/长度上限），"
        "导致工具收到空参数而无法执行。请把正文拆成更小的块分多次写入，"
        "第一块用 mode=\"overwrite\" 重写目标文件，后续块用 mode=\"append\"。"
    )
    return f"{header}\n{base}" if base else header


def _native_truncated_write_loop_break_response(backend: str, attempts: int) -> ModelResponse:
    return ModelResponse(
        text=(
            "系统已停止本次写入循环：write_file 的参数在流式生成时连续 "
            f"{attempts} 次被截断（max_tokens/长度上限），分块纠偏后仍未成功闭合参数 JSON。\n"
            "请不要再用单次大块 write_file 重试同一目标；改用显著更小的分块写入"
            "（每块正文更短，第一块 overwrite、后续 append），或先 task_progress 记录已写进度再续写。"
        ),
        backend=backend,
        runtime_status="unfinished",
        runtime_reason="NATIVE_TRUNCATED_WRITE_LOOP",
        runtime_source="tool_loop",
    )


def _long_content_recovery_tool_call_decision(
    request: ToolLoopResponseDecisionRequest,
    calls: list[dict[str, object]],
) -> ToolLoopResponseDecision | None:
    state = long_content_recovery_state_from_records(request.params.archive_tool_calls)
    if not state.active:
        return None
    max_inline_chars = _agent_tool_write_inline_max_chars(request.agent)
    for call in calls:
        if long_content_recovery_payload_too_large(call, state, max_inline_chars=max_inline_chars):
            request.params.tool_context.append(
                long_content_recovery_block_context(call, state, max_inline_chars=max_inline_chars)
            )
            return ToolLoopResponseDecision("continue", None, [], request.counters)
    return None


def _agent_tool_write_inline_max_chars(agent: object) -> int | None:
    value = getattr(getattr(agent, "config", None), "tool_write_inline_max_chars", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _target_coverage_rework_tool_call_decision(
    request: ToolLoopResponseDecisionRequest,
    calls: list[dict[str, object]],
) -> ToolLoopResponseDecision | None:
    report = _latest_closeout_report(request.agent, request.params)
    status = report.get("target_coverage_status") if isinstance(report, dict) else {}
    if not isinstance(status, dict) or status.get("should_block") is not True:
        return None
    if not _has_premature_delivery_call(calls, report):
        return None
    if _has_missing_coverage_exploration_call(calls, status):
        return None
    request.params.tool_context.append(_target_coverage_rework_context(status, calls))
    return ToolLoopResponseDecision("continue", None, [], request.counters)


def _latest_closeout_report(agent: object, params: ToolLoopExecuteParams) -> dict[str, object]:
    root = current_run_task_workspace_root(agent, params)
    if root is None:
        root = Path(getattr(agent, "root", ".")).resolve()
    path = root / ".agent_delivery" / "closeout.json"
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _has_premature_delivery_call(calls: list[dict[str, object]], report: dict[str, object]) -> bool:
    final_paths = _final_artifact_paths_from_report(report)
    for call in calls:
        tool = _call_tool(call)
        if tool == "submit_for_acceptance":
            return True
        if tool == "write_file" and _call_path_matches(call, final_paths):
            return True
    return False


def _final_artifact_paths_from_report(report: dict[str, object]) -> set[str]:
    paths: set[str] = set()
    artifacts = report.get("artifacts")
    if not isinstance(artifacts, list):
        return paths
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        path = _normalized_path_text(artifact.get("path"))
        if path:
            paths.add(path)
    return paths


def _call_path_matches(call: dict[str, object], final_paths: set[str]) -> bool:
    if not final_paths:
        return False
    return _normalized_path_text(call.get("path")) in final_paths


def _has_missing_coverage_exploration_call(calls: list[dict[str, object]], status: dict[str, object]) -> bool:
    missing_roots = _missing_source_roots(status)
    if not missing_roots:
        return False
    for call in calls:
        tool = _call_tool(call)
        if tool not in {"list_files", "read_file", "search_text", "find_files"}:
            continue
        path = _normalized_path_text(call.get("path") or call.get("query"))
        if _path_under_any(path, missing_roots):
            return True
    return False


def _missing_source_roots(status: dict[str, object]) -> set[str]:
    roots: set[str] = set()
    for key in ("missing_items", "repair_hints"):
        roots.update(_source_roots_from_items(status.get(key)))
    return roots


def _source_roots_from_items(items: object) -> set[str]:
    if not isinstance(items, list):
        return set()
    return {
        path
        for item in items
        if isinstance(item, dict)
        for field in ("source_ref", "source_path", "target_id")
        if (path := _normalized_path_text(item.get(field)))
    }


def _path_under_any(path: str, roots: set[str]) -> bool:
    if not path:
        return False
    return any(path == root or path.startswith(root.rstrip("/\\") + "/") for root in roots)


def _normalized_path_text(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return str(Path(text).expanduser().resolve(strict=False))
    except OSError:
        return text


def _call_tool(call: dict[str, object]) -> str:
    return str(call.get("tool") or call.get("tool_name") or "").strip()


def _target_coverage_rework_context(status: dict[str, object], calls: list[dict[str, object]]) -> str:
    payload = {
        "blocked_tools": [_call_tool(call) for call in calls if _call_tool(call)],
        "missing_count": int(status.get("missing_count") or 0),
        "repair_hints": list(status.get("repair_hints") or [])[:8],
        "required_next_tools": ["list_files", "read_file"],
    }
    return (
        "[tool-system target-coverage-rework-guard]\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True)
        + "\n当前 closeout 仍缺 required source coverage。先按 repair_hints 对缺失源码目录执行 list_files/read_file；"
        "不要读取旧报告、写最终交付物或 submit_for_acceptance。补齐覆盖后，再更新最终交付物并提交验收。"
    )


def _no_tool_calls_decision(request: _NoToolCallsRequest) -> ToolLoopResponseDecision:
    if _is_runtime_status_response(request.response):
        return ToolLoopResponseDecision("break", request.response, [], request.counters)
    local_progress_decision = _local_progress_no_tool_call_decision(request)
    if local_progress_decision is not None:
        return local_progress_decision
    unresolved_issue_decision = unresolved_runtime_issue_no_tool_call_decision(
        _unresolved_runtime_issue_request(request)
    )
    if unresolved_issue_decision is not None:
        return _unresolved_runtime_issue_decision(unresolved_issue_decision)
    exploration_fuse_decision = exploration_fuse_no_tool_call_decision(_exploration_request(request, []))
    if exploration_fuse_decision is not None:
        return _exploration_decision(exploration_fuse_decision)
    if not request.has_protected_marker:
        return ToolLoopResponseDecision("break", request.response, [], request.counters)
    if request.counters.protected_marker_repairs < 1:
        return ToolLoopResponseDecision("continue", None, [], _inc_protected_marker(request.counters))
    final = protected_tool_marker_block_response(request.response.backend)
    return ToolLoopResponseDecision("break", final, [], request.counters)


def _is_runtime_status_response(response: object) -> bool:
    status = str(getattr(response, "runtime_status", "") or "").strip()
    if status and status != "ok":
        return True
    for field in ("runtime_reason", "runtime_source"):
        if str(getattr(response, field, "") or "").strip():
            return True
    return False


def _local_progress_no_tool_call_decision(
    request: _NoToolCallsRequest,
) -> ToolLoopResponseDecision | None:
    if not has_required_local_progress_guard(request.agent, request.params, []):
        return None
    repair_context = local_progress_guard_context(
        request.agent,
        request.counters.local_progress_redirects,
        request.params,
    )
    if repair_context:
        request.params.tool_context.append(repair_context)
        return None
    return None


def _local_progress_tool_call_decision(
    request: ToolLoopResponseDecisionRequest,
    calls: list[dict[str, object]],
) -> ToolLoopResponseDecision | None:
    if not has_required_local_progress_guard(request.agent, request.params, calls):
        return None
    repair_context = local_progress_guard_context(
        request.agent,
        request.counters.local_progress_redirects,
        request.params,
    )
    if repair_context:
        request.params.tool_context.append(repair_context)
    return None


def exploration_fuse_tool_call_decision(
    request: ExplorationFuseDecisionRequest,
) -> ExplorationFuseDecision | None:
    if not has_required_exploration_fuse(request.agent, request.calls, request.params):
        return None
    context = exploration_fuse_context(request.agent, request.counters.exploration_fuse_redirects, request.params)
    if context:
        request.params.tool_context.append(context)
        return None
    return None


def exploration_fuse_no_tool_call_decision(
    request: ExplorationFuseDecisionRequest,
) -> ExplorationFuseDecision | None:
    if not has_pending_exploration_fuse(request.agent):
        return None
    context = exploration_fuse_context(request.agent, request.counters.exploration_fuse_redirects, request.params)
    if context:
        request.params.tool_context.append(context)
        return ExplorationFuseDecision("continue", None, [], _inc_exploration_fuse(request.counters))
    return None


def unresolved_runtime_issue_no_tool_call_decision(
    request: UnresolvedRuntimeIssueDecisionRequest,
) -> UnresolvedRuntimeIssueDecision | None:
    if not has_unresolved_runtime_issues(request.params):
        return None
    if request.counters.unresolved_runtime_issue_redirects >= 1:
        return UnresolvedRuntimeIssueDecision("break", request.response, [], request.counters)
    repair_context = unresolved_runtime_issue_context(
        request.params,
        request.counters.unresolved_runtime_issue_redirects,
    )
    if repair_context:
        request.params.tool_context.append(repair_context)
        return UnresolvedRuntimeIssueDecision(
            "continue",
            None,
            [],
            _inc_unresolved_runtime_issue(request.counters),
        )
    return UnresolvedRuntimeIssueDecision("break", request.response, [], request.counters)


def _exploration_decision(decision: ExplorationFuseDecision) -> ToolLoopResponseDecision:
    return ToolLoopResponseDecision(decision.action, decision.response, decision.calls, decision.counters)


def _unresolved_runtime_issue_decision(decision: UnresolvedRuntimeIssueDecision) -> ToolLoopResponseDecision:
    return ToolLoopResponseDecision(decision.action, decision.response, decision.calls, decision.counters)


def _unresolved_runtime_issue_request(
    request: _NoToolCallsRequest,
) -> UnresolvedRuntimeIssueDecisionRequest:
    return UnresolvedRuntimeIssueDecisionRequest(request.agent, request.params, request.response, request.counters)


def _exploration_request(
    request: ToolLoopResponseDecisionRequest | _NoToolCallsRequest,
    calls: list[dict[str, object]],
) -> ExplorationFuseDecisionRequest:
    return ExplorationFuseDecisionRequest(request.agent, request.params, request.response, request.counters, calls)
