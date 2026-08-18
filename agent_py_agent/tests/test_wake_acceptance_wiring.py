"""#233-3/4 acceptance 生产接线测试。

覆盖：
- repo.pending_dispatch_for_scope：owner/task/run 精确过滤、lease 未过期、
  只认 claimed+dispatched（非 pending/handed_off）、无 pending → None。
- _accept_wake_dispatch_for_run：仅 source=background_main_agent 的后台 run
  建 attempt 后按 pending dispatch 幂等 accept → intent claimed→handed_off +
  dispatch→accepted；非后台 source / 无 pending dispatch → noop 不碰状态机；
  accept 失败 fail-silent 不抛。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.agent_core.runtime_mixin import (
    _accept_wake_dispatch_for_run,
    _bind_main_agent_authority,
)
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository

NOW = 1_700_000_000.0


@pytest.fixture
def repo(tmp_path):
    return RuntimeRepository(tmp_path / "home" / "runtime.db")


def _real_task(repo):
    task = repo.create_task(owner_id="local/main", title="wiring-test")
    return task["task_id"]


def _real_attempt_for_task(repo, task_id, *, run_id=""):
    """创建与 intent.run_id 对齐的 canonical attempt（accept 归属链要求
    intent.run_id 非空时 attempt 的 agent_run.run_id 必须匹配）。"""
    task_run = repo.create_task_run(task_id=task_id)
    agent_run = repo.create_agent_run(
        task_run_id=task_run["task_run_id"], role="main", run_id=run_id
    )
    attempt = repo.create_attempt(agent_run["agent_run_id"])
    return str(attempt["attempt_id"])


def _registered_intent(repo, task_id, *, owner_id="local/main", run_id="",
                       intent_id="wiring-intent", dedup_key="wk", now=NOW):
    return repo.create_wake_intent(
        intent_id=intent_id, dedup_key=dedup_key, owner_id=owner_id,
        source="cron", wake_reason="cron_due", continuation_policy="async",
        policy_generation=1, provider_scope_ref="opencode",
        task_id=task_id, run_id=run_id, next_wake_at=now - 1, now=now,
    )


def _claim(repo, intent_id="wiring-intent", owner="gw-1", *, now=NOW):
    return repo.claim_and_record_wake_dispatch(
        intent_id, lease_owner=owner, lease_seconds=300,
        claim_token=f"{owner}:t{int(now * 1000)}",
        dispatch_event_id=f"ev-{intent_id}-{int(now * 1000)}",
        handoff_id=f"hd-{intent_id}-{int(now * 1000)}",
        now=now,
    )


def _params(*, source="background_main_agent", run_id="", task_id=""):
    return SimpleNamespace(source=source, run_id=run_id, task_id=task_id)


# ------------------------------------------------- pending_dispatch_for_scope
def test_pending_dispatch_hit_returns_dispatch_and_handoff(repo):
    task_id = _real_task(repo)
    _registered_intent(repo, task_id)
    c = _claim(repo)
    pending = repo.pending_dispatch_for_scope("local/main", task_id, now=NOW)
    assert pending is not None
    assert pending["dispatch_id"] == c["dispatch_id"]
    assert pending["handoff_id"] == c["handoff_id"]


def test_pending_dispatch_none_when_no_claim(repo):
    task_id = _real_task(repo)
    _registered_intent(repo, task_id)  # pending，未 claim
    assert repo.pending_dispatch_for_scope("local/main", task_id, now=NOW) is None


def test_pending_dispatch_none_when_already_handed_off(repo):
    task_id = _real_task(repo)
    _registered_intent(repo, task_id)
    c = _claim(repo)
    aid = _real_attempt_for_task(repo, task_id)
    acc = repo.accept_wake_dispatch(
        c["dispatch_id"], handoff_id=c["handoff_id"], attempt_id=aid, now=NOW + 1
    )
    assert acc["accepted"] is True
    assert repo.pending_dispatch_for_scope("local/main", task_id, now=NOW + 2) is None


def test_pending_dispatch_owner_mismatch_excluded(repo):
    task_id = _real_task(repo)
    _registered_intent(repo, task_id)
    _claim(repo)
    assert repo.pending_dispatch_for_scope("other/owner", task_id, now=NOW) is None


def test_pending_dispatch_lease_expired_excluded(repo):
    task_id = _real_task(repo)
    _registered_intent(repo, task_id)
    _claim(repo)
    assert repo.pending_dispatch_for_scope("local/main", task_id, now=NOW + 9999) is None


def test_pending_dispatch_run_id_filter(repo):
    task_id = _real_task(repo)
    _registered_intent(repo, task_id, run_id="bg-run-1")
    _claim(repo)
    # run_id 精确匹配 → 命中；不匹配 → None
    assert repo.pending_dispatch_for_scope(
        "local/main", task_id, run_id="bg-run-1", now=NOW
    ) is not None
    assert repo.pending_dispatch_for_scope(
        "local/main", task_id, run_id="other-run", now=NOW
    ) is None


# ------------------------------------------- _accept_wake_dispatch_for_run
def test_background_run_accepts_pending_dispatch(repo):
    task_id = _real_task(repo)
    _registered_intent(repo, task_id, run_id="bg-run-1")
    c = _claim(repo)
    aid = _real_attempt_for_task(repo, task_id, run_id="bg-run-1")
    _accept_wake_dispatch_for_run(
        repo,
        _params(run_id="bg-run-1", task_id=task_id),
        owner_id="local/main", task_id=task_id, attempt_id=aid,
        now=NOW + 1,
    )
    intent = repo.get_wake_intent("wiring-intent")
    assert intent["status"] == "handed_off"
    assert intent["active_attempt_id"] == aid
    d = repo.get_wake_dispatch(c["dispatch_id"])
    assert d["status"] == "accepted" and d["attempt_id"] == aid


def test_non_background_source_noop(repo):
    """用户手动 run（source=gateway）即使同 task 有 pending dispatch 也不 accept。"""
    task_id = _real_task(repo)
    _registered_intent(repo, task_id, run_id="bg-run-1")
    _claim(repo)
    aid = _real_attempt_for_task(repo, task_id)
    _accept_wake_dispatch_for_run(
        repo,
        _params(source="gateway", run_id="bg-run-1", task_id=task_id),
        owner_id="local/main", task_id=task_id, attempt_id=aid,
    )
    assert repo.get_wake_intent("wiring-intent")["status"] == "claimed"


def test_no_pending_dispatch_noop(repo):
    task_id = _real_task(repo)
    aid = _real_attempt_for_task(repo, task_id)
    _accept_wake_dispatch_for_run(
        repo,
        _params(run_id="bg-run-1", task_id=task_id),
        owner_id="local/main", task_id=task_id, attempt_id=aid,
    )
    # 无 intent → 状态机无扰动（查询返回 None，不抛）
    assert repo.get_wake_intent("wiring-intent") is None


def test_accept_rejection_fail_silent(repo):
    """attempt 归属不匹配（另一个 task 的 attempt）→ accept 拒绝，不抛异常。"""
    task_id = _real_task(repo)
    _registered_intent(repo, task_id, run_id="bg-run-1")
    _claim(repo)
    other_task_id = _real_task(repo)
    foreign_aid = _real_attempt_for_task(repo, other_task_id)
    _accept_wake_dispatch_for_run(
        repo,
        _params(run_id="bg-run-1", task_id=task_id),
        owner_id="local/main", task_id=task_id, attempt_id=foreign_aid,
    )
    assert repo.get_wake_intent("wiring-intent")["status"] == "claimed"


# ------------------------------ 端到端：_bind_main_agent_authority 接线
def _scoped_agent(repo):
    from types import SimpleNamespace

    return SimpleNamespace(
        subagents=SimpleNamespace(runtime_db=repo),
        home_paths=SimpleNamespace(owner_id="local/main"),
    )


def test_bind_main_agent_authority_background_run_accepts_dispatch(repo):
    """dispatcher claim 后，owner scheduler 后台 run 经 _bind_main_agent_authority
    建 attempt → 自动 accept → intent handed_off（#233-3/4 生产接线闭环）。

    用真实时钟（claim 与 accept 同源），模拟生产时序——_bind_main_agent_authority
    不持注入时钟，accept 走 repo 实时时钟，claim 也须在实时时钟下做。"""
    import time as _time

    now = _time.time()
    task_id = _real_task(repo)
    _registered_intent(repo, task_id, run_id="bg-run-1", now=now)
    c = _claim(repo, now=now)
    agent = _scoped_agent(repo)
    params = RunParams(
        source="background_main_agent",
        run_id="bg-run-1",
        task_id=task_id,
        root_user_prompt="canary",
        task_attributes={"conversation_task_id": task_id},
    )
    bound = _bind_main_agent_authority(agent, params)
    assert bound.attempt_id  # attempt 已建
    intent = repo.get_wake_intent("wiring-intent")
    assert intent["status"] == "handed_off", f"后台 run 应自动 accept: {intent['status']}"
    assert intent["active_attempt_id"] == bound.attempt_id
    d = repo.get_wake_dispatch(c["dispatch_id"])
    assert d["status"] == "accepted" and d["attempt_id"] == bound.attempt_id


def test_bind_main_agent_authority_manual_run_does_not_accept(repo):
    """用户手动 run（source=gateway）同 task 有 pending dispatch → 不 accept，
    intent 保持 claimed（手动执行不是 wake intent 的执行席接收）。"""
    import time as _time

    now = _time.time()
    task_id = _real_task(repo)
    _registered_intent(repo, task_id, run_id="bg-run-1", now=now)
    _claim(repo, now=now)
    agent = _scoped_agent(repo)
    params = RunParams(
        source="gateway",
        run_id="bg-run-1",
        task_id=task_id,
        root_user_prompt="manual",
        task_attributes={"conversation_task_id": task_id},
    )
    bound = _bind_main_agent_authority(agent, params)
    assert bound.attempt_id  # attempt 照常建
    assert repo.get_wake_intent("wiring-intent")["status"] == "claimed"
