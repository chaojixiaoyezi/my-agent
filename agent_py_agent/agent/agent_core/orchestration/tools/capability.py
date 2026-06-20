# LLM: resolve_capability_requests 工具实现，属于 orchestration/tools。这是主代理对
#   子代理做"收口裁决"的单一显式入口（按 decision 参数复用，不为每种裁决新增工具）：
#     - decision=grant：走 lifecycle.record_capability_grant（请求状态→GRANTED）；
#     - decision=deny：请求状态→CLOSED（协议现有终态，不发明新状态）；
#     grant/deny 都发 wake 唤醒子代理续跑；write_roots 越界（任务工作区与主代理
#     workspace 之外）必须结构化拒绝，不允许静默放行。
#     - decision=accept_output_gaps：登记声明产物缺失豁免到子代理
#       attributes.output_delivery_exemptions（exempt_refs 指定具体声明，缺省=整体
#       通配 "*"，用于纯汇报任务/已确认接受），解除 closeout 的
#       SUBAGENTS_DECLARED_OUTPUTS_MISSING 拦截。豁免可审计、不是静默放水、不中断任务。
#   改动时同步检查 tests/test_resolve_capability_requests_tool.py、
#   delivery_closeout/subagent_aggregation.py 与 docs/audits/R4-goattack-20260611.md。
# 模块用途: 修 R4 多代理收口根因——子代理 capability_request 无人处理、声明产物缺失
#   无豁免出口。这个工具给主代理模型一个显式、结构化、可审计的统一收口入口，配合
#   closeout 的 SUBAGENTS_CAPABILITY_REQUESTS_OPEN / SUBAGENTS_DECLARED_OUTPUTS_MISSING
#   拦截形成闭环。
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ....runtime_errors import runtime_error_report
from ....subagents.model_capabilities import capability_request_requires_parent_resolution
from ....subagents.services.lifecycle import RecordCapabilityGrantParams
from ....tooling.models import BaseTool, ToolExecutionResult
from ..tool_specs import build_resolve_capability_requests_spec

if TYPE_CHECKING:
    from ....core import SimpleAgent

_FILESYSTEM_WRITE_TOOLS = ("write_file", "apply_patch")
_CAPABILITY_DECISIONS = {"grant", "deny"}
_OUTPUT_GAP_DECISION = "accept_output_gaps"
_DECISIONS = _CAPABILITY_DECISIONS | {_OUTPUT_GAP_DECISION}
_OUTPUT_GAP_EXEMPTION_ATTR = "output_delivery_exemptions"


@dataclass(frozen=True)
class _ResolveContext:
    """一次工具调用的裁决上下文（task + 决策 + 原始参数）。"""

    task: Any
    decision: str
    params: dict[str, object]
    reason: str


def _resolve_param_error(run_id: str, decision: str, reason: str) -> ToolExecutionResult | None:
    # 精准报缺哪个参数(修 T5-deliver 阶段3:旧版把 run_id/decision/reason 笼统列一起,
    # M2.7 只缺 reason 却误判成别的参数、试 18 轮没搞清)。reason 仍必填,此处给示例引导。
    if not run_id:
        return _error_result("缺少 run_id(子代理 run_id)。", error_code="TOOL_PARAMETER_REQUIRED")
    if decision not in _DECISIONS:
        return _error_result(
            "缺少或非法 decision,须为 grant|deny|accept_output_gaps 之一。",
            error_code="TOOL_PARAMETER_REQUIRED",
        )
    if not reason:
        return _error_result(
            '缺少 reason(裁决原因,写入审计;收口也要一句话)。示例:'
            'decision="deny",reason="按现有权限写自己的 output 目录即可";'
            'decision="accept_output_gaps",reason="纯汇报任务,产物已在最终报告无需文件"。',
            error_code="TOOL_PARAMETER_REQUIRED",
        )
    return None


class ResolveCapabilityRequestsTool(BaseTool):
    # 类用途: 主代理模型处理子代理能力申请的唯一显式入口；grant/deny 都唤醒子代理。
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_resolve_capability_requests_spec()

    # LLM: 工具响应必须是结构化 JSON（resolved/errors），失败也要给出机器可读原因；
    #   不要把自然语言判断混进裁决路径。持久化顺序契约：先 save 请求状态，再逐条落
    #   grant（record_capability_grant 内部重新加载，顺序反了旧副本会覆盖 grants）。
    # 函数用途: 解析参数、裁决每条未决请求、落盘并发 wake。
    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        run_id = str(params.get("run_id") or "").strip()
        decision = str(params.get("decision") or "").strip().lower()
        reason = str(params.get("reason") or "").strip()
        invalid = _resolve_param_error(run_id, decision, reason)
        if invalid is not None:
            return invalid
        try:
            task = self.agent.subagents.load(run_id)
        except FileNotFoundError:
            return _error_result(f"run_id 不存在：{run_id}")
        if decision == _OUTPUT_GAP_DECISION:
            return self._accept_output_gaps(task, params, reason)
        request_id = str(params.get("request_id") or "").strip()
        pending = _pending_requests(task, request_id)
        if not pending:
            return _error_result(
                f"没有匹配的未决 capability_request（run_id={run_id}"
                + (f", request_id={request_id}" if request_id else "")
                + "）。"
            )
        ctx = _ResolveContext(task=task, decision=decision, params=params, reason=reason)
        resolved, errors, pending_grants = self._judge_pending(ctx, pending)
        self.agent.subagents.save(task)
        self._persist_grants(ctx, pending_grants)
        task = self.agent.subagents.load(run_id)
        _wake_subagent(self.agent, _ResolveContext(task=task, decision=decision, params=params, reason=reason))
        payload = {
            "ok": bool(resolved) and not errors,
            "run_id": run_id,
            "decision": decision,
            "resolved": resolved,
            "errors": errors,
        }
        return ToolExecutionResult(
            "resolve_capability_requests",
            bool(resolved) and not errors,
            json.dumps(payload, ensure_ascii=False, indent=2),
        )

    # 函数用途: 逐条裁决未决请求，返回 (resolved, errors, 待落盘的 grant 列表)。
    def _judge_pending(
        self,
        ctx: _ResolveContext,
        pending: list[Any],
    ) -> tuple[list[dict[str, object]], list[dict[str, object]], list[tuple[Any, dict[str, object]]]]:
        resolved: list[dict[str, object]] = []
        errors: list[dict[str, object]] = []
        pending_grants: list[tuple[Any, dict[str, object]]] = []
        for request in pending:
            record = self._judge_one(ctx, request, errors)
            if record is None:
                continue
            if record.get("ok") and ctx.decision == "grant":
                pending_grants.append((request, record))
            (resolved if record.get("ok") else errors).append(record)
        return resolved, errors, pending_grants

    # 函数用途: 裁决一条请求；异常转结构化 error 记入 errors 并返回 None。
    def _judge_one(self, ctx: _ResolveContext, request: Any, errors: list[dict[str, object]]) -> dict[str, object] | None:
        try:
            if ctx.decision == "grant":
                return self._mark_grant(ctx, request)
            return _deny_one(request, ctx.reason)
        except Exception as exc:
            errors.append(
                {"request_id": getattr(request, "id", ""), **runtime_error_report(exc, context="resolve_capability_requests")}
            )
            return None

    # LLM: accept_output_gaps 裁决（R4 子项③的豁免出口）。把声明产物缺失豁免登记到
    #   子代理 attributes.output_delivery_exemptions（结构化、可审计），closeout 的
    #   _missing_declared_refs 会扣除豁免。exempt_refs 指定具体声明；缺省登记通配 "*"
    #   （整体豁免，用于纯汇报任务/已确认接受全部缺失）。幂等：同 ref 不重复登记。
    # 函数用途: 主代理显式接受某子代理的声明产物缺失，解除 closeout 拦截，不中断任务。
    def _accept_output_gaps(self, task: Any, params: dict[str, object], reason: str) -> ToolExecutionResult:
        targets = _string_list(params.get("exempt_refs")) or ["*"]
        attrs = dict(getattr(task, "attributes", {}) or {})
        records = list(attrs.get(_OUTPUT_GAP_EXEMPTION_ATTR) or [])
        existing_refs = {str(r.get("ref") or "") for r in records if isinstance(r, dict)}
        now = time.time()
        added: list[dict[str, object]] = []
        for ref in targets:
            if ref in existing_refs:
                continue
            entry = {"ref": ref, "reason": reason, "accepted_by": "resolve_capability_requests", "at": now}
            records.append(entry)
            existing_refs.add(ref)
            added.append(entry)
        attrs[_OUTPUT_GAP_EXEMPTION_ATTR] = records
        task.attributes = attrs
        self.agent.subagents.save(task)
        payload = {
            "ok": True,
            "run_id": str(getattr(task, "id", "") or ""),
            "decision": _OUTPUT_GAP_DECISION,
            "exempted": added,
            "total_exemptions": len(records),
        }
        return ToolExecutionResult(
            "resolve_capability_requests",
            True,
            json.dumps(payload, ensure_ascii=False, indent=2),
        )

    # 函数用途: 把标记通过的 grant 逐条写进任务（副作用：record_capability_grant 落盘）。
    def _persist_grants(self, ctx: _ResolveContext, pending_grants: list[tuple[Any, dict[str, object]]]) -> None:
        for request, record in pending_grants:
            grant = self.agent.subagents.lifecycle.record_capability_grant(
                ctx.task.id,
                RecordCapabilityGrantParams(
                    request_id=request.id,
                    grant_type=str(getattr(request, "capability_type", "") or "generic"),
                    tools=list(record.get("tools") or []),
                    skills=_string_list(getattr(request, "requested_skills", [])),
                    reason=ctx.reason,
                    path_scope=list(record.get("path_scope") or []),
                    request_scope={"resolved_by": "resolve_capability_requests"},
                ),
            )
            record["grant_id"] = grant.id

    # LLM: grant 裁决（标记阶段，不落盘）。write_roots 围栏是客观事实硬门：目录必须在
    #   任务工作区或主代理 workspace 内。filesystem 类授权自动并入写工具，否则 path_scope
    #   在运行时不生效（见 runner_context_service._granted_filesystem_write_roots 的 tools 要求）。
    # 函数用途: 校验一条 grant 并标记请求状态，返回结构化裁决记录（grant 落盘在 execute）。
    def _mark_grant(self, ctx: _ResolveContext, request: Any) -> dict[str, object]:
        requested_roots = _string_list(ctx.params.get("write_roots")) or _string_list(getattr(request, "path_scope", []))
        allowed_roots, rejected_roots = self._partition_safe_roots(ctx.task, requested_roots)
        if requested_roots and not allowed_roots:
            return {
                "ok": False,
                "request_id": request.id,
                "error": "write_roots 全部越界（必须在任务工作区或主代理 workspace 内），未授权。",
                "rejected_write_roots": rejected_roots,
            }
        tools = _string_list(ctx.params.get("tools")) or _string_list(getattr(request, "requested_tools", []))
        if allowed_roots:
            tools = list(dict.fromkeys([*tools, *_FILESYSTEM_WRITE_TOOLS]))
        request.status = "GRANTED"
        record: dict[str, object] = {
            "ok": True,
            "request_id": request.id,
            "status": "GRANTED",
            "path_scope": allowed_roots,
            "tools": tools,
        }
        if rejected_roots:
            record["rejected_write_roots"] = rejected_roots
        return record

    # 函数用途: 把申请目录按安全围栏分成可授权/越界两组。
    def _partition_safe_roots(self, task: Any, roots: list[str]) -> tuple[list[str], list[str]]:
        safe_roots = _safe_grant_roots(self.agent, task)
        allowed: list[str] = []
        rejected: list[str] = []
        for raw in roots:
            target = Path(raw).expanduser().resolve(strict=False)
            bucket = allowed if any(_is_relative_to(target, base) for base in safe_roots) else rejected
            if raw not in bucket:
                bucket.append(raw)
        return allowed, rejected


# 函数用途: 给一条请求落显式拒绝（协议终态 CLOSED），原因写入 constraints 审计。
def _deny_one(request: Any, reason: str) -> dict[str, object]:
    request.status = "CLOSED"
    constraints = dict(getattr(request, "constraints", {}) or {})
    constraints["denial_reason"] = reason
    request.constraints = constraints
    return {"ok": True, "request_id": request.id, "status": "CLOSED", "denial_reason": reason}


# 函数用途: grant 目录围栏的基准集合：子代理任务工作区 + 主代理 workspace roots。
def _safe_grant_roots(agent: Any, task: Any) -> list[Path]:
    bases: list[Path] = []
    for raw in (
        getattr(task, "task_workspace_dir", ""),
        getattr(task, "task_dir", ""),
        getattr(agent, "workspace_root", "") or getattr(agent, "root", ""),
        *(getattr(agent, "workspace_roots", None) or []),
    ):
        text = str(raw or "").strip()
        if text:
            bases.append(Path(text).expanduser().resolve(strict=False))
    return bases


# 函数用途: 找出该 run 需要父级裁决的请求（OPEN 或 fail-closed 的非法状态）。
def _pending_requests(task: Any, request_id: str) -> list[Any]:
    pending = [
        request
        for request in (getattr(task, "capability_requests", None) or [])
        if capability_request_requires_parent_resolution(getattr(request, "status", "OPEN"))
    ]
    if request_id:
        pending = [request for request in pending if str(getattr(request, "id", "")) == request_id]
    return pending


# LLM: grant/deny 都要唤醒子代理（wake observation + signal + attrs 账本），deny 的
#   文案必须告诉子代理"按现有权限调整方案"，不能让它继续等待授权。
#   决策账本必须先落盘（裁决事实比信号重要）；wake 信号失败/无 thread 只补记状态不抛。
# 函数用途: 处理完请求后记录裁决账本并唤醒子代理续跑。
def _wake_subagent(agent: Any, ctx: _ResolveContext) -> None:
    run_id = str(getattr(ctx.task, "id", "") or "")
    _record_resolution_wake(agent, ctx, status="resolved", wake_signal_id="")
    store = getattr(agent, "conversation_store", None)
    if store is None:
        return
    try:
        thread = store.thread_for_task(run_id)
        if thread is None:
            _record_resolution_wake(agent, ctx, status="no_thread", wake_signal_id="")
            return
        observation = store.append_observation(_resolution_observation(thread, ctx, run_id))
        signal = store.raise_wake_signal(_resolution_signal(thread, ctx, run_id, observation))
        _record_resolution_wake(agent, ctx, status="raised", wake_signal_id=signal.wake_signal_id)
    except Exception as exc:
        attrs = dict(getattr(ctx.task, "attributes", {}) or {})
        attrs["capability_resolution_wake_error"] = runtime_error_report(
            exc, context="resolve_capability_requests.wake"
        )
        ctx.task.attributes = attrs
        try:
            agent.subagents.save(ctx.task)
        except Exception:
            # 容忍:这是 wake 失败后"把错误注记落盘"的尽力而为二次保存,主错误已记入
            # attributes;再崩会在错误处理里制造更严重的崩溃。不再无声——记日志可查。
            logging.getLogger(__name__).warning(
                "capability wake-error attribute save failed (run_id=%s)",
                str(getattr(ctx.task, "id", "") or ""),
                exc_info=True,
            )
            return


# 函数用途: 构造裁决结果的 observation payload（告知子代理线程已授权/已拒绝）。
def _resolution_observation(thread: Any, ctx: _ResolveContext, run_id: str) -> dict[str, object]:
    summary = (
        f"子代理 {run_id} 的 capability_request 已授权，继续同一任务后续执行。"
        if ctx.decision == "grant"
        else f"子代理 {run_id} 的 capability_request 已被显式拒绝（{ctx.reason}）；按现有权限调整方案或交付能完成的部分。"
    )
    return {
        "thread_id": thread.thread_id,
        "event_type": f"subagent_capability_{'granted' if ctx.decision == 'grant' else 'denied'}",
        "summary": summary,
        "urgency": "normal",
        "source_agent_id": run_id,
        "parent_agent_id": str(getattr(ctx.task, "parent_id", "") or ""),
        "root_task_id": str(getattr(ctx.task, "root_id", "") or run_id),
        "requires_main_agent": False,
        "metadata": {"run_id": run_id, "capability_resolution": ctx.decision},
    }


# 函数用途: 构造裁决结果的 wake signal payload（带去重键，让子代理续跑）。
def _resolution_signal(thread: Any, ctx: _ResolveContext, run_id: str, observation: Any) -> dict[str, object]:
    return {
        "thread_id": thread.thread_id,
        "observation": observation,
        "urgency": "normal",
        "reason": f"subagent_capability_{'granted' if ctx.decision == 'grant' else 'denied'}",
        "source_agent_id": run_id,
        "parent_agent_id": str(getattr(ctx.task, "parent_id", "") or ""),
        "root_task_id": str(getattr(ctx.task, "root_id", "") or run_id),
        "dedupe_key": f"capability-{ctx.decision}:{run_id}",
        "metadata": {"run_id": run_id, "grant_wake": ctx.decision == "grant"},
    }


# 函数用途: 落结构化裁决账本（attributes.capability_resolution_wake）并保存任务。
def _record_resolution_wake(agent: Any, ctx: _ResolveContext, *, status: str, wake_signal_id: str) -> None:
    attrs = dict(getattr(ctx.task, "attributes", {}) or {})
    attrs["capability_resolution_wake"] = {
        "schema_version": "capability_resolution_wake.v1",
        "decision": ctx.decision,
        "wake_signal_id": wake_signal_id,
        "status": status,
    }
    ctx.task.attributes = attrs
    try:
        agent.subagents.save(ctx.task)
    except Exception:
        # capability 裁决账本是授权决策的权威记录,丢失要可查(error 级)。in-memory
        # attribute 已先写,这里只是持久化失败;不抛——本函数也用于 wake 成功后落账,
        # 抛出会把已成功的唤醒回滚成整体失败。
        logging.getLogger(__name__).error(
            "capability_resolution_wake ledger save failed (run_id=%s decision=%s status=%s)",
            str(getattr(ctx.task, "id", "") or ""),
            ctx.decision,
            status,
            exc_info=True,
        )
        return


# 函数用途: 统一把工具参数解析成去重字符串列表。
def _string_list(value: object) -> list[str]:
    if isinstance(value, str):
        items = [piece.strip() for piece in value.split(",")]
    elif isinstance(value, list | tuple | set):
        items = [str(item or "").strip() for item in value]
    else:
        return []
    return list(dict.fromkeys([item for item in items if item]))


# 函数用途: Path.relative_to 的布尔包装（含相等）。
def _is_relative_to(path: Path, root: Path) -> bool:
    if path == root:
        return True
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


# 函数用途: 统一的参数错误响应（带准确分类码，避免无码兜底成 UNKNOWN_ERROR 误导模型放弃）。
def _error_result(message: str, *, error_code: str = "TOOL_INVALID_ARGUMENTS") -> ToolExecutionResult:
    return ToolExecutionResult("resolve_capability_requests", False, message, error_code=error_code)


__all__ = ["ResolveCapabilityRequestsTool"]
