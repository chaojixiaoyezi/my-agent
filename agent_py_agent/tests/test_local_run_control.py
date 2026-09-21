"""本地控制的真实临时账本与受控 worker 回归；不计作真实 TUI 验收。"""
from __future__ import annotations

import queue
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.agent_core.runtime_mixin import _bind_main_agent_authority
from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.conversation.local_run_control import (
    LocalRunControl,
    prepare_local_task_stop,
)
from agent_py_agent.agent.runtime_db.managed_operation_store import AuthorityContextMissing
from agent_py_agent.agent.runtime_db.run_cancellation import RuntimeCancellationConflict
from agent_py_agent.agent.tooling import process_resource_stop as resources
from agent_py_agent.cli.chat_parts.control_runtime import (
    ChatControlExecution,
    ChatControlState,
    execute_chat_control,
)
from agent_py_agent.cli.chat_parts.plain_handlers import PlainJobContext, _plain_local_handle
from agent_py_agent.cli.chat_parts.plain_state import ChatJob
from agent_py_agent.cli.chat_parts.tui_worker import _reset_worker_refs, _tui_update_running_state
from agent_py_agent.cli.chat_parts.tui_worker_local import _worker_local_path
from agent_py_agent.tests.test_task_resource_stop import _authority, _write
from agent_py_agent.tests.test_task_resource_stop import main_task as main_task


# LLM: 测试只使用临时 RuntimeDB 的正式记录，消息编号有意与 task/run 分开；不构造宿主进程。
# 函数用途: 给本地控制句柄发布真实主链身份，覆盖消息与执行身份不同的路径。
def _control(record, request_id="message"):
    control = LocalRunControl(request_id)
    control.bind_runtime_authority({
        "invocation_run_id": request_id, "task_id": "task", "run_id": "main",
        "agent_run_id": record["agent_run_id"], "attempt_id": record["attempt_id"],
    })
    return control


def test_local_stop_uses_published_ids_and_frozen_resources(main_task, monkeypatch):
    agent, _link, record, scope, store = main_task
    control = _control(record)
    _write(store, scope, "bg-main")
    _write(store, replace(scope, run_id="unrelated"), "bg-other")
    agent._current_run_params = SimpleNamespace(request_id="wrong", task_id="wrong", run_id="wrong")
    cleaned, batches = threading.Event(), []

    def clean(batch):
        batches.append(batch)
        cleaned.set()

    monkeypatch.setattr(resources, "cleanup_process_stop", clean)
    monkeypatch.setattr(agent, "cancel_request_subagents", Mock())
    execution = ChatControlExecution(agent, False, ChatControlState(True, 0, "工作", 0, "ui-session", "message", control))
    result = execute_chat_control(execution, parse_conversation_control("/stop"))
    assert result.ok and result.delivery_status == "accepted"
    assert cleaned.wait(2)
    assert [row["session_id"] for row in batches[0].receipt.records] == ["bg-main"]
    assert not store.load("bg-other").record["stop_requested"]
    operations, authority = _authority(agent, record)
    with pytest.raises(AuthorityContextMissing):
        operations.require_authority(authority)
    assert control.runtime_authority()["run_id"] == "main"


def test_local_interrupt_keeps_authority_background_and_child_resources(main_task, monkeypatch):
    agent, _link, record, scope, store = main_task
    control = _control(record)
    _write(store, scope, "bg-main")
    clean = Mock(side_effect=AssertionError("纯中断不清理后台"))
    monkeypatch.setattr(resources, "freeze_process_stop", clean)
    child_cancel = Mock(side_effect=AssertionError("纯中断不取消孩子"))
    monkeypatch.setattr(agent, "cancel_request_subagents", child_cancel)
    result = execute_chat_control(
        ChatControlExecution(agent, False, ChatControlState(True, 0, "工作", 0, "session", "message", control)),
        parse_conversation_control("/interrupt"),
    )
    assert result.ok
    assert not store.load("bg-main").record["stop_requested"]
    operations, authority = _authority(agent, record)
    operations.require_authority(authority)
    clean.assert_not_called()
    child_cancel.assert_not_called()


def test_local_partial_freeze_reports_unknown_and_retains_committed_batch(main_task, monkeypatch):
    agent, _link, record, scope, store = main_task
    _write(store, scope, "bg-committed")
    control = _control(record)
    cleaned, batches = threading.Event(), []

    def clean(batch):
        batches.append(batch)
        cleaned.set()

    monkeypatch.setattr(resources.pty_session_registry, "request_stop", Mock(side_effect=OSError("unavailable")))
    monkeypatch.setattr(resources, "cleanup_process_stop", clean)
    monkeypatch.setattr(agent, "cancel_request_subagents", Mock())
    result = execute_chat_control(
        ChatControlExecution(agent, False, ChatControlState(True, 0, "工作", 0, "session", "message", control)),
        parse_conversation_control("/stop"),
    )
    assert not result.ok and result.delivery_status == "unknown"
    assert cleaned.wait(3) and batches[0].pty_request_error == "OSError"
    assert [row["session_id"] for row in batches[0].receipt.records] == ["bg-committed"]


def test_stop_before_publication_closes_new_unstarted_attempt(main_task):
    agent, _link, _record, _scope, _store = main_task
    control = LocalRunControl("new-message")
    assert prepare_local_task_stop(agent, control) is None
    with pytest.raises(InterruptedError):
        _bind_main_agent_authority(agent, RunParams(
            task_id="new-task", run_id="new-run", request_id="new-message",
            conversation_task_binding_callback=control,
        ))
    row = agent.subagents.runtime_db.agent_run_for_run_id("new-run")
    attempt = agent.subagents.runtime_db.current_attempt(row["agent_run_id"])
    assert attempt["status"] == "cancelled"
    assert control.runtime_authority() == {}


def test_local_stop_waits_for_compact_publication_before_freezing(main_task, monkeypatch):
    agent, _link, record, scope, store = main_task
    control = _control(record)
    _write(store, scope, "bg-old")
    created, release, selected = threading.Event(), threading.Event(), threading.Event()
    output, failures = {}, []
    snapshot = control.runtime_authority

    def read_binding():
        value = snapshot()
        if threading.current_thread().name == "local-stop-test":
            selected.set()
        return value

    monkeypatch.setattr(control, "runtime_authority", read_binding)

    def compact():
        try:
            with agent.conversation_store.tasks.transition_guard("task"):
                fresh = agent.subagents.runtime_db.create_attempt(record["agent_run_id"])
                output["attempt"] = fresh["attempt_id"]
                created.set()
                assert release.wait(3)
                control.bind_runtime_authority({**snapshot(), "attempt_id": fresh["attempt_id"], "invocation_run_id": "message"})
                _write(store, replace(scope, attempt_id=fresh["attempt_id"]), "bg-current")
        except BaseException as exc:
            failures.append(exc)

    def stop():
        try:
            output["batch"] = prepare_local_task_stop(agent, control)
        except BaseException as exc:
            failures.append(exc)

    binder = threading.Thread(target=compact, daemon=True)
    stopper = threading.Thread(target=stop, name="local-stop-test", daemon=True)
    binder.start()
    try:
        assert created.wait(3)
        stopper.start()
        assert selected.wait(3)
    finally:
        release.set()
        binder.join(4)
        if stopper.ident is not None:
            stopper.join(4)
    assert not binder.is_alive() and not stopper.is_alive() and not failures, failures
    assert control.runtime_authority()["attempt_id"] == output["attempt"]
    assert agent.subagents.runtime_db.current_attempt(record["agent_run_id"])["status"] == "cancelled"
    assert {row["session_id"] for row in output["batch"].receipt.records} == {"bg-old", "bg-current"}


def test_old_local_binding_cannot_follow_a_resumed_run(main_task):
    agent, _link, record, scope, store = main_task
    control = _control(record)
    fresh = agent.subagents.runtime_db.create_attempt(record["agent_run_id"])
    _write(store, replace(scope, attempt_id=fresh["attempt_id"]), "bg-resumed")
    with pytest.raises(RuntimeCancellationConflict):
        prepare_local_task_stop(agent, control)
    assert not store.load("bg-resumed").record["stop_requested"]
    assert agent.subagents.runtime_db.current_attempt(record["agent_run_id"])["status"] != "cancelled"


@pytest.mark.parametrize("field,value", [("invocation_run_id", "other"), ("attempt_id", ""), ("task_id", "other"), ("run_id", "other")])
def test_binding_cannot_switch_identity_or_accept_incomplete_ids(main_task, field, value):
    control = _control(main_task[2])
    original = control.runtime_authority()
    with pytest.raises(ValueError):
        control.bind_runtime_authority({**original, "invocation_run_id": "message", field: value})
    assert control.runtime_authority() == original


def test_unmanaged_or_wrong_control_identity_is_not_reported_as_cleaned():
    agent = SimpleNamespace(subagents=SimpleNamespace(runtime_db=None))
    control = LocalRunControl("message")
    execution = ChatControlExecution(agent, False, ChatControlState(True, 0, "工作", 0, "session", "message", control))
    result = execute_chat_control(execution, parse_conversation_control("/stop"))
    assert not result.ok and result.delivery_status == "unknown"
    wrong = replace(execution, state=replace(execution.state, request_id="other"))
    assert execute_chat_control(wrong, parse_conversation_control("/stop")).error_code == "TASK_CONTROL_IDENTITY_CONFLICT"


@pytest.mark.parametrize("callback_kind", ["absent", "local", "unmanaged_local"])
def test_local_callback_preserves_existing_task_promotion_contract(main_task, callback_kind):
    from agent_py_agent.agent.conversation.task_promotion import promote_current_conversation_task

    agent, link, record, _scope, _store = main_task
    control = None if callback_kind == "absent" else _control(record)
    if callback_kind == "unmanaged_local":
        agent.subagents.runtime_db = None
        control = LocalRunControl("message")
    before = control.runtime_authority() if control else {}
    agent._current_run_params = RunParams(
        request_id="message", run_id="main", task_id="task", root_user_prompt="整理资料",
        task_attributes={"conversation_thread_id": link.thread_id, "conversation_task_id": "task"},
        conversation_task_binding_callback=control,
    )
    promoted = promote_current_conversation_task(agent)
    assert promoted is not None and promoted.task_id == "task"
    if control is not None:
        assert control.runtime_authority() == before
        control.finish()
        assert control(promoted) is False


def test_worker_job_change_finishes_only_old_control():
    cfg = SimpleNamespace(
        state_lock=threading.Lock(), pending_jobs_ref=[2], is_running_ref=[False], running_prompt_ref=[""],
        running_request_id_ref=[""], running_started_at_ref=[0], local_run_ref=[None], use_gateway=False,
    )
    _tui_update_running_state(cfg, SimpleNamespace(request_id="first", user="第一条"))
    first = cfg.local_run_ref[0]
    _reset_worker_refs(cfg)
    _tui_update_running_state(cfg, SimpleNamespace(request_id="second", user="第二条"))
    second = cfg.local_run_ref[0]
    assert first is not second and cfg.running_request_id_ref[0] == "second"
    first.request_interrupt()
    second.check_admission()
    with pytest.raises(InterruptedError):
        first.bind_runtime_authority({"invocation_run_id": "first", "task_id": "t", "run_id": "r", "agent_run_id": "a", "attempt_id": "g"})


def test_finished_handle_does_not_signal_a_later_registration_with_same_name():
    from agent_py_agent.agent.concurrency.interrupt import is_interrupted, register_interruptible

    old = LocalRunControl("message")
    old.finish()
    with register_interruptible("conversation-request:message"):
        assert old.request_interrupt() is False
        assert not is_interrupted()


@pytest.mark.parametrize("surface", ["plain", "tui"])
@pytest.mark.parametrize("interrupted", [False, True])
def test_local_worker_passes_original_callback_and_checks_start_admission(surface, interrupted):
    control = LocalRunControl("message")
    captured = []

    def run(_prompt, **kwargs):
        captured.append(kwargs)
        assert kwargs["params"].conversation_task_binding_callback is control
        return SimpleNamespace(response="", runtime_status="cancelled", runtime_reason="user_stop")

    agent = SimpleNamespace(config=SimpleNamespace(agent_name="test"), run=run)
    job = ChatJob(user="工作", show_prompt=False, inject=[], prompt_files=[], request_id="message")
    if interrupted:
        control.request_interrupt()

    def invoke():
        if surface == "plain":
            return _plain_local_handle(PlainJobContext(
                job, agent, SimpleNamespace(no_save=False), None, [], lambda: "", local_run=control,
            ))
        return _worker_local_path(SimpleNamespace(
            cfg=SimpleNamespace(agent=agent, local_run_ref=[control]), job=job, turn_inject=[], turn_adapter=None,
        ))

    if interrupted:
        with pytest.raises(InterruptedError):
            invoke()
        assert not captured
    else:
        invoke()
        assert len(captured) == 1 and captured[0]["request_id"] == "message"


@pytest.mark.parametrize("surface", ["plain", "tui"])
def test_worker_classifies_structured_interruption_without_failure(surface, monkeypatch, capsys):
    from agent_py_agent.cli.chat_parts import plain_worker, tui_worker
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime

    jobs = queue.Queue()
    job = ChatJob(user="工作", show_prompt=False, inject=[], prompt_files=[], request_id="message")
    jobs.put(job)
    cfg = SimpleNamespace(
        jobs=jobs, state_lock=threading.Lock(), pending_jobs_ref=[1], is_running_ref=[False],
        running_prompt_ref=[""], running_request_id_ref=[""], running_started_at_ref=[0], local_run_ref=[None],
        use_gateway=False, stop_event=threading.Event(), tui_runtime=TuiRuntime("session"),
    )
    summaries, controls = [], []

    def execute(*_args):
        controls.append(cfg.local_run_ref[0])
        cfg.stop_event.set()
        raise InterruptedError("控制请求已经关闭本轮")

    if surface == "tui":
        complete = cfg.tui_runtime.complete_turn

        def record(request_id, summary):
            summaries.append(summary)
            complete(request_id, summary)

        monkeypatch.setattr(cfg.tui_runtime, "complete_turn", record)
        monkeypatch.setattr(tui_worker, "_tui_process_job", execute)
        tui_worker._tui_worker_body(cfg)
        assert len(summaries) == 1 and summaries[0].interrupted and not summaries[0].error
    else:
        monkeypatch.setattr(plain_worker, "_plain_process_job", execute)
        get = jobs.get

        def get_once():
            if cfg.stop_event.is_set():
                raise EOFError("测试队列已结束")
            return get()

        monkeypatch.setattr(jobs, "get", get_once)
        with pytest.raises(EOFError):
            plain_worker._plain_worker(cfg)
        assert "错误" not in capsys.readouterr().out
    assert jobs.unfinished_tasks == 0 and cfg.local_run_ref[0] is None
    with pytest.raises(InterruptedError):
        controls[0].check_admission()
