
from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from ....subagents.authorization_gate import (
    OperationRequest,
    authorize_operation,
    authorize_tree_scope,
)
from ....subagents.models import (
    SUBAGENT_FAILED_RESULT_STATUSES,
    SUBAGENT_RESOLVED_TERMINAL_STATUSES,
    TaskStatus,
    task_status_in,
)
from ....tooling.models import (
    BaseTool,
    ConcurrencyPolicy,
    EffectResolverPolicy,
    ResourceScopePolicy,
    ToolHandlerOutcome,
    ToolRuntimePolicy,
)
from ...agent_tree.status import agent_tree_status_payload
from ..create_policy import _current_run_id
from ..context.live_summary import orchestration_payload_summary
from ..tool_specs import (
    build_inspect_agent_tree_model_spec,
)

if TYPE_CHECKING:
    from ....core import SimpleAgent


class InspectAgentTreeTool(BaseTool):
    model_spec = build_inspect_agent_tree_model_spec()
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("read_only"),
        concurrency_policy=ConcurrencyPolicy("parallel_safe"),
        # seq 253 闭合：root_id/run_id 是逻辑 ID 不是路径。
        # seq 266 #1：run_id 与 cancel/dispatch/wait 的 run 参数是同一
        # agent_run 资源（统一域，跨工具互斥）。
        resource_scopes=ResourceScopePolicy(
            parameter_names=("root_id", "run_id"),
            parameter_kinds={"root_id": "logical", "run_id": "logical"},
            resource_domains={"run_id": "agent_run", "root_id": "run_tree"},
        ),
    )

    def __init__(self, agent: SimpleAgent):
        self.agent = agent

    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        cached = _cached_cooldown_payload_before_render(self.agent, params)
        if cached is not None:
            return _inspect_result(cached)
        denied = _authorize_inspect_scope(self.agent, params)
        if denied is not None:
            return denied
        payload = agent_tree_status_payload(self.agent, params)
        cooldown = _cooldown_payload_for_current(self.agent, params, payload)
        if cooldown is not None:
            return _inspect_result(cooldown)
        _remember_payload(self.agent, params, payload)
        return _inspect_result(payload)


def _authorize_inspect_scope(
    agent: SimpleAgent,
    params: dict[str, object],
) -> ToolHandlerOutcome | None:
    """3.txt B.4：inspect 走统一授权查询门（跨树越权拒绝）。

    无 run_id/root_id（主代理看全树/当前 scope）→ 不拦（系统语义）；
    有目标 → run_id 单点门 / root_id 树级门。
    """
    target_run = str(params.get("run_id") or "").strip()
    target_root = str(params.get("root_id") or "").strip()
    if not target_run and not target_root:
        return None
    owner = str(getattr(getattr(agent, "home_paths", None), "owner_id", "") or "")
    requester_run = _current_run_id(agent)
    try:
        if target_run:
            authorize_operation(
                agent.subagents,
                OperationRequest(
                    operation="inspect",
                    run_id=target_run,
                    requester_owner=owner,
                    requester_run_id=requester_run,
                ),
            )
        else:
            authorize_tree_scope(
                agent.subagents,
                OperationRequest(
                    operation="inspect",
                    run_id=target_root,
                    requester_owner=owner,
                    requester_run_id=requester_run,
                ),
                target_root,
            )
    except (PermissionError, OSError) as exc:
        return _inspect_result({"schema_version": "agent_tree_status.v1", "effect": "read_only",
                                "error": f"inspect 被授权门拒绝: {exc}"})
    return None


def _inspect_result(payload: dict[str, object]) -> ToolHandlerOutcome:
    return ToolHandlerOutcome(
        "inspect_agent_tree",
        True,
        json.dumps(payload, ensure_ascii=False, indent=2),
        result_envelope={
            "tool_output_policy": {
                "live_prompt_output": orchestration_payload_summary(
                    "inspect_agent_tree",
                    payload,
                )
            }
        },
    )


def _cooldown_payload_for_current(
    agent: SimpleAgent,
    params: dict[str, object],
    current_payload: dict[str, object],
) -> dict[str, object] | None:
    key = _cache_key(params)
    cache = getattr(agent, "_inspect_agent_tree_recent_cache", None)
    if not isinstance(cache, dict):
        return None
    row = cache.get(key)
    if not isinstance(row, dict):
        return None
    cooldown_seconds = _cooldown_seconds(agent, params)
    age = time.time() - float(row.get("created_at", 0.0) or 0.0)
    if age < 0 or age > cooldown_seconds:
        return None
    previous_payload = row.get("payload")
    if _payload_change_signature(previous_payload) != _payload_change_signature(current_payload):
        return None
    payload = _cooldown_payload(current_payload, cooldown_seconds)
    payload["cooldown_active"] = True
    payload["cooldown_seconds"] = cooldown_seconds
    payload["cooldown_age_seconds"] = round(age, 3)
    warnings = list(payload.get("warnings") if isinstance(payload.get("warnings"), list) else [])
    if "inspect_agent_tree_recent_duplicate" not in warnings:
        warnings.append("inspect_agent_tree_recent_duplicate")
    payload["warnings"] = warnings
    _apply_cooldown_wait_policy(payload, cooldown_seconds)
    return payload


def _cached_cooldown_payload_before_render(agent: SimpleAgent, params: dict[str, object]) -> dict[str, object] | None:
    key = _cache_key(params)
    cache = getattr(agent, "_inspect_agent_tree_recent_cache", None)
    if not isinstance(cache, dict):
        return None
    row = cache.get(key)
    if not isinstance(row, dict):
        return None
    cooldown_seconds = _cooldown_seconds(agent, params)
    age = time.time() - float(row.get("created_at", 0.0) or 0.0)
    if age < 0 or age > cooldown_seconds:
        return None
    fingerprint = _tree_state_fingerprint(agent)
    if not fingerprint or fingerprint != row.get("tree_state_fingerprint"):
        return None
    previous_payload = row.get("payload")
    payload = _cooldown_payload(previous_payload, cooldown_seconds)
    payload["cooldown_active"] = True
    payload["cooldown_seconds"] = cooldown_seconds
    payload["cooldown_age_seconds"] = round(age, 3)
    payload["cooldown_source"] = "cached_tree_state_fingerprint"
    warnings = list(payload.get("warnings") if isinstance(payload.get("warnings"), list) else [])
    if "inspect_agent_tree_recent_duplicate" not in warnings:
        warnings.append("inspect_agent_tree_recent_duplicate")
    payload["warnings"] = warnings
    _apply_cooldown_wait_policy(payload, cooldown_seconds)
    return payload


def _apply_cooldown_wait_policy(payload: dict[str, object], cooldown_seconds: float) -> None:
    wait_seconds = max(60, int(cooldown_seconds) or 0)
    wait_call = {"tool": "wait", "seconds": wait_seconds, "reason": "inspect_agent_tree cooldown"}
    policy = dict(payload.get("policy") if isinstance(payload.get("policy"), dict) else {})
    policy["next_step"] = "刚刚已经查看过同一代理树；除非需要验收、接管或已有新事实，否则先推进汇总/等待子代理产物，不要高频轮询。"
    policy["suggested_tool_call"] = wait_call
    payload["policy"] = policy
    direct_children = dict(payload.get("direct_children") if isinstance(payload.get("direct_children"), dict) else {})
    direct_children["suggested_tool_call"] = wait_call
    payload["direct_children"] = direct_children


def _cooldown_payload(value: object, cooldown_seconds: float) -> dict[str, object]:
    previous = value if isinstance(value, dict) else {}
    status = previous.get("status_buckets") if isinstance(previous.get("status_buckets"), dict) else {}
    nodes = previous.get("nodes") if isinstance(previous.get("nodes"), list) else []
    wait_seconds = max(60, int(cooldown_seconds) or 0)
    return {
        "schema_version": previous.get("schema_version", "agent_tree_status.v1"),
        "root_id": previous.get("root_id", ""),
        "status": "POLL_COOLDOWN",
        "summary": "同一代理树刚刚已经检查过；cooldown 内不重复返回完整树，避免父代理高频轮询或误判后重复派工。",
        "direct_children": {
            "total": len(nodes),
            "by_status": {
                "running": status.get("running", []),
                "blocked": status.get("blocked", []),
                "completed": status.get("completed", []),
                "failed": status.get("failed", []),
            },
            "running_run_ids": status.get("running", []),
            "planning_run_ids": _node_ids_with_status(
                nodes,
                {TaskStatus.PLANNING.value, TaskStatus.PENDING.value},
            ),
            "unfinished_run_ids": _unfinished_run_ids(nodes),
            "next_action": "end_turn_or_continue_own_work",
            "suggested_tool_call": {"tool": "wait", "seconds": wait_seconds, "reason": "登记非阻塞进度提醒后结束本回合;子代理有进展会用事件唤醒你,别原地轮询"},
        },
    }


def _payload_change_signature(value: object) -> tuple[tuple[object, ...], ...]:
    payload = value if isinstance(value, dict) else {}
    nodes = payload.get("nodes") if isinstance(payload.get("nodes"), list) else []
    rows: list[tuple[object, ...]] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        rows.append(
            (
                str(node.get("run_id") or ""),
                str(node.get("status") or "").strip(),
                str(node.get("verification_status") or "").strip(),
                _safe_float(node.get("progress")),
                str(node.get("current_step") or ""),
                str(node.get("current_tool") or ""),
                tuple(str(item) for item in _list(node.get("artifact_refs"))),
                tuple(str(item) for item in _list(node.get("declared_output_refs"))),
                str(node.get("latest_summary") or ""),
                str(node.get("last_progress_summary") or ""),
            )
        )
    return tuple(sorted(rows))


def _node_ids_with_status(nodes: list[object], statuses: set[str]) -> list[str]:
    ids: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        status = str(node.get("status") or "").strip()
        run_id = str(node.get("run_id") or "")
        if run_id and status in statuses:
            ids.append(run_id)
    return ids


def _poll_inactive_statuses() -> frozenset[str]:
    return frozenset({
        TaskStatus.DONE.value,
        *SUBAGENT_RESOLVED_TERMINAL_STATUSES,
        *SUBAGENT_FAILED_RESULT_STATUSES,
    })


def _unfinished_run_ids(nodes: list[object]) -> list[str]:
    return [
        str(node.get("run_id") or "")
        for node in nodes
        if isinstance(node, dict)
        if str(node.get("run_id") or "")
        if not task_status_in(node.get("status"), _poll_inactive_statuses())
    ]


def _safe_float(value: object) -> float:
    try:
        return round(float(value or 0.0), 6)
    except (TypeError, ValueError):
        return 0.0


def _list(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def _remember_payload(agent: SimpleAgent, params: dict[str, object], payload: dict[str, object]) -> None:
    cache = getattr(agent, "_inspect_agent_tree_recent_cache", None)
    if not isinstance(cache, dict):
        cache = {}
        agent._inspect_agent_tree_recent_cache = cache
    cache[_cache_key(params)] = {
        "created_at": time.time(),
        "payload": payload,
        "tree_state_fingerprint": _tree_state_fingerprint(agent),
    }


def _cache_key(params: dict[str, object]) -> str:
    normalized = {
        key: params.get(key)
        for key in ("root_id", "run_id", "scope", "visible_run_ids")
        if key in params
    }
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, default=str)


def _cooldown_seconds(agent: SimpleAgent, params: dict[str, object]) -> float:
    raw = params.get("cooldown_seconds", _configured_watch_interval(agent))
    try:
        value = float(raw or 0) if isinstance(raw, int | float | str) else 120.0
    except (TypeError, ValueError):
        value = 120.0
    if value <= 0:
        return 0.0
    return max(60.0, min(value, 7200.0))


def _configured_watch_interval(agent: SimpleAgent) -> object:
    config = getattr(agent, "config", None)
    return getattr(config, "subagent_watch_interval_seconds", 120)


def _tree_state_fingerprint(agent: SimpleAgent) -> tuple[tuple[str, int, int], ...]:
    manager = getattr(agent, "subagents", None)
    workspace = getattr(manager, "workspace", None)
    if workspace is None:
        return ()
    try:
        task_files = sorted(workspace.glob("*/task.json"))
    except OSError:
        return ()
    rows: list[tuple[str, int, int]] = []
    for path in task_files:
        try:
            stat = path.stat()
        except OSError:
            return ()
        rows.append((path.parent.name, int(stat.st_mtime_ns), int(stat.st_size)))
    return tuple(rows)
