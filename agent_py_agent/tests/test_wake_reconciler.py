"""wake_reconciler 测试（#233 第 5 步：审计+补 intent，零执行）。

覆盖：补写未完成有 policy 任务；同窗幂等；零模型/零 attempt；无 policy 无 run→
ORPHANED；无 policy 有 run→RECONCILIATION_REQUIRED；source 不在 allowed→拒；
interactive-only 不补写；limit 上限；单任务异常不阻断。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.wake_producer import ensure_wake_policy
from agent_py_agent.agent.wake_reconciler import reconcile_missing_wake_intents

NOW = 1_700_000_000.0
OWNER = "local/main"


@pytest.fixture
def home(tmp_path):
    """构造 owner_home 磁盘布局（link + ledger），供 unfinished_task_ids 判活。"""
    return tmp_path / "home"


def _write_link(home: Path, task_id: str, status: str = "active") -> None:
    link_dir = (
        home / "workspace" / "runtime" / "workspaces" / "ws-1"
        / "conversations" / "tasks"
    )
    link_dir.mkdir(parents=True, exist_ok=True)
    (link_dir / f"{task_id}.json").write_text(
        json.dumps({"task_id": task_id, "status": status}), encoding="utf-8"
    )


def _write_task(home: Path, day: str, name: str, task_id: str, status: str = "RUNNING") -> None:
    work_dir = home / "tasks" / day / name / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "state.json").write_text(
        json.dumps({"task_id": task_id, "status": status}), encoding="utf-8"
    )


def _repo_for(home: Path) -> RuntimeRepository:
    return RuntimeRepository(home / "runtime.db")


def _real_task(repo: RuntimeRepository, task_id: str):
    """建 runtime.db tasks 行（owner 匹配）。"""
    return repo.create_task(
        owner_id=OWNER, task_id=task_id, conversation_task_id=task_id, title="t"
    )


def _make_unfinished(home: Path, repo: RuntimeRepository, task_id: str) -> None:
    """让 task 进 unfinished_task_ids（link active + ledger RUNNING + runtime 有 run）。"""
    _write_link(home, task_id, "active")
    _write_task(home, "2026-08-17", task_id, task_id, "RUNNING")
    _real_task(repo, task_id)
    # 权威 run（role=main）→ 非终态 → 需驱动
    repo.record_run_creation(
        owner_id=OWNER, conversation_task_id=task_id, run_id=task_id, role="main"
    )


def _register_async_policy(repo: RuntimeRepository, *, allowed_sources: str = "cron") -> None:
    ensure_wake_policy(
        repo, owner_id=OWNER, continuation_policy="async", policy_generation=1,
        allowed_sources=allowed_sources, provider_scope_ref="opencode",
        scope_ref=OWNER, now=NOW,
    )


# ------------------------------------------------------- 补写路径
def test_backfills_missing_intent_for_unfinished_task_with_policy(home, tmp_path):
    repo = _repo_for(home)
    _register_async_policy(repo)
    task_id = "task-bfill"
    _make_unfinished(home, repo, task_id)
    counts = reconcile_missing_wake_intents(
        repo, owner_id=OWNER, owner_home=home, now=NOW
    )
    assert counts["backfilled"] == 1
    intent = repo.wake_intent_for_task(OWNER, task_id)
    assert intent is not None
    assert intent["status"] == "pending"
    assert intent["source"] == "cron"
    assert intent["continuation_policy"] == "async"


def test_idempotent_same_window(home, tmp_path):
    repo = _repo_for(home)
    _register_async_policy(repo)
    task_id = "task-idem"
    _make_unfinished(home, repo, task_id)
    c1 = reconcile_missing_wake_intents(repo, owner_id=OWNER, owner_home=home, now=NOW)
    c2 = reconcile_missing_wake_intents(repo, owner_id=OWNER, owner_home=home, now=NOW)
    assert c1["backfilled"] == 1
    assert c2["already_present"] == 1
    assert c2["backfilled"] == 0
    # 无重复行（dedup_key unique）
    with repo._runtime_connection() as conn:
        n = conn.execute(
            "SELECT count(*) FROM wake_intents WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
    assert n == 1


def test_never_calls_model_or_creates_attempt(home, tmp_path):
    """零执行硬门：reconciler 只写 intent，不建 attempt/不调模型/不 claim。

    _make_unfinished 已建 1 条权威 run attempt（fixture 前置）；reconciler 跑完后
    该计数必须不变（reconciler 不新增 attempt），且 wake_dispatches 恒 0（无 claim）。
    """
    repo = _repo_for(home)
    _register_async_policy(repo)
    task_id = "task-zeroexec"
    _make_unfinished(home, repo, task_id)
    with repo._runtime_connection() as conn:
        attempts_before = conn.execute("SELECT count(*) FROM agent_attempts").fetchone()[0]
    counts = reconcile_missing_wake_intents(repo, owner_id=OWNER, owner_home=home, now=NOW)
    assert counts["backfilled"] == 1
    # intent 已补写但保持 pending（从未 claim → 从未建 attempt/调模型）
    intent = repo.wake_intent_for_task(OWNER, task_id)
    assert intent is not None and intent["status"] == "pending"
    with repo._runtime_connection() as conn:
        attempts_after = conn.execute("SELECT count(*) FROM agent_attempts").fetchone()[0]
        dispatches = conn.execute("SELECT count(*) FROM wake_dispatches").fetchone()[0]
    assert attempts_after == attempts_before  # reconciler 不新增 attempt
    assert dispatches == 0  # 无 claim → 无 dispatch/无 provider 副作用
    # 模块不得 import dispatcher（源码断言）
    import inspect
    import agent_py_agent.agent.wake_reconciler as wr_mod
    src = inspect.getsource(wr_mod)
    assert "wake_dispatcher" not in src  # reconciler 零执行：不 import dispatcher


# ------------------------------------------------------- 标记路径
def test_marks_orphaned_when_no_policy_no_run(home, tmp_path):
    """无 policy + 无权威 run → ORPHANED 台账行 + 不补写。"""
    repo = _repo_for(home)
    task_id = "task-orphan"
    _write_link(home, task_id, "active")
    _write_task(home, "2026-08-17", task_id, task_id, "RUNNING")
    _real_task(repo, task_id)  # 无 run
    counts = reconcile_missing_wake_intents(repo, owner_id=OWNER, owner_home=home, now=NOW)
    assert counts["orphaned"] == 1
    assert repo.wake_intent_for_task(OWNER, task_id) is None
    migration = repo.get_legacy_migration(task_id)
    assert migration is not None
    assert migration["isolation_state"] == "ORPHANED"
    assert migration["reason"] == "legacy_unbound_no_run_no_policy"


def test_marks_reconciliation_required_when_no_policy_but_run(home, tmp_path):
    """无 policy 但有权威 run → RECONCILIATION_REQUIRED。"""
    repo = _repo_for(home)
    task_id = "task-recreq"
    _make_unfinished(home, repo, task_id)  # 有 run（record_run_creation）
    counts = reconcile_missing_wake_intents(repo, owner_id=OWNER, owner_home=home, now=NOW)
    assert counts["reconciliation_required"] == 1
    migration = repo.get_legacy_migration(task_id)
    assert migration is not None
    assert migration["isolation_state"] == "RECONCILIATION_REQUIRED"


def test_skips_when_source_not_allowed(home, tmp_path):
    """policy 存在但 allowed_sources 与 backfill 优先级无交集 → 拒补写。"""
    repo = _repo_for(home)
    _register_async_policy(repo, allowed_sources="sleep")  # 不在 (heartbeat,cron,interrupted_run_recovery)
    task_id = "task-srcgap"
    _make_unfinished(home, repo, task_id)
    counts = reconcile_missing_wake_intents(repo, owner_id=OWNER, owner_home=home, now=NOW)
    assert counts["reconciliation_required"] == 1
    assert repo.wake_intent_for_task(OWNER, task_id) is None


def test_never_backfills_interactive_only(home, tmp_path):
    """interactive-only → 不补写（事件驱动禁周期扫描）。"""
    repo = _repo_for(home)
    ensure_wake_policy(
        repo, owner_id=OWNER, continuation_policy="interactive", policy_generation=1,
        allowed_sources="inbound", provider_scope_ref="opencode",
        scope_ref=OWNER, now=NOW,
    )
    task_id = "task-intonly"
    _make_unfinished(home, repo, task_id)
    counts = reconcile_missing_wake_intents(repo, owner_id=OWNER, owner_home=home, now=NOW)
    assert counts["backfilled"] == 0
    assert repo.wake_intent_for_task(OWNER, task_id) is None


def test_respects_limit(home, tmp_path):
    repo = _repo_for(home)
    _register_async_policy(repo)
    task_ids = [f"task-limit-{i}" for i in range(3)]
    for task_id in task_ids:
        _make_unfinished(home, repo, task_id)
    counts = reconcile_missing_wake_intents(repo, owner_id=OWNER, owner_home=home, now=NOW, limit=2)
    assert counts["backfilled"] == 2
    remaining = [t for t in task_ids if repo.wake_intent_for_task(OWNER, t) is None]
    assert len(remaining) == 1


def test_errors_isolated(home, tmp_path, monkeypatch):
    """单任务 policy 查询抛异常不阻断整批。"""
    repo = _repo_for(home)
    _register_async_policy(repo)
    good = "task-good"
    bad = "task-bad"
    _make_unfinished(home, repo, good)
    _make_unfinished(home, repo, bad)

    # 只对 bad 任务 poison（good 走正常路径）：按 task_id 判，不误伤 good。
    import agent_py_agent.agent.wake_reconciler as wr

    _orig_reconcile_one = wr._reconcile_one

    def _poison(repo_, *, owner_id, task_id, now, counts):
        if task_id == bad:
            raise RuntimeError("poisoned")
        return _orig_reconcile_one(repo_, owner_id=owner_id, task_id=task_id, now=now, counts=counts)

    monkeypatch.setattr(wr, "_reconcile_one", _poison)

    counts = reconcile_missing_wake_intents(repo, owner_id=OWNER, owner_home=home, now=NOW)
    # bad 异常被捕获为 errors；good 正常补写
    assert counts["errors"] == 1
    assert repo.wake_intent_for_task(OWNER, good) is not None
