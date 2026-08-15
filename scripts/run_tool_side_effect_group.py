#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_env_loader import ensure_model_key

"""第③组：副作用工具隔离 harness（复核 seq 342 前置条件）。

矩阵(完全隔离, 无真实外部副作用——副作用只落在 run_root 内隔离 workspace):
  G3-001 approval ask   (真模型, approval=always, 空 approved_actions)
  G3-002 approval grant (确定性注入: 首拦取 key -> 批准 -> 同 key 重发 -> 放行)
  G3-003 binding mismatch(确定性注入: 同 tool+key 错 args_hash -> deny)
  G3-004 cancel          (真模型, run_command sleep 60 + 8s 后 set_interrupt)
  G3-005 timeout         (真模型, timeout=2 跑 sleep 60 -> TOOL_TIMEOUT)
  G3-006 idempotent replay(确定性注入: 同 operation_id+key 双发 -> 单份效果)
  G3-007 concurrent write(确定性注入: 双线程同文件写 -> 无交错无悬挂异常)

判据只认结构化机器事实(error_code/status/handler_executed/ok/operation 终态),
不看模型话术; 每个场景完整记录 operation/handler/effect/approval/unknown 事实。
"""

import argparse
import json
import os
import platform
import shlex
import subprocess
import threading
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

_CALL_FIELDS = (
    "call_id",
    "tool",
    "parameters",
    "source_protocol",
    "schema_hash",
    "turn_id",
    "attempt_id",
    "required_action_id",
    "operation_id",
    "idempotency_key",
    "status",
    "ok",
    "error_code",
    "error_category",
    "retryable",
    "handler_executed",
    "failure_stage",
    "duration_ms",
    "effect_outcome",
    "effect_source_ref",
    "tool_operation_status",
    "tool_operation_attempt_count",
    "tool_operation_replayed",
    "result_ref",
    "artifact_ref",
    "output_chars",
    "output_bytes",
    "output_sha256",
    "output_preview",
    "tool_result_refs",
    "tool_result_envelope",
)

_APPROVAL_CODE = "APPROVAL_REQUIRED"
_MISMATCH_CODE = "APPROVAL_BINDING_MISMATCH"
_CANCEL_CODES = {"CANCELLED", "cancelled"}
_TIMEOUT_CODES = {"TOOL_TIMEOUT", "timed_out"}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run my-agent side-effect tool isolation harness (G3 group)."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=REPO_ROOT / "agent_py_agent" / "config" / "agent_config.yaml",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=REPO_ROOT / "validation" / "real_runs",
    )
    parser.add_argument("--no-stream", action="store_true")
    parser.add_argument(
        "--skip-real",
        action="store_true",
        help="只跑确定性注入用例(本地冒烟), 不调用真实模型、不要求 API key。",
    )
    return parser.parse_args()


def _make_agent(args: argparse.Namespace, run_root: Path, case_id: str):
    """每用例独立 agent: 独立 workspace + 独立 my-agent-home(隔离副作用)。"""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import load_config

    workspace = run_root / f"ws-{case_id}"
    home = run_root / f"home-{case_id}"
    workspace.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    base = load_config(args.config)
    config = replace(
        base,
        workspace_root=str(workspace),
        my_agent_home=str(home),
        stream_enabled=not args.no_stream,
        tool_protocol="native",
        enable_tools=True,
        enable_subagents=False,
        auto_save_memory=False,
        home_context_enabled=False,
        memory_curator_enabled=False,
        run_task_workspace_enabled=False,
        max_tokens=min(4096, max(1024, int(base.max_tokens or 0))),
    )
    agent = SimpleAgent(config, workspace, workspace_roots=[workspace])
    return agent, workspace


def _set_approval_always(agent: object) -> None:
    """装配级策略覆盖: run_command 一律先过人工审批(harness 控制面, 不动生产代码)。"""
    from agent_py_agent.agent.tooling.models import ApprovalPolicy

    tool = agent.tools.tools["run_command"]
    tool.runtime_policy = replace(tool.runtime_policy, approval_policy=ApprovalPolicy("always"))


def _snapshot(agent: object, run_id: str):
    return agent.tools.runtime_snapshot(allowed_tools=["run_command"], run_id=run_id)


def _inject_call(
    agent: object,
    *,
    run_id: str,
    call_id: str,
    command: str,
    operation_id: str = "",
    idempotency_key: str = "",
    timeout: int | None = None,
):
    from agent_py_agent.agent.tooling.runtime_contracts import ToolCall

    arguments: dict[str, object] = {"command": command}
    if timeout is not None:
        arguments["timeout"] = timeout
    tool = agent.tools.tools["run_command"]
    return ToolCall(
        call_id=call_id,
        tool_name="run_command",
        arguments=arguments,
        source_protocol="native",
        schema_hash=tool.model_spec.schema_hash,
        run_id=run_id,
        turn_id=f"{run_id}:turn-1",
        attempt_id=f"{run_id}:attempt-1",
        operation_id=operation_id,
        idempotency_key=idempotency_key,
    )


def _execute_injected(
    agent: object,
    workspace: Path,
    call,
    *,
    approved_actions: list[dict[str, object]] | None = None,
):
    from agent_py_agent.agent.tooling.executor import ToolExecutor, ToolExecutorRequest

    boundary: dict[str, object] = {"allowed_write_roots": [str(workspace)]}
    if approved_actions:
        boundary["approved_actions"] = approved_actions
    execution = ToolExecutor().execute(
        ToolExecutorRequest(
            call=call,
            runtime_snapshot=_snapshot(agent, call.run_id),
            workspace_root=workspace,
            workspace_roots=(workspace,),
            write_boundary=boundary,
            operation_store=agent.local_store,
            operation_store_required=True,
        )
    )
    return execution


def _execution_evidence(execution) -> dict[str, object]:
    decision = execution.decision
    result = execution.result
    call = execution.call
    evidence = {
        "decision_status": str(getattr(decision, "status", "") or ""),
        "decision_allowed": bool(getattr(decision, "allowed", False)),
        "decision_codes": list(getattr(decision, "reason_codes", ()) or ()),
        "approval_request": dict(getattr(decision, "approval_request", None) or {}),
        "result_ok": bool(getattr(result, "ok", False)),
        "result_handler_executed": bool(getattr(result, "handler_executed", False)),
        "result_error_code": str(getattr(result, "error_code", "") or ""),
        "result_status": str(getattr(result, "status", "") or ""),
        "result_effect_outcome": str(getattr(result, "effect_outcome", "") or ""),
        "result_retryable": bool(getattr(result, "retryable", False)),
        "states": list(getattr(execution, "states", ()) or ()),
        "call": {
            "call_id": call.call_id,
            "tool_name": call.tool_name,
            "arguments": dict(call.arguments),
            "run_id": call.run_id,
            "operation_id": call.operation_id,
            "idempotency_key": call.idempotency_key,
            "args_hash": call.args_hash,
        },
    }
    metadata = dict(getattr(result, "metadata", None) or {})
    if metadata:
        evidence["result_metadata_keys"] = sorted(metadata.keys())
    return evidence


def _case_metrics(result: object) -> dict[str, object]:
    records = [
        item
        for item in list(getattr(result, "archive_tool_calls", None) or [])
        if isinstance(item, dict)
    ]
    operations = [item for item in records if str(item.get("operation_id") or "")]
    return {
        "canonical_tool_call_count": len(records),
        "canonical_tool_result_count": len(records),
        "handler_execution_count": sum(item.get("handler_executed") is True for item in records),
        "operation_count": len(operations),
        "tool_calls_and_results": [
            {key: record[key] for key in _CALL_FIELDS if key in record} for record in records
        ],
    }


def _model_call_evidence(agent: object, run_id: str) -> list[dict[str, object]]:
    ledger = getattr(agent, "_model_call_ledger", None)
    records = ledger.records() if ledger is not None else ()
    evidence: list[dict[str, object]] = []
    for record in records:
        if str(getattr(record, "run_id", "") or "") != run_id:
            continue
        payload = record.to_dict()
        evidence.append(
            {
                key: payload.get(key)
                for key in (
                    "call_id",
                    "backend",
                    "model",
                    "request_id",
                    "run_id",
                    "status",
                    "input_tokens",
                    "output_tokens",
                    "provider_attempt_count",
                    "provider_attempts",
                    "events",
                    "metadata",
                )
            }
        )
    return evidence


def _run_model_case(
    agent: object,
    *,
    case_id: str,
    user_input: str,
    interrupt_after: float | None = None,
    interrupt_when_process: str = "",
) -> dict[str, object]:
    run_id = f"side-effect-{case_id.lower()}-{uuid.uuid4().hex[:10]}"
    outcome: dict[str, object] = {}
    if interrupt_after is None and not interrupt_when_process:
        try:
            outcome["result"] = agent.run(
                user_input,
                request_id=run_id,
                run_id=run_id,
                source="tool_side_effect_group",
                allowed_tools=["run_command"],
                save=False,
            )
        except Exception as exc:  # noqa: BLE001 - evidence must survive one failed case
            outcome["error"] = f"{type(exc).__name__}: {exc}"
    else:
        from agent_py_agent.agent.concurrency.interrupt import set_interrupt

        def target() -> None:
            try:
                outcome["result"] = agent.run(
                    user_input,
                    request_id=run_id,
                    run_id=run_id,
                    source="tool_side_effect_group",
                    allowed_tools=["run_command"],
                    save=False,
                )
            except Exception as exc:  # noqa: BLE001
                outcome["error"] = f"{type(exc).__name__}: {exc}"

        thread = threading.Thread(target=target, name=f"g3-{case_id}", daemon=True)
        thread.start()
        if interrupt_when_process:
            # interrupt 必须打在命令运行中(而不是模型请求进行中)才有 CANCELLED
            # 记录:轮询 pgrep 探测目标命令进程,命中后延迟 2s 再 set_interrupt,
            # 保证 shell 进程被杀 -> CommandInterruptedError -> CANCELLED。
            _interrupt_when_process_running(thread, interrupt_when_process)
        else:
            time.sleep(interrupt_after)
            current_tid = thread.ident
            if current_tid is not None:
                set_interrupt(True, current_tid)
        thread.join(timeout=300)
        clear_tid = thread.ident
        if clear_tid is not None:
            set_interrupt(False, clear_tid)
        outcome["interrupt_after_seconds"] = interrupt_after or 0.0
        outcome["interrupt_when_process"] = interrupt_when_process
        outcome["thread_alive_after_join"] = thread.is_alive()
        if thread.is_alive():
            outcome["error"] = str(outcome.get("error") or "run did not finish within join timeout")

    result = outcome.get("result")
    case: dict[str, object] = {
        "test_id": case_id,
        "kind": "model",
        "original_input": user_input,
        "run_id": run_id,
        "backend": str(getattr(result, "backend", "") or ""),
        "runtime_status": str(getattr(result, "runtime_status", "") or ""),
        "runtime_reason": str(getattr(result, "runtime_reason", "") or ""),
        "runtime_source": str(getattr(result, "runtime_source", "") or ""),
        "final_answer": str(getattr(result, "response", "") or ""),
        "metrics": _case_metrics(result),
        "model_calls": _model_call_evidence(agent, run_id),
        "operation_verification": dict(getattr(result, "operation_verification", None) or {}),
    }
    if outcome.get("error"):
        case["run_error"] = outcome["error"]
    if interrupt_after is not None or interrupt_when_process:
        case["interrupt_after_seconds"] = outcome.get("interrupt_after_seconds", 0.0)
        case["interrupt_when_process"] = interrupt_when_process
        case["thread_alive_after_join"] = outcome.get("thread_alive_after_join", False)
    return case


def _process_running(marker: str) -> bool:
    import re
    import subprocess

    # pgrep -f 把模式当扩展正则: `(60)` 会被当作分组字符, 永远匹配不上带括号的
    # 命令行(实测 time.sleep(60) 探测 rc=1)。re.escape 转义成字面量匹配。
    try:
        proc = subprocess.run(
            ["pgrep", "-f", re.escape(marker)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return proc.returncode == 0
    except Exception:  # noqa: BLE001 - probe must never kill the case
        return False


def _interrupt_when_process_running(
    thread: threading.Thread,
    marker: str,
    *,
    timeout_seconds: float = 300.0,
) -> None:
    from agent_py_agent.agent.concurrency.interrupt import set_interrupt

    # thread.start() 后 ident 可能短暂为 None(线程尚未开始运行),此时 set_interrupt
    # 会落到调用者线程上,目标线程永远收不到中断旗。探测循环内每次实时取值,
    # 命中时线程必然在跑命令,ident 必然已赋值。
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not thread.is_alive():
            return
        if _process_running(marker):
            time.sleep(2.0)
            current_tid = thread.ident
            if current_tid is not None:
                set_interrupt(True, current_tid)
            return
        time.sleep(1.0)
    current_tid = thread.ident
    if current_tid is not None:
        set_interrupt(True, current_tid)


def _records_of(case: dict[str, object]) -> list[dict[str, object]]:
    return [
        item
        for item in (case.get("metrics") or {}).get("tool_calls_and_results") or []
        if isinstance(item, dict)
    ]


def _findings_g3_001(case: dict[str, object]) -> list[str]:
    """approval ask: 全部调用被拦, handler 全 False, 无 ok=True, 有最终回答。"""
    findings: list[str] = []
    records = _records_of(case)
    approved = [
        item for item in records if item.get("error_code") == _APPROVAL_CODE
    ]
    if not approved:
        findings.append(f"no_{_APPROVAL_CODE}_record")
    if any(item.get("handler_executed") is True for item in records):
        findings.append("unexpected_handler_execution")
    if any(item.get("ok") is True for item in records):
        findings.append("unexpected_ok_result")
    if not case.get("final_answer"):
        findings.append("final_answer_empty")
    if case.get("run_error"):
        findings.append(f"run_error:{case['run_error']}")
    return findings


def _envelope_reports(item: dict[str, object], codes: set[str]) -> bool:
    """record 顶层 error_code 被 reconcile 包装时,取消/超时信号藏在 envelope 里。"""
    envelope = item.get("tool_result_envelope") or {}
    reported = envelope.get("reported_tool_result") or {}
    for key in ("reported_error_code", "error_code", "status"):
        value = str(reported.get(key) or "")
        if value in codes:
            return True
    return False


def _findings_g3_004(case: dict[str, object]) -> list[str]:
    """cancel: 存在 CANCELLED/cancelled 记录(含 envelope 内), 且命令从未 ok。"""
    findings: list[str] = []
    records = _records_of(case)
    cancelled = [
        item
        for item in records
        if item.get("error_code") in _CANCEL_CODES
        or item.get("status") in _CANCEL_CODES
        or _envelope_reports(item, _CANCEL_CODES)
    ]
    if not cancelled:
        findings.append("no_cancelled_record")
    ok_sleep = [
        item
        for item in records
        if item.get("ok") is True and "time.sleep(60)" in str((item.get("parameters") or {}).get("command") or "")
    ]
    if ok_sleep:
        findings.append("cancelled_command_ok")
    if case.get("run_error"):
        findings.append(f"run_error:{case['run_error']}")
    return findings


def _findings_g3_005(case: dict[str, object]) -> list[str]:
    """timeout: 超时命令被结构化处理, 无成功重试, 如实收口。

    真实路径: shell 层 TOOL_TIMEOUT(进程被 kill) -> reconcile 对被杀命令的副作用
    不可确认 -> TOOL_OPERATION_OUTCOME_UNKNOWN -> L2 单次 unknown 收口(unfinished)。
    顶层记录 error_code 可能落在两者之一, 也可能藏在 envelope 内, 一并接受。
    """
    findings: list[str] = []
    records = _records_of(case)
    timeout_unknown = {
        *_TIMEOUT_CODES,
        "TOOL_OPERATION_OUTCOME_UNKNOWN",
        "unknown",
    }
    timed_out = [
        item
        for item in records
        if item.get("error_code") in timeout_unknown
        or item.get("status") in timeout_unknown
        or _envelope_reports(item, timeout_unknown)
    ]
    if not timed_out:
        findings.append("no_timeout_record")
    ok_sleep = [
        item
        for item in records
        if item.get("ok") is True and "time.sleep(30)" in str((item.get("parameters") or {}).get("command") or "")
    ]
    if ok_sleep:
        findings.append("timed_out_command_ok_retry")
    if not case.get("final_answer"):
        findings.append("final_answer_empty")
    if case.get("run_error"):
        findings.append(f"run_error:{case['run_error']}")
    return findings


def _case_done(case: dict[str, object]) -> None:
    case["passed"] = not case["findings"]
    case["exit_code"] = 0 if case["passed"] else 1


def _run_g3_001(args: argparse.Namespace, run_root: Path) -> dict[str, object]:
    """真模型 approval=always + 空 approved_actions: 每调用均 APPROVAL_REQUIRED。"""
    agent, workspace = _make_agent(args, run_root, "G3-001")
    _set_approval_always(agent)
    case = _run_model_case(
        agent,
        case_id="G3-001",
        user_input="请在隔离测试环境里执行命令 `pwd`，并把输出告诉我。",
    )
    case["kind"] = "model"
    case["policy"] = {"approval_mode": "always", "approved_actions": []}
    case["findings"] = _findings_g3_001(case)
    _case_done(case)
    return case


def _run_g3_002(args: argparse.Namespace, run_root: Path) -> dict[str, object]:
    """approval grant: 首拦取 key -> 批准 -> 同 key 重发 -> 放行且 handler 真执行。"""
    agent, workspace = _make_agent(args, run_root, "G3-002")
    _set_approval_always(agent)
    payload = "g3-002-payload"
    target = workspace / "g3-002.txt"
    call = _inject_call(
        agent,
        run_id="g3-002-inject-run",
        call_id="g3-002-call",
        command=f"printf '%s\\n' '{payload}' >> '{target}'",
    )
    first = _execute_injected(agent, workspace, call)
    findings: list[str] = []
    if first.decision.status != "ask":
        findings.append(f"first_not_ask:{first.decision.status}")
    if first.result.error_code != _APPROVAL_CODE:
        findings.append(f"first_error_code:{first.result.error_code}")
    if first.result.handler_executed is True:
        findings.append("first_handler_executed")
    if first.result.ok is True:
        findings.append("first_ok")
    approval_request = dict(first.decision.approval_request or {})
    binding = {
        **approval_request,
        "approval_id": f"approval-{uuid.uuid4().hex[:8]}",
        "status": "APPROVED",
    }
    second = _execute_injected(
        agent, workspace, call, approved_actions=[binding]
    )
    if second.decision.allowed is not True:
        findings.append(f"second_not_allowed:{second.decision.status}")
    if second.result.ok is not True:
        findings.append(f"second_not_ok:{second.result.error_code}")
    if second.result.handler_executed is not True:
        findings.append("second_handler_not_executed")
    file_lines = target.read_text(encoding="utf-8").strip().splitlines() if target.exists() else []
    if file_lines != [payload]:
        findings.append(f"file_content_mismatch:{file_lines!r}")
    case: dict[str, object] = {
        "test_id": "G3-002",
        "kind": "injected",
        "original_input": f"run_command {call.arguments['command']} (确定性注入)",
        "run_id": call.run_id,
        "policy": {"approval_mode": "always"},
        "executions": [
            _execution_evidence(first),
            _execution_evidence(second),
        ],
        "approved_action": {
            key: value for key, value in binding.items() if key != "args_hash"
        },
        "file_after": file_lines,
        "findings": findings,
    }
    _case_done(case)
    return case


def _run_g3_003(args: argparse.Namespace, run_root: Path) -> dict[str, object]:
    """binding mismatch: 同 tool+key 但 args_hash 错 -> APPROVAL_BINDING_MISMATCH deny。"""
    agent, workspace = _make_agent(args, run_root, "G3-003")
    _set_approval_always(agent)
    payload = "g3-003-payload"
    target = workspace / "g3-003.txt"
    call = _inject_call(
        agent,
        run_id="g3-003-inject-run",
        call_id="g3-003-call",
        command=f"printf '%s\\n' '{payload}' >> '{target}'",
    )
    first = _execute_injected(agent, workspace, call)
    findings: list[str] = []
    if first.decision.status != "ask":
        findings.append(f"first_not_ask:{first.decision.status}")
    approval_request = dict(first.decision.approval_request or {})
    binding = {
        **approval_request,
        "approval_id": f"approval-{uuid.uuid4().hex[:8]}",
        "status": "APPROVED",
        "args_hash": "sha256:deliberately-wrong",
    }
    second = _execute_injected(
        agent, workspace, call, approved_actions=[binding]
    )
    if second.result.error_code != _MISMATCH_CODE:
        findings.append(f"second_error_code:{second.result.error_code}")
    if second.result.handler_executed is True:
        findings.append("mismatch_handler_executed")
    if second.result.ok is True:
        findings.append("mismatch_ok")
    if target.exists():
        findings.append("mismatch_wrote_file")
    case: dict[str, object] = {
        "test_id": "G3-003",
        "kind": "injected",
        "original_input": f"run_command {call.arguments['command']} (确定性注入)",
        "run_id": call.run_id,
        "policy": {"approval_mode": "always"},
        "executions": [
            _execution_evidence(first),
            _execution_evidence(second),
        ],
        "binding_args_hash_override": "sha256:deliberately-wrong",
        "file_after_exists": target.exists(),
        "findings": findings,
    }
    _case_done(case)
    return case


def _run_g3_004(args: argparse.Namespace, run_root: Path) -> dict[str, object]:
    """cancel: 真模型跑 python3 time.sleep(60), 探测到进程后 set_interrupt 杀进程。"""
    agent, workspace = _make_agent(args, run_root, "G3-004")
    case = _run_model_case(
        agent,
        case_id="G3-004",
        user_input=(
            "请立刻调用 run_command 执行 `python3 -c \"import time; time.sleep(60)\"` 并等待它完成。"
            "这是一个模拟耗时任务，请只运行这一条命令，不要做其他事情。"
        ),
        interrupt_when_process="time.sleep(60)",
    )
    case["kind"] = "model"
    case["findings"] = _findings_g3_004(case)
    _case_done(case)
    return case


def _run_g3_005(args: argparse.Namespace, run_root: Path) -> dict[str, object]:
    """timeout: 真模型带 timeout=2 跑 python3 time.sleep(30) -> 超时, 无成功重试。

    注:sleep 命令会被 run_command 的 USE_WAIT_FOR_DELAY 门在执行前拦掉(产品有意
    引导用 wait 工具),所以用 python3 -c 模拟耗时命令,绕过延迟门测真实超时路径。
    """
    agent, workspace = _make_agent(args, run_root, "G3-005")
    case = _run_model_case(
        agent,
        case_id="G3-005",
        user_input=(
            "请用 run_command 执行命令 `python3 -c \"import time; time.sleep(30)\"`，"
            "并把 timeout 参数显式设为 2（秒）。执行完把结果告诉我。"
        ),
    )
    case["kind"] = "model"
    case["findings"] = _findings_g3_005(case)
    _case_done(case)
    return case


def _run_g3_006(args: argparse.Namespace, run_root: Path) -> dict[str, object]:
    """idempotent replay: 同 operation_id+key 双发, handler 只跑一次, 文件单份。"""
    agent, workspace = _make_agent(args, run_root, "G3-006")
    payload = "g3-006-payload"
    target = workspace / "g3-006.txt"
    call = _inject_call(
        agent,
        run_id="g3-006-inject-run",
        call_id="g3-006-call",
        command=f"printf '%s\\n' '{payload}' >> '{target}'",
        operation_id="g3-006-op",
        idempotency_key="g3-006-key",
    )
    first = _execute_injected(agent, workspace, call)
    second = _execute_injected(agent, workspace, call)
    findings: list[str] = []
    if first.result.ok is not True or first.result.handler_executed is not True:
        findings.append(f"first_not_executed:{first.result.error_code}")
    if second.result.ok is not True:
        findings.append(f"second_failed:{second.result.error_code}")
    file_lines = target.read_text(encoding="utf-8").strip().splitlines() if target.exists() else []
    if file_lines != [payload]:
        findings.append(f"file_lines_not_single:{file_lines!r}")
    case: dict[str, object] = {
        "test_id": "G3-006",
        "kind": "injected",
        "original_input": f"run_command {call.arguments['command']} 同 key 双发 (确定性注入)",
        "run_id": call.run_id,
        "policy": {"approval_mode": "dangerous", "idempotency_scope": "operation"},
        "executions": [
            _execution_evidence(first),
            _execution_evidence(second),
        ],
        "file_after": file_lines,
        "findings": findings,
    }
    _case_done(case)
    return case


def _run_g3_007(args: argparse.Namespace, run_root: Path) -> dict[str, object]:
    """concurrent write: 双线程独立 operation 同文件写, 终态无交错无悬挂异常。"""
    agent, workspace = _make_agent(args, run_root, "G3-007")
    target = workspace / "g3-007.txt"
    call_a = _inject_call(
        agent,
        run_id="g3-007-run-a",
        call_id="g3-007-call-a",
        command=f"python3 -c \"open('{target}','w').write('A'*100000)\"",
        operation_id="g3-007-op-a",
        idempotency_key="g3-007-key-a",
    )
    call_b = _inject_call(
        agent,
        run_id="g3-007-run-b",
        call_id="g3-007-call-b",
        command=f"python3 -c \"open('{target}','w').write('B'*100000)\"",
        operation_id="g3-007-op-b",
        idempotency_key="g3-007-key-b",
    )
    executions: dict[str, object] = {}
    errors: list[str] = []

    def run_one(name: str, call) -> None:
        try:
            executions[name] = _execute_injected(agent, workspace, call)
        except Exception as exc:  # noqa: BLE001 - record, never hang
            errors.append(f"{name}:{type(exc).__name__}:{exc}")

    threads = [
        threading.Thread(target=run_one, args=("a", call_a), name="g3-007-a", daemon=True),
        threading.Thread(target=run_one, args=("b", call_b), name="g3-007-b", daemon=True),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    findings: list[str] = list(errors)
    content = target.read_text(encoding="utf-8") if target.exists() else ""
    expected_a, expected_b = "A" * 100000, "B" * 100000
    if content not in {expected_a, expected_b}:
        findings.append(
            f"final_content_not_single_author:len={len(content)}"
            f",a_ratio={content.count('A') / max(len(content), 1):.3f}"
        )
    for name in ("a", "b"):
        execution = executions.get(name)
        if execution is None:
            findings.append(f"execution_{name}_missing")
            continue
        evidence = _execution_evidence(execution)
        if evidence["result_ok"] is not True:
            findings.append(f"execution_{name}_not_ok:{evidence['result_error_code']}")
    case: dict[str, object] = {
        "test_id": "G3-007",
        "kind": "injected",
        "original_input": "双线程同文件写入 (确定性注入)",
        "run_id": f"{call_a.run_id}|{call_b.run_id}",
        "policy": {"approval_mode": "dangerous", "idempotency_scope": "operation"},
        "executions": {
            name: _execution_evidence(execution)
            for name, execution in sorted(executions.items())
        },
        "final_file_len": len(content),
        "final_file_is_a": content == expected_a,
        "final_file_is_b": content == expected_b,
        "errors": errors,
        "findings": findings,
    }
    _case_done(case)
    return case


def _git_head() -> str:
    pinned = os.environ.get("MY_AGENT_GIT_HEAD", "").strip()
    if pinned:
        return pinned
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


def main() -> int:
    args = _parse_args()
    if not args.skip_real and not ensure_model_key():
        print("AGENT_API_KEY is required; no fake fallback is allowed.", file=sys.stderr)
        return 2
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_root = args.output_root.expanduser().resolve() / f"tool-g3-{timestamp}"
    run_root.mkdir(parents=True, exist_ok=False)
    exact_command = " ".join(shlex.quote(item) for item in [sys.executable, *sys.argv])

    runners = [
        _run_g3_001,
        _run_g3_002,
        _run_g3_003,
        _run_g3_004,
        _run_g3_005,
        _run_g3_006,
        _run_g3_007,
    ]
    if args.skip_real:
        runners = [_run_g3_002, _run_g3_003, _run_g3_006, _run_g3_007]

    cases: list[dict[str, object]] = []
    for runner in runners:
        case_id = runner.__name__.replace("_run_g3_", "G3-").upper()
        try:
            case = runner(args, run_root)
        except Exception as exc:  # noqa: BLE001 - evidence must survive one failed case
            case = {
                "test_id": case_id,
                "kind": "injected",
                "passed": False,
                "exit_code": 1,
                "findings": [f"{type(exc).__name__}: {exc}"],
            }
        cases.append(case)
        print(f"{case['test_id']}: {'PASS' if case.get('passed') else 'FAIL'}", flush=True)

    report = {
        "schema_version": "tool-side-effect-group.v1",
        "created_at": datetime.now(UTC).isoformat(),
        "git_head": _git_head(),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "run_root": str(run_root),
            "config": str(args.config.expanduser().resolve()),
            "skip_real": args.skip_real,
            "api_key_present": not args.skip_real,
            "api_key_recorded": False,
        },
        "exact_test_command": exact_command,
        "cases": cases,
        "passed": all(case.get("passed") is True for case in cases),
    }
    report_path = run_root / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"report={report_path}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
