"""任务2 合同单测：显式 run_id 落在当前 owner 可见范围之外时，list_agents 必须给出结构化范围裁决，
且不泄露目标 run 是否存在（"不存在"与"无权看"给同一个答复）。

来源：Claude 会话 dsh-9b 的 R16 跨 owner 隔离验收随附发现（2026-09-28）。
对照 AGENTS.md 架构铁律：显式身份参数与当前 runner 上下文冲突时不能静默猜，
工具响应必须暴露 scope_resolution/scope_warnings 这类结构化裁决事实。

复现方法：
    cd <worktree> && PYTHONPATH=. python3 -m pytest agent_py_agent/tests/test_list_agents_scope_resolution.py -q

变异验证：把 agent_tree/status.py 里 `_resolve_unmatched_run_query` 的接入去掉（或让它恒返回 None），
本文件 3 个用例应重新变红。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_py_agent.agent.agent_core.orchestration.tools.list_agents import ListAgentsTool
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents.services.base import CreateRunParams

# 与 agent_tree/status.py 中新增常量保持一致；这里是模型可见的裁决码，不是自然语言说明。
UNMATCHED_CODE = "requested_run_id_not_in_visible_scope"


def _agent(root: Path, home: Path) -> SimpleAgent:
    return SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(home)), root)


def _query(agent: SimpleAgent, params: dict[str, object]) -> dict[str, object]:
    result = ListAgentsTool(agent).execute(params)
    assert result.ok is True
    return json.loads(result.output)


@pytest.fixture()
def two_owners(tmp_path: Path) -> tuple[SimpleAgent, SimpleAgent, str]:
    """A/B 两个互不相干的 owner；返回 (A, B, A 的子代理 run_id)。"""
    agent_a = _agent(tmp_path / "work_a", tmp_path / "home_a")
    agent_b = _agent(tmp_path / "work_b", tmp_path / "home_b")
    child = agent_a.subagents.create_run(
        params=CreateRunParams(goal="A 的子代理", thought="", plan=[], role="researcher")
    )
    return agent_a, agent_b, child.id


def test_same_owner_explicit_run_id_stays_visible_without_warning(two_owners):
    agent_a, _agent_b, child_run_id = two_owners

    payload = _query(agent_a, {"run_id": child_run_id})

    assert child_run_id in {node["run_id"] for node in payload["nodes"]}
    assert payload["root_id"] == child_run_id
    resolution = payload["scope_resolution"]
    assert resolution["effective"]["run_id"] == child_run_id
    assert UNMATCHED_CODE not in (resolution.get("warnings") or [])
    assert not payload.get("scope_warnings")


def test_cross_owner_run_id_returns_empty_with_structured_resolution(two_owners):
    _agent_a, agent_b, child_run_id = two_owners

    payload = _query(agent_b, {"run_id": child_run_id})

    assert payload["nodes"] == []
    assert payload["root_id"] == ""
    resolution = payload["scope_resolution"]
    # 请求的 id 必须如实记录在 explicit 里，供调用方看见自己传了什么。
    assert resolution["explicit"]["run_id"] == child_run_id
    # 实际生效的范围不能再回显这个不可见的 run_id（否则等于告诉调用方"这个 id 是有效范围"）。
    assert resolution["effective"].get("run_id") != child_run_id
    assert UNMATCHED_CODE in (resolution.get("warnings") or [])
    assert UNMATCHED_CODE in (payload.get("scope_warnings") or [])


def test_missing_run_id_returns_identical_verdict_without_leaking_existence(two_owners):
    _agent_a, agent_b, child_run_id = two_owners

    cross_owner = _query(agent_b, {"run_id": child_run_id})
    missing = _query(agent_b, {"run_id": "subagent-does-not-exist-00000000"})

    # "不存在"和"无权看"必须是同一个答复形状：节点、根、生效范围、告警码全部一致。
    assert missing["nodes"] == cross_owner["nodes"] == []
    assert missing["root_id"] == cross_owner["root_id"] == ""
    assert (
        missing["scope_resolution"]["effective"].get("run_id")
        == cross_owner["scope_resolution"]["effective"].get("run_id")
        is None
    )
    assert missing["scope_resolution"]["warnings"] == cross_owner["scope_resolution"]["warnings"]
    assert missing["scope_warnings"] == cross_owner["scope_warnings"]
    # 两种情况下都不能出现任何指向目标 run 的可见内容。
    assert child_run_id not in json.dumps(missing, ensure_ascii=False)
