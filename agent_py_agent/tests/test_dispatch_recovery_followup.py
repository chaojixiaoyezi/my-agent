from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from agent_py_agent.agent.agent_core import dispatch_recovery_followup as followup


@dataclass
class Record:
    step: str
    action: str
    ok: bool = True
    applied: bool = False
    run_id: str = ""
    created_run_ids: list[str] | None = None
    runner_created_child_ids: list[str] | None = None


def test_post_runner_recovery_followup_reruns_after_takeover_action(monkeypatch):
    ctx = SimpleNamespace(
        cfg=object(),
        take_over_by="",
        locked_files=[],
        limit=20,
        root_id="",
        include_run_ids=[],
        exclude_run_ids=[],
        runner_instruction="",
    )
    params = SimpleNamespace(apply=True, execute_runners=True)
    runner_failure = Record(step="runner", action="execute_runner", ok=False, applied=True)
    action_record = Record(step="action_apply", action="takeover_or_reassign", ok=True, applied=True)
    rerun_record = Record(step="runner", action="execute_runner", ok=True, applied=True)

    monkeypatch.setattr(followup, "make_action_apply_records", lambda params: [action_record])
    monkeypatch.setattr(followup, "run_dispatch_runner_stage", lambda request: [*request.records, rerun_record])

    records = followup.run_post_runner_recovery_followup(
        followup.PostRunnerRecoveryFollowupParams(object(), ctx, params, [runner_failure])
    )

    assert records == [runner_failure, action_record, rerun_record]
    assert "takeover/repair run" in ctx.runner_instruction


def test_post_runner_recovery_followup_includes_created_recovery_runs(monkeypatch):
    ctx = SimpleNamespace(
        cfg=object(),
        take_over_by="",
        locked_files=[],
        limit=20,
        root_id="source-run",
        include_run_ids=["source-run"],
        exclude_run_ids=[],
        runner_instruction="",
    )
    params = SimpleNamespace(apply=True, execute_runners=True)
    runner_failure = Record(step="runner", action="execute_runner", ok=False, run_id="source-run")
    action_record = Record(
        step="action_apply",
        action="takeover_or_reassign",
        ok=True,
        applied=True,
        run_id="source-run",
        created_run_ids=["takeover-run"],
    )
    captured_include_run_ids: list[str] = []

    monkeypatch.setattr(followup, "make_action_apply_records", lambda params: [action_record])

    def fake_run_dispatch_runner_stage(request):
        captured_include_run_ids.extend(request.ctx.include_run_ids)
        return request.records

    monkeypatch.setattr(followup, "run_dispatch_runner_stage", fake_run_dispatch_runner_stage)

    followup.run_post_runner_recovery_followup(
        followup.PostRunnerRecoveryFollowupParams(object(), ctx, params, [runner_failure])
    )

    assert captured_include_run_ids == ["takeover-run"]


def test_post_runner_recovery_followup_uses_dispatch_child_ids_when_action_ids_are_projected(monkeypatch):
    ctx = SimpleNamespace(
        cfg=object(),
        take_over_by="",
        locked_files=[],
        limit=20,
        root_id="source-run",
        include_run_ids=["source-run"],
        exclude_run_ids=[],
        runner_instruction="",
    )
    params = SimpleNamespace(apply=True, execute_runners=True)
    runner_failure = Record(step="runner", action="execute_runner", ok=False, run_id="source-run")
    action_record = Record(
        step="action_apply",
        action="create_repair_child_from_artifact_integrity_refs",
        ok=True,
        applied=True,
        run_id="source-run",
        runner_created_child_ids=["repair-run"],
    )
    captured_include_run_ids: list[str] = []

    monkeypatch.setattr(followup, "make_action_apply_records", lambda params: [action_record])

    def fake_run_dispatch_runner_stage(request):
        captured_include_run_ids.extend(request.ctx.include_run_ids)
        return request.records

    monkeypatch.setattr(followup, "run_dispatch_runner_stage", fake_run_dispatch_runner_stage)

    followup.run_post_runner_recovery_followup(
        followup.PostRunnerRecoveryFollowupParams(object(), ctx, params, [runner_failure])
    )

    assert captured_include_run_ids == ["repair-run"]


def test_post_runner_recovery_followup_reruns_continuable_timeout_before_takeover(monkeypatch):
    ctx = SimpleNamespace(
        cfg=object(),
        take_over_by="",
        locked_files=[],
        limit=20,
        root_id="source-run",
        include_run_ids=["source-run"],
        exclude_run_ids=[],
        runner_instruction="",
        max_runners=1,
    )
    params = SimpleNamespace(apply=True, execute_runners=True)
    runner_failure = Record(step="runner", action="execute_runner", ok=False, run_id="source-run")
    source_task = SimpleNamespace(id="source-run")
    agent = SimpleNamespace(subagents=SimpleNamespace(load=lambda run_id: source_task))
    captured: dict[str, object] = {}

    def fake_strategy(request):
        return SimpleNamespace(
            recommended_action="rerun_original_from_continue_packet",
            runner_instruction="读取 latest_continue_packet.json 接续写完 HTML。",
        )

    def fake_action_apply(params):
        raise AssertionError("continuable timeout should rerun before action apply")

    def fake_run_dispatch_runner_stage(request):
        captured["include_run_ids"] = list(request.ctx.include_run_ids)
        captured["runner_instruction"] = request.ctx.runner_instruction
        return [*request.records, Record(step="runner", action="execute_runner", ok=True, run_id="source-run")]

    monkeypatch.setattr(followup, "build_subagent_recovery_strategy", fake_strategy)
    monkeypatch.setattr(followup, "make_action_apply_records", fake_action_apply)
    monkeypatch.setattr(followup, "run_dispatch_runner_stage", fake_run_dispatch_runner_stage)

    records = followup.run_post_runner_recovery_followup(
        followup.PostRunnerRecoveryFollowupParams(agent, ctx, params, [runner_failure])
    )

    assert captured == {
        "include_run_ids": ["source-run"],
        "runner_instruction": "读取 latest_continue_packet.json 接续写完 HTML。",
    }
    assert records[-1].ok is True


def test_post_runner_recovery_followup_skips_when_runner_did_not_fail(monkeypatch):
    calls: list[str] = []
    ctx = SimpleNamespace()
    params = SimpleNamespace(apply=True, execute_runners=True)

    monkeypatch.setattr(followup, "make_action_apply_records", lambda params: calls.append("called"))

    records = followup.run_post_runner_recovery_followup(
        followup.PostRunnerRecoveryFollowupParams(object(), ctx, params, [Record(step="runner", action="execute_runner")])
    )

    assert records == [Record(step="runner", action="execute_runner")]
    assert calls == []
