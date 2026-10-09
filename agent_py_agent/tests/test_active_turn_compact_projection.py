"""跨片 Compact 先校验完整请求，再沿真实 Store/checkpoint/CAS 提交；只替身摘要模型。"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.context_compactor import runtime_compact_policy
from agent_py_agent.agent.backends import http
from agent_py_agent.agent.backends.base import BackendOptions
from agent_py_agent.agent.backends.http import HttpBackend
from agent_py_agent.agent.backends.openai_chat import OpenAICompatibleBackend
from agent_py_agent.agent.backends.tool_ir import AssistantTurn, UserTurn
from agent_py_agent.agent.common.cancellation import ToolCancelled
from agent_py_agent.agent.conversation import compact as compact_module
from agent_py_agent.agent.conversation.active_turn_compact import (
    ActiveTurnArchiveCompactRequest,
    ActiveTurnCacheFork,
    compact_carried_active_turn_archive,
    model_visible_active_turn_tool_calls,
)
from agent_py_agent.agent.conversation.authority import CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR
from agent_py_agent.agent.conversation.compact_checkpoint import committed_compact_checkpoint_chain
from agent_py_agent.agent.conversation.compact_guard import (
    CompactCapacityFacts,
    ConversationCompactError,
)
from agent_py_agent.agent.conversation.compact_progress import COMPACT_CAPACITY_PROGRESS_FIELDS
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
from agent_py_agent.agent.conversation.compact_tool_summary import compact_tool_summary_text
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.memory_archive import compact_semantic_summary, estimate_tokens
from agent_py_agent.agent.memory_archive.compact_semantic_summary import (
    summarize_live_tool_history as _real_summarize_live_tool_history,
)
from agent_py_agent.agent.prompting_parts.cache_layout import prompt_cache_layout
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_history_call,
    canonical_history_result,
)

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
        max_tokens=64, model_context_window_tokens=10_000,
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
    assert calls == [(_SUMMARY, (), 1)]
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
    assert model_visible_active_turn_tool_calls(case.agent, case.attrs, case.records) == []
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
    assert retained == [unknown]
    assert result.retained_call_ids == ()
    checkpoint, = committed_compact_checkpoint_chain(case.agent, result.thread)
    assert checkpoint["source_tool_call_ids"] == ["call-0", "call-1", "call-2"]
    assert checkpoint["retained_tool_refs"] == []
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
    projected, measured = [], []

    def project(summary, records, _generation):
        projected.append((summary, list(records)))
        return ConversationCompactProjection(tokens, object())

    # 宿主的“只计量、不提交”入口：只在候选真被拒时按预计代次调用一次。
    def measure(generation):
        measured.append(generation)
        return ConversationCompactProjection(3_100, object())

    with pytest.raises(ConversationCompactError) as error:
        _compact(case, request_projector=project, fixed_request_projector=measure,
                 progress_callback=case.progress.append)
    assert error.value.code == "COMPACT_CANDIDATE_TOO_LARGE"
    _assert_uncommitted(case)
    assert len(case.summaries) == 1 and not case.checkpoint_path.exists()
    # 失败要留下容量计量：完整请求多大、输入上限（触发线 90%×10000）、摘要与固定开销各占多少、保留几条、试了几个候选。
    # 候选投影器只见真实摘要，不再被拿去投影空摘要；固定开销只走只计量入口。
    (summary, retained), = projected
    assert summary and measured == [1]
    expected = CompactCapacityFacts(
        candidate_tokens=tokens, input_ceiling_tokens=9_000, summary_tokens=estimate_tokens(summary),
        # 没有工具来源，保留 IR 保持 0。
        fixed_tokens=3_100, retained_items=len(retained), retained_ir_items=0, retained_ir_tokens=0,
        candidates_tried=1,
    )
    assert error.value.capacity == expected
    failed, = [event for event in case.progress if event.get("phase") == "failed"]
    assert failed["source_kind"] == "active_turn_tool_archive"
    assert {key: failed[key] for key in COMPACT_CAPACITY_PROGRESS_FIELDS} == asdict(expected)


# 只计量入口缺失或投影异常时固定开销如实缺失（None）：failed 进度不带该字段、不补 0，原错误码与其它计量不变。
@pytest.mark.parametrize("measure", ["missing", "raises"])
def test_rejected_candidate_without_fixed_measurement_leaves_the_field_missing(carried_case, measure):
    case = carried_case

    def broken(_generation):
        raise RuntimeError("只计量入口不可用")

    fields = {} if measure == "missing" else {"fixed_request_projector": broken}
    with pytest.raises(ConversationCompactError) as error:
        _compact(case, request_projector=lambda *_: ConversationCompactProjection(12_000, object()),
                 progress_callback=case.progress.append, **fields)
    assert error.value.code == "COMPACT_CANDIDATE_TOO_LARGE"
    assert error.value.capacity.fixed_tokens is None and error.value.capacity.candidate_tokens == 12_000
    failed, = [event for event in case.progress if event.get("phase") == "failed"]
    assert "fixed_tokens" not in failed and failed["candidate_tokens"] == 12_000
    _assert_uncommitted(case)


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


@pytest.mark.parametrize("stop_at", ["summary", "projection"])
def test_typed_cancellation_without_token_state_does_not_open_circuit(carried_case, monkeypatch, stop_at):
    case = carried_case

    def cancel_summary(_request):
        raise ToolCancelled("摘要直接取消")

    def cancel_projection(*_args):
        raise ToolCancelled("候选投影直接取消")

    if stop_at == "summary":
        monkeypatch.setattr(compact_semantic_summary, "summarize_live_tool_history", cancel_summary)
    with pytest.raises(ToolCancelled):
        _compact(
            case,
            request_projector=cancel_projection if stop_at == "projection" else None,
            interrupt_check=lambda: False,
            progress_callback=lambda event: case.progress.append(event),
        )
    current = _assert_uncommitted(case)
    assert current.compact_consecutive_failures == 0
    assert not case.checkpoint_path.exists()
    assert case.progress[-1]["stage"] == "candidate_discarded"


def test_typed_cancellation_at_after_checkpoint_without_token_state_prevents_cas(carried_case):
    case = carried_case

    def progress(event):
        case.progress.append(event)
        if event["percent"] == 92:
            raise ToolCancelled("checkpoint之后直接取消")

    with pytest.raises(ToolCancelled):
        _compact(
            case,
            request_projector=lambda *_: ConversationCompactProjection(100, object()),
            interrupt_check=lambda: False,
            progress_callback=progress,
        )
    current = _assert_uncommitted(case)
    assert current.compact_consecutive_failures == 0
    assert case.checkpoint_path.exists()
    assert len(case.checkpoint_path.read_text(encoding="utf-8").splitlines()) == 1
    assert any(event["percent"] == 92 for event in case.progress)
    assert case.progress[-1]["stage"] == "candidate_discarded"


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
    assert surface.tools is tools
    assert request.tools == ()
    assert request.tool_choice is not None
    assert (request.tool_choice.mode, request.tool_choice.reason) == ("none", "active_turn_summary_no_prefix")
    assert request.system_instruction == surface.system_instruction
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


def test_active_turn_summary_without_prefix_contract_sends_none_without_retry(carried_case, monkeypatch):
    case = carried_case
    case.agent.backend = OpenAICompatibleBackend(BackendOptions(
        api_base="https://api.deepseek.com/v1", api_key="offline", model_name="deepseek-v4-flash",
        max_tokens=1_000, context_window_tokens=10_000, reasoning_control="effort", stream_enabled=False,
    ))
    monkeypatch.setattr(compact_semantic_summary, "summarize_live_tool_history", _real_summarize_live_tool_history)
    payloads = []
    tool = {"type": "function", "function": {
        "name": "read_file", "description": "读取文件", "parameters": {"type": "object", "properties": {}}
    }}

    def post_json(request):
        payload = json.loads(json.dumps(request.payload))
        payloads.append(payload)
        if payload.get("tools") and payload.get("tool_choice") != "none":
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call-active-compact-retry", "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }]}
            finish_reason = "tool_calls"
        else:
            message = {"role": "assistant", "content": _SUMMARY}
            finish_reason = "stop"
        return {
            "id": "chatcmpl-active-compact", "object": "chat.completion", "created": 1,
            "model": "deepseek-v4-flash",
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        }

    monkeypatch.setattr(http, "post_json", post_json)
    surface = ConversationCompactProviderSurface("主请求稳定前缀", (tool,), "主请求 system 指令")
    result = _compact(
        case, provider_surface=surface,
        request_projector=lambda *_: ConversationCompactProjection(100, object()),
    )

    assert result.compacted
    assert len(payloads) == 1, f"不应触发工具调用后的额外无工具重试：{len(payloads)} 次"
    payload = payloads[0]
    choice = payload.get("tool_choice")
    mode = choice if isinstance(choice, str) else str(
        (choice or {}).get("type") or (choice or {}).get("mode") or ""
    )
    assert mode == "none", f"active-turn 摘要必须明确禁止工具选择：{choice!r}"
    assert not payload.get("tools"), "active-turn 摘要不承诺主请求前缀，不应发送工具定义"


# LLM: 缓存分叉用例共用的主请求材料：原生 IR 按真实顺序排（任务 → 每个调用 → 结果 → 收尾文字），调用身份与 _record 一致。
# 函数用途: 造一份与 carried_case 归档同身份的主请求缓存前缀；ir_call_ids 控制哪些调用出现在主请求 IR 里。
def _fork(case, ir_call_ids=("call-0", "call-1", "call-2"), *, tail=("call-tail",)):
    ir = [UserTurn("# User Task\n继续核对原始材料")]
    for call_id in (*ir_call_ids, *tail):
        index = call_id.rsplit("-", 1)[-1]
        call = canonical_history_call(
            "read_file", {"path": f"evidence-{index}.txt"}, call_id=call_id,
            run_id="origin-run", attempt_id="origin-attempt", turn_id=f"origin-turn-{index}",
        )
        ir += [AssistantTurn(tool_calls=[call]), canonical_history_result(call, f"ORIGINAL_EVIDENCE_{index}")]
    tool = {"name": "read_file", "description": "读取文件", "input_schema": {"type": "object", "properties": {}}}
    return ActiveTurnCacheFork(
        provider_prompt="主请求提示", provider_history_messages=({"role": "user", "content": "之前的历史"},),
        tool_ir_history=tuple(ir), tools=(tool,), system_instruction="主请求 system 指令",
    )


# LLM: 10-05 生产：回合中归档摘要 48 小时命中 0%。有分叉且来源全在主请求 IR 时，摘要必须带主请求的工具、system、之前历史
#   与提示，IR 截到最后一个被替代调用的结果（之后的保留调用不进前缀），tool_choice 交有界发送按 auto 走。
# 函数用途: 钉住回合中归档摘要优先复用主请求缓存前缀。
def test_active_turn_summary_reuses_parent_prefix_when_fork_covers_sources(carried_case):
    case = carried_case
    fork = _fork(case)
    result = _compact(case, cache_fork=fork, request_projector=lambda *_: ConversationCompactProjection(100, object()))

    assert result.compacted
    request = case.summaries[-1]
    assert request.tools == fork.tools and request.tool_choice is None
    assert request.system_instruction == fork.system_instruction and request.provider_prompt == fork.provider_prompt
    assert request.provider_history_messages == fork.provider_history_messages
    sent_call_ids = [item.call_id for item in request.history if hasattr(item, "call_id")]
    assert sent_call_ids == ["call-0", "call-1", "call-2"], "前缀必须截到最后一个被替代调用的结果，保留调用不进前缀"
    assert request.history == fork.tool_ir_history[:len(request.history)], "前缀必须是主请求 IR 的原样开头"
    fallback_text = compact_tool_summary_text(request.fallback_history)
    assert all(f"call-{index}" in fallback_text for index in range(3)), "机械兜底必须覆盖全部被替代来源"
    assert "call-tail" not in fallback_text and "继续核对原始材料" not in fallback_text, (
        "保留调用、任务原文等非来源内容不能进兜底摘要，否则主请求逐代膨胀")


# LLM: 跨片只在归档里的来源不在主请求 IR 中：分叉会漏摘它，必须退回独立摘要（空工具 + none + 来源 IR），不丢来源。
# 函数用途: 钉住来源不全在主请求 IR 时的回退。
def test_active_turn_summary_falls_back_when_a_source_is_archive_only(carried_case):
    case = carried_case
    result = _compact(case, cache_fork=_fork(case, ("call-0", "call-1")),
                      request_projector=lambda *_: ConversationCompactProjection(100, object()))

    assert result.compacted
    request = case.summaries[-1]
    assert request.tools == () and request.tool_choice.reason == "active_turn_summary_no_prefix"
    assert "call-2" in json.dumps([getattr(item, "call_id", "") for item in request.history]) or any(
        "call-2" in json.dumps(item, default=str) for item in request.history), "回退摘要必须带上全部来源"


# LLM: 同一调用编号在主请求 IR 里出现两次时无法证明截断点，按结构化事实回退，不猜哪个才是来源。
# 函数用途: 钉住调用编号重复时的回退。
def test_active_turn_summary_falls_back_when_call_ids_repeat(carried_case):
    case = carried_case
    result = _compact(case, cache_fork=_fork(case, ("call-0", "call-1", "call-2", "call-1")),
                      request_projector=lambda *_: ConversationCompactProjection(100, object()))

    assert result.compacted
    assert case.summaries[-1].tool_choice.reason == "active_turn_summary_no_prefix"


# LLM: 恢复入口在卸掉主请求历史前冻结分叉材料：之前的历史、当前回合 IR、提示原样取自冻结输入，工具与 system 取自
#   同一份完整投影（与真实发送一致）；投影不完整时不给分叉（摘要退回独立请求）。只替身投影函数，不发请求。
# 函数用途: 钉住分叉材料与主请求同源、投影未知时不分叉。
def test_recovery_freezes_parent_prefix_for_active_turn_fork(monkeypatch):
    from agent_py_agent.agent.agent_core import compact_request_recovery as recovery

    projection = SimpleNamespace(status="ready", tools=[{"name": "read_file"}], system_instruction="主请求 system 指令")
    monkeypatch.setattr(recovery, "project_tool_loop_request", lambda _frozen: projection)
    frozen = SimpleNamespace(provider_history_messages=({"role": "user", "content": "之前的历史"},),
                             tool_ir_history=("当前回合 IR",))
    fork = recovery._active_turn_cache_fork("主请求提示", frozen)
    assert fork == ActiveTurnCacheFork(
        provider_prompt="主请求提示", provider_history_messages=frozen.provider_history_messages,
        tool_ir_history=("当前回合 IR",), tools=({"name": "read_file"},), system_instruction="主请求 system 指令",
    )
    projection.status = "unknown"
    assert recovery._active_turn_cache_fork("主请求提示", frozen) is None


# LLM: 实际归档选择、摘要执行、预算与运输都运行，仅模型响应替身；完整来源可复用时不许悄悄换工具或旧尾项。
# 函数用途: 抓跨片归档压缩的完整Responses负载及预算来源，守住同源前缀和最后指令。
@pytest.mark.parametrize("remote", [True, False])
def test_compactcall_active_archive_wire_and_budget_are_same_source(carried_case, monkeypatch, remote):
    from dataclasses import replace

    from agent_py_agent.agent.backends.base import ProviderRequestOptions
    from agent_py_agent.agent.backends.message_adapter import project_native_history_messages
    from agent_py_agent.agent.conversation import compact_message_source, compact_request_budget
    from agent_py_agent.agent.prompting_parts.cache_layout import CacheStructuredPrompt
    from agent_py_agent.tests.test_compact_remote_provider import BLOCK, _subscription_backend

    case = carried_case
    backend, _, _ = _subscription_backend()
    case.agent.backend = backend
    case.agent.config.model_context_window_tokens = 128_000
    case.agent.config.memory_compact_remote_enabled = remote
    fork = replace(_fork(case, tail=()), provider_prompt=CacheStructuredPrompt("稳定规则", " \n旧动态事实\n "))
    second_tool = {"name": "list_files", "description": "列目录", "input_schema": {"type": "object"}}
    fork = replace(fork, tools=(*fork.tools, second_tool))
    payloads, measured = [], []

    def send(_path, payload, _headers, **_options):
        payloads.append(deepcopy(payload))
        output = [BLOCK["item"]] if payload["input"][-1].get("type") == "compaction_trigger" else [
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": _SUMMARY}]}]
        return {"status": "completed", "output": output}

    estimate = compact_message_source.estimate_compact_payload

    def measure(payload):
        measured.append({**payload, "messages": list(deepcopy(list(payload["messages"])))})
        return estimate(payload)

    monkeypatch.setattr(compact_message_source, "estimate_compact_payload", measure)
    monkeypatch.setattr(compact_request_budget, "estimate_compact_payload", measure)
    monkeypatch.setattr(compact_semantic_summary, "summarize_live_tool_history", _real_summarize_live_tool_history)
    backend.request_json = send
    backend.generate(fork.provider_prompt, tools=list(fork.tools),
                     messages=project_native_history_messages(fork.tool_ir_history, fork.provider_history_messages),
                     request_options=ProviderRequestOptions(system_instruction=fork.system_instruction))
    result = _compact(case, cache_fork=fork, request_projector=lambda *_, **_kw: ConversationCompactProjection(100, object()))
    assert result.compacted and len(payloads) == 2 and measured
    main, compact = payloads
    _assert_compactcall_archive_prefix(main, compact, remote)
    # 把预算读取过的同一材料交真实serializer，不能仅比较估算数字或对象身份。
    material = measured[-1]
    assert material["tools"] == list(fork.tools) and material["system_instruction"] == fork.system_instruction
    _assert_compactcall_budget_wire(backend, payloads, material, compact)


# LLM: 工具排序及旧历史逐字相同才算共享前缀；新控制或摘要要求不能插在任何旧项之前。
# 函数用途: 比较归档压缩与主请求的完整固定负载及尾部要求。
def _assert_compactcall_archive_prefix(main, compact, remote):
    for field in ("instructions", "tools", "tool_choice", "reasoning"):
        assert json.dumps(compact.get(field), ensure_ascii=False) == json.dumps(main.get(field), ensure_ascii=False)
    assert [tool["name"] for tool in compact["tools"]] == ["read_file", "list_files"]
    assert compact["input"][:len(main["input"])] == main["input"]
    tail = compact["input"][-1]
    assert tail == {"type": "compaction_trigger"} if remote else "完整替代摘要" in str(tail)


# LLM: 去掉本次追加的控制项后，真实serializer输入必须与预算所见材料完全相同，不能只用估算值证明同源。
# 函数用途: 重放预算材料并与实际摘要请求比较完整历史。
def _assert_compactcall_budget_wire(backend, payloads, material, compact):
    from agent_py_agent.agent.backends.base import ProviderRequestOptions

    before = len(payloads)
    backend.generate(material["prompt"], messages=material["messages"], tools=material["tools"],
                     request_options=ProviderRequestOptions(system_instruction=material["system_instruction"]))
    assert len(payloads) == before + 1
    projected = payloads[-1]["input"]
    expected = [item for item in compact["input"] if item.get("type") not in {"configuration_update", "compaction_trigger"}]
    assert projected == expected
