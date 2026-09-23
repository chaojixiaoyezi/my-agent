"""跨片 Compact 先校验完整请求，再沿真实 Store/checkpoint/CAS 提交；只替身摘要模型。"""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.context_compactor import runtime_compact_policy
from agent_py_agent.agent.backends.base import BackendOptions
from agent_py_agent.agent.backends.http import HttpBackend
from agent_py_agent.agent.conversation import compact as compact_module
from agent_py_agent.agent.conversation.active_turn_compact import (
    ActiveTurnArchiveCompactRequest,
    compact_carried_active_turn_archive,
    model_visible_active_turn_tool_calls,
)
from agent_py_agent.agent.conversation.authority import CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR
from agent_py_agent.agent.conversation.compact_checkpoint import committed_compact_checkpoint_chain
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.conversation.compact_projection import (
    ConversationCompactProjection,
    ConversationCompactSource,
)
from agent_py_agent.agent.conversation.compact_provider_surface import (
    ConversationCompactProviderSurface,
)
from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE, CompactScope
from agent_py_agent.agent.conversation.compact_summary_view import (
    AppliedCompactContext,
    resolve_compact_summary_view,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.memory_archive import compact_semantic_summary, estimate_tokens
from agent_py_agent.agent.prompting_parts.cache_layout import prompt_cache_layout
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests._tool_runtime_harness import canonical_history_call

_SUMMARY = (
    "[compact-live-handoff.v1]\ncurrent_progress: 已核对来源\nuser_constraints: 保留原要求\n"
    "completed: 已读取文件\nfailures: 无\nunresolved: 继续核对\nnext_step: 检查下一项"
)


# LLM: 测试只替换摘要生成，原边界选择、磁盘归档、checkpoint 内容封印和原子 CAS 全部真实执行。
# 函数用途: 在临时 home 初始化可追踪停止和摘要调用次数的 carried 压缩环境，不使用网络。
@pytest.fixture
def carried_case(tmp_path, monkeypatch):
    config = AgentConfig(
        model_backend="echo", my_agent_home=str(tmp_path / "home"), prompt_files=[],
        max_tokens=64, model_context_window_tokens=10_000, model_context_window_explicit=True,
        memory_compact_auto_trigger_percent=90,
    )
    config.config_sources = {"model_context_window_tokens": {"source": "test"}}
    agent = SimpleAgent(config, tmp_path)
    store = agent.conversation_store
    thread = store.threads.get_or_create({
        "canonical_user_id": "carried-owner", "channel": "test",
        "channel_conversation_id": "carried-projection", "channel_user_id": "carried-owner",
    })
    case = SimpleNamespace(
        agent=agent, store=store, thread=thread, records=[_record(index) for index in range(3)],
        summaries=[], progress=[], stop=False, stop_after_summary=False,
        attrs={CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True, "conversation_thread_id": thread.thread_id},
        checkpoint_path=agent.home_paths.owner_compact_dir / "conversations" / f"{thread.thread_id}.jsonl",
    )

    def summarize(request):
        case.summaries.append(request)
        if case.stop_after_summary:
            case.stop = True
        return _SUMMARY

    monkeypatch.setattr(compact_semantic_summary, "summarize_live_tool_history", summarize)
    return case


# LLM: 被压缩调用的身份来自原 ToolCall，不能以摘要调用的新 request/attempt 补全。
# 函数用途: 创建可在真实 checkpoint 中验证来源边界的完整工具记录。
def _record(index):
    call = canonical_history_call(
        "read_file", {"path": f"evidence-{index}.txt"}, call_id=f"call-{index}",
        run_id="origin-run", attempt_id="origin-attempt", turn_id=f"origin-turn-{index}",
    )
    return {
        **call.to_dict(), "tool": call.tool_name, "ok": True,
        "parameters": dict(call.arguments), "model_parameters": dict(call.arguments),
        "output_preview": f"ORIGINAL_EVIDENCE_{index}", "tool_round": index + 1,
    }


# LLM: helper 只包装原入口，不伪造 thread/检查点；获选 projection 仍由被测真实入口返还。
# 函数用途: 用本例明确的归档与准备身份执行一次跨片压缩。
def _compact(case, *, records=None, **fields):
    return compact_carried_active_turn_archive(
        case.agent, case.store, case.thread, case.records if records is None else records,
        ActiveTurnArchiveCompactRequest(
            task_attributes=case.attrs, request_id="resume-request", attempt_id="resume-attempt",
            task_prompt="继续核对原始材料", **fields,
        ),
    )


# LLM: CAS 前失败允许原熔断记录变化，但摘要覆盖权、游标和原归档不得变化。
# 函数用途: 从磁盘和原可见性入口验证候选没有获得隐藏来源的权限。
def _assert_uncommitted(case):
    current = case.store.threads.require(case.thread.thread_id)
    assert current.compact_generation == 0 and current.compact_checkpoint_id == ""
    assert current.summary == "" and current.compacted_through_message_id == ""
    assert current.compacted_through_byte_offset == 0
    assert committed_compact_checkpoint_chain(case.agent, current) == ()
    assert model_visible_active_turn_tool_calls(case.agent, case.attrs, case.records) == case.records
    return current


def test_complete_projection_commits_its_exact_material_and_full_measurement(carried_case):
    case = carried_case
    original = deepcopy(case.records)
    calls, projections = [], []
    frozen = {
        "prompt": "固定宿主要求", "tools": [{"name": "read_file", "input_schema": {"type": "object"}}],
        "media": [{"type": "image", "ref": "prepared-image"}], "runtime_guidance": "同一准备内的引导",
    }

    def project(summary, retained_records, expected_generation):
        assert case.store.threads.require(case.thread.thread_id).compact_generation == 0
        assert not case.checkpoint_path.exists()
        calls.append((summary, retained_records, expected_generation))
        material = {**frozen, "summary": summary, "retained_records": retained_records}
        projection = ConversationCompactProjection(estimate_tokens(material), material)
        projections.append(projection)
        return projection

    result = _compact(
        case, request_projector=project, projected_tokens_before=12_345,
        progress_callback=lambda event: case.progress.append(event),
    )

    assert len(calls) == len(case.summaries) == 1
    assert calls == [(_SUMMARY, tuple(case.records[1:]), 1)]
    assert result.compacted and result.request_projection is projections[0]
    assert result.request_projection.material["tools"] is frozen["tools"]
    assert result.request_projection.material["media"] is frozen["media"]
    assert result.projected_tokens_before == 12_345
    assert result.projected_tokens_after == projections[0].projected_tokens
    assert case.records == original
    current = case.store.threads.require(case.thread.thread_id)
    assert result.thread == current and current.compact_generation == 1
    checkpoint, = committed_compact_checkpoint_chain(case.agent, current)
    assert checkpoint["projected_tokens_before"] == 12_345
    assert checkpoint["projected_tokens_after"] == projections[0].projected_tokens
    assert checkpoint["source_tool_refs"][0]["attempt_id"] == "origin-attempt"
    assert model_visible_active_turn_tool_calls(case.agent, case.attrs, case.records) == case.records[1:]
    assert {event["before_tokens"] for event in case.progress} == {12_345}
    assert case.progress[-1]["after_tokens"] == projections[0].projected_tokens


@pytest.mark.parametrize("missing", ["call_id", "run_id", "attempt_id", "turn_id"])
def test_candidate_retains_unknown_archive_identity_in_original_order(carried_case, missing):
    case = carried_case
    unknown = _record(7)
    unknown.pop(missing)
    case.records.insert(1, unknown)
    original = deepcopy(case.records)
    retained = []

    def project(_summary, records, _generation):
        retained.extend(records)
        return ConversationCompactProjection(100, records)

    result = _compact(case, request_projector=project)
    assert result.compacted and case.records == original
    assert retained == case.records[1:]
    assert result.retained_call_ids == ("call-1", "call-2")
    checkpoint, = committed_compact_checkpoint_chain(case.agent, result.thread)
    assert checkpoint["source_tool_call_ids"] == ["call-0"]
    assert [ref["call_id"] for ref in checkpoint["retained_tool_refs"]] == ["call-1", "call-2"]
    assert model_visible_active_turn_tool_calls(case.agent, case.attrs, case.records) == retained


@pytest.mark.parametrize("tokens", [None, "unknown", -1, True, 1.5, "missing-material"])
def test_unknown_projection_never_falls_back_to_small_estimate(carried_case, tokens):
    case = carried_case
    projection = (
        None if tokens is None else ConversationCompactProjection(
            1 if tokens == "missing-material" else tokens,
            None if tokens == "missing-material" else object(),
        )
    )
    with pytest.raises(ConversationCompactError) as error:
        _compact(case, request_projector=lambda *_: projection)
    assert error.value.code == "COMPACT_REQUEST_PROJECTION_UNKNOWN"
    current = _assert_uncommitted(case)
    assert current.compact_consecutive_failures == 1 and not case.checkpoint_path.exists()


@pytest.mark.parametrize("tokens", [9_000, 12_000])
def test_complete_request_at_or_above_trigger_cannot_commit(carried_case, tokens):
    case = carried_case
    with pytest.raises(ConversationCompactError) as error:
        _compact(case, request_projector=lambda *_: ConversationCompactProjection(tokens, object()))
    assert error.value.code == "COMPACT_CANDIDATE_TOO_LARGE"
    _assert_uncommitted(case)
    assert len(case.summaries) == 1 and not case.checkpoint_path.exists()


@pytest.mark.parametrize("tokens,accepted", [(2_999, True), (3_000, False), (4_000, False)])
def test_actual_backend_output_reserve_limits_the_same_gate(carried_case, tokens, accepted):
    case = carried_case
    case.agent.backend = HttpBackend(BackendOptions(
        api_base="https://example.invalid", api_key="", model_name="offline-capacity",
        max_tokens=7_000, context_window_tokens=10_000,
    ))
    project = lambda *_: ConversationCompactProjection(tokens, object())  # noqa: E731
    if accepted:
        result = _compact(case, request_projector=project)
        assert result.compacted and result.projected_tokens_after == tokens
    else:
        with pytest.raises(ConversationCompactError) as error:
            _compact(case, request_projector=project)
        assert error.value.code == "COMPACT_CANDIDATE_TOO_LARGE"
        _assert_uncommitted(case)
        assert not case.checkpoint_path.exists()


@pytest.mark.parametrize("stop_at", ["summary", "projection", "committing"])
def test_cancellation_discards_projected_material_without_publishing(carried_case, stop_at):
    case = carried_case
    case.stop_after_summary = stop_at == "summary"
    projections = []

    def project(*_):
        projections.append(ConversationCompactProjection(100, object()))
        if stop_at == "projection":
            case.stop = True
        return projections[-1]

    def progress(event):
        case.progress.append(event)
        if event["stage"] == stop_at:
            case.stop = True

    with pytest.raises(InterruptedError):
        _compact(case, request_projector=project, interrupt_check=lambda: case.stop, progress_callback=progress)
    current = _assert_uncommitted(case)
    assert current.compact_consecutive_failures == 0
    assert len(projections) == (0 if stop_at == "summary" else 1)
    assert case.progress[-1]["stage"] == "candidate_discarded"
    assert case.checkpoint_path.exists() is (stop_at == "committing")


def test_projection_does_not_bypass_real_generation_cas(carried_case):
    case = carried_case
    winner = []

    def project(*_):
        winner.append(_compact(case, records=[_record(10)]))
        return ConversationCompactProjection(100, object())

    with pytest.raises(RuntimeError, match="generation changed"):
        _compact(case, request_projector=project)
    current = case.store.threads.require(case.thread.thread_id)
    assert current == winner[0].thread
    checkpoint, = committed_compact_checkpoint_chain(case.agent, current)
    assert checkpoint["source_tool_call_ids"] == ["call-10"]
    assert model_visible_active_turn_tool_calls(case.agent, case.attrs, case.records) == case.records
    assert len(case.checkpoint_path.read_text().splitlines()) == 2


def test_existing_provider_surface_and_applied_summary_reach_original_summary_api(carried_case):
    case = carried_case
    first = _compact(case)
    case.thread = first.thread
    tools = ({"name": "read_file", "input_schema": {"type": "object"}},)
    surface = ConversationCompactProviderSurface(
        "原先已准备的稳定前缀", tools, "原先 system 规则",
        (("prompt.tool_recommendations", "原先动态名卡"),),
    )
    interrupt_check = lambda: case.stop  # noqa: E731
    result = _compact(
        case, records=[_record(10)], provider_surface=surface, interrupt_check=interrupt_check,
        request_projector=lambda *_: ConversationCompactProjection(100, object()),
    )
    request = case.summaries[-1]
    assert result.compacted and result.thread.compact_generation == 2
    assert len(case.summaries) == 2
    assert request.tools is tools and request.system_instruction == surface.system_instruction
    assert request.interrupt_check is interrupt_check
    assert prompt_cache_layout(request.provider_prompt).stable_prefix == surface.stable_prompt_prefix
    history = json.dumps(request.provider_history_messages, ensure_ascii=False)
    assert _SUMMARY in history.replace("\\n", "\n") and "原先动态名卡" in history
    assert request.previous_summary == _SUMMARY
    checkpoint = committed_compact_checkpoint_chain(case.agent, result.thread)[-1]
    assert checkpoint["summary_base_checkpoint_id"] == first.thread.compact_checkpoint_id


@pytest.mark.parametrize("kind", ["task", "turn"])
def test_scoped_active_checkpoint_inherits_its_distinct_evidence_not_global(carried_case, monkeypatch, kind):
    case = carried_case
    monkeypatch.setattr(compact_module, "_summarize", lambda _agent, _previous, _evidence, rows, **_: rows[0].content)
    scope = (CompactScope(kind="task", task_id="detached-task", created_at=1.0) if kind == "task"
             else CompactScope(kind="turn", turn_id="audit-turn"))

    # LLM: 操作证据由真实 transcript merger 从 assistant 行计算，不手填 thread 投影或 checkpoint。
    # 函数用途: 先建立非空全局证据，再建立内容不同的局部证据，供后续活动压缩继承。
    def compact_transcript(selected_scope, count, label):
        rows = tuple(case.store.messages.append({
            "thread_id": case.thread.thread_id, "role": "assistant", "content": label,
            "metadata": {"conversation_request_id": f"{label}-{index}"},
        }) for index in range(count))
        context = AppliedCompactContext(
            case.thread.thread_id, selected_scope,
            resolve_compact_summary_view(case.agent, case.thread, selected_scope),
        )
        source = ConversationCompactSource(
            case.thread, rows, runtime_compact_policy(case.agent), {}, context,
        )
        result = compact_module.prepare_conversation_context(
            case.agent, case.store, case.thread,
            options=compact_module.ConversationCompactOptions(
                source=source, force=True,
                request_projector=lambda _: ConversationCompactProjection(100, object()),
            ),
        )
        assert result.compacted
        case.thread = result.thread
        return result.thread

    global_thread = compact_transcript(THREAD_COMPACT_SCOPE, 1, "全局摘要")
    local_thread = compact_transcript(scope, 2, "局部摘要")
    local = resolve_compact_summary_view(case.agent, local_thread, scope)
    assert global_thread.compact_operation_evidence["assistant_message_count"] == 1
    assert local.operation_evidence["assistant_message_count"] == 2
    assert local.operation_evidence != global_thread.compact_operation_evidence
    context = AppliedCompactContext(local_thread.thread_id, scope, local)

    result = _compact(
        case, compact_context=context,
        request_projector=lambda *_: ConversationCompactProjection(100, object()),
    )

    committed = case.store.threads.require(case.thread.thread_id)
    actual = resolve_compact_summary_view(case.agent, committed, scope)
    assert result.compacted and result.thread == committed and committed.compact_generation == 3
    assert actual.checkpoint_id == committed.compact_checkpoint_id
    assert actual.operation_evidence == context.view.operation_evidence
    assert committed.compact_operation_evidence == global_thread.compact_operation_evidence
    assert committed.summary == global_thread.summary
    assert case.summaries[-1].previous_summary == local.summary
    checkpoint = committed_compact_checkpoint_chain(case.agent, committed)[-1]
    assert checkpoint["summary_base_checkpoint_id"] == local.checkpoint_id
    assert checkpoint["operation_evidence"] == local.operation_evidence
