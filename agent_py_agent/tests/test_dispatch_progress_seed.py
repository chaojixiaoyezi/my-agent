"""create_subagents 派工即种 task_progress 账本(学 终端应用 TodoWrite 的结构化落地)。

契约:每个子代理一条 in_progress 待办 + 一条整合验证收尾项;幂等(重复派同 id 不重复种);
种子永不抛错(失败绝不影响派工)。
"""

from __future__ import annotations

from types import SimpleNamespace

from agent.agent_core.orchestration.dispatch_progress_seed import seed_dispatch_task_progress
from agent.task_progress import read_task_progress


def _agent(tmp_path, run_id="run-seed-1"):
    return SimpleNamespace(home_paths=None, root=tmp_path, _main_agent_run_id=run_id)


def test_seed_uses_current_run_params_run_id_over_fallback(tmp_path):
    # canonical run_id=本run的RunParams.run_id(交付闸读的同一本),必须优先于 _main_agent_run_id 兜底。
    agent = SimpleNamespace(
        home_paths=None,
        root=tmp_path,
        _current_run_params=SimpleNamespace(run_id="req_canonical_1"),
        _main_agent_run_id="stale-fallback",
    )
    seed = seed_dispatch_task_progress(agent, [_task("subagent-aa11", "建后端")])
    assert seed["run_id"] == "req_canonical_1"
    assert read_task_progress(tmp_path, "req_canonical_1").get("items")
    assert not read_task_progress(tmp_path, "stale-fallback").get("items")


def _task(task_id, goal):
    return SimpleNamespace(id=task_id, goal=goal)


def test_seed_creates_subagent_items_and_integration_tail(tmp_path):
    seed = seed_dispatch_task_progress(
        _agent(tmp_path), [_task("subagent-aa11", "建后端API"), _task("subagent-bb22", "建前端页面")]
    )
    assert seed == {"run_id": "run-seed-1", "seeded": 3}
    progress = read_task_progress(tmp_path, "run-seed-1")
    by_id = {item["id"]: item for item in progress["items"]}
    assert by_id["subagent-aa11"]["status"] == "in_progress"
    assert "建后端API" in by_id["subagent-aa11"]["title"]
    assert by_id["integrate-and-verify"]["status"] == "pending"


def test_seed_idempotent_on_same_ids(tmp_path):
    agent = _agent(tmp_path)
    assert seed_dispatch_task_progress(agent, [_task("subagent-aa11", "建后端")])["seeded"] == 2
    assert seed_dispatch_task_progress(agent, [_task("subagent-aa11", "建后端")]) is None
    assert len(read_task_progress(tmp_path, "run-seed-1")["items"]) == 2


def test_seed_appends_new_dispatch_without_touching_existing(tmp_path):
    agent = _agent(tmp_path)
    seed_dispatch_task_progress(agent, [_task("subagent-aa11", "建后端")])
    seed = seed_dispatch_task_progress(agent, [_task("subagent-cc33", "写测试")])
    assert seed["seeded"] == 1  # 只补新子代理,整合项已存在不重复
    ids = {item["id"] for item in read_task_progress(tmp_path, "run-seed-1")["items"]}
    assert ids == {"subagent-aa11", "subagent-cc33", "integrate-and-verify"}


def test_seed_never_raises_on_broken_agent():
    assert seed_dispatch_task_progress(SimpleNamespace(home_paths=None, root=None), [_task("x", "y")]) is None
