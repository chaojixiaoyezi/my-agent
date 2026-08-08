"""H 批:任务账本 RUNNING → 续跑 wake(tick 层 _enqueue_unfinished_task_resume_wakes)。

真机:celery 复刻 RUNNING 6h 无人驱动——发现层把 RUNNING 任务判为硬事实进池,
但 tick 只消费 wake/policy,账本没有消费者变成待驱动信号,主代理永远不被拉起。
本测试钉住:未完成任务 → 落 dedupe wake → 既有消费链拉起主代理续跑,直到终态。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.conversation.runtime import _BackgroundSchedulerTickMixin
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings.config import AgentConfig


def _agent(tmp_path) -> SimpleAgent:
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
    # 冷却状态挂在 tick 对象上;同 agent 复用同一 fake,模拟真实 tick 实例跨轮保留。
    fake = getattr(agent, "_task_resume_fake", None)
    if fake is None:
        fake = SimpleNamespace(
            runtime=SimpleNamespace(agent=agent),
            store=agent.conversation_store,
        )
        agent._task_resume_fake = fake
    _BackgroundSchedulerTickMixin._enqueue_unfinished_task_resume_wakes(fake, now=now)


def _new_thread(agent: SimpleAgent) -> object:
    return agent.conversation_store.get_or_create_thread(
        {
            "canonical_user_id": "u1",
            "channel": "unknown",
            "channel_conversation_id": "c1",
            "channel_user_id": "u1",
            "now": 10.0,
        }
    )


def _bind(agent: SimpleAgent, thread_id: str, task_id: str) -> None:
    agent.conversation_store.bind_task(
        {"thread_id": thread_id, "task_id": task_id, "now": 20.0}
    )


def test_running_task_writes_resume_wake(tmp_path) -> None:
    agent = _agent(tmp_path)
    thread = _new_thread(agent)
    task_id = "req_111"
    _bind(agent, thread.thread_id, task_id)
    _write_task(Path(agent.home_paths.owner_home_dir), "2026-08-08", "celery", task_id, "RUNNING")

    _call_enqueue(agent)

    pending = agent.conversation_store.pending_wake_signals()
    assert len(pending) == 1
    assert pending[0].reason == "task_ledger_resume"
    assert pending[0].root_task_id == task_id
    assert pending[0].thread_id == thread.thread_id


def test_pending_wake_not_duplicated_same_round(tmp_path) -> None:
    agent = _agent(tmp_path)
    thread = _new_thread(agent)
    task_id = "req_222"
    _bind(agent, thread.thread_id, task_id)
    _write_task(Path(agent.home_paths.owner_home_dir), "2026-08-08", "celery", task_id, "RUNNING")

    _call_enqueue(agent)
    _call_enqueue(agent)  # 同轮重复 tick:dedupe_key 命中已有 pending wake,不重写

    pending = agent.conversation_store.pending_wake_signals()
    assert len(pending) == 1


def test_consumed_wake_is_recreated_after_cooldown(tmp_path) -> None:
    """消费后任务仍 RUNNING → 冷却期内不重复催促(子代理在跑/主代理 turn 推进的窗口,
    重复拉起只有模型调用成本),过冷却期再落一条续跑直到终态。"""
    agent = _agent(tmp_path)
    thread = _new_thread(agent)
    task_id = "req_333"
    _bind(agent, thread.thread_id, task_id)
    _write_task(Path(agent.home_paths.owner_home_dir), "2026-08-08", "celery", task_id, "RUNNING")

    _call_enqueue(agent, now=100.0)
    signal = agent.conversation_store.pending_wake_signals()[0]
    agent.conversation_store.mark_wake_signal_handled(signal.wake_signal_id, now=110.0)
    assert not agent.conversation_store.pending_wake_signals()

    _call_enqueue(agent, now=120.0)  # 冷却期(900s)内:不重生成

    assert agent.conversation_store.pending_wake_signals() == []

    _call_enqueue(agent, now=1000.0)  # 过冷却期:再落一条

    pending = agent.conversation_store.pending_wake_signals()
    assert len(pending) == 1
    assert pending[0].root_task_id == task_id


def test_terminal_tasks_do_not_write_wake(tmp_path) -> None:
    """终态以 link 为准(生命周期权威):bind 后 update_task_status 置终态 → 不再落 wake。
    账本投影怎么写都不影响(旧版 DONE 后账本残留 RUNNING 的场景见发现层测试)。"""
    agent = _agent(tmp_path)
    thread = _new_thread(agent)
    home = Path(agent.home_paths.owner_home_dir)
    for status in ("done", "failed", "abandoned", "cancelled"):
        task_id = f"req-done-{status}"
        _bind(agent, thread.thread_id, task_id)
        agent.conversation_store.update_task_status(
            {"task_id": task_id, "status": status, "now": 25.0}
        )
        _write_task(home, "2026-08-08", f"task-{status}", task_id, "RUNNING")  # 账本残留 RUNNING

    _call_enqueue(agent)

    assert agent.conversation_store.pending_wake_signals() == []


def test_audit_kind_task_skipped_has_own_wake_channel(tmp_path) -> None:
    """audit/goal 任务有自己的唤醒通道(观察+audit wake 消费链),续跑 wake 会双重拉起
    主代理(测试实锤:capacity wake 冻结回复被二次模型调用打破)。按 link 的 work_kind
    结构化跳过,不落 task_ledger_resume wake。"""
    agent = _agent(tmp_path)
    thread = _new_thread(agent)
    task_id = "audit-aggregate-capacity"
    agent.conversation_store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": task_id,
            "goal": "持续审计",
            "status": "active",
            "work_kind": "audit",
            "work_name": "安全审计",
            "now": 2.0,
        }
    )
    _write_task(Path(agent.home_paths.owner_home_dir), "2026-08-08", "安全审计", task_id, "RUNNING")

    _call_enqueue(agent)

    assert agent.conversation_store.pending_wake_signals() == []


def test_task_without_thread_link_skipped(tmp_path) -> None:
    agent = _agent(tmp_path)
    task_id = "req-orphan"
    _write_task(Path(agent.home_paths.owner_home_dir), "2026-08-08", "orphan", task_id, "RUNNING")

    _call_enqueue(agent)

    assert agent.conversation_store.pending_wake_signals() == []
