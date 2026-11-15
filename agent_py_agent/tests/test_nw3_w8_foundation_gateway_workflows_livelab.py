from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent_py_agent.agent.gateway_parts.background_resource_report as bg_resource_report
from agent_py_agent.agent.agent_core.orchestration.dispatch import capability_auto_sweep
from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.workflow_store import (
    WorkflowDefinition,
    WorkflowStepSpec,
    WorkflowStore,
)
from agent_py_agent.agent.contracts.tool_approval import (
    ToolApprovalRequest,
    build_tool_approval_request,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import business_api_v13_readers as readers_mod
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.agent.gateway_parts.paths import claimed_request_chunk_path, gateway_paths
from agent_py_agent.agent.gateway_parts.queue_service import ensure_gateway_folders
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents.manager import SubAgentManager, SubagentRunIndexUnavailable
from agent_py_agent.agent.tooling.process_session_records import PROCESS_SESSION_SCHEMA
from agent_py_agent.agent.tooling.process_session_store import ProcessSessionStore
from agent_py_agent.agent.workflows.models import WorkflowRunRecord
from agent_py_agent.agent.workflows.reference_sources import WorkflowReferenceReader
from agent_py_agent.agent.workflows.run_list_index import RunListIndex
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore
from scripts.live_lab import main_agent_compact_stress as compact_stress_mod
from scripts.live_lab.compact_observations import (
    collect_compaction_observations,
    compute_pre_post_compaction_cache_ratios,
)
from scripts.live_lab.session import LabSessionManager


def _create_empty_workflow_index(owner_home: Path) -> sqlite3.Connection:
    index_db = owner_home / "data" / "workflow_run_index.sqlite3"
    index_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(index_db)
    conn.execute("CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY, status TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS pending(run_id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE IF NOT EXISTS legacy(run_id TEXT PRIMARY KEY)")
    return conn


def _write_workflow_run_json(
    owner_home: Path,
    run_id: str,
    *,
    status: str = "succeeded",
    ended_at: float | None = None,
) -> Path:
    wf_dir = owner_home / "data" / "workflow_runs" / run_id
    wf_dir.mkdir(parents=True, exist_ok=True)
    doc: dict[str, object] = {
        "schema_version": "workflow_run.v1",
        "workflow_run_id": run_id,
        "status": status,
    }
    if ended_at is not None:
        doc["ended_at"] = ended_at
    (wf_dir / "run.json").write_text(json.dumps(doc), encoding="utf-8")
    return wf_dir


def _seed_bg_process(
    store_root: Path,
    owner_home: Path,
    *,
    session_id: str,
    thread_id: str,
    root_task_id: str,
    run_id: str,
    started_at: float,
) -> None:
    store = ProcessSessionStore(store_root)
    record: dict[str, object] = {
        "schema": PROCESS_SESSION_SCHEMA,
        "session_id": session_id,
        "command": "sleep 10",
        "cwd": str(owner_home),
        "output_file": str(owner_home / f"{session_id}.log"),
        "host_state_file": "",
        "completion_notice_id": "",
        "launcher_pid": 999999,
        "launcher_birth_token": "birth-test-1",
        "pid": 999999,
        "pid_birth_token": "birth-test-1",
        "child_pid": 999998,
        "child_pid_birth_token": "birth-test-2",
        "child_launch_started": True,
        "handoff_confirmed": True,
        "status": "running",
        "stop_requested": False,
        "retain_until_consumed": True,
        "revision": 0,
        "reserved_at": started_at,
        "started_at": started_at,
        "finished_at": None,
        "exit_code": None,
        "termination": {},
        "completion_target": {},
        "activation_scope": None,
        "access_scope": {
            "owner_id": "owner-test",
            "conversation_id": thread_id,
            "owner_home": str(owner_home.resolve(strict=False)),
        },
        "execution_scope": {
            "owner_home": str(owner_home.resolve(strict=False)),
            "thread_id": thread_id,
            "root_task_id": root_task_id,
            "run_id": run_id,
            "attempt_id": "att-1",
        },
    }
    store.write(record)


def test_f04_indexed_workflow_snapshot_avoids_full_scandir_and_resolves_beyond_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_home = tmp_path / "owner_home"
    store_root = tmp_path / "bg_store"
    runs_dir = owner_home / "data" / "workflow_runs"
    base_ts = 1_700_000_000.0
    now_ts = base_ts + 2_000.0

    with _create_empty_workflow_index(owner_home) as conn:
        for idx in range(400):
            wf_id = f"wf-{idx:03d}"
            conn.execute("INSERT INTO runs(run_id, status) VALUES(?, 'succeeded')", (wf_id,))
            _write_workflow_run_json(owner_home, wf_id, status="succeeded", ended_at=base_ts + idx)
        conn.execute("INSERT INTO runs(run_id, status) VALUES('wf-active-401', 'running')")
        _write_workflow_run_json(owner_home, "wf-active-401", status="running", ended_at=None)
        conn.commit()

    # 仅 bg-* 关联超过 max_settled_dir_scan=256 的运行 wf-350 与 root_task_id=wf-360。
    _seed_bg_process(
        store_root,
        owner_home,
        session_id="bg-settled-350",
        thread_id="thread-350",
        root_task_id="task-350",
        run_id="wf-350",
        started_at=base_ts - 100.0,
    )
    _seed_bg_process(
        store_root,
        owner_home,
        session_id="bg-settled-root-360",
        thread_id="thread-360",
        root_task_id="wf-360",
        run_id="sub-360",
        started_at=base_ts - 100.0,
    )
    _seed_bg_process(
        store_root,
        owner_home,
        session_id="bg-active-401",
        thread_id="thread-401",
        root_task_id="task-401",
        run_id="wf-active-401",
        started_at=base_ts,
    )

    monkeypatch.setattr(
        bg_resource_report,
        "_stop_settled_batch",
        lambda **_kwargs: [],
    )

    scandir_targets: list[Path] = []
    real_scandir = os.scandir

    def _recording_scandir(path=None, *args, **kwargs):
        if path is not None:
            scandir_targets.append(Path(path).resolve(strict=False))
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(bg_resource_report.os, "scandir", _recording_scandir)

    report = bg_resource_report.freeze_settled_leftover_processes(
        owner_home,
        min_settled_age_seconds=300,
        now_ts=now_ts,
        store_root=store_root,
    )

    assert runs_dir.resolve(strict=False) not in scandir_targets
    settled_by_id = {row["session_id"]: row for row in report["settled_processes"]}
    assert set(settled_by_id) == {"bg-settled-350", "bg-settled-root-360"}
    assert settled_by_id["bg-settled-350"]["reason"] != "settled_at_unknown"
    assert settled_by_id["bg-settled-350"]["settled_age_known"] is True
    assert settled_by_id["bg-settled-350"]["settled_at"] == pytest.approx(base_ts + 350.0)
    assert settled_by_id["bg-settled-root-360"]["reason"] != "settled_at_unknown"
    assert settled_by_id["bg-settled-root-360"]["settled_age_known"] is True
    assert settled_by_id["bg-settled-root-360"]["settled_at"] == pytest.approx(base_ts + 360.0)
    assert [row["session_id"] for row in report["active_processes"]] == ["bg-active-401"]

    # 3 态退化保真：跳过范围内的符号链接运行目录必须判 unknown，不能误放行。
    symlink_target = tmp_path / "external-wf-370"
    symlink_target.mkdir()
    wf_370 = runs_dir / "wf-370"
    (wf_370 / "run.json").unlink()
    wf_370.rmdir()
    wf_370.symlink_to(symlink_target)
    _seed_bg_process(
        store_root,
        owner_home,
        session_id="bg-symlink-370",
        thread_id="thread-370",
        root_task_id="task-370",
        run_id="wf-370",
        started_at=base_ts - 100.0,
    )
    report_sym = bg_resource_report.freeze_settled_leftover_processes(
        owner_home,
        min_settled_age_seconds=300,
        now_ts=now_ts,
        store_root=store_root,
    )
    unknown_by_id = {row["session_id"]: row for row in report_sym["unknown_processes"]}
    assert unknown_by_id["bg-symlink-370"]["reason"] == "workflow_run_dir_symlink"

    # 缺失索引时仍回退到目录扫描。
    (owner_home / "data" / "workflow_run_index.sqlite3").unlink()
    wf_370.unlink()
    _write_workflow_run_json(owner_home, "wf-370", status="succeeded", ended_at=base_ts + 370.0)
    scandir_targets.clear()
    report_fallback = bg_resource_report.freeze_settled_leftover_processes(
        owner_home,
        min_settled_age_seconds=300,
        now_ts=now_ts,
        store_root=store_root,
    )
    assert runs_dir.resolve(strict=False) in scandir_targets
    assert "bg-settled-350" in {row["session_id"] for row in report_fallback["settled_processes"]}


def test_f05_reference_sources_backfills_legacy_lockfree_and_keeps_pending_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wf_store = WorkflowStore(tmp_path)
    wf_store.save_workflow(
        WorkflowDefinition(
            AssetDescriptor("wf-target", "workflow", "wf-target"),
            (WorkflowStepSpec("step-1", "step-1", "condition"),),
        )
    )
    run_store = WorkflowRunStore(tmp_path)
    index = RunListIndex(tmp_path)

    # 1. open_ref_complete=1 但 legacy 队列非空 -> 必须触发无锁 backfill 并解析真实阻塞引用。
    active_run = WorkflowRunRecord(
        workflow_run_id="legacy-active-1",
        workflow_id="wf-target",
        owner_id="main",
        trigger_kind="manual",
        status="running",
        started_at=100.0,
        ended_at=0.0,
        duration_ms=0.0,
    )
    run_store.create_run(active_run)
    with index.connection() as conn:
        conn.execute("UPDATE open_ref_scan SET complete = 1 WHERE singleton = 1")
        conn.execute("DELETE FROM runs WHERE run_id = ?", ("legacy-active-1",))
        conn.execute("DELETE FROM open_ref_runs WHERE run_id = ?", ("legacy-active-1",))
        conn.execute("INSERT OR IGNORE INTO legacy(run_id) VALUES(?)", ("legacy-active-1",))

    lock_calls: list[str] = []
    real_run_lock = WorkflowRunStore._lock

    def guarded_run_lock(self, workflow_run_id, *args, **kwargs):
        lock_calls.append(str(workflow_run_id))
        return real_run_lock(self, workflow_run_id, *args, **kwargs)

    monkeypatch.setattr(WorkflowRunStore, "_lock", guarded_run_lock)
    res_legacy = WorkflowReferenceReader(tmp_path).read_references("wf-target")
    assert lock_calls == []
    assert res_legacy["coverage"] == "complete"
    assert any(b["reason_code"] == "ACTIVE_WORKFLOW_RUN" for b in res_legacy["blockers"])
    with index.connection() as conn:
        assert conn.execute("SELECT run_id FROM legacy").fetchall() == []

    # 2. open_ref_complete=1 且 pending 队列非空 -> 不得在读口调用 replay 抢 run_store._lock，必须 fail-closed 返回 unknown。
    index.mark_pending("pending-unreplayed-1")
    res_pending = WorkflowReferenceReader(tmp_path).read_references("wf-clean-target")
    assert lock_calls == []
    assert res_pending["coverage"] == "unknown"
    assert any(b["reason_code"] == "WORKFLOW_RUN_REFERENCE_SOURCE_INCOMPLETE" for b in res_pending["blockers"])
    assert index.pending_ids(10) == ["pending-unreplayed-1"]
    index.ack_pending("pending-unreplayed-1")

    # pending 清空后恢复 complete。
    res_clear = WorkflowReferenceReader(tmp_path).read_references("wf-clean-target")
    assert res_clear["coverage"] == "complete"
    assert res_clear["blockers"] == []

    # 3. 不可吸收/停滞的 legacy 行不空转 32 批，单批即提前退出并 fail-closed。
    backfill_calls = 0

    def noop_backfill(self, store, batch_size=32):
        nonlocal backfill_calls
        backfill_calls += 1

    with index.connection() as conn:
        conn.execute("INSERT OR IGNORE INTO legacy(run_id) VALUES(?)", ("stuck-legacy-1",))
    monkeypatch.setattr(RunListIndex, "backfill", noop_backfill)
    res_stuck = WorkflowReferenceReader(tmp_path).read_references("wf-clean-target")
    assert backfill_calls == 1
    assert res_stuck["coverage"] == "unknown"
    assert any(b["reason_code"] == "WORKFLOW_RUN_REFERENCE_SOURCE_INCOMPLETE" for b in res_stuck["blockers"])


def test_f06_native_approval_reader_enforces_inner_vs_outer_request_id_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home-f06"
    ws = tmp_path / "ws-f06"
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path / "os-home-f06"))
    config = AgentConfig(
        model_backend="echo",
        my_agent_home=str(home),
        memory_path="memory.jsonl",
        execution_mode="local_unmanaged",
        enable_tools=True,
        business_spaces_enabled=True,
    )
    agent = SimpleAgent(config, ws)
    paths = gateway_paths(agent)
    ensure_gateway_folders(paths)
    store = agent.conversation_store

    store.threads.get_or_create(
        {
            "channel": "chat",
            "channel_conversation_id": "conv-f06",
            "channel_user_id": "local-user",
            "canonical_user_id": "local-user",
            "owner_id": agent.home_paths.owner_id,
            "owner_home": str(agent.home_paths.owner_home_dir),
            "title": "f06-thread",
        }
    )

    now = time.time()
    req_outer_id = "req-outer-f06"
    req_path = paths.processing / f"{req_outer_id}.json"
    write_json_file_atomic(
        req_path,
        {
            "id": req_outer_id,
            "kind": "ask",
            "status": "processing",
            "turn_phase": "open",
            "cancel_requested": False,
            "lease_started_at": now - 5.0,
            "user_id": "local-user",
            "conversation": {
                "channel": "chat",
                "channel_conversation_id": "conv-f06",
                "channel_user_id": "local-user",
                "canonical_user_id": "local-user",
            },
            "host_request_owner.v1": {
                "provider": "local",
                "owner_kind": "main",
                "owner_id": "main",
            },
        },
    )

    def _make_req(inner_req_id: str, perm_call_id: str) -> ToolApprovalRequest:
        call = SimpleNamespace(
            call_id=perm_call_id,
            tool_name="run_command",
            run_id=f"run-{inner_req_id}",
            operation_id=f"op-{inner_req_id}",
            idempotency_key=f"idem-{inner_req_id}-0001",
            args_hash=f"hash-{inner_req_id}",
            turn_id=f"turn-{inner_req_id}",
        )
        raw = build_tool_approval_request(
            call,
            request_id=inner_req_id,
            round_number=1,
            call_index=0,
            description=f"action for {inner_req_id}",
        )
        return ToolApprovalRequest(
            permission_id=raw.permission_id,
            request_id=raw.request_id,
            tool_name=raw.tool_name,
            round=raw.round,
            call_index=raw.call_index,
            title=raw.title,
            description=raw.description,
            binding=raw.binding,
            options=raw.options,
            requested_at=now,
            expires_at=now + 600.0,
        )

    mismatched_req = _make_req("req-other-hijack", "call-mismatch")
    matching_req = _make_req(req_outer_id, "call-match")
    chunk_path = claimed_request_chunk_path(req_path, req_outer_id)
    events = [
        {"kind": "permission_requested", "t": now, "permission": mismatched_req.to_dict()},
        {"kind": "permission_requested", "t": now, "permission": matching_req.to_dict()},
    ]
    chunk_path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")

    readers = readers_mod.native_approval_readers(agent, paths, agent.home_paths.owner_id, store)
    page = readers["approvals"]({})
    assert [row.value["approval_id"] for row in page.records] == [matching_req.permission_id]

    detail_mismatch = readers["approvals.get"]({"approval_id": mismatched_req.permission_id})
    assert len(detail_mismatch.records) == 0

    detail_match = readers["approvals.get"]({"approval_id": matching_req.permission_id})
    assert [row.value["approval"]["approval_id"] for row in detail_match.records] == [matching_req.permission_id]


def test_f12_capability_auto_sweep_sanitizes_and_bounds_load_error_summary() -> None:
    report_errors = [
        {
            "error_code": "SQLITE_CORRUPT",
            "error_type": "DatabaseError",
            "path": "/Users/alice/.my-agent/subagent_run_index.sqlite3",
            "message": "Authorization: Bearer sk-proj-secret123 failed",
        },
        {
            "error_code": "sk-invalid-secret",
            "error_type": "PermissionError",
            "message": "denied /private/secret/path",
        },
        {
            "error_type": "TimeoutError",
        },
        {
            "error_code": "/Users/alice/bad_code",
            "error_type": "OSError",
        },
        "/tmp/raw-secret-path Bearer topsecret",
    ]
    class _StubManager(SubAgentManager):
        def __init__(self) -> None:
            pass

        def list_recovery_runs_report(self):
            return SimpleNamespace(runs=[], load_errors=report_errors)

        def list_current_runs_report(self, **_kwargs):
            return SimpleNamespace(runs=[], load_errors=report_errors)

    stub_manager = _StubManager()

    with pytest.raises(SubagentRunIndexUnavailable) as exc_info:
        capability_auto_sweep._select_supervision_recovery_runs(stub_manager)

    err = exc_info.value
    msg = str(err)
    assert msg.startswith("subagent recovery index unavailable")
    assert err.load_errors == report_errors
    assert err.load_error_summary["count"] == 5
    assert len(err.load_error_summary["error_types"]) == 3
    assert err.load_error_summary["truncated"] is True
    assert err.load_error_summary["error_types"] == [
        "SQLITE_CORRUPT:DatabaseError",
        "PermissionError",
        "TimeoutError",
    ]
    serialized = json.dumps({"msg": msg, "summary": err.load_error_summary})
    for forbidden in ("/Users/", "/private/", "/tmp/", "sk-", "Bearer", "topsecret", ".sqlite3"):
        assert forbidden not in serialized

    with pytest.raises(SubagentRunIndexUnavailable) as cur_info:
        capability_auto_sweep._select_supervision_current_runs(stub_manager)
    assert str(cur_info.value).startswith("subagent current index unavailable")
    assert cur_info.value.load_error_summary == err.load_error_summary


def test_f09_mid_turn_compaction_partitions_pre_and_post_calls() -> None:
    # Case 1: Boundary turn containing pre, compact_summary, and post calls.
    turn_with_calls = {
        "turn": "turn_2",
        "turn_index": 1,
        "started_at": 200.0,
        "finished_at": 260.0,
        "compaction_occurred": True,
        "calls": [
            {"created_at": 205.0, "purpose": "main", "input_tokens": 1000, "cached_tokens": 900, "call_count": 1},
            {"created_at": 210.0, "purpose": "main", "input_tokens": 1000, "cached_tokens": 900, "call_count": 1},
            {"created_at": 230.0, "purpose": "conversation_compact_summary", "input_tokens": 500, "cached_tokens": 0, "call_count": 1},
            {"created_at": 235.0, "purpose": "main", "input_tokens": 1000, "cached_tokens": 200, "call_count": 1},
            {"created_at": 250.0, "purpose": "main", "input_tokens": 1000, "cached_tokens": 900, "call_count": 1},
        ],
    }
    ratios_embedded = compute_pre_post_compaction_cache_ratios(
        [
            {"turn": "turn_1", "started_at": 100.0, "finished_at": 150.0, "input_tokens": 1000, "cached_tokens": 900, "call_count": 1},
            turn_with_calls,
        ],
        compaction_turn_indices=[1],
        compaction_timestamps=[230.0],
    )
    assert ratios_embedded["pre_compaction"]["call_count"] == 3
    assert ratios_embedded["pre_compaction"]["observed_cached_ratio"] == pytest.approx(0.9)
    assert ratios_embedded["first_post_compaction"]["call_count"] == 1
    assert ratios_embedded["first_post_compaction"]["observed_cached_ratio"] == pytest.approx(0.2)
    assert ratios_embedded["later_post_compaction"]["call_count"] == 1
    assert ratios_embedded["later_post_compaction"]["observed_cached_ratio"] == pytest.approx(0.9)

    # Case 2: Single coarse turn + external fine-grained usage_rows spanning compaction boundary.
    coarse_turn = {
        "turn": "turn_1",
        "turn_index": 0,
        "started_at": 100.0,
        "finished_at": 200.0,
        "call_count": 4,
        "paired_cache_calls": 4,
        "paired_input_tokens": 4000,
        "paired_cached_tokens": 2900,
        "compaction_occurred": True,
    }
    ledger_rows = [
        {"created_at": 110.0, "purpose": "main", "input_tokens": 1000, "cached_tokens": 900, "call_count": 1},
        {"created_at": 120.0, "purpose": "main", "input_tokens": 1000, "cached_tokens": 900, "call_count": 1},
        {"created_at": 160.0, "purpose": "main", "input_tokens": 1000, "cached_tokens": 200, "call_count": 1},
        {"created_at": 180.0, "purpose": "main", "input_tokens": 1000, "cached_tokens": 900, "call_count": 1},
    ]
    ratios_single = compute_pre_post_compaction_cache_ratios(
        [coarse_turn],
        compaction_turn_indices=[0],
        compaction_timestamps=[150.0],
        usage_rows=ledger_rows,
    )
    assert ratios_single["pre_compaction"]["call_count"] == 2
    assert ratios_single["pre_compaction"]["observed_cached_ratio"] == pytest.approx(0.9)
    assert ratios_single["first_post_compaction"]["call_count"] == 1
    assert ratios_single["first_post_compaction"]["observed_cached_ratio"] == pytest.approx(0.2)
    assert ratios_single["later_post_compaction"]["call_count"] == 1
    assert ratios_single["later_post_compaction"]["observed_cached_ratio"] == pytest.approx(0.9)


def test_f15_compact_stress_wires_turn_markers_and_usage_ledger_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_src = tmp_path / "agent_config.yaml"
    cfg_src.write_text("model_backend: echo\n", encoding="utf-8")
    args = SimpleNamespace(
        config=str(cfg_src),
        runs_dir=str(tmp_path / "runs"),
        run_id="f15-stress",
        real_llm=False,
        count=1,
        timeout=60,
    )
    session = LabSessionManager(args)
    session.setup()
    facts = compact_stress_mod._stress_facts()
    monkeypatch.setattr(compact_stress_mod, "_stress_size_mb", lambda: 1)

    home_ctx = collect_compaction_observations(config_path=session.config_path)["home_context"]
    compact_dir = Path(home_ctx["owner_compact_dir"]) / "conversations"
    threads_dir = Path(home_ctx["threads_dir"])
    usage_dir = Path(home_ctx["model_usage_dir"])
    compact_dir.mkdir(parents=True, exist_ok=True)
    threads_dir.mkdir(parents=True, exist_ok=True)
    usage_dir.mkdir(parents=True, exist_ok=True)

    captured: dict[str, object] = {}

    class _FakeLab:
        config_path = session.config_path
        fixture_root = session.fixture_root
        run_root = session.run_root
        responses_dir = session.responses_dir
        turn_usage_records: list[dict[str, object]] = []

        def __init__(self) -> None:
            self.args = args
            self.turn_markers = [{"turn": "turn_1", "turn_index": 0, "started_at": 500.0, "finished_at": 600.0}]

        def section(self, _title: str) -> None:
            return None

        def log(self, _msg: str) -> None:
            return None

        def record_prompt(self, _case: str, _prompt: str) -> None:
            return None

        def agent_command(self, *parts: str) -> list[str]:
            return ["echo", *parts]

        def record_case_telemetry(self, _name: str, telemetry_data: dict[str, object]) -> None:
            captured["telemetry"] = telemetry_data

        def run_command(self, _cmd, **_kwargs):
            report_path = session.fixture_root / "lab_outputs" / "compact-stress" / "report.md"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_text = "\n".join(
                f"{f.section:03d} {f.city} 最终决定 {f.decision} {f.risk} {f.checkpoint}"
                for f in facts
            ) + ("\n" + ("补充现场证据记录。\n" * 200))
            report_path.write_text(report_text, encoding="utf-8")

            # Seed 5 committed checkpoints with cycle 1 created at t=550.0.
            prev_id = ""
            rows = []
            for gen in range(1, 6):
                cid = f"compact-v3-{gen}"
                rows.append(
                    {
                        "schema": "conversation_compact_checkpoint.v3",
                        "thread_id": "stress-f15",
                        "checkpoint_id": cid,
                        "previous_checkpoint_id": prev_id,
                        "generation": gen,
                        "mode": "independent_summary",
                        "pre_tokens": 100000,
                        "post_tokens": 10000,
                        "commit_authority": "conversation_thread.compact_checkpoint_id",
                        "created_at": 550.0 + (gen - 1) * 5.0,
                    }
                )
                prev_id = cid
            (compact_dir / "stress-f15.jsonl").write_text(
                "\n".join(json.dumps(r) for r in rows) + "\n",
                encoding="utf-8",
            )
            (threads_dir / "stress-f15.json").write_text(
                json.dumps(
                    {
                        "thread_id": "stress-f15",
                        "compact_generation": 5,
                        "compact_checkpoint_id": prev_id,
                    }
                ),
                encoding="utf-8",
            )

            # Seed canonical usage events in home_ctx model_usage_dir straddling t=550.0.
            def _usage_row(idx: int, ts: float, inp: int, cached: int, *, purpose: str = "main") -> str:
                return json.dumps(
                    {
                        "schema_version": "thread_model_usage_event.v1",
                        "event_id": f"evt-{idx}",
                        "thread_id": "stress-f15",
                        "request_id": f"req-{idx}",
                        "run_id": f"run-{idx}",
                        "task_id": "",
                        "source": purpose,
                        "purpose": purpose,
                        "created_at": ts,
                        "model_calls": {
                            "schema": "model_call_summary.v1",
                            "usage_breakdown": {
                                "provider": {
                                    "call_count": 1,
                                    "input_tokens": inp,
                                    "input_tokens_reported_call_count": 1,
                                    "output_tokens": 100,
                                    "output_tokens_reported_call_count": 1,
                                    "cache_read_input_tokens": cached,
                                    "cache_read_input_tokens_reported_call_count": 1,
                                }
                            },
                        },
                    }
                )

            (usage_dir / "stress-f15.jsonl").write_text(
                "\n".join(
                    [
                        _usage_row(1, 510.0, 1000, 900),
                        _usage_row(2, 520.0, 1000, 900),
                        _usage_row(3, 550.0, 500, 0, purpose="conversation_compact_summary"),
                        _usage_row(4, 560.0, 1000, 200),
                        _usage_row(5, 580.0, 1000, 900),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            return SimpleNamespace(stdout="", stderr="")

    compact_stress_mod.case_main_compact_stress_long_read(_FakeLab())

    obs = captured["telemetry"]
    assert len(obs["turn_usage"]) == 1
    assert obs["turn_usage"][0]["call_count"] == 5
    phases = obs["cache_phases"]
    assert phases["pre_compaction"]["observed_cached_ratio"] == pytest.approx(0.9)
    assert phases["first_post_compaction"]["observed_cached_ratio"] == pytest.approx(0.2)
    assert phases["later_post_compaction"]["observed_cached_ratio"] == pytest.approx(0.9)
