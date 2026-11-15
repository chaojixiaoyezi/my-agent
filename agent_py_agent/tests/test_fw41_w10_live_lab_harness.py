from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.settings import load_config
from scripts.live_lab.compact_observations import (
    LiveLabIsolationError,
    aggregate_usage_records,
    assert_isolated_home_path,
    collect_bg_process_census,
    collect_compaction_observations,
    default_host_agent_homes,
    diff_bg_process_census,
    load_canonical_compaction_checkpoints,
    parse_compaction_telemetry,
    prepare_isolated_subprocess_env,
    real_user_home_dir,
    slice_usage_jsonl_turns,
    snapshot_compaction_baseline,
)
from scripts.live_lab.main_agent_compact_stress import (
    _assert_current_run_report_provenance,
    _disable_shell_tool_for_compact_stress,
    _ensure_compact_stress_config,
    _seed_stress_file,
    _stress_facts,
    case_main_compact_stress_long_read,
)
from scripts.live_lab.reporter import LabReporter
from scripts.live_lab.runner import LabRunner, LiveLabError, _utf8_env
from scripts.live_lab.session import LabSessionManager


def _make_args(tmp_path: Path, *, run_id: str = "w10-test", suite: str = "smoke", timeout: int = 120) -> SimpleNamespace:
    source_cfg = tmp_path / "source_config.yaml"
    if not source_cfg.exists():
        source_cfg.write_text("model_backend: echo\nmodel_context_window_tokens: 200000\n", encoding="utf-8")
    return SimpleNamespace(
        config=str(source_cfg),
        runs_dir=str(tmp_path / "runs"),
        run_id=run_id,
        suite=suite,
        real_llm=False,
        count=1,
        timeout=timeout,
        keep_going=False,
    )


def _v4_bg_record(
    *,
    session_id: str,
    owner_home: str,
    workspace_root: str,
    status: str,
    child_pid: int,
    host_pid: int = 9001,
    launcher_pid: int = 9000,
    finished_at: float | None = None,
    exit_code: int | None = None,
) -> dict[str, object]:
    child_started = status != "not_started"
    return {
        "schema": "managed_process_session.v4",
        "session_id": session_id,
        "access_scope": {
            "owner_id": "local:main",
            "conversation_id": "thread-1",
            "owner_home": owner_home,
        },
        "execution_scope": {
            "owner_home": owner_home,
            "thread_id": "thread-1",
            "root_task_id": "task-1",
            "run_id": "run-1",
            "attempt_id": "attempt-1",
        },
        "activation_scope": None,
        "retain_until_consumed": False,
        "completion_target": {},
        "launcher_pid": launcher_pid,
        "launcher_birth_token": "launcher-1",
        "pid": host_pid if child_started else 0,
        "pid_birth_token": "host-1" if child_started else "",
        "child_pid": child_pid,
        "child_pid_birth_token": "child-1" if child_pid > 0 else "",
        "revision": 1,
        "stop_requested": status == "killed",
        "handoff_confirmed": False,
        "child_launch_started": child_started,
        "reserved_at": 100.0,
        "started_at": 101.0 if child_pid > 0 else 0.0,
        "status": status,
        "finished_at": finished_at,
        "exit_code": exit_code,
        "command": "python -m http.server",
        "cwd": workspace_root,
        "output_file": "",
        "host_state_file": "",
        "completion_notice_id": "",
    }


def test_session_turn_usage_attribution_slices_per_turn_deltas_accurately(tmp_path: Path) -> None:
    session = LabSessionManager(_make_args(tmp_path))
    session.setup()

    usage_file = session.run_root / "llm_usage.jsonl"
    session.usage_ledger_path = usage_file
    assert session.model_usage_dir is not None
    session.model_usage_dir.mkdir(parents=True, exist_ok=True)
    thread_usage_file = session.model_usage_dir / "thread-alpha.jsonl"

    # Pre-existing line before turn 1 must NOT be attributed to turn 1.
    usage_file.write_text(
        json.dumps({"turn": 0, "purpose": "decision", "input_tokens": 9999, "output_tokens": 99, "cached_tokens": 0})
        + "\n",
        encoding="utf-8",
    )

    # Turn 1: two flat rows (one without cached_tokens reported -> must not fill 0 into paired ratio).
    t1 = session.begin_turn_usage("turn-1")
    with usage_file.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"purpose": "main", "input_tokens": 1000, "output_tokens": 100, "cached_tokens": 800}) + "\n"
        )
        handle.write(json.dumps({"purpose": "auxiliary", "input_tokens": 500, "output_tokens": 50}) + "\n")
    r1 = session.end_turn_usage(t1)

    assert r1["call_count"] == 2
    assert r1["input_tokens"] == 1500
    assert r1["output_tokens"] == 150
    assert r1["cached_tokens"] == 800
    # Paired subset only includes calls with BOTH input and cached valid (800 / 1000 == 0.8).
    assert r1["paired_cache_calls"] == 1
    assert r1["observed_cached_ratio"] == pytest.approx(0.8)
    assert r1["coverage"] == "partial"

    # Turn 2: canonical thread_model_usage_event.v1 row + trailing incomplete (unterminated) tail line.
    t2 = session.begin_turn_usage("turn-2")
    canonical_event = {
        "schema_version": "thread_model_usage_event.v1",
        "event_id": "evt-2",
        "thread_id": "thread-alpha",
        "request_id": "req-2",
        "run_id": "run-2",
        "task_id": "task-2",
        "source": "main",
        "created_at": 200.0,
        "model_calls": {
            "schema": "model_call_summary.v1",
            "logical_model_turn_count": 1,
            "physical_model_attempt_count": 1,
            "provider_http_attempt_count": 1,
            "accounted_input_tokens": 2000,
            "output_tokens": 250,
            "cached_input_tokens": 1500,
            "usage_breakdown": {
                "provider": {
                    "input_tokens": 2000,
                    "output_tokens": 250,
                    "cache_read_input_tokens": 1500,
                    "call_count": 1,
                    "input_tokens_reported_call_count": 1,
                    "output_tokens_reported_call_count": 1,
                    "cache_read_input_tokens_reported_call_count": 1,
                },
                "estimated": {"input_tokens": 0, "output_tokens": 0, "call_count": 0, "unfinished_call_count": 0},
            },
            "purpose_breakdown": {
                "schema": "model_call_purpose_breakdown.v1",
                "main": {
                    "usage_breakdown": {
                        "provider": {
                            "input_tokens": 2000,
                            "output_tokens": 250,
                            "cache_read_input_tokens": 1500,
                            "call_count": 1,
                            "input_tokens_reported_call_count": 1,
                            "output_tokens_reported_call_count": 1,
                            "cache_read_input_tokens_reported_call_count": 1,
                        },
                        "estimated": {"call_count": 0},
                    }
                },
            },
        },
    }
    thread_usage_file.write_text(json.dumps(canonical_event) + "\n", encoding="utf-8")
    partial_line = json.dumps({"purpose": "main", "input_tokens": 400, "output_tokens": 40, "cached_tokens": 300})
    with usage_file.open("a", encoding="utf-8") as handle:
        # Write incomplete tail without trailing newline; turn-2 must leave cursor before it.
        handle.write(partial_line)
    r2 = session.end_turn_usage(t2)

    assert r2["call_count"] == 1
    assert r2["input_tokens"] == 2000
    assert r2["output_tokens"] == 250
    assert r2["cached_tokens"] == 1500
    assert r2["observed_cached_ratio"] == pytest.approx(0.75)
    assert r2["coverage"] == "complete"
    assert r2["delta_metadata"]["pending_tail_bytes"][str(usage_file.resolve())] == len(partial_line.encode("utf-8"))

    # Turn 3: finishing the newline makes that formerly partial row land cleanly in turn-3.
    t3 = session.begin_turn_usage("turn-3")
    with usage_file.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    r3 = session.end_turn_usage(t3)

    assert r3["call_count"] == 1
    assert r3["input_tokens"] == 400
    assert r3["output_tokens"] == 40
    assert r3["cached_tokens"] == 300
    assert r3["observed_cached_ratio"] == pytest.approx(0.75)

    # Standalone slicer + zero-fill check for missing cached_tokens.
    turns_offline = slice_usage_jsonl_turns(usage_file)
    assert [row["input_tokens"] for row in turns_offline] == [9999, 1900]
    unreported = aggregate_usage_records([{"purpose": "main", "input_tokens": 600, "output_tokens": 60}])
    assert unreported["cached_tokens"] is None
    assert unreported["observed_cached_ratio"] is None


def test_compact_observations_parses_fork_independent_and_no_progress_telemetry(tmp_path: Path) -> None:
    session = LabSessionManager(_make_args(tmp_path))
    session.setup()
    ctx = session.resolved_home_context
    compact_dir = Path(ctx["owner_compact_dir"])
    threads_dir = Path(ctx["threads_dir"])
    threads_dir.mkdir(parents=True, exist_ok=True)
    (compact_dir / "conversations").mkdir(parents=True, exist_ok=True)

    # Thread at generation 3 where gen 1 was pre-existing baseline, gen 2..3 are current run,
    # and a failed/superseded candidate sits in the same JSONL ledger (D4-COUNTER).
    (threads_dir / "thread-1.json").write_text(
        json.dumps(
            {
                "thread_id": "thread-1",
                "compact_generation": 3,
                "compact_checkpoint_id": "compact-v3-3",
            }
        ),
        encoding="utf-8",
    )
    ledger_rows = [
        {
            "schema": "conversation_compact_checkpoint.v3",
            "thread_id": "thread-1",
            "generation": 1,
            "checkpoint_id": "compact-v3-1",
            "previous_checkpoint_id": "",
            "projected_tokens_before": 100_000,
            "projected_tokens_after": 20_000,
            "trigger_tokens": 140_000,
            "context_window_tokens": 200_000,
            "recovery_target_tokens": 120_000,
            "mode": "cache_fork",
        },
        {
            "schema": "conversation_compact_checkpoint.v3",
            "thread_id": "thread-1",
            "generation": 2,
            "checkpoint_id": "compact-v3-2",
            "previous_checkpoint_id": "compact-v3-1",
            "projected_tokens_before": 150_000,
            "projected_tokens_after": 15_000,
            "trigger_tokens": 140_000,
            "context_window_tokens": 200_000,
            "recovery_target_tokens": 120_000,
            "cache_fork_used": True,
        },
        {
            "schema": "conversation_compact_checkpoint.v3",
            "thread_id": "thread-1",
            "generation": 2,
            "checkpoint_id": "compact-v3-2-superseded",
            "previous_checkpoint_id": "compact-v3-1",
            "status": "superseded",
            "projected_tokens_before": 150_000,
            "projected_tokens_after": 150_000,
        },
        {
            "schema": "conversation_compact_checkpoint.v3",
            "thread_id": "thread-1",
            "generation": 3,
            "checkpoint_id": "compact-v3-3",
            "previous_checkpoint_id": "compact-v3-2",
            "projected_tokens_before": 160_000,
            "projected_tokens_after": 8_000,
            "trigger_tokens": 140_000,
            "context_window_tokens": 200_000,
            "recovery_target_tokens": 120_000,
            "used_cache_fork": False,
        },
    ]
    (compact_dir / "conversations" / "thread-1.jsonl").write_text(
        "\n".join(json.dumps(r) for r in ledger_rows) + "\n",
        encoding="utf-8",
    )

    checkpoints = load_canonical_compaction_checkpoints(
        compact_dir=compact_dir,
        threads_dir=threads_dir,
        baseline={"thread_generations": {"thread-1": 1}},
    )
    assert checkpoints["successful_cycle_count"] == 2
    assert checkpoints["uncommitted_or_superseded_count"] == 1
    assert [c["generation"] for c in checkpoints["cycles"]] == [2, 3]
    assert [c["mode"] for c in checkpoints["cycles"]] == ["cache_fork", "independent_summary"]
    assert checkpoints["cycles"][0]["ratio"] == pytest.approx(10.0)
    assert checkpoints["cycles"][1]["ratio"] == pytest.approx(20.0)
    assert checkpoints["ratio_min"] == pytest.approx(10.0)
    assert checkpoints["ratio_median"] == pytest.approx(15.0)

    class RuntimeFactsTurn:
        pass

    telemetry_log = session.run_root / "compact_events.jsonl"
    events = [
        {"event": "compact", "mode": "cache_fork", "committed": True, "retained_runtime_facts_count": 4},
        {
            "event": "compact",
            "tool_choice": {"type": "none", "reason": "active_turn_summary_no_prefix"},
            "retained_runtime_facts": ["a", "b"],
        },
        {
            "event": "compact_skip",
            "reason_code": "COMPACT_NO_PROGRESS",
            "committed": False,
            "tokens_before": 145_000,
            "candidate_tokens": 149_000,
            "thread_id": "thread-1",
            "generation": 4,
        },
        {"event": "compact", "reason_code": "COMPACT_LOCAL_RECOVERY_EXHAUSTED", "mode": "turn_local"},
    ]
    telemetry_log.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")

    # Also test RuntimeFactsTurn counting from an IR list directly.
    telemetry = parse_compaction_telemetry(
        [
            telemetry_log,
            {"event": "compact_ir_snapshot", "retained_ir_history": [RuntimeFactsTurn(), {"type": "runtime_facts"}, {"type": "user"}]},
        ]
    )
    assert telemetry["cache_fork_count"] == 1
    assert telemetry["independent_summary_count"] == 1
    assert telemetry["turn_local_attempt_count"] == 1
    assert telemetry["no_progress_count"] == 1
    assert telemetry["runaway_no_progress_detected"] is False
    assert telemetry["no_progress_events"][0]["reason_code"] == "COMPACT_NO_PROGRESS"
    assert telemetry["no_progress_events"][0]["committed"] is False
    assert telemetry["no_progress_events"][0]["tokens_before"] == 145_000
    assert telemetry["no_progress_events"][0]["candidate_tokens"] == 149_000
    assert telemetry["retained_runtime_facts_counts"] == [4, 2, 2]
    assert telemetry["latest_retained_runtime_facts_count"] == 2

    # Repeated consecutive COMPACT_NO_PROGRESS must flag a runaway loop.
    loop_telemetry = parse_compaction_telemetry(
        [{"reason_code": "COMPACT_NO_PROGRESS", "committed": False} for _ in range(3)]
    )
    assert loop_telemetry["runaway_no_progress_detected"] is True

    report = collect_compaction_observations(
        config_path=session.config_path,
        turn_usages=[
            {"input_tokens": 10_000, "cached_tokens": 9_000, "call_count": 1, "compaction_occurred": False},
            {"input_tokens": 20_000, "cached_tokens": 4_000, "call_count": 1, "compaction_occurred": True},
            {"input_tokens": 25_000, "cached_tokens": 22_500, "call_count": 1, "compaction_occurred": False},
        ],
        telemetry_sources=[telemetry_log],
        baseline={"thread_generations": {"thread-1": 1}},
    )
    assert report["checkpoints"]["successful_cycle_count"] == 2
    assert report["cache_phases"]["pre_compaction"]["observed_cached_ratio"] == pytest.approx(0.9)
    assert report["cache_phases"]["first_post_compaction"]["observed_cached_ratio"] == pytest.approx(0.2)
    assert report["cache_phases"]["later_post_compaction"]["observed_cached_ratio"] == pytest.approx(0.9)


def test_runner_enforces_isolated_home_and_reports_bg_process_census(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_agent_homes = default_host_agent_homes()
    real_pw_home = real_user_home_dir()

    # 1. Direct refusal when target resolves to host ~/.my-agent or bare real HOME.
    if host_agent_homes:
        real_dot_agent = host_agent_homes[0]
        with pytest.raises(LiveLabIsolationError, match="HOST_HOME_FORBIDDEN"):
            assert_isolated_home_path(real_dot_agent, label="MY_AGENT_HOME")
        with pytest.raises(LiveLabError):
            _ensure_runs_dir_rejected(real_dot_agent, tmp_path)
    if real_pw_home is not None:
        with pytest.raises(LiveLabIsolationError, match="HOST_HOME_FORBIDDEN"):
            assert_isolated_home_path(real_pw_home, label="HOME")
        with pytest.raises(LiveLabIsolationError, match="CALLER_HOST_HOME_ENV_FORBIDDEN"):
            prepare_isolated_subprocess_env(
                isolated_home=None,
                base_env={"HOME": str(real_pw_home), "MY_AGENT_HOME": str(real_pw_home / ".my-agent")},
                refuse_host_env=True,
            )

    # 2. Override caller env pointing at real ~/.my-agent when LabRunner uses isolated session.
    session = LabSessionManager(_make_args(tmp_path))
    session.setup()
    if real_pw_home is not None:
        monkeypatch.setenv("HOME", str(real_pw_home))
        monkeypatch.setenv("MY_AGENT_HOME", str(real_pw_home / ".my-agent"))
        monkeypatch.setenv("TMPDIR", str(real_pw_home / ".my-agent" / "tmp"))

    env = _utf8_env(session)
    assert Path(env["MY_AGENT_HOME"]).resolve() == session.isolated_home.resolve()
    assert Path(env["HOME"]).resolve() == (session.run_root / "process_home").resolve()
    assert Path(env["TMPDIR"]).resolve() == (session.run_root / "tmp").resolve()

    # 3. 3-state bg-* process census + diff across active, settled, and unknown records.
    assert session.process_store_root is not None
    store_root = session.process_store_root
    store_root.mkdir(parents=True, exist_ok=True)
    owner_home = str(session.owner_home_dir)
    workspace_root = str(session.fixture_root)

    (store_root / "bg-settled-1.json").write_text(
        json.dumps(
            _v4_bg_record(
                session_id="bg-settled-1",
                owner_home=owner_home,
                workspace_root=workspace_root,
                status="exited",
                child_pid=41001,
                finished_at=105.0,
                exit_code=0,
            )
        ),
        encoding="utf-8",
    )
    (store_root / "bg-active-1.json").write_text(
        json.dumps(
            _v4_bg_record(
                session_id="bg-active-1",
                owner_home=owner_home,
                workspace_root=workspace_root,
                status="running",
                child_pid=41002,
            )
        ),
        encoding="utf-8",
    )
    (store_root / "bg-orphan-1.json").write_text(
        json.dumps(
            _v4_bg_record(
                session_id="bg-orphan-1",
                owner_home=owner_home,
                workspace_root=workspace_root,
                status="running",
                child_pid=41003,
            )
        ),
        encoding="utf-8",
    )
    (store_root / "bg-corrupt-1.json").write_text("{not-valid-json", encoding="utf-8")

    # Dead/alive OS pid lookup mock: only 41002 is alive in OS.
    census = collect_bg_process_census(store_root, pid_alive_fn=lambda pid: pid == 41002)
    assert census["counts"] == {"active": 1, "settled": 1, "unknown": 2, "total": 4}
    assert [item["session_id"] for item in census["active"]] == ["bg-active-1"]
    assert [item["session_id"] for item in census["settled"]] == ["bg-settled-1"]
    assert {item["session_id"] for item in census["unknown"]} == {"bg-orphan-1", "bg-corrupt-1"}

    pre = collect_bg_process_census(tmp_path / "nonexistent-bg-dir")
    diff = diff_bg_process_census(pre, census)
    assert diff["new_active_count"] == 1
    assert diff["new_unknown_count"] == 2
    assert diff["newly_settled_count"] == 1
    assert diff["leaked_count"] == 3

    # 4. LabRunner.run_suite captures pre/post census and isolation into live_lab_summary.json.
    # Replace active/orphan records with clean settled store for suite completion test.
    for path in list(store_root.glob("bg-*.json")):
        path.unlink()
    (store_root / "bg-settled-1.json").write_text(
        json.dumps(
            _v4_bg_record(
                session_id="bg-settled-1",
                owner_home=owner_home,
                workspace_root=workspace_root,
                status="exited",
                child_pid=41001,
                finished_at=105.0,
                exit_code=0,
            )
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr("scripts.live_lab.runner._default_check", None, raising=False)
    monkeypatch.setattr(
        "scripts.live_lab.compact_observations._default_pid_alive",
        lambda pid: False,
    )
    reporter = LabReporter(session, session.args)
    runner = LabRunner(session, reporter, session.args)
    rc = runner.run_suite(lambda lab, case_name: None)
    assert rc == 0
    summary = json.loads(session.summary_path.read_text(encoding="utf-8"))
    assert summary["ok"] is True
    assert summary["home_isolation"]["isolated"] is True
    assert Path(summary["isolated_home"]) == session.isolated_home
    assert summary["bg_process_census"]["post"]["counts"]["settled"] == 1
    assert summary["bg_process_census"]["diff"]["leaked_count"] == 0


def _ensure_runs_dir_rejected(real_dot_agent: Path, tmp_path: Path) -> None:
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("model_backend: echo\n", encoding="utf-8")
    args = SimpleNamespace(
        config=str(cfg),
        runs_dir=str(real_dot_agent),
        run_id="bad-home",
        suite="smoke",
        real_llm=False,
        count=1,
        timeout=60,
        keep_going=False,
    )
    LabSessionManager(args)


def test_compact_stress_d4_home_and_d4_key_use_cli_resolved_policy_and_paths(tmp_path: Path) -> None:
    session = LabSessionManager(_make_args(tmp_path, run_id="d4-home-key"))
    session.setup()
    lab = SimpleNamespace(
        config_path=session.config_path,
        fixture_root=session.fixture_root,
        args=session.args,
        _session=session,
        log=lambda _msg="": None,
    )

    home_ctx = _ensure_compact_stress_config(lab)
    cfg_text = session.config_path.read_text(encoding="utf-8")
    assert "memory_compact_trigger_percent: 70" in cfg_text
    assert "memory_compact_auto_trigger_percent" not in cfg_text

    loaded = load_config(session.config_path)
    assert loaded.memory_compact_trigger_percent == 70
    assert not any("memory_compact_auto_trigger_percent" in w for w in loaded.config_warnings)
    assert home_ctx["compact_policy"]["effective_trigger_percent"] == 70
    assert home_ctx["compact_policy"]["output_reserve_tokens"] > 0

    policy_path = _disable_shell_tool_for_compact_stress(lab, home_ctx=home_ctx)
    assert policy_path == session.owner_tool_policy_json
    assert policy_path.is_file()
    assert not policy_path.resolve().is_relative_to(session.fixture_root.resolve())
    assert not (session.fixture_root / ".my_agent" / "home").exists()


def test_compact_stress_d4_reuse_and_counter_enforce_provenance_and_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = LabSessionManager(_make_args(tmp_path, run_id="reused-run"))
    session.setup()
    assert session.reused_existing_run_root is False

    # Re-create with the exact same run_id: must record reuse provenance without deleting unrelated files.
    unrelated = session.fixture_root / "notes" / "keep_me.txt"
    unrelated.write_text("important unrelated data", encoding="utf-8")
    stale_report = session.fixture_root / "lab_outputs" / "compact-stress" / "report.md"
    stale_report.parent.mkdir(parents=True, exist_ok=True)
    stale_report.write_text("stale leftover report " * 200, encoding="utf-8")

    # Pre-existing ledger with 5 historical apply_ids that have zero committed canonical thread head.
    ctx = session.resolved_home_context
    legacy_ledger = Path(ctx["owner_home_dir"]) / "workspace" / "memory_archive" / "compact_applies" / "ledger.jsonl"
    legacy_ledger.parent.mkdir(parents=True, exist_ok=True)
    legacy_ledger.write_text(
        "\n".join(json.dumps({"apply_id": f"hist-{i}"}) for i in range(5)) + "\n",
        encoding="utf-8",
    )

    reused_session = LabSessionManager(_make_args(tmp_path, run_id="reused-run"))
    reused_session.setup()
    assert reused_session.reused_existing_run_root is True
    assert unrelated.read_text(encoding="utf-8") == "important unrelated data"
    provenance = json.loads(reused_session.provenance_path.read_text(encoding="utf-8"))
    assert provenance["reused_existing_run_root"] is True
    assert provenance["preexisting_artifacts"]["compact_stress_report"]["exists"] is True

    # Stale leftover report unchanged during run must fail closed.
    pre_stamp = reused_session.preexisting_artifacts["compact_stress_report"]
    with pytest.raises(RuntimeError, match="STALE_REPORT_PROVENANCE"):
        _assert_current_run_report_provenance(stale_report, pre_stamp)

    # Seed checks mismatched content and regenerates rather than trusting byte size alone.
    facts = _stress_facts()
    journal = reused_session.fixture_root / "data" / "long_field_journal.txt"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_bytes(b"X" * (1024 * 1024 + 50))
    seed_meta = _seed_stress_file(journal, facts, size_mb=1)
    assert seed_meta["reused_verified_content"] is False
    assert facts[0].checkpoint in journal.read_text(encoding="utf-8")

    # End-to-end stress case test with mocked lab.run_command writing fresh report & canonical chain < 5 vs == 5.
    reporter = LabReporter(reused_session, reused_session.args)
    runner = LabRunner(reused_session, reporter, reused_session.args)

    def _fake_report_text() -> str:
        lines = ["# Compact Stress Report", "", "## 总体结论", "完成完整分段阅读，最终决定均已核对。"]
        for fact in facts:
            lines.append(
                f"章节 {fact.section:03d} | {fact.city} | 最终决定：{fact.decision} | {fact.risk} | {fact.checkpoint}"
            )
        return "\n".join(lines) + ("\n补充证据" * 150)

    def _mock_run_only_report(*_args, **_kwargs):
        stale_report.write_text(_fake_report_text(), encoding="utf-8")
        return SimpleNamespace(stdout="no committed compact occurred", stderr="", returncode=0)

    monkeypatch.setattr(runner, "run_command", _mock_run_only_report)
    from scripts.live_lab.runner import _LabInterface

    lab = _LabInterface(runner)
    with pytest.raises(RuntimeError, match="COMPACT_CYCLES_INSUFFICIENT"):
        case_main_compact_stress_long_read(lab)

    # Now simulate 5 new canonical committed generations -> must pass and write telemetry JSON!
    threads_dir = Path(ctx["threads_dir"])
    compact_dir = Path(ctx["owner_compact_dir"])
    threads_dir.mkdir(parents=True, exist_ok=True)
    (compact_dir / "conversations").mkdir(parents=True, exist_ok=True)
    rows = []
    prev_id = ""
    for gen in range(1, 6):
        cid = f"compact-v3-{gen}"
        rows.append(
            {
                "schema": "conversation_compact_checkpoint.v3",
                "thread_id": "stress-thread",
                "generation": gen,
                "checkpoint_id": cid,
                "previous_checkpoint_id": prev_id,
                "projected_tokens_before": 150_000,
                "projected_tokens_after": 12_000,
                "mode": "cache_fork" if gen % 2 == 1 else "independent_summary",
            }
        )
        prev_id = cid
    (compact_dir / "conversations" / "stress-thread.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    (threads_dir / "stress-thread.json").write_text(
        json.dumps({"thread_id": "stress-thread", "compact_generation": 5, "compact_checkpoint_id": prev_id}),
        encoding="utf-8",
    )

    baseline_check = snapshot_compaction_baseline(compact_dir=compact_dir, threads_dir=threads_dir)
    assert baseline_check["thread_generations"]["stress-thread"] == 5

    # Reset thread baseline before invoking pass run: use a fresh thread_id with baseline=0 -> 5 cycles passes.
    (threads_dir / "stress-thread.json").unlink()
    (compact_dir / "conversations" / "stress-thread.jsonl").unlink()

    def _mock_run_with_5_cycles(*_args, **_kwargs):
        stale_report.write_text(_fake_report_text(), encoding="utf-8")
        (compact_dir / "conversations" / "stress-thread-2.jsonl").write_text(
            "\n".join(json.dumps({**r, "thread_id": "stress-thread-2"}) for r in rows) + "\n", encoding="utf-8"
        )
        (threads_dir / "stress-thread-2.json").write_text(
            json.dumps({"thread_id": "stress-thread-2", "compact_generation": 5, "compact_checkpoint_id": "compact-v3-5"}),
            encoding="utf-8",
        )
        return SimpleNamespace(stdout='{"event":"compact","mode":"cache_fork","committed":true}\n', stderr="", returncode=0)

    monkeypatch.setattr(runner, "run_command", _mock_run_with_5_cycles)
    case_main_compact_stress_long_read(lab)
    telemetry_file = lab.responses_dir / "main_compact_stress_long_read.telemetry.json"
    assert telemetry_file.is_file()
    saved_obs = json.loads(telemetry_file.read_text(encoding="utf-8"))
    assert saved_obs["checkpoints"]["successful_cycle_count"] == 5
    assert saved_obs["compact_policy"]["effective_trigger_percent"] == 70
    assert saved_obs["bg_census_diff"]["leaked_count"] == 0
