"""运行中工具 Compact 的逐调用来源合同：四元身份（run/attempt/turn/call）才有隐藏权。

原三元 ToolCallRef 场景逐条迁到 v3 四元 refs；另验证主线 66a598cf3 实际写出的
v2 + tool_call_ref.v1 检查点按 legacy 读取：可读、不报错、不隐藏任何记录、不丢行。
"""
from __future__ import annotations

import copy
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.active_turn_compact import (
    ActiveTurnArchiveCompactRequest,
    compact_carried_active_turn_archive,
    model_visible_active_turn_tool_calls,
    partition_carried_tool_records,
)
from agent_py_agent.agent.conversation.authority import (
    CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR,
)
from agent_py_agent.agent.conversation.compact_checkpoint import (
    LiveToolCompactCheckpointRequest,
    committed_compact_checkpoint_chain,
    write_live_tool_compact_checkpoint,
)
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.conversation.live_tool_compact import (
    LiveToolCompactBinding,
    LiveToolCompactCommitRequest,
    commit_live_tool_compact,
)
from agent_py_agent.agent.conversation.store import ConversationStore

_POLICY = SimpleNamespace(
    context_window_tokens=200, trigger_percent=90, trigger_tokens=180, recovery_target_tokens=100,
)

# 主线 66a598cf3 的原 writer 在真实临时 Store 中生成的一行（仅 thread_id 在用例中替换）。
_MAIN_66A598CF3_V2_ROW = {
    "attempt_id": "submitter-attempt", "backend": "fixture",
    "checkpoint_id": "compact-1-f853fe0e9c2d568ec90b",
    "commit_authority": "conversation_thread.compact_checkpoint_id", "context_window_tokens": 200,
    "created_at": 1790218013.9036522, "event": "conversation_compact_checkpoint", "forced": False,
    "generation": 1, "model": "fixture-model", "operation_evidence": {}, "previous_checkpoint_id": "",
    "previous_generation": 0, "projected_tokens_after": 50, "projected_tokens_before": 100,
    "recovery_target_tokens": 100, "request_id": "submitter-request",
    "retained_tool_call_ids": ["call_1"],
    "retained_tool_call_refs": [{"attempt_id": "current-attempt", "call_id": "call_1",
                                 "run_id": "current-run", "schema": "tool_call_ref.v1"}],
    "retained_tool_pairs": 1, "schema": "conversation_compact_checkpoint.v2",
    "source_end_byte_offset": 0, "source_end_message_id": "", "source_kind": "live_tool_ir",
    "source_messages": 0, "source_messages_total": 0, "source_start_byte_offset": 0,
    "source_start_message_id": "", "source_tool_call_ids": ["call_1"],
    "source_tool_call_refs": [{"attempt_id": "old-attempt", "call_id": "call_1",
                               "run_id": "old-run", "schema": "tool_call_ref.v1"}],
    "source_tool_pairs": 1, "source_tool_pairs_total": 1, "status": "validated_candidate",
    "summary": "complete replacement",
    "summary_sha256": "cd27469325f2c97ad6c3cc2d21034b6cc14b2fe81a337118cee8015bceef7592",
    "thread_id": "THREAD", "trigger_percent": 90, "trigger_tokens": 180,
}


def _record(run="old-run", attempt="old-attempt", call="call_1", request="old-request", turn=""):
    return {
        "run_id": run, "attempt_id": attempt, "turn_id": turn or f"{run}:{attempt}:turn",
        "call_id": call, "request_id": request, "conversation_request_id": request,
        "scoped_call_id": f"{run}:{call}", "tool": "read_file", "ok": True,
        "output_preview": f"{run}/{attempt}/{call}",
    }


def _ref(record):
    return {key: record[key] for key in ("run_id", "attempt_id", "turn_id", "call_id")}


def _environment(tmp_path):
    store = ConversationStore(tmp_path / "store")
    thread = store.threads.get_or_create({
        "canonical_user_id": "local/test", "channel": "test",
        "channel_conversation_id": "refs", "channel_user_id": "local/test",
    })
    agent = SimpleNamespace(
        conversation_store=store, home_paths=SimpleNamespace(owner_compact_dir=tmp_path / "compact"),
    )
    attrs = {CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True, "conversation_thread_id": thread.thread_id}
    return agent, LiveToolCompactBinding(store=store, thread=thread), attrs


def _request(sources, retained=()):
    return LiveToolCompactCommitRequest(
        summary="complete replacement",
        source_tool_call_ids=tuple(item["call_id"] for item in sources),
        retained_tool_call_ids=tuple(item["call_id"] for item in retained),
        projected_tokens_before=100, projected_tokens_after=50, policy=_POLICY,
        request_id="submitter-request", attempt_id="submitter-attempt",
        source_tool_refs=tuple(_ref(item) for item in sources),
        retained_tool_refs=tuple(_ref(item) for item in retained),
    )


def _committed(tmp_path, sources, retained=()):
    agent, binding, attrs = _environment(tmp_path)
    thread = commit_live_tool_compact(agent, binding, _request(sources, retained))
    return agent, replace(binding, thread=thread), attrs


def _legacy_main_row_environment(tmp_path, *, mutate=None):
    """用主线 66a598cf3 原 writer 的实际行格式建立已提交链，thread 指针指向该行。"""
    agent, binding, attrs = _environment(tmp_path)
    row = copy.deepcopy(_MAIN_66A598CF3_V2_ROW)
    row["thread_id"] = binding.thread.thread_id
    if mutate is not None:
        mutate(row)
    path = tmp_path / "compact" / "conversations" / f"{binding.thread.thread_id}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    thread = replace(binding.thread, compact_checkpoint_id=row["checkpoint_id"], compact_generation=1)
    agent.conversation_store = SimpleNamespace(threads=SimpleNamespace(load_report=lambda _thread_id: (thread, None)))
    return agent, thread, attrs


@pytest.mark.parametrize("run,attempt,turn", [
    ("new-run", "new-attempt", ""), ("old-run", "new-attempt", ""), ("old-run", "old-attempt", "later-turn"),
])
def test_exact_source_does_not_hide_new_same_number(tmp_path, run, attempt, turn):
    old = _record()
    current = _record(run, attempt, request="new-request", turn=turn)
    agent, _binding, attrs = _committed(tmp_path, [old])
    assert model_visible_active_turn_tool_calls(agent, attrs, [old, current]) == [current]


def test_one_checkpoint_can_hide_mixed_sources_and_retain_same_bare_id(tmp_path):
    old, carried, current = _record(), _record("carried-run"), _record("current-run")
    agent, _binding, attrs = _committed(tmp_path, [old, carried], retained=[current])
    assert model_visible_active_turn_tool_calls(agent, attrs, [old, carried, current]) == [current]


def test_legacy_bare_source_cannot_hide_any_domain(tmp_path):
    old, current = _record(), _record("current-run")
    agent, _thread, attrs = _legacy_main_row_environment(
        tmp_path, mutate=lambda row: [row.pop(key) for key in ("source_tool_call_refs", "retained_tool_call_refs")],
    )
    before = copy.deepcopy([old, current])
    visible = model_visible_active_turn_tool_calls(agent, attrs, [old, current])
    assert visible == before
    assert [old, current] == before


def test_main_66a598cf3_v2_ref_rows_read_as_legacy_without_hiding_or_losing_rows(tmp_path):
    old, current = _record(turn="old-run:old-attempt:turn"), _record("current-run", "current-attempt")
    agent, thread, attrs = _legacy_main_row_environment(tmp_path)
    chain = committed_compact_checkpoint_chain(agent, thread)
    assert [row["checkpoint_id"] for row in chain] == [_MAIN_66A598CF3_V2_ROW["checkpoint_id"]]
    assert chain[0]["source_tool_call_refs"] == _MAIN_66A598CF3_V2_ROW["source_tool_call_refs"]
    visible = model_visible_active_turn_tool_calls(agent, attrs, [old, current])
    # 三元 v1 引用缺 turn，不能冒充四元来源：已压的旧调用会重新可见，但不会误隐藏或丢失任何行。
    assert visible == [old, current]


def test_record_without_origin_is_kept_even_when_scoped_string_matches(tmp_path):
    old = _record()
    unknown = {key: value for key, value in old.items() if key not in {"run_id", "attempt_id", "turn_id"}}
    agent, _binding, attrs = _committed(tmp_path, [old])
    assert model_visible_active_turn_tool_calls(agent, attrs, [old, unknown]) == [unknown]


def test_orphan_ref_never_hides_current_call(tmp_path):
    old, current = _record(), _record("current-run")
    agent, binding, attrs = _committed(tmp_path, [old])
    # 只写候选、不经原 CAS：孤立候选即使内容完整也没有隐藏权。
    write_live_tool_compact_checkpoint(agent, LiveToolCompactCheckpointRequest(
        thread=binding.thread, summary="orphan", source_tool_call_ids=("call_1",), retained_tool_call_ids=(),
        projected_tokens_before=100, projected_tokens_after=50, policy=_POLICY,
        request_id="late-request", attempt_id="late-attempt", source_tool_refs=(_ref(current),),
    ))
    assert model_visible_active_turn_tool_calls(agent, attrs, [old, current]) == [current]


def test_carried_boundary_selects_records_not_bare_ids():
    records = [_record("first"), _record("second"), _record("tail")]
    source = partition_carried_tool_records(records, recent_tail_tokens=1)
    assert tuple(ref["call_id"] for ref in source.source_tool_refs) == ("call_1", "call_1", "call_1")
    assert source.retained_tool_refs == ()


def test_carried_boundary_allows_source_and_tail_to_share_bare_id():
    records = [_record("first"), _record("second"), _record("tail")]
    source = partition_carried_tool_records(records, recent_tail_tokens=10000)
    assert tuple(ref["call_id"] for ref in source.source_tool_refs) == ("call_1",)
    assert tuple(ref["call_id"] for ref in source.retained_tool_refs) == ("call_1", "call_1")
    assert tuple(ref["run_id"] for ref in source.source_tool_refs) == ("first",)
    assert tuple(ref["run_id"] for ref in source.retained_tool_refs) == ("second", "tail")


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
    assert [item["request_id"] for item in records] == ["r1", "r2"]
    assert all(not item["attempt_id"] and not item["turn_id"] for item in records)


def test_orphan_with_same_bare_inputs_cannot_replace_committed_refs(tmp_path):
    agent, binding, attrs = _environment(tmp_path)
    old, current = _record(), _record("current-run")
    commit_live_tool_compact(agent, binding, _request([old]))
    with pytest.raises(RuntimeError, match="generation changed"):
        # 同代次迟到候选先落账再输掉 CAS；它不能以同一 ID 覆盖已提交来源。
        commit_live_tool_compact(agent, binding, _request([current]))
    assert model_visible_active_turn_tool_calls(agent, attrs, [old, current]) == [current]


def test_orphan_cannot_replace_committed_retained_boundary(tmp_path):
    agent, binding, _attrs = _environment(tmp_path)
    first = _request([_record()], [_record("tail-a")])
    thread = commit_live_tool_compact(agent, binding, first)
    second = _request([_record()], [_record("tail-b")])
    with pytest.raises(RuntimeError, match="generation changed"):
        commit_live_tool_compact(agent, binding, second)
    row, = committed_compact_checkpoint_chain(agent, thread)
    assert [ref["run_id"] for ref in row["retained_tool_refs"]] == ["tail-a"]


def test_commit_keeps_duplicate_bare_ids_counts_and_submitter_semantics(tmp_path):
    agent, binding, _attrs = _environment(tmp_path)
    sources, retained = [_record("source-a"), _record("source-b")], [_record("tail")]
    thread = commit_live_tool_compact(agent, binding, _request(sources, retained))
    row, = committed_compact_checkpoint_chain(agent, thread)
    assert row["source_tool_call_ids"] == ["call_1", "call_1"]
    assert row["retained_tool_call_ids"] == ["call_1"]
    assert row["source_tool_pairs"] == row["source_tool_pairs_total"] == thread.compact_source_tool_pairs == 2
    assert row["source_tool_refs"] == [_ref(item) for item in sources]
    assert row["request_id"] == "submitter-request"
    assert row["attempt_id"] == "submitter-attempt"


@pytest.mark.parametrize(("invalid", "code"), [
    ("overlap", "COMPACT_TOOL_BOUNDARY_INVALID"), ("duplicate", "COMPACT_TOOL_BOUNDARY_INVALID"),
    ("missing", "COMPACT_TOOL_COVERAGE_UNKNOWN"), ("mismatch", "COMPACT_TOOL_BOUNDARY_INVALID"),
])
def test_invalid_exact_boundary_never_writes_candidate(tmp_path, invalid, code):
    agent, binding, _attrs = _environment(tmp_path)
    req = _request([_record()], [_record("tail")])
    if invalid == "overlap":
        req = replace(req, retained_tool_call_ids=req.source_tool_call_ids, retained_tool_refs=req.source_tool_refs)
    elif invalid == "duplicate":
        req = replace(req, source_tool_call_ids=("call_1", "call_1"), source_tool_refs=req.source_tool_refs * 2)
    elif invalid == "missing":
        req = replace(req, source_tool_refs=({"call_id": "call_1"},))
    else:
        req = replace(req, source_tool_call_ids=("another-call",))
    with pytest.raises(ConversationCompactError) as caught:
        commit_live_tool_compact(agent, binding, req)
    assert caught.value.code == code
    assert not (tmp_path / "compact").exists()


def test_old_unscoped_large_archive_is_explicitly_uncompactable(tmp_path):
    agent, binding, attrs = _environment(tmp_path)
    records = [{"call_id": "call_1", "run_id": "old-run", "output_preview": "x" * 100_000}]
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
    source = partition_carried_tool_records([unknown, known], recent_tail_tokens=10000)
    assert tuple(ref["call_id"] for ref in source.source_tool_refs) == ("call_1",)
    assert source.source_tool_refs[0]["run_id"] == "known"
    assert source.retained_records == (unknown,)
    assert source.retained_tool_refs == ()


@pytest.mark.parametrize("field,value", [
    ("schema", "tool_call_ref.v0"), ("attempt_id", ""), ("run_id", 123), ("call_id", False),
])
def test_malformed_legacy_ref_never_acquires_filter_authority(tmp_path, field, value):
    old = _record(turn="old-run:old-attempt:turn")

    def mutate(row):
        row["source_tool_call_refs"][0][field] = value

    agent, _thread, attrs = _legacy_main_row_environment(tmp_path, mutate=mutate)
    assert model_visible_active_turn_tool_calls(agent, attrs, [old]) == [old]


def test_tampered_v3_ref_fails_closed_instead_of_hiding(tmp_path):
    old, current = _record(), _record("current-run")
    agent, binding, attrs = _committed(tmp_path, [old])
    path = tmp_path / "compact" / "conversations" / f"{binding.thread.thread_id}.jsonl"
    row = json.loads(path.read_text(encoding="utf-8"))
    row["source_tool_refs"][0]["run_id"] = "current-run"
    path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    with pytest.raises(OSError, match="content identity"):
        model_visible_active_turn_tool_calls(agent, attrs, [old, current])


def test_native_plan_freezes_call_origin_before_summary_and_commits_exact_tail(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core import _tool_loop_service as service
    from agent_py_agent.agent.agent_core import tool_request_projection
    from agent_py_agent.agent.backends.tool_ir import AssistantTurn
    from agent_py_agent.tests._tool_runtime_harness import (
        canonical_history_call,
        canonical_history_result,
    )

    agent, binding, _attrs = _environment(tmp_path)
    calls = [canonical_history_call(
        "read_file", {}, call_id="call_1", source_protocol="native", run_id="origin-run",
        attempt_id=attempt, turn_id=f"turn-{attempt}",
    ) for attempt in ("old-attempt", "new-attempt")]
    history = []
    for call in calls:
        history.extend([AssistantTurn(tool_calls=[call]), canonical_history_result(call, "body")])
    params = SimpleNamespace(
        tool_ir_history=history, request_id="submitter", attempt_id="submitter-attempt",
        run_id="submitter-run", compact_context=None,
    )
    policy = SimpleNamespace(allow_persistent_apply=True, trigger_tokens=180, context_window_tokens=200,
                             recovery_target_tokens=100, recent_tail_tokens=20, trigger_percent=90)
    monkeypatch.setattr(tool_request_projection, "text_request_capacity_known", lambda *_: True)
    monkeypatch.setattr(service, "_native_compact_policy", lambda *_: policy)
    monkeypatch.setattr(service, "_native_compact_floor_tokens", lambda *_: 1)
    monkeypatch.setattr(service, "_native_compact_interrupted", lambda *_: False)
    monkeypatch.setattr(service, "_emit_native_compact_progress", lambda *_, **__: None)

    def summary(*_args, **_kwargs):
        # 调用方在摘要期间替换列表，提交仍必须使用 I/O 前冻结的原四元来源。
        params.tool_ir_history = history[-2:]
        return binding, "complete replacement", 1, "operation", "active_turn_tool_archive", "conversation_thread"

    monkeypatch.setattr(service, "_live_compact_binding_and_summary", summary)
    plan = service._prepare_native_compact_plan(agent, params, "prompt", estimator=lambda: 190, force=True)
    assert tuple(ref["attempt_id"] for ref in plan.before_tool_refs) == ("old-attempt", "new-attempt")
    commit = service._commit_native_ir_generation(agent, params, plan, after_tokens=50)
    assert commit.generation == 1
    updated = binding.store.threads.load(binding.thread.thread_id)
    row, = committed_compact_checkpoint_chain(agent, updated)
    assert row["source_tool_refs"] == [{"run_id": "origin-run", "attempt_id": "old-attempt",
                                        "turn_id": "turn-old-attempt", "call_id": "call_1"}]
    assert row["retained_tool_refs"][0]["attempt_id"] == "new-attempt"
    assert row["attempt_id"] == "submitter-attempt"
