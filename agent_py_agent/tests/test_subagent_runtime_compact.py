from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.model.context_pressure import (
    preflight_context_pressure_response,
)
from agent_py_agent.agent.agent_core.models import AgentRunResult
from agent_py_agent.agent.agent_core.runtime.owner_roots import runtime_scope_root
from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.conversation.agent_thread import (
    AgentThreadTurnInput,
    prepare_subagent_thread_turn,
)
from agent_py_agent.agent.conversation.agent_transcript import (
    read_agent_transcript_events,
)
from agent_py_agent.agent.conversation.authority import (
    CONVERSATION_EXECUTION_CWD_ATTR,
    CONVERSATION_RUNTIME_WORKSPACE_ROOTS_ATTR,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.prompting_parts.cache_layout import prompt_cache_layout
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
        self.model_messages: list[list[dict]] = []
        self.model_kwargs: list[dict[str, object]] = []
        self.summary_prompts: list[str] = []
        self.summary_kwargs: list[dict[str, object]] = []

    def generate(self, prompt: str, on_chunk=None, **kwargs):
        if "You maintain a conversation summary" in prompt:
            self.summary_prompts.append(prompt)
            self.summary_kwargs.append(dict(kwargs))
            return ModelResponse(
                text="旧轮次已完成现状核对；继续执行当前子任务。",
                backend=self.name,
            )
        self.model_prompts.append(prompt)
        self.model_messages.append(list(kwargs.get("messages") or []))
        self.model_kwargs.append(dict(kwargs))
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


class _WriteThenCompleteChildBackend(_OverflowThenCompleteChildBackend):
    name = "write-then-complete-child"

    def __init__(self) -> None:
        super().__init__(overflow_once=False)

    def generate(self, prompt: str, on_chunk=None, **kwargs):
        self.model_prompts.append(prompt)
        self.model_messages.append(list(kwargs.get("messages") or []))
        self.model_kwargs.append(dict(kwargs))
        if len(self.model_prompts) == 1:
            if on_chunk is not None:
                on_chunk("我先写入子代理负责的文件。")
            return ModelResponse(
                text="我先写入子代理负责的文件。",
                backend=self.name,
                tool_use_blocks=[
                    {
                        "id": "call-child-write-1",
                        "name": "write_file",
                        "input": {
                            "path": "child-owned.txt",
                            "content": "child output\n",
                        },
                    }
                ],
            )
        return ModelResponse(text="子代理写入完成。", backend=self.name)


@pytest.mark.parametrize("error_type", [InterruptedError, ValueError])
def test_child_failure_keeps_native_history_in_own_thread(tmp_path, error_type):
    from agent_py_agent.agent.conversation.native_history import provider_history_messages_from_rows

    class WriteThenFail(_WriteThenCompleteChildBackend):
        def generate(self, prompt, on_chunk=None, **kwargs):
            if self.model_prompts:
                raise error_type("test child failure after tool")
            return super().generate(prompt, on_chunk, **kwargs)

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    parent = agent.conversation_store.threads.get_or_create({"channel": "chat", "channel_conversation_id": "parent"})
    workspace = agent.home_paths.owner_home_dir / "project"
    workspace.mkdir(parents=True)
    agent.conversation_store.tasks.bind({"thread_id": parent.thread_id, "task_id": "parent-task", "status": "active", "task_path": str(workspace)})
    child = agent.subagents.create_run(goal="整理本地文件", root_id="parent-task", role="worker",
        allowed_tools=["write_file"], extra_write_roots=[str(workspace)], attributes={
            "conversation_thread_id": parent.thread_id, "conversation_task_id": "parent-task",
            CONVERSATION_EXECUTION_CWD_ATTR: str(workspace), CONVERSATION_RUNTIME_WORKSPACE_ROOTS_ATTR: [str(workspace)],
        })
    agent.backend = WriteThenFail()
    result = agent.run_subagent(child.id, dry_run=False, probe=False)
    assert not result.ok
    rows = agent.conversation_store.messages.recent(child.agent_thread_id, limit=0)
    native = provider_history_messages_from_rows(rows)
    assert "call-child-write-1" in str(native)
    assert any("tool_result" in str(row) for row in native)
    assert rows[-1].content == ""
    assert rows[-1].metadata["turn_end_reason"] == ("aborted" if error_type is InterruptedError else "error")
    assert agent.conversation_store.messages.recent(parent.thread_id, limit=0) == []


class _OverflowThenListThenCompleteChildBackend(_OverflowThenCompleteChildBackend):
    name = "overflow-list-then-complete-child"

    def generate(self, prompt: str, on_chunk=None, **kwargs):
        if "You maintain a conversation summary" in prompt:
            self.summary_prompts.append(prompt)
            self.summary_kwargs.append(dict(kwargs))
            return ModelResponse(
                text="旧轮次已压缩；继续使用同一子代理执行权。",
                backend=self.name,
            )
        self.model_prompts.append(prompt)
        self.model_messages.append(list(kwargs.get("messages") or []))
        self.model_kwargs.append(dict(kwargs))
        if len(self.model_prompts) == 1:
            return ModelResponse(
                text="provider reported context pressure",
                backend=self.name,
                runtime_status="context_overflow",
                runtime_reason="context_overflow",
                runtime_source="provider_error",
                usage={"input_tokens": 90_000, "output_tokens": 10},
            )
        if len(self.model_prompts) == 2:
            return ModelResponse(
                text="压缩后继续读取当前工作区。",
                backend=self.name,
                tool_use_blocks=[
                    {
                        "id": "call-child-list-after-compact",
                        "name": "list_files",
                        "input": {"path": "."},
                    }
                ],
            )
        return ModelResponse(text="压缩后工具调用成功，子任务完成。", backend=self.name)


class _ActiveTurnSummaryChildBackend:
    """只承担测试里的 active-turn Compact 摘要调用。"""

    name = "active-turn-summary-child"
    context_window_tokens = 128_000

    def generate(self, _prompt: str, on_chunk=None, **_kwargs):
        del on_chunk
        return ModelResponse(
            text=(
                "[compact-live-handoff.v1]\n"
                "current_progress: 已完成多项工具核对，正在继续同一个子任务。\n"
                "user_constraints: 保持原任务范围与当前工作目录，不重复已经成功的副作用。\n"
                "completed: 已读取并核对五个输入文件，精确调用记录保存在 owner archive。\n"
                "failures: none。\n"
                "unresolved: 仍需形成最终结论并回复直接父代理。\n"
                "next_step: 基于当前文件和保留的近期记录完成汇总。"
            ),
            backend=self.name,
            usage={"input_tokens": 2_000, "output_tokens": 180},
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
    thread = agent.conversation_store.threads.load(task.agent_thread_id)
    assert thread is not None
    agent.conversation_store.messages.append(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": "上一轮先核对已有项目状态。",
            "metadata": {"conversation_request_id": "prior-attempt"},
        }
    )
    agent.conversation_store.messages.append(
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

    updated = agent.conversation_store.threads.load(task.agent_thread_id)
    messages, errors = agent.conversation_store.messages.recent_report(
        task.agent_thread_id,
        limit=0,
    )
    assert result.ok
    assert len(backend.model_prompts) == 2
    assert len(backend.summary_prompts) == 1
    summary_layout = prompt_cache_layout(backend.summary_prompts[0])
    resumed_layout = prompt_cache_layout(backend.model_prompts[-1])
    assert summary_layout is not None and resumed_layout is not None
    assert summary_layout.stable_prefix == resumed_layout.stable_prefix
    assert backend.summary_kwargs[0]["tools"] == backend.model_kwargs[-1]["tools"]
    assert backend.summary_kwargs[0]["messages"]
    assert "# Agent Thread Context" not in backend.model_prompts[-1]
    assert "旧轮次已完成现状核对" in json.dumps(
        backend.model_messages[-1],
        ensure_ascii=False,
    )
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


@pytest.mark.parametrize("overflow_count", [1, 9])
def test_subagent_provider_overflow_compacts_unfinished_tool_archive_before_retry(
    tmp_path: Path,
    monkeypatch,
    overflow_count: int,
) -> None:
    """有新工具进展的 child 可跨过旧的 8 次上限，始终在原尝试推进正式代次。"""

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
        goal="在长工具链发生上下文压力后继续完成子任务",
        thought="使用当前子代理自己的 Compact 账本续接。",
        plan=["读取输入", "形成结论"],
        role="worker",
    )
    records = [
        {
            "call_id": f"call-active-{index}",
            "scoped_call_id": f"run:call-active-{index}",
            "tool": "read_file",
            "ok": True,
            "model_parameters": {
                "tool": "read_file",
                "path": f"input-{index}.txt",
            },
            "output_preview": f"result-{index}",
            "effect_outcome": "succeeded",
        }
        for index in range(1, 5 * overflow_count + 1)
    ]
    agent.backend = _ActiveTurnSummaryChildBackend()
    run_params_seen = []

    def fake_run(prompt: str, *, params):
        run_params_seen.append(params)
        if len(run_params_seen) <= overflow_count:
            params.on_chunk.write_progress({
                "round": 1,
                "call_index": 1,
                "tool": "read_file",
                "phase": "completed",
                "ok": True,
                "output": f"第 {len(run_params_seen)} 批工具结果",
            })
            return AgentRunResult(
                prompt=prompt,
                response="provider reported context pressure",
                backend=agent.backend.name,
                used_memories=0,
                archive_tool_calls=records[:5 * len(run_params_seen)],
                runtime_status="context_overflow",
                runtime_reason="context_overflow",
                runtime_source="preflight",
                turn_end_reason="max-tokens",
            )
        return AgentRunResult(
            prompt=prompt,
            response="子代理已基于压缩后的上下文完成结论。",
            backend=agent.backend.name,
            used_memories=0,
            archive_tool_calls=records,
            runtime_status="ok",
            turn_end_reason="completed",
        )

    monkeypatch.setattr(agent, "run", fake_run)

    result = agent.run_subagent(task.id, dry_run=False, probe=False)

    thread = agent.conversation_store.threads.load(task.agent_thread_id)
    assert result.ok
    assert len(run_params_seen) == overflow_count + 1
    assert len({params.attempt_id for params in run_params_seen}) == 1
    assert {params.run_id for params in run_params_seen} == {task.id}
    assert thread is not None and thread.compact_generation == overflow_count
    assert thread.compact_checkpoint_id
    assert runtime_compact_count(task, agent.conversation_store) == overflow_count
    assert run_params_seen[1].conversation_history_seed.compact_generation == 1
    assert run_params_seen[-1].conversation_history_seed.compact_generation == overflow_count
    assert run_params_seen[-1].carried_archive_tool_calls == records
    events = read_agent_transcript_events(agent, run_id=task.id, after=0)["events"]
    completed = [
        row
        for row in events
        if row.get("kind") == "conversation_compaction_completed"
    ]
    assert len(completed) == overflow_count
    assert completed[-1]["payload"]["source_kind"] == "active_turn_tool_archive"
    tools = [row for row in events if row.get("kind") == "tool_completed"]
    assert len(tools) == overflow_count
    assert len({row["block_id"] for row in tools}) == overflow_count


def test_subagent_overflow_without_compactable_progress_still_fails(tmp_path, monkeypatch):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path
    )
    task = agent.subagents.create_run(
        goal="核对输入", thought="读取真实材料", plan=["核对"], role="worker"
    )
    model_calls = []

    def overflow_without_progress(prompt, *, params):
        model_calls.append(params)
        return AgentRunResult(
            prompt=prompt,
            response="",
            backend="echo",
            used_memories=0,
            runtime_status="context_overflow",
            runtime_reason="context_overflow",
            runtime_source="provider_error",
            turn_end_reason="max-tokens",
        )

    monkeypatch.setattr(agent, "run", overflow_without_progress)

    result = agent.run_subagent(task.id, dry_run=False, probe=False)

    assert not result.ok
    assert len(model_calls) == 1
    ended = agent.subagents.load(task.id)
    assert ended.status == "FAILED"
    assert ended.failure_type == "runner_error"
    assert "cannot compact the overflowing active turn" in ended.runner_last_error
    assert agent.conversation_store.threads.load(task.agent_thread_id).compact_generation == 0


def test_subagent_compact_retry_keeps_exact_attempt_authorized_for_later_tools(
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
        goal="上下文压缩后继续调用工具并完成子任务",
        thought="同一 attempt 内续接。",
        plan=["触发压缩", "继续调用工具", "完成"],
        role="worker",
        allowed_tools=["list_files"],
    )
    for role, content in (
        ("user", "上一轮已经完成了初始目录核对。"),
        ("assistant", "目录事实已经记录，可以继续后续工具步骤。"),
    ):
        agent.conversation_store.messages.append(
            {
                "thread_id": task.agent_thread_id,
                "role": role,
                "content": content,
                "metadata": {"conversation_request_id": "prior-attempt"},
            }
        )
    backend = _OverflowThenListThenCompleteChildBackend()
    agent.backend = backend

    result = agent.run_subagent(task.id, dry_run=False, probe=False)

    updated = agent.subagents.load(task.id)
    ledger = updated.attributes.get("tool_failure_ledger", {})
    assert result.ok
    assert len(backend.model_prompts) == 3
    assert updated.status == "DONE"
    assert ledger.get("failures") == []
    assert "TOOL_AUTHORITY_CONTEXT_MISSING" not in updated.result


def test_child_transcript_thread_does_not_rebind_parent_conversation_task(
    tmp_path: Path,
) -> None:
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            enable_tools=True,
            my_agent_home=str(tmp_path / "home"),
        ),
        tmp_path,
    )
    workspace = agent.home_paths.owner_home_dir / "project"
    workspace.mkdir(parents=True)
    parent_thread = agent.conversation_store.threads.get_or_create(
        {
            "canonical_user_id": "local/main",
            "channel": "tui",
            "channel_conversation_id": "parent-chat",
            "channel_user_id": "local-user",
            "cwd": str(workspace),
        }
    )
    parent_task_id = "root-conversation-task"
    agent.conversation_store.tasks.bind(
        {
            "thread_id": parent_thread.thread_id,
            "task_id": parent_task_id,
            "goal": "parent task",
            "status": "active",
            "task_path": str(workspace),
        }
    )
    task = agent.subagents.create_run(
        goal="write one child-owned file",
        thought="use the child runner",
        plan=["write", "finish"],
        role="worker",
        root_id=parent_task_id,
        allowed_tools=["write_file"],
        extra_write_roots=[str(workspace)],
        attributes={
            "conversation_thread_id": parent_thread.thread_id,
            "conversation_task_id": parent_task_id,
            CONVERSATION_EXECUTION_CWD_ATTR: str(workspace),
            CONVERSATION_RUNTIME_WORKSPACE_ROOTS_ATTR: [str(workspace)],
        },
    )
    agent.backend = _WriteThenCompleteChildBackend()

    result = agent.run_subagent(task.id, dry_run=False, probe=False)

    parent_link = agent.conversation_store.tasks.load(parent_task_id)
    child_thread = agent.conversation_store.threads.load(task.agent_thread_id)
    child_rows = agent.conversation_store.messages.recent(task.agent_thread_id, limit=0)
    assert result.ok
    assert (workspace / "child-owned.txt").read_text(encoding="utf-8") == "child output\n"
    assert parent_link is not None
    assert parent_link.thread_id == parent_thread.thread_id
    assert child_thread is not None
    assert [row.role for row in child_rows] == ["user", "assistant", "assistant"]
    assert [row.metadata.get("assistant_part_id") for row in child_rows[1:]] == [
        "commentary:1",
        "final",
    ]
    assert child_rows[1].content == "我先写入子代理负责的文件。"
    assert child_rows[-1].metadata["terminal_tool_fold"]["tool_call_count"] == 1

    followup = prepare_subagent_thread_turn(
        agent,
        task,
        turn=AgentThreadTurnInput("继续核对刚才写入的文件", "followup-attempt"),
    )

    assert followup.compact_generation == 0
    assert followup.history_seed is not None
    history = json.dumps(followup.history_seed.messages, ensure_ascii=False)
    assert "conversation-terminal-tool-fold" in history
    assert "我先写入子代理负责的文件。" in history
    assert "write_file" in history
    assert "call-child-write-1" in history


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
        agent.conversation_store.messages.append(
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

    thread = agent.conversation_store.threads.load(task.agent_thread_id)
    assert result.ok
    # 300K 字符源超过 64K 窗口，必须分段而不是单次超窗发送；每段禁用执行工具，全部覆盖后仅提交一代。
    assert len(backend.summary_prompts) > 1
    assert all(k["tools"] == [] and k["tool_choice"].mode == "none" for k in backend.summary_kwargs)
    source = "".join(k["messages"][0]["content"][0]["text"].split("]：\n", 1)[1]
                     for k in backend.summary_kwargs)
    assert "old requirement " + "x" * 150_000 in source
    assert "old completed work " + "y" * 150_000 in source
    assert len(backend.model_prompts) == 1
    assert "Earlier Conversation Summary (generation 1)" in json.dumps(
        backend.model_messages[0],
        ensure_ascii=False,
    )
    assert thread is not None
    assert thread.compact_generation == 1
    progress_page = read_agent_transcript_events(agent, run_id=task.id, after=0)
    compact_events = [
        row
        for row in progress_page["events"]
        if str(row.get("kind") or "").startswith("conversation_compaction_")
    ]
    assert compact_events[0]["kind"] == "conversation_compaction_started"
    assert compact_events[-1]["kind"] == "conversation_compaction_completed"
    assert all(
        row["kind"] == "conversation_compaction_progress"
        for row in compact_events[1:-1]
    )
    assert len(compact_events) >= 3
    operation_ids = {
        str(row.get("payload", {}).get("operation_id") or "")
        for row in compact_events
    }
    assert len(operation_ids) == 1
    assert next(iter(operation_ids)).startswith("transcript:")


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

    parent_thread = agent.conversation_store.threads.load(parent.agent_thread_id)
    child_thread = agent.conversation_store.threads.load(child.agent_thread_id)
    assert parent_thread is not None
    assert child_thread is not None
    assert parent_thread.thread_id != child_thread.thread_id
    assert child_thread.metadata["parent_agent_thread_id"] == parent_thread.thread_id
    assert child_thread.metadata["root_agent_thread_id"] == parent_thread.thread_id
    assert child_thread.metadata["agent_run_id"] == child.id
    assert parent_thread.channel_bindings == ()
    assert child_thread.channel_bindings == ()

    agent.conversation_store.messages.append(
        {
            "thread_id": parent_thread.thread_id,
            "role": "assistant",
            "content": "parent-only history",
        }
    )
    child_rows, child_errors = agent.conversation_store.messages.recent_report(
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
