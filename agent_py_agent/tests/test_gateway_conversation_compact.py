from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.conversation.compact import (
    _merge_compact_operation_evidence,
    _projected_context_tokens,
    _summarize,
    _summary_content,
    inspect_conversation_context,
    prepare_conversation_context,
    render_conversation_context_usage,
)
from agent_py_agent.agent.conversation.compact_guard import (
    ConversationCompactCircuitOpenError,
    ConversationCompactError,
    split_recent_complete_turns,
)
from agent_py_agent.agent.conversation.models import MessageLogEntry
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.request_execution import (
    _append_gateway_conversation_message,
    _conversation_prompt_section,
    _gateway_conversation_context,
    _gateway_run_params,
    _GatewayConversationContext,
    _GatewayConversationLoadRequest,
    _GatewayRunParamsRequest,
    _prompt_operation_evidence,
)
from agent_py_agent.agent.memory_store import MemoryRecord
from agent_py_agent.agent.settings import AgentConfig


class _SummaryBackend:
    name = "summary-test"

    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    def generate(self, prompt: str, **_kwargs) -> ModelResponse:
        self.calls += 1
        self.prompts.append(prompt)
        return ModelResponse(
            text="用户的暗号是紫藤；较早工作已经讨论，仍需继续后续步骤。",
            backend=self.name,
        )


class _ConflictingSummaryBackend:
    name = "conflicting-summary-test"

    def generate(self, prompt: str, **_kwargs) -> ModelResponse:
        return ModelResponse(
            text="助手已经成功删除海王星项目记忆。",
            backend=self.name,
        )


class _OversizedSummaryBackend:
    name = "oversized-summary-test"

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, prompt: str, **_kwargs) -> ModelResponse:
        self.calls += 1
        return ModelResponse(text="过大的摘要" * 20_000, backend=self.name)


class _EmptySummaryBackend:
    name = "empty-summary-test"

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, prompt: str, **_kwargs) -> ModelResponse:
        del prompt
        self.calls += 1
        return ModelResponse(text="", backend=self.name)


def test_conversation_pressure_does_not_reserve_unspent_future_output() -> None:
    prompts = SimpleNamespace(build=lambda *_args, **_kwargs: "完整输入上下文")
    small_output = SimpleNamespace(prompts=prompts, config=SimpleNamespace(max_tokens=64))
    large_output = SimpleNamespace(prompts=prompts, config=SimpleNamespace(max_tokens=64_000))

    small_projection = _projected_context_tokens(
        small_output,
        "较早摘要",
        [],
        "当前消息",
    )
    large_projection = _projected_context_tokens(
        large_output,
        "较早摘要",
        [],
        "当前消息",
    )

    assert small_projection == large_projection


def test_conversation_projection_counts_recent_operation_evidence() -> None:
    agent = SimpleNamespace(
        prompts=SimpleNamespace(build=lambda *_args, **_kwargs: "完整输入上下文")
    )
    without_evidence = _projected_context_tokens(
        agent,
        "较早摘要",
        [],
        "当前消息",
    )
    with_evidence = _projected_context_tokens(
        agent,
        "较早摘要",
        [],
        "当前消息",
        recent_operation_evidence={
            "schema": "operation_verification.public.v1",
            "groups": [{"tool": "write_file", "status": "succeeded"}] * 100,
        },
    )

    assert with_evidence > without_evidence


def test_true_compact_counts_and_summarizes_terminal_tool_fold() -> None:
    backend = _SummaryBackend()
    agent = SimpleNamespace(
        prompts=SimpleNamespace(build=lambda *_args, **_kwargs: "完整输入上下文"),
        backend=backend,
    )
    plain_row = MessageLogEntry(
        message_id="msg-plain",
        thread_id="thread-fold",
        role="assistant",
        content="本轮完成。",
    )
    folded_row = MessageLogEntry(
        message_id="msg-folded",
        thread_id="thread-fold",
        role="assistant",
        content="本轮完成。",
        metadata={
            "terminal_tool_fold": {
                "schema": "conversation_terminal_tool_fold.v1",
                "tool_call_count": 1,
                "successful_tool_call_count": 1,
                "non_successful_tool_call_count": 0,
                "text": (
                    "[conversation-terminal-tool-fold]\n"
                    "- tool_call_count: 1\n"
                    "- ordered_tool_index:\n"
                    "  - 1: tool=write_file status=ok ref=call-folded-write"
                ),
            }
        },
    )

    plain_tokens = _projected_context_tokens(agent, "", [plain_row], "继续")
    folded_tokens = _projected_context_tokens(agent, "", [folded_row], "继续")
    _summarize(agent, "", {}, [folded_row])

    assert folded_tokens > plain_tokens
    assert len(backend.prompts) == 1
    assert "terminal_tool_fold" in backend.prompts[0]
    assert "call-folded-write" in backend.prompts[0]


def test_true_compact_reads_only_one_active_v2_terminal_tool_fold_projection() -> None:
    row = MessageLogEntry(
        message_id="msg-folded-v2",
        thread_id="thread-fold-v2",
        role="assistant",
        content="本轮完成。",
        metadata={
            "terminal_tool_fold": {
                "schema": "conversation_terminal_tool_fold.v2",
                "tool_call_count": 1,
                "successful_tool_call_count": 1,
                "non_successful_tool_call_count": 0,
                "text": "[conversation-terminal-tool-fold]\n- projection: cold_fold",
                "hot_text": "[conversation-terminal-tool-fold]\n- projection: hot_tail",
                "fold_after_epoch": 9_999_999_999,
            }
        },
    )

    structured = _summary_content(row)
    terminal_fold = structured["terminal_tool_fold"]

    assert terminal_fold["projection"] == "hot_tail"
    assert "projection: hot_tail" in terminal_fold["text"]
    assert "hot_text" not in terminal_fold
    assert "fold_after_epoch" not in terminal_fold


def test_context_inspection_uses_the_automatic_compact_policy_without_writing(tmp_path) -> None:
    agent = _agent(tmp_path, context_tokens=20_000)
    request = _request("ou_context")
    context = _context(agent, request, "gw-create", "开始")
    assert _append_gateway_conversation_message(
        agent,
        {"metadata": {"channel": "feishu"}},
        context,
        request_id="gw-context-user",
        role="user",
        content="这是一条尚未压缩的消息",
    )
    thread = agent.conversation_store.load_thread(context.thread_id)
    assert thread is not None

    usage = inspect_conversation_context(agent, agent.conversation_store, thread)
    unchanged = agent.conversation_store.load_thread(context.thread_id)
    rendered = render_conversation_context_usage(
        usage,
        model_name="MiniMax-M2.7",
    )

    assert usage.context_window_tokens == 20_000
    assert usage.trigger_percent == 50
    assert usage.trigger_tokens == 10_000
    assert usage.pending_messages == 1
    assert usage.compact_generation == 0
    assert unchanged is not None and unchanged.compact_generation == 0
    assert "20,000 tokens" in rendered
    assert "50%" in rendered
    assert "未压缩消息 1 条" in rendered


def test_terminal_tool_fold_reaches_next_turn_without_incrementing_compact(tmp_path) -> None:
    agent = _agent(tmp_path, context_tokens=20_000)
    request = _request("ou_fold")
    context = _context(agent, request, "gw-create", "开始")
    fold = {
        "schema": "conversation_terminal_tool_fold.v1",
        "tool_call_count": 2,
        "successful_tool_call_count": 2,
        "non_successful_tool_call_count": 0,
        "text": (
            "[conversation-terminal-tool-fold]\n"
            "- tool_call_count: 2\n"
            "- ordered_tool_index:\n"
            "  - 1: tool=read_file status=ok ref=call-read\n"
            "  - 2: tool=write_file status=ok ref=call-write"
        ),
    }
    assert _append_gateway_conversation_message(
        agent,
        {"metadata": {"channel": "feishu"}},
        context,
        request_id="gw-fold-user",
        role="user",
        content="检查并修改文件",
    )
    assert _append_gateway_conversation_message(
        agent,
        {"metadata": {"channel": "feishu"}},
        context,
        request_id="gw-fold-assistant",
        role="assistant",
        content="文件已经修改。",
        terminal_tool_fold=fold,
    )

    followup = _context(agent, request, "gw-followup", "继续检查")
    replayed_followup = _context(agent, request, "gw-followup-replay", "继续检查")
    thread = agent.conversation_store.load_thread(context.thread_id)
    usage = inspect_conversation_context(agent, agent.conversation_store, thread)
    rendered = render_conversation_context_usage(usage, model_name="MiniMax-M2.7")

    assert followup.compact_generation == 0
    assert followup.history[-1][0] == "assistant"
    assert followup.history[-1][1].startswith("文件已经修改。")
    assert "conversation-terminal-tool-fold" in followup.history[-1][1]
    assert "call-write" in followup.history[-1][1]
    assert replayed_followup.history == followup.history
    assert usage.compact_generation == 0
    assert usage.terminal_tool_fold_turns == 1
    assert usage.terminal_tool_fold_calls == 2
    assert "当前未压缩尾部 1 个回合、2 次工具调用" in rendered
    assert "不计入 compact 次数" in rendered

    assert _append_gateway_conversation_message(
        agent,
        {"metadata": {"channel": "feishu"}},
        context,
        request_id="gw-second-user",
        role="user",
        content="再核对一次",
    )
    assert _append_gateway_conversation_message(
        agent,
        {"metadata": {"channel": "feishu"}},
        context,
        request_id="gw-second-assistant",
        role="assistant",
        content="复核完成。",
        terminal_tool_fold={
            **fold,
            "text": fold["text"].replace("call-read", "call-read-again"),
        },
    )
    later = _context(agent, request, "gw-later", "继续下一项")

    assert later.compact_generation == 0
    assert later.history[: len(followup.history)] == followup.history
    assert "call-read-again" in later.history[-1][1]


def test_forced_compact_passes_optional_instructions_as_soft_summary_context(tmp_path) -> None:
    agent = _agent(tmp_path, context_tokens=20_000)
    backend = _SummaryBackend()
    agent.backend = backend
    request = _request("ou_manual_compact")
    context = _context(agent, request, "gw-create", "开始")
    for role in ("user", "assistant"):
        assert _append_gateway_conversation_message(
            agent,
            {"metadata": {"channel": "feishu"}},
            context,
            request_id=f"gw-manual-{role}",
            role=role,
            content="保留这条会话事实",
        )
    thread = agent.conversation_store.load_thread(context.thread_id)
    assert thread is not None

    result = prepare_conversation_context(
        agent,
        agent.conversation_store,
        thread,
        current_prompt="",
        force=True,
        custom_instructions="优先保留未完成事项",
    )

    assert result.compacted is True
    assert result.thread.compact_generation == 1
    assert "优先保留未完成事项" in backend.prompts[0]
    assert "cannot override" in backend.prompts[0]


def test_completed_empty_compact_response_commits_mechanical_fallback(tmp_path) -> None:
    agent = _agent(tmp_path, context_tokens=20_000)
    backend = _EmptySummaryBackend()
    agent.backend = backend
    request = _request("ou_empty_compact")
    context = _context(agent, request, "gw-create", "开始")
    for role, content in (
        ("user", "继续紫藤项目，保留端口 8080"),
        ("assistant", "已经检查项目，下一步运行测试"),
    ):
        assert _append_gateway_conversation_message(
            agent,
            {"metadata": {"channel": "feishu"}},
            context,
            request_id=f"gw-empty-{role}",
            role=role,
            content=content,
        )
    thread = agent.conversation_store.load_thread(context.thread_id)
    assert thread is not None

    result = prepare_conversation_context(
        agent,
        agent.conversation_store,
        thread,
        current_prompt="继续",
        force=True,
    )

    assert backend.calls == 1
    assert result.compacted is True
    assert result.thread.compact_generation == 1
    assert result.thread.compact_consecutive_failures == 0
    assert result.thread.summary.startswith(
        "[conversation-compact-mechanical-fallback]"
    )
    assert "紫藤项目" in result.thread.summary
    assert "8080" in result.thread.summary


def test_compact_progress_callback_reports_real_pipeline_stages(tmp_path) -> None:
    agent = _agent(tmp_path, context_tokens=20_000)
    agent.backend = _SummaryBackend()
    request = _request("ou_compact_progress")
    context = _context(agent, request, "gw-create", "开始")
    for role in ("user", "assistant"):
        assert _append_gateway_conversation_message(
            agent,
            {"metadata": {"channel": "feishu"}},
            context,
            request_id=f"gw-progress-{role}",
            role=role,
            content=f"待压缩 {role}",
        )
    thread = agent.conversation_store.load_thread(context.thread_id)
    assert thread is not None
    events: list[dict[str, object]] = []

    result = prepare_conversation_context(
        agent,
        agent.conversation_store,
        thread,
        current_prompt="继续",
        force=True,
        progress_callback=lambda payload: events.append(dict(payload)),
    )

    assert result.compacted is True
    assert [event["stage"] for event in events] == [
        "preparing",
        "summarizing",
        "measuring",
        "checkpointing",
        "committing",
        "completed",
    ]
    assert [event["percent"] for event in events] == [5, 15, 52, 78, 92, 100]
    assert events[0]["before_tokens"] > 0
    assert events[-1]["after_tokens"] == result.projected_tokens
    assert all(event["generation"] == 1 for event in events)
    assert all("summary" not in event for event in events)


def test_conversation_projection_does_not_count_detached_audit_delivery_body() -> None:
    agent = SimpleNamespace(
        prompts=SimpleNamespace(build=lambda *_args, **_kwargs: "完整输入上下文")
    )
    baseline = _projected_context_tokens(agent, "", [], "当前消息")
    projected = _projected_context_tokens(
        agent,
        "",
        [
            MessageLogEntry(
                message_id="msg-audit",
                thread_id="thread-audit",
                role="assistant",
                content="不应进入普通上下文的审计正文" * 5000,
                metadata={
                    "task_id": "audit-old",
                    "reason": "audit_finding",
                    "background_delivery_reason": "audit_finding_report",
                },
            )
        ],
        "当前消息",
    )

    assert projected == baseline


def test_conversation_compact_does_not_summarize_detached_audit_delivery_body() -> None:
    backend = _SummaryBackend()
    agent = SimpleNamespace(backend=backend)
    rows = [
        MessageLogEntry(
            message_id="msg-audit",
            thread_id="thread-audit",
            role="assistant",
            content="不应进入摘要的新受益人事件正文",
            metadata={
                "task_id": "audit-old",
                "reason": "audit_finding",
                "background_delivery_reason": "audit_finding_report",
            },
        ),
        MessageLogEntry(
            message_id="msg-user",
            thread_id="thread-audit",
            role="user",
            content="普通聊天里的青黛暗号",
        ),
    ]

    _summarize(agent, "", {}, rows)

    assert len(backend.prompts) == 1
    assert "普通聊天里的青黛暗号" in backend.prompts[0]
    assert "不应进入摘要的新受益人事件正文" not in backend.prompts[0]


def _agent(tmp_path, *, context_tokens: int, max_turns: int = 20) -> SimpleAgent:
    config = AgentConfig(
        model_backend="echo",
        my_agent_home=str(tmp_path / "home"),
        prompt_files=[],
        max_tokens=64,
        model_context_window_tokens=context_tokens,
        memory_compact_auto_trigger_percent=50,
        conversation_history_max_turns=max_turns,
    )
    config.config_sources = {"model_context_window_tokens": {"source": "test"}}
    return SimpleAgent(config, tmp_path)


def _request(user_id: str = "ou_alice") -> dict:
    return {
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": f"oc_{user_id}",
            "channel_user_id": user_id,
            "canonical_user_id": user_id,
        }
    }


def _context(agent: SimpleAgent, request: dict, request_id: str, prompt: str):
    return _gateway_conversation_context(
        _GatewayConversationLoadRequest(agent, request, request_id, prompt)
    )


def test_same_thread_accumulates_beyond_recent_turn_setting_until_compact(tmp_path) -> None:
    agent = _agent(tmp_path, context_tokens=1_000_000, max_turns=2)
    request = _request()
    first = _context(agent, request, "gw-create", "开始")
    for index in range(30):
        for role in ("user", "assistant"):
            assert _append_gateway_conversation_message(
                agent,
                {"metadata": {"channel": "feishu"}},
                first,
                request_id=f"gw-{index}-{role}",
                role=role,
                content=f"第 {index} 轮 {role} 内容",
            )

    followup = _context(agent, request, "gw-follow", "继续")

    assert followup.compact_generation == 0
    assert len(followup.history) == 60
    assert followup.history[0][1] == "第 0 轮 user 内容"


def test_compact_keeps_raw_transcript_and_indexes_old_messages_per_owner(tmp_path) -> None:
    agent = _agent(tmp_path, context_tokens=12_000, max_turns=3)
    backend = _SummaryBackend()
    agent.backend = backend
    request = _request()
    first = _context(agent, request, "gw-create", "开始")
    for index in range(12):
        content = ("紫藤暗号 " if index == 0 else "阶段资料 ") + ("内容" * 900)
        for role in ("user", "assistant"):
            assert _append_gateway_conversation_message(
                agent,
                {"metadata": {"channel": "feishu"}},
                first,
                request_id=f"gw-{index}-{role}",
                role=role,
                content=content,
            )

    followup = _context(agent, request, "gw-follow", "我们接着做")
    stored = agent.conversation_store.load_thread(first.thread_id)
    raw_rows = agent.conversation_store.recent_messages(first.thread_id, limit=0)
    hits = agent.local_store.search(
        "紫藤暗号",
        limit=5,
        source_type="conversation_message",
    )

    assert followup.load_errors == ()
    assert followup.compact_generation >= 1
    assert followup.compact_summary.startswith("用户的暗号是紫藤")
    assert backend.calls == 1
    assert stored is not None
    assert stored.compacted_through_message_id
    assert stored.compacted_through_byte_offset > 0
    assert len(raw_rows) == 24
    assert hits
    assert hits[0].metadata["thread_id"] == first.thread_id
    assert str(agent.home_paths.owner_home_dir) == followup.scope.owner_home
    section = _conversation_prompt_section(followup)
    assert "Earlier Conversation Summary" in section
    assert "原始逐条记录仍是事实源" in section
    tail, tail_errors = agent.conversation_store.messages_after_compact_report(stored)
    assert tail_errors == []
    assert tail == [], "当前请求之前的完整历史应一次替换为摘要，原始 transcript 仍保留"


def test_compact_preserves_bounded_complete_recent_turns_and_full_checkpoint(tmp_path) -> None:
    agent = _agent(tmp_path, context_tokens=30_000)
    backend = _SummaryBackend()
    agent.backend = backend
    request = _request("ou_recent_tail")
    context = _context(agent, request, "gw-create", "开始")
    for index in range(20):
        for role in ("user", "assistant"):
            assert _append_gateway_conversation_message(
                agent,
                {"metadata": {"channel": "feishu"}},
                context,
                request_id=f"gw-tail-{index}-{role}",
                role=role,
                content=f"第 {index} 轮 {role} " + ("近期内容" * 75),
            )

    compacted = _gateway_conversation_context(
        _GatewayConversationLoadRequest(agent, request, "gw-follow", "继续")
    )
    stored = agent.conversation_store.load_thread(context.thread_id)
    assert stored is not None
    tail, errors = agent.conversation_store.messages_after_compact_report(stored)
    checkpoint_path = (
        agent.home_paths.owner_compact_dir
        / "conversations"
        / f"{context.thread_id}.jsonl"
    )
    checkpoint = json.loads(
        checkpoint_path.read_text(encoding="utf-8").splitlines()[-1]
    )

    assert errors == []
    assert compacted.compact_generation == 1
    assert len(tail) == 4
    assert [row.content for row in tail] == [
        f"第 {index} 轮 {role} " + ("近期内容" * 75)
        for index in range(18, 20)
        for role in ("user", "assistant")
    ]
    assert checkpoint["schema"] == "conversation_compact_checkpoint.v1"
    assert checkpoint["status"] == "validated_candidate"
    assert checkpoint["checkpoint_id"] == stored.compact_checkpoint_id
    assert checkpoint["previous_checkpoint_id"] == ""
    assert checkpoint["summary"] == stored.summary
    assert checkpoint["summary_sha256"]
    assert checkpoint["source_start_byte_offset"] == 0
    assert checkpoint["source_end_byte_offset"] == stored.compacted_through_byte_offset
    assert checkpoint["source_messages"] == 36
    assert checkpoint["retained_tail_messages"] == 4
    assert checkpoint["retained_tail_message_ids"] == [
        row.message_id for row in tail
    ]
    assert checkpoint["projected_tokens_after"] < checkpoint["trigger_tokens"]
    assert compacted.compact_operation_evidence_ref == str(checkpoint_path)


def test_repeated_compact_checkpoints_form_one_generation_chain(tmp_path) -> None:
    agent = _agent(tmp_path, context_tokens=1_000_000)
    agent.backend = _SummaryBackend()
    request = _request("ou_checkpoint_chain")
    context = _context(agent, request, "gw-create", "开始")
    for index in range(6):
        for role in ("user", "assistant"):
            assert _append_gateway_conversation_message(
                agent,
                {"metadata": {"channel": "feishu"}},
                context,
                request_id=f"gw-chain-{index}-{role}",
                role=role,
                content=f"链路 {index} {role}",
            )

    first = _gateway_conversation_context(
        _GatewayConversationLoadRequest(agent, request, "gw-first", "继续"),
        force_compact=True,
    )
    first_thread = agent.conversation_store.load_thread(context.thread_id)
    assert first_thread is not None
    first_tail, first_tail_errors = (
        agent.conversation_store.messages_after_compact_report(first_thread)
    )
    assert first_tail_errors == []
    assert first_tail == []
    for role in ("user", "assistant"):
        assert _append_gateway_conversation_message(
            agent,
            {"metadata": {"channel": "feishu"}},
            first,
            request_id=f"gw-new-{role}",
            role=role,
            content=f"新一代 {role}",
        )
    second = _gateway_conversation_context(
        _GatewayConversationLoadRequest(agent, request, "gw-second", "再继续"),
        force_compact=True,
    )
    path = (
        agent.home_paths.owner_compact_dir
        / "conversations"
        / f"{context.thread_id}.jsonl"
    )
    checkpoints = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert first.compact_generation == 1
    assert second.compact_generation == 2
    assert [row["generation"] for row in checkpoints] == [1, 2]
    assert checkpoints[1]["previous_checkpoint_id"] == checkpoints[0]["checkpoint_id"]
    assert checkpoints[1]["source_start_byte_offset"] == checkpoints[0][
        "source_end_byte_offset"
    ]


def test_invalid_summary_candidate_never_advances_cursor_and_opens_circuit(tmp_path) -> None:
    agent = _agent(tmp_path, context_tokens=2_000)
    backend = _OversizedSummaryBackend()
    agent.backend = backend
    request = _request("ou_bad_candidate")
    context = _context(agent, request, "gw-create", "开始")
    for index in range(2):
        for role in ("user", "assistant"):
            assert _append_gateway_conversation_message(
                agent,
                {"metadata": {"channel": "feishu"}},
                context,
                request_id=f"gw-bad-{index}-{role}",
                role=role,
                content=f"消息 {index} {role}",
            )

    for _attempt in range(3):
        thread = agent.conversation_store.load_thread(context.thread_id)
        assert thread is not None
        with pytest.raises(
            ConversationCompactError,
            match="did not reach the configured recovery target",
        ):
            prepare_conversation_context(
                agent,
                agent.conversation_store,
                thread,
                current_prompt="继续",
                force=True,
            )
    calls_before_circuit = backend.calls
    thread = agent.conversation_store.load_thread(context.thread_id)
    assert thread is not None
    agent.config.model_context_window_tokens = 1_000_000
    normal_context = prepare_conversation_context(
        agent,
        agent.conversation_store,
        thread,
        current_prompt="普通短消息仍可继续",
    )
    assert normal_context.compacted is False
    assert backend.calls == calls_before_circuit
    with pytest.raises(ConversationCompactCircuitOpenError):
        prepare_conversation_context(
            agent,
            agent.conversation_store,
            thread,
            current_prompt="继续",
            force=True,
        )
    stored = agent.conversation_store.load_thread(context.thread_id)

    assert stored is not None
    assert backend.calls == calls_before_circuit
    assert stored.compact_generation == 0
    assert stored.compacted_through_message_id == ""
    assert stored.compacted_through_byte_offset == 0
    assert stored.compact_checkpoint_id == ""
    assert stored.summary == ""
    assert stored.compact_consecutive_failures == 3
    assert stored.compact_failure_code == "COMPACT_CANDIDATE_TOO_LARGE"
    checkpoint_path = (
        agent.home_paths.owner_compact_dir
        / "conversations"
        / f"{context.thread_id}.jsonl"
    )
    assert not checkpoint_path.exists()


def test_compact_circuit_half_opens_after_cooldown_and_success_resets_it(tmp_path) -> None:
    agent = _agent(tmp_path, context_tokens=1_000_000)
    agent.backend = _SummaryBackend()
    request = _request("ou_compact_half_open")
    context = _context(agent, request, "gw-create", "开始")
    for role in ("user", "assistant"):
        assert _append_gateway_conversation_message(
            agent,
            {"metadata": {"channel": "feishu"}},
            context,
            request_id=f"gw-half-open-{role}",
            role=role,
            content=f"待压缩 {role}",
        )
    for failure_index in range(3):
        agent.conversation_store.record_compact_failure(
            context.thread_id,
            failure_code=f"TEST_FAILURE_{failure_index}",
            expected_generation=0,
            now=1.0 + failure_index,
        )

    thread = agent.conversation_store.load_thread(context.thread_id)
    assert thread is not None
    assert thread.compact_consecutive_failures == 3
    compacted = prepare_conversation_context(
        agent,
        agent.conversation_store,
        thread,
        current_prompt="继续",
        force=True,
    )
    stored = agent.conversation_store.load_thread(context.thread_id)

    assert compacted.compacted is True
    assert stored is not None
    assert stored.compact_generation == 1
    assert stored.compact_consecutive_failures == 0
    assert stored.compact_failure_updated_at == 0.0
    assert stored.compact_failure_code == ""


def test_checkpoint_write_failure_does_not_commit_candidate(tmp_path, monkeypatch) -> None:
    agent = _agent(tmp_path, context_tokens=1_000_000)
    agent.backend = _SummaryBackend()
    request = _request("ou_checkpoint_failure")
    context = _context(agent, request, "gw-create", "开始")
    for role in ("user", "assistant"):
        assert _append_gateway_conversation_message(
            agent,
            {"metadata": {"channel": "feishu"}},
            context,
            request_id=f"gw-checkpoint-{role}",
            role=role,
            content=f"待压缩 {role}",
        )

    def fail_checkpoint(*_args, **_kwargs):
        raise OSError("checkpoint unavailable")

    monkeypatch.setattr(
        "agent_py_agent.agent.conversation.compact.write_compact_checkpoint",
        fail_checkpoint,
    )
    thread = agent.conversation_store.load_thread(context.thread_id)
    assert thread is not None
    with pytest.raises(OSError, match="checkpoint unavailable"):
        prepare_conversation_context(
            agent,
            agent.conversation_store,
            thread,
            current_prompt="继续",
            force=True,
        )
    stored = agent.conversation_store.load_thread(context.thread_id)

    assert stored is not None
    assert stored.compact_generation == 0
    assert stored.compacted_through_byte_offset == 0
    assert stored.compact_checkpoint_id == ""
    assert stored.compact_consecutive_failures == 1


def test_recent_tail_keeps_typed_operation_evidence_visible(tmp_path) -> None:
    agent = _agent(tmp_path, context_tokens=30_000)
    agent.backend = _SummaryBackend()
    request = _request("ou_recent_operation")
    context = _context(agent, request, "gw-create", "开始")
    for index in range(20):
        assert _append_gateway_conversation_message(
            agent,
            {"metadata": {"channel": "feishu"}},
            context,
            request_id=f"gw-operation-{index}-user",
            role="user",
            content=f"第 {index} 步 " + ("近期内容" * 75),
        )
        verification = (
            {
                "schema": "operation_verification.public.v1",
                "status": "succeeded",
                "operation_count": 1,
                "counts": {"succeeded": 1},
                "groups": [
                    {
                        "tool": "write_file",
                        "action": "write",
                        "label": "write_file/write",
                        "status": "succeeded",
                        "count": 1,
                        "replayed": False,
                    }
                ],
            }
            if index == 19
            else None
        )
        assert _append_gateway_conversation_message(
            agent,
            {"metadata": {"channel": "feishu"}},
            context,
            request_id=f"gw-operation-{index}-assistant",
            role="assistant",
            content=f"完成第 {index} 步 " + ("近期内容" * 75),
            operation_verification=verification,
        )

    compacted = _gateway_conversation_context(
        _GatewayConversationLoadRequest(agent, request, "gw-follow", "继续")
    )
    rendered = _conversation_prompt_section(compacted)

    assert compacted.recent_operation_evidence["operation_count"] == 1
    assert "Program-Verified Operations From Recent Raw History" in rendered
    assert "write_file/write" in rendered


def test_recent_tail_selection_uses_roles_not_message_wording() -> None:
    rows = [
        MessageLogEntry(
            message_id=f"msg-{index}-{role}",
            thread_id="thread-1",
            role=role,
            content=f"任意语言 {index} {role}",
        )
        for index in range(6)
        for role in ("user", "assistant")
    ]
    rows.append(
        MessageLogEntry(
            message_id="msg-interrupted",
            thread_id="thread-1",
            role="user",
            content="没有 assistant 的中断输入",
        )
    )

    prefix, tail = split_recent_complete_turns(
        rows,
        max_turns=2,
        max_tail_tokens=1_000_000,
    )

    assert [row.message_id for row in prefix] == [
        f"msg-{index}-{role}"
        for index in range(4)
        for role in ("user", "assistant")
    ]
    assert [row.message_id for row in tail] == [
        *[
            f"msg-{index}-{role}"
            for index in range(4, 6)
            for role in ("user", "assistant")
        ],
        "msg-interrupted",
    ]


def test_compact_and_history_index_keep_typed_operation_verification(tmp_path) -> None:
    agent = _agent(tmp_path, context_tokens=1_000_000)
    request = _request()
    context = _context(agent, request, "gw-create", "开始")
    verification = {
        "schema": "operation_verification.public.v1",
        "status": "succeeded",
        "operation_count": 1,
        "counts": {"succeeded": 1},
        "groups": [
            {
                "tool": "remember",
                "action": "remove",
                "label": "remember/remove",
                "status": "succeeded",
                "count": 1,
                "replayed": False,
            }
        ],
    }
    assert _append_gateway_conversation_message(
        agent,
        {"metadata": {"channel": "feishu"}},
        context,
        request_id="gw-operation",
        role="assistant",
        content="已经处理。",
        operation_verification=verification,
    )

    row = agent.conversation_store.recent_messages(context.thread_id, limit=1)[0]
    stored_verification = row.metadata["operation_verification"]
    projected = _summary_content(row)
    indexed = agent.local_store.get_record(
        agent.local_store.make_record_id(
            "conversation_message",
            f"{context.thread_id}:{row.message_id}",
        )
    )

    assert projected == {
        "content": "已经处理。",
        "operation_verification": stored_verification,
    }
    assert indexed is not None
    assert indexed.metadata["operation_verification"] == stored_verification


def test_compact_keeps_operation_evidence_outside_conflicting_model_summary(tmp_path) -> None:
    agent = _agent(tmp_path, context_tokens=1_000_000)
    agent.backend = _ConflictingSummaryBackend()
    request = _request()
    context = _context(agent, request, "gw-create", "开始")
    verification = {
        "schema": "operation_verification.public.v1",
        "status": "succeeded",
        "operation_count": 1,
        "counts": {"succeeded": 1},
        "groups": [
            {
                "tool": "remember",
                "action": "list",
                "label": "remember/list",
                "status": "succeeded",
                "count": 1,
                "replayed": False,
                "call_id": "must-not-survive",
            }
        ],
    }
    assert _append_gateway_conversation_message(
        agent,
        {"metadata": {"channel": "feishu"}},
        context,
        request_id="gw-conflicting-claim",
        role="assistant",
        content="已经删除海王星项目记忆。",
        operation_verification=verification,
    )

    followup = _gateway_conversation_context(
        _GatewayConversationLoadRequest(
            agent,
            request,
            "gw-follow",
            "请准确说明是否真的删除。",
        ),
        force_compact=True,
    )

    assert followup.compact_summary == "助手已经成功删除海王星项目记忆。"
    evidence = followup.compact_operation_evidence
    assert evidence["schema"] == "conversation_operation_evidence.v1"
    assert evidence["coverage"] == "complete"
    assert evidence["assistant_message_count"] == 1
    assert evidence["verified_assistant_message_count"] == 1
    assert evidence["operation_event_count"] == 1
    assert evidence["operation_count"] == 1
    assert evidence["events"][0]["verification"]["groups"] == [
        {
            "tool": "remember",
            "action": "list",
            "label": "remember/list",
            "status": "succeeded",
            "count": 1,
            "replayed": False,
        }
    ]
    section = _conversation_prompt_section(followup)
    assert section.index("助手已经成功删除") < section.index(
        "Program-Verified Operations From Compacted History"
    )
    assert "remember/list" in section
    assert "remember/remove" not in section
    assert "must-not-survive" not in section


def test_compact_operation_evidence_is_bounded_and_reports_legacy_gaps() -> None:
    rows = [
        MessageLogEntry(
            message_id=f"msg-{index}",
            thread_id="thread-1",
            role="assistant",
            content=f"assistant {index}",
            metadata=(
                {
                    "operation_verification": {
                        "schema": "operation_verification.public.v1",
                        "status": "succeeded",
                        "operation_count": 1,
                        "counts": {"succeeded": 1},
                        "groups": [
                            {
                                "tool": "remember",
                                "action": "list",
                                "label": "remember/list",
                                "status": "succeeded",
                                "count": 1,
                                "replayed": False,
                            }
                        ],
                    }
                }
                if index != 7
                else {}
            ),
        )
        for index in range(40)
    ]

    evidence = _merge_compact_operation_evidence({}, rows)

    assert evidence["assistant_message_count"] == 40
    assert evidence["verified_assistant_message_count"] == 39
    assert evidence["unverified_assistant_message_count"] == 1
    assert evidence["coverage"] == "partial"
    assert evidence["operation_event_count"] == 39
    assert evidence["operation_count"] == 39
    assert len(evidence["events"]) == 32
    assert evidence["omitted_event_count"] == 7
    assert evidence["events"][0]["assistant_sequence"] == 9
    assert evidence["events"][-1]["assistant_sequence"] == 40


def test_prompt_operation_evidence_keeps_full_counts_and_only_recent_details() -> None:
    evidence = {
        "schema": "conversation_operation_evidence.v1",
        "coverage": "complete",
        "assistant_message_count": 20,
        "verified_assistant_message_count": 20,
        "unverified_assistant_message_count": 0,
        "operation_event_count": 12,
        "operation_count": 12,
        "counts": {"succeeded": 12},
        "omitted_event_count": 3,
        "events": [
            {
                "assistant_sequence": index,
                "verification": {
                    "schema": "operation_verification.public.v1",
                    "status": "succeeded",
                    "operation_count": 1,
                    "groups": [{"label": f"event-{index}"}],
                },
            }
            for index in range(12)
        ],
    }

    projection = _prompt_operation_evidence(evidence)
    section = _conversation_prompt_section(
        _GatewayConversationContext(
            thread_id="thread-bounded-evidence",
            compact_operation_evidence=evidence,
            compact_operation_evidence_ref="/owner/compact/conversations/thread.jsonl",
        )
    )

    assert projection["operation_event_count"] == 12
    assert projection["operation_count"] == 12
    assert projection["counts"] == {"succeeded": 12}
    assert projection["omitted_event_count"] == 11
    assert projection["prompt_event_limit"] == 4
    assert [row["assistant_sequence"] for row in projection["events"]] == [8, 9, 10, 11]
    assert "event-0" not in section
    assert "event-8" in section
    assert "/owner/compact/conversations/thread.jsonl" in section
    assert len(evidence["events"]) == 12


def test_summary_content_does_not_infer_operation_from_assistant_prose() -> None:
    row = MessageLogEntry(
        message_id="msg-1",
        thread_id="thread-1",
        role="assistant",
        content="我已经删除了记忆。",
        metadata={},
    )

    assert _summary_content(row) == "我已经删除了记忆。"


def test_forced_compact_keeps_current_gateway_turn_out_of_summary(tmp_path) -> None:
    agent = _agent(tmp_path, context_tokens=1_000_000, max_turns=3)
    backend = _SummaryBackend()
    agent.backend = backend
    request = _request()
    first = _context(agent, request, "gw-create", "开始")
    for index in range(2):
        for role in ("user", "assistant"):
            assert _append_gateway_conversation_message(
                agent,
                {"metadata": {"channel": "feishu"}},
                first,
                request_id=f"gw-old-{index}-{role}",
                role=role,
                content=f"旧消息 {index} {role}",
            )
    current_marker = "本轮输入不能进入较早摘要"
    assert _append_gateway_conversation_message(
        agent,
        {"metadata": {"channel": "feishu"}},
        first,
        request_id="gw-current",
        role="user",
        content=current_marker,
    )

    refreshed = _gateway_conversation_context(
        _GatewayConversationLoadRequest(agent, request, "gw-current", current_marker),
        force_compact=True,
    )
    stored = agent.conversation_store.load_thread(first.thread_id)
    tail, errors = agent.conversation_store.messages_after_compact_report(stored)

    assert errors == []
    assert refreshed.compact_generation == 1
    assert backend.calls == 1
    assert current_marker not in backend.prompts[0]
    assert any(
        row.metadata.get("gateway_request_id") == "gw-current" and row.content == current_marker
        for row in tail
    )
    assert all(current_marker not in content for _role, content in refreshed.history)
    event_path = agent.home_paths.owner_compact_dir / "conversations" / f"{first.thread_id}.jsonl"
    assert '"forced": true' in event_path.read_text(encoding="utf-8")


def test_gateway_thread_marks_runtime_context_as_conversation_scoped(tmp_path) -> None:
    agent = _agent(tmp_path, context_tokens=1_000_000)
    request = _request()
    conversation = _context(agent, request, "gw-context", "开始")
    context = SimpleNamespace(
        request_id="gw-context",
        request_path=tmp_path / "request.json",
        response_path=tmp_path / "response.json",
        on_chunk=lambda _chunk: None,
    )

    params = _gateway_run_params(
        _GatewayRunParamsRequest(request, context, conversation, "开始")
    )

    assert params.context_scope == "conversation"


def test_conversation_compact_keeps_persona_and_related_memory_in_next_prompt(tmp_path) -> None:
    """Compact 只替换旧 transcript 的模型视图，不应吞掉 owner Persona/Memory。"""

    agent = _agent(tmp_path, context_tokens=30_000, max_turns=3)
    backend = _SummaryBackend()
    agent.backend = backend
    agent.home_paths.owner_soul_md.write_text("人格原则：耐心、直接。\n", encoding="utf-8")
    agent.home_paths.owner_user_md.write_text("称呼用户为青禾。\n", encoding="utf-8")
    request = _request("ou_persona_memory")
    first = _context(agent, request, "gw-create", "开始")
    for index in range(20):
        for role in ("user", "assistant"):
            assert _append_gateway_conversation_message(
                agent,
                {"metadata": {"channel": "feishu"}},
                first,
                request_id=f"gw-old-{index}-{role}",
                role=role,
                content=f"旧消息 {index} {role} " + ("阶段内容" * 75),
            )

    current_prompt = "暗号是什么"
    compacted = _gateway_conversation_context(
        _GatewayConversationLoadRequest(agent, request, "gw-current", current_prompt)
    )
    context = SimpleNamespace(
        request_id="gw-current",
        request_path=tmp_path / "request.json",
        response_path=tmp_path / "response.json",
        on_chunk=lambda _chunk: None,
    )
    params = _gateway_run_params(
        _GatewayRunParamsRequest(request, context, compacted, current_prompt)
    )
    rendered = agent.prompts.build(
        current_prompt,
        memories=[MemoryRecord(role="user", content="长期暗号是白鹭湾", kind="preference")],
        inject=params.inject,
        context_scope=params.context_scope,
    )

    assert compacted.compact_generation == 1
    history_seed = params.conversation_history_seed
    assert history_seed is not None
    assert history_seed.compact_summary == compacted.compact_summary
    assert history_seed.compact_generation == 1
    assert "人格原则：耐心、直接。" in rendered
    assert "称呼用户为青禾。" in rendered
    assert "长期暗号是白鹭湾" in rendered
    history_text = "\n".join(content for _role, content in history_seed.messages)
    assert "旧消息 0" not in history_text
    assert "旧消息 19 user" in history_text
    assert "旧消息 19 assistant" in history_text


def test_conversation_search_index_does_not_cross_owner_local_stores(tmp_path) -> None:
    alice = _agent(tmp_path / "alice", context_tokens=1_000_000)
    bob = _agent(tmp_path / "bob", context_tokens=1_000_000)
    alice_context = _context(alice, _request("ou_alice"), "gw-a", "开始")
    assert _append_gateway_conversation_message(
        alice,
        {"metadata": {"channel": "feishu"}},
        alice_context,
        request_id="gw-a",
        role="user",
        content="只属于 Alice 的银杏计划",
    )

    assert alice.local_store.search("银杏计划", source_type="conversation_message")
    assert bob.local_store.search("银杏计划", source_type="conversation_message") == []
