"""/recover 子代理分支（O1，2026-09-30 ae 真实模型验收）。

真机：子代理 runner 在写操作 handler 返回后被 SIGKILL，attempt 与 agent_run 停在 unknown、write_file 停在 EXECUTING；
父级已用替补接替（来源 TAKEN_OVER），主代理 done，TaskRun 因 unknown 子代理一直不关；而 /recover 只看根主代理，
回答“没有待核对项”，用户没有任何入口。钉住：
1. /recover 查看会列出本线程未关 TaskRun 里被中断的子代理、接替情况和未确认操作，且只读；
2. 主链不阻塞且恰好一条时，显式处置走同一个 unknown→recovered CAS；已被接替的来源收成 cancelled，TaskRun 随后关闭；
3. 主链阻塞时仍只处理主链；多于一条子代理时拒绝并列清单、不写库；
4. 范围只到本线程、未关 TaskRun、非根代理；写事务内复核目标，状态变了就拒绝；没有任何自动处置；
5. 网关不越层导入：接替判定经 SubAgentManager.taken_over_successor（真实类上必须存在），TaskRun 收口与执行收口边共用
   会话层 conversation/task_run_closeout.settle_terminal_task_run。
"""
from __future__ import annotations

import json
import time
import uuid
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime_mixin import _settle_main_agent_run
from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts import control_service
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.turn_recovery_control import execute_turn_recovery_control
from agent_py_agent.agent.runtime_db.child_recovery import (
    recover_child_attempt_unknown,
    unknown_child_attempts_for_thread,
)
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.subagents.manager import SubAgentManager, _taken_over_successor
from agent_py_agent.agent.subagents.model_task import SubAgentTask

THREAD = "thread-o1"
_OK = SimpleNamespace(runtime_status="ok", runtime_reason="", runtime_source="", tool_rounds=1)
VIEW = parse_conversation_control("/recover")


@pytest.fixture
def world(tmp_path):
    repo = RuntimeRepository(tmp_path / "home" / "runtime.db")
    store = ConversationStore(tmp_path / "conversations")
    records: dict[str, SubAgentTask] = {}

    def load(run_id: str) -> SubAgentTask:
        if run_id not in records:
            raise FileNotFoundError(run_id)
        return records[run_id]

    # 假的 manager 只替换存储，接替判定仍调用真实实现（真实类上的方法见最后一条用例）。
    subagents = SimpleNamespace(runtime_db=repo, load=load)
    subagents.taken_over_successor = lambda run_id: _taken_over_successor(subagents, run_id)
    owner = SimpleNamespace(subagents=subagents, conversation_store=store)
    main = _tree_root(repo, "run-main", "gwreq-o1", THREAD)
    thread = SimpleNamespace(thread_id=THREAD, workspace_task_id=main["task_id"])
    return SimpleNamespace(repo=repo, owner=owner, main=main, thread=thread, records=records)


def _tree_root(repo, run_id: str, task_id: str, thread_id: str) -> dict:
    return repo.record_run_creation(owner_id="local/main", goal="g", run_id=run_id, role="main",
                                    conversation_task_id=task_id, thread_id=thread_id)


def _killed_child(world, run_id: str, *, parent: str = "run-main", taken_over_by: str = "") -> dict:
    """复刻 O1：子代理执行轮与 run 都停在 unknown，write_file 停在 EXECUTING；接替关系写在子代理记录里。"""
    child = world.repo.record_run_creation(owner_id="local/main", run_id=run_id, role="worker", parent_run_id=parent)
    now = time.time()
    with world.repo.transaction() as conn:
        conn.execute("UPDATE agent_attempts SET status='unknown', ended_at=? WHERE attempt_id=?",
                     (now, child["attempt_id"]))
        conn.execute("UPDATE agent_runs SET status='unknown' WHERE agent_run_id=?", (child["agent_run_id"],))
        conn.execute(
            "INSERT INTO tool_operations(operation_id, agent_run_id, attempt_id, attempt_generation, "
            "tool_operation_generation, operation_type, status, handler_started_at, created_at, updated_at) "
            "VALUES(?,?,?,1,1,'write_file','EXECUTING',?,?,?)",
            (f"tool_operation:{uuid.uuid4().hex}", child["agent_run_id"], child["attempt_id"], now - 3, now, now),
        )
    world.records[run_id] = SubAgentTask(id=run_id, goal="g", thought="", plan=[],
                                         status="TAKEN_OVER" if taken_over_by else "BLOCKED",
                                         takeover_by=taken_over_by)
    return child


def _finish_main(world) -> None:
    params = SimpleNamespace(run_id="run-main", task_id=world.main["task_id"],
                             attempt_id=world.main["attempt_id"], task_attributes={})
    _settle_main_agent_run(world.owner, params, _OK)


def _statuses(world, rec) -> tuple[str, str]:
    conn = world.repo._runtime_connect()
    run = conn.execute("SELECT status FROM agent_runs WHERE agent_run_id=?", (rec["agent_run_id"],)).fetchone()
    attempt = conn.execute("SELECT status FROM agent_attempts WHERE attempt_id=?", (rec["attempt_id"],)).fetchone()
    return str(run[0]), str(attempt[0])


def _task_run_closed(world) -> bool:
    row = world.repo._runtime_connect().execute(
        "SELECT closed_at FROM task_runs WHERE task_run_id=?", (world.main["task_run_id"],)).fetchone()
    return row[0] > 0


def _events(world, event_type: str) -> list[dict]:
    rows = world.repo._runtime_connect().execute(
        "SELECT payload_json FROM runtime_events WHERE event_type=? ORDER BY rowid", (event_type,)).fetchall()
    return [json.loads(row[0]) for row in rows]


def _recover(world, text: str):
    return execute_turn_recovery_control(world.owner, world.thread, parse_conversation_control(text))


def test_view_lists_killed_child_with_takeover_and_unsettled_operation_without_writing(world):
    child = _killed_child(world, "subagent-src", taken_over_by="subagent-repl")
    _finish_main(world)
    assert not _task_run_closed(world)

    result = _recover(world, "/recover")

    # 改前：主代理 done → “当前会话没有等待核对的执行”。
    assert result.ok is True and "无需恢复" not in result.message
    assert "子代理 subagent-src（worker）｜已由 subagent-repl 接替" in result.message
    assert "write_file｜执行中断，结果未回传" in result.message
    assert "/recover recorded" in result.message and "由主代理决定" in result.message
    assert _statuses(world, child) == ("unknown", "unknown")
    assert not _task_run_closed(world) and _events(world, "attempt_recovered") == []


def test_apply_recovers_taken_over_child_settles_it_and_closes_the_task_run(world):
    child = _killed_child(world, "subagent-src", taken_over_by="subagent-repl")
    _finish_main(world)

    result = _recover(world, "/recover recorded")

    assert result.ok is True
    assert "解除子代理 subagent-src 的阻塞" in result.message and "已由 subagent-repl 接替" in result.message
    assert _statuses(world, child) == ("cancelled", "recovered")
    [recovered] = _events(world, "attempt_recovered")
    assert recovered["operator"] == "conversation-control:/recover"
    assert recovered["effect_disposition"] == "recorded"
    assert (recovered["recovery_target"], recovered["thread_id"]) == ("child_agent_run", THREAD)
    assert recovered["task_run_id"] == world.main["task_run_id"]
    assert any(p.get("runtime_reason") == "taken_over" for p in _events(world, "agent_run.completed"))
    assert _task_run_closed(world)
    assert "无需恢复" in _recover(world, "/recover").message


def test_child_that_was_not_taken_over_stays_resumable_for_the_parent(world):
    child = _killed_child(world, "subagent-src")
    _finish_main(world)

    result = _recover(world, "/recover confirmed_noop")

    assert result.ok is True and "没有被接替，是否让它接着做由主代理决定" in result.message
    assert _statuses(world, child) == ("created", "recovered")
    # recovered 算静止：树规则允许关；之后若父级续跑，create_attempt 会重新打开 TaskRun。
    assert _task_run_closed(world)
    world.repo.create_attempt(child["agent_run_id"])
    assert not _task_run_closed(world)


def test_unreadable_subagent_record_shows_unknown_takeover_and_skips_takeover_settle(world):
    child = _killed_child(world, "subagent-src", taken_over_by="subagent-repl")
    del world.records["subagent-src"]
    _finish_main(world)

    assert "接替情况未知（子代理记录读不到）" in _recover(world, "/recover").message
    result = _recover(world, "/recover abandoned")

    assert result.ok is True and "没有被接替" in result.message
    assert _statuses(world, child) == ("created", "recovered")
    assert not any(p.get("runtime_reason") == "taken_over" for p in _events(world, "agent_run.completed"))


def test_only_a_taken_over_status_counts_as_takeover(world):
    child = _killed_child(world, "subagent-src", taken_over_by="subagent-repl")
    world.records["subagent-src"].status = "BLOCKED"  # 记录不一致：有 takeover_by 但状态不是 TAKEN_OVER
    _finish_main(world)

    assert "没有被接替" in _recover(world, "/recover").message
    assert _recover(world, "/recover recorded").ok is True
    assert _statuses(world, child) == ("created", "recovered")


def test_superseded_source_is_not_treated_as_taken_over(world):
    child = _killed_child(world, "subagent-src", taken_over_by="subagent-repl")
    record = world.records["subagent-src"]
    record.takeover_by, record.superseded_by = "", "subagent-repl"  # 只记 superseded_by：不是“已被接管”
    _finish_main(world)

    assert "没有被接替" in _recover(world, "/recover").message
    assert _recover(world, "/recover recorded").ok is True
    assert _statuses(world, child) == ("created", "recovered")


def test_root_block_keeps_priority_and_children_wait_their_turn(world):
    child = _killed_child(world, "subagent-src", taken_over_by="subagent-repl")
    with world.repo.transaction() as conn:
        conn.execute("UPDATE agent_attempts SET status='unknown', ended_at=1 WHERE attempt_id=?",
                     (world.main["attempt_id"],))
        conn.execute("UPDATE agent_runs SET status='unknown' WHERE agent_run_id=?", (world.main["agent_run_id"],))

    view = _recover(world, "/recover")
    assert view.message.startswith("上一轮执行中断") and "另有 1 个子代理执行中断待核对" in view.message
    applied = _recover(world, "/recover recorded")

    assert applied.ok is True and "下一条消息会接着原任务继续" in applied.message
    assert _statuses(world, world.main) == ("created", "recovered")
    assert _statuses(world, child) == ("unknown", "unknown")
    assert "子代理 subagent-src" in _recover(world, "/recover").message


def test_two_children_are_listed_but_apply_is_refused_without_writing(world):
    first = _killed_child(world, "subagent-a")
    second = _killed_child(world, "subagent-b")
    _finish_main(world)

    view = _recover(world, "/recover")
    assert "subagent-a" in view.message and "subagent-b" in view.message
    assert "暂不支持指定目标" in view.message and "/recover recorded" not in view.message
    refused = _recover(world, "/recover recorded")

    assert refused.ok is False and refused.error_code == "RUN_RECOVERY_REJECTED"
    assert "子代理 subagent-a（worker）" in refused.message and "子代理 subagent-b（worker）" in refused.message
    assert _statuses(world, first) == ("unknown", "unknown") and _statuses(world, second) == ("unknown", "unknown")
    assert _events(world, "attempt_recovered") == []


def test_projection_is_limited_to_this_thread_open_task_runs_and_non_root_runs(world):
    _killed_child(world, "subagent-src")
    _tree_root(world.repo, "run-other", "gwreq-other", "thread-other")
    _killed_child(world, "subagent-other", parent="run-other")
    closed_root = _tree_root(world.repo, "run-closed", "gwreq-closed", THREAD)
    _tree_root(world.repo, "run-nothread", "gwreq-nothread", "")
    _killed_child(world, "subagent-nothread", parent="run-nothread")
    _killed_child(world, "subagent-closed", parent="run-closed")
    with world.repo.transaction() as conn:
        conn.execute("UPDATE task_runs SET closed_at=1 WHERE task_run_id=?", (closed_root["task_run_id"],))
        conn.execute("UPDATE agent_attempts SET status='unknown' WHERE attempt_id=?", (world.main["attempt_id"],))

    assert [t.run_id for t in unknown_child_attempts_for_thread(world.repo, THREAD)] == ["subagent-src"]
    assert [t.run_id for t in unknown_child_attempts_for_thread(world.repo, "thread-other")] == ["subagent-other"]
    assert unknown_child_attempts_for_thread(world.repo, "") == []
    assert unknown_child_attempts_for_thread(world.repo, "  ") == []


def test_write_rechecks_the_target_inside_the_transaction(world):
    child = _killed_child(world, "subagent-src")
    [target] = unknown_child_attempts_for_thread(world.repo, THREAD)

    invalid = recover_child_attempt_unknown(world.repo, target, "done", "test")
    assert invalid == {"recovered": False, "reason": "invalid_effect_disposition"}
    forged = recover_child_attempt_unknown(
        world.repo, type(target)(**{**target.__dict__, "thread_id": "thread-other"}), "recorded", "test")
    assert forged == {"recovered": False, "reason": "target_changed"}
    assert world.repo.recover_attempt_unknown(target.attempt_id, operator="elsewhere")["recovered"] is True
    stale = recover_child_attempt_unknown(world.repo, target, "recorded", "test")
    assert stale == {"recovered": False, "reason": "target_changed"}
    assert _statuses(world, child) == ("created", "recovered")
    assert len(_events(world, "attempt_recovered")) == 1


def test_gateway_dispatch_reaches_the_child_branch(world, tmp_path, monkeypatch):
    child = _killed_child(world, "subagent-src", taken_over_by="subagent-repl")
    _finish_main(world)
    monkeypatch.setattr(world.owner.conversation_store.threads, "resolve", lambda **_kwargs: world.thread)
    monkeypatch.setattr(control_service, "_request_agent_for_scope", lambda _base, _scope: world.owner)
    scope = control_service.GatewayControlScope(user_id="local-agent", channel="feishu", conversation_id="oc-1")

    result = control_service.execute_gateway_conversation_control(
        object(), gateway_paths_from_root(tmp_path), parse_conversation_control("/recover recorded"), scope,
    )

    assert result.ok is True and "解除子代理 subagent-src 的阻塞" in result.message
    assert _statuses(world, child) == ("cancelled", "recovered") and _task_run_closed(world)


def test_gateway_reaches_lower_layers_only_through_allowed_paths():
    from agent_py_agent.agent.agent_core import runtime_mixin
    from agent_py_agent.agent.conversation import task_run_closeout
    from agent_py_agent.agent.gateway_parts import turn_recovery_control
    from scripts.check_import_boundaries import check_import_boundaries

    # TaskRun 收口只有一份实现：执行收口边与 /recover 子代理分支调用同一个会话层函数。
    assert runtime_mixin.settle_terminal_task_run is task_run_closeout.settle_terminal_task_run
    assert turn_recovery_control.settle_terminal_task_run is task_run_closeout.settle_terminal_task_run
    assert callable(SubAgentManager.taken_over_successor)
    findings = [item for item in check_import_boundaries() if item.source.endswith("turn_recovery_control.py")]
    assert findings == []
