"""自动 Compact 与恢复只消费当前宿主事实，历史规模不扩大普通读取窗口。"""
import json
from collections import Counter
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.finalization_compact_auto import compact_auto_cycle_fields
from agent_py_agent.agent.conversation.authority import CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.request_binding import GatewayTaskBindingWriter
from agent_py_agent.agent.gateway_parts.request_context import GatewayAskRunContext
from agent_py_agent.agent.gateway_parts.request_execution import (
    _gateway_recovered_active_turn_tool_calls,
)
from agent_py_agent.agent.memory_archive import compact_apply, compact_suggest
from agent_py_agent.agent.memory_archive.compact import (
    MemoryCompactPlanOptions,
    build_memory_compact_plan,
)
from agent_py_agent.agent.memory_archive.compact_apply import (
    MemoryCompactApplyOptions,
    apply_memory_compact,
)
from agent_py_agent.agent.memory_archive.compact_auto import (
    MemoryCompactAutoCycleOptions,
    run_memory_compact_auto_cycle,
)
from agent_py_agent.agent.memory_archive.compact_context_bundle import load_main_context_bundle_ref
from agent_py_agent.agent.memory_archive.compact_tool_output_refs import CarriedIndexSource
from agent_py_agent.agent.memory_archive.runtime import (
    ArchiveLiveToolCallParams,
    ArchiveRunTurnResult,
    archive_live_tool_call,
    archive_run_turn,
)
from agent_py_agent.agent.memory_archive.runtime.turn_archiver import (
    ArchiveRunTurnParams,
    ArchiveTurnContext,
)
from agent_py_agent.agent.memory_archive.storage import write_compression_snapshot_file
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.user_space.context_bundle import (
    MainContextBundleRequest,
    build_main_context_bundle,
)
from agent_py_agent.tests.memory_compact_support import compact_snapshot, write_compact_fixture
from agent_py_agent.tests.test_memory_runtime_compact_auto_continuation import (
    CaptureBackend,
    _finalize_context_for_continuation,
)
from agent_py_agent.tests.test_r5db_closeout_tool_index_window import SCOPE, _externalize, _history


@pytest.fixture
def current_turn(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"),
                                   enable_plugins=False, enable_subagents=False), tmp_path / "workspace")
    agent.backend = CaptureBackend()
    ctx = replace(_finalize_context_for_continuation(tool_rounds=1, executed_tools=["read_file"]),
                  **SCOPE, compact_auto_continue_depth=0, do_save=False)
    return agent, ctx


def _history_files(root, previous, count):
    for directory, suffix in ((root / "audit", ".jsonl"),
                              (root / "memory_archive/snapshots", ".json"),
                              (root / "memory_archive/tokens", ".json")):
        directory.mkdir(parents=True, exist_ok=True)
        for number in range(previous, count):
            (directory / f"history-{number}{suffix}").write_text("{}\n")


@pytest.mark.parametrize("delegated", [False, True])
def test_below_threshold_finalization_never_discovers_or_reads_history(current_turn, monkeypatch, delegated,
                                                                    record_property):
    agent, ctx = current_turn
    if delegated:
        ctx = replace(ctx, task_attributes={CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True})
    root = agent.home_paths.owner_home_dir
    original_glob, original_open = Path.glob, Path.open
    counts = {"glob": 0, "history_reads": 0}

    def glob(path, *args, **kwargs):
        counts["glob"] += 1
        return original_glob(path, *args, **kwargs)

    def opening(path, *args, **kwargs):
        counts["history_reads"] += path.name.startswith("history-")
        return original_open(path, *args, **kwargs)

    previous = 0
    for history in (0, 100, 1000, 10000):
        _history_files(root, previous, history)
        counts.update(glob=0, history_reads=0)
        with monkeypatch.context() as measured:
            measured.setattr(Path, "glob", glob)
            measured.setattr(Path, "open", opening)
            fields = compact_auto_cycle_fields(agent, ctx, {"turn": 1, "active": 1})
        assert fields["memory_compact_auto_status"] == (
            "delegated_to_conversation_store" if delegated else "skipped_below_threshold")
        assert counts == {"glob": 0, "history_reads": 0}
        record_property(f"below_threshold_{delegated}_{history}", dict(counts))
        previous = history


class _MeasuredFile:
    def __init__(self, handle, name, counts):
        self.handle, self.name, self.counts = handle, name, counts

    def __enter__(self):
        self.handle.__enter__()
        return self

    def __exit__(self, *args):
        return self.handle.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self.handle, name)

    def _count(self, data):
        self.counts["bytes"][self.name] += len(data.encode("utf-8") if isinstance(data, str) else data)
        return data

    def read(self, *args):
        return self._count(self.handle.read(*args))

    def __iter__(self):
        for line in self.handle:
            yield self._count(line)


@contextmanager
def _measure_indices(monkeypatch):
    original_open, original_glob = Path.open, Path.glob
    counts = {"opens": Counter(), "bytes": Counter(), "glob": 0}

    def opening(path, *args, **kwargs):
        handle = original_open(path, *args, **kwargs)
        if path.name == "index.jsonl" and path.parent.name == "tool_outputs":
            counts["opens"][str(path)] += 1
            return _MeasuredFile(handle, str(path), counts)
        return handle

    def glob(path, *args, **kwargs):
        counts["glob"] += 1
        return original_glob(path, *args, **kwargs)

    with monkeypatch.context() as measured:
        measured.setattr(Path, "open", opening)
        measured.setattr(Path, "glob", glob)
        yield counts


def _bundle(agent, roots):
    return build_main_context_bundle(MainContextBundleRequest(
        root=agent.home_paths.owner_home_dir, home_paths=agent.home_paths, user_prompt="继续当前任务", **SCOPE,
        tool_index_sources=tuple(CarriedIndexSource(root, f"source-{index}") for index, root in enumerate(roots))))


def _current_plan(agent, **kwargs):
    return MemoryCompactPlanOptions(source_mode="current_turn", session_id=agent.config.agent_name, **SCOPE, **kwargs)


def _prepare(agent, bundle):
    root = agent.home_paths.owner_home_dir
    options = MemoryCompactApplyOptions(_current_plan(agent), main_context_bundle_ref=bundle.json_path)
    plan = build_memory_compact_plan(root, options.plan_options)
    paths, payload = compact_apply._prepare_apply_metadata(compact_apply._PrepareApplyMetadataRequest(
        root, plan, options, "2026-11-08T00:00:00+00:00"))
    return plan, paths, payload


@pytest.mark.parametrize("old_rows", [100, 1000, 10000])
def test_actual_restore_reads_original_two_index_windows(current_turn, monkeypatch, old_rows, record_property):
    agent, _ = current_turn
    owner = agent.home_paths.owner_home_dir
    task = owner / "runs/physical/arbitrary/work"
    paths = (_history(owner, old_rows), _history(task, old_rows))
    first = _bundle(agent, (owner, task))
    marks = first.bundle["tool_index_marks"]
    _externalize(owner, SCOPE, "owner-call")
    _externalize(task, {**SCOPE, "attempt_id": "first", "turn_id": "turn"}, "same-call")
    continuation = _bundle(agent, (owner, task))
    _externalize(task, {**SCOPE, "attempt_id": "second", "turn_id": "turn"}, "same-call")
    _externalize(task, {**SCOPE, "request_id": "foreign"}, "foreign")
    assert continuation.bundle["tool_index_marks"] == marks
    for number in range(old_rows):
        (owner / f"tasks/archived/{number}/work/blobs/tool_outputs").mkdir(parents=True)
    plan, apply_paths, payload = _prepare(agent, continuation)
    with _measure_indices(monkeypatch) as counts:
        restored = compact_apply.restore_refs_payload(payload, plan, apply_paths, "now")
    outputs = restored["source_refs"]["tool_outputs"]
    assert [row["call_id"] for row in outputs] == ["owner-call", "same-call", "same-call"]
    assert [row["attempt_id"] for row in outputs[1:]] == ["first", "second"]
    assert set(counts["opens"]) == {str(path) for path in paths} and counts["glob"] == 0
    for path in paths:
        assert counts["bytes"][str(path)] <= path.stat().st_size - marks[str(path)][2] + 3
    record_property(f"restore_{old_rows}", {"opens": dict(counts["opens"]), "bytes": dict(counts["bytes"])})


@pytest.mark.parametrize("fault", ["missing_marks", "bad_mark", "missing_index", "replace", "truncate",
                                    "unaligned", "wrong_request", "partial"])
def test_automatic_apply_never_falls_back_from_invalid_provenance(current_turn, monkeypatch, fault):
    agent, _ = current_turn
    owner = agent.home_paths.owner_home_dir
    index = _history(owner, 1000)
    bundle = _bundle(agent, (owner,))
    _externalize(owner, SCOPE, "current")
    payload = json.loads(Path(bundle.json_path).read_text())
    if fault == "missing_marks":
        payload.pop("tool_index_marks")
    elif fault == "bad_mark":
        payload["tool_index_marks"][str(index)] = None
    elif fault == "missing_index":
        index.unlink()
    elif fault == "replace":
        replacement = index.with_suffix(".replacement")
        replacement.write_bytes(index.read_bytes())
        replacement.replace(index)
    elif fault == "truncate":
        index.write_bytes(b"")
    elif fault == "unaligned":
        payload["tool_index_marks"][str(index)][2] -= 1
    elif fault == "wrong_request":
        payload["scope"]["request_id"] = "foreign"
    elif fault == "partial":
        payload["tool_index_sources_complete"] = False
    Path(bundle.json_path).write_text(json.dumps(payload))
    options = MemoryCompactApplyOptions(_current_plan(agent), main_context_bundle_ref=bundle.json_path)
    with _measure_indices(monkeypatch) as counts, pytest.raises(compact_apply.IncompleteCompactSources):
        apply_memory_compact(owner, options)
    assert counts["glob"] == 0 and sum(counts["bytes"].values()) <= 1


def test_no_declared_bundle_keeps_unknown_tool_coverage_without_discovery(current_turn, monkeypatch):
    agent, _ = current_turn
    owner = agent.home_paths.owner_home_dir
    _history(owner, 1000)
    _externalize(owner, SCOPE, "unproven-window")
    plan, paths, payload = _prepare(agent, SimpleNamespace(json_path=""))
    with _measure_indices(monkeypatch) as counts:
        restored = compact_apply.restore_refs_payload(payload, plan, paths, "now")
    assert not counts["opens"] and counts["glob"] == 0
    assert restored["source_refs"]["tool_outputs"] == []
    assert restored["source_scope"]["tool_index_coverage"] == "unknown"
    assert restored["source_scope"]["coverage"] == "partial"


@pytest.mark.parametrize("receipt", [None, ArchiveRunTurnResult((), 0, 0, (), (), ())])
@pytest.mark.parametrize("missing_id", [False, True])
def test_empty_receipt_is_distinct_from_unknown_and_never_claims_prior_carry(current_turn, monkeypatch, receipt, missing_id):
    agent, _ = current_turn
    raw_refs = ({"path": "", "event_id": "missing-path"},) if missing_id else ()
    with monkeypatch.context() as guarded:
        guarded.setattr(Path, "glob", lambda *_args, **_kwargs: pytest.fail("history discovery"))
        plan = build_memory_compact_plan(agent.home_paths.owner_home_dir,
            _current_plan(agent, archive_result=receipt, raw_archive_refs=raw_refs))
    assert plan["archive"]["receipt_known"] is (receipt is not None)
    assert plan["archive"]["receipt_event_count"] == (None if receipt is None else 0)
    assert plan["archive"]["current_append_complete"] is (receipt is not None and not missing_id)
    assert plan["archive"]["raw_archive_refs"] == list(raw_refs)
    assert plan["source_scope"] == {"mode": "current_turn", "coverage": "partial", "prior_carry": "unknown"}
    assert plan["tokens"]["latest"] == [] and plan["tokens"]["known"] is False


def test_explicit_historical_plan_keeps_all_original_sources(tmp_path):
    write_compact_fixture(tmp_path)
    plan = build_memory_compact_plan(tmp_path, MemoryCompactPlanOptions())
    assert (plan["archive"]["record_count"], plan["snapshots"]["file_count"], plan["tokens"]["ledger_count"]) == (2, 1, 1)
    assert plan["source_scope"]["mode"] == "historical"


def test_triggered_real_cycle_keeps_terminal_receipt_and_all_live_tool_refs(current_turn, monkeypatch, record_property):
    agent, ctx = current_turn
    owner = agent.home_paths.owner_home_dir
    _history(owner, 100)
    bundle = _bundle(agent, (owner,))
    _externalize(owner, SCOPE, "indexed-now")
    calls = []
    for index in range(25):
        call = {"id": f"live-{index}", "tool": "read_file", "parameters": {}, "ok": True, "output": "done"}
        live = archive_live_tool_call(ArchiveLiveToolCallParams(
            root=owner, session_id=agent.config.agent_name, **SCOPE, tool_index=index, tool_record=call))
        calls.append({**call, "raw_archive_path": str(live.write_paths[0]), "raw_archive_event_id": live.event_ids[0]})
    receipt = archive_run_turn(ArchiveRunTurnParams(owner, ArchiveTurnContext(
        session_id=agent.config.agent_name, user_prompt="继续当前任务", response_text="本轮已完成", backend="echo",
        tool_calls=calls, **SCOPE)))
    assert receipt.event_count == 2
    snapshot = replace(compact_snapshot(), session_id=agent.config.agent_name, turn_range={**SCOPE, "start": 1, "end": 1},
                       dispatch_events=[], task_refs=[SCOPE["task_id"]])
    snapshot_path = write_compression_snapshot_file(owner, snapshot)
    ctx = replace(ctx, archive_tool_calls=calls, compression_snapshot_path=str(snapshot_path),
                  main_context_bundle_path=bundle.json_path)
    observed, original_plan, original_open = [], compact_suggest.build_memory_compact_plan, Path.open

    def plan(root, options):
        value = original_plan(root, options)
        observed.append(value)
        return value

    def opening(path, *args, **kwargs):
        if path.parent == owner / "audit" and "r" in (args[0] if args else kwargs.get("mode", "r")):
            pytest.fail("automatic planning reopened a daily raw archive")
        if path.name.startswith("history-"):
            pytest.fail("automatic planning parsed unrelated history")
        return original_open(path, *args, **kwargs)

    previous = 0
    for history in (0, 100, 1000, 10000):
        _history_files(owner, previous, history)
        with monkeypatch.context() as guarded:
            guarded.setattr(compact_suggest, "build_memory_compact_plan", plan)
            guarded.setattr(Path, "open", opening)
            fields = compact_auto_cycle_fields(agent, ctx, {"turn": 1_000_000, "active": 1_000_000},
                                                archive_result=receipt)
        assert fields["memory_compact_auto_status"] == "needs_user_confirmation"
        current = observed[-1]
        assert current["archive"]["events"] == [event.to_dict() for event in receipt.events]
        assert current["archive"]["event_ids"] == list(receipt.event_ids)
        assert current["archive"]["content_hashes"] == list(receipt.content_hashes)
        assert len(current["archive"]["raw_archive_refs"]) == 25
        assert set(current["archive"]["files"][0]["event_ids"]) == set(receipt.event_ids) | {
            call["raw_archive_event_id"] for call in calls}
        assert current["snapshots"]["latest"][0]["file_path"] == str(snapshot_path)
        assert current["tokens"]["current"]["active"] == 1_000_000 and current["tokens"]["latest"] == []
        assert current["source_scope"]["coverage"] == "partial"
        record_property(f"triggered_history_{history}", {"daily_archive_reads": 0, "live_refs": 25})
        previous = history
    with monkeypatch.context() as guarded:
        guarded.setattr(Path, "open", opening)
        applied = apply_memory_compact(owner, MemoryCompactApplyOptions(_current_plan(
            agent, archive_result=receipt, raw_archive_refs=tuple(observed[-1]["archive"]["raw_archive_refs"]),
            compression_snapshot_path=str(snapshot_path), token_ledger={"turn": 1}), main_context_bundle_ref=bundle.json_path))
    restored = json.loads(Path(applied["refs"]["restore_refs"]).read_text())
    assert len(restored["source_refs"]["raw_archive_refs"]) == 25
    assert restored["source_refs"]["tool_outputs"][0]["call_id"] == "indexed-now"
    assert restored["source_refs"]["token_ledgers"] == []
    assert applied["main_context_bundle"]["tool_index_marks"] == bundle.bundle["tool_index_marks"]
    # 没有快照时保留 partial，由原 action guard 判断；不能退回整读原日日志来凑齐。
    with monkeypatch.context() as guarded:
        guarded.setattr(Path, "open", opening)
        partial = apply_memory_compact(owner, MemoryCompactApplyOptions(_current_plan(
            agent, archive_result=receipt), main_context_bundle_ref=bundle.json_path))
    assert partial["source_plan"]["source_scope"]["coverage"] == "partial"


def _recovery_context(agent, task):
    request = {"id": SCOPE["request_id"], "active_turn_recovery": {
        "schema_version": "gateway_active_turn_recovery.v1", "request_id": SCOPE["request_id"]}}
    path = agent.home_paths.owner_home_dir / "recovered-request.json"
    path.write_text(json.dumps(request))
    writer = GatewayTaskBindingWriter(path, SCOPE["request_id"], request)
    assert writer(SimpleNamespace(thread_id="thread", task_id=SCOPE["task_id"], task_path=str(task)))
    return GatewayAskRunContext(agent, request, path, path.with_suffix(".response"), SCOPE["request_id"], None)


def test_gateway_recovery_uses_only_host_bound_owner_task_and_pinned_roots(current_turn, monkeypatch, record_property):
    agent, _ = current_turn
    owner = agent.home_paths.owner_home_dir
    task, pinned = owner / "runs/physical/current", owner / "runs/physical/pinned/work"
    roots = (owner, task / "work", pinned)
    for index, root in enumerate(roots):
        _history(root, 1)
        _externalize(root, SCOPE, f"current-{index}")
    _externalize(pinned, {**SCOPE, "request_id": "other"}, "foreign")
    context = _recovery_context(agent, task)
    agent._current_run_params = SimpleNamespace(request_id=SCOPE["request_id"], task_attributes={
        "runtime_tool_output_archive_root": str(pinned), "run_workspace": {"work_dir": str(task / "work")}})
    results = []
    previous = 0
    for history in (0, 100, 1000, 10000):
        for number in range(previous, history):
            (owner / f"runs/history/{number}/work/blobs/tool_outputs").mkdir(parents=True)
        with _measure_indices(monkeypatch) as counts:
            records = _gateway_recovered_active_turn_tool_calls(context)
        assert [row["call_id"] for row in records] == ["current-0", "current-1", "current-2"]
        assert set(counts["opens"]) == {str(root / "blobs/tool_outputs/index.jsonl") for root in roots}
        assert counts["glob"] == 0
        results.append((sum(counts["opens"].values()), sum(counts["bytes"].values())))
        previous = history
    assert results == [results[0]] * 4
    context.request["active_turn_recovery"]["request_id"] = "mismatched"
    with _measure_indices(monkeypatch) as counts:
        assert _gateway_recovered_active_turn_tool_calls(context) == []
    assert not counts["opens"]
    record_property("gateway_exact_sources", results)


@pytest.mark.parametrize("old_rows", [100, 1000, 10000])
def test_legacy_recovery_reports_the_real_one_shot_owner_index_byte_cost(current_turn, monkeypatch, old_rows,
                                                                      record_property):
    agent, _ = current_turn
    owner = agent.home_paths.owner_home_dir
    index = _history(owner, old_rows)
    _externalize(owner, SCOPE, "truth")
    context = _recovery_context(agent, owner / "not-yet-created-task")
    with _measure_indices(monkeypatch) as counts:
        records = _gateway_recovered_active_turn_tool_calls(context)
    assert [row["call_id"] for row in records] == ["truth"]
    assert counts["opens"] == {str(index): 1} and counts["bytes"][str(index)] == index.stat().st_size
    record_property(f"legacy_recovery_bytes_{old_rows}", counts["bytes"][str(index)])


def test_ten_thousand_restore_windows_keep_the_original_current_facts(current_turn, monkeypatch, record_property):
    agent, _ = current_turn
    owner = agent.home_paths.owner_home_dir
    index = _history(owner, 10000)
    bundle = _bundle(agent, (owner,))
    _externalize(owner, SCOPE, "current")
    plan, paths, payload = _prepare(agent, bundle)
    with _measure_indices(monkeypatch) as counts:
        for _ in range(10000):
            restored = compact_apply.restore_refs_payload(payload, plan, paths, "now")
            assert [row["call_id"] for row in restored["source_refs"]["tool_outputs"]] == ["current"]
            assert restored["source_scope"]["coverage"] == "partial"
    assert counts["glob"] == 0 and counts["opens"] == {str(index): 30000}
    window_bytes = index.stat().st_size - bundle.bundle["tool_index_marks"][str(index)][2]
    assert counts["bytes"][str(index)] <= 10000 * (window_bytes + 3)
    record_property("ten_thousand_windows", {"opens": 30000, "bytes": counts["bytes"][str(index)]})


def test_real_finalization_passes_its_original_archive_receipt(current_turn, monkeypatch):
    from agent_py_agent.agent.agent_core import _finalization_service

    agent, _ = current_turn
    original, observed = _finalization_service.compact_auto_cycle_fields, []

    def observing(*args, **kwargs):
        observed.append(kwargs.get("archive_result"))
        return original(*args, **kwargs)

    monkeypatch.setattr(_finalization_service, "compact_auto_cycle_fields", observing)
    result = agent.run("请记录本轮结束。", save=True)
    assert len(observed) == 1 and isinstance(observed[0], ArchiveRunTurnResult)
    assert observed[0].event_count == result.archive_events and observed[0].write_paths


@pytest.mark.parametrize("current,limit,forced,status", [(10, 20, False, "skipped_below_threshold"),
    (20, 20, False, "needs_user_confirmation"), (1, 20, True, "needs_user_confirmation")])
def test_auto_eligibility_keeps_absolute_and_force_trigger_policy(current_turn, monkeypatch, current, limit, forced, status):
    agent, _ = current_turn
    monkeypatch.setattr(Path, "glob", lambda *_args, **_kwargs: pytest.fail("history discovery"))
    result = run_memory_compact_auto_cycle(agent.home_paths.owner_home_dir, MemoryCompactAutoCycleOptions(
        current_tokens=current, max_context_tokens=100000, trigger_tokens=limit, force_trigger=forced,
        plan_options=_current_plan(agent)))
    assert result["status"] == status and result["trigger"]["forced"] is forced
    assert result["suggestion"]["source_scope"]["mode"] == "current_turn"


@pytest.mark.parametrize("fault", ["missing_id", "missing_hash", "wrong_request"])
def test_invalid_archive_receipt_retains_partial_instead_of_inventing_current_facts(current_turn, monkeypatch, fault):
    agent, _ = current_turn
    scope = {**SCOPE, "request_id": "foreign"} if fault == "wrong_request" else SCOPE
    receipt = archive_run_turn(ArchiveRunTurnParams(agent.home_paths.owner_home_dir, ArchiveTurnContext(
        session_id=agent.config.agent_name, user_prompt="当前需求", response_text="当前结果", backend="fake", **scope)))
    if fault == "missing_id":
        receipt = replace(receipt, event_ids=("", *receipt.event_ids[1:]))
    elif fault == "missing_hash":
        receipt = replace(receipt, content_hashes=("", *receipt.content_hashes[1:]))
    monkeypatch.setattr(Path, "glob", lambda *_args, **_kwargs: pytest.fail("history discovery"))
    plan = build_memory_compact_plan(agent.home_paths.owner_home_dir, _current_plan(agent, archive_result=receipt))
    assert plan["archive"]["receipt_known"] and not plan["archive"]["current_append_complete"]
    assert plan["source_scope"]["coverage"] == "partial" and plan["archive"]["prior_carry"] == "unknown"
    if fault == "wrong_request":
        assert plan["archive"]["events"] == [] and plan["archive"]["event_ids"] == []
