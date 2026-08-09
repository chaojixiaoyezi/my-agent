# LLM: resolve_capability_requests 工具实现，属于 orchestration/tools。这是主代理对
#   子代理做能力申请裁决的单一显式入口（按 decision 参数复用，不为每种裁决新增工具）：
#     - decision=grant：走 lifecycle.record_capability_grant（请求状态→GRANTED）；
#     - decision=deny：请求状态→CLOSED（协议现有终态，不发明新状态）；
#     grant/deny 都发 wake 唤醒子代理续跑；write_roots 越界（任务工作区与主代理
#     workspace 之外）必须结构化拒绝，不允许静默放行。
#   改动时同步检查 tests/test_resolve_capability_requests_tool.py、
#   子代理聚合记录与 docs/audits/R4-goattack-20260611.md。
# 模块用途: 子代理 capability_request 的显式、结构化、可审计处理入口。
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ....runtime_errors import runtime_error_report
from ....subagents.authorization_gate import OperationRequest, authorize_operation
from ....subagents.model_capabilities import capability_request_requires_parent_resolution
from ....subagents.models import SUBAGENT_ENDED_STATUSES, task_status_in
from ....subagents.services.lifecycle import RecordCapabilityGrantParams
from ....tooling.models import (
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ResourceScopePolicy,
    ToolHandlerOutcome,
    ToolRuntimePolicy,
)
from ..create_policy import _current_run_id
from ..tool_specs import build_resolve_capability_requests_model_spec

if TYPE_CHECKING:
    from ....core import SimpleAgent

_FILESYSTEM_WRITE_TOOLS = ("write_file", "apply_patch")
_CAPABILITY_DECISIONS = {"grant", "deny"}


@dataclass(frozen=True)
class _ResolveContext:
    """一次工具调用的裁决上下文（task + 决策 + 原始参数）。"""

    task: Any
    decision: str
    params: dict[str, object]
    reason: str


def _resolve_param_error(run_id: str, decision: str, reason: str) -> ToolHandlerOutcome | None:
    # 精准报缺哪个参数(修 T5-deliver 阶段3:旧版把 run_id/decision/reason 笼统列一起,
    # M2.7 只缺 reason 却误判成别的参数、试 18 轮没搞清)。reason 仍必填,此处给示例引导。
    if not run_id:
        return _error_result("缺少 run_id(子代理 run_id)。", error_code="TOOL_PARAMETER_REQUIRED")
    if decision not in _CAPABILITY_DECISIONS:
        return _error_result(
            "缺少或非法 decision,须为 grant|deny 之一。",
            error_code="TOOL_PARAMETER_REQUIRED",
        )
    if not reason:
        return _error_result(
            '缺少 reason(裁决原因,写入审计)。示例:'
            'decision="deny",reason="按现有权限写自己的 output 目录即可"。',
            error_code="TOOL_PARAMETER_REQUIRED",
        )
    return None


class ResolveCapabilityRequestsTool(BaseTool):
    # 类用途: 主代理模型处理子代理能力申请的唯一显式入口；grant/deny 都唤醒子代理。
    model_spec = build_resolve_capability_requests_model_spec()
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy(
            "mutating",
            by_parameter=(("decision", (("grant", "dangerous"), ("deny", "mutating"))),),
        ),
        idempotency_policy=IdempotencyPolicy("operation"),
        resource_scopes=ResourceScopePolicy(parameter_names=("run_id", "request_id", "write_roots")),
    )

    def __init__(self, agent: SimpleAgent):
        self.agent = agent

    # LLM: 工具响应必须是结构化 JSON（resolved/errors），失败也要给出机器可读原因；
    #   不要把自然语言判断混进裁决路径。持久化顺序契约：先 save 请求状态，再逐条落
    #   grant（record_capability_grant 内部重新加载，顺序反了旧副本会覆盖 grants）。
    # 函数用途: 解析参数、裁决每条未决请求、落盘并发 wake。
    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        run_id = str(params.get("run_id") or "").strip()
        decision = str(params.get("decision") or "").strip().lower()
        reason = str(params.get("reason") or "").strip()
        invalid = _resolve_param_error(run_id, decision, reason)
        if invalid is not None:
            return invalid
        try:
            # 3.txt B.4：resolve capability 走统一授权查询门（owner + 子树）。
            task = authorize_operation(
                self.agent.subagents,
                OperationRequest(
                    operation="resolve_capability",
                    run_id=run_id,
                    requester_owner=str(
                        getattr(getattr(self.agent, "home_paths", None), "owner_id", "") or ""
                    ),
                    requester_run_id=_current_run_id(self.agent),
                ),
            )
        except FileNotFoundError:
            return _error_result(f"run_id 不存在：{run_id}。{_known_children_hint(self.agent)}")
        except PermissionError as exc:
            return _error_result(f"无权操作 {run_id}：{exc}")
        request_id = str(params.get("request_id") or "").strip()
        pending = _pending_requests(task, request_id)
        if not pending:
            all_pending = _pending_requests(task, "") if request_id else []
            if all_pending:
                return _error_result(
                    f"request_id 不匹配：{request_id}。该子代理当前未决申请: "
                    + ", ".join(str(getattr(item, "id", "")) for item in all_pending)
                    + "。用这些 request_id 重试,或不传 request_id 一次裁决全部。"
                )
            return _no_pending_result(task, run_id, decision)
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
        return ToolHandlerOutcome(
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

    # 函数用途: 把标记通过的 grant 逐条写进任务（副作用：record_capability_grant 落盘）。
    def _persist_grants(self, ctx: _ResolveContext, pending_grants: list[tuple[Any, dict[str, object]]]) -> None:
        for request, record in pending_grants:
            grant = self.agent.subagents.lifecycle.record_capability_grant(
                ctx.task.id,
                RecordCapabilityGrantParams(
                    request_id=request.id,
                    grant_type=str(getattr(request, "capability_type", "") or "generic"),
                    tools=list(record.get("tools") or []),
                    skills=list(record.get("skills") or []),
                    capability_cards=list(record.get("capability_cards") or []),
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
        skills, capability_cards, skill_error = _resolved_skill_grant(
            self.agent,
            _string_list(getattr(request, "requested_skills", [])),
        )
        if skill_error:
            return {
                "ok": False,
                "request_id": request.id,
                "error": skill_error,
            }
        if allowed_roots:
            tools = list(dict.fromkeys([*tools, *_FILESYSTEM_WRITE_TOOLS]))
        request.status = "GRANTED"
        record: dict[str, object] = {
            "ok": True,
            "request_id": request.id,
            "status": "GRANTED",
            "path_scope": allowed_roots,
            "tools": tools,
            "skills": skills,
            "capability_cards": capability_cards,
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


def _resolved_skill_grant(
    agent: object,
    requested: list[str],
) -> tuple[list[str], list[dict[str, str]], str]:
    if not requested:
        return [], [], ""
    snapshot = agent.current_skill_snapshot()
    skills: list[str] = []
    cards: list[dict[str, str]] = []
    missing: list[str] = []
    for reference in requested:
        entry = snapshot.resolve(reference)
        if entry is None:
            missing.append(reference)
            continue
        if entry.stable_id in skills:
            continue
        skills.append(entry.stable_id)
        cards.append(
            {
                "id": f"skill:{entry.stable_id}",
                "kind": "skill",
                "name": entry.name,
                "stable_id": entry.stable_id,
                "source": entry.source,
                "content_sha256": entry.content_sha256,
                "path": "",
            }
        )
    if missing:
        return [], [], "requested_skills 当前不可用或已禁用：" + ", ".join(missing)
    return skills, cards, ""


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


# LLM: "没有待裁决申请"是幂等 no-op 成功,不是失败。真机实锤(0/22 编队全灭链):常规
#   申请早被机制层自动批掉,主代理对着 BLOCKED 子代理反复 grant → 同参数业务失败 3 次
#   触发工具熔断 → 熔断兜底文案又像权限墙 → 模型判定"解阻工具坏了"放弃整条编队。
#   这里必须给足客观事实(子代理状态+申请账目)和下一步指引,让模型转去重派/给提示/了结。
# 函数用途: 无未决申请时的结构化成功响应(状态实情 + 可执行下一步,终结重试螺旋)。
def _no_pending_result(task: Any, run_id: str, decision: str) -> ToolHandlerOutcome:
    counts: dict[str, int] = {}
    for request in getattr(task, "capability_requests", None) or []:
        status = str(getattr(request, "status", "") or "UNKNOWN").upper()
        counts[status] = counts.get(status, 0) + 1
    payload = {
        "ok": True,
        "run_id": run_id,
        "decision": decision,
        "resolved": [],
        "status": "no_pending_requests",
        "child_status": str(getattr(task, "status", "") or ""),
        "capability_request_status_counts": counts,
        "note": (
            "该子代理没有待裁决的能力申请(常规申请由机制层自动批准,GRANTED/CLOSED 的无需重复裁决),"
            "不必再调本工具。若它仍未推进:dispatch_subagents 重派、send_guidance 补提示、"
            "救不回来就 cancel_subagents 了结,别晾着拖收尾。"
        ),
    }
    return ToolHandlerOutcome(
        "resolve_capability_requests", True, json.dumps(payload, ensure_ascii=False, indent=2)
    )


# 函数用途: run_id 打错时给出真实子代理名册(有未决申请的排前、未终态次之),供模型自纠。
def _known_children_hint(agent: Any) -> str:
    try:
        tasks = agent.subagents.list_runs()
    except Exception:
        return ""
    rows: list[tuple[int, int, str, str]] = []
    for task in tasks:
        open_requests = sum(
            1
            for request in (getattr(task, "capability_requests", None) or [])
            if capability_request_requires_parent_resolution(getattr(request, "status", "OPEN"))
        )
        status = str(getattr(task, "status", "") or "")
        ended = 1 if task_status_in(status, SUBAGENT_ENDED_STATUSES) else 0
        rows.append((open_requests, ended, str(getattr(task, "id", "") or ""), status))
    rows.sort(key=lambda item: (-item[0], item[1]))
    listing = "; ".join(
        f"{run_id}({status}" + (f",未决申请x{open_requests}" if open_requests else "") + ")"
        for open_requests, _ended, run_id, status in rows[:12]
        if run_id
    )
    return f"当前子代理: {listing}。用列表里的真实 run_id 重试。" if listing else ""


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
def _error_result(message: str, *, error_code: str = "TOOL_INVALID_ARGUMENTS") -> ToolHandlerOutcome:
    return ToolHandlerOutcome("resolve_capability_requests", False, message, error_code=error_code)


__all__ = ["ResolveCapabilityRequestsTool"]
