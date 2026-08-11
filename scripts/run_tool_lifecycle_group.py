#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_env_loader import ensure_model_key

"""第④组：工具生命周期隔离 harness（复核 seq 345 前置条件）。

矩阵(完全隔离, 无真实外部副作用——副作用只落在 run_root 内隔离 workspace):
  G4-001 主代理真实任务完整生命周期 (真模型: 建小项目+跑测试, runtime ok+产物正确)
  G4-002 子代理 closeout            (注入 create_subagents + dispatch 驱动, 子代理真模型跑)
  G4-003 compact 后继续             (真模型: 大上下文触发压缩 -> compact 续跑完成)
  G4-004 resume                    (真模型: 首轮任务 save, 次轮 recovery 续跑收口)
  G4-005 跨 owner 隔离              (注入: owner B 的写操作被拒, A 数据不被污染)
  G4-006 失败恢复                  (注入 COMMAND_FAILED + 真模型如实收口, 无死循环无假 DONE)
  G4-007 USE_WAIT_FOR_DELAY 门      (注入: sleep 头命令 handler=0/effect=not_started)
  G4-008 InterruptedError 冒泡     (真模型: 同步 run 中断 -> 结构化 cancelled 事实, 不重试不复活)

判据只认结构化机器事实(error_code/status/handler_executed/ok/operation 终态/runtime
收口), 不看模型话术; 每用例完整记录 operation/handler/effect/approval/unknown 事实。

Provenance: checkout_head 与 harness_revision 分开记录, MY_AGENT_GIT_HEAD 只作为
显式 override 字段单独列出(复核 seq 345 要求——不能被 env 覆盖充当独立 provenance)。
"""

import argparse
import json
import os
import platform
import re
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

_DELAY_CODE = "USE_WAIT_FOR_DELAY"
_COMMAND_FAILED = "COMMAND_FAILED"
_BLOCKED = "PATH_OWNER_SCOPE_BLOCKED"
_CANCEL_CODES = {"CANCELLED", "cancelled"}
_OK_STATUSES = {"ok", "succeeded"}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run my-agent tool lifecycle isolation harness (G4 group)."
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


def _make_agent(
    args: argparse.Namespace,
    run_root: Path,
    case_id: str,
    *,
    enable_subagents: bool = False,
) -> tuple[Any, Path]:
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
        enable_subagents=enable_subagents,
        auto_save_memory=False,
        home_context_enabled=False,
        memory_curator_enabled=False,
        run_task_workspace_enabled=False,
        max_tokens=min(4096, max(1024, int(base.max_tokens or 0))),
    )
    agent = SimpleAgent(config, workspace, workspace_roots=[workspace])
    return agent, workspace


def _snapshot(agent: Any, run_id: str):
    return agent.tools.runtime_snapshot(allowed_tools=["run_command"], run_id=run_id)


def _inject_call(
    agent: Any,
    *,
    run_id: str,
    call_id: str,
    command: str,
    operation_id: str = "",
    idempotency_key: str = "",
    timeout: int | None = None,
) -> Any:
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
    agent: Any,
    workspace: Path,
    call,
    *,
    approved_actions: list[dict[str, object]] | None = None,
) -> Any:
    from agent_py_agent.agent.tooling.executor import ToolExecutor, ToolExecutorRequest

    boundary: dict[str, object] = {"allowed_write_roots": [str(workspace)]}
    if approved_actions:
        boundary["approved_actions"] = approved_actions
    return ToolExecutor().execute(
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


def _case_done(case: dict[str, object]) -> None:
    case["passed"] = not case["findings"]
    case["exit_code"] = 0 if case["passed"] else 1


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


def _model_call_evidence(agent: Any, run_id: str) -> list[dict[str, object]]:
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
    agent: Any,
    *,
    case_id: str,
    user_input: str,
    run_kwargs: dict[str, object] | None = None,
    interrupt_when_process: str = "",
) -> dict[str, object]:
    """真模型用例公共驱动器(含同步 run 的 interrupt 进程探测路径)。

    同步路径 InterruptedError 冒泡(披露行为②): interrupt 命中模型请求进行中时
    _run_once_with_params 只 update_runtime_fact 后 re-raise, 异常会抛到调用线程。
    这里不吞掉: 把异常种类/消息记入 outcome.error, 与 result 终态并列, 供判据
    检查「上层不把 cancelled 呈现为普通失败、不重试/不恢复复活」。
    """
    run_id = f"lifecycle-{case_id.lower()}-{uuid.uuid4().hex[:10]}"
    kwargs: dict[str, object] = {
        "request_id": run_id,
        "run_id": run_id,
        "source": "tool_lifecycle_group",
        "allowed_tools": ["run_command"],
        "save": False,
    }
    if run_kwargs:
        kwargs.update(run_kwargs)
    outcome: dict[str, object] = {}
    if not interrupt_when_process:
        try:
            outcome["result"] = agent.run(user_input, **kwargs)
        except Exception as exc:  # noqa: BLE001 - evidence must survive one failed case
            outcome["error"] = f"{type(exc).__name__}: {exc}"
    else:
        from agent_py_agent.agent.concurrency.interrupt import set_interrupt

        def target() -> None:
            try:
                outcome["result"] = agent.run(user_input, **kwargs)
            except Exception as exc:  # noqa: BLE001
                outcome["error"] = f"{type(exc).__name__}: {exc}"

        thread = threading.Thread(target=target, name=f"g4-{case_id}", daemon=True)
        thread.start()
        _interrupt_when_process_running(thread, interrupt_when_process)
        thread.join(timeout=420)
        clear_tid = thread.ident
        if clear_tid is not None:
            set_interrupt(False, clear_tid)
        outcome["interrupt_when_process"] = interrupt_when_process
        outcome["thread_alive_after_join"] = thread.is_alive()
        if thread.is_alive():
            outcome["error"] = str(outcome.get("error") or "run did not finish within join timeout")

    result = outcome.get("result")
    case: dict[str, object] = {
        "test_id": case_id,
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
        "final_prompt": str(getattr(result, "final_prompt", "") or ""),
        "runtime_injections": [
            str(item) for item in (getattr(result, "runtime_injections", None) or []) or []
        ],
        "compression_applied": bool(getattr(result, "compression_applied", False)),
        "compact_continued": bool(getattr(result, "memory_compact_auto_continued", False)),
        "compact_continue_depth": int(getattr(result, "memory_compact_auto_continuation_depth", 0) or 0),
    }
    if outcome.get("error"):
        case["run_error"] = outcome["error"]
    if interrupt_when_process:
        case["interrupt_when_process"] = interrupt_when_process
        case["thread_alive_after_join"] = outcome.get("thread_alive_after_join", False)
    return case


def _process_running(marker: str) -> bool:
    # pgrep -f 按 ERE 解析(第③组实证): 括号/点号必须 re.escape, 否则匹配不到。
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

    # thread.start() 后 ident 可能短暂为 None(第③组实证) -> 每次实时取。
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


def _envelope_reports(item: dict[str, object], codes: set[str]) -> bool:
    envelope = item.get("tool_result_envelope") or {}
    reported = envelope.get("reported_tool_result") or {}
    for key in ("reported_error_code", "error_code", "status"):
        value = str(reported.get(key) or "")
        if value in codes:
            return True
    return False


def _findings_g4_001(case: dict[str, object], workspace: Path) -> list[str]:
    """主代理真实任务: runtime ok + 产物存在内容正确 + handler>0 + 无 run_error。"""
    findings: list[str] = []
    if case.get("run_error"):
        findings.append(f"run_error:{case['run_error']}")
    if case.get("runtime_status") not in _OK_STATUSES:
        findings.append(f"runtime_status:{case.get('runtime_status')}")
    records = _records_of(case)
    if not records:
        findings.append("no_tool_calls")
    if not any(item.get("handler_executed") is True for item in records):
        findings.append("no_handler_execution")
    greeter = workspace / "greeter.py"
    test_file = workspace / "test_greeter.py"
    if not greeter.exists():
        findings.append("greeter_missing")
    else:
        text = greeter.read_text(encoding="utf-8", errors="replace")
        if "greet" not in text or "Hello" not in text:
            findings.append("greeter_content_wrong")
    if not test_file.exists():
        findings.append("test_file_missing")
    else:
        text = test_file.read_text(encoding="utf-8", errors="replace")
        if "greet" not in text or "World" not in text:
            findings.append("test_content_wrong")
    if not case.get("final_answer"):
        findings.append("final_answer_empty")
    return findings


def _run_g4_001(args: argparse.Namespace, run_root: Path) -> dict[str, object]:
    """主代理真实任务完整生命周期: 建小项目+跑测试, 产物与操作记录完整。"""
    agent, workspace = _make_agent(args, run_root, "G4-001")
    case = _run_model_case(
        agent,
        case_id="G4-001",
        user_input=(
            "请在隔离测试环境里用 run_command 完成一个小任务，不要做任何超出任务范围的事：\n"
            "1. 创建文件 greeter.py，内容为一个函数 greet(name)，返回 \"Hello, {name}!\"；\n"
            "2. 创建文件 test_greeter.py，断言 greet(\"World\") == \"Hello, World!\"；\n"
            "3. 运行 python3 test_greeter.py 确认断言通过。\n"
            "完成后用一两句话报告结果。"
        ),
    )
    case["kind"] = "model"
    case["findings"] = _findings_g4_001(case, workspace)
    _case_done(case)
    return case


def _findings_g4_002(case: dict[str, object]) -> list[str]:
    """子代理 closeout: 创建成功 -> dispatch 驱动 -> DONE+VERIFIED 且产物落地。"""
    findings: list[str] = []
    if case.get("create_outcome_error"):
        findings.append(f"create_failed:{case['create_outcome_error']}")
    if not case.get("created_run_ids"):
        findings.append("no_created_run_ids")
        return findings
    statuses = list(case.get("final_statuses") or [])
    if not statuses:
        findings.append("no_subagent_status")
    for entry in statuses:
        if entry.get("status") not in {"DONE", "VERIFIED"} and not (
            entry.get("status") == "DONE" and entry.get("verification_status") == "VERIFIED"
        ):
            findings.append(
                f"subagent_not_done:{entry.get('run_id')}:{entry.get('status')}/{entry.get('verification_status')}"
            )
    artifacts = list(case.get("artifact_files") or [])
    if not artifacts:
        findings.append("no_artifact_files")
    elif not case.get("artifact_content_ok"):
        findings.append("artifact_content_wrong")
    if not case.get("dispatch_report"):
        findings.append("no_dispatch_report")
    return findings


def _run_g4_002(args: argparse.Namespace, run_root: Path) -> dict[str, object]:
    """子代理 closeout: 注入 create_subagents 创建子代理, dispatch 驱动真模型子代理完成。

    注入层只创建任务(服务函数直调,确定性); 子代理执行本身是真模型 runner
    (deepseek-v4-flash@工具运行时), 走完整 dispatch 驱动链(apply+start_runners)。
    """
    from agent_py_agent.agent.agent_core.orchestration_tools import _execute_create_subagents

    agent, workspace = _make_agent(args, run_root, "G4-002", enable_subagents=True)
    case: dict[str, object] = {"test_id": "G4-002", "kind": "mixed-injected-create-model-run"}
    try:
        outcome = _execute_create_subagents(
            agent,
            {
                "goal": (
                    "在隔离工作区创建文件 subagent_proof.txt，内容为 subagent-ok 四个字符，"
                    "然后用 run_command 运行 cat subagent_proof.txt 验证内容正确。"
                ),
                "output_files": [str(workspace / "subagent_proof.txt")],
            },
        )
        case["create_outcome"] = {
            "ok": outcome.ok,
            "error_code": outcome.error_code,
            "handler_executed": outcome.handler_executed,
            "output": outcome.output[:2000],
        }
        if not outcome.ok:
            case["create_outcome_error"] = outcome.output[:500]
    except Exception as exc:  # noqa: BLE001 - evidence must survive one failed case
        case["create_outcome_error"] = f"{type(exc).__name__}: {exc}"

    runs = list(agent.subagents.list_runs())
    run_ids = [task.id for task in runs]
    case["created_run_ids"] = run_ids
    if run_ids:
        from agent_py_agent.agent.agent_core.orchestration.dispatch.params import DispatchParams

        try:
            report = agent.dispatch_subagents(
                None,
                params=params(
                    apply=True,
                    start_runners=True,
                    include_run_ids=run_ids,
                    max_runners=1,
                    probe=False,
                ),
            )
            case["dispatch_report"] = {
                "completed": bool(getattr(report, "completed", False)),
                "run_ids": list(getattr(report, "run_ids", ()) or ()),
                "counts": dict(getattr(report, "counts", None) or {}),
                "summary": str(getattr(report, "summary", ""))[:2000],
            }
        except Exception as exc:  # noqa: BLE001
            case["dispatch_error"] = f"{type(exc).__name__}: {exc}"

        final_statuses: list[dict[str, object]] = []
        for task in agent.subagents.list_runs():
            if task.id not in run_ids:
                continue
            final_statuses.append(
                {
                    "run_id": task.id,
                    "status": str(task.status or ""),
                    "verification_status": str(task.verification_status or ""),
                    "artifact_refs": list(task.artifact_refs or ()),
                    "output_dir": str(task.output_dir or ""),
                }
            )
        case["final_statuses"] = final_statuses

    proof = workspace / "subagent_proof.txt"
    if proof.exists():
        case["artifact_files"] = [str(proof)]
        case["artifact_content_ok"] = proof.read_text(encoding="utf-8", errors="replace").strip() == "subagent-ok"
    else:
        case["artifact_files"] = []
        case["artifact_content_ok"] = False
    case["findings"] = _findings_g4_002(case)
    _case_done(case)
    return case


def _snapshot_files(agent: Any) -> list[Path]:
    """压缩快照落盘位置(宽松收集: home 下 memory_archive/snapshots 的 JSON 文件)。"""
    found: list[Path] = []
    for root in {agent.config.my_agent_home, agent.root}:
        if not root:
            continue
        snap = Path(root) / "memory_archive" / "snapshots"
        if snap.exists():
            found.extend(snap.rglob("*.json"))
    return found


def _findings_g4_003(case: dict[str, object], agent: Any, workspace: Path) -> list[str]:
    """compact: 压缩发生(压缩应用或 snapshot 落盘) + 任务继续完成(产物正确)。"""
    findings: list[str] = []
    if case.get("run_error"):
        findings.append(f"run_error:{case['run_error']}")
    snapshots = _snapshot_files(agent)
    case["snapshot_count"] = len(snapshots)
    compressed = bool(case.get("compression_applied")) or bool(snapshots)
    if not compressed:
        findings.append("no_compression_evidence")
    count_file = workspace / "count.txt"
    if not count_file.exists():
        findings.append("count_file_missing")
    else:
        content = count_file.read_text(encoding="utf-8", errors="replace").strip()
        expected = "6"
        if content != expected:
            findings.append(f"count_content_wrong:{content!r}!=expected:{expected!r}")
    if not case.get("final_answer"):
        findings.append("final_answer_empty")
    return findings


def _run_g4_003(args: argparse.Namespace, run_root: Path) -> dict[str, object]:
    """compact 后继续: 大上下文(约1万 token)注入触发压缩, 任务压缩后仍完成。

    触发机制: _compression_service.check_and_apply 在 _full_prompt_estimate(ctx)
    > max_tokens(本 harness 压低到 4096)时写 snapshot + 压缩 memories -> applied=True。
    """
    agent, workspace = _make_agent(args, run_root, "G4-003")
    filler = ("这是一段用于撑大上下文触发压缩的重复文本。" * 1500)
    case = _run_model_case(
        agent,
        case_id="G4-003",
        user_input=(
            "下面是需要你处理的长文本。请统计其中出现子串 \"xyz\" 的次数，"
            f"把结果(一个数字)写入文件 count.txt，然后简要报告。\n\n{filler}\n"
            f"{filler}\n{filler}\nxyz\n{filler}\nxyz\n{filler}\nxyz\n{filler}\nxyz\n{filler}\nxyz\n{filler}\nxyz\n"
        ),
    )
    case["kind"] = "model"
    case["max_tokens_configured"] = 4096
    case["findings"] = _findings_g4_003(case, agent, workspace)
    _case_done(case)
    return case


def _findings_g4_004(case: dict[str, object], workspace: Path) -> list[str]:
    """resume: 首轮写前半, 次轮带恢复上下文续写后半, 终态文件两行齐全。"""
    findings: list[str] = []
    if case.get("first_run_error"):
        findings.append(f"first_run_error:{case['first_run_error']}")
    if case.get("second_run_error"):
        findings.append(f"second_run_error:{case['second_run_error']}")
    notes = workspace / "notes.md"
    if not notes.exists():
        findings.append("notes_missing")
    else:
        content = notes.read_text(encoding="utf-8", errors="replace")
        if "first-step-done" not in content:
            findings.append("first_step_missing")
        if "second-step-done" not in content:
            findings.append("second_step_missing")
    if not case.get("resume_evidence"):
        findings.append("no_resume_evidence")
    return findings


def _run_g4_004(args: argparse.Namespace, run_root: Path) -> dict[str, object]:
    """resume: 首轮任务 save=True 落盘, 次轮 resume_context=True 带恢复触发词续跑。

    恢复上下文由 build_auto_resume_context 构建(loop_support._resume_context_for_request
    以 request.resume_context 开关), 命中首轮归档证据时注入 Auto Recovery Context。
    """
    agent, workspace = _make_agent(args, run_root, "G4-004")
    first = _run_model_case(
        agent,
        case_id="G4-004-first",
        user_input=(
            "请在隔离工作区创建文件 notes.md，写入一行内容 first-step-done，"
            "然后简要报告。"
        ),
        run_kwargs={"save": True},
    )
    second = _run_model_case(
        agent,
        case_id="G4-004-second",
        user_input=(
            "继续恢复上次的任务：在 notes.md 追加一行 second-step-done（不要覆盖已有内容），"
            "然后简要报告。"
        ),
        run_kwargs={"save": True, "resume_context": True},
    )
    resume_evidence: list[dict[str, object]] = []
    for label, item in (("first", first), ("second", second)):
        if not item:
            continue
        final_prompt = str(item.get("final_prompt") or "")
        if "Auto Recovery Context" in final_prompt or "恢复" in final_prompt:
            resume_evidence.append({"run": label, "final_prompt_has_resume": True})
    case: dict[str, object] = {
        "test_id": "G4-004",
        "kind": "model",
        "first_run_id": first.get("run_id"),
        "second_run_id": second.get("run_id"),
        "first_run_error": first.get("run_error", ""),
        "second_run_error": second.get("run_error", ""),
        "first_runtime_status": first.get("runtime_status"),
        "second_runtime_status": second.get("runtime_status"),
        "resume_evidence": resume_evidence,
        "metrics_first": first.get("metrics"),
        "metrics_second": second.get("metrics"),
    }
    case["findings"] = _findings_g4_004(case, workspace)
    _case_done(case)
    return case


def _run_g4_005(args: argparse.Namespace, run_root: Path) -> dict[str, object]:
    """跨 owner 隔离: owner B(owner_scope_root=home-A/owners/B) 的写操作判定。

    - policy 层机器事实(与 R0 授权门同款 PathAccessPolicy):
      B 写自己 scope 内 -> allow; 写 A 的 owner 目录 -> PATH_CROSS_OWNER_BLOCKED;
      写 home-A 顶层(非 owners) -> PATH_OWNER_SCOPE_BLOCKED。
    - 注入层完整链路: run_command 命令字符串里的重定向路径不经 policy 解析
      (gate 只查 payload 路径参数, shell 执行权归 sandbox/fence 层), 执行层拒绝
      写 -> 命令失败 -> reconcile 保守 TOOL_OPERATION_OUTCOME_UNKNOWN(无法证明
      副作用)。判据=污染检测(A 数据区无 B 写入的文件), UNKNOWN 照实记录不辩解。
    """
    agent, workspace = _make_agent(args, run_root, "G4-005")
    home_a = Path(agent.config.my_agent_home)
    owners_root = home_a / "owners"
    (owners_root / "B").mkdir(parents=True, exist_ok=True)
    (owners_root / "A").mkdir(parents=True, exist_ok=True)
    b_scope = owners_root / "B"
    b_target = b_scope / "b-own.txt"
    a_target = owners_root / "A" / "a-data.txt"
    home_top_target = home_a / "private.txt"

    def b_call(call_id: str, command: str):
        return _inject_call(agent, run_id="g4-005-run", call_id=call_id, command=command)

    def b_execute(call):
        boundary: dict[str, object] = {
            "allowed_write_roots": [str(b_scope)],
        }
        from agent_py_agent.agent.tooling.executor import ToolExecutor, ToolExecutorRequest

        return ToolExecutor().execute(
            ToolExecutorRequest(
                call=call,
                runtime_snapshot=_snapshot(agent, call.run_id),
                workspace_root=b_scope,
                workspace_roots=(b_scope,),
                owner_scope_root=str(b_scope),
                write_boundary=boundary,
                operation_store=agent.local_store,
                operation_store_required=True,
            )
        )

    own = b_execute(b_call("g4-005-own", f"printf '%s\\n' 'b-own' > '{b_target}'"))
    cross = b_execute(b_call("g4-005-cross", f"printf '%s\\n' 'evil' > '{a_target}'"))
    top = b_execute(b_call("g4-005-top", f"printf '%s\\n' 'evil' > '{home_top_target}'"))

    # policy 层机器事实(R0 授权门同款): PathAccessPolicy(owner_scope_root=B) 判定。
    from agent_py_agent.agent.path_access_policy import PathAccessPolicy

    policy = PathAccessPolicy.from_values(
        mode="normal",
        dangerous_roots=(),
        owner_scope_root=str(b_scope),
    )
    policy_decisions = {
        "own_scope": {
            "allowed": policy.check(b_target).allowed,
            "code": policy.check(b_target).code,
        },
        "cross_owner_a": {
            "allowed": policy.check(a_target).allowed,
            "code": policy.check(a_target).code,
        },
        "home_top": {
            "allowed": policy.check(home_top_target).allowed,
            "code": policy.check(home_top_target).code,
        },
    }

    findings: list[str] = []
    if own.result.ok is not True or own.result.handler_executed is not True:
        findings.append(f"own_scope_not_allowed:{own.result.error_code}")
    if policy_decisions["own_scope"]["allowed"] is not True:
        findings.append(f"policy_own_scope_blocked:{policy_decisions['own_scope']['code']}")
    if policy_decisions["cross_owner_a"]["code"] != "PATH_CROSS_OWNER_BLOCKED":
        findings.append(
            f"policy_cross_code:{policy_decisions['cross_owner_a']['code']}"
        )
    if policy_decisions["home_top"]["code"] != "PATH_OWNER_SCOPE_BLOCKED":
        findings.append(f"policy_home_top_code:{policy_decisions['home_top']['code']}")
    if a_target.exists():
        findings.append("a_data_polluted")
    if home_top_target.exists():
        findings.append("home_top_polluted")
    if not b_target.exists():
        findings.append("b_own_file_missing")
    case: dict[str, object] = {
        "test_id": "G4-005",
        "kind": "injected",
        "original_input": "owner B 写 A 数据区 (确定性注入)",
        "run_id": "g4-005-run",
        "owner_scope_root": str(b_scope),
        "policy_decisions": policy_decisions,
        "executions": {
            "own_scope": _execution_evidence(own),
            "cross_owner": _execution_evidence(cross),
            "home_top": _execution_evidence(top),
        },
        "a_data_polluted": a_target.exists(),
        "home_top_polluted": home_top_target.exists(),
        "findings": findings,
    }
    _case_done(case)
    return case


def _run_g4_006(args: argparse.Namespace, run_root: Path) -> dict[str, object]:
    """失败恢复: 注入 COMMAND_FAILED 结构化事实 + 真模型如实收口无死循环无假 DONE。

    命令用 `ls /definitely-nonexistent-dir-xyz`: ls 在 read_only 名单, 静态分析可证明
    无副作用 -> COMMAND_FAILED 原样呈现 + effect_outcome=not_started + retryable=True。
    `false` 不在 read_only 名单, 无法证明无副作用, 会被保守 reconcile 成
    TOOL_OPERATION_OUTCOME_UNKNOWN(防重复副作用的通用合同,设计行为)——所以不选它。
    """
    agent, workspace = _make_agent(args, run_root, "G4-006")
    injected = _execute_injected(
        agent,
        workspace,
        _inject_call(
            agent,
            run_id="g4-006-inject-run",
            call_id="g4-006-inject-call",
            command="ls /definitely-nonexistent-dir-xyz",
        ),
    )
    findings: list[str] = []
    if injected.result.error_code != _COMMAND_FAILED:
        findings.append(f"injected_error_code:{injected.result.error_code}")
    if injected.result.handler_executed is not True:
        findings.append("injected_handler_not_executed")
    if injected.result.ok is True:
        findings.append("injected_ok")
    if not injected.result.retryable:
        findings.append("injected_not_retryable")

    if args.skip_real:
        case: dict[str, object] = {
            "test_id": "G4-006",
            "kind": "injected",
            "original_input": "ls /definitely-nonexistent-dir-xyz (确定性注入)",
            "run_id": "g4-006-inject-run",
            "executions": [_execution_evidence(injected)],
            "model_part": "skipped(--skip-real)",
            "findings": findings,
        }
        _case_done(case)
        return case

    model_case = _run_model_case(
        agent,
        case_id="G4-006-model",
        user_input=(
            "请用 run_command 执行命令 ls /definitely-nonexistent-dir-xyz"
            "（该命令必定失败），"
            "然后如实收口：报告失败并停止，不要重试超过 2 次，也不要编造成功。"
        ),
    )
    model_case["kind"] = "model"
    records = _records_of(model_case)
    failed_calls = [
        item
        for item in records
        if item.get("error_code") == _COMMAND_FAILED
        or item.get("status") == "failed"
        or _envelope_reports(item, {_COMMAND_FAILED, "failed"})
    ]
    if not failed_calls:
        findings.append("no_model_command_failed_record")
    if any(item.get("ok") is True for item in records):
        findings.append("model_fake_success")
    attempts = [
        int(item.get("tool_operation_attempt_count") or 0)
        for item in records
        if item.get("error_code") == _COMMAND_FAILED
    ]
    if attempts and max(attempts) > 3:
        findings.append(f"unbounded_retries:{max(attempts)}")
    if model_case.get("run_error"):
        findings.append(f"model_run_error:{model_case['run_error']}")
    if not model_case.get("final_answer"):
        findings.append("model_final_answer_empty")
    case = {
        "test_id": "G4-006",
        "kind": "mixed",
        "injected_execution": _execution_evidence(injected),
        "model_case": model_case,
        "findings": findings,
    }
    _case_done(case)
    return case


def _run_g4_007(args: argparse.Namespace, run_root: Path) -> dict[str, object]:
    """USE_WAIT_FOR_DELAY 门: sleep 头命令执行前拦截, 副作用未开始(not_started)。

    这是复核 seq 345 要求②的专门证据: 纯延迟命令(error_code=USE_WAIT_FOR_DELAY)
    0ms 级拦截、进程从未启动——effect_outcome=not_started(副作用已证明未发生)。
    handler_executed=True 的语义是「拦截判断进入了 handler」,不是副作用执行了;
    判据认 effect_outcome=not_started + 无 ok + error_code 原样呈现。
    注: `timeout 3 sleep 5`(GNU 包装器)不是纯延迟——tokens 多段,门不拦,会真执行
    3 秒后被 TOOL_TIMEOUT 杀掉→unknown 保守 UNKNOWN(设计行为,G3-005 已覆盖)。
    门的 timeout 分支只认 Windows 风格单参数 `timeout 3`/`timeout /t 3`,这里用它。
    """
    agent, workspace = _make_agent(args, run_root, "G4-007")
    calls = [
        _inject_call(agent, run_id="g4-007-run", call_id="g4-007-sleep", command="sleep 5"),
        _inject_call(
            agent,
            run_id="g4-007-run",
            call_id="g4-007-timeout",
            command="timeout 3",
        ),
        _inject_call(
            agent,
            run_id="g4-007-run",
            call_id="g4-007-start",
            command="start-sleep 2",
        ),
    ]
    findings: list[str] = []
    executions: dict[str, object] = {}
    for call in calls:
        execution = _execute_injected(agent, workspace, call)
        executions[call.call_id] = _execution_evidence(execution)
        if execution.result.error_code != _DELAY_CODE:
            findings.append(f"{call.call_id}_error_code:{execution.result.error_code}")
        if execution.result.effect_outcome != "not_started":
            findings.append(f"{call.call_id}_effect_outcome:{execution.result.effect_outcome}")
        if execution.result.ok is True:
            findings.append(f"{call.call_id}_ok")
    case: dict[str, object] = {
        "test_id": "G4-007",
        "kind": "injected",
        "original_input": "sleep/timeout/start-sleep 头命令 (确定性注入)",
        "run_id": "g4-007-run",
        "policy": {"approval_mode": "dangerous"},
        "executions": executions,
        "findings": findings,
    }
    _case_done(case)
    return case


def _runtime_fact_status(agent: Any, request_id: str) -> dict[str, object]:
    """结构化 cancelled 事实(披露行为②的持久证据): memory_archive/runtime_facts。"""
    from agent_py_agent.agent.agent_core.runtime.owner_roots import runtime_archive_roots

    for root in runtime_archive_roots(agent):
        path = Path(root) / "memory_archive" / "runtime_facts" / request_id / "task.json"
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                return {"error": f"{type(exc).__name__}: {exc}"}
            run_status = payload.get("run_status") or {}
            return {
                "status": str(run_status.get("status") or ""),
                "phase": str((payload.get("runtime_progress") or {}).get("phase") or ""),
                "has_error": bool(run_status.get("error")),
            }
    return {}


def _findings_g4_008(case: dict[str, object]) -> list[str]:
    """InterruptedError 冒泡: 上层收到异常不当作普通失败, 不重试不复活, 事实落账。"""
    findings: list[str] = []
    run_error = str(case.get("run_error") or "")
    if "InterruptedError" not in run_error:
        findings.append(f"no_interrupted_error_bubble:{run_error or 'none'}")
    records = _records_of(case)
    sleep_ok = [
        item
        for item in records
        if item.get("ok") is True and "time.sleep(60)" in str((item.get("parameters") or {}).get("command") or "")
    ]
    if sleep_ok:
        findings.append("cancelled_command_ok_retry")
    fact = dict(case.get("runtime_fact") or {})
    if fact.get("status") != "cancelled":
        findings.append(f"runtime_fact_not_cancelled:{fact.get('status') or 'absent'}")
    if case.get("thread_alive_after_join") is True:
        findings.append("thread_alive_after_join")
    return findings


def _run_g4_008(args: argparse.Namespace, run_root: Path) -> dict[str, object]:
    """InterruptedError 冒泡(复核 seq 345 要求③): 同步 run 中断 -> 异常抛给调用者,
    runtime fact 记 cancelled, 无自动重试/复活。save=True 打开 live archive 以落账。"""
    agent, workspace = _make_agent(args, run_root, "G4-008")
    case = _run_model_case(
        agent,
        case_id="G4-008",
        user_input=(
            "请立刻调用 run_command 执行 `python3 -c \"import time; time.sleep(60)\"` 并等待它完成。"
            "这是一个模拟耗时任务，请只运行这一条命令，不要做其他事情。"
        ),
        interrupt_when_process="time.sleep(60)",
        run_kwargs={"save": True},
    )
    case["kind"] = "model"
    case["runtime_fact"] = _runtime_fact_status(agent, str(case.get("run_id") or ""))
    case["findings"] = _findings_g4_008(case)
    _case_done(case)
    return case


def _git_head() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _harness_revision() -> str:
    """harness 自身 provenance: 报告生成时脚本内容的 sha256, 独立于 checkout HEAD。"""
    import hashlib

    digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return f"sha256:{digest[:16]}"


def main() -> int:
    args = _parse_args()
    if not args.skip_real and not ensure_model_key():
        print("AGENT_API_KEY is required; no fake fallback is allowed.", file=sys.stderr)
        return 2
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_root = args.output_root.expanduser().resolve() / f"tool-g4-{timestamp}"
    run_root.mkdir(parents=True, exist_ok=False)
    exact_command = " ".join(shlex.quote(item) for item in [sys.executable, *sys.argv])

    runners = [
        _run_g4_001,
        _run_g4_002,
        _run_g4_003,
        _run_g4_004,
        _run_g4_005,
        _run_g4_006,
        _run_g4_007,
        _run_g4_008,
    ]
    if args.skip_real:
        runners = [_run_g4_005, _run_g4_006, _run_g4_007]

    cases: list[dict[str, object]] = []
    for runner in runners:
        case_id = runner.__name__.replace("_run_g4_", "G4-").upper()
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

    checkout_head = _git_head()
    env_head = os.environ.get("MY_AGENT_GIT_HEAD", "").strip()
    report = {
        "schema_version": "tool-lifecycle-group.v1",
        "created_at": datetime.now(UTC).isoformat(),
        "provenance": {
            # 复核 seq 345: 实际 checkout HEAD 与 harness revision 分开记录,
            # env override 只作为显式覆盖字段单独列出, 不充当独立 provenance。
            "checkout_head": checkout_head,
            "harness_revision": _harness_revision(),
            "my_agent_git_head_override": env_head or None,
        },
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
