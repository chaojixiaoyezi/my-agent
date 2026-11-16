"""W6 (D-11 option A): a network-off sandbox-local script that exits non-zero records a determined failure, and a
declared retry policy replays it within the same run; uncertain outcomes keep the original replay refusal."""
from __future__ import annotations

import json

import pytest

from agent_py_agent.agent.workflows import ScriptRunner, WorkflowEngine

# 只声明本地文件目标：脚本按非网络运行（未声明目标的脚本沿原规则默认开网络）。
_LOCAL = [{"target_id": "local", "type": "file", "endpoint": "workspace"}]


def _flaky(marker_dir):
    marker = marker_dir / "attempt-marker"
    return (f"import json, pathlib, sys\nmarker = pathlib.Path({str(marker)!r})\nseen = marker.exists()\n"
            "marker.write_text('1')\nif not seen:\n    sys.exit(9)\nprint(json.dumps({'value': 1}))\n")


def _engine(home, marker_dir):
    engine = WorkflowEngine(home)
    engine.runner = ScriptRunner(home, extra_write_roots=(marker_dir,))
    return engine


def _script(home, code, script_id="collect", **options):
    folder = home / "scripts" / script_id
    folder.mkdir(parents=True, exist_ok=True)
    metadata = {"id": script_id, "kind": "script", "name": "fixture", "owner_id": "alice", "data_sources": [],
                "access_targets": _LOCAL, "io_contract": {"outputs": ["value"]}, "entrypoint": "main.py",
                "interpreter": "python3", "timeout_seconds": 5, **options}
    (folder / "script.json").write_text(json.dumps(metadata))
    (folder / "main.py").write_text(code)


def _workflow(**step_options):
    step = {"step_id": "collect", "name": "collect", "mode": "deterministic_script", "script_id": "collect",
            "retry_policy": {"max_attempts": 2, "retryable_codes": ["SCRIPT_EXIT_NONZERO"]}, **step_options}
    return {"descriptor": {"id": "flaky", "kind": "workflow", "name": "flaky", "owner_id": "alice"}, "steps": [step]}


@pytest.mark.parametrize(("code", "options", "expected"), [
    ("import sys; sys.exit(1)", {}, "failed"),
    ("import sys; sys.exit(1)", {"access_targets": []}, "unknown"),
    ("import time; time.sleep(5)", {"timeout_seconds": 0.2}, "unknown"),
    ('print(\'{"value": 1}\')', {}, "confirmed"),
])
def test_runner_outcome_for_observed_exits(tmp_path, code, options, expected):
    _script(tmp_path, code, **options)
    result = ScriptRunner(tmp_path).run_script("collect", {})
    assert result.effect_outcome == expected


def test_script_with_data_sources_keeps_unknown(tmp_path):
    _script(tmp_path, "import sys; sys.exit(1)",
            data_sources=[{"source_id": "apps", "system": "logs", "query_scope": {"active_appnames": ["A"]}}])
    assert ScriptRunner(tmp_path).run_script("collect", {}).effect_outcome == "unknown"


def test_missing_entrypoint_is_not_started(tmp_path):
    _script(tmp_path, "print(1)")
    (tmp_path / "scripts" / "collect" / "main.py").unlink()
    assert ScriptRunner(tmp_path).run_script("collect", {}).effect_outcome == "not_started"


def test_declared_retry_replays_a_local_failure_within_the_run(tmp_path):
    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()
    _script(tmp_path, _flaky(marker_dir))
    run = _engine(tmp_path, marker_dir).run_workflow(_workflow())
    step = run.steps[0]
    assert run.status == "succeeded", (run.status, step.error_code, step.escalation_result)
    assert step.attempt_count == 2 and step.status == "succeeded"
    assert step.escalation_result["retry_history"][0]["error_code"] == "SCRIPT_EXIT_NONZERO"


def test_network_on_failure_still_refuses_replay(tmp_path):
    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()
    _script(tmp_path, _flaky(marker_dir), access_targets=[])
    run = _engine(tmp_path, marker_dir).run_workflow(_workflow(on_error="fail"))
    step = run.steps[0]
    assert step.attempt_count == 1 and step.status == "failed"
    assert step.escalation_result["retry_suppressed_reason"] == "UNCERTAIN_EFFECT_REPLAY_FORBIDDEN"


def test_exhausted_retries_still_escalate(tmp_path):
    _script(tmp_path, "import sys; sys.exit(3)")
    run = WorkflowEngine(tmp_path).run_workflow(_workflow())
    step = run.steps[0]
    assert step.attempt_count == 2 and step.status == "paused_for_resume"
