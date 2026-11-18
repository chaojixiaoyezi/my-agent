"""PP1118 R1 返工回归：waiting 终态 postcheck 锁外一致性、SimpleAgent 100MiB 切片预算接线与单次替换防护。"""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from agent_py_agent.agent.conversation.models import BackgroundMainAgentReport
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.scheduler import postcheck as postcheck_module
from agent_py_agent.agent.scheduler import service as service_module
from agent_py_agent.agent.scheduler.repository import (
    SchedulerJobCreateRequest,
    SchedulerRepository,
)
from agent_py_agent.agent.scheduler.service import SchedulerRunClaim, SchedulerService
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.settings.defaults import default_config_int


def _build_real_agent(
    tmp_path: Path,
    name: str,
    *,
    slice_bytes: int | None = None,
) -> tuple[SimpleAgent, Path, str]:
    sub = tmp_path / name
    workspace = sub / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    cfg_kwargs: dict[str, object] = {
        "my_agent_home": str(sub / "home"),
        "enable_tools": False,
        "memory_path": "memory.jsonl",
        "orphan_supervision_interval_seconds": 0,
    }
    if slice_bytes is not None:
        cfg_kwargs["retention_scan_slice_bytes"] = slice_bytes
    agent = SimpleAgent(AgentConfig(**cfg_kwargs), workspace)  # type: ignore[arg-type]
    agent.scheduler_service.monitor_jitter_window_seconds = 0
    thread = agent.conversation_store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": f"chat-{name}",
            "channel_user_id": "open-1",
            "cwd": str(workspace),
            "now": 900.0,
        }
    )
    return agent, workspace, thread.thread_id


def _enqueue_and_claim(agent: SimpleAgent, job_id: str, *, now: float) -> tuple[SchedulerRunClaim, object]:
    run = agent.scheduler_repository.reserve_manual_run(job_id, now=now)
    dispatch_ts = float(run["dispatch_after"])
    agent.scheduler_service.enqueue_ready_runs(now=dispatch_ts)
    pending = [
        sig for sig in agent.conversation_store.wakes.pending() if sig.root_task_id == run["run_id"]
    ]
    assert len(pending) == 1
    signal = pending[0]
    claimed = agent.scheduler_service.claim_wake(signal, now=dispatch_ts)
    assert claimed.status == "claimed" and claimed.claim is not None
    agent.scheduler_repository.mark_run_running(
        claimed.claim.run_id,
        claimed.claim.claim_id,
        now=dispatch_ts,
    )
    return claimed.claim, signal


def _park_and_complete_waiting_task(
    agent: SimpleAgent,
    claim: SchedulerRunClaim,
    initial_signal: object,
    *,
    now: float,
    task_status: str = "completed",
    final_response: str = "final waiting output",
    handle_followup_wake: bool = True,
) -> object:
    service = agent.scheduler_service
    store = agent.conversation_store
    parked = service.park_waiting(claim, response="interim", now=now)
    assert parked is not None and parked["status"] == "waiting"
    store.wakes.mark_handled(initial_signal.wake_signal_id, now=now)  # type: ignore[attr-defined]
    followup_sig = store.wakes.raise_signal(
        {
            "thread_id": str(claim.run["thread_id"]),
            "root_task_id": claim.run_id,
            "reason": "subagent_runner_finished",
            "now": now + 1.0,
        }
    )
    store.tasks.update_status({"task_id": claim.run_id, "status": task_status, "now": now + 1.0})
    if task_status == "completed":
        report = BackgroundMainAgentReport(
            thread_id=str(claim.run["thread_id"]),
            task_id=claim.run_id,
            reason=followup_sig.reason,
            response=final_response,
            route_channel="feishu",
            route_target="chat-target",
            created_at=now + 1.0,
            delivery_status="sent",
            delivery_reason="subagent_completion",
            wake_handled=True,
        )
        assert service.repository.record_waiting_delivery(report, now=now + 2.0) is True
    if handle_followup_wake:
        store.wakes.mark_handled(followup_sig.wake_signal_id, now=now + 2.0)
    return followup_sig


def test_waiting_and_direct_completion_produce_identical_postcheck_outcomes_outside_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent, workspace, thread_id = _build_real_agent(tmp_path, "parity")
    service = agent.scheduler_service
    repo = agent.scheduler_repository
    artifact = workspace / "out" / "result.json"

    job, _ = repo.create_job(
        SchedulerJobCreateRequest(
            name="parity-postcheck-job",
            prompt="write result.json",
            thread_id=thread_id,
            schedule={"kind": "every", "every_seconds": 600, "anchor_at": 9_000_000_000.0, "timezone": "UTC"},
            execution={"mode": "isolated"},
            postcheck={
                "artifacts": [
                    {
                        "path": "out/result.json",
                        "min_bytes": 10,
                        "max_age_seconds": 30,
                        "json_pointer": "/status",
                        "equals": "complete",
                    }
                ]
            },
            now=950.0,
        )
    )

    lock_depth = 0
    eval_calls: list[int] = []
    original_scope = repo._mutation_scope
    real_eval = service_module.evaluate_postcheck

    @contextmanager
    def _tracked_scope():
        nonlocal lock_depth
        lock_depth += 1
        try:
            with original_scope() as ctx:
                yield ctx
        finally:
            lock_depth -= 1

    def _tracked_eval(spec, *, root, now, read_bytes):
        assert lock_depth == 0, "evaluate_postcheck ran inside _mutation_scope"
        eval_calls.append(read_bytes)
        return real_eval(spec, root=root, now=now, read_bytes=read_bytes)

    monkeypatch.setattr(repo, "_mutation_scope", _tracked_scope)
    monkeypatch.setattr(service_module, "evaluate_postcheck", _tracked_eval)

    cases = [
        ("missing", None, 1100.0, "failed", "SCHEDULER_POSTCHECK_FAILED", "business", "ARTIFACT_MISSING"),
        (
            "passing",
            (json.dumps({"status": "complete", "count": 1}), 1215.0),
            1200.0,
            "done",
            "",
            "",
            "",
        ),
        (
            "stalled",
            (json.dumps({"status": "complete", "count": 1}), 1200.0),
            1300.0,
            "failed",
            "SCHEDULER_OUTPUT_STALLED",
            "stalled",
            "ARTIFACT_STALE",
        ),
        ("unavailable_dir", "DIR", 1400.0, "unknown", "SCHEDULER_POSTCHECK_UNAVAILABLE", "postcheck_unavailable", "ARTIFACT_UNREADABLE"),
    ]

    for idx, (_label, state_setup, base_ts, exp_status, exp_code, exp_kind, exp_check_code) in enumerate(cases):
        if artifact.is_dir():
            artifact.rmdir()
        elif artifact.exists():
            artifact.unlink()
        if state_setup == "DIR":
            artifact.mkdir(parents=True, exist_ok=True)
        elif isinstance(state_setup, tuple):
            content, mtime = state_setup
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text(content, encoding="utf-8")
            os.utime(artifact, (mtime, mtime))

        eval_ts = base_ts + 20.0
        # Direct finish
        d_claim, d_sig = _enqueue_and_claim(agent, job["job_id"], now=base_ts)
        agent.conversation_store.wakes.mark_handled(d_sig.wake_signal_id, now=base_ts + 3.0)  # type: ignore[attr-defined]
        d_term = service.finish(d_claim, status="done", response="direct ok", now=eval_ts)
        assert d_term is not None

        # Waiting reconcile_waiting_run on identical filesystem state & eval timestamp
        w_claim, w_sig = _enqueue_and_claim(agent, job["job_id"], now=base_ts + 5.0)
        _park_and_complete_waiting_task(agent, w_claim, w_sig, now=base_ts + 8.0)
        eval_before = len(eval_calls)
        w_term = service.reconcile_waiting_run(w_claim.run_id, now=eval_ts)
        assert w_term is not None
        assert len(eval_calls) == eval_before + 1, f"case {idx} waiting path did not evaluate postcheck"

        assert d_term["status"] == w_term["status"] == exp_status
        assert d_term["error_code"] == w_term["error_code"] == exp_code
        assert d_term.get("failure_kind", "") == w_term.get("failure_kind", "") == exp_kind
        assert d_term["postcheck_observation"] == w_term["postcheck_observation"]
        assert d_term["postcheck_observation"]["checks"][0]["code"] == exp_check_code


def test_waiting_postcheck_preserves_delivery_gate_cas_single_investigation_and_non_done_skip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent, _workspace, thread_id = _build_real_agent(tmp_path, "waiting-gates")
    service = agent.scheduler_service
    repo = agent.scheduler_repository

    job, _ = repo.create_job(
        SchedulerJobCreateRequest(
            name="waiting-inv-job",
            prompt="produce deliverable",
            thread_id=thread_id,
            schedule={"kind": "every", "every_seconds": 600, "anchor_at": 9_000_000_000.0, "timezone": "UTC"},
            execution={"mode": "isolated"},
            policy={"on_failure": {"investigate": True, "after_failures": 1, "retry_once": False}},
            postcheck={"artifacts": [{"path": "missing_deliverable.json", "min_bytes": 1}]},
            now=950.0,
        )
    )

    postcheck_invocations = 0
    real_eval = service_module.evaluate_postcheck

    def _counting_eval(spec, *, root, now, read_bytes):
        nonlocal postcheck_invocations
        postcheck_invocations += 1
        return real_eval(spec, root=root, now=now, read_bytes=read_bytes)

    monkeypatch.setattr(service_module, "evaluate_postcheck", _counting_eval)

    # 1. While follow-up delivery wake is still pending, reconcile_waiting_run returns None without postcheck.
    claim1, sig1 = _enqueue_and_claim(agent, job["job_id"], now=1000.0)
    followup_sig = _park_and_complete_waiting_task(
        agent,
        claim1,
        sig1,
        now=1002.0,
        handle_followup_wake=False,
    )
    assert service.reconcile_waiting_run(claim1.run_id, now=1005.0) is None
    assert postcheck_invocations == 0
    assert repo.get_active_run(claim1.run_id)["status"] == "waiting"

    # 2. Once delivery wake is handled, reconcile_waiting_run fails postcheck and schedules 1 investigation.
    agent.conversation_store.wakes.mark_handled(followup_sig.wake_signal_id, now=1006.0)  # type: ignore[attr-defined]
    term1 = service.reconcile_waiting_run(claim1.run_id, now=1007.0)
    assert term1 is not None
    assert term1["status"] == "failed"
    assert term1["error_code"] == "SCHEDULER_POSTCHECK_FAILED"
    assert term1["failure_kind"] == "business"
    assert term1["response"] == "final waiting output"
    assert term1["delivery_status"] == "sent"
    assert postcheck_invocations == 1

    updated_job = repo.get_job(job["job_id"])
    assert updated_job is not None
    streak = updated_job["failure_streak"]
    assert streak["count"] == 1
    inv_id = streak["investigation_run_id"]
    assert inv_id

    # Duplicate reconcile_waiting_run hits waiting guard / CAS and creates no second investigation or history row.
    assert service.reconcile_waiting_run(claim1.run_id, now=1008.0) is None
    assert len(repo.history(job_id=job["job_id"])[0]) == 1
    assert repo.get_job(job["job_id"])["failure_streak"]["investigation_run_id"] == inv_id

    # Drain the investigation run cleanly so next manual run can proceed.
    inv_claim = repo.claim_run(inv_id, lease_seconds=300, now=1009.0)
    assert inv_claim is not None
    service.finish(
        SchedulerRunClaim(inv_id, str(inv_claim["claim_id"]), inv_claim),
        status="done",
        response="checked",
        now=1010.0,
    )

    # 3. Concurrent waiting CAS loss during evaluate_postcheck returns None cleanly.
    claim2, sig2 = _enqueue_and_claim(agent, job["job_id"], now=1020.0)
    _park_and_complete_waiting_task(agent, claim2, sig2, now=1022.0)
    won_inside: list[dict[str, object] | None] = []

    def _racing_eval(spec, *, root, now, read_bytes):
        res = real_eval(spec, root=root, now=now, read_bytes=read_bytes)
        if not won_inside:
            won_inside.append(
                repo.finish_waiting_run(
                    claim2.run_id,
                    service_module.SchedulerRunFinish(
                        status="cancelled",
                        error_code="SCHEDULED_TASK_CANCELLED",
                        now=now,
                    ),
                )
            )
        return res

    monkeypatch.setattr(service_module, "evaluate_postcheck", _racing_eval)
    assert service.reconcile_waiting_run(claim2.run_id, now=1025.0) is None
    assert len(won_inside) == 1 and won_inside[0] is not None
    assert won_inside[0]["status"] == "cancelled"

    # 4. Non-success waiting TaskLink status ("failed") skips postcheck entirely.
    monkeypatch.setattr(service_module, "evaluate_postcheck", _counting_eval)
    before_count = postcheck_invocations
    claim3, sig3 = _enqueue_and_claim(agent, job["job_id"], now=1040.0)
    _park_and_complete_waiting_task(agent, claim3, sig3, now=1042.0, task_status="failed")
    term3 = service.reconcile_waiting_run(claim3.run_id, now=1045.0)
    assert term3 is not None
    assert term3["status"] == "failed"
    assert term3["error_code"] == "SCHEDULED_TASK_FAILED"
    assert "postcheck_observation" not in term3
    assert postcheck_invocations == before_count


def test_real_simple_agent_wires_100mib_and_lower_retention_scan_slice_bytes(tmp_path: Path) -> None:
    # 1. Real SimpleAgent configured with 100 MiB passes a > 8 MiB valid JSON artifact without manual repository.config.
    hundred_mib = 100 * 1024 * 1024
    agent_big, ws_big, thread_big = _build_real_agent(tmp_path, "big-budget", slice_bytes=hundred_mib)
    assert not hasattr(agent_big.scheduler_repository, "config")
    assert agent_big.scheduler_service._postcheck_read_bytes() == hundred_mib

    large_json_path = ws_big / "large_summary.json"
    large_body = '{"status":"complete","filler":"' + ("a" * (9 * 1024 * 1024)) + '"}\n'
    large_json_path.write_text(large_body, encoding="utf-8")
    assert large_json_path.stat().st_size > 8 * 1024 * 1024

    job_big, _ = agent_big.scheduler_repository.create_job(
        SchedulerJobCreateRequest(
            name="big-json-job",
            prompt="produce large json",
            thread_id=thread_big,
            schedule={"kind": "every", "every_seconds": 600, "anchor_at": 9_000_000_000.0, "timezone": "UTC"},
            execution={"mode": "isolated"},
            postcheck={
                "artifacts": [
                    {
                        "path": "large_summary.json",
                        "json_pointer": "/status",
                        "equals": "complete",
                    }
                ]
            },
            now=1950.0,
        )
    )
    now_big = large_json_path.stat().st_mtime + 1.0
    claim_big, sig_big = _enqueue_and_claim(agent_big, job_big["job_id"], now=now_big)
    agent_big.conversation_store.wakes.mark_handled(sig_big.wake_signal_id, now=now_big + 1.0)  # type: ignore[attr-defined]
    term_big = agent_big.scheduler_service.finish(claim_big, status="done", response="ok", now=now_big + 2.0)
    assert term_big is not None
    assert term_big["status"] == "done"
    assert term_big["postcheck_observation"]["status"] == "passed"

    # Waiting completion on the same > 8 MiB JSON artifact also passes under the 100 MiB SimpleAgent budget.
    wclaim_big, wsig_big = _enqueue_and_claim(agent_big, job_big["job_id"], now=now_big + 5.0)
    _park_and_complete_waiting_waiting_now = now_big + 7.0
    _park_and_complete_waiting_task(agent_big, wclaim_big, wsig_big, now=_park_and_complete_waiting_waiting_now)
    wterm_big = agent_big.scheduler_service.reconcile_waiting_run(wclaim_big.run_id, now=now_big + 10.0)
    assert wterm_big is not None
    assert wterm_big["status"] == "done"
    assert wterm_big["postcheck_observation"]["status"] == "passed"
    large_json_path.unlink()

    # 2. Lower-than-default budget (4 KiB) rejects an 8 KiB JSON artifact as unavailable, then passes after live config bump.
    agent_small, ws_small, thread_small = _build_real_agent(tmp_path, "small-budget", slice_bytes=4096)
    medium_json = ws_small / "medium.json"
    medium_json.write_text(json.dumps({"status": "complete", "pad": "b" * 8192}), encoding="utf-8")
    job_small, _ = agent_small.scheduler_repository.create_job(
        SchedulerJobCreateRequest(
            name="small-budget-job",
            prompt="produce medium json",
            thread_id=thread_small,
            schedule={"kind": "every", "every_seconds": 600, "anchor_at": 9_000_000_000.0, "timezone": "UTC"},
            execution={"mode": "isolated"},
            postcheck={"artifacts": [{"path": "medium.json", "json_pointer": "/status", "equals": "complete"}]},
            now=2950.0,
        )
    )
    now_small = medium_json.stat().st_mtime + 1.0
    c_small, s_small = _enqueue_and_claim(agent_small, job_small["job_id"], now=now_small)
    agent_small.conversation_store.wakes.mark_handled(s_small.wake_signal_id, now=now_small + 1.0)  # type: ignore[attr-defined]
    t_small = agent_small.scheduler_service.finish(c_small, status="done", response="ok", now=now_small + 2.0)
    assert t_small is not None
    assert t_small["status"] == "unknown"
    assert t_small["error_code"] == "SCHEDULER_POSTCHECK_UNAVAILABLE"

    agent_small.config = replace(agent_small.config, retention_scan_slice_bytes=32_768)
    c_raised, s_raised = _enqueue_and_claim(agent_small, job_small["job_id"], now=now_small + 10.0)
    agent_small.conversation_store.wakes.mark_handled(s_raised.wake_signal_id, now=now_small + 11.0)  # type: ignore[attr-defined]
    t_raised = agent_small.scheduler_service.finish(c_raised, status="done", response="ok", now=now_small + 12.0)
    assert t_raised is not None
    assert t_raised["status"] == "done"

    # 3. Standalone SchedulerService uses registry/AgentConfig canonical default.
    standalone_store = ConversationStore(tmp_path / "standalone_conv")
    standalone_repo = SchedulerRepository(
        tmp_path / "standalone_sched",
        owner_provider="local",
        owner_kind="main",
        owner_id="local/main",
    )
    standalone_service = SchedulerService(standalone_repo, conversation_store=standalone_store)
    assert standalone_service._postcheck_read_bytes() == default_config_int("retention_scan_slice_bytes")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="mkfifo requires POSIX")
def test_scheduler_service_waiting_and_direct_reject_fifo_and_parent_swaps_without_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent, workspace, thread_id = _build_real_agent(tmp_path, "swap-service")
    service = agent.scheduler_service
    repo = agent.scheduler_repository
    sub_dir = workspace / "sub"
    sub_dir.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside_swap"
    outside.mkdir(parents=True, exist_ok=True)
    (outside / "item.json").write_text(json.dumps({"ok": True}), encoding="utf-8")

    job, _ = repo.create_job(
        SchedulerJobCreateRequest(
            name="swap-guard-job",
            prompt="write item",
            thread_id=thread_id,
            schedule={"kind": "every", "every_seconds": 600, "anchor_at": 9_000_000_000.0, "timezone": "UTC"},
            execution={"mode": "isolated"},
            policy={"on_failure": {"investigate": False, "retry_once": True}},
            postcheck={"artifacts": [{"path": "sub/item.json", "json_pointer": "/ok", "equals": True}]},
            now=3950.0,
        )
    )

    real_discover = postcheck_module._discover_exact_target

    # Waiting run + FIFO swap: must return unknown/SCHEDULER_POSTCHECK_UNAVAILABLE quickly and queue zero retries.
    item = sub_dir / "item.json"
    item.write_text(json.dumps({"ok": True}), encoding="utf-8")
    fifo_swapped = False

    def _fifo_hook(**kwargs):
        nonlocal fifo_swapped
        res = real_discover(**kwargs)
        if not fifo_swapped and res[4] == "":
            fifo_swapped = True
            item.unlink()
            os.mkfifo(item, 0o600)
        return res

    monkeypatch.setattr(postcheck_module, "_discover_exact_target", _fifo_hook)
    w_claim, w_sig = _enqueue_and_claim(agent, job["job_id"], now=4010.0)
    _park_and_complete_waiting_task(agent, w_claim, w_sig, now=4012.0)

    out_holder: dict[str, object] = {}
    worker = threading.Thread(
        target=lambda: out_holder.setdefault(
            "term",
            service.reconcile_waiting_run(w_claim.run_id, now=4015.0),
        ),
        daemon=True,
    )
    worker.start()
    worker.join(timeout=2.0)
    assert not worker.is_alive(), "reconcile_waiting_run blocked on FIFO swap"
    term_fifo = out_holder["term"]
    assert isinstance(term_fifo, dict)
    assert term_fifo["status"] == "unknown"
    assert term_fifo["error_code"] == "SCHEDULER_POSTCHECK_UNAVAILABLE"
    assert repo.queued_runs() == []

    # Direct finish + parent directory symlink swap: must return unknown/SCHEDULER_POSTCHECK_UNAVAILABLE.
    item.unlink()
    item.write_text(json.dumps({"ok": False}), encoding="utf-8")
    parent_swapped = False

    def _parent_hook(**kwargs):
        nonlocal parent_swapped
        res = real_discover(**kwargs)
        if not parent_swapped and res[4] == "":
            parent_swapped = True
            os.rename(sub_dir, tmp_path / "moved_sub_dir")
            sub_dir.symlink_to(outside)
        return res

    monkeypatch.setattr(postcheck_module, "_discover_exact_target", _parent_hook)
    d_claim, d_sig = _enqueue_and_claim(agent, job["job_id"], now=4030.0)
    agent.conversation_store.wakes.mark_handled(d_sig.wake_signal_id, now=4031.0)  # type: ignore[attr-defined]
    term_parent = service.finish(d_claim, status="done", response="ok", now=4032.0)
    assert term_parent is not None
    assert term_parent["status"] == "unknown"
    assert term_parent["error_code"] == "SCHEDULER_POSTCHECK_UNAVAILABLE"
    assert repo.queued_runs() == []
