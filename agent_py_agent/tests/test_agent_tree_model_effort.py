"""功能验收缺口修复：list_agents 展示子代理实际使用的模型和智能程度（TDD 红绿）。

来源：2026-09-30 功能验收发现——派出去的子代理实际用了哪个模型、哪个智能程度，
my-agent 的 list_agents 看不到（节点只有 goal_digest/status 等，无 model/effort）。
宿主记录核实：子代理线程 model_profile_id=536c11f9（deepseek-v4.1-flash）、
reasoning_effort=low，但工具回执不展示。本文件先写失败测试，再实现投影。

权威来源（按 3a 约定）：已物化的子代理线程上的 model_profile_id / reasoning_effort；
线程未物化或读取失败时回退任务属性 host_model_profile.v1 / host_reasoning_effort.v1
里冻结的编号。任何读取失败显示“未知”，不能让 list_agents 失败；输出绝不含密钥。

复现方法：
    cd <worktree> && $PY -m pytest agent_py_agent/tests/test_agent_tree_model_effort.py -q --tb=short

变异验证：把 node_rendering 里模型/档位投影的接线去掉（或恒返回空），本文件用例应重新变红；
把 model_view._NODE_FIELDS 里的 model/reasoning_effort 字段删掉同样变红。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_py_agent.agent.agent_core.orchestration.tools.list_agents import ListAgentsTool
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents.services.base import CreateRunParams

MODEL_ID = "536c11f9-f618-42a2-8fd4-b374df97dc94"
MODEL_NAME = "deepseek-v4.1-flash"


def _agent(root: Path, home: Path) -> SimpleAgent:
    return SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(home)), root)


def _create_child(agent: SimpleAgent, *, attributes: dict | None = None) -> str:
    child = agent.subagents.create_run(params=CreateRunParams(
        goal="数一下 README.md 行数", thought="", plan=[], attributes=attributes,
    ))
    return child.id


def _list_nodes(agent: SimpleAgent, run_id: str) -> list[dict]:
    result = ListAgentsTool(agent).execute({"run_id": run_id})
    assert result.ok is True
    return json.loads(result.output)["nodes"]


def test_explicit_model_and_effort_visible_in_list_agents(tmp_path: Path):
    """显式指定模型和档位的子代理：list_agents 节点显示“名称（编号）”和档位。"""
    agent = _agent(tmp_path / "work", tmp_path / "home")
    run_id = _create_child(agent, attributes={
        "host_model_profile.v1": {"profile_id": MODEL_ID},
        "host_reasoning_effort.v1": {"level": "low"},
    })
    with patch(
        "agent_py_agent.agent.agent_core.agent_tree.node_rendering._resolve_model_name",
        return_value=MODEL_NAME,
    ):
        nodes = _list_nodes(agent, run_id)
    assert len(nodes) == 1
    row = nodes[0]
    assert row["model"] == f"{MODEL_NAME}（{MODEL_ID}）"
    assert row["reasoning_effort"] == "low"


def test_unset_model_and_effort_show_inherit_defaults(tmp_path: Path):
    """未指定模型和档位的子代理：模型显示“继承会话默认”，档位显示“默认”。"""
    agent = _agent(tmp_path / "work", tmp_path / "home")
    run_id = _create_child(agent)  # 不带 attributes，走默认继承
    nodes = _list_nodes(agent, run_id)
    row = nodes[0]
    assert row["model"] == "继承会话默认"
    assert row["reasoning_effort"] == "默认"


def test_deleted_profile_shows_unknown_without_error(tmp_path: Path):
    """档案被删除（模型目录里查不到编号）：显示“未知”，list_agents 不报错。"""
    agent = _agent(tmp_path / "work", tmp_path / "home")
    run_id = _create_child(agent, attributes={
        "host_model_profile.v1": {"profile_id": "deleted-profile-id"},
    })
    nodes = _list_nodes(agent, run_id)  # _resolve_model_name 真实读空目录，返回空 → 未知
    row = nodes[0]
    assert row["model"] == "未知"
    assert row["reasoning_effort"] in {"默认", "auto"}


def test_output_contains_no_credentials(tmp_path: Path):
    """list_agents 输出里绝不含密钥、接口地址或令牌字样。"""
    agent = _agent(tmp_path / "work", tmp_path / "home")
    run_id = _create_child(agent, attributes={
        "host_model_profile.v1": {"profile_id": MODEL_ID},
        "host_reasoning_effort.v1": {"level": "max"},
    })
    with patch(
        "agent_py_agent.agent.agent_core.agent_tree.node_rendering._resolve_model_name",
        return_value=MODEL_NAME,
    ):
        result = ListAgentsTool(agent).execute({"run_id": run_id})
    assert result.ok is True
    output = json.dumps(json.loads(result.output), ensure_ascii=False)
    for secret in ("api_key", "api_base", "token", "sk-", "authorization"):
        assert secret not in output


def test_thread_load_failure_falls_back_to_frozen_task_attrs():
    """线程读取失败（损坏/未物化）时回退任务属性冻结值，不抛错、显示未知模型。"""
    from agent_py_agent.agent.agent_core.agent_tree.node_rendering import node_from_kernel_run
    from agent_py_agent.agent.subagents.kernel import SubagentKernelRun

    class BrokenThreads:
        def load(self, thread_id):
            raise OSError("线程文件损坏")

    agent = SimpleNamespace(
        conversation_store=SimpleNamespace(threads=BrokenThreads()),
        subagents=None,
    )
    row = SubagentKernelRun(
        run_id="child",
        thread_id="thread-child",
        model_profile_id=MODEL_ID,
        reasoning_effort="low",
    )
    node = node_from_kernel_run(agent, row)
    assert node["reasoning_effort"] == "low"
    assert node["model"] in {"未知", f"{MODEL_NAME}（{MODEL_ID}）"}