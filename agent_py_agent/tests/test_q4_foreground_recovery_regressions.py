"""Q4：真实运行库与会话根上的前台恢复边界；不启动模型或外部服务。"""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import runtime_mixin
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.runtime_db.operations import RuntimeRecoveryRequiredError
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.test_im_execution_threads import fixture_agent
from agent_py_agent.tests.test_turn_recovery_control import _insert_operation


# LLM: 在临时目录建立真实ThreadStore/TaskLink/RuntimeDB，原发起人由宿主typed参数冻结；SQL只模拟中断状态。
# 函数用途: 提供本地、私聊和群聊的未知主链，不借模型属性制造身份。
def _q4_case(tmp_path, *, group=False, channel="tui"):
    identity = OwnerIdentity("feishu", "group", "oc_source") if group else OwnerIdentity.local_main()
    agent, thread = fixture_agent(tmp_path, identity=identity, channel="feishu" if group else channel)
    actor = agent._current_run_params.host_channel_actor
    repo = RuntimeRepository(str(agent.root / "q4-runtime.db"))
    agent.subagents = SimpleNamespace(runtime_db=repo)
    rec = repo.record_run_creation(owner_id=agent.home_paths.owner_id, run_id="q4-run",
        conversation_task_id="q4-task", thread_id=thread.thread_id, role="main")
    operation = _insert_operation(repo, rec, status="UNKNOWN", started_at=1234.5)
    with repo.transaction() as conn:
        conn.execute("UPDATE agent_attempts SET status='unknown' WHERE attempt_id=?", (rec["attempt_id"],))
        conn.execute("UPDATE agent_runs SET status='unknown' WHERE agent_run_id=?", (rec["agent_run_id"],))
    agent.conversation_store.tasks.bind({"thread_id": thread.thread_id, "task_id": "q4-task", "goal": "合成"},
        host_channel_actor=actor)
    params = RunParams(source="gateway", request_id="q4-next", run_id="q4-run", task_id="q4-task",
        task_attributes={"conversation_thread_id": thread.thread_id},
        host_channel_actor=replace(actor, entry_request_id="q4-next") if actor else None)
    return agent, repo, rec, operation, params


# LLM: 必须走真实绑定入口并确认旧未知行和attempt数量不变，不能只断言某个helper返回False。
# 函数用途: 核对关掉开关、宿主回执和唤醒事件都不能自动放行。
@pytest.mark.parametrize("reason", ["disabled", "ack", "empty_ack", "wake", "http"])
def test_q4_noninteractive_or_disabled_recovery_stays_unknown(tmp_path, reason):
    agent, repo, rec, _operation, params = _q4_case(tmp_path, channel="http" if reason == "http" else "tui")
    if reason == "disabled":
        agent.config.foreground_unknown_auto_recover = False
    elif reason in {"ack", "empty_ack"}:
        params.task_attributes["host_im_task_ack"] = {"task_id": "ack"} if reason == "ack" else {}
    elif reason == "wake":
        params.turn_trigger = SimpleNamespace(kind="session_message")
    with pytest.raises(RuntimeRecoveryRequiredError):
        runtime_mixin._bind_main_agent_authority(agent, params)
    assert repo.get_attempt(rec["attempt_id"])["status"] == "unknown"
    assert len(repo.attempts_for_run(rec["agent_run_id"])) == 1


# LLM: 原群成员不等于任务原principal；同owner的另一人、旧request、跨owner/线程以及attrs伪造均须拒绝。
# 函数用途: 验证只有准确原发起人的新入站可以自动恢复群任务。
@pytest.mark.parametrize("mutation", ["other_member", "owner", "thread", "request", "dict", "attrs", "missing"])
def test_q4_cross_principal_recovery_is_refused(tmp_path, mutation):
    agent, repo, rec, _operation, params = _q4_case(tmp_path, group=True)
    actor = params.host_channel_actor
    if mutation == "dict":
        params.host_channel_actor = actor.to_record()
    elif mutation in {"attrs", "missing"}:
        params.host_channel_actor = None
        if mutation == "attrs":
            params.task_attributes["host_channel_actor"] = actor.to_record()
    else:
        key, value = {"other_member": ("actor_id", "ou_b"), "owner": ("owner_id", "foreign"),
            "thread": ("source_thread_id", "foreign-thread"), "request": ("entry_request_id", "stale")}[mutation]
        params.host_channel_actor = replace(actor, **{key: value})
    with pytest.raises(RuntimeRecoveryRequiredError):
        runtime_mixin._bind_main_agent_authority(agent, params)
    assert repo.get_attempt(rec["attempt_id"])["status"] == "unknown"


# LLM: 正例使用真实主链恢复与原模型注入口，缺省开关仍True；提示仅包含允许的结构化操作事实。
# 函数用途: 验证本地及群原发起人可恢复，并收到工具名、开始时间和operation_id，不泄露操作正文。
@pytest.mark.parametrize("group", [False, True])
def test_q4_principal_gets_structured_verification_notice(tmp_path, group, monkeypatch):
    agent, repo, rec, operation, params = _q4_case(tmp_path, group=group)
    original = repo.unsettled_attempt_operations
    monkeypatch.setattr(repo, "unsettled_attempt_operations", lambda attempt: [
        {**row, "arguments": "SECRET_ARGS", "result": "SECRET_RESULT"} for row in original(attempt)])
    bound = runtime_mixin._bind_main_agent_authority(agent, params)
    notice = "\n".join(bound.inject)
    assert repo.get_attempt(rec["attempt_id"])["status"] == "recovered"
    assert all(value in notice for value in ("operation_type", "handler_started_at", "operation_id",
        "run_command", "1234.5", operation, "只读", "不会自动重做"))
    assert "SECRET_ARGS" not in notice and "SECRET_RESULT" not in notice


# LLM: 部分确认回执通过真实绑定路径注入，必须保留原UNKNOWN，不得继续创建新attempt或静默忽略。
# 函数用途: 保护任务单要求的remaining_unknown失败关闭与日志告警。
def test_q4_remaining_unknown_is_logged_and_blocks_mount(tmp_path, monkeypatch, caplog):
    agent, repo, rec, operation, params = _q4_case(tmp_path)
    monkeypatch.setattr(repo, "recover_attempt_unknown", lambda *args, **kwargs: {
        "recovered": True, "remaining_unknown": [operation], "recorded": []})
    with pytest.raises(RuntimeRecoveryRequiredError):
        runtime_mixin._bind_main_agent_authority(agent, params)
    assert "remaining_unknown" in caplog.text
    assert repo.get_attempt(rec["attempt_id"])["status"] == "unknown"
    assert len(repo.attempts_for_run(rec["agent_run_id"])) == 1


# LLM: 同群同线程不等于同principal；真实子链及恢复事件属于A，B新任务既不能恢复也不能接收A的核实事项。
# 函数用途: 防止别人的未知子任务或待交接提示阻断当前用户自己的新任务。
@pytest.mark.parametrize("pending", [False, True])
def test_q4_foreign_child_does_not_block_unrelated_new_task(tmp_path, pending):
    from agent_py_agent.agent.runtime_db.child_recovery import (
        recover_child_attempt_unknown,
        unknown_child_attempts_for_thread,
    )

    agent, repo, rec, _operation, params = _q4_case(tmp_path, group=True)
    child = repo.record_run_creation(owner_id=agent.home_paths.owner_id, run_id="q4-child",
        parent_run_id="q4-run", role="worker")
    child_operation = _insert_operation(repo, child, status="UNKNOWN", started_at=77, tool="foreign_tool")
    with repo.transaction() as conn:
        conn.execute("UPDATE agent_attempts SET status='unknown' WHERE attempt_id=?", (child["attempt_id"],))
        conn.execute("UPDATE agent_runs SET status='unknown' WHERE agent_run_id=?", (child["agent_run_id"],))
    if pending:
        target, = unknown_child_attempts_for_thread(repo, params.task_attributes["conversation_thread_id"])
        assert recover_child_attempt_unknown(repo, target, "abandoned", "foreground-user-turn")["recovered"]
    params = replace(params, run_id="own-new-run", task_id="own-new-task",
        host_channel_actor=replace(params.host_channel_actor, actor_id="ou_b"))
    bound = runtime_mixin._bind_main_agent_authority(agent, params)
    assert repo.get_attempt(bound.attempt_id)["status"] == "running"
    assert repo.get_attempt(rec["attempt_id"])["status"] == "unknown"
    assert repo.get_attempt(child["attempt_id"])["status"] == ("recovered" if pending else "unknown")
    assert "foreign_tool" not in "\n".join(bound.inject or [])
    assert child_operation not in "\n".join(bound.inject or [])
    assert not getattr(bound, "foreground_recovery_attempt_ids", ())
