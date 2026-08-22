from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.model.context_pressure import (
    preflight_context_pressure_response,
)
from agent_py_agent.agent.agent_core.runtime.owner_roots import runtime_scope_root
from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents.services.control_plane_projection import (
    runtime_compact_count,
)
from agent_py_agent.tests._tool_runtime_harness import make_test_protocol_snapshot


class _OverflowThenCompleteChildBackend:
    name = "overflow-then-complete-child"
    context_window_tokens = 128_000

    def probe_tool_capability(self):
        from agent_py_agent.agent.backends.base import ProviderToolCapability, _utc_now_iso

        return ProviderToolCapability(
            provider=self.name, endpoint="local://child-thread", model="",
            stream=False, native_supported=True,
            evidence="test_backend_declares_native_tools",
            observed_at=_utc_now_iso(),
        )

    def __init__(self, *, overflow_once: bool = True):
        self.overflow_once = overflow_once
        self.model_prompts: list[str] = []
        self.summary_prompts: list[str] = []

    def generate(self, prompt: str, on_chunk=None, **kwargs):
        if prompt.startswith("You maintain a conversation summary"):
            self.summary_prompts.append(prompt)
            return ModelResponse(
                text="旧轮次已完成现状核对；继续执行当前子任务。",
                backend=self.name,
            )
        self.model_prompts.append(prompt)
        if self.overflow_once and len(self.model_prompts) == 1:
            return ModelResponse(
                text="provider reported context pressure",
                backend=self.name,
                runtime_status="context_overflow",
                runtime_reason="context_overflow",
                runtime_source="provider_error",
                usage={"input_tokens": 90_000, "output_tokens": 10},
            )
        return ModelResponse(
            text=(
                "[SUBAGENT_RESULT]\n"
                + json.dumps(
                    {
                        "status": "DONE",
                        "summary": "child completed after ConversationThread Compact",
                        "used_tools": [],
                        "used_skills": [],
                        "evidence": [],
                        "capability_requests": [],
                        "artifacts": [],
                        "tests": [],
                        "patches": [],
                        "lessons": [],
                        "next_actions": [],
                        "blocked_reason": "",
                        "failure_type": "",
                    },
                    ensure_ascii=False,
                )
                + "\n[/SUBAGENT_RESULT]"
            ),
            backend=self.name,
            usage={"input_tokens": 500, "output_tokens": 100},
        )


def test_subagent_uses_own_conversation_thread_for_forced_compact_and_retry(
    tmp_path: Path,
) -> None:
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            enable_tools=True,
            my_agent_home=str(tmp_path / "home"),
            tool_context_ptl_retry_max=0,
            model_context_window_tokens=128_000,
            memory_compact_auto_trigger_percent=90,
        ),
        tmp_path,
    )
    task = agent.subagents.create_run(
        goal="在供应商报告上下文压力后继续完成子任务",
        thought="使用独立会话线程恢复。",
        plan=["核对旧轮次", "继续完成"],
        role="worker",
    )
    run_home = Path(task.agent_run_workspace_dir)
    thread = agent.conversation_store.load_thread(task.agent_thread_id)
    assert thread is not None
    agent.conversation_store.append_message(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": "上一轮先核对已有项目状态。",
            "metadata": {"conversation_request_id": "prior-attempt"},
        }
    )
    agent.conversation_store.append_message(
        {
            "thread_id": thread.thread_id,
            "role": "assistant",
            "content": "已完成状态核对，下一轮继续实现。",
            "metadata": {"conversation_request_id": "prior-attempt"},
        }
    )
    backend = _OverflowThenCompleteChildBackend()
    agent.backend = backend
    owner_memory_before = _file_bytes(agent.memory.path)

    result = agent.run_subagent(task.id, dry_run=False, probe=False)

    updated = agent.conversation_store.load_thread(task.agent_thread_id)
    messages, errors = agent.conversation_store.recent_messages_report(
        task.agent_thread_id,
        limit=0,
    )
    assert result.ok
    assert len(backend.model_prompts) == 2
    assert len(backend.summary_prompts) == 1
    assert "# Agent Thread Context" in backend.model_prompts[-1]
    assert "旧轮次已完成现状核对" in backend.model_prompts[-1]
    assert updated is not None
    assert updated.compact_generation == 1
    assert updated.compact_checkpoint_id
    assert runtime_compact_count(task, agent.conversation_store) == 1
    assert [row.role for row in messages] == ["user", "assistant", "user", "assistant"]
    assert errors == []
    assert not list(run_home.rglob("compact_applies"))
    assert _file_bytes(agent.memory.path) == owner_memory_before
    assert not (run_home / "compactions").exists()
    assert not (run_home / "recovery").exists()


def test_subagent_preflight_compacts_large_completed_history_before_sampling(
    tmp_path: Path,
) -> None:
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
            model_context_window_tokens=64_000,
            memory_compact_auto_trigger_percent=90,
        ),
        tmp_path,
    )
    task = agent.subagents.create_run(
        goal="continue a long delegated task",
        thought="resume exact child history",
        plan=["continue"],
        role="worker",
    )
    for role, content in (
        ("user", "old requirement " + ("x" * 150_000)),
        ("assistant", "old completed work " + ("y" * 150_000)),
    ):
        agent.conversation_store.append_message(
            {
                "thread_id": task.agent_thread_id,
                "role": role,
                "content": content,
                "metadata": {"conversation_request_id": "large-prior-attempt"},
            }
        )
    backend = _OverflowThenCompleteChildBackend(overflow_once=False)
    backend.context_window_tokens = 64_000
    agent.backend = backend

    result = agent.run_subagent(task.id, dry_run=False, probe=False)

    thread = agent.conversation_store.load_thread(task.agent_thread_id)
    assert result.ok
    assert len(backend.summary_prompts) == 1
    assert len(backend.model_prompts) == 1
    assert "Earlier Agent Summary (generation 1)" in backend.model_prompts[0]
    assert thread is not None
    assert thread.compact_generation == 1


def test_child_and_grandchild_materialize_independent_agent_threads(
    tmp_path: Path,
) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")),
        tmp_path,
    )
    parent = agent.subagents.create_run(
        goal="parent work",
        thought="delegate",
        plan=["coordinate"],
        role="coordinator",
    )
    child = agent.subagents.create_run(
        goal="child work",
        thought="execute",
        plan=["work"],
        role="worker",
        parent_id=parent.id,
        root_id=parent.id,
        depth=1,
    )

    parent_thread = agent.conversation_store.load_thread(parent.agent_thread_id)
    child_thread = agent.conversation_store.load_thread(child.agent_thread_id)
    assert parent_thread is not None
    assert child_thread is not None
    assert parent_thread.thread_id != child_thread.thread_id
    assert child_thread.metadata["parent_agent_thread_id"] == parent_thread.thread_id
    assert child_thread.metadata["root_agent_thread_id"] == parent_thread.thread_id
    assert child_thread.metadata["agent_run_id"] == child.id
    assert parent_thread.channel_bindings == ()
    assert child_thread.channel_bindings == ()

    agent.conversation_store.append_message(
        {
            "thread_id": parent_thread.thread_id,
            "role": "assistant",
            "content": "parent-only history",
        }
    )
    child_rows, child_errors = agent.conversation_store.recent_messages_report(
        child_thread.thread_id,
        limit=0,
    )
    assert child_rows == []
    assert child_errors == []


def test_task_local_preflight_uses_the_configured_exact_compact_threshold(
    monkeypatch,
    tmp_path: Path,
) -> None:
    run_home = tmp_path / "task" / "work" / "agents" / "run-1"
    run_home.mkdir(parents=True)
    agent = SimpleNamespace(
        config=AgentConfig(
            auto_save_memory=True,
            memory_compact_auto_trigger_percent=90,
            model_context_window_tokens=1_000,
        ),
        backend=SimpleNamespace(context_window_tokens=1_000, name="fake"),
    )
    params = SimpleNamespace(
        context_scope="task_local",
        task_attributes={"agent_run_workspace_dir": str(run_home)},
        live_archive_state={},
        tool_protocol_snapshot=make_test_protocol_snapshot(),
    )
    request = SimpleNamespace(agent=agent, params=params, prompt="child prompt", tool_rounds=0)

    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.model.context_pressure.estimate_tokens",
        lambda _prompt: 899,
    )
    assert preflight_context_pressure_response(request) is None

    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.model.context_pressure.estimate_tokens",
        lambda _prompt: 900,
    )
    response = preflight_context_pressure_response(request)

    assert response is not None
    assert response.runtime_status == "context_overflow"
    assert "compact_threshold=900" in response.text


def test_task_local_storage_fails_closed_without_a_child_run_home(tmp_path: Path) -> None:
    agent = SimpleNamespace(root=tmp_path)

    with pytest.raises(RuntimeError, match="refusing to fall back to owner storage"):
        runtime_scope_root(
            agent,
            context_scope="task_local",
            task_attributes={},
        )


def _file_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        return b""
