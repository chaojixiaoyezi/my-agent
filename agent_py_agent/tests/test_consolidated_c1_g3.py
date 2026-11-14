"""Wave 3 consolidated tests for test_consolidated_c1_g3.py.

- test_r7_a_w9.py: 自动 Compact 与恢复只消费当前宿主事实，历史规模不扩大普通读取窗口。
- test_r7_w1_generation_guard_contention.py: R7-W1：同进程线程同时建连只等待代次门的进程内互斥，不得误报 generation_busy；EX 排空语义不变。
- test_r7_w2_passive_reads_no_history_scan.py: R7-W2：被动状态/通知读口在索引装配时只按精确 id 回读，索引失败报未知而不扫描全部历史 run。
- test_r7_w3_recovery_report_cache.py: 恢复集合读取只回读当前 id，不得淘汰共享缓存里仍存在的历史 run。
- test_r7_w4_idle_busy_indexed.py: R7-W4：闲置压缩忙态与学习保护只读运行索引的当前集合/本会话谱系，不随 owner 全部历史 run 增长。
- test_r7_w5_retention_symlink_shapes.py: R7-W5：活体形状的 owner 根/工作区成员符号链接不得让 retention 引用保护整轮失败；引用来源内的链接仍失败关闭。
- test_r7_w6_events_backlog_drain.py: 有界事件删除的积压由原分页游标逐拍排空，不等待下一次全库轮转。
"""
import json
import os
import sqlite3
import threading
import time
from collections import Counter
from contextlib import closing, contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.finalization_compact_auto import compact_auto_cycle_fields
from agent_py_agent.agent.agent_core.tool_loop.completion import _delegated_work_facts
from agent_py_agent.agent.capability.skill_learning_protection import learning_protection_for_agent
from agent_py_agent.agent.common import json_io
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.agent_activity import conversation_agent_activity
from agent_py_agent.agent.conversation.authority import CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import compact_operations as idle
from agent_py_agent.agent.gateway_parts import control_service
from agent_py_agent.agent.gateway_parts.request_binding import GatewayTaskBindingWriter
from agent_py_agent.agent.gateway_parts.request_context import GatewayAskRunContext
from agent_py_agent.agent.gateway_parts.request_execution import (
    _gateway_recovered_active_turn_tool_calls,
)
from agent_py_agent.agent.local_storage import LocalStore, maintenance
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
from agent_py_agent.agent.memory_store.retention import apply_owner_retention
from agent_py_agent.agent.runtime_db import recovery_guard as guard
from agent_py_agent.agent.runtime_db.recovery_guard import RuntimeDatabaseDeferred
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.user_space import owner_maintenance as om
from agent_py_agent.agent.user_space.context_bundle import (
    MainContextBundleRequest,
    build_main_context_bundle,
)
from agent_py_agent.tests.memory_compact_support import compact_snapshot, write_compact_fixture
from agent_py_agent.tests.test_conversation_agent_activity import _run
from agent_py_agent.tests.test_memory_retention_wave5 import NOW, old_file, runtime
from agent_py_agent.tests.test_memory_runtime_compact_auto_continuation import (
    CaptureBackend,
    _finalize_context_for_continuation,
)
from agent_py_agent.tests.test_r5db_closeout_tool_index_window import SCOPE, _externalize, _history
from agent_py_agent.tests.test_runtime_guidance import _tool_loop_params
from agent_py_agent.tests.test_subagent_listruns_cache import _selected_manager, _selected_task_file

# ============================================================================
# Source: agent_py_agent/tests/test_r7_a_w9.py
# ============================================================================

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



# ============================================================================
# Source: agent_py_agent/tests/test_r7_w1_generation_guard_contention.py
# ============================================================================

@pytest.fixture
def database(tmp_path):
    path = tmp_path / "runtime.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE facts(value)")
    connection.close()
    yield path
    # 所有共享门都已随连接物理关闭归还时，EX 才能成功。
    with guard.runtime_db_generation_guard(path, exclusive=True):
        pass


def _mutex_for(path: Path):
    key = str(Path(os.path.abspath(path)).with_name(path.name + ".generation.lock"))
    with json_io._JSON_FILE_LOCKS_GUARD:
        handle = json_io._JSON_FILE_LOCKS.get(key)
        if handle is None:
            handle = json_io._PathLock()
            json_io._JSON_FILE_LOCKS[key] = handle
    return handle


def test_shared_admission_waits_for_in_process_mutex_instead_of_busy(database):
    handle = _mutex_for(database)  # 强引用保持弱字典中的同一把锁
    started, results = threading.Event(), []

    def admit():
        started.set()
        try:
            with guard.runtime_db_generation_guard(database):
                results.append("admitted")
        except BaseException as error:  # noqa: BLE001 记录真实失败类型供断言
            results.append(error)

    assert handle.lock.acquire(timeout=5)
    thread = threading.Thread(target=admit)
    try:
        thread.start()
        assert started.wait(5)
        thread.join(0.2)
        assert results == [], f"共享准入不得因进程内互斥竞争立即失败: {results!r}"
    finally:
        handle.lock.release()
    thread.join(5)
    assert not thread.is_alive()
    assert results == ["admitted"]


def test_concurrent_guarded_connects_never_report_generation_busy(database):
    errors, barrier = [], threading.Barrier(12)

    def worker():
        try:
            barrier.wait(5)
            for _ in range(40):
                conn = guard.guarded_runtime_connect(database, lambda: sqlite3.connect(database))
                try:
                    conn.execute("SELECT count(*) FROM facts").fetchone()
                finally:
                    guard.close_runtime_connection(conn)
        except BaseException as error:  # noqa: BLE001 工作线程的异常必须传回测试线程
            errors.append(error)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []


def test_local_store_reads_under_thread_contention_stay_indexed(tmp_path):
    store = LocalStore(tmp_path / "local_store" / "local.db", enable_fts=False)
    errors, barrier = [], threading.Barrier(8)

    def worker():
        try:
            barrier.wait(5)
            for _ in range(25):
                store.list_agent_tree("task-root")
        except BaseException as error:  # noqa: BLE001 任何异常都会让活动面退回历史全扫
            errors.append(type(error).__name__ + ":" + str(error))

    # R7-A-W1 的零等待只读连接不负责创建 WAL 侧车；保留一个真实读者，隔离最后读者关闭时的侧车回收竞争。
    with closing(sqlite3.connect(store.db_path)) as keeper:
        keeper.execute("SELECT count(*) FROM legacy_agent_runs").fetchone()
        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        assert not any(thread.is_alive() for thread in threads)
        assert errors == []


def test_exclusive_recovery_still_refuses_while_a_shared_connection_is_open(database):
    conn = guard.guarded_runtime_connect(database, lambda: sqlite3.connect(database))
    try:
        with pytest.raises(guard.RuntimeDatabaseDeferred, match="generation_busy"):
            with guard.runtime_db_generation_guard(database, exclusive=True):
                pytest.fail("共享连接未关闭时 EX 不能证明排空")
    finally:
        guard.close_runtime_connection(conn)



# ============================================================================
# Source: agent_py_agent/tests/test_r7_w2_passive_reads_no_history_scan.py
# ============================================================================

class _IndexedManager:
    """生产形状：有索引与精确读口；全量读口只记账，证明是否发生历史扫描。"""

    def __init__(self, runs, *, tree_error=None):
        self.runs, self.scans, self.selected = list(runs), [], []
        records = [SimpleNamespace(run_id=run.id, root_task_id=run.root_id, parent_run_id=run.parent_id,
                                   depth=run.depth) for run in self.runs]

        def tree(_root_id):
            if tree_error is not None:
                raise tree_error
            return SimpleNamespace(runs=records)

        self.local_store = SimpleNamespace(list_agent_tree=tree)

    def list_runs_by_ids_report(self, run_ids):
        wanted = list(run_ids)
        self.selected.append(wanted)
        return SimpleNamespace(runs=[run for run in self.runs if run.id in wanted], load_errors=[])

    def list_runs_report(self):
        self.scans.append("list_runs_report")
        return SimpleNamespace(runs=list(self.runs), load_errors=[])

    def list_runs(self):
        self.scans.append("list_runs")
        return list(self.runs)


def _active_store():
    return SimpleNamespace(tasks=SimpleNamespace(active_report=lambda _thread_id: (
        [SimpleNamespace(task_id="task-live", status="active")], [])))


def test_activity_index_failure_reports_unknown_without_history_scan():
    manager = _IndexedManager([_run("child-1")], tree_error=RuntimeDatabaseDeferred("generation_busy"))
    activity = conversation_agent_activity(SimpleNamespace(subagents=manager), _active_store(), "thread-1")
    assert manager.scans == [], "索引读失败不得退回全部历史 run 扫描"
    assert activity.subagents == ()
    assert activity.subagent_projection_ok is False
    assert "subagent_runs_unavailable" in activity.warnings


def test_activity_index_success_reads_exact_children_only():
    manager = _IndexedManager([_run("child-1"), _run("child-2", parent_id="child-1", depth=2)])
    activity = conversation_agent_activity(SimpleNamespace(subagents=manager), _active_store(), "thread-1")
    assert manager.scans == [] and manager.selected == [["child-1"]]
    assert [row["run_id"] for row in activity.subagents] == ["child-1"]


def test_legacy_manager_without_index_keeps_compatibility_scan():
    legacy = SimpleNamespace(list_runs_report=lambda: SimpleNamespace(runs=[_run("child-1")], load_errors=[]))
    activity = conversation_agent_activity(SimpleNamespace(subagents=legacy), _active_store(), "thread-1")
    assert [row["run_id"] for row in activity.subagents] == ["child-1"]


def test_gateway_status_counts_read_exact_request_runs():
    runs = [SimpleNamespace(id="r1", status="RUNNING", failure_type=""),
            SimpleNamespace(id="r2", status="DONE", failure_type=""),
            SimpleNamespace(id="other", status="RUNNING", failure_type="")]
    manager = _IndexedManager([])
    manager.runs = runs
    agent = SimpleNamespace(subagent_run_ids_for_request=lambda _request_id: ["r1", "r2"], subagents=manager)
    counts = control_service._subagent_status(agent, ["gwreq-1"])
    assert manager.scans == [] and manager.selected == [["r1", "r2"]]
    assert (counts.running, counts.done) == (1, 1)


def test_delegated_work_facts_read_exact_request_runs():
    params = _tool_loop_params(root_user_prompt="并行完成后汇总",
                               executed_tools=["create_subagents", "dispatch_subagents"])
    manager = _IndexedManager([])
    manager.runs = [SimpleNamespace(id="run-1", status="DONE"), SimpleNamespace(id="run-2", status="RUNNING"),
                    SimpleNamespace(id="unrelated", status="RUNNING")]
    agent = SimpleNamespace(subagent_run_ids_for_request=lambda task_id: ["run-1", "run-2"] if task_id == "task-1" else [],
                            subagents=manager)
    facts = _delegated_work_facts(agent, params)
    assert manager.scans == [] and manager.selected == [["run-1", "run-2"]]
    assert facts["status_counts"] == {"DONE": 1, "RUNNING": 1}


@pytest.mark.parametrize("mode", ["none", "error"])
def test_unavailable_exact_reader_never_enables_a_history_fallback(mode):
    manager = _IndexedManager([_run("child-1")])

    def unavailable(run_ids):
        if mode == "error":
            raise RuntimeDatabaseDeferred("generation_busy")
        return None

    manager.list_runs_by_ids_report = unavailable
    agent = SimpleNamespace(subagents=manager, subagent_run_ids_for_request=lambda _key: ["child-1"])
    activity = conversation_agent_activity(agent, _active_store(), "thread-1")
    assert activity.subagent_projection_ok is False
    assert "subagent_runs_unavailable" in activity.warnings
    facts = _delegated_work_facts(agent, _tool_loop_params(root_user_prompt="汇总当前工作",
        executed_tools=["create_subagents", "dispatch_subagents"]))
    assert facts == {} and manager.scans == []



# ============================================================================
# Source: agent_py_agent/tests/test_r7_w3_recovery_report_cache.py
# ============================================================================

def _indexed_manager(tmp_path, total=20, running=2):
    manager = _selected_manager(tmp_path)
    index = manager.persistence.run_index
    with index.connect() as db:
        for number in range(total):
            run_id = f"run-{number}"
            _selected_task_file(manager, run_id, "RUNNING" if number < running else "DONE")
            index.upsert(manager.load(run_id), connection=db)
    return manager


def _count_loads(monkeypatch, service):
    loads, original = [], service.load

    def observed(run_id):
        loads.append(run_id)
        return original(run_id)

    monkeypatch.setattr(service, "load", observed)
    return loads


def test_recovery_read_keeps_history_cache_warm(tmp_path, monkeypatch):
    manager = _indexed_manager(tmp_path)
    service = manager.persistence
    assert not manager.list_runs_report().load_errors
    warm = set(service._run_cache)
    assert len(warm) == 20
    report = manager.list_recovery_runs_report()
    assert not report.load_errors and {run.id for run in report.runs} == {"run-0", "run-1"}
    assert set(service._run_cache) == warm
    loads = _count_loads(monkeypatch, service)
    assert not manager.list_runs_report().load_errors
    assert loads == [], "恢复读之后全量读应命中缓存，不能重新解析历史"


def test_recovery_index_error_does_not_flush_cache(tmp_path, monkeypatch):
    manager = _indexed_manager(tmp_path)
    service = manager.persistence
    assert not manager.list_runs_report().load_errors
    warm = set(service._run_cache)

    def unavailable(**_kwargs):
        raise OSError("index unavailable")

    monkeypatch.setattr(service.run_index, "recovery_ids", unavailable)
    report = manager.list_recovery_runs_report()
    assert report.load_errors and not report.runs
    assert set(service._run_cache) == warm


def test_deleted_run_is_still_evicted_by_full_listing(tmp_path):
    manager = _indexed_manager(tmp_path, total=4, running=1)
    service = manager.persistence
    assert not manager.list_runs_report().load_errors
    (manager.workspace / "run-3" / "task.json").unlink()
    manager.list_recovery_runs_report()
    assert not manager.list_runs_report().load_errors
    assert "run-3" not in service._run_cache



# ============================================================================
# Source: agent_py_agent/tests/test_r7_w4_idle_busy_indexed.py
# ============================================================================

def _write_run(manager, run_id, status, *, root, thread_id, attributes=None):
    path = manager.workspace / run_id / "task.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "id": run_id, "goal": "fixture", "thought": "", "plan": [], "status": status,
        "root_id": root, "parent_id": root, "created_at": 1.0, "updated_at": 2.0,
        "attributes": {"conversation_thread_id": thread_id, **(attributes or {})},
    }), encoding="utf-8")
    old = time.time_ns() - 10_000_000_000
    os.utime(path, ns=(old, old))
    manager.persistence.run_index.upsert(manager.load(run_id))
    return path


@pytest.fixture
def case(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / "conversations")
    row = replace(store.threads.create(canonical_user_id="user"), task_ids=("task-a",))
    manager = _selected_manager(tmp_path)
    for number in range(60):
        _write_run(manager, f"history-{number}", "DONE", root="task-old", thread_id="thread-other")
    loaded, original = [], manager.persistence._cached_run_ref

    def observed(run_id, path):
        loaded.append(run_id)
        return original(run_id, path)

    monkeypatch.setattr(manager.persistence, "_cached_run_ref", observed)
    monkeypatch.setattr(idle, "list_background_processes", lambda *_, **__: {"projection_ok": True, "items": []})
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=tmp_path), subagents=manager)
    return SimpleNamespace(store=store, row=row, manager=manager, agent=agent, loaded=loaded)


def test_terminal_blocked_run_of_this_thread_stays_busy_without_history_scan(case):
    _write_run(case.manager, "blocked-1", "BLOCKED", root="task-a", thread_id=case.row.thread_id)
    case.loaded.clear()
    assert idle.idle_compact_busy_state(case.agent, case.store, case.row) == "subagent_blocked"
    assert set(case.loaded) == {"blocked-1"}, "只应回读当前集合与本会话谱系，不扫描历史 run"


def test_active_run_of_this_thread_is_busy(case):
    _write_run(case.manager, "running-1", "RUNNING", root="task-a", thread_id=case.row.thread_id)
    case.loaded.clear()
    assert idle.idle_compact_busy_state(case.agent, case.store, case.row) == "subagent_active"
    assert set(case.loaded) == {"running-1"}


def test_other_threads_history_does_not_block_and_is_not_read(case):
    case.loaded.clear()
    assert idle.idle_compact_busy_state(case.agent, case.store, case.row) == ""
    assert case.loaded == []


def test_indexed_run_with_corrupt_canonical_fails_closed(case):
    path = _write_run(case.manager, "running-1", "RUNNING", root="task-a", thread_id=case.row.thread_id)
    path.write_text("{invalid json", encoding="utf-8")
    assert idle.idle_compact_busy_state(case.agent, case.store, case.row) == "activity_unknown"


def test_learning_protection_reads_current_set_only(case, tmp_path):
    _write_run(case.manager, "running-1", "RUNNING", root="task-a", thread_id=case.row.thread_id,
               attributes={"allowed_skills": ["owner:child"]})
    case.loaded.clear()
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=tmp_path),
                            conversation_store=None, scheduler_repository=None, subagents=case.manager)
    learning_protection_for_agent(agent)
    assert set(case.loaded) == {"running-1"}



# ============================================================================
# Source: agent_py_agent/tests/test_r7_w5_retention_symlink_shapes.py
# ============================================================================

# LLM: 走生产默认预算入口并按 scan_partial 续调，错误码按全部分片累计。
# 函数用途: 运行 owner retention 直到本周期结束，返回末片回执与累计错误码。
def _apply_until_settled(home):
    codes = []
    for _ in range(64):
        report = apply_owner_retention(home, now=NOW)
        codes.extend(error.error_code for error in report.errors)
        if not report.scan_partial:
            return report, codes
    pytest.fail("retention apply never settled")


# LLM: 两种形状均取自 PID 70255 owner 树：根目录 link_vol15 与工作区内部的散落链接。
# 函数用途: 证明成员枚举里的链接不再把整轮 pins 置为不完整。
@pytest.mark.parametrize("shape", ["owner_root", "workspace_member"])
def test_live_symlink_shapes_keep_retention_pins_complete(tmp_path, shape):
    home, _candidates, _service = runtime(tmp_path)
    owner = Path(home.owner_home_dir)
    target = owner / "tasks/2026-11-07/work/encyc"
    target.mkdir(parents=True)
    workspace = Path(home.owner_workspace_dir) / "runtime/workspaces/main-real"
    workspace.mkdir(parents=True)
    link = owner / "link_vol15" if shape == "owner_root" else workspace / "stray-link"
    link.symlink_to(target)
    report, codes = _apply_until_settled(home)
    assert "MEMORY_RETENTION_PIN_SOURCE_UNREADABLE" not in codes
    assert not report.scan_partial


# LLM: 引用来源文件本身是链接时不能被当作缺失或空集；保护必须失败关闭。
# 函数用途: 守住成员枚举放宽不外溢到 pin 来源读取。
def test_symlinked_pin_source_still_fails_closed(tmp_path):
    home, _candidates, _service = runtime(tmp_path)
    real = old_file(Path(tmp_path) / "outside.jsonl", '{"source_refs": ["evidence/a"]}\n')
    runs = Path(home.owner_memory_curator_runs_dir)
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "pins.jsonl").symlink_to(real)
    _report, codes = _apply_until_settled(home)
    assert "MEMORY_RETENTION_PIN_SOURCE_UNREADABLE" in codes



# ============================================================================
# Source: agent_py_agent/tests/test_r7_w6_events_backlog_drain.py
# ============================================================================

@pytest.mark.parametrize("event_count", [40, 39])
def test_capped_event_steps_keep_store_pending_until_backlog_drained(tmp_path, monkeypatch, event_count):
    monkeypatch.setattr(maintenance, "STEADY_STATE_EVENTS_KEEP_LAST", 10)
    monkeypatch.setattr(maintenance, "STEADY_STATE_EVENTS_MAX_DELETE", 5)
    store = LocalStore(tmp_path / "local_store" / "local.db")
    payload = {"padding": "x" * (128 * 1024)}
    for _ in range(event_count):
        store.record_event("backlog", payload=payload)
    assert store.stats()["event_count"] == event_count
    # 活动 JSONL 不被分步裁剪，足够大时每拍都沿真实有界 SQL 路径继续。
    assert store.events_path.stat().st_size > 4 * 1024 * 1024
    now, cursor, deleted = time.time(), None, 0
    reference = str(store.db_path.relative_to(tmp_path))

    for tick in range(20):
        selected, position = om._maintenance_store_page([store.db_path], tmp_path, cursor)
        assert selected == [store.db_path]
        result = om._maintain_event_store(store.root, now=now)
        assert result["executed"] is True
        if tick == 0:
            assert result["deleted_db"] == 5
            assert result["partial"] is True
        assert 0 <= result["deleted_db"] <= 5
        deleted += result["deleted_db"]
        page = om._maintenance_store_position(selected, tmp_path, [result], position)
        if result.get("partial"):
            assert result["selected"] == 5
            assert page["partial"] is True
            assert position["pending"] == [reference]
            cursor = page["cursor"]
        else:
            assert result["selected"] < 5
            assert not page.get("partial")
            assert position["pending"] == []
            break
    else:
        pytest.fail("events backlog did not drain within twenty bounded steps")

    assert deleted == event_count - 10
    assert store.stats()["event_count"] == 10
