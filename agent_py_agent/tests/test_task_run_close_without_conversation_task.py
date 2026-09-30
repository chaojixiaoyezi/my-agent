"""没有会话任务关联的请求，代理树结束后 TaskRun 也要关闭（2026-09-30，ae 真实模型验收 D3）。

真机（bf4f740c3 与 10041de02 各复现）：请求 record 没有 conversation_runtime、conversations/tasks/<请求>.json 不存在
（只调 list_agents、管理员共享模型、不调工具），请求已 done、主 agent_run 已 done，TaskRun 却永远 created。
唯一的关闭路径要求会话任务关联已终态；没有关联文件时直接返回。锁定：
- 关联文件确实不存在 → 代理树结束后用同一个树终态 CAS 关闭（operator=agent-runtime，reason=no_conversation_task）；
- 子代理仍在运行时不关，最后一个子代理收口时关；
- unknown attempt 的子代理仍挡住关闭（被 SIGKILL 的子代理是另一个问题 O1，不放宽）；
- 关联文件读坏、没有会话存储 → 保持打开（证明不了“没有关联”）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime_mixin import (
    _settle_main_agent_run,
    _settle_terminal_conversation_task_run,
)
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository

_OK = SimpleNamespace(runtime_status="ok", runtime_reason="", runtime_source="", tool_rounds=1)


@pytest.fixture
def world(tmp_path):
    repo = RuntimeRepository(tmp_path / "home" / "runtime.db")
    store = ConversationStore(tmp_path / "conversations")
    agent = SimpleNamespace(subagents=SimpleNamespace(runtime_db=repo), conversation_store=store)
    main = repo.record_run_creation(owner_id="local/main", goal="只调 list_agents", run_id="run-main",
                                    role="main", conversation_task_id="gwreq-1790801045-demo")
    return SimpleNamespace(repo=repo, store=store, agent=agent, main=main, tmp_path=tmp_path)


def _params(rec, run_id):
    return SimpleNamespace(run_id=run_id, task_id=rec["task_id"], attempt_id=rec["attempt_id"], task_attributes={})


def _task_run(world):
    return world.repo._runtime_connect().execute(
        "SELECT status, closed_at FROM task_runs WHERE task_run_id = ?", (world.main["task_run_id"],)).fetchone()


def _close_events(world):
    return world.repo._runtime_connect().execute(
        "SELECT payload_json FROM runtime_events WHERE task_run_id = ? AND event_type = 'task_run.closed'",
        (world.main["task_run_id"],)).fetchall()


def test_request_without_conversation_task_closes_its_task_run(world):
    assert world.store.tasks.load(world.main["task_id"]) is None
    _settle_main_agent_run(world.agent, _params(world.main, "run-main"), _OK)
    # 改前：主 run 已 done，TaskRun 仍 ("created", 0)。
    row = _task_run(world)
    assert (row["status"], row["closed_at"] > 0) == ("done", True)
    [event] = _close_events(world)
    assert '"operator": "agent-runtime"' in event["payload_json"]
    assert '"reason": "no_conversation_task"' in event["payload_json"]


def test_running_child_keeps_it_open_until_the_last_child_edge(world):
    child = world.repo.record_run_creation(owner_id="local/main", run_id="run-child", role="worker",
                                           parent_run_id="run-main")
    assert child["task_run_id"] == world.main["task_run_id"]
    _settle_main_agent_run(world.agent, _params(world.main, "run-main"), _OK)
    assert _task_run(world)["closed_at"] == 0
    world.repo.settle_agent_run(agent_run_id=child["agent_run_id"], status="done", attempt_id=child["attempt_id"])
    _settle_terminal_conversation_task_run(world.agent, _params(child, "run-child"))
    assert (_task_run(world)["status"], _task_run(world)["closed_at"] > 0) == ("done", True)


def test_unknown_child_attempt_still_blocks_closing(world):
    child = world.repo.record_run_creation(owner_id="local/main", run_id="run-child", role="worker",
                                           parent_run_id="run-main")
    conn = world.repo._runtime_connect()
    conn.execute("UPDATE agent_attempts SET status = 'unknown', ended_at = 1 WHERE attempt_id = ?", (child["attempt_id"],))
    conn.commit()
    _settle_main_agent_run(world.agent, _params(world.main, "run-main"), _OK)
    assert _task_run(world)["closed_at"] == 0


def test_unreadable_link_or_missing_store_keeps_it_open(world):
    link_path = world.store.storage.task_path(world.main["task_id"])
    link_path.parent.mkdir(parents=True, exist_ok=True)
    link_path.write_text("{broken", encoding="utf-8")
    _settle_main_agent_run(world.agent, _params(world.main, "run-main"), _OK)
    assert _task_run(world)["closed_at"] == 0
    link_path.unlink()
    no_store = SimpleNamespace(subagents=world.agent.subagents, conversation_store=None)
    _settle_terminal_conversation_task_run(no_store, _params(world.main, "run-main"))
    assert _task_run(world)["closed_at"] == 0
    # 关联文件确实不存在时，同一条总账随后照常关闭。
    _settle_terminal_conversation_task_run(world.agent, _params(world.main, "run-main"))
    assert _task_run(world)["closed_at"] > 0


def test_new_attempt_reopens_a_closed_request_task_run(world):
    _settle_main_agent_run(world.agent, _params(world.main, "run-main"), _OK)
    assert _task_run(world)["closed_at"] > 0
    world.repo.create_attempt(world.main["agent_run_id"])
    assert (_task_run(world)["status"], _task_run(world)["closed_at"]) == ("created", 0)


def test_empty_task_id_is_not_taken_as_no_link(world):
    # 空任务身份证明不了“没有关联”（读关联时空 ID 同样返回空），不能据此关闭。
    conn = world.repo._runtime_connect()
    conn.execute("UPDATE task_runs SET task_id = '' WHERE task_run_id = ?", (world.main["task_run_id"],))
    conn.commit()
    _settle_main_agent_run(world.agent, _params(world.main, "run-main"), _OK)
    assert _task_run(world)["closed_at"] == 0
