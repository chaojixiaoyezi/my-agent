from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.conversation.compact import (
    _merge_compact_operation_evidence,
    _projected_context_tokens,
    _summary_content,
)
from agent_py_agent.agent.conversation.models import MessageLogEntry
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.request_execution import (
    _append_gateway_conversation_message,
    _conversation_prompt_section,
    _gateway_conversation_context,
    _gateway_run_params,
    _GatewayConversationLoadRequest,
    _GatewayRunParamsRequest,
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

    agent = _agent(tmp_path, context_tokens=1_000_000, max_turns=3)
    backend = _SummaryBackend()
    agent.backend = backend
    agent.home_paths.owner_soul_md.write_text("人格原则：耐心、直接。\n", encoding="utf-8")
    agent.home_paths.owner_user_md.write_text("称呼用户为青禾。\n", encoding="utf-8")
    request = _request("ou_persona_memory")
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

    current_prompt = "暗号是什么"
    compacted = _gateway_conversation_context(
        _GatewayConversationLoadRequest(agent, request, "gw-current", current_prompt),
        force_compact=True,
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
    assert "# Earlier Conversation Summary" in rendered
    assert "人格原则：耐心、直接。" in rendered
    assert "称呼用户为青禾。" in rendered
    assert "长期暗号是白鹭湾" in rendered
    assert rendered.count("旧消息") == 0


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
