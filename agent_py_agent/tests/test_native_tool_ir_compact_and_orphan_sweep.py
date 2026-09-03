from __future__ import annotations

"""Step 3/4 测试：native 下 compact 对 IR「整对」增删 + 出站孤儿净化。

Step 3（compact 在 native 下整对操作 IR）：
- window（完整请求 token 预算）回收最旧工具往返 → IR 整对摘除，出站 messages 无孤儿；
- PTL（provider 实报上下文超限）回收最旧一批 → IR 整对摘除，返回对数；
- 「文本条目 → tool_use id」映射（按 [tool-record round=N index=M] 标记）正确，
  据此整对摘除后无孤儿。

Step 4（出站孤儿净化 sweep，最后防线）：
- 孤儿 tool_use（有调用无结果）→ 补 stub tool_result，不删 assistant 文本；
- 孤儿 tool_result（指向不存在的 tool_use）→ 剔除，空 user 消息整条删；
- 已配对的 messages → 原样不动；
- 真实 to_provider_messages 出口默认带 sweep。

全程不依赖真实模型/网络。text 协议路径在既有套件中验证不变，这里只测 native 红线。
"""

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core._tool_loop_service import (
    _fit_native_ir_to_shared_budget,
    _ptl_reclaim_oldest,
    _record_tool_call,
    build_tool_loop_prompt,
)
from agent_py_agent.agent.agent_core.model.context_pressure import (
    model_visible_context_tokens,
    preflight_context_pressure_response,
)
from agent_py_agent.agent.agent_core.runtime.conversation_state import (
    conversation_runtime_state_section,
)
from agent_py_agent.agent.agent_core.tool_ir_compact import (
    compact_native_ir_to_token_budget,
    reclaim_oldest_native_ir_pairs,
    tool_use_ids_for_tool_records,
)
from agent_py_agent.agent.agent_core.tool_ir_history import (
    drop_tool_call_pairs,
    replace_compaction_summary_ir,
)
from agent_py_agent.agent.agent_core.tool_loop.round_execution import ToolCallRecordParams
from agent_py_agent.agent.agent_core.tool_model_generation import _native_provider_messages
from agent_py_agent.agent.backends.message_adapter import (
    AnthropicMessageAdapter,
    strip_orphaned_tool_blocks,
)
from agent_py_agent.agent.backends.tool_ir import (
    AssistantTurn,
    CompactionSummary,
    ToolResult,
    UserTurn,
)
from agent_py_agent.agent.conversation.active_turn_compact import (
    ActiveTurnArchiveCompactRequest,
    compact_carried_active_turn_archive,
    model_visible_active_turn_tool_calls,
)
from agent_py_agent.agent.conversation.authority import (
    AGENT_THREAD_ID_ATTR,
    CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR,
)
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.memory_archive import estimate_tokens
from agent_py_agent.agent.tooling.cancellation import CancellationToken
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_history_call,
    canonical_history_result,
    make_test_protocol_snapshot,
)

# --- shared native fixtures (no archive/network side effects) -----------------


def _native_agent(root: Path, *, protocol: str = "native", backend: str = "anthropic_compatible"):
    return SimpleNamespace(
        backend=SimpleNamespace(name=backend),
        config=SimpleNamespace(
            tool_protocol=protocol,
            enable_tools=True,
            auto_save_memory=False,
            tool_output_externalize_min_chars=10_000_000,  # keep results inline
            tool_output_preview_chars=160,
        ),
        root=root,
        tools=SimpleNamespace(),
    )


def _params(*, protocol: str = "native") -> ToolLoopExecuteParams:
    return ToolLoopExecuteParams(
        user_prompt="x",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        write_boundary=None,
        task_attributes={},
        request_id="r",
        run_id="run",
        task_id="t",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
        tool_protocol_snapshot=make_test_protocol_snapshot(
            run_id="run",
            source_protocol=protocol,
        ),
        save=False,
        delivery_contract={},
    )


def _rec(agent, params, *, rnd, idx, cid, body):
    call = canonical_history_call(
        "read_file",
        {"path": f"{cid}.md"},
        call_id=cid,
        source_protocol=params.tool_protocol_snapshot.source_protocol,
        run_id=params.run_id,
        turn_id=f"{params.run_id}:round-{rnd}",
        attempt_id=params.request_id,
    )
    _record_tool_call(
        agent,
        ToolCallRecordParams(
            params=params,
            tool_rounds=rnd,
            idx=idx,
            call=call,
            result=canonical_history_result(call, body),
        ),
    )


def _message_block_ids(messages):
    """(tool_use ids, tool_result ids) actually present in provider messages."""
    tool_use = {b["id"] for m in messages for b in m["content"] if b["type"] == "tool_use"}
    tool_result = {
        b["tool_use_id"] for m in messages for b in m["content"] if b["type"] == "tool_result"
    }
    return tool_use, tool_result


def _assert_no_orphans(messages):
    tool_use, tool_result = _message_block_ids(messages)
    assert tool_use == tool_result, f"orphan! tool_use={tool_use} tool_result={tool_result}"


def _provider_message_tokens(agent, params) -> int:
    return estimate_tokens(_native_provider_messages(agent, params) or [])


def _record_large_write_calls(agent, params, *, start: int, stop: int, chars: int) -> None:
    for index in range(start, stop + 1):
        call = canonical_history_call(
            "write_file",
            {
                "path": f"checkpoint-{index}.txt",
                "content": f"STATE-{index}-" + ("x" * chars),
            },
            call_id=f"write_{index}",
            source_protocol=params.tool_protocol_snapshot.source_protocol,
            run_id=params.run_id,
            turn_id=f"{params.run_id}:round-{index}",
            attempt_id=params.attempt_id or params.request_id,
        )
        _record_tool_call(
            agent,
            ToolCallRecordParams(
                params=params,
                tool_rounds=index,
                idx=1,
                call=call,
                result=canonical_history_result(call, "written"),
            ),
        )


def _valid_live_handoff(label: str = "") -> str:
    """Return the complete six-field live Compact schema used by production."""

    return (
        "[compact-live-handoff.v1]\n"
        f"current_progress: 摘要-{label or '当前'}；继续原项目；已恢复缺失用例。\n"
        "user_constraints: 使用真实 rg=/opt/reference/rg，不重新寻找路径，保留现有实现。\n"
        "completed: 已读取 checkpoint、核对结构化工具结果并确认已有写入。\n"
        "failures: 旧摘要夹具不完整，已改用当前六字段交接合同。\n"
        "unresolved: 仍需扩展剩余用例并运行定向测试。\n"
        "next_step: 从现有 checkpoint 继续，不重复已完成读取和路径发现。"
    )


class _SummaryBackend:
    name = "anthropic_compatible"

    def __init__(self, context_window_tokens: int):
        self.context_window_tokens = context_window_tokens
        self.calls = []

    def generate(self, prompt, on_chunk=None, tools=None, messages=None):
        del on_chunk, tools
        self.calls.append((prompt, list(messages or [])))
        return SimpleNamespace(text=_valid_live_handoff(str(len(self.calls))))


class _EmptySummaryBackend:
    name = "anthropic_compatible"

    def __init__(self, context_window_tokens: int):
        self.context_window_tokens = context_window_tokens
        self.calls = []

    def generate(self, prompt, on_chunk=None, tools=None, messages=None):
        del on_chunk, tools
        self.calls.append((prompt, list(messages or [])))
        return SimpleNamespace(text="")


class _ContextCompactionSink:
    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []
        self.progress_rows: list[dict[str, object]] = []

    def write_context_compaction(self, value: dict[str, object]) -> bool:
        self.rows.append(dict(value))
        return True

    def write_conversation_compact_progress(self, value: dict[str, object]) -> bool:
        self.progress_rows.append(dict(value))
        return True


def _carried_record(index: int, *, tool: str = "read_file") -> dict[str, object]:
    call_id = f"carried-{index}"
    parameters = {"tool": tool, "path": f"artifact-{index}.txt"}
    return {
        "call_id": call_id,
        "scoped_call_id": f"run:{call_id}",
        "tool": tool,
        "ok": True,
        "parameters": dict(parameters),
        "model_parameters": dict(parameters),
        "output_preview": f"result-{index}",
        "tool_round": index,
    }


def test_active_turn_archive_compact_hides_only_committed_source_calls(tmp_path):
    """完整工具账保持不变，模型只隐藏 thread 指针确认过的旧调用；孤儿候选无权隐藏。"""

    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "local/main",
            "channel": "test",
            "channel_conversation_id": "active-turn-recovery",
            "channel_user_id": "local/main",
        }
    )
    agent = _native_agent(tmp_path)
    agent.backend = _SummaryBackend(128_000)
    agent.config.model_context_window_tokens = 128_000
    agent.config.memory_compact_auto_trigger_percent = 90
    agent.conversation_store = store
    agent.home_paths = SimpleNamespace(owner_compact_dir=tmp_path / "compact")
    records = [_carried_record(index) for index in range(1, 6)]
    original_records = json.loads(json.dumps(records))
    attrs = {
        CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True,
        "conversation_thread_id": thread.thread_id,
    }
    progress: list[dict[str, object]] = []

    result = compact_carried_active_turn_archive(
        agent,
        store,
        thread,
        records,
        ActiveTurnArchiveCompactRequest(
            task_attributes=attrs,
            request_id="request-active-turn",
            attempt_id="attempt-active-turn",
            task_prompt="继续完成长任务并保留全部约束",
            progress_callback=lambda value: progress.append(dict(value)),
        ),
    )

    assert result.compacted is True
    assert result.thread.compact_generation == 1
    assert result.source_call_ids == ("carried-1",)
    assert records == original_records
    visible = model_visible_active_turn_tool_calls(agent, attrs, records)
    assert [item["call_id"] for item in visible] == [
        "carried-2",
        "carried-3",
        "carried-4",
        "carried-5",
    ]
    assert [row["percent"] for row in progress] == [5, 20, 65, 82, 92, 100]
    assert progress[-1]["generation"] == 1
    assert {row["source_kind"] for row in progress} == {"active_turn_tool_archive"}
    assert {row["commit_authority"] for row in progress} == {
        "conversation_thread"
    }

    checkpoint_path = tmp_path / "compact" / "conversations" / f"{thread.thread_id}.jsonl"
    with checkpoint_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "schema": "conversation_compact_checkpoint.v2",
                    "checkpoint_id": "orphan-generation-2",
                    "previous_checkpoint_id": result.thread.compact_checkpoint_id,
                    "thread_id": thread.thread_id,
                    "generation": 2,
                    "source_kind": "live_tool_ir",
                    "source_tool_call_ids": ["carried-2"],
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    visible_after_orphan = model_visible_active_turn_tool_calls(agent, attrs, records)
    assert [item["call_id"] for item in visible_after_orphan] == [
        "carried-2",
        "carried-3",
        "carried-4",
        "carried-5",
    ]


def test_active_turn_archive_interrupt_after_summary_does_not_commit_or_fail(tmp_path):
    """慢摘要刚返回时收到停止，只撤候选，不写 checkpoint、代次或失败熔断。"""

    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "local/main",
            "channel": "test",
            "channel_conversation_id": "active-turn-interrupt",
            "channel_user_id": "local/main",
        }
    )
    agent = _native_agent(tmp_path)
    backend = _SummaryBackend(128_000)
    token = CancellationToken()
    original_generate = backend.generate

    def generate_then_interrupt(*args, **kwargs):
        response = original_generate(*args, **kwargs)
        token.cancel("stop-after-summary")
        return response

    backend.generate = generate_then_interrupt
    agent.backend = backend
    agent.config.model_context_window_tokens = 128_000
    agent.config.memory_compact_auto_trigger_percent = 90
    agent.conversation_store = store
    agent.home_paths = SimpleNamespace(owner_compact_dir=tmp_path / "compact")
    attrs = {
        CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True,
        "conversation_thread_id": thread.thread_id,
    }
    progress: list[dict[str, object]] = []

    with pytest.raises(InterruptedError, match="interrupted by user"):
        compact_carried_active_turn_archive(
            agent,
            store,
            thread,
            [_carried_record(index) for index in range(1, 6)],
            ActiveTurnArchiveCompactRequest(
                task_attributes=attrs,
                request_id="request-active-turn-interrupt",
                attempt_id="attempt-active-turn-interrupt",
                task_prompt="继续长任务",
                progress_callback=lambda value: progress.append(dict(value)),
                interrupt_check=lambda: token.cancelled,
            ),
        )

    unchanged = store.load_thread(thread.thread_id)
    assert unchanged is not None
    assert unchanged.compact_generation == 0
    assert unchanged.compact_checkpoint_id == ""
    assert unchanged.compact_consecutive_failures == 0
    assert progress[-1]["phase"] == "superseded"
    assert progress[-1]["stage"] == "candidate_discarded"
    assert not (tmp_path / "compact" / "conversations" / f"{thread.thread_id}.jsonl").exists()


def test_reconstructed_runtime_uses_full_archive_but_compacted_model_projection():
    """Compact 不能重置工具轮数或成功工具事实，也不能把已替代的旧正文再次喂给模型。"""

    from agent_py_agent.agent.agent_core.runtime.loop_support import (
        _reconstructed_runtime_state,
    )

    records = [_carried_record(index) for index in range(1, 5)]
    state = _reconstructed_runtime_state(
        records,
        model_visible_records=records[-2:],
    )

    assert state.tool_rounds == 4
    assert state.executed_tools == ["read_file"] * 4
    assert state.loaded_tool_names == set()
    rendered = "\n".join(state.tool_context)
    assert "artifact-1.txt" not in rendered
    assert "artifact-2.txt" not in rendered
    assert "artifact-3.txt" in rendered
    assert "artifact-4.txt" in rendered


# === Step 3: window reclaim → IR integer-pair drop, no orphans ================


def test_window_reclaim_drops_oldest_ir_pairs_without_orphans(tmp_path):
    agent = _native_agent(tmp_path)
    params = _params()
    for i in range(1, 6):
        _rec(agent, params, rnd=i, idx=1, cid=f"toolu_{i}", body="X" * 2000)

    _assert_no_orphans(_native_provider_messages(agent, params))  # healthy before

    dropped = compact_native_ir_to_token_budget(
        params,
        max_tokens=1500,
        token_estimator=lambda: _provider_message_tokens(agent, params),
    )

    messages = _native_provider_messages(agent, params)
    _assert_no_orphans(messages)  # the core red line: still paired after compact
    assert dropped >= 1
    tool_use, _ = _message_block_ids(messages)
    assert "toolu_1" not in tool_use  # oldest reclaimed
    assert "toolu_5" in tool_use  # newest preserved


def test_window_under_budget_is_noop(tmp_path):
    agent = _native_agent(tmp_path)
    params = _params()
    _rec(agent, params, rnd=1, idx=1, cid="toolu_a", body="tiny")
    assert (
        compact_native_ir_to_token_budget(
            params,
            max_tokens=10_000_000,
            token_estimator=lambda: _provider_message_tokens(agent, params),
        )
        == 0
    )
    _assert_no_orphans(_native_provider_messages(agent, params))


def test_window_keeps_at_least_newest_pair(tmp_path):
    # Even with an absurdly small budget, never drop the last remaining pair to zero.
    agent = _native_agent(tmp_path)
    params = _params()
    _rec(agent, params, rnd=1, idx=1, cid="toolu_only", body="X" * 5000)
    compact_native_ir_to_token_budget(
        params,
        max_tokens=1,
        token_estimator=lambda: _provider_message_tokens(agent, params),
    )
    messages = _native_provider_messages(agent, params)
    tool_use, _ = _message_block_ids(messages)
    assert tool_use == {"toolu_only"}
    _assert_no_orphans(messages)


def test_tool_window_never_discards_current_turn_user_input(tmp_path):
    agent = _native_agent(tmp_path)
    params = _params()
    _rec(agent, params, rnd=1, idx=1, cid="toolu_old", body="X" * 3000)
    params.tool_ir_history.append(UserTurn("用户刚补充的当前任务要求"))
    _rec(agent, params, rnd=2, idx=1, cid="toolu_new", body="Y" * 3000)

    compact_native_ir_to_token_budget(
        params,
        max_tokens=100,
        token_estimator=lambda: _provider_message_tokens(agent, params),
    )

    assert any(
        isinstance(item, UserTurn) and item.text == "用户刚补充的当前任务要求"
        for item in params.tool_ir_history
    )
    messages = _native_provider_messages(agent, params)
    assert messages is not None
    assert sum("用户刚补充" in str(message) for message in messages) == 1
    _assert_no_orphans(messages)


def test_window_via_loop_helper_is_gated_native_only(tmp_path):
    # text protocol: the native IR window helper must be a no-op even with stray IR.
    text_agent = _native_agent(tmp_path, protocol="text")
    text_params = _params(protocol="text")
    # seed stray IR by recording under a native agent, then feed the same list (mutated
    # in place; params is frozen so we extend rather than reassign).
    seed_agent = _native_agent(tmp_path)
    seed_params = _params()
    for i in range(1, 5):
        _rec(seed_agent, seed_params, rnd=i, idx=1, cid=f"toolu_{i}", body="X" * 30_000)
    text_params.tool_ir_history.extend(seed_params.tool_ir_history)
    before = len(text_params.tool_ir_history)
    _fit_native_ir_to_shared_budget(text_agent, text_params, "prompt")
    assert len(text_params.tool_ir_history) == before  # text protocol: IR untouched


def test_conversation_prompt_at_200k_90_percent_uses_shared_native_ir_window(tmp_path):
    agent = _native_agent(tmp_path)
    agent.backend = _SummaryBackend(200_000)
    agent.config.model_context_window_tokens = 200_000
    agent.config.memory_compact_auto_trigger_percent = 90
    agent.prompts = SimpleNamespace(build=lambda *_args, **_kwargs: "prompt")
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "local/main",
            "channel": "test",
            "channel_conversation_id": "conversation-window",
            "channel_user_id": "local/main",
        }
    )
    agent.conversation_store = store
    agent.home_paths = SimpleNamespace(owner_compact_dir=tmp_path / "compact")
    params = replace(
        _params(),
        context_scope="conversation",
        consume_pending_turn_input=False,
        save=True,
        task_attributes={
            CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True,
            "conversation_thread_id": thread.thread_id,
        },
    )
    for i in range(1, 9):
        _rec(agent, params, rnd=i, idx=1, cid=f"toolu_{i}", body="X" * 120_000)
    before = len(params.tool_ir_history)

    build_tool_loop_prompt(agent, params)

    assert len(params.tool_ir_history) < before
    remaining_results = [item for item in params.tool_ir_history if isinstance(item, ToolResult)]
    assert remaining_results == []
    assert "tool_context_window_overflow" not in params.live_archive_state
    assert (
        preflight_context_pressure_response(
            SimpleNamespace(
                agent=agent,
                params=params,
                prompt="prompt",
                tool_rounds=8,
            )
        )
        is None
    )
    messages = _native_provider_messages(agent, params)
    assert messages is not None
    _assert_no_orphans(messages)
    tool_use, _ = _message_block_ids(messages)
    assert "toolu_1" not in tool_use
    assert "toolu_8" not in tool_use
    summaries = [item for item in params.tool_ir_history if isinstance(item, CompactionSummary)]
    assert len(summaries) == 1
    assert len(agent.backend.calls) == 1
    assert "真实 rg=/opt/reference/rg" in summaries[0].text


def test_shared_native_window_counts_large_tool_call_arguments(tmp_path):
    agent = _native_agent(tmp_path)
    agent.backend.context_window_tokens = 10_000
    agent.config.model_context_window_tokens = 10_000
    agent.config.memory_compact_auto_trigger_percent = 90
    agent.prompts = SimpleNamespace(build=lambda *_args, **_kwargs: "base-prompt")
    sink = _ContextCompactionSink()
    params = replace(
        _params(),
        context_scope="conversation",
        consume_pending_turn_input=False,
        save=True,
        task_attributes={},
        effective_on_chunk=sink,
    )
    _record_large_write_calls(agent, params, start=1, stop=6, chars=12_000)

    raw_before = model_visible_context_tokens(agent, params, "base-prompt")
    assert raw_before >= 9_000
    params.live_archive_state["_provider_context_observation"] = {
        "schema": "provider_context_observation.v1",
        "raw_estimated_tokens": raw_before,
        "provider_input_tokens": raw_before,
    }

    build_tool_loop_prompt(agent, params)

    after = model_visible_context_tokens(agent, params, "base-prompt")
    assert after < 9_000
    assert "_provider_context_observation" not in params.live_archive_state
    messages = _native_provider_messages(agent, params)
    assert messages is not None
    # window marker 会在本轮作为 runtime guidance 进入真实 native messages；它必须已被
    # 最终预算计入，而不是 compact 后偷偷把请求重新顶过 90% 线。
    assert (
        estimate_tokens(
            {
                "initial_user_prompt": "base-prompt",
                "messages": messages,
                "tools": [],
            }
        )
        < 9_000
    )
    _assert_no_orphans(messages)
    tool_use, _ = _message_block_ids(messages)
    assert "write_1" not in tool_use
    assert "write_6" in tool_use
    assert "STATE-6-" in str(messages)
    assert params.tool_context[0].startswith("[tool-context-window]")
    remaining = len(tool_use)
    assert len(sink.rows) == 1
    event = sink.rows[0]
    assert event["schema"] == "model_visible_context_compaction.v1"
    assert event["generation"] == 1
    assert event["before_tokens"] >= event["trigger_tokens"] == 9_000
    assert event["after_tokens"] < event["before_tokens"]
    assert event["dropped_pairs"] > 0
    assert event["preserved_pairs"] == remaining
    assert set(event) == {
        "schema",
        "generation",
        "before_tokens",
        "after_tokens",
        "trigger_tokens",
        "dropped_pairs",
        "preserved_pairs",
    }
    assert [row["stage"] for row in sink.progress_rows] == [
        "preparing",
        "summarizing",
        "measuring",
        "completed",
    ]
    assert [row["percent"] for row in sink.progress_rows] == [5, 20, 65, 100]
    assert {row["generation"] for row in sink.progress_rows} == {1}
    assert {row["source_kind"] for row in sink.progress_rows} == {"turn_local_tool_ir"}
    assert {row["commit_authority"] for row in sink.progress_rows} == {"turn_local"}
    build_tool_loop_prompt(agent, params)

    assert len(_message_block_ids(_native_provider_messages(agent, params))[0]) == remaining
    assert (
        preflight_context_pressure_response(
            SimpleNamespace(
                agent=agent,
                params=params,
                prompt="base-prompt",
                tool_rounds=6,
            )
        )
        is None
    )


def test_native_window_defers_when_completed_history_alone_reaches_trigger(tmp_path):
    """旧会话前缀已超线时不生成无效 live Compact，交给 transcript Compact。"""
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "local/main",
            "channel": "test",
            "channel_conversation_id": "history-dominates",
            "channel_user_id": "local/main",
        }
    )
    agent = _native_agent(tmp_path)
    agent.backend = _SummaryBackend(10_000)
    agent.config.model_context_window_tokens = 10_000
    agent.config.memory_compact_auto_trigger_percent = 90
    agent.prompts = SimpleNamespace(build=lambda *_args, **_kwargs: "base-prompt")
    agent.conversation_store = store
    agent.home_paths = SimpleNamespace(owner_compact_dir=tmp_path / "compact")
    params = replace(
        _params(),
        save=True,
        provider_history_messages=[
            {
                "role": "user",
                "content": [{"type": "text", "text": "H" * 40_000}],
            }
        ],
        task_attributes={
            CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True,
            "conversation_thread_id": thread.thread_id,
        },
    )
    _record_large_write_calls(agent, params, start=1, stop=6, chars=12_000)
    before_ir = list(params.tool_ir_history)

    prompt = build_tool_loop_prompt(agent, params)

    assert params.tool_ir_history == before_ir
    assert agent.backend.calls == []
    assert store.load_thread(thread.thread_id).compact_generation == 0
    pressure = preflight_context_pressure_response(
        SimpleNamespace(agent=agent, params=params, prompt=prompt, tool_rounds=6)
    )
    assert pressure is not None
    assert pressure.runtime_status == "context_overflow"


def test_native_window_defers_when_completed_history_consumes_recovery_headroom(tmp_path):
    """旧会话虽低于 90% 但已吃掉恢复余量时，不先烧一次必重压的 live summary。"""
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "local/main",
            "channel": "test",
            "channel_conversation_id": "history-consumes-headroom",
            "channel_user_id": "local/main",
        }
    )
    agent = _native_agent(tmp_path)
    agent.backend = _SummaryBackend(10_000)
    agent.config.model_context_window_tokens = 10_000
    agent.config.memory_compact_auto_trigger_percent = 90
    agent.prompts = SimpleNamespace(build=lambda *_args, **_kwargs: "base-prompt")
    agent.conversation_store = store
    agent.home_paths = SimpleNamespace(owner_compact_dir=tmp_path / "compact")
    params = replace(
        _params(),
        save=True,
        provider_history_messages=[
            {
                "role": "user",
                "content": [{"type": "text", "text": "H" * 25_000}],
            }
        ],
        task_attributes={
            CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True,
            "conversation_thread_id": thread.thread_id,
        },
    )
    base_tokens = model_visible_context_tokens(
        agent,
        replace(params, tool_ir_history=[]),
        "base-prompt",
    )
    assert 8_100 <= base_tokens < 9_000
    _record_large_write_calls(agent, params, start=1, stop=6, chars=12_000)
    before_ir = list(params.tool_ir_history)

    prompt = build_tool_loop_prompt(agent, params)

    assert params.tool_ir_history == before_ir
    assert agent.backend.calls == []
    assert store.load_thread(thread.thread_id).compact_generation == 0
    pressure = preflight_context_pressure_response(
        SimpleNamespace(agent=agent, params=params, prompt=prompt, tool_rounds=6)
    )
    assert pressure is not None
    assert pressure.runtime_status == "context_overflow"


def test_native_window_summary_may_replace_latest_pair_to_reach_recovery_target(
    tmp_path,
):
    """完整摘要覆盖最新巨型回执后可释放最后一对，避免假失败和立即重压。"""
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "local/main",
            "channel": "test",
            "channel_conversation_id": "thin-live-candidate",
            "channel_user_id": "local/main",
        }
    )
    agent = _native_agent(tmp_path)
    agent.backend = _SummaryBackend(10_000)
    summary_calls: list[bool] = []

    def thin_summary(*_args, **_kwargs):
        summary_calls.append(True)
        return SimpleNamespace(text=_valid_live_handoff("thin"))

    agent.backend.generate = thin_summary
    agent.config.model_context_window_tokens = 10_000
    agent.config.memory_compact_auto_trigger_percent = 90
    # 该用例专门验证 live-tool 候选；提高恢复目标，避免基线历史先转交 transcript Compact。
    agent.config.memory_compact_recovery_target_percent = 80
    agent.prompts = SimpleNamespace(build=lambda *_args, **_kwargs: "base-prompt")
    agent.conversation_store = store
    agent.home_paths = SimpleNamespace(owner_compact_dir=tmp_path / "compact")
    params = replace(
        _params(),
        save=True,
        effective_on_chunk=(sink := _ContextCompactionSink()),
        provider_history_messages=[
            {
                "role": "user",
                "content": [{"type": "text", "text": "H" * 20_000}],
            }
        ],
        task_attributes={
            CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True,
            "conversation_thread_id": thread.thread_id,
        },
    )
    _record_large_write_calls(agent, params, start=1, stop=10, chars=3_000)
    before_ir = list(params.tool_ir_history)

    prompt = build_tool_loop_prompt(agent, params)

    assert params.tool_ir_history != before_ir
    assert summary_calls == [True]
    updated = store.load_thread(thread.thread_id)
    assert updated is not None and updated.compact_generation == 1
    assert sink.progress_rows[-1]["phase"] == "completed"
    assert sink.progress_rows[-1]["after_tokens"] <= 8_100
    assert (
        preflight_context_pressure_response(
            SimpleNamespace(agent=agent, params=params, prompt=prompt, tool_rounds=10)
        )
        is None
    )
    _assert_no_orphans(_native_provider_messages(agent, params))


def test_native_window_commits_below_trigger_when_recovery_target_is_unreachable(
    tmp_path,
    monkeypatch,
):
    """有效 native 候选即使未到 60% 目标，也必须推进代次而不是反复烧摘要。"""

    from agent_py_agent.agent.agent_core import _tool_loop_service as service

    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "local/main",
            "channel": "test",
            "channel_conversation_id": "native-trigger-fallback",
            "channel_user_id": "local/main",
        }
    )
    agent = _native_agent(tmp_path)
    agent.backend = _SummaryBackend(10_000)
    agent.config.model_context_window_tokens = 10_000
    agent.config.memory_compact_auto_trigger_percent = 90
    agent.config.memory_compact_recovery_target_percent = 80
    agent.prompts = SimpleNamespace(build=lambda *_args, **_kwargs: "base-prompt")
    agent.conversation_store = store
    agent.home_paths = SimpleNamespace(owner_compact_dir=tmp_path / "compact")
    params = replace(
        _params(),
        save=True,
        effective_on_chunk=(sink := _ContextCompactionSink()),
        provider_history_messages=[
            {
                "role": "user",
                "content": [{"type": "text", "text": "H" * 20_000}],
            }
        ],
        task_attributes={
            CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True,
            "conversation_thread_id": thread.thread_id,
        },
    )
    _record_large_write_calls(agent, params, start=1, stop=10, chars=3_000)
    original_settle = service._settle_native_ir_window

    def settle_below_trigger(**kwargs):
        dropped, _actual_tokens = original_settle(**kwargs)
        return dropped, 8_500

    monkeypatch.setattr(service, "_settle_native_ir_window", settle_below_trigger)

    build_tool_loop_prompt(agent, params)

    updated = store.load_thread(thread.thread_id)
    assert updated is not None and updated.compact_generation == 1
    assert sink.progress_rows[-1]["phase"] == "completed"
    assert sink.progress_rows[-1]["after_tokens"] == 8_500
    assert sink.progress_rows[-1]["trigger_tokens"] == 9_000
    assert sink.progress_rows[-1]["after_tokens"] > 8_100
    _assert_no_orphans(_native_provider_messages(agent, params))


def test_native_window_summary_removes_covered_assistant_tool_turn_text(tmp_path):
    """工具对的长思考正文已由完整摘要覆盖时必须同轮回收，不能留下空壳顶爆窗口。"""
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "local/main",
            "channel": "test",
            "channel_conversation_id": "covered-assistant-tool-turns",
            "channel_user_id": "local/main",
        }
    )
    agent = _native_agent(tmp_path)
    agent.backend = _SummaryBackend(10_000)
    agent.config.model_context_window_tokens = 10_000
    agent.config.memory_compact_auto_trigger_percent = 90
    agent.prompts = SimpleNamespace(build=lambda *_args, **_kwargs: "base-prompt")
    agent.conversation_store = store
    agent.home_paths = SimpleNamespace(owner_compact_dir=tmp_path / "compact")
    params = replace(
        _params(),
        save=True,
        effective_on_chunk=(sink := _ContextCompactionSink()),
        task_attributes={
            CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True,
            "conversation_thread_id": thread.thread_id,
        },
    )
    for index in range(1, 7):
        _rec(
            agent,
            params,
            rnd=index,
            idx=1,
            cid=f"thought_{index}",
            body="done",
        )
    for item in params.tool_ir_history:
        if isinstance(item, AssistantTurn) and item.tool_calls:
            object.__setattr__(
                item, "text", f"covered-round-{item.tool_calls[0].call_id}-" + "思" * 8_000
            )

    prompt = build_tool_loop_prompt(agent, params)

    updated = store.load_thread(thread.thread_id)
    assert updated is not None and updated.compact_generation == 1
    assert sink.progress_rows[-1]["phase"] == "completed"
    assert sink.progress_rows[-1]["after_tokens"] <= 8_100
    assert not any(
        isinstance(item, AssistantTurn) and str(item.text or "").startswith("covered-round-")
        for item in params.tool_ir_history
    )
    assert (
        preflight_context_pressure_response(
            SimpleNamespace(agent=agent, params=params, prompt=prompt, tool_rounds=6)
        )
        is None
    )
    _assert_no_orphans(_native_provider_messages(agent, params))


def test_native_window_rolls_back_summary_that_still_exceeds_trigger(tmp_path):
    """摘要或最新工具对过大时不提交 generation、不丢 IR，也不误报失败。"""
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "local/main",
            "channel": "test",
            "channel_conversation_id": "summary-too-large",
            "channel_user_id": "local/main",
        }
    )
    agent = _native_agent(tmp_path)
    agent.backend = _SummaryBackend(10_000)
    agent.backend.generate = lambda *_args, **_kwargs: SimpleNamespace(text="S" * 12_000)
    agent.config.model_context_window_tokens = 10_000
    agent.config.memory_compact_auto_trigger_percent = 90
    # 该用例专门验证候选回滚；提高恢复目标，确保先进入 live-tool 候选分支。
    agent.config.memory_compact_recovery_target_percent = 80
    agent.prompts = SimpleNamespace(build=lambda *_args, **_kwargs: "base-prompt")
    agent.conversation_store = store
    agent.home_paths = SimpleNamespace(owner_compact_dir=tmp_path / "compact")
    params = replace(
        _params(),
        save=True,
        effective_on_chunk=(sink := _ContextCompactionSink()),
        provider_history_messages=[
            {
                "role": "user",
                "content": [{"type": "text", "text": "H" * 22_000}],
            }
        ],
        task_attributes={
            CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True,
            "conversation_thread_id": thread.thread_id,
        },
    )
    _record_large_write_calls(agent, params, start=1, stop=6, chars=12_000)
    before_ir = list(params.tool_ir_history)

    prompt = build_tool_loop_prompt(agent, params)

    assert params.tool_ir_history == before_ir
    updated = store.load_thread(thread.thread_id)
    assert updated.compact_generation == 0
    assert updated.compact_consecutive_failures == 0
    assert sink.progress_rows[-1]["phase"] == "superseded"
    assert sink.progress_rows[-1]["stage"] == "candidate_discarded"
    assert not any(row["phase"] in {"completed", "failed"} for row in sink.progress_rows)
    pressure = preflight_context_pressure_response(
        SimpleNamespace(agent=agent, params=params, prompt=prompt, tool_rounds=6)
    )
    assert pressure is not None
    assert pressure.runtime_status == "context_overflow"


@pytest.mark.parametrize("thread_attr", ["conversation_thread_id", AGENT_THREAD_ID_ATTR])
def test_shared_native_window_commits_main_or_child_conversation_compact(
    tmp_path,
    thread_attr,
):
    """真实 main/child IR 回收写同一 checkpoint/CAS，事件展示 canonical generation。"""
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "local/main",
            "channel": "test",
            "channel_conversation_id": f"compact-{thread_attr}",
            "channel_user_id": "local/main",
        }
    )
    agent = _native_agent(tmp_path)
    agent.backend = _SummaryBackend(10_000)
    agent.config.model_context_window_tokens = 10_000
    agent.config.memory_compact_auto_trigger_percent = 90
    agent.prompts = SimpleNamespace(build=lambda *_args, **_kwargs: "base-prompt")
    agent.conversation_store = store
    agent.home_paths = SimpleNamespace(owner_compact_dir=tmp_path / "compact")
    sink = _ContextCompactionSink()
    params = replace(
        _params(),
        request_id="request-child",
        run_id="run-child",
        attempt_id="attempt-child",
        context_scope="task_local",
        save=True,
        effective_on_chunk=sink,
        task_attributes={
            CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True,
            thread_attr: thread.thread_id,
        },
    )

    _record_large_write_calls(agent, params, start=1, stop=6, chars=12_000)
    build_tool_loop_prompt(agent, params)
    first = store.load_thread(thread.thread_id)
    assert first is not None
    assert len(sink.rows) == 1
    assert sink.rows[0]["generation"] == first.compact_generation == 1
    assert '"compact_generation":1' in conversation_runtime_state_section(params)
    assert first.compact_source_messages == 0
    assert first.compact_source_tool_pairs > 0
    assert first.compact_checkpoint_id
    assert "真实 rg=/opt/reference/rg" in first.summary
    checkpoint_path = tmp_path / "compact" / "conversations" / f"{thread.thread_id}.jsonl"
    checkpoints = [
        json.loads(line)
        for line in checkpoint_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert checkpoints[-1]["source_kind"] == "live_tool_ir"
    assert checkpoints[-1]["generation"] == 1
    assert checkpoints[-1]["checkpoint_id"] == first.compact_checkpoint_id
    assert checkpoints[-1]["source_tool_pairs"] == first.compact_source_tool_pairs

    build_tool_loop_prompt(agent, params)
    assert len(sink.rows) == 1
    assert store.load_thread(thread.thread_id).compact_generation == 1

    _record_large_write_calls(agent, params, start=7, stop=12, chars=12_000)
    build_tool_loop_prompt(agent, params)
    second = store.load_thread(thread.thread_id)
    assert second is not None
    assert len(sink.rows) == 2
    assert sink.rows[-1]["generation"] == second.compact_generation == 2
    assert '"compact_generation":2' in conversation_runtime_state_section(params)
    assert second.compact_source_tool_pairs > first.compact_source_tool_pairs
    assert agent.backend.calls[-1][0] == "x"
    final_blocks = agent.backend.calls[-1][1][-1]["content"]
    final_text = "".join(str(block.get("text") or "") for block in final_blocks)
    assert first.summary in final_text
    assert "摘要-1" not in str(agent.backend.calls[-1][1][:-1])


def test_background_main_run_params_commit_live_compact_despite_save_false(tmp_path):
    """生产后台主代理参数虽由外层保存回复，仍须提交同一会话 Compact。"""
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundRunRequest,
        _run_params,
    )

    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "local/main",
            "channel": "test",
            "channel_conversation_id": "background-live-compact",
            "channel_user_id": "local/main",
        }
    )
    agent = _native_agent(tmp_path)
    agent.backend = _SummaryBackend(10_000)
    agent.config.model_context_window_tokens = 10_000
    agent.config.memory_compact_auto_trigger_percent = 90
    agent.prompts = SimpleNamespace(build=lambda *_args, **_kwargs: "base-prompt")
    agent.conversation_store = store
    agent.home_paths = SimpleNamespace(owner_compact_dir=tmp_path / "compact")
    run_params = _run_params(
        thread.thread_id,
        BackgroundRunRequest(
            thread_id=thread.thread_id,
            task_id="task-background",
            reason="scheduled_progress_report",
        ),
    )
    sink = _ContextCompactionSink()
    params = replace(
        _params(),
        save=run_params.save,
        source=run_params.source,
        request_id=run_params.request_id,
        run_id=run_params.run_id,
        task_id=run_params.task_id,
        attempt_id="background-attempt",
        context_scope=run_params.context_scope,
        task_attributes=run_params.task_attributes,
        effective_on_chunk=sink,
    )
    _record_large_write_calls(agent, params, start=1, stop=6, chars=12_000)

    build_tool_loop_prompt(agent, params)

    updated = store.load_thread(thread.thread_id)
    assert run_params.save is False
    assert run_params.task_attributes is not None
    assert run_params.task_attributes[CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR] is True
    assert updated is not None
    assert updated.compact_generation == 1
    assert updated.compact_checkpoint_id
    assert sink.rows[-1]["generation"] == 1
    assert sink.progress_rows[-1]["phase"] == "completed"


def test_persistent_native_window_commits_completed_empty_summary_fallback(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "local/main",
            "channel": "test",
            "channel_conversation_id": "compact-empty-response",
            "channel_user_id": "local/main",
        }
    )
    agent = _native_agent(tmp_path)
    agent.backend = _EmptySummaryBackend(10_000)
    agent.config.model_context_window_tokens = 10_000
    agent.config.memory_compact_auto_trigger_percent = 90
    agent.prompts = SimpleNamespace(build=lambda *_args, **_kwargs: "base-prompt")
    agent.conversation_store = store
    agent.home_paths = SimpleNamespace(owner_compact_dir=tmp_path / "compact")
    params = replace(
        _params(),
        user_prompt="继续现有项目并保留 checkpoint",
        save=True,
        task_attributes={
            CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True,
            "conversation_thread_id": thread.thread_id,
        },
        effective_on_chunk=(sink := _ContextCompactionSink()),
    )
    _record_large_write_calls(agent, params, start=1, stop=6, chars=12_000)

    build_tool_loop_prompt(agent, params)

    updated = store.load_thread(thread.thread_id)
    summaries = [item for item in params.tool_ir_history if isinstance(item, CompactionSummary)]
    assert updated is not None
    assert updated.compact_generation == 1
    assert updated.compact_consecutive_failures == 0
    assert updated.compact_source_tool_pairs > 0
    assert len(agent.backend.calls) == 1
    assert len(summaries) == 1
    assert summaries[0].text.startswith("[compact-mechanical-fallback]")
    assert "checkpoint" in summaries[0].text
    assert sink.progress_rows[-1]["phase"] == "completed"
    assert not any(row["phase"] == "failed" for row in sink.progress_rows)


def test_persistent_native_window_restores_ir_when_summary_fails(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "local/main",
            "channel": "test",
            "channel_conversation_id": "compact-failure",
            "channel_user_id": "local/main",
        }
    )
    agent = _native_agent(tmp_path)
    agent.backend.context_window_tokens = 10_000
    agent.config.model_context_window_tokens = 10_000
    agent.config.memory_compact_auto_trigger_percent = 90
    agent.prompts = SimpleNamespace(build=lambda *_args, **_kwargs: "base-prompt")
    agent.conversation_store = store
    agent.home_paths = SimpleNamespace(owner_compact_dir=tmp_path / "compact")
    params = replace(
        _params(),
        save=True,
        task_attributes={
            CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True,
            "conversation_thread_id": thread.thread_id,
        },
        effective_on_chunk=(sink := _ContextCompactionSink()),
    )
    _record_large_write_calls(agent, params, start=1, stop=6, chars=12_000)
    before_ir = list(params.tool_ir_history)

    with pytest.raises(ConversationCompactError, match="empty summary"):
        build_tool_loop_prompt(agent, params)

    updated = store.load_thread(thread.thread_id)
    assert updated is not None
    assert params.tool_ir_history == before_ir
    assert updated.compact_generation == 0
    assert updated.compact_checkpoint_id == ""
    assert updated.compact_consecutive_failures == 1
    assert [row["stage"] for row in sink.progress_rows] == [
        "preparing",
        "summarizing",
        "failed",
    ]
    assert not any(row["phase"] == "completed" for row in sink.progress_rows)


def test_persistent_native_interrupt_after_summary_restores_ir_without_failure(tmp_path):
    """摘要请求已计费但停止先于内存改写时，原生历史保持逐项相同且不触发熔断。"""

    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "local/main",
            "channel": "test",
            "channel_conversation_id": "native-summary-interrupt",
            "channel_user_id": "local/main",
        }
    )
    agent = _native_agent(tmp_path)
    backend = _SummaryBackend(10_000)
    token = CancellationToken()
    original_generate = backend.generate

    def generate_then_interrupt(*args, **kwargs):
        response = original_generate(*args, **kwargs)
        token.cancel("stop-after-summary")
        return response

    backend.generate = generate_then_interrupt
    agent.backend = backend
    agent.config.model_context_window_tokens = 10_000
    agent.config.memory_compact_auto_trigger_percent = 90
    agent.prompts = SimpleNamespace(build=lambda *_args, **_kwargs: "base-prompt")
    agent.conversation_store = store
    agent.home_paths = SimpleNamespace(owner_compact_dir=tmp_path / "compact")
    params = replace(
        _params(),
        save=True,
        cancellation_token=token,
        task_attributes={
            CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True,
            "conversation_thread_id": thread.thread_id,
        },
        effective_on_chunk=(sink := _ContextCompactionSink()),
    )
    _record_large_write_calls(agent, params, start=1, stop=6, chars=12_000)
    before_ir = list(params.tool_ir_history)
    before_context = list(params.tool_context)

    with pytest.raises(InterruptedError, match="interrupted by user"):
        build_tool_loop_prompt(agent, params)

    unchanged = store.load_thread(thread.thread_id)
    assert unchanged is not None
    assert params.tool_ir_history == before_ir
    assert params.tool_context == before_context
    assert unchanged.compact_generation == 0
    assert unchanged.compact_checkpoint_id == ""
    assert unchanged.compact_consecutive_failures == 0
    assert sink.progress_rows[-1]["phase"] == "superseded"
    assert not any(row["phase"] == "failed" for row in sink.progress_rows)


def test_persistent_native_already_cancelled_never_starts_summary_or_mutation(tmp_path):
    """进入预算检查前已经取消时，不再调用摘要模型，也不碰现有原生历史。"""

    agent = _native_agent(tmp_path)
    backend = _SummaryBackend(10_000)
    agent.backend = backend
    agent.config.model_context_window_tokens = 10_000
    agent.config.memory_compact_auto_trigger_percent = 90
    agent.prompts = SimpleNamespace(build=lambda *_args, **_kwargs: "base-prompt")
    token = CancellationToken()
    token.cancel("already-stopped")
    params = replace(
        _params(),
        save=True,
        cancellation_token=token,
        effective_on_chunk=(sink := _ContextCompactionSink()),
    )
    _record_large_write_calls(agent, params, start=1, stop=6, chars=12_000)
    before_ir = list(params.tool_ir_history)
    before_context = list(params.tool_context)

    with pytest.raises(InterruptedError, match="interrupted by user"):
        build_tool_loop_prompt(agent, params)

    assert backend.calls == []
    assert params.tool_ir_history == before_ir
    assert params.tool_context == before_context
    assert sink.progress_rows == []


def test_persistent_native_interrupt_after_ir_mutation_rolls_back_without_failure(
    tmp_path,
    monkeypatch,
):
    """内存候选已经成对裁剪后收到停止，必须恢复 IR/tool-context 再退出。"""

    import agent_py_agent.agent.agent_core._tool_loop_service as tool_loop_service

    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "local/main",
            "channel": "test",
            "channel_conversation_id": "native-mutation-interrupt",
            "channel_user_id": "local/main",
        }
    )
    agent = _native_agent(tmp_path)
    agent.backend = _SummaryBackend(10_000)
    agent.config.model_context_window_tokens = 10_000
    agent.config.memory_compact_auto_trigger_percent = 90
    agent.prompts = SimpleNamespace(build=lambda *_args, **_kwargs: "base-prompt")
    agent.conversation_store = store
    agent.home_paths = SimpleNamespace(owner_compact_dir=tmp_path / "compact")
    token = CancellationToken()
    params = replace(
        _params(),
        save=True,
        cancellation_token=token,
        task_attributes={
            CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True,
            "conversation_thread_id": thread.thread_id,
        },
        effective_on_chunk=(sink := _ContextCompactionSink()),
    )
    _record_large_write_calls(agent, params, start=1, stop=6, chars=12_000)
    before_ir = list(params.tool_ir_history)
    before_context = list(params.tool_context)
    original_settle = tool_loop_service._settle_native_ir_window

    def settle_then_interrupt(**kwargs):
        result = original_settle(**kwargs)
        token.cancel("stop-after-ir-mutation")
        return result

    monkeypatch.setattr(tool_loop_service, "_settle_native_ir_window", settle_then_interrupt)
    with pytest.raises(InterruptedError, match="interrupted by user"):
        build_tool_loop_prompt(agent, params)

    unchanged = store.load_thread(thread.thread_id)
    assert unchanged is not None
    assert params.tool_ir_history == before_ir
    assert params.tool_context == before_context
    assert unchanged.compact_generation == 0
    assert unchanged.compact_checkpoint_id == ""
    assert unchanged.compact_consecutive_failures == 0
    assert sink.progress_rows[-1]["phase"] == "superseded"


def test_persistent_native_interrupt_after_checkpoint_leaves_orphan_without_cas(tmp_path):
    """checkpoint 已写而 CAS 尚未执行时停止，只留下不可达候选，不推进代次或失败账。"""

    token = CancellationToken()

    class InterruptAtCommitSink(_ContextCompactionSink):
        def write_conversation_compact_progress(self, value: dict[str, object]) -> bool:
            super().write_conversation_compact_progress(value)
            if value.get("stage") == "committing":
                token.cancel("stop-after-checkpoint")
            return True

    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "local/main",
            "channel": "test",
            "channel_conversation_id": "native-checkpoint-interrupt",
            "channel_user_id": "local/main",
        }
    )
    agent = _native_agent(tmp_path)
    agent.backend = _SummaryBackend(10_000)
    agent.config.model_context_window_tokens = 10_000
    agent.config.memory_compact_auto_trigger_percent = 90
    agent.prompts = SimpleNamespace(build=lambda *_args, **_kwargs: "base-prompt")
    agent.conversation_store = store
    agent.home_paths = SimpleNamespace(owner_compact_dir=tmp_path / "compact")
    sink = InterruptAtCommitSink()
    params = replace(
        _params(),
        save=True,
        cancellation_token=token,
        task_attributes={
            CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True,
            "conversation_thread_id": thread.thread_id,
        },
        effective_on_chunk=sink,
    )
    _record_large_write_calls(agent, params, start=1, stop=6, chars=12_000)
    before_ir = list(params.tool_ir_history)
    before_context = list(params.tool_context)

    with pytest.raises(InterruptedError, match="interrupted by user"):
        build_tool_loop_prompt(agent, params)

    unchanged = store.load_thread(thread.thread_id)
    assert unchanged is not None
    assert params.tool_ir_history == before_ir
    assert params.tool_context == before_context
    assert unchanged.compact_generation == 0
    assert unchanged.compact_checkpoint_id == ""
    assert unchanged.compact_consecutive_failures == 0
    checkpoint_path = tmp_path / "compact" / "conversations" / f"{thread.thread_id}.jsonl"
    orphan = json.loads(checkpoint_path.read_text(encoding="utf-8").splitlines()[-1])
    assert orphan["status"] == "validated_candidate"
    assert orphan["checkpoint_id"] != unchanged.compact_checkpoint_id
    assert [row["stage"] for row in sink.progress_rows][-2:] == [
        "committing",
        "candidate_discarded",
    ]
    assert not any(row["phase"] == "failed" for row in sink.progress_rows)


def test_persistent_native_window_restores_ir_when_compact_cas_fails(
    tmp_path,
    monkeypatch,
):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "local/main",
            "channel": "test",
            "channel_conversation_id": "compact-cas-failure",
            "channel_user_id": "local/main",
        }
    )
    agent = _native_agent(tmp_path)
    agent.backend = _SummaryBackend(10_000)
    agent.config.model_context_window_tokens = 10_000
    agent.config.memory_compact_auto_trigger_percent = 90
    agent.prompts = SimpleNamespace(build=lambda *_args, **_kwargs: "base-prompt")
    agent.conversation_store = store
    agent.home_paths = SimpleNamespace(owner_compact_dir=tmp_path / "compact")
    params = replace(
        _params(),
        save=True,
        task_attributes={
            CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True,
            "conversation_thread_id": thread.thread_id,
        },
        effective_on_chunk=(sink := _ContextCompactionSink()),
    )
    _record_large_write_calls(agent, params, start=1, stop=6, chars=12_000)
    before_ir = list(params.tool_ir_history)

    def fail_compact_cas(*_args, **_kwargs):
        raise RuntimeError("synthetic compact CAS conflict")

    monkeypatch.setattr(store, "update_compact_state", fail_compact_cas)
    with pytest.raises(RuntimeError, match="synthetic compact CAS conflict"):
        build_tool_loop_prompt(agent, params)

    updated = store.load_thread(thread.thread_id)
    assert updated is not None
    assert params.tool_ir_history == before_ir
    assert updated.compact_generation == 0
    assert updated.compact_checkpoint_id == ""
    assert updated.compact_consecutive_failures == 1
    checkpoint_path = tmp_path / "compact" / "conversations" / f"{thread.thread_id}.jsonl"
    orphan_candidate = json.loads(checkpoint_path.read_text(encoding="utf-8").splitlines()[-1])
    assert orphan_candidate["status"] == "validated_candidate"
    assert orphan_candidate["checkpoint_id"] != updated.compact_checkpoint_id
    assert sink.progress_rows[-1]["stage"] == "failed"
    assert not any(row["phase"] == "completed" for row in sink.progress_rows)


def test_shared_native_window_stays_stable_across_repeated_pressure(tmp_path):
    agent = _native_agent(tmp_path)
    agent.backend.context_window_tokens = 20_000
    agent.config.model_context_window_tokens = 20_000
    agent.config.memory_compact_auto_trigger_percent = 90
    agent.prompts = SimpleNamespace(build=lambda *_args, **_kwargs: "base-prompt")
    params = replace(
        _params(),
        context_scope="conversation",
        consume_pending_turn_input=False,
        save=True,
        task_attributes={},
    )
    _record_large_write_calls(agent, params, start=1, stop=10, chars=10_000)
    build_tool_loop_prompt(agent, params)
    first_ids, _ = _message_block_ids(_native_provider_messages(agent, params))
    assert "write_10" in first_ids
    assert model_visible_context_tokens(agent, params, "base-prompt") < 18_000

    _record_large_write_calls(agent, params, start=11, stop=20, chars=10_000)
    build_tool_loop_prompt(agent, params)
    messages = _native_provider_messages(agent, params)
    _assert_no_orphans(messages)
    second_ids, _ = _message_block_ids(messages)
    assert "write_20" in second_ids
    assert "write_10" not in second_ids
    assert model_visible_context_tokens(agent, params, "base-prompt") < 18_000
    assert sum(item.startswith("[tool-context-window]") for item in params.tool_context) == 1

    build_tool_loop_prompt(agent, params)
    assert _message_block_ids(_native_provider_messages(agent, params))[0] == second_ids


def test_shared_native_window_reuses_semantic_summary_across_repeated_pressure(tmp_path):
    agent = _native_agent(tmp_path)
    agent.backend = _SummaryBackend(20_000)
    agent.config.model_context_window_tokens = 20_000
    agent.config.memory_compact_auto_trigger_percent = 90
    agent.config.memory_compact_semantic_summary_enabled = True
    agent.prompts = SimpleNamespace(build=lambda *_args, **_kwargs: "base-prompt")
    params = replace(
        _params(),
        user_prompt="继续原项目，不要重新寻找真实 rg。",
        context_scope="conversation",
        consume_pending_turn_input=False,
        save=True,
        task_attributes={},
    )
    _record_large_write_calls(agent, params, start=1, stop=10, chars=10_000)
    params.tool_ir_history.append(UserTurn("恢复缺失用例后再扩展"))

    build_tool_loop_prompt(agent, params)

    summaries = [item for item in params.tool_ir_history if isinstance(item, CompactionSummary)]
    assert len(summaries) == 1
    assert "真实 rg=/opt/reference/rg" in summaries[0].text
    assert len(agent.backend.calls) == 1
    messages = _native_provider_messages(agent, params)
    assert messages is not None
    assert str(messages).count("真实 rg=/opt/reference/rg") == 1
    assert "恢复缺失用例后再扩展" in str(messages)
    _assert_no_orphans(messages)

    build_tool_loop_prompt(agent, params)

    assert len(agent.backend.calls) == 1
    assert str(_native_provider_messages(agent, params)).count("真实 rg=/opt/reference/rg") == 1

    _record_large_write_calls(agent, params, start=11, stop=20, chars=10_000)
    build_tool_loop_prompt(agent, params)

    assert len(agent.backend.calls) == 2
    assert "摘要-1" in str(agent.backend.calls[1][1])
    summaries = [item for item in params.tool_ir_history if isinstance(item, CompactionSummary)]
    assert len(summaries) == 1
    assert "摘要-2" in summaries[0].text
    assert "摘要-1" not in summaries[0].text
    messages = _native_provider_messages(agent, params)
    assert messages is not None
    assert str(messages).count("真实 rg=/opt/reference/rg") == 1
    assert model_visible_context_tokens(agent, params, "base-prompt") < 18_000
    _assert_no_orphans(messages)


def test_second_compact_replaces_thread_summary_but_preserves_carried_handoff():
    params = _params()
    params.tool_ir_history.extend(
        [
            UserTurn("# User Task\n继续当前任务"),
            CompactionSummary(
                "[active-turn-tool-handoff]\n"
                "- schema_version: active-turn-tool-handoff.v1\n"
                "- HANDOFF-SECOND-COMPACT"
            ),
            AssistantTurn(text="working"),
        ]
    )

    assert replace_compaction_summary_ir(params, "summary-generation-1") is True
    assert replace_compaction_summary_ir(params, "summary-generation-2") is True

    summaries = [item for item in params.tool_ir_history if isinstance(item, CompactionSummary)]
    assert len(summaries) == 2
    assert summaries[0].text.startswith("[active-turn-tool-handoff]")
    assert summaries[1].text == "summary-generation-2"
    assert "summary-generation-1" not in str(params.tool_ir_history)


# === Step 3: PTL reclaim → IR integer-pair drop ==============================


def test_ptl_reclaim_drops_oldest_ir_pairs(tmp_path):
    agent = _native_agent(tmp_path)
    params = _params()
    for i in range(1, 6):
        _rec(agent, params, rnd=i, idx=1, cid=f"tu_{i}", body="BODY")

    dropped = reclaim_oldest_native_ir_pairs(params, fraction=0.2)

    assert dropped == 1  # max(1, 5*0.2)
    messages = _native_provider_messages(agent, params)
    _assert_no_orphans(messages)
    tool_use, _ = _message_block_ids(messages)
    assert "tu_1" not in tool_use and "tu_5" in tool_use


def test_ptl_reclaim_empty_ir_returns_zero(tmp_path):
    # No tool round in IR -> 0 so the caller stops PTL retry and falls back to compact.
    params = _params()
    assert reclaim_oldest_native_ir_pairs(params, fraction=0.2) == 0


def test_ptl_loop_helper_native_drops_ir_text_drops_text(tmp_path):
    # native: provider overflow forces the same full-budget IR compact transaction.
    agent = _native_agent(tmp_path)
    agent.backend = _SummaryBackend(10_000)
    agent.config.model_context_window_tokens = 10_000
    agent.config.memory_compact_auto_trigger_percent = 90
    params = _params()
    _record_large_write_calls(agent, params, start=1, stop=6, chars=12_000)
    ir_pairs_before = sum(1 for it in params.tool_ir_history if isinstance(it, ToolResult))
    assert _ptl_reclaim_oldest(agent, params, prompt="base-prompt") is True
    ir_pairs_after = sum(1 for it in params.tool_ir_history if isinstance(it, ToolResult))
    assert ir_pairs_after < ir_pairs_before
    _assert_no_orphans(_native_provider_messages(agent, params))

    # text protocol: IR stays empty, text track is what gets reclaimed.
    text_agent = _native_agent(tmp_path, protocol="text")
    text_params = _params(protocol="text")
    for i in range(1, 4):
        _rec(text_agent, text_params, rnd=i, idx=1, cid=f"t_{i}", body="X" * 400)
    assert text_params.tool_ir_history == []  # text never builds IR
    # the text reclaim path returns True while there is reclaimable text body.
    assert _ptl_reclaim_oldest(text_agent, text_params) is True


def test_authoritative_no_save_provider_overflow_commits_same_conversation_compact(
    tmp_path,
):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "local/main",
            "channel": "test",
            "channel_conversation_id": "provider-overflow",
            "channel_user_id": "local/main",
        }
    )
    agent = _native_agent(tmp_path)
    agent.backend = _SummaryBackend(10_000)
    agent.config.model_context_window_tokens = 10_000
    agent.config.memory_compact_auto_trigger_percent = 90
    agent.conversation_store = store
    agent.home_paths = SimpleNamespace(owner_compact_dir=tmp_path / "compact")
    params = replace(
        _params(),
        # Background main slices save their final through ConversationStore, not Agent.run.
        save=False,
        task_attributes={
            CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True,
            "conversation_thread_id": thread.thread_id,
        },
        effective_on_chunk=(sink := _ContextCompactionSink()),
    )
    _record_large_write_calls(agent, params, start=1, stop=6, chars=12_000)

    assert _ptl_reclaim_oldest(agent, params, prompt="base-prompt") is True

    updated = store.load_thread(thread.thread_id)
    assert updated is not None
    assert updated.compact_generation == 1
    checkpoint_path = tmp_path / "compact" / "conversations" / f"{thread.thread_id}.jsonl"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8").splitlines()[-1])
    assert checkpoint["source_kind"] == "live_tool_ir"
    assert checkpoint["forced"] is True
    assert checkpoint["checkpoint_id"] == updated.compact_checkpoint_id
    assert [row["stage"] for row in sink.progress_rows] == [
        "preparing",
        "summarizing",
        "measuring",
        "checkpointing",
        "committing",
        "completed",
    ]
    assert [row["percent"] for row in sink.progress_rows] == [5, 20, 65, 82, 92, 100]
    assert {row["generation"] for row in sink.progress_rows} == {1}
    assert {row["source_kind"] for row in sink.progress_rows} == {
        "active_turn_tool_archive"
    }
    assert {row["commit_authority"] for row in sink.progress_rows} == {
        "conversation_thread"
    }


# === Step 3: text-entry → tool_use id mapping (round/index join) ==============


def test_tool_use_ids_for_reclaimed_text_records(tmp_path):
    agent = _native_agent(tmp_path)
    params = _params()
    _rec(agent, params, rnd=1, idx=1, cid="cid_A", body="A")
    _rec(agent, params, rnd=1, idx=2, cid="cid_B", body="B")  # same round, second call
    _rec(agent, params, rnd=2, idx=1, cid="cid_C", body="C")

    reclaimed = [e for e in params.tool_context if "round=1 index=2" in e or "round=2 index=1" in e]
    mapped = tool_use_ids_for_tool_records(params.tool_ir_history, reclaimed)
    assert mapped == {"cid_B", "cid_C"}

    # dropping by that mapping leaves the survivor paired, no orphan.
    drop_tool_call_pairs(params, mapped)
    messages = _native_provider_messages(agent, params)
    _assert_no_orphans(messages)
    tool_use, _ = _message_block_ids(messages)
    assert tool_use == {"cid_A"}


def test_tool_use_id_mapping_ignores_unparseable_entries(tmp_path):
    agent = _native_agent(tmp_path)
    params = _params()
    _rec(agent, params, rnd=1, idx=1, cid="cid_X", body="X")
    # entries with no [tool-record round= index=] marker map to nothing.
    assert (
        tool_use_ids_for_tool_records(params.tool_ir_history, ["garbage", "[tool-system]\nfoo"])
        == set()
    )
    # out-of-range index is skipped, not mis-mapped.
    assert (
        tool_use_ids_for_tool_records(params.tool_ir_history, ["[tool-record round=1 index=9]"])
        == set()
    )


# === Step 4: orphan sweep — stub for orphan tool_use =========================


def test_sweep_stubs_orphan_tool_use_without_dropping_assistant_text():
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "推理过程"},
                {
                    "type": "tool_use",
                    "id": "tu_orphan",
                    "name": "read_file",
                    "input": {"path": "a"},
                },
            ],
        }
    ]
    out = strip_orphaned_tool_blocks(messages)

    # assistant text + tool_use preserved (we never delete assistant content).
    assert out[0]["role"] == "assistant"
    assert any(b["type"] == "text" and b["text"] == "推理过程" for b in out[0]["content"])
    assert any(b["type"] == "tool_use" and b["id"] == "tu_orphan" for b in out[0]["content"])
    # a stub tool_result was synthesized to pair the orphan tool_use.
    stub = out[1]
    assert stub["role"] == "user"
    assert stub["content"][0]["tool_use_id"] == "tu_orphan"
    assert stub["content"][0]["is_error"] is True
    _assert_no_orphans(out)


def test_sweep_stubs_only_missing_of_multiple_tool_use():
    # one tool_use has a real result, the sibling doesn't -> only the sibling gets a stub.
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "has_result", "name": "r", "input": {}},
                {"type": "tool_use", "id": "no_result", "name": "r", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "has_result", "content": "ok"}],
        },
    ]
    out = strip_orphaned_tool_blocks(messages)
    _assert_no_orphans(out)
    result_ids = [b["tool_use_id"] for m in out for b in m["content"] if b["type"] == "tool_result"]
    assert sorted(result_ids) == ["has_result", "no_result"]
    # exactly one synthesized stub (for no_result), the real one kept its content.
    real = [b for m in out for b in m["content"] if b.get("tool_use_id") == "has_result"][0]
    assert real["content"] == "ok"


# === Step 4: orphan sweep — strip orphan tool_result =========================


def test_sweep_strips_orphan_tool_result_and_removes_empty_user():
    messages = [
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "ghost", "content": "x"}],
        }
    ]
    assert strip_orphaned_tool_blocks(messages) == []


def test_sweep_strips_only_orphan_result_keeps_sibling():
    messages = [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "real", "name": "r", "input": {}}],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "real", "content": "ok"},
                {"type": "tool_result", "tool_use_id": "ghost", "content": "drop me"},
            ],
        },
    ]
    out = strip_orphaned_tool_blocks(messages)
    _assert_no_orphans(out)
    result_ids = [b["tool_use_id"] for m in out for b in m["content"] if b["type"] == "tool_result"]
    assert result_ids == ["real"]  # ghost removed, real kept


def test_sweep_leaves_well_paired_messages_unchanged():
    messages = [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "a", "name": "r", "input": {}}],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": "ok"}]},
    ]
    assert strip_orphaned_tool_blocks(messages) == messages


def test_sweep_handles_empty_and_blockless_messages():
    assert strip_orphaned_tool_blocks([]) == []
    plain = [{"role": "user", "content": "plain string prompt"}]
    assert strip_orphaned_tool_blocks(plain) == plain


# === Step 4: the sweep runs at the real out-bound boundary ====================
# (translation stays pure in to_provider_messages; the sweep is applied by
#  _native_provider_messages right before backend.generate.)


def test_outbound_boundary_stubs_orphan_tool_use(tmp_path):
    agent = _native_agent(tmp_path)
    params = _params()
    # seed a record then forge an orphan tool_use into the IR (e.g. a result lost to a
    # mid-round truncation that Step3's pair-drop didn't cover).
    _rec(agent, params, rnd=1, idx=1, cid="kept", body="ok")
    params.tool_ir_history.append(
        AssistantTurn(
            text="",
            tool_calls=[
                canonical_history_call(
                    "read_file",
                    {"path": "x"},
                    call_id="lonely",
                    run_id=params.run_id,
                    turn_id="run:orphan",
                    attempt_id=params.request_id,
                )
            ],
        )
    )

    messages = _native_provider_messages(agent, params)

    _assert_no_orphans(messages)  # boundary sweep paired the orphan
    assert any(
        b["type"] == "tool_result" and b["tool_use_id"] == "lonely"
        for m in messages
        for b in m["content"]
    )


def test_outbound_boundary_strips_orphan_tool_result(tmp_path):
    agent = _native_agent(tmp_path)
    params = _params()
    _rec(agent, params, rnd=1, idx=1, cid="kept", body="ok")
    # forge a ToolResult that references no ToolCall (e.g. a stale resume fragment).
    ghost_call = canonical_history_call(
        "read_file",
        {"path": "ghost"},
        call_id="ghost",
        run_id=params.run_id,
        turn_id="run:orphan",
        attempt_id=params.request_id,
    )
    params.tool_ir_history.append(canonical_history_result(ghost_call, "dangling"))

    messages = _native_provider_messages(agent, params)

    _assert_no_orphans(messages)
    result_ids = {
        b["tool_use_id"] for m in messages for b in m["content"] if b["type"] == "tool_result"
    }
    assert "ghost" not in result_ids and "kept" in result_ids


def test_to_provider_messages_stays_pure_translation():
    # the translator itself must NOT sweep — isolated fragments map 1:1 (unit contract).
    history = [
        AssistantTurn(
            text="",
            tool_calls=[
                canonical_history_call(
                    "read_file",
                    {"path": "x"},
                    call_id="solo",
                )
            ],
        ),
    ]
    messages = AnthropicMessageAdapter().to_provider_messages(history)
    # no synthesized stub here: pure translation leaves the lone tool_use as-is.
    assert messages == [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "solo", "name": "read_file", "input": {"path": "x"}}
            ],
        }
    ]
