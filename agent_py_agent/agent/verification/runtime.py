# LLM: 公共工具出口先保存验证事实，再尽力写遥测；两条支路互不影响工具结果。
# 模块用途: 记录验证证据、写后过期及工具耗时。
from __future__ import annotations

"""Connect passive verification evidence to the common tool-call seam.

LLM: this module consumes structured tool payload/result facts only.  Failures
to write advisory evidence must never change the underlying tool result.
模块用途: 在所有主代理和子代理共用的工具出口记录测试，并在文件修改后让旧测试过期。
"""

import sqlite3
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from ..agent_core.run_task_workspace_writer import current_run_task_workspace_root
from ..agent_core.runtime.owner_roots import runtime_owner_root
from ..agent_core.runtime.task_identity import durable_task_id
from ..runtime_context import current_subagent_run_id, current_task_attributes
from ..tooling.runtime_contracts import ToolCall, ToolResult
from ..tooling.write_boundary import WRITE_TOOL_NAMES, declared_write_paths
from .project_facts import (
    classify_verification_commands,
    hidden_verification_commands,
    project_facts_for,
)
from .repository import VerificationContext, VerificationEvidence, VerificationEvidenceRepository


# LLM: this is the only runtime integration point for the evidence ledger.  Do
# not add per-IM or per-tool prompt patches elsewhere.
# run_command may prove several verifications at once (an `&&` chain that returns 0): the envelope keeps
# the last event as `verification_evidence` and adds the ordered `verification_evidence_chain`. When a
# verifier ran but a pipe, `;`, `||` or background `&` hid its exit status, nothing is written to the ledger
# and the envelope carries `verification_skipped` so the model can rerun it in a countable form.
# 函数用途: 根据真实工具结果记录测试证据、标记旧证据过期并写入Owner工具调用耗时遥测；已落账通过事实反馈本run采用的准确自学版本，不改变原工具结果。
def record_tool_verification(
    agent: object,
    call: ToolCall,
    result: ToolResult,
) -> ToolResult:
    context = None
    try:
        repository = VerificationEvidenceRepository(runtime_owner_root(agent))
        context = _verification_context(agent)
        additions: dict[str, object] = {}
        if call.tool_name == "run_command":
            evidence = _record_command(
                repository,
                context,
                agent=agent,
                call=call,
                result=result,
            )
            if evidence:
                from ..capability.skill_learning_host_feedback import observe_command_verifications
                observe_command_verifications(agent,call,evidence)
                # 单条证据的形状保持不变；&& 串联整体通过时另附完整有序列表，读归档的消费者不能漏看前面几段。
                additions["verification_evidence"] = evidence[-1]
            if len(evidence) > 1:
                additions["verification_evidence_chain"] = evidence
            skipped = [] if evidence else _hidden_verification(agent, call, result)
            if skipped:
                additions["verification_skipped"] = _skipped_fact(skipped)
        if result.ok and call.tool_name in WRITE_TOOL_NAMES:
            states = _mark_writes(
                repository,
                context,
                agent=agent,
                call=call,
                result=result,
            )
            if states:
                additions["verification_state"] = states
        if additions:
            result = _with_verification_metadata(result, additions)
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError, sqlite3.Error):
        pass
    try:
        _record_tool_telemetry(agent, context or _verification_context(agent), call, result)
    except Exception:
        pass
    return result


# LLM: verification facts belong to the canonical handler_details envelope used by archive and live prompt projection;
# never attach a second top-level metadata shape that downstream readers cannot observe.
# 函数用途: 把测试证据合并进公共工具结果信封，让归档、compact 和收口逻辑读取同一份结构化事实。
def _with_verification_metadata(
    result: ToolResult,
    additions: dict[str, object],
) -> ToolResult:
    metadata = dict(result.metadata)
    raw_details = metadata.get("handler_details")
    details = dict(raw_details) if isinstance(raw_details, dict) else {}
    details.update(additions)
    metadata["handler_details"] = details
    return replace(result, metadata=metadata)


# LLM: 一次命令可能证明多条验证（&& 串联整体返回 0）；逐条落账并按原顺序返回公开事实。进程未正常退出、
#   没有命令或返回码不可读时不记任何证据。副作用：写 owner 验证账。
# 函数用途: 把一次 run_command 的真实结果登记为零到多条验证证据。
def _record_command(
    repository: VerificationEvidenceRepository,
    context: VerificationContext,
    *,
    agent: object,
    call: ToolCall,
    result: ToolResult,
) -> list[dict[str, Any]]:
    process = _handler_metadata(result).get("process")
    if not isinstance(process, dict) or process.get("status") != "exited":
        return []
    command = str(call.arguments.get("command") or "").strip()
    if not command:
        return []
    try:
        exit_code = int(process.get("return_code"))
    except (TypeError, ValueError):
        return []
    cwd = _command_cwd(agent, call.arguments)
    classified = classify_verification_commands(
        command,
        cwd=cwd,
        exit_code=exit_code,
        output=result.output,
    )
    return [
        _public_evidence(repository.record(context, VerificationEvidence(**asdict(item))))
        for item in classified
    ]


# LLM: 只在命令真实退出、却因管道、;、|| 或后台没能计入时列出其中的验证命令；不写验证账，不改工具结果的成败。
# 函数用途: 找出这次 run_command 里没有计入证据的验证命令，供工具结果如实说明。
def _hidden_verification(agent: object, call: ToolCall, result: ToolResult) -> list[str]:
    process = _handler_metadata(result).get("process")
    if not isinstance(process, dict) or process.get("status") != "exited":
        return []
    command = str(call.arguments.get("command") or "").strip()
    return hidden_verification_commands(command, cwd=_command_cwd(agent, call.arguments)) if command else []


# LLM: 结构化字段（status/reason/canonical_commands）是事实；hint 只是给模型的软提示，不参与任何判断。
# 函数用途: 生成“验证命令未计入”的公开事实。
def _skipped_fact(canonical_commands: list[str]) -> dict[str, object]:
    return {
        "status": "not_recorded",
        "reason": "exit_status_hidden",
        "canonical_commands": canonical_commands,
        "hint": "这条命令里的验证没有计入证据：管道、;、|| 或后台会让返回码不属于验证命令本身。"
                "单独运行验证命令（可用 && 串联，不要接 | tail 或 | head，工具会自动截断过长输出）才会计入。",
    }


# LLM: 只根据成功写工具的结构化路径更新仓库证据状态，副作用局限于当前 owner。
# 函数用途: 将被修改工程的旧验证标成过期。
def _mark_writes(
    repository: VerificationEvidenceRepository,
    context: VerificationContext,
    *,
    agent: object,
    call: ToolCall,
    result: ToolResult,
) -> list[dict[str, object]]:
    paths = _structured_changed_paths(agent, call, result)
    by_root: dict[str, list[str]] = {}
    for path in paths:
        facts = project_facts_for(Path(path).parent)
        if facts is not None:
            by_root.setdefault(str(facts.root), []).append(path)
    states: list[dict[str, Any]] = []
    for root, changed_paths in sorted(by_root.items()):
        repository.mark_edited(context, root=root, paths=changed_paths)
        states.append(repository.status(context, root=root))
    return [
        {
            "status": state["status"],
            "root": state["root"],
            "changed_paths": state["changed_paths"],
            **_last_verification_ref(state),
        }
        for state in states
    ]


# LLM: a stale-cycle signature must use the durable verification event, not prose or a changing list of edits;
# this lets closeout remind once per real verify-then-edit cycle without looping after every additional write.
# 函数用途: 从仓库状态中提取上一次真实验证的编号和结果，供收口去重使用。
def _last_verification_ref(state: dict[str, Any]) -> dict[str, object]:
    evidence = state.get("evidence")
    if not isinstance(evidence, dict):
        return {}
    result: dict[str, object] = {
        "last_verification_status": str(evidence.get("status") or ""),
    }
    try:
        result["last_verification_id"] = int(evidence.get("id") or 0)
    except (TypeError, ValueError):
        result["last_verification_id"] = 0
    return result


# LLM: 从宿主的任务参数及 owner 根派生归属，不从工具参数接受身份。
# 函数用途: 取得验证账本所需的任务上下文。
def _verification_context(
    agent: object,
) -> VerificationContext:
    current = getattr(agent, "_current_run_params", None)
    attrs = getattr(current, "task_attributes", None) if current is not None else None
    if not isinstance(attrs, dict):
        attrs = current_task_attributes(agent) or {}
    thread_id = str(attrs.get("conversation_thread_id") or "").strip()
    task_id = str(attrs.get("conversation_task_id") or "").strip()
    task_id = task_id or (durable_task_id(current) if current is not None else "")
    task_id = task_id or current_subagent_run_id(agent) or str(getattr(agent, "_current_request_id", "") or "default")
    home_paths = getattr(agent, "home_paths", None)
    owner_ref = str(getattr(home_paths, "owner_id", "") or runtime_owner_root(agent).name or "local")
    return VerificationContext(owner_ref=owner_ref, thread_id=thread_id or "local", task_id=task_id)


# LLM: 仅在真实 owner home 追加调用；树身份来自宿主作用域，资产参数由账本脱敏后供 Inspector 自动聚合。
# 函数用途: 把主代理与子代理每次工具调用的起止时间、duration_ms、输出字节和关联身份记入 ToolTelemetryLedger。
def _record_tool_telemetry(
    agent: object,
    context: VerificationContext,
    call: ToolCall,
    result: ToolResult,
) -> None:
    import time

    from ..observability.telemetry_ledger import ToolTelemetryLedger

    home_paths = getattr(agent, "home_paths", None)
    raw_home = getattr(home_paths, "owner_home_dir", None)
    if not raw_home:
        return
    owner_home = Path(raw_home)
    if not owner_home.is_dir():
        return
    owner_id = str(getattr(home_paths, "owner_id", "") or context.owner_ref)
    duration_ms = max(0.0, float(result.duration_ms or 0.0))
    ended_at = time.time()
    started_at = max(0.0, ended_at - duration_ms / 1000.0)
    error_code = str(result.error_code or "")
    status = (
        "ok"
        if result.ok
        else "denied"
        if result.failure_stage in {"authorization", "runtime_gate"}
        else "timeout"
        if "TIMEOUT" in error_code.upper()
        else "error"
    )
    args = dict(call.arguments) if isinstance(call.arguments, dict) else {}
    ToolTelemetryLedger(owner_home, owner_id=owner_id).record_tool_call({
        "call_id": str(call.call_id or ""),
        "tool_name": str(call.tool_name or ""),
        "owner_id": owner_id,
        "task_id": context.task_id,
        **_telemetry_identity(agent, context, call),
        "workflow_run_id": str(args.get("workflow_run_id") or ""),
        "step_id": str(args.get("step_id") or ""),
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_ms": duration_ms,
        "status": status,
        "error_code": error_code,
        "args_summary": args,
        "output_bytes": len(str(result.output or "").encode("utf-8", errors="replace")),
        "artifact_ref": str(result.refs[0].ref_id) if result.refs else "",
    })


# LLM: 先确定 run 再选匹配来源的完整父链；同时读取宿主 _current_run_params 与 task_attributes，不跨 run 拼字段，不采用模型参数里的身份。
# 函数用途: 提取完整运行树身份，兼容生产 task_attributes、RunParams、字典和对象作用域。
def _telemetry_identity(agent, context, call):
    current = getattr(agent, "_current_run_params", None)
    attrs = getattr(current, "task_attributes", None) if current is not None else None
    if not isinstance(attrs, dict):
        attrs = getattr(agent, "task_attributes", None)
    if not isinstance(attrs, dict):
        attrs = current_task_attributes(agent) or {}
    scope = getattr(agent, "_current_run_scope", None)
    keys = ("run_id", "parent_run_id", "root_run_id")
    sources = [{k: str((s.get(k) if isinstance(s, dict) else getattr(s, k, "")) or "") for k in keys}
               for s in (context, scope, current, attrs)]
    run_id = str(call.run_id or next((s["run_id"] for s in sources if s["run_id"]), "") or current_subagent_run_id(agent) or "")
    matched = [s for s in sources if s["run_id"] == run_id and (s["parent_run_id"] or s["root_run_id"])]
    if not matched:
        matched = [s for s in sources if s["run_id"] == run_id]
    fallback = [s for s in sources if not s["run_id"] and (s["parent_run_id"] or s["root_run_id"])]
    source = next(iter(matched or fallback), {})
    return {"run_id": run_id, "parent_run_id": source.get("parent_run_id", ""), "root_run_id": source.get("root_run_id", "")}


# LLM: 调用方必须已经授权精确 Goal/thread/task；本入口只沿同一 owner 验证库投影，不验证目标质量或创造验证记录。
# 函数用途: 为已授权目标读取现有验证编号、结果与过期状态，排除命令和输出正文。
def verification_task_metadata(agent: object, *, thread_id: str, task_id: str) -> list[dict]:
    context = replace(_verification_context(agent), thread_id=thread_id, task_id=task_id)
    return VerificationEvidenceRepository(runtime_owner_root(agent)).task_metadata(context)


# LLM: 显式工作目录优先，其次任务工作区和 agent 根；不写文件。
# 函数用途: 确定命令的实际工程目录。
def _command_cwd(agent: object, payload: dict[str, object]) -> Path:
    raw = str(payload.get("working_dir") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve(strict=False)
    task_root = current_run_task_workspace_root(agent, getattr(agent, "_current_run_params", None))
    return task_root or Path(getattr(agent, "root", ".")).expanduser().resolve(strict=False)


# LLM: 采用宿主结果路径，缺失时回退工具声明；仅做路径归一和去重。
# 函数用途: 收集成功工具改动的文件路径。
def _structured_changed_paths(
    agent: object,
    call: ToolCall,
    result: ToolResult,
) -> list[str]:
    envelope = _handler_metadata(result)
    raw_paths: list[object] = []
    raw_paths.extend(envelope.get(key) for key in ("path", "target_path", "output_path"))
    files_modified = envelope.get("files_modified")
    if isinstance(files_modified, list):
        raw_paths.extend(files_modified)
    if not any(str(path or "").strip() for path in raw_paths):
        raw_paths.extend(declared_write_paths(call.tool_name, call.arguments))
    root = Path(getattr(agent, "root", ".")).expanduser().resolve(strict=False)
    task_root = current_run_task_workspace_root(agent, getattr(agent, "_current_run_params", None))
    resolved: list[str] = []
    for raw in raw_paths:
        text = str(raw or "").strip()
        if not text:
            continue
        path = Path(text).expanduser()
        if not path.is_absolute():
            base = task_root if task_root is not None and text.replace("\\", "/").startswith(("output/", "work/")) else root
            path = base / path
        normalized = str(path.resolve(strict=False))
        if normalized not in resolved:
            resolved.append(normalized)
    return resolved


# LLM: 返回信封副本，避免修改原始工具结果。
# 函数用途: 读取处理器结构化元信息。
def _handler_metadata(result: ToolResult) -> dict[str, Any]:
    value = result.metadata.get("handler_details")
    return dict(value) if isinstance(value, dict) else {}


# LLM: 只公开验证事实必需字段，不带账本内部数据。
# 函数用途: 转换一条持久证据为工具附加信息。
def _public_evidence(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "status": row["status"],
        "kind": row["kind"],
        "scope": row["scope"],
        "canonical_command": row["canonical_command"],
        "exit_code": row["exit_code"],
        "root": row["root"],
        "created_at": row["created_at"],
    }


__all__ = ["record_tool_verification"]
