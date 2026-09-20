"""模块用途: 调度器 tick 层 wake_queue 闹钟字条的新契约测试(调度改造 2a/2b)。

旧契约(本文件前身 test_task_ledger_resume_wake.py)钉住"账本 RUNNING 任务 →
立即落 task_ledger_resume wake", 已被 2026-08-17 owner 拍板的 wake_queue
设计取代: 热层只弹到期字条(索引查询, 不翻任务目录); 三源对账降到 5min
且只给 active-goal 任务补 goal_tick 字条(EXEC-39: 普通任务不自动醒, 除非
模型自己 clock.sleep); audit 任务另有观察/audit 唤醒通道, 对账跳过防双重
拉起。本测试钉住新契约: 到期字条 → wake_queue_due 唤醒链、对账分频、
EXEC-39 门、audit 通道跳过与终态清字条。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.conversation.runtime import _BackgroundSchedulerTickMixin
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings.config import AgentConfig


def _agent(tmp_path: Path) -> SimpleAgent:
    return SimpleAgent(
        AgentConfig(
            model_backend="echo",
            enable_tools=False,
            my_agent_home=str(tmp_path / "home"),
        ),
        tmp_path,
    )


def _write_task(owner_home: Path, day: str, name: str, task_id: str, status: str) -> None:
    work = owner_home / "tasks" / day / name / "work"
    work.mkdir(parents=True, exist_ok=True)
    (work / "state.json").write_text(
        json.dumps({"task_id": task_id, "status": status}),
        encoding="utf-8",
    )


def _call_enqueue(agent: SimpleAgent, *, now: float = 100.0) -> None:
    # 对账频率门(_wake_reconcile_last_at)挂在 tick 对象上; 同一 fake 跨轮保留。
    fake = getattr(agent, "_task_resume_fake", None)
    if fake is None:
        fake = _BackgroundSchedulerTickMixin()
        fake.runtime = SimpleNamespace(agent=agent)
        fake.store = agent.conversation_store
        agent._task_resume_fake = fake
    _BackgroundSchedulerTickMixin._enqueue_unfinished_task_resume_wakes(fake, now=now)


def _new_thread(agent: SimpleAgent) -> object:
    return agent.conversation_store.threads.get_or_create(
        {
            "canonical_user_id": "u1",
            "channel": "unknown",
            "channel_conversation_id": "c1",
            "channel_user_id": "u1",
            "now": 10.0,
        }
    )


def _bind(agent: SimpleAgent, thread_id: str, task_id: str, **extra) -> None:
    agent.conversation_store.tasks.bind(
        {"thread_id": thread_id, "task_id": task_id, "now": 20.0, **extra}
    )


def _create_goal(agent: SimpleAgent, thread_id: str, task_id: str) -> None:
    agent.conversation_store.goals.create(
        {
            "thread_id": thread_id,
            "objective": "持续推进直到完成",
            "task_id": task_id,
        }
    )


def test_due_sleep_note_raises_wake_queue_signal(tmp_path) -> None:
    """热层: 到期 sleep 字条 → 核线程档案 → wake_queue_due 唤醒(不翻任务目录)。"""
    agent = _agent(tmp_path)
    thread = _new_thread(agent)
    task_id = "req-111"
    _bind(agent, thread.thread_id, task_id)
    _write_task(Path(agent.home_paths.owner_home_dir), "2026-08-08", "celery", task_id, "RUNNING")
    agent.subagents.runtime_db.upsert_wake(
        root_task_id=task_id, next_due_at=50.0, kind="sleep", root_run_id="run-111",
    )

    _call_enqueue(agent, now=100.0)

    pending = agent.conversation_store.wakes.pending()
    assert len(pending) == 1
    assert pending[0].reason == "wake_queue_due"
    assert pending[0].root_task_id == task_id
    assert pending[0].thread_id == thread.thread_id
    # 一次性闹钟: 弹出即清行
    assert agent.subagents.runtime_db.list_pending_wakes() == []


def test_due_note_without_thread_link_completed_not_woken(tmp_path) -> None:
    """热层: 档案没了(任务已终结/未挂线程) → 清字条, 不唤醒。"""
    agent = _agent(tmp_path)
    task_id = "req-orphan"
    agent.subagents.runtime_db.upsert_wake(
        root_task_id=task_id, next_due_at=50.0, kind="sleep",
    )

    _call_enqueue(agent, now=100.0)

    assert agent.conversation_store.wakes.pending() == []
    assert agent.subagents.runtime_db.list_pending_wakes() == []


def test_reconcile_upserts_goal_tick_only_for_active_goal_tasks(tmp_path) -> None:
    """低频对账(5min 门): 未完成任务补 goal_tick 只限 active-goal 任务
    (EXEC-39 同源); 普通任务无字条不自动醒。"""
    agent = _agent(tmp_path)
    thread = _new_thread(agent)
    goal_task = "req-goal"
    plain_task = "req-plain"
    _bind(agent, thread.thread_id, goal_task)
    _bind(agent, thread.thread_id, plain_task)
    _create_goal(agent, thread.thread_id, goal_task)
    home = Path(agent.home_paths.owner_home_dir)
    _write_task(home, "2026-08-08", "goal-task", goal_task, "RUNNING")
    _write_task(home, "2026-08-08", "plain-task", plain_task, "RUNNING")

    _call_enqueue(agent, now=1000.0)  # 1000-0 >= 300 → 首次对账

    pending_notes = agent.subagents.runtime_db.list_pending_wakes()
    assert len(pending_notes) == 1
    assert pending_notes[0]["root_task_id"] == goal_task
    assert pending_notes[0]["kind"] == "goal_tick"
    # 对账只落字条(冷却后到期), 不立即 raise wake
    assert agent.conversation_store.wakes.pending() == []
    assert pending_notes[0]["next_due_at"] > 1000.0


def test_reconcile_gated_by_five_minute_interval(tmp_path) -> None:
    """对账分频: 5min 内不重跑; 过期后才补字条。"""
    agent = _agent(tmp_path)
    thread = _new_thread(agent)
    task_id = "req-freq"
    _bind(agent, thread.thread_id, task_id)
    _create_goal(agent, thread.thread_id, task_id)
    _write_task(Path(agent.home_paths.owner_home_dir), "2026-08-08", "freq", task_id, "RUNNING")

    _call_enqueue(agent, now=100.0)  # 未到首次对账门(0→100 < 300)
    assert agent.subagents.runtime_db.list_pending_wakes() == []

    _call_enqueue(agent, now=250.0)  # 仍未到(距上次对账 0→250 < 300)
    assert agent.subagents.runtime_db.list_pending_wakes() == []

    _call_enqueue(agent, now=401.0)  # 距上次对账 0→401 >= 300 → 对账补字条
    pending = agent.subagents.runtime_db.list_pending_wakes()
    assert len(pending) == 1
    assert pending[0]["root_task_id"] == task_id


def test_reconcile_cancels_stale_notes_for_terminal_tasks(tmp_path) -> None:
    """对账清僵尸: 字条在但任务已终结(link 终态) → 清字条, 不唤醒。"""
    agent = _agent(tmp_path)
    thread = _new_thread(agent)
    task_id = "req-done"
    _bind(agent, thread.thread_id, task_id)
    agent.subagents.runtime_db.upsert_wake(
        root_task_id=task_id, next_due_at=50.0, kind="sleep",
    )
    agent.conversation_store.tasks.update_status(
        {"task_id": task_id, "status": "done", "now": 25.0}
    )
    _write_task(
        Path(agent.home_paths.owner_home_dir), "2026-08-08", "done-task", task_id, "RUNNING"
    )  # 账本残留 RUNNING, 但 link 终态为权威

    _call_enqueue(agent, now=1000.0)

    assert agent.subagents.runtime_db.list_pending_wakes() == []
    assert agent.conversation_store.wakes.pending() == []


def test_reconcile_skips_audit_work_kind_tasks(tmp_path) -> None:
    """audit 任务有自己的观察/audit 唤醒通道, 对账跳过防双重拉起
    (H 批实锤: capacity wake 冻结回复被二次模型调用打破)。"""
    agent = _agent(tmp_path)
    thread = _new_thread(agent)
    task_id = "audit-aggregate-capacity"
    _bind(
        agent, thread.thread_id, task_id,
        goal="持续审计", work_kind="audit", work_name="安全审计",
    )
    _create_goal(agent, thread.thread_id, task_id)  # 即使有 active goal 也跳过
    _write_task(
        Path(agent.home_paths.owner_home_dir), "2026-08-08", "安全审计", task_id, "RUNNING"
    )

    _call_enqueue(agent, now=1000.0)

    assert agent.subagents.runtime_db.list_pending_wakes() == []
    assert agent.conversation_store.wakes.pending() == []


def test_goal_tick_note_pops_after_cooldown_and_wakes(tmp_path) -> None:
    """端到端节奏: 对账落 goal_tick(冷却后到期) → 冷却内不唤醒 → 到期弹出
    一次性 wake_queue_due 唤醒; 同 tick 对账为仍未完成任务重新武装下一轮
    冷却字条(旧契约"消费后隔 cooldown 再落"的等价)。"""
    agent = _agent(tmp_path)
    thread = _new_thread(agent)
    task_id = "req-333"
    _bind(agent, thread.thread_id, task_id)
    _create_goal(agent, thread.thread_id, task_id)
    _write_task(Path(agent.home_paths.owner_home_dir), "2026-08-08", "celery", task_id, "RUNNING")

    _call_enqueue(agent, now=1000.0)  # 对账: goal_tick 到期 at 1000+900=1900
    assert agent.conversation_store.wakes.pending() == []

    _call_enqueue(agent, now=1500.0)  # 冷却内: 字条未到期, 不唤醒
    assert agent.conversation_store.wakes.pending() == []

    _call_enqueue(agent, now=1901.0)  # 到期: 弹出并唤醒
    pending = agent.conversation_store.wakes.pending()
    assert len(pending) == 1
    assert pending[0].reason == "wake_queue_due"
    assert pending[0].root_task_id == task_id
    # 消费后同 tick 对账重新武装下一轮冷却字条(到期时间 > 本次 pop 时刻)
    notes = agent.subagents.runtime_db.list_pending_wakes()
    assert len(notes) == 1
    assert notes[0]["root_task_id"] == task_id
    assert notes[0]["kind"] == "goal_tick"
    assert notes[0]["next_due_at"] > 1901.0
