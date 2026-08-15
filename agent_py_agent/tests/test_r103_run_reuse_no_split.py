"""R1-03 补漏红测：主代理续跑禁止分裂 run 树。

真机根因（2026-08-12 testbox）：同一任务 req_1786515758774 出现两个
task_run + 两个 main agent_run——gateway 请求执行用 run_id=req_{id}
登记第一棵 run 树；任务 runtime 未闭合（unfinished）后发现层继续驱动，
主代理后台续跑用 run_id=bg-main-thread-{thread} 登记第二棵 run 树
（_bind_main_agent_authority 按 run_id 查不到对方 → record_run_creation
新建，绕过 create_attempt 挂载闸）→ 自喂循环以「同 task 分裂新 run」
形态继续。

修复：主代理续跑身份按 run_id 查不到登记时，回退按 task 查
role='main' root run——已登记 → create_attempt 续挂（挂载闸轮换
generation）；未登记 → 新建。子代理路径（current_subagent_run_id 非空）
与 LOCAL_UNMANAGED（repo None）不受影响。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.agent_core.runtime_mixin import _bind_main_agent_authority
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository


@pytest.fixture
def repo(tmp_path):
    return RuntimeRepository(tmp_path / "home" / "runtime.db")


def _agent(repo):
    return SimpleNamespace(
        subagents=SimpleNamespace(runtime_db=repo),
        home_paths=SimpleNamespace(owner_id="local/main"),
    )


def _main_run_rows(repo, task_id):
    return repo._runtime_connect().execute(
        "SELECT ar.* FROM agent_runs ar "
        "JOIN task_runs tr ON tr.task_run_id = ar.task_run_id "
        "WHERE tr.task_id = ? AND ar.role = 'main' AND ar.parent_agent_run_id = '' "
        "ORDER BY ar.created_at",
        (task_id,),
    ).fetchall()


def test_cross_identity_resume_does_not_split_run_tree(repo):
    """请求身份（req_{id}）登记后，主代理后台身份（bg-main-thread-{thread}）
    续跑同一 task——必须续挂原 main run，不得新建第二棵 run 树。"""
    task_id = "req_1786515758774_1022791_0"
    first = repo.record_run_creation(
        owner_id="local/main",
        goal="工具测试",
        conversation_task_id=task_id,
        run_id=task_id,  # gateway 请求身份
        role="main",
    )
    agent = _agent(repo)
    params = RunParams(
        run_id="bg-main-thread-ebc98e5f465b41ad",
        task_id=task_id,
        root_user_prompt="工具测试",
    )
    bound = _bind_main_agent_authority(agent, params)

    # 不分裂：同一 task 仍只有一棵 main run 树、一个 task_run。
    runs = _main_run_rows(repo, task_id)
    assert len(runs) == 1, f"续跑必须复用原 run，实际分裂为 {len(runs)} 棵"
    assert runs[0]["agent_run_id"] == first["agent_run_id"]
    assert len(repo.task_runs_for_task(task_id)) == 1
    # 续挂语义：返回 attempt 属于原 run，generation 递增到 2。
    assert bound.attempt_id != first["attempt_id"]
    assert bound.attempt_id  # 续挂 attempt 已就位


def test_fresh_task_creates_new_tree(repo):
    """无登记任务的首次执行 → 新建一棵 run 树（保护既有行为）。"""
    task_id = "req_1786515520749_1022459_0"
    agent = _agent(repo)
    params = RunParams(
        run_id=task_id,
        task_id=task_id,
        root_user_prompt="canary",
    )
    bound = _bind_main_agent_authority(agent, params)
    runs = _main_run_rows(repo, task_id)
    assert len(runs) == 1
    assert runs[0]["run_id"] == task_id
    assert bound.attempt_id == runs[0]["current_attempt_id"]


def test_resume_after_terminal_main_run_reuses_same_run(repo):
    """任务终态（done）后显式重跑 → 续挂同一 run（R1-03 任务级闸裁决重跑，
    create_attempt 对终态放行），不新建 run 树。"""
    task_id = "req_1786515652038_1022459_1"
    first = repo.record_run_creation(
        owner_id="local/main",
        goal="cancel 竞态",
        conversation_task_id=task_id,
        run_id=task_id,
        role="main",
    )
    repo.settle_agent_run(
        agent_run_id=first["agent_run_id"],
        status="done",
        payload={"status": "done", "runtime_status": "ok"},
    )
    agent = _agent(repo)
    params = RunParams(
        run_id="bg-main-thread-other",
        task_id=task_id,
        root_user_prompt="cancel 竞态",
    )
    bound = _bind_main_agent_authority(agent, params)
    runs = _main_run_rows(repo, task_id)
    assert len(runs) == 1, f"终态重跑必须复用原 run，实际分裂为 {len(runs)} 棵"
    assert bound.attempt_id != first["attempt_id"]
    assert len(repo.task_runs_for_task(task_id)) == 1
