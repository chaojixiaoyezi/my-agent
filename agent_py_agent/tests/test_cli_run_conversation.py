from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.cli_run_conversation import (
    CliRunConversationPersistenceError,
    bind_cli_run_conversation,
    persist_cli_run_assistant,
)
from agent_py_agent.agent.agent_core.run_task_workspace_writer import (
    attach_run_task_workspace_context,
    finish_run_task_workspace_if_needed,
)
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.owner_wake_discovery import unfinished_task_ids
from agent_py_agent.agent.settings import AgentConfig


class _RememberOnceBackend:
    name = "cli-run-remember-once"
    context_window_tokens = 200_000

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None, tools=None, messages=None) -> ModelResponse:
        del prompt, on_chunk, tools, messages
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    '{"tool":"remember","action":"add","content":"用户的阅读清单代号是雪松",'
                    '"kind":"fact","origin":"user_explicit",'
                    '"subject_key":"personal.reading-list-code",'
                    '"scope":{"scope_type":"personal","scope_key":"personal"}}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        return ModelResponse(text="已经按实际结果保存。", backend=self.name)


class _StaticBackend:
    name = "cli-run-static"
    context_window_tokens = 200_000

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None, tools=None, messages=None) -> ModelResponse:
        del prompt, on_chunk, tools, messages
        self.calls += 1
        return ModelResponse(text="本次运行已完成。", backend=self.name)


def _agent(tmp_path) -> SimpleAgent:
    return SimpleAgent(
        AgentConfig(
            model_backend="echo",
            tool_protocol="native",
            prompt_files=[],
            my_agent_home=str(tmp_path / "home"),
        ),
        tmp_path / "workspace",
    )


def test_cli_run_user_message_is_authoritative_before_remember(tmp_path) -> None:
    agent = _agent(tmp_path)
    backend = _RememberOnceBackend()
    agent.backend = backend

    result = agent.run(
        "请长期记住：我的阅读清单代号是雪松。",
        save=True,
        source="cli_run",
        request_id="request-cli-memory",
        run_id="run-cli-memory",
        task_id="task-cli-memory",
    )

    assert backend.calls == 2
    assert result.conversation_persist_degraded is False
    candidate = agent.memory_candidates.list()[0]
    assert candidate.status == "promoted"
    assert len(candidate.source_message_refs) == 1
    assert candidate.source_message_refs[0]["role"] == "user"
    assert len(agent.memory.all()) == 1

    threads = agent.conversation_store.list_threads(limit=0)
    assert len(threads) == 1
    rows = agent.conversation_store.recent_messages(threads[0].thread_id, limit=0)
    assert [row.role for row in rows] == ["user", "assistant"]
    assert [row.metadata["gateway_request_id"] for row in rows] == [
        "request-cli-memory",
        "request-cli-memory",
    ]
    assert agent.conversation_store.task_links(threads[0].thread_id) == []

    state_paths = list(agent.home_paths.owner_tasks_dir.rglob("work/state.json"))
    assert len(state_paths) == 1
    state = json.loads(state_paths[0].read_text(encoding="utf-8"))
    assert state["status"] == "DONE"
    assert unfinished_task_ids(agent.home_paths.owner_home_dir) == []
    repo = agent.subagents.runtime_db
    assert repo is not None
    with repo.transaction() as connection:
        status_conflicts = connection.execute(
            "SELECT COUNT(*) FROM runtime_events WHERE event_type = 'status_conflict'"
        ).fetchone()[0]
    assert status_conflicts == 0


def test_cli_run_transcript_append_is_idempotent_per_request_and_role(tmp_path) -> None:
    agent = _agent(tmp_path)
    params = RunParams(
        source="cli_run",
        request_id="request-cli-replay",
        run_id="run-cli-replay",
        task_id="task-cli-replay",
    )

    first = bind_cli_run_conversation(agent, params, "同一条用户输入")
    second = bind_cli_run_conversation(agent, first, "同一条用户输入")
    result = SimpleNamespace(
        response="同一条最终回复",
        operation_verification=None,
        conversation_persist_degraded=False,
        conversation_persist_error="",
    )
    assert persist_cli_run_assistant(agent, second, result)
    assert persist_cli_run_assistant(agent, second, result)

    thread_id = second.task_attributes["conversation_thread_id"]
    rows = agent.conversation_store.recent_messages(thread_id, limit=0)
    assert [row.role for row in rows] == ["user", "assistant"]
    assert "conversation_task_id" not in second.task_attributes


def test_cli_run_reused_request_rejects_different_run_lineage(tmp_path) -> None:
    agent = _agent(tmp_path)
    first = RunParams(
        source="cli_run",
        request_id="request-cli-lineage",
        run_id="run-cli-lineage-one",
        task_id="task-cli-lineage-one",
    )
    second = RunParams(
        source="cli_run",
        request_id="request-cli-lineage",
        run_id="run-cli-lineage-two",
        task_id="task-cli-lineage-two",
    )

    bind_cli_run_conversation(agent, first, "同一条用户输入")

    with pytest.raises(CliRunConversationPersistenceError):
        bind_cli_run_conversation(agent, second, "同一条用户输入")


def test_cli_run_user_persistence_failure_stops_before_model(tmp_path, monkeypatch) -> None:
    agent = _agent(tmp_path)
    backend = _StaticBackend()
    agent.backend = backend

    def fail_append(*_args, **_kwargs):
        raise OSError("transcript unavailable")

    monkeypatch.setattr(agent.conversation_store, "append_message_once", fail_append)

    with pytest.raises(CliRunConversationPersistenceError):
        agent.run(
            "这条输入不能在无证据时继续。",
            save=False,
            source="cli_run",
            request_id="request-cli-user-failure",
            run_id="run-cli-user-failure",
            task_id="task-cli-user-failure",
        )

    assert backend.calls == 0


def test_cli_run_assistant_persistence_failure_is_typed_degradation(
    tmp_path,
    monkeypatch,
) -> None:
    agent = _agent(tmp_path)
    backend = _StaticBackend()
    agent.backend = backend
    original = agent.conversation_store.append_message_once
    calls = {"count": 0}

    def fail_second_append(request, *, dedupe_key):
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("assistant transcript unavailable")
        return original(request, dedupe_key=dedupe_key)

    monkeypatch.setattr(
        agent.conversation_store,
        "append_message_once",
        fail_second_append,
    )

    result = agent.run(
        "执行一次普通 CLI 任务。",
        save=False,
        source="cli_run",
        request_id="request-cli-assistant-failure",
        run_id="run-cli-assistant-failure",
        task_id="task-cli-assistant-failure",
    )

    assert backend.calls == 1
    assert result.conversation_persist_degraded is True
    assert "assistant transcript append failed" in result.conversation_persist_error
    thread = agent.conversation_store.list_threads(limit=0)[0]
    rows = agent.conversation_store.recent_messages(thread.thread_id, limit=0)
    assert [row.role for row in rows] == ["user"]


def test_cli_run_with_conversation_identity_still_closes_standalone_workspace(
    tmp_path,
) -> None:
    agent = _agent(tmp_path)
    params = RunParams(
        save=True,
        source="cli_run",
        request_id="request-cli-workspace",
        run_id="run-cli-workspace",
        task_id="task-cli-workspace",
        task_attributes={
            "conversation_thread_id": "thread-cli-workspace",
            "conversation_task_id": "task-cli-workspace",
        },
    )
    bound = attach_run_task_workspace_context(agent, params, "创建并收口 CLI 工作区")

    finished = finish_run_task_workspace_if_needed(
        agent,
        bound,
        SimpleNamespace(runtime_status="ok", runtime_reason="", runtime_source=""),
    )

    workspace_root = bound.task_attributes["run_workspace"]["task_root"]
    state = json.loads(
        (Path(workspace_root) / "work/state.json").read_text(encoding="utf-8")
    )
    assert finished == workspace_root
    assert state["status"] == "DONE"


def test_gateway_conversation_identity_keeps_existing_workspace_skip(tmp_path) -> None:
    agent = _agent(tmp_path)
    params = RunParams(
        save=True,
        source="gateway",
        request_id="request-gateway-workspace",
        run_id="run-gateway-workspace",
        task_id="task-gateway-workspace",
        task_attributes={
            "conversation_thread_id": "thread-gateway-workspace",
            "conversation_task_id": "task-gateway-workspace",
        },
    )
    bound = attach_run_task_workspace_context(agent, params, "Gateway 会话任务")

    assert (
        finish_run_task_workspace_if_needed(
            agent,
            bound,
            SimpleNamespace(runtime_status="ok", runtime_reason="", runtime_source=""),
        )
        == ""
    )
    workspace_root = bound.task_attributes["run_workspace"]["task_root"]
    state = json.loads(
        (Path(workspace_root) / "work/state.json").read_text(encoding="utf-8")
    )
    assert state["status"] == "RUNNING"


def test_cli_run_continuation_round_is_structured_internal_event(tmp_path) -> None:
    """缺口F(双席复核 seq1835): 续跑轮消息是 typed continuation event——
    role=system(不伪装成普通用户请求), metadata 显式记 is_continuation/
    continuation_seq/continuation_reason/continuation_parent_attempt_id;
    首轮用户原文 role=user 不受影响, dedupe_key 按轮派生互不破坏。"""
    agent = _agent(tmp_path)
    first_params = RunParams(
        source="cli_run",
        request_id="request-root",
        run_id="run-root",
        task_id="task-root",
    )
    bound_first = bind_cli_run_conversation(agent, first_params, "原始用户请求")
    first_thread_id = bound_first.task_attributes["conversation_thread_id"]

    resume_params = RunParams(
        source="cli_run",
        request_id="run-root#cont-1",
        run_id="run-root",
        task_id="task-root",
        continuation_seq=1,
        continuation_root_task_id="task-root",
        continuation_root_run_id="run-root",
        continuation_root_thread_id=first_thread_id,
        continuation_root_request_id="request-root",
        continuation_parent_attempt_id="attempt-0",
        continuation_reason="TOOL_ROUND_LIMIT_REACHED",
    )
    bound_resume = bind_cli_run_conversation(
        agent, resume_params, "【系统续跑 #1】上一轮因 TOOL_ROUND_LIMIT_REACHED 收口…"
    )
    assert (
        bound_resume.task_attributes["conversation_thread_id"] == first_thread_id
    )  # 同一 thread 复用

    rows = agent.conversation_store.recent_messages(first_thread_id, limit=0)
    assert [row.role for row in rows] == ["user", "system"]  # 首轮 user + 续跑 system
    first_entry, resume_entry = rows
    # 首轮 user 原文不动
    assert first_entry.content == "原始用户请求"
    assert first_entry.metadata["dedupe_key"] == "cli_run:request-root:user"
    assert "is_continuation" not in first_entry.metadata
    # 续跑轮 typed continuation event
    assert resume_entry.content.startswith("【系统续跑 #1】")
    assert resume_entry.metadata["is_continuation"] is True
    assert resume_entry.metadata["continuation_seq"] == 1
    assert resume_entry.metadata["continuation_reason"] == "TOOL_ROUND_LIMIT_REACHED"
    assert resume_entry.metadata["continuation_parent_attempt_id"] == "attempt-0"
    assert resume_entry.metadata["dedupe_key"] == "cli_run:run-root#cont-1:system"
    # 结构化身份仍可反查
    assert resume_entry.metadata["gateway_request_id"] == "run-root#cont-1"
    assert resume_entry.metadata["run_id"] == "run-root"
    assert resume_entry.metadata["task_id"] == "task-root"


def test_cli_run_continuation_event_not_consumed_as_user_request(tmp_path) -> None:
    """缺口F(双席复核 seq1835): 续跑 system 事件不被当成用户请求——
    role=user 是记忆/curator 等消费方的用户证据过滤条件, 续跑消息
    role=system 时按 role=user 收集的用户消息数不因续跑轮增加。"""
    agent = _agent(tmp_path)
    first_params = RunParams(
        source="cli_run",
        request_id="request-root-b",
        run_id="run-root-b",
        task_id="task-root-b",
    )
    bound_first = bind_cli_run_conversation(agent, first_params, "用户原文")
    first_thread_id = bound_first.task_attributes["conversation_thread_id"]

    resume_params = RunParams(
        source="cli_run",
        request_id="run-root-b#cont-1",
        run_id="run-root-b",
        task_id="task-root-b",
        continuation_seq=1,
        continuation_root_task_id="task-root-b",
        continuation_root_run_id="run-root-b",
        continuation_root_thread_id=first_thread_id,
        continuation_root_request_id="request-root-b",
        continuation_parent_attempt_id="attempt-0",
        continuation_reason="TOOL_ROUND_LIMIT_REACHED",
    )
    bind_cli_run_conversation(agent, resume_params, "【系统续跑 #1】…")

    rows = agent.conversation_store.recent_messages(first_thread_id, limit=0)
    user_roles = [row for row in rows if row.role == "user"]
    assert len(user_roles) == 1  # 只有首轮用户原文, 续跑轮不增 user
    assert user_roles[0].content == "用户原文"
