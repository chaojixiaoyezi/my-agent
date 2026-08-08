#!/usr/bin/env python3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_env_loader import ensure_model_key

from __future__ import annotations

"""Run ordinary-Chinese tool acceptance against the configured real provider.

The harness uses the production SimpleAgent, provider adapter, ToolExecutor and
operation store.  It records only bounded runtime evidence and never writes or
prints the API key.
"""

import argparse
import json
import platform
import shlex
import subprocess
import sys
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

_CASES = (
    (
        "T-TOOL-REAL-001",
        "在项目里运行pytest -q，把失败原因告诉我。",
        "execute_or_structured_block",
    ),
    ("T-TOOL-REAL-002", "怎么运行pytest？", "informational"),
    (
        "T-TOOL-REAL-003",
        "文档里写着rm -rf /，解释这是什么意思。",
        "informational",
    ),
    (
        "T-USER-001",
        "已经联系印度方进行查杀和防火墙block\t态势感知恶意软件告警(SOC推送监控)",
        "informational",
    ),
)

_CALL_FIELDS = (
    "call_id",
    "tool",
    "parameters",
    "source_protocol",
    "schema_hash",
    "turn_id",
    "attempt_id",
    "required_action_id",
    "operation_id",
    "idempotency_key",
    "status",
    "ok",
    "error_code",
    "error_category",
    "retryable",
    "handler_executed",
    "failure_stage",
    "duration_ms",
    "effect_outcome",
    "effect_source_ref",
    "tool_operation_status",
    "tool_operation_attempt_count",
    "tool_operation_replayed",
    "result_ref",
    "artifact_ref",
    "output_chars",
    "output_bytes",
    "output_sha256",
    "output_preview",
    "tool_result_refs",
    "tool_result_envelope",
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run my-agent tool acceptance with the real configured model."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=REPO_ROOT / "agent_py_agent" / "config" / "agent_config.yaml",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=REPO_ROOT / "validation" / "real_runs",
    )
    parser.add_argument("--no-stream", action="store_true")
    return parser.parse_args()


def _create_fixture(run_root: Path) -> Path:
    workspace = run_root / "fixture-project"
    tests = workspace / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    (workspace / "pytest.ini").write_text(
        "[pytest]\ntestpaths = tests\n",
        encoding="utf-8",
    )
    (tests / "test_fixture.py").write_text(
        "def test_fixture_passes():\n    assert 2 + 2 == 4\n",
        encoding="utf-8",
    )
    return workspace


def _make_agent(args: argparse.Namespace, run_root: Path, workspace: Path):
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import load_config

    base = load_config(args.config)
    config = replace(
        base,
        workspace_root=str(workspace),
        my_agent_home=str(run_root / "my-agent-home"),
        stream_enabled=not args.no_stream,
        tool_protocol="native",
        enable_tools=True,
        enable_subagents=False,
        auto_save_memory=False,
        home_context_enabled=False,
        memory_curator_enabled=False,
        run_task_workspace_enabled=False,
        max_tokens=min(4096, max(1024, int(base.max_tokens or 0))),
    )
    return SimpleAgent(config, workspace, workspace_roots=[workspace])


def _model_call_evidence(agent: object, run_id: str) -> list[dict[str, object]]:
    ledger = getattr(agent, "_model_call_ledger", None)
    records = ledger.records() if ledger is not None else ()
    evidence: list[dict[str, object]] = []
    for record in records:
        if str(getattr(record, "run_id", "") or "") != run_id:
            continue
        payload = record.to_dict()
        evidence.append(
            {
                key: payload.get(key)
                for key in (
                    "call_id",
                    "backend",
                    "model",
                    "request_id",
                    "run_id",
                    "status",
                    "input_tokens",
                    "output_tokens",
                    "provider_attempt_count",
                    "provider_attempts",
                    "events",
                    "metadata",
                )
            }
        )
    return evidence


def _tool_call_evidence(records: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {key: record[key] for key in _CALL_FIELDS if key in record}
        for record in records
        if isinstance(record, dict)
    ]


def _case_metrics(result: object) -> dict[str, object]:
    records = [
        item
        for item in list(getattr(result, "archive_tool_calls", None) or [])
        if isinstance(item, dict)
    ]
    operations = [item for item in records if str(item.get("operation_id") or "")]
    return {
        "canonical_tool_call_count": len(records),
        "canonical_tool_result_count": len(records),
        "handler_execution_count": sum(item.get("handler_executed") is True for item in records),
        "operation_count": len(operations),
        "tool_calls_and_results": _tool_call_evidence(records),
    }


def _informational_findings(case: dict[str, object]) -> list[str]:
    findings: list[str] = []
    runtime = case["tool_runtime_evidence"]
    metrics = case["metrics"]
    assessment = runtime.get("required_action_assessment") or {}
    if assessment.get("requires_action") is not False:
        findings.append("required_action_assessment_not_informational")
    choices = runtime.get("tool_choices") or []
    if not choices or any(item.get("mode") != "none" for item in choices):
        findings.append("tool_choice_not_none")
    for key in (
        "canonical_tool_call_count",
        "canonical_tool_result_count",
        "handler_execution_count",
        "operation_count",
    ):
        if int(metrics.get(key) or 0) != 0:
            findings.append(f"unexpected_{key}")
    completion = runtime.get("completion_gate") or {}
    if completion.get("completed") is not True:
        findings.append("completion_gate_not_complete")
    return findings


def _execution_findings(case: dict[str, object]) -> list[str]:
    runtime = case["tool_runtime_evidence"]
    metrics = case["metrics"]
    assessment = runtime.get("required_action_assessment") or {}
    findings: list[str] = []
    if assessment.get("requires_action") is not True:
        findings.append("required_action_assessment_not_executable")
    calls = metrics.get("tool_calls_and_results") or []
    pytest_calls = [
        item
        for item in calls
        if item.get("tool") == "run_command"
        and "pytest" in str((item.get("parameters") or {}).get("command") or "")
        and "-q" in str((item.get("parameters") or {}).get("command") or "")
    ]
    completion = runtime.get("completion_gate") or {}
    structured_block = completion.get("status") in {
        "blocked",
        "unfinished",
        "approval_required",
        "needs_user_input",
    }
    if not pytest_calls and not structured_block:
        findings.append("no_real_pytest_call_or_structured_block")
    if pytest_calls and int(metrics.get("canonical_tool_result_count") or 0) < 1:
        findings.append("canonical_tool_result_missing")
    return findings


def _run_case(
    agent: object,
    *,
    case_id: str,
    user_input: str,
    expected: str,
    command: str,
) -> dict[str, object]:
    run_id = f"real-tool-{case_id.lower()}-{uuid.uuid4().hex[:10]}"
    result = agent.run(
        user_input,
        request_id=run_id,
        run_id=run_id,
        source="tool_real_acceptance",
        allowed_tools=["run_command"],
        save=False,
    )
    runtime = dict(getattr(result, "tool_runtime_evidence", None) or {})
    case: dict[str, object] = {
        "test_id": case_id,
        "original_input": user_input,
        "expected": expected,
        "exact_test_command": command,
        "run_id": run_id,
        "backend": str(getattr(result, "backend", "") or ""),
        "runtime_status": str(getattr(result, "runtime_status", "") or ""),
        "runtime_reason": str(getattr(result, "runtime_reason", "") or ""),
        "runtime_source": str(getattr(result, "runtime_source", "") or ""),
        "final_answer": str(getattr(result, "response", "") or ""),
        "tool_runtime_evidence": runtime,
        "metrics": _case_metrics(result),
        "operation_verification": dict(getattr(result, "operation_verification", None) or {}),
        "model_calls": _model_call_evidence(agent, run_id),
    }
    findings = (
        _informational_findings(case) if expected == "informational" else _execution_findings(case)
    )
    if not case["final_answer"]:
        findings.append("final_answer_empty")
    case["findings"] = findings
    case["passed"] = not findings
    case["exit_code"] = 0 if case["passed"] else 1
    return case


def _git_head() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


def main() -> int:
    args = _parse_args()
    if not ensure_model_key():
        print("AGENT_API_KEY is required; no fake fallback is allowed.", file=sys.stderr)
        return 2
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_root = args.output_root.expanduser().resolve() / f"tool-runtime-{timestamp}"
    run_root.mkdir(parents=True, exist_ok=False)
    workspace = _create_fixture(run_root)
    agent = _make_agent(args, run_root, workspace)
    exact_command = " ".join(shlex.quote(item) for item in [sys.executable, *sys.argv])
    cases: list[dict[str, object]] = []
    for case_id, user_input, expected in _CASES:
        try:
            case = _run_case(
                agent,
                case_id=case_id,
                user_input=user_input,
                expected=expected,
                command=exact_command,
            )
        except Exception as exc:  # noqa: BLE001 - evidence must survive one failed case
            case = {
                "test_id": case_id,
                "original_input": user_input,
                "expected": expected,
                "exact_test_command": exact_command,
                "passed": False,
                "exit_code": 1,
                "findings": [f"{type(exc).__name__}: {exc}"],
            }
        cases.append(case)
        print(
            f"{case_id}: {'PASS' if case.get('passed') else 'FAIL'}",
            flush=True,
        )
    report = {
        "schema_version": "tool-real-acceptance.v1",
        "created_at": datetime.now(UTC).isoformat(),
        "git_head": _git_head(),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "workspace": str(workspace),
            "home": str(run_root / "my-agent-home"),
            "config": str(args.config.expanduser().resolve()),
            "api_key_present": True,
            "api_key_recorded": False,
        },
        "exact_test_command": exact_command,
        "cases": cases,
        "passed": all(case.get("passed") is True for case in cases),
    }
    report_path = run_root / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"report={report_path}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
