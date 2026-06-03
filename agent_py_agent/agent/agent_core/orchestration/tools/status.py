
from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from ....tools import BaseTool, ToolExecutionResult
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
    payload = dict(row.get("payload") if isinstance(row.get("payload"), dict) else {})
    payload["cooldown_active"] = True
    payload["cooldown_seconds"] = cooldown_seconds
    payload["cooldown_age_seconds"] = round(age, 3)
    warnings = list(payload.get("warnings") if isinstance(payload.get("warnings"), list) else [])
    if "inspect_agent_tree_recent_duplicate" not in warnings:
        warnings.append("inspect_agent_tree_recent_duplicate")
    payload["warnings"] = warnings
    policy = dict(payload.get("policy") if isinstance(payload.get("policy"), dict) else {})
    policy["next_step"] = "刚刚已经查看过同一代理树；除非需要验收、接管或已有新事实，否则先推进汇总/等待子代理产物，不要高频轮询。"
    payload["policy"] = policy
    return payload


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
