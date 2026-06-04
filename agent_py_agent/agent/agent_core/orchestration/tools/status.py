
from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from ....tooling.models import BaseTool, ToolExecutionResult
from ...agent_tree.status import agent_tree_status_payload
from ..tool_specs import (
    build_inspect_agent_tree_spec,
)

if TYPE_CHECKING:
    from ....core import SimpleAgent


class InspectAgentTreeTool(BaseTool):
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_inspect_agent_tree_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        cached = _cached_payload(self.agent, params)
        if cached is not None:
            return ToolExecutionResult(
                "inspect_agent_tree",
                True,
                json.dumps(cached, ensure_ascii=False, indent=2),
            )
        payload = agent_tree_status_payload(self.agent, params)
        _remember_payload(self.agent, params, payload)
        return ToolExecutionResult(
            "inspect_agent_tree",
            True,
            json.dumps(payload, ensure_ascii=False, indent=2),
        )


def _cached_payload(agent: SimpleAgent, params: dict[str, object]) -> dict[str, object] | None:
    key = _cache_key(params)
    cache = getattr(agent, "_inspect_agent_tree_recent_cache", None)
    if not isinstance(cache, dict):
        return None
    row = cache.get(key)
    if not isinstance(row, dict):
        return None
    cooldown_seconds = _cooldown_seconds(params)
    age = time.time() - float(row.get("created_at", 0.0) or 0.0)
    if age < 0 or age > cooldown_seconds:
        return None
    payload = _cooldown_payload(row.get("payload"))
    payload["cooldown_active"] = True
    payload["cooldown_seconds"] = cooldown_seconds
    payload["cooldown_age_seconds"] = round(age, 3)
    warnings = list(payload.get("warnings") if isinstance(payload.get("warnings"), list) else [])
    if "inspect_agent_tree_recent_duplicate" not in warnings:
        warnings.append("inspect_agent_tree_recent_duplicate")
    payload["warnings"] = warnings
    policy = dict(payload.get("policy") if isinstance(payload.get("policy"), dict) else {})
    wait_call = {"tool": "wait", "seconds": max(120, int(cooldown_seconds) or 0), "reason": "inspect_agent_tree cooldown"}
    policy["next_step"] = "刚刚已经查看过同一代理树；除非需要验收、接管或已有新事实，否则先推进汇总/等待子代理产物，不要高频轮询。"
    policy["suggested_tool_call"] = wait_call
    payload["policy"] = policy
    direct_children = dict(payload.get("direct_children") if isinstance(payload.get("direct_children"), dict) else {})
    direct_children["suggested_tool_call"] = wait_call
    payload["direct_children"] = direct_children
    return payload


def _cooldown_payload(value: object) -> dict[str, object]:
    previous = value if isinstance(value, dict) else {}
    direct = previous.get("direct_children") if isinstance(previous.get("direct_children"), dict) else {}
    return {
        "schema_version": previous.get("schema_version", "agent_tree_status.v1"),
        "root_id": previous.get("root_id", ""),
        "status": "POLL_COOLDOWN",
        "summary": "同一代理树刚刚已经检查过；cooldown 内不重复返回完整树，避免父代理高频轮询或误判后重复派工。",
        "direct_children": {
            "total": direct.get("total", 0),
            "by_status": direct.get("by_status", {}),
            "running_run_ids": direct.get("running_run_ids", []),
            "planning_run_ids": direct.get("planning_run_ids", []),
            "unfinished_run_ids": direct.get("unfinished_run_ids", []),
            "next_action": "wait_for_subagents_or_read_completed_refs",
            "suggested_tool_call": {"tool": "wait", "seconds": 120, "reason": "等待子代理完成事件"},
        },
    }


def _remember_payload(agent: SimpleAgent, params: dict[str, object], payload: dict[str, object]) -> None:
    cache = getattr(agent, "_inspect_agent_tree_recent_cache", None)
    if not isinstance(cache, dict):
        cache = {}
        agent._inspect_agent_tree_recent_cache = cache
    cache[_cache_key(params)] = {
        "created_at": time.time(),
        "payload": payload,
    }


def _cache_key(params: dict[str, object]) -> str:
    normalized = {
        key: params.get(key)
        for key in ("root_id", "run_id", "scope", "visible_run_ids")
        if key in params
    }
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, default=str)


def _cooldown_seconds(params: dict[str, object]) -> float:
    raw = params.get("cooldown_seconds", 30)
    try:
        value = float(raw or 0)
    except (TypeError, ValueError):
        value = 30.0
    return max(0.0, min(value, 300.0))
