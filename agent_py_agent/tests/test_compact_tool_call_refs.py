from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.active_turn_compact import (
    _build_active_turn_compact_plan,
    model_visible_active_turn_tool_calls,
)
from agent_py_agent.agent.conversation.authority import (
    CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR,
)


def _record(run="old-run", attempt="old-attempt", call="call_1", request="old-request"):
    return {
        "run_id": run, "attempt_id": attempt, "call_id": call,
        "request_id": request, "conversation_request_id": request,
        "scoped_call_id": f"{run}:{call}", "tool": "read_file", "ok": True,
        "output_preview": f"{run}/{attempt}/{call}",
    }


def _ref(record):
    return {
        "schema": "tool_call_ref.v1",
        **{key: record[key] for key in ("run_id", "attempt_id", "call_id")},
    }


def _committed(tmp_path, sources, *, retained=(), legacy=False, orphan=()):
    thread = SimpleNamespace(
        thread_id="thread", compact_checkpoint_id="compact-1", compact_generation=1,
    )
    row = {
        "schema": "conversation_compact_checkpoint.v2", "thread_id": "thread",
        "checkpoint_id": "compact-1", "previous_checkpoint_id": "", "generation": 1,
        "source_kind": "live_tool_ir", "request_id": "submitter-request",
        "attempt_id": "submitter-attempt",
        "source_tool_call_ids": [item["call_id"] for item in sources],
        "retained_tool_call_ids": [item["call_id"] for item in retained],
    }
    if not legacy:
        row["source_tool_call_refs"] = [_ref(item) for item in sources]
        row["retained_tool_call_refs"] = [_ref(item) for item in retained]
    rows = [row]
    if orphan:
        rows.append({
            **row, "checkpoint_id": "orphan-2", "generation": 2,
            "previous_checkpoint_id": "compact-1",
            "source_tool_call_ids": [item["call_id"] for item in orphan],
            "source_tool_call_refs": [_ref(item) for item in orphan],
            "retained_tool_call_ids": [], "retained_tool_call_refs": [],
        })
    path = tmp_path / "conversations" / "thread.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_compact_dir=tmp_path),
        conversation_store=SimpleNamespace(threads=SimpleNamespace(
            load_report=lambda _thread_id: (thread, None),
        )),
    )
    attrs = {CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True, "conversation_thread_id": "thread"}
    return agent, attrs


@pytest.mark.parametrize("run,attempt,request_id", [
    ("new-run", "new-attempt", "new-request"),
    ("old-run", "new-attempt", "old-request"),
])
def test_exact_source_does_not_hide_new_same_number(tmp_path, run, attempt, request_id):
    old = _record()
    current = _record(run, attempt, request=request_id)
    agent, attrs = _committed(tmp_path, [old])
    assert model_visible_active_turn_tool_calls(agent, attrs, [old, current]) == [current]


def test_one_checkpoint_can_hide_mixed_sources_and_retain_same_bare_id(tmp_path):
    old, carried, current = _record(), _record("carried-run"), _record("current-run")
    agent, attrs = _committed(tmp_path, [old, carried], retained=[current])
    assert model_visible_active_turn_tool_calls(agent, attrs, [old, carried, current]) == [current]


def test_legacy_bare_source_is_uncertain_and_cannot_hide_any_domain(tmp_path):
    old, current = _record(), _record("current-run")
    agent, attrs = _committed(tmp_path, [old], legacy=True)
    visible = model_visible_active_turn_tool_calls(agent, attrs, [old, current])
    assert len(visible) == 2
    assert all(item["compact_source_resolution"]["status"] == "uncertain" for item in visible)
    assert all("compact_source_resolution" not in item for item in [old, current])


def test_record_without_origin_is_kept_even_when_scoped_string_matches(tmp_path):
    old = _record()
    unknown = {key: value for key, value in old.items() if key not in {"run_id", "attempt_id"}}
    agent, attrs = _committed(tmp_path, [old])
    visible = model_visible_active_turn_tool_calls(agent, attrs, [old, unknown])
    assert len(visible) == 1
    assert visible[0]["compact_source_resolution"]["status"] == "uncertain"


def test_orphan_ref_never_hides_current_call(tmp_path):
    old, current = _record(), _record("current-run")
    agent, attrs = _committed(tmp_path, [old], orphan=[current])
    assert model_visible_active_turn_tool_calls(agent, attrs, [old, current]) == [current]


def test_carried_boundary_selects_records_not_bare_ids():
    records = [_record("first"), _record("second"), _record("tail")]
    policy = SimpleNamespace(recent_tail_tokens=1, trigger_tokens=100, context_window_tokens=200)
    binding = SimpleNamespace(thread=SimpleNamespace(compact_generation=0))
    plan = _build_active_turn_compact_plan(
        binding, policy, records, [(item, item["call_id"]) for item in records],
    )
    assert plan.source_call_ids == ("call_1", "call_1", "call_1")
    assert plan.retained_call_ids == ()


def test_carried_boundary_allows_source_and_tail_to_share_bare_id():
    records = [_record("first"), _record("second"), _record("tail")]
    policy = SimpleNamespace(recent_tail_tokens=10000, trigger_tokens=100, context_window_tokens=200)
    binding = SimpleNamespace(thread=SimpleNamespace(compact_generation=0))
    plan = _build_active_turn_compact_plan(
        binding, policy, records, [(item, item["call_id"]) for item in records],
    )
    assert plan.source_call_ids == ("call_1",)
    assert plan.retained_call_ids == ("call_1", "call_1")
    assert tuple(ref.run_id for ref in plan.source_call_refs) == ("first",)
    assert tuple(ref.run_id for ref in plan.retained_call_refs) == ("second", "tail")


@pytest.mark.parametrize("min_chars", [0, 10000])
def test_externalized_and_inline_indexes_preserve_exact_attempts(tmp_path, min_chars):
    from agent_py_agent.agent.memory_archive.compact_tool_output_refs import (
        carried_tool_call_records,
    )
    from agent_py_agent.agent.memory_archive.tool_output_externalizer import (
        ExternalizeToolOutputRequest,
        externalize_tool_output_record,
    )

    for attempt in ("a1", "a2", "a2"):
        output = externalize_tool_output_record(ExternalizeToolOutputRequest(
            root=tmp_path, tool="test_tool", call_id="call_1", output="body", ok=True,
            run_id="run", attempt_id=attempt, turn_id=f"turn-{attempt}",
            request_id=f"request-{attempt}", conversation_request_id="origin",
            min_chars=min_chars, preview_chars=100,
        ))
        assert output["attempt_id"] == attempt
    records = carried_tool_call_records(tmp_path, {"conversation_request_id": "origin"})
    assert [item["attempt_id"] for item in records] == ["a1", "a2"]
    assert [item["turn_id"] for item in records] == ["turn-a1", "turn-a2"]


def test_legacy_carried_rows_cannot_dedupe_by_scoped_string(tmp_path):
    from agent_py_agent.agent.memory_archive.compact_tool_output_refs import (
        carried_tool_call_records,
    )
    from agent_py_agent.agent.memory_archive.tool_output_externalizer import (
        ExternalizeToolOutputRequest,
        externalize_tool_output_record,
    )

    for request_id in ("r1", "r2"):
        externalize_tool_output_record(ExternalizeToolOutputRequest(
            root=tmp_path, tool="test_tool", call_id="call_1", output="body", ok=True,
            run_id="run", request_id=request_id, conversation_request_id="origin",
            min_chars=10000, preview_chars=100,
        ))
    records = carried_tool_call_records(tmp_path, {"conversation_request_id": "origin"})
    assert len(records) == 2
    assert all(item["compact_source_resolution"]["status"] == "uncertain" for item in records)


def _commit_environment(tmp_path):
    from agent_py_agent.agent.conversation.live_tool_compact import LiveToolCompactBinding
    from agent_py_agent.agent.conversation.store import ConversationStore

    store = ConversationStore(tmp_path / "store")
    thread = store.threads.get_or_create({
        "canonical_user_id": "local/test", "channel": "test",
        "channel_conversation_id": "refs", "channel_user_id": "local/test",
    })
    agent = SimpleNamespace(
        conversation_store=store, home_paths=SimpleNamespace(owner_compact_dir=tmp_path / "compact"),
    )
    return agent, LiveToolCompactBinding(store=store, thread=thread)


def _commit_request(sources, retained=()):
    from agent_py_agent.agent.conversation.live_tool_compact import LiveToolCompactCommitRequest
    from agent_py_agent.agent.tooling.call_ref import ToolCallRef

    return LiveToolCompactCommitRequest(
        summary="complete replacement", source_tool_call_ids=tuple(item["call_id"] for item in sources),
        retained_tool_call_ids=tuple(item["call_id"] for item in retained),
        source_tool_call_refs=tuple(ToolCallRef.from_dict(_ref(item)) for item in sources),
        retained_tool_call_refs=tuple(ToolCallRef.from_dict(_ref(item)) for item in retained),
        request_id="submitter-request", attempt_id="submitter-attempt",
        projected_tokens_before=100, projected_tokens_after=50,
        policy=SimpleNamespace(context_window_tokens=200, trigger_percent=90, trigger_tokens=180,
                               recovery_target_tokens=100),
    )


def test_orphan_with_same_legacy_hash_inputs_cannot_replace_committed_refs(tmp_path):
    from agent_py_agent.agent.conversation.live_tool_compact import commit_live_tool_compact

    agent, binding = _commit_environment(tmp_path)
    old, current = _record(), _record("current-run")
    committed = commit_live_tool_compact(agent, binding, _commit_request([old]))
    with pytest.raises(RuntimeError, match="generation changed"):
        # A stale candidate can append before losing CAS at the same target generation.
        commit_live_tool_compact(agent, binding, _commit_request([current]))
    attrs = {
        CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True,
        "conversation_thread_id": committed.thread_id,
    }
    assert model_visible_active_turn_tool_calls(agent, attrs, [old, current]) == [current]


def test_commit_keeps_duplicate_bare_ids_counts_and_submitter_semantics(tmp_path):
    from agent_py_agent.agent.conversation.compact_checkpoint import (
        committed_compact_checkpoint_chain,
    )
    from agent_py_agent.agent.conversation.live_tool_compact import commit_live_tool_compact

    agent, binding = _commit_environment(tmp_path)
    sources, retained = [_record("source-a"), _record("source-b")], [_record("tail")]
    thread = commit_live_tool_compact(agent, binding, _commit_request(sources, retained))
    row, = committed_compact_checkpoint_chain(agent, thread)
    assert row["source_tool_call_ids"] == ["call_1", "call_1"]
    assert row["retained_tool_call_ids"] == ["call_1"]
    assert row["source_tool_pairs"] == row["source_tool_pairs_total"] == thread.compact_source_tool_pairs == 2
    assert row["source_tool_call_refs"] == [_ref(item) for item in sources]
    assert row["request_id"] == "submitter-request"
    assert row["attempt_id"] == "submitter-attempt"


@pytest.mark.parametrize("invalid", ["overlap", "duplicate", "missing", "mismatch"])
def test_invalid_exact_boundary_never_writes_candidate(tmp_path, invalid):
    from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
    from agent_py_agent.agent.conversation.live_tool_compact import commit_live_tool_compact

    agent, binding = _commit_environment(tmp_path)
    req = _commit_request([_record()], [_record("tail")])
    if invalid == "overlap":
        req = replace(req, retained_tool_call_refs=req.source_tool_call_refs)
    elif invalid == "duplicate":
        req = replace(req, source_tool_call_ids=("call_1", "call_1"),
                      source_tool_call_refs=req.source_tool_call_refs * 2)
    elif invalid == "missing":
        req = replace(req, source_tool_call_refs=(None,))
    else:
        req = replace(req, source_tool_call_ids=("another-call",))
    with pytest.raises(ConversationCompactError, match="(overlap|duplicate|missing|mismatch)"):
        commit_live_tool_compact(agent, binding, req)
    assert not (tmp_path / "compact").exists()


def test_old_unscoped_large_archive_is_explicitly_uncompactable(tmp_path):
    from agent_py_agent.agent.conversation.active_turn_compact import (
        ActiveTurnArchiveCompactRequest,
        compact_carried_active_turn_archive,
    )

    agent, binding = _commit_environment(tmp_path)
    records = [{"call_id": "call_1", "run_id": "old-run", "output_preview": "x" * 100_000}]
    attrs = {CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True,
             "conversation_thread_id": binding.thread.thread_id}
    result = compact_carried_active_turn_archive(
        agent, binding.store, binding.thread, records,
        ActiveTurnArchiveCompactRequest(attrs, "current-request", "current-attempt"),
    )
    assert result.compacted is False
    assert result.source_resolution == "uncertain"
    assert result.uncertain_call_count == 1
    assert result.thread.compact_generation == 0
    assert not (tmp_path / "compact").exists()
    assert records == [{"call_id": "call_1", "run_id": "old-run", "output_preview": "x" * 100_000}]


def test_mixed_known_and_unknown_archive_only_replaces_known_source():
    unknown, known = {"call_id": "call_1", "run_id": "legacy"}, _record("known")
    records = [unknown, known]
    policy = SimpleNamespace(recent_tail_tokens=10000, trigger_tokens=100, context_window_tokens=200)
    binding = SimpleNamespace(thread=SimpleNamespace(compact_generation=0))
    plan = _build_active_turn_compact_plan(
        binding, policy, records, [(item, item["call_id"]) for item in records],
    )
    assert plan.source_call_ids == ("call_1",)
    assert plan.source_call_refs[0].run_id == "known"
    assert plan.retained_records == (unknown,)
    assert plan.retained_call_refs == (None,)


@pytest.mark.parametrize("field,value", [
    ("schema", "tool_call_ref.v0"), ("attempt_id", ""), ("run_id", 123), ("call_id", False),
])
def test_malformed_ref_never_acquires_filter_authority(tmp_path, field, value):
    old = _record()
    agent, attrs = _committed(tmp_path, [old])
    path = tmp_path / "conversations" / "thread.jsonl"
    row = json.loads(path.read_text(encoding="utf-8"))
    row["source_tool_call_refs"][0][field] = value
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    visible = model_visible_active_turn_tool_calls(agent, attrs, [old])
    assert len(visible) == 1
    assert visible[0]["compact_source_resolution"]["status"] == "uncertain"


def test_legacy_checkpoint_id_inputs_keep_original_digest():
    import hashlib

    from agent_py_agent.agent.conversation.compact_checkpoint import live_tool_compact_checkpoint_id

    thread = SimpleNamespace(thread_id="thread", compact_generation=7)
    expected = hashlib.sha256(b"thread\x008\x00live_tool_ir\x00attempt\x00call_1\x00summary").hexdigest()[:20]
    assert live_tool_compact_checkpoint_id(
        thread, summary="summary", source_tool_call_ids=("call_1",), attempt_id="attempt",
    ) == f"compact-8-{expected}"


def test_native_plan_freezes_call_origin_before_summary_and_commits_exact_tail(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core import _tool_loop_service as service
    from agent_py_agent.agent.backends.tool_ir import AssistantTurn
    from agent_py_agent.agent.conversation.compact_checkpoint import (
        committed_compact_checkpoint_chain,
    )
    from agent_py_agent.tests._tool_runtime_harness import (
        canonical_history_call,
        canonical_history_result,
    )

    agent, binding = _commit_environment(tmp_path)
    calls = [canonical_history_call(
        "read_file", {}, call_id="call_1", source_protocol="native", run_id="origin-run",
        attempt_id=attempt, turn_id=f"turn-{attempt}",
    ) for attempt in ("old-attempt", "new-attempt")]
    history = []
    for call in calls:
        history.extend([AssistantTurn(tool_calls=[call]), canonical_history_result(call, "body")])
    params = SimpleNamespace(
        tool_ir_history=history, request_id="submitter", attempt_id="submitter-attempt", run_id="submitter-run",
    )
    policy = SimpleNamespace(allow_persistent_apply=True, trigger_tokens=180, context_window_tokens=200,
                             recovery_target_tokens=100, recent_tail_tokens=20, trigger_percent=90)
    monkeypatch.setattr(service, "_native_compact_policy", lambda *_: policy)
    monkeypatch.setattr(service, "_native_compact_floor_tokens", lambda *_: 1)
    monkeypatch.setattr(service, "_native_compact_interrupted", lambda *_: False)
    monkeypatch.setattr(service, "_emit_native_compact_progress", lambda *_, **__: None)

    def summary(*_args, **_kwargs):
        # The submitted candidate must retain its before-I/O origin even if the caller replaces a list.
        params.tool_ir_history = history[-2:]
        return binding, "complete replacement", 1, "operation", "active_turn_tool_archive", "conversation_thread"

    monkeypatch.setattr(service, "_live_compact_binding_and_summary", summary)
    plan = service._prepare_native_compact_plan(agent, params, "prompt", estimator=lambda: 190, force=True)
    assert tuple(ref.attempt_id for ref in plan.before_call_refs) == ("old-attempt", "new-attempt")
    assert service._commit_native_ir_generation(agent, params, plan, after_tokens=50) == 1
    updated = binding.store.threads.load(binding.thread.thread_id)
    row, = committed_compact_checkpoint_chain(agent, updated)
    assert row["source_tool_call_refs"] == [{"schema": "tool_call_ref.v1", "run_id": "origin-run",
                                            "attempt_id": "old-attempt", "call_id": "call_1"}]
    assert row["retained_tool_call_refs"][0]["attempt_id"] == "new-attempt"
    assert row["attempt_id"] == "submitter-attempt"
