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

from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import runtime_mixin
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


def test_legacy_thread_run_from_old_task_cannot_hijack_new_task(repo):
    """旧版线程级后台 run 已属于任务 A 时，任务 B 的续跑必须挂到任务 B 主链。"""
    legacy_run_id = "bg-main-thread-shared"
    old_task_id = "task-old"
    new_task_id = "task-new"
    old = repo.record_run_creation(
        owner_id="local/main",
        goal="旧任务",
        conversation_task_id=old_task_id,
        run_id=legacy_run_id,
        role="main",
    )
    fresh = repo.record_run_creation(
        owner_id="local/main",
        goal="新任务",
        conversation_task_id=new_task_id,
        run_id=new_task_id,
        role="main",
    )

    bound = _bind_main_agent_authority(
        _agent(repo),
        RunParams(
            run_id=legacy_run_id,
            task_id=new_task_id,
            root_user_prompt="继续新任务",
        ),
    )

    assert bound.attempt_id not in {old["attempt_id"], fresh["attempt_id"]}
    new_rows = _main_run_rows(repo, new_task_id)
    assert len(new_rows) == 1
    assert new_rows[0]["agent_run_id"] == fresh["agent_run_id"]
    assert new_rows[0]["current_attempt_id"] == bound.attempt_id
    assert repo.get_agent_run(old["agent_run_id"])["current_attempt_id"] == old["attempt_id"]


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


def test_gateway_transport_attempt_is_replaced_before_root_closeout(repo, monkeypatch):
    """Gateway 外层 attempt 不能覆盖 RuntimeDB 权威 attempt；正常返回必须关闭后者。"""
    task_id = "gwreq-authority-closeout"
    agent = _agent(repo)
    observed: dict[str, str] = {}

    monkeypatch.setattr(
        runtime_mixin,
        "bind_cli_run_conversation",
        lambda _agent, params, _prompt: params,
    )
    monkeypatch.setattr(
        runtime_mixin,
        "attach_run_task_workspace_context",
        lambda _agent, params, _prompt: params,
    )

    def fake_run_once(_agent, _prompt, params):
        observed["attempt_id"] = params.attempt_id
        return SimpleNamespace(
            runtime_status="ok",
            runtime_reason="",
            runtime_source="gateway",
            tool_rounds=1,
            response="已启动子代理。",
        )

    monkeypatch.setattr(runtime_mixin, "_run_once_with_params", fake_run_once)
    monkeypatch.setattr(
        runtime_mixin,
        "compact_auto_continuation_decision",
        lambda _result, depth: SimpleNamespace(should_continue=False),
    )
    monkeypatch.setattr(runtime_mixin, "finish_run_task_workspace_if_needed", lambda *_: None)
    monkeypatch.setattr(runtime_mixin, "persist_cli_run_assistant", lambda *_: None)

    result = runtime_mixin._run_with_params(
        agent,
        "只负责盯着多个子代理完成任务。",
        RunParams(
            request_id=task_id,
            run_id=task_id,
            task_id=task_id,
            attempt_id="gateway-attempt-transport-only",
            root_user_prompt="只负责盯着多个子代理完成任务。",
            source="gateway",
        ),
    )

    run = repo.main_agent_run_for_task(task_id)
    assert run is not None
    authoritative_attempt_id = str(run["current_attempt_id"])
    assert observed["attempt_id"] == authoritative_attempt_id
    assert authoritative_attempt_id != "gateway-attempt-transport-only"
    assert str(run["status"]) == "done"
    attempt = repo.current_attempt(str(run["agent_run_id"]))
    assert attempt is not None
    assert str(attempt["status"]) == "done"
    assert float(attempt["ended_at"] or 0) > 0
    assert result.runtime_status == "ok"

    with repo.transaction() as conn:
        blocked = conn.execute(
            "SELECT COUNT(*) AS count FROM runtime_events "
            "WHERE agent_run_id = ? AND event_type = 'closeout_blocked'",
            (str(run["agent_run_id"]),),
        ).fetchone()
    assert int(blocked["count"] or 0) == 0


def test_compact_continuation_closeout_uses_latest_authoritative_attempt(repo, monkeypatch):
    """Compact 续接换代后，最终收口必须使用新 generation，而非入口 transport id。"""
    task_id = "gwreq-compact-authority-closeout"
    agent = _agent(repo)
    observed_attempts: list[str] = []

    monkeypatch.setattr(
        runtime_mixin,
        "bind_cli_run_conversation",
        lambda _agent, params, _prompt: params,
    )
    monkeypatch.setattr(
        runtime_mixin,
        "attach_run_task_workspace_context",
        lambda _agent, params, _prompt: params,
    )

    def fake_run_once(_agent, _prompt, params):
        observed_attempts.append(params.attempt_id)
        status = "context_overflow" if len(observed_attempts) == 1 else "ok"
        return SimpleNamespace(
            runtime_status=status,
            runtime_reason="",
            runtime_source="gateway",
            tool_rounds=len(observed_attempts),
            response="继续" if status == "context_overflow" else "本轮结束。",
        )

    monkeypatch.setattr(runtime_mixin, "_run_once_with_params", fake_run_once)
    monkeypatch.setattr(
        runtime_mixin,
        "compact_auto_continuation_decision",
        lambda result, depth: SimpleNamespace(
            should_continue=result.runtime_status == "context_overflow",
            injection="compact checkpoint",
            user_prompt="继续上一轮",
        ),
    )
    monkeypatch.setattr(
        runtime_mixin,
        "release_active_turn_inputs_for_compact",
        lambda *_: (),
    )
    monkeypatch.setattr(
        runtime_mixin,
        "_compact_auto_continue_params",
        lambda params, _injection, _result, released_active_turn_input_ids=(): replace(
            params,
            attempt_id="gateway-attempt-compact-transport",
            continuation_seq=1,
        ),
    )
    monkeypatch.setattr(
        runtime_mixin,
        "mark_compact_auto_continued",
        lambda continued, _previous, depth: continued,
    )
    monkeypatch.setattr(runtime_mixin, "finish_run_task_workspace_if_needed", lambda *_: None)
    monkeypatch.setattr(runtime_mixin, "persist_cli_run_assistant", lambda *_: None)

    result = runtime_mixin._run_with_params(
        agent,
        "执行长任务。",
        RunParams(
            request_id=task_id,
            run_id=task_id,
            task_id=task_id,
            attempt_id="gateway-attempt-initial-transport",
            root_user_prompt="执行长任务。",
            source="gateway",
        ),
    )

    run = repo.main_agent_run_for_task(task_id)
    assert run is not None
    attempts = repo.attempts_for_run(str(run["agent_run_id"]))
    assert len(observed_attempts) == 2
    assert len(attempts) == 2
    assert observed_attempts[0] == str(attempts[0]["attempt_id"])
    assert observed_attempts[1] == str(attempts[1]["attempt_id"])
    assert observed_attempts[0] != observed_attempts[1]
    assert str(run["current_attempt_id"]) == observed_attempts[1]
    assert str(run["status"]) == "done"
    assert str(attempts[1]["status"]) == "done"
    assert result.runtime_status == "ok"
