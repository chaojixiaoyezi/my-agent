"""宿主摘要器接缝：真实临时消息、计量与 checkpoint/CAS，不联网、不读取日常 home。"""
from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PIL import Image

from agent_py_agent.agent.backends.base import BackendOptions, ModelResponse
from agent_py_agent.agent.backends.http import HttpBackend
from agent_py_agent.agent.common.cancellation import ToolCancelled
from agent_py_agent.agent.conversation import ConversationStore, auxiliary_model_call, compact
from agent_py_agent.agent.conversation import compact_request_budget as budget
from agent_py_agent.agent.conversation.compact_calibration import CompactRequestCalibration
from agent_py_agent.agent.conversation.compact_checkpoint import committed_compact_checkpoint_chain
from agent_py_agent.agent.conversation.compact_landmarks import (
    LandmarkOptions,
    landmark_source,
    render_landmarked_summary,
)
from agent_py_agent.agent.conversation.compact_media_policy import (
    MEDIA_POLICY_ARCHIVED_REFS,
    CompactMediaDecision,
)
from agent_py_agent.agent.conversation.compact_projection import ConversationCompactProjection
from agent_py_agent.agent.conversation.compact_provider_surface import (
    ConversationCompactProviderSurface,
)
from agent_py_agent.agent.conversation.compact_remote import provider_compaction_record
from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE, CompactScope
from agent_py_agent.agent.conversation.compact_summary_view import resolve_compact_summary_view
from agent_py_agent.agent.conversation.message_replay import MessageSnapshotRows
from agent_py_agent.agent.conversation.native_history import (
    CANONICAL_NATIVE_MESSAGES_METADATA_KEY,
    canonical_native_messages_envelope,
)
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.agent.tooling.compact_policy_tool import CompactPolicyTool

_OMITTED = object()


def _case(tmp_path, *, repeats=1):
    store = ConversationStore(tmp_path / "conversation")
    thread = store.threads.get_or_create({"canonical_user_id": "owner", "now": 1})
    rows = tuple(store.messages.append({
        "thread_id": thread.thread_id,
        "role": "user" if index % 2 == 0 else "assistant",
        "content": f"payload-{index}" * repeats,
        "metadata": {"conversation_request_id": f"completed-{index // 2}"},
    }) for index in range(4))
    agent = SimpleNamespace(
        conversation_store=store,
        home_paths=SimpleNamespace(owner_compact_dir=tmp_path / "compact"),
        backend=SimpleNamespace(name="fake", model_name="fake", max_tokens=128),
        prompts=SimpleNamespace(build=lambda prompt, *_args, **_kwargs: prompt),
        config=SimpleNamespace(model_context_window_tokens=20_000),
    )
    source = compact.load_conversation_compact_source(agent, store, thread, scope=THREAD_COMPACT_SCOPE)
    return agent, thread, source, rows


def _run(agent, thread, source, summarizer=_OMITTED, **options):
    selected = {"force": True, "exclude_request_id": "active-request", "source": source, **options}
    wiring = {} if summarizer is _OMITTED else {"summarizer": summarizer}
    if wiring:
        assert "summarizer" in inspect.signature(compact.prepare_conversation_context).parameters
    return compact.prepare_conversation_context(
        agent, agent.conversation_store, thread,
        options=compact.ConversationCompactOptions(**selected), **wiring,
    )


def _assert_uncommitted(agent, thread, *, failures=0):
    current = agent.conversation_store.threads.require(thread.thread_id)
    assert current.compact_generation == thread.compact_generation
    assert current.compact_checkpoint_id == thread.compact_checkpoint_id
    assert current.summary == thread.summary
    assert current.compacted_through_byte_offset == thread.compacted_through_byte_offset
    assert current.compacted_through_message_id == thread.compacted_through_message_id
    assert current.compact_consecutive_failures == failures
    assert committed_compact_checkpoint_chain(agent, current) == ()


class _Summarizer:
    def __init__(self):
        self.calls = []

    def __call__(self, agent, previous_summary, operation_evidence, rows, *, call=None):
        self.calls.append((agent, previous_summary, operation_evidence, tuple(rows), call))
        return "宿主提供的摘要"


class _FalseySummarizer(_Summarizer):
    def __bool__(self):
        return False


def test_protocol_has_exact_summarize_call_signature():
    assert "ConversationSummarizer" in vars(compact)
    protocol = inspect.signature(compact.ConversationSummarizer.__call__)
    implementation = inspect.signature(compact._summarize)
    assert list(protocol.parameters.values())[1:] == list(implementation.parameters.values())
    assert protocol.return_annotation == implementation.return_annotation
    assert "ConversationSummarizer" in compact.__all__
    with pytest.raises(TypeError):
        compact.ConversationSummarizer()


@pytest.mark.parametrize("summarizer", [_OMITTED, None], ids=["omitted", "explicit-none"])
def test_default_still_uses_original_summary_and_checkpoint(tmp_path, monkeypatch, summarizer):
    agent, thread, source, rows = _case(tmp_path)
    requests = []

    def generate(request):
        requests.append(request)
        return ModelResponse(text="默认模型摘要", backend="fake")

    monkeypatch.setattr(budget, "generate_auxiliary_model_response", generate)
    result = _run(agent, thread, source, summarizer)
    assert result.compacted and result.thread.compact_generation == 1
    assert result.thread.summary.startswith("默认模型摘要")
    assert len(requests) == 1 and requests[0].purpose == "conversation_compact_summary"
    chain = committed_compact_checkpoint_chain(agent, result.thread)
    assert len(chain) == 1 and chain[0]["source_message_ids"] == [row.message_id for row in rows]
    assert chain[0]["summary"] == result.thread.summary


@pytest.mark.parametrize("kind", ["function", "object", "falsey"])
def test_injected_callable_reaches_real_commit_without_default_provider(tmp_path, monkeypatch, kind):
    agent, thread, source, rows = _case(tmp_path)
    implementation = _FalseySummarizer() if kind == "falsey" else _Summarizer()
    summarizer = implementation.__call__ if kind == "function" else implementation
    events = []

    def forbidden(*_args, **_kwargs):
        pytest.fail("显式注入不能调用默认摘要器或发模型请求")

    monkeypatch.setattr(compact, "_summarize", forbidden)
    monkeypatch.setattr(budget, "generate_auxiliary_model_response", forbidden)
    result = _run(agent, thread, source, summarizer, progress_callback=events.append,
                  custom_instructions="只整理已知事实")
    assert len(implementation.calls) == 1
    actual_agent, previous, evidence, selected_rows, call = implementation.calls[0]
    assert actual_agent is agent and previous == "" and selected_rows == rows
    assert call.thread_id == thread.thread_id and call.request_id == "active-request"
    assert call.custom_instructions == "只整理已知事实" and call.compact_generation == 0
    assert call.provider_outcome == call.media_outcome == call.landmark_outcome == []
    assert result.thread.summary == "宿主提供的摘要" and result.compacted
    chain = committed_compact_checkpoint_chain(agent, result.thread)
    assert len(chain) == 1 and chain[0]["operation_evidence"] == evidence
    assert chain[0]["source_message_ids"] == [row.message_id for row in rows]
    assert result.thread.compact_source_messages == len(rows)
    assert result.thread.compacted_through_message_id == rows[-1].message_id
    assert [event["stage"] for event in events][-3:] == ["checkpointing", "committing", "completed"]


def test_candidate_entrypoint_accepts_custom_summarizer(tmp_path):
    assert "summarizer" in inspect.signature(compact._build_compact_candidate).parameters
    agent, thread, source, rows = _case(tmp_path)
    request = compact._prepare_compact_request(agent, agent.conversation_store, thread,
        compact.ConversationCompactOptions(source=source, force=True))
    summarizer = _Summarizer()
    candidate = compact._build_compact_candidate(
        request, source.messages[:2], source.messages[2:], provider_surface=None,
        summarizer=summarizer,
    )
    assert candidate.summary == "宿主提供的摘要"
    assert candidate.compact_rows == rows[:2] and candidate.retained_tail == rows[2:]
    assert summarizer.calls[0][3] == rows[:2]
    _assert_uncommitted(agent, thread)


@pytest.mark.parametrize("override", [False, True], ids=["request-injection", "candidate-override"])
def test_candidate_inherits_request_summarizer_unless_explicitly_overridden(tmp_path, override):
    agent, thread, source, rows = _case(tmp_path)
    inherited, explicit = _Summarizer(), _Summarizer()
    request = compact._prepare_compact_request(
        agent, agent.conversation_store, thread,
        compact.ConversationCompactOptions(source=source, force=True), summarizer=inherited,
    )
    candidate = compact._build_compact_candidate(
        request, source.messages[:2], source.messages[2:], provider_surface=None,
        summarizer=explicit if override else None,
    )
    assert candidate.summary == "宿主提供的摘要" and candidate.retained_tail == rows[2:]
    assert len(inherited.calls) == (0 if override else 1)
    assert len(explicit.calls) == (1 if override else 0)
    _assert_uncommitted(agent, thread)


@pytest.mark.parametrize("mismatch", [
    "source-thread", "source-generation", "source-checkpoint", "scope-thread", "scope-type",
    "foreign-row", "duplicate-row", "stale-view",
])
def test_injection_cannot_bypass_source_scope_validation(tmp_path, mismatch):
    agent, thread, source, rows = _case(tmp_path)
    if mismatch.startswith("source-"):
        changes = {
            "source-thread": {"thread_id": "other-thread"},
            "source-generation": {"compact_generation": 1},
            "source-checkpoint": {"compact_checkpoint_id": "other-checkpoint"},
        }[mismatch]
        source = replace(source, thread=replace(thread, **changes))
    elif mismatch == "scope-thread":
        source = replace(source, compact_context=replace(source.compact_context, thread_id="other-thread"))
    elif mismatch == "scope-type":
        # 非类型化宿主输入也必须由来源门拒绝，不能先靠正常容器构造器报错充当本入口的拒绝。
        source = replace(source, compact_context=SimpleNamespace(
            thread_id=thread.thread_id, scope="untrusted", view=source.compact_context.view,
        ))
    elif mismatch == "foreign-row":
        source = replace(source, messages=(replace(rows[0], thread_id="other-thread"),))
    elif mismatch == "duplicate-row":
        source = replace(source, messages=(rows[0], rows[0]))
    else:
        source = replace(source, compact_context=replace(
            source.compact_context, view=replace(source.compact_context.view, summary="伪造的旧摘要"),
        ))
    summarizer = _Summarizer()

    def forbidden_projection(_view):
        pytest.fail("错误范围必须在摘要和请求投影之前拒绝")

    with pytest.raises(compact.ConversationCompactError) as error:
        _run(agent, thread, source, summarizer, request_projector=forbidden_projection)
    assert error.value.code == "COMPACT_SOURCE_CHANGED"
    assert summarizer.calls == []
    _assert_uncommitted(agent, thread)


def test_local_scope_stays_local_and_previously_covered_rows_are_rejected(tmp_path):
    agent, thread, _source, rows = _case(tmp_path)
    scope = CompactScope(kind="turn", turn_id="local-turn")
    source = compact.load_conversation_compact_source(
        agent, agent.conversation_store, thread, scope=scope,
    )
    summarizer = _Summarizer()
    result = _run(agent, thread, source, summarizer)
    assert result.compacted and result.thread.compact_generation == 1
    assert result.thread.summary == thread.summary == ""
    assert result.thread.compacted_through_message_id == thread.compacted_through_message_id
    assert result.thread.compacted_through_byte_offset == thread.compacted_through_byte_offset
    assert resolve_compact_summary_view(agent, result.thread, scope).summary == "宿主提供的摘要"
    assert resolve_compact_summary_view(agent, result.thread, THREAD_COMPACT_SCOPE).summary == ""
    checkpoint, = committed_compact_checkpoint_chain(agent, result.thread)
    assert checkpoint["scope"] == scope.to_dict()
    assert checkpoint["source_message_ids"] == [row.message_id for row in rows]

    following = compact.load_conversation_compact_source(
        agent, agent.conversation_store, result.thread, scope=scope,
    )
    assert not following.messages
    with pytest.raises(compact.ConversationCompactError) as error:
        _run(agent, result.thread, replace(following, messages=(rows[0],)), summarizer)
    assert error.value.code == "COMPACT_SOURCE_CHANGED" and len(summarizer.calls) == 1
    assert agent.conversation_store.threads.require(thread.thread_id) == result.thread
    assert len(committed_compact_checkpoint_chain(agent, result.thread)) == 1


@pytest.mark.parametrize("empty", [False, True], ids=["under-threshold", "empty-source"])
def test_preflight_noop_does_not_call_injected_summarizer(tmp_path, empty):
    agent, thread, source, rows = _case(tmp_path)
    if empty:
        source = replace(source, messages=())
    summarizer = _Summarizer()
    result = _run(agent, thread, source, summarizer, force=empty)
    assert not result.compacted and result.messages == (() if empty else rows)
    assert summarizer.calls == []
    _assert_uncommitted(agent, thread)


@pytest.mark.parametrize("failure", [
    "transport", "cancel", "typed-cancel", "pre-cas-cancel", "rewrite", "pre-cas-rewrite", "cas",
])
def test_injected_summary_failures_keep_original_checkpoint_and_cas_rules(tmp_path, monkeypatch, failure):
    agent, thread, source, _rows = _case(tmp_path)
    store, stopped, calls, events = agent.conversation_store, [False], [], []
    path = store.storage.message_path(thread.thread_id)
    before = path.read_bytes()
    rewritten = before.replace(b"payload-0", b"changed-0", 1)
    assert rewritten != before and len(rewritten) == len(before)

    def summarize(_agent, _previous, _evidence, _rows, *, call=None):
        calls.append(call)
        if failure == "transport":
            raise RuntimeError("controlled provider failure")
        if failure == "typed-cancel":
            raise ToolCancelled("controlled cancellation")
        if failure == "cancel":
            stopped[0] = True
        if failure == "rewrite":
            path.write_bytes(rewritten)
        return "未发布候选"

    def progress(event):
        events.append(event)
        if event["stage"] == "committing":
            if failure == "pre-cas-rewrite":
                path.write_bytes(rewritten)
            if failure == "pre-cas-cancel":
                stopped[0] = True

    if failure == "cas":
        original = store.threads.update_compact_state

        def conflicting_commit(thread_id, **kwargs):
            kwargs["expected_generation"] += 1
            return original(thread_id, **kwargs)

        monkeypatch.setattr(store.threads, "update_compact_state", conflicting_commit)
    expected = (DataCorruptionError if "rewrite" in failure else ToolCancelled if failure == "typed-cancel"
                else InterruptedError if "cancel" in failure else RuntimeError)
    with pytest.raises(expected):
        _run(agent, thread, source, summarize, interrupt_check=lambda: stopped[0], progress_callback=progress)
    assert len(calls) == 1
    assert events[-1]["phase"] == ("superseded" if "cancel" in failure else "failed")
    assert all(event["phase"] != "completed" for event in events)
    _assert_uncommitted(agent, thread, failures=0 if "cancel" in failure else 1)
    assert path.read_bytes() == (rewritten if "rewrite" in failure else before)


@pytest.mark.parametrize("row_index", [0, 2], ids=["compacted-prefix", "retained-tail"])
def test_both_snapshot_partitions_are_reverified_immediately_before_cas(tmp_path, row_index):
    agent, thread, source, rows = _case(tmp_path)
    source = replace(source, policy=replace(source.policy, recent_tail_max_turns=1, recent_tail_tokens=1_000))
    path = agent.conversation_store.storage.message_path(thread.thread_id)
    before = path.read_bytes()
    calls = []

    def summarize(_agent, _previous, _evidence, selected, *, call=None):
        assert isinstance(selected, MessageSnapshotRows) and selected == rows[:2]
        calls.append(call)
        return "冻结前缀摘要"

    def project(view):
        if view.is_candidate and not view.measure_only:
            assert isinstance(view.messages, MessageSnapshotRows) and view.messages == rows[2:]
        tokens = 0 if view.measure_only else 200 if view.is_candidate else source.policy.trigger_tokens + 1
        return ConversationCompactProjection(tokens, object())

    def progress(event):
        if event["stage"] == "committing":
            original = f"payload-{row_index}".encode()
            altered = f"changed-{row_index}".encode()
            path.write_bytes(before.replace(original, altered, 1))

    with pytest.raises(DataCorruptionError, match="canonical message source changed"):
        _run(agent, thread, source, summarize, force=False, request_projector=project, progress_callback=progress)
    assert len(calls) == 1 and path.read_bytes() != before
    _assert_uncommitted(agent, thread, failures=1)


@pytest.mark.parametrize("candidate_tokens", [6_000, 6_001], ids=["at-ceiling", "over-ceiling"])
def test_injected_summary_still_requires_full_projection_under_output_reserve(tmp_path, candidate_tokens):
    agent, thread, source, _rows = _case(tmp_path)
    # 输出预留只接受真实HTTP后端的结构事实，轻量SimpleNamespace不会宣称共享窗口。
    agent.backend = HttpBackend(BackendOptions(
        api_base="https://example.invalid", api_key="", model_name="unit-model",
        context_window_tokens=10_000, max_tokens=4_000,
    ))
    source = replace(source, policy=replace(
        source.policy, context_window_tokens=10_000, trigger_tokens=9_000, recovery_target_tokens=6_000,
    ))
    summarizer, views = _Summarizer(), []

    def project(view):
        views.append(view)
        return ConversationCompactProjection(candidate_tokens if view.is_candidate else 12_000, object())

    with pytest.raises(compact.ConversationCompactError) as error:
        _run(agent, thread, source, summarizer, request_projector=project)
    assert error.value.code == "COMPACT_CANDIDATE_TOO_LARGE"
    assert error.value.capacity.candidate_tokens == candidate_tokens
    assert error.value.capacity.input_ceiling_tokens == 6_000
    assert len(summarizer.calls) == 1
    assert any(view.is_candidate and view.summary == "宿主提供的摘要" for view in views)
    _assert_uncommitted(agent, thread, failures=1)


def test_injected_summary_keeps_calibration_and_raw_projection_separate(tmp_path):
    agent, thread, source, _rows = _case(tmp_path)
    agent.backend = HttpBackend(BackendOptions(
        api_base="https://example.invalid", api_key="", model_name="unit-model",
        context_window_tokens=10_000, max_tokens=4_000,
    ))
    source = replace(source, policy=replace(
        source.policy, context_window_tokens=10_000, trigger_tokens=9_000, recovery_target_tokens=6_000,
    ))
    calibration = CompactRequestCalibration(
        raw_estimated_tokens=10_000, provider_input_tokens=7_000,
        context_surface_fingerprint="a" * 64, compact_generation=0, calibration_scope="current_run",
    )
    summarizer, events = _Summarizer(), []

    def project(view):
        return ConversationCompactProjection(8_000 if view.is_candidate else 12_000, {"summary": view.summary})

    result = _run(agent, thread, source, summarizer, request_projector=project, calibration=calibration,
                  progress_callback=events.append)
    assert summarizer.calls[0][-1].calibration is calibration
    assert result.projected_tokens == 5_600 and result.request_projection.projected_tokens == 8_000
    assert result.request_projection.material == {"summary": result.thread.summary}
    checkpoint, = committed_compact_checkpoint_chain(agent, result.thread)
    assert checkpoint["projected_tokens_before"] == 9_000 and checkpoint["projected_tokens_after"] == 5_600
    assert events[0]["before_tokens"] == 9_000 and events[-1]["after_tokens"] == 5_600


def test_provider_media_and_landmark_outcomes_reach_original_measurement_and_checkpoint(tmp_path):
    agent, thread, source, rows = _case(tmp_path, repeats=180)
    image_path = tmp_path / "chart.png"
    Image.new("RGB", (1, 1), "red").save(image_path)
    image_bytes = image_path.read_bytes()
    image_sha = hashlib.sha256(image_bytes).hexdigest()
    native = [
        {"role": "user", "content": [
            {"type": "text", "text": "看附件"},
            {"type": "image", "source": {
                "type": "local_file", "path": str(image_path), "sha256": image_sha,
                "media_type": "image/png", "size_bytes": len(image_bytes), "name": image_path.name,
            }},
        ]},
        {"role": "assistant", "content": [{"type": "text", "text": "图里是红色"}]},
    ]
    for role, text in (("user", "看附件"), ("assistant", "图里是红色")):
        metadata = {"conversation_request_id": "completed-media"}
        if role == "assistant":
            metadata[CANONICAL_NATIVE_MESSAGES_METADATA_KEY] = canonical_native_messages_envelope(native)
        row = agent.conversation_store.messages.append({
            "thread_id": thread.thread_id, "role": role, "content": text, "metadata": metadata,
        })
        rows = (*rows, row)
    agent.config.compact_media_policy = MEDIA_POLICY_ARCHIVED_REFS
    source = compact.load_conversation_compact_source(agent, agent.conversation_store, thread,
                                                    scope=THREAD_COMPACT_SCOPE)
    source = replace(source, policy=replace(source.policy, recovery_target_tokens=100))
    agent.backend.provider_compaction_scope = lambda: {"protocol": "responses", "endpoint": "unit-test"}
    old_record = provider_compaction_record(agent.backend, {"encrypted_content": "old-synthetic-content"})
    record = provider_compaction_record(agent.backend, {"encrypted_content": "synthetic-content"})
    surface = ConversationCompactProviderSurface("冻结前缀", (), "冻结系统")
    media = CompactMediaDecision(MEDIA_POLICY_ARCHIVED_REFS, fact_source="fixture", reason="host-selected")
    semantic, trailing = "必须保留的语义摘要", "\n必须保留的机械尾文"
    options = LandmarkOptions(landmark_budget_tokens=1_200)
    landmarks = landmark_source("", rows)
    rendered = render_landmarked_summary(semantic, landmarks, options)
    rebuild = compact._LandmarkRebuild(semantic, landmarks, trailing, options, rendered.used_tokens)
    assert rebuild.used_tokens > rebuild.minimum_tokens()
    calls, views = [], []

    def summarize(_agent, previous, _evidence, selected, *, call=None):
        assert previous == "" and selected == rows and call.previous_provider_compaction == ""
        assert call.provider_surface is surface
        assert call.media_archived and call.media_facts.blocks == 1 and call.media_facts.refs == (image_sha,)
        call.provider_outcome.extend((old_record, record))
        call.media_outcome.append(media)
        call.landmark_outcome.append(rebuild)
        call.source_progress(len(selected), len(selected))
        calls.append(call)
        return rendered.text + trailing

    def project(view):
        if view.is_candidate:
            assert view.provider_compaction == record
            views.append(view)
        return ConversationCompactProjection(800 + compact.estimate_tokens(view.summary), {"summary": view.summary})

    result = _run(agent, thread, source, summarize, provider_surface=surface, request_projector=project)
    assert len(calls) == 1 and len(views) == 2
    assert views[0].summary == rendered.text + trailing
    assert views[1].summary == result.thread.summary == rebuild.render(0)
    assert result.thread.summary.startswith(semantic) and result.thread.summary.endswith(trailing)
    assert result.request_projection.material == {"summary": result.thread.summary}
    assert result.projected_tokens < 800 + compact.estimate_tokens(views[0].summary)
    checkpoint, = committed_compact_checkpoint_chain(agent, result.thread)
    assert checkpoint["provider_compaction"] == record
    assert checkpoint["media_policy"] == media.policy and checkpoint["media_policy_reason"] == media.reason
    assert checkpoint["media_fact_source"] == media.fact_source and checkpoint["media_refs"] == [image_sha]
    assert checkpoint["media_blocks_archived"] == 1 and checkpoint["media_blocks_summarized"] == 0
    assert checkpoint["projected_tokens_after"] == result.projected_tokens
    assert checkpoint["summary"] == result.thread.summary
    following = compact.load_conversation_compact_source(agent, agent.conversation_store, result.thread,
                                                       scope=THREAD_COMPACT_SCOPE)
    assert following.compact_context.view.provider_compaction == record


@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
def test_standalone_usage_settlement_still_runs_for_every_injected_outcome(tmp_path, monkeypatch, outcome):
    agent, thread, source, _rows = _case(tmp_path)
    settled = []
    monkeypatch.setattr(auxiliary_model_call, "settle_standalone_model_usage", settled.append)

    def summarize(_agent, _previous, _evidence, _rows, *, call=None):
        if outcome == "failure":
            raise RuntimeError("controlled summary error")
        if outcome == "cancel":
            raise ToolCancelled("controlled cancellation")
        return "独立摘要"

    if outcome == "success":
        assert _run(agent, thread, source, summarize, exclude_request_id="").compacted
    else:
        expected = ToolCancelled if outcome == "cancel" else RuntimeError
        with pytest.raises(expected):
            _run(agent, thread, source, summarize, exclude_request_id="")
        _assert_uncommitted(agent, thread, failures=1 if outcome == "failure" else 0)
    assert len(settled) == 1
    request, = settled
    assert request.summarizer is summarize and request.standalone_usage
    assert request.thread.thread_id == thread.thread_id
    assert request.request_id.startswith("conversation-compact:transcript:")


def test_checkpoint_failure_cannot_publish_an_injected_summary(tmp_path, monkeypatch):
    agent, thread, source, _rows = _case(tmp_path)
    summarizer = _Summarizer()

    def failed_checkpoint(*_args, **_kwargs):
        raise OSError("controlled checkpoint failure")

    monkeypatch.setattr(compact, "write_compact_checkpoint", failed_checkpoint)
    with pytest.raises(OSError, match="controlled checkpoint failure"):
        _run(agent, thread, source, summarizer)
    assert len(summarizer.calls) == 1
    _assert_uncommitted(agent, thread, failures=1)


def test_compact_tool_json_schema_does_not_expose_host_summarizer():
    schema = json.loads(json.dumps(CompactPolicyTool().model_spec.input_schema))
    assert "summarizer" not in schema["properties"]
    assert "ConversationSummarizer" not in json.dumps(schema)
    assert schema["additionalProperties"] is False
    assert "summarizer" not in compact.ConversationCompactOptions.__dataclass_fields__


@pytest.mark.parametrize("scope", ["subagent", "isolated", "task_local", "control_plane"])
def test_existing_tool_permission_boundary_still_rejects_restricted_scopes(tmp_path, scope):
    agent, thread, _source, _rows = _case(tmp_path)
    agent._current_subagent_run_id = "test-child" if scope == "subagent" else ""
    agent._current_task_attributes = {"context_scope": scope}
    tool = CompactPolicyTool(agent)
    outcome = tool.execute({"action": "set_thread", "trigger_percent": 80})
    assert outcome.error_code == "TOOL_PERMISSION_DENIED"
    assert outcome.effect_outcome == "not_started"
    _assert_uncommitted(agent, thread)


def test_default_summarizer_is_resolved_after_request_preparation(tmp_path, monkeypatch):
    agent, thread, source, rows = _case(tmp_path)
    request = compact._prepare_compact_request(
        agent, agent.conversation_store, thread, compact.ConversationCompactOptions(source=source, force=True),
    )
    replacement = _Summarizer()
    monkeypatch.setattr(compact, "_summarize", replacement)
    candidate = compact._build_compact_candidate(request, source.messages, (), provider_surface=None)
    assert candidate.summary == "宿主提供的摘要" and replacement.calls[0][3] == rows
    _assert_uncommitted(agent, thread)
