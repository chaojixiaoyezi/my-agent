"""监督工具结算的分类入口与五类失败回执。"""

from __future__ import annotations

import ast
import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from agent_py_agent.agent.tooling import tool_operation_coordinator as coordinator
from agent_py_agent.agent.tooling.models import BaseTool, ToolHandlerOutcome
from agent_py_agent.tests._tool_runtime_harness import (
    execute_canonical_test_call,
    make_test_model_spec,
    make_test_runtime_policy,
)
from agent_py_agent.tests.test_conveyor_m33_operation_status import ledger as ledger

_ROOT = Path(__file__).resolve().parents[1] / "agent"
_AUTHORITY = _ROOT / "contracts" / "effect_settlement.py"
_AUTHORITY_NAMES = {
    "classify_effect_settlement", "normalize_legacy_operation_status", "operation_status_for_settlement",
}
_RETIRED_NAMES = {"_is_deterministic_pre_handler_failure", "_PRE_HANDLER_DETERMINISTIC_CODES"}


def _settlement_duplicates(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in _RETIRED_NAMES:
            yield node.lineno, node.id
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in _AUTHORITY_NAMES | _RETIRED_NAMES:
            yield node.lineno, node.name
            continue
        descendants = tuple(ast.walk(node))
        names = {item.id for item in descendants if isinstance(item, ast.Name)}
        if names & _AUTHORITY_NAMES:
            continue
        inputs = names | {item.arg for item in descendants if isinstance(item, ast.arg)}
        inputs |= {item.attr for item in descendants if isinstance(item, ast.Attribute)}
        inputs |= {
            item.args[0].value for item in descendants
            if isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute)
            and item.func.attr == "get" and item.args and isinstance(item.args[0], ast.Constant)
            and isinstance(item.args[0].value, str)
        }
        returns = tuple(
            ret.value for ret in descendants if isinstance(ret, ast.Return)
            and isinstance(ret.value, (ast.Constant, ast.Name, ast.Attribute, ast.IfExp))
        )
        outputs = {
            item.value for ret in returns for item in ast.walk(ret)
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        }
        outputs |= {
            item.id.removeprefix("TOOL_OPERATION_").lower()
            for ret in returns for item in ast.walk(ret)
            if isinstance(item, ast.Name) and item.id.startswith("TOOL_OPERATION_")
        }
        if {"effect_outcome", "handler_executed", "status"} <= inputs and "unknown" in outputs and (
            {"failed", "not_started", "succeeded", "applied", "partial"} & outputs
        ):
            yield node.lineno, node.name


def test_effect_settlement_has_one_classifier():
    findings = []
    for path in sorted(_ROOT.rglob("*.py")):
        if path == _AUTHORITY:
            continue
        tree = ast.parse(path.read_bytes(), filename=str(path))
        findings.extend(f"{path.relative_to(_ROOT)}:{line}: {name}" for line, name in _settlement_duplicates(tree))
    assert not findings, findings


def test_guard_detects_a_classifier_with_a_different_name():
    tree = ast.parse("""
def status_from_receipt(receipt):
    if not receipt.handler_executed and receipt.effect_outcome == "not_started":
        return "failed"
    return "succeeded" if receipt.status == "succeeded" else "unknown"
""")
    assert list(_settlement_duplicates(tree)) == [(2, "status_from_receipt")]


class _WriteTool(BaseTool):
    model_spec = make_test_model_spec("fixture_write", input_schema={
        "type": "object", "properties": {"value": {"type": "integer"}},
        "required": ["value"], "additionalProperties": False,
    })
    runtime_policy = make_test_runtime_policy("mutating")

    def __init__(self, failure):
        self.failure = failure
        self.invocations = 0

    def execute(self, params):
        self.invocations += 1
        if self.failure == "handler":
            raise RuntimeError("simulated handler error")
        if self.failure == "known_failure":
            return ToolHandlerOutcome("fixture_write", False, "known result", effect_outcome="failed")
        return ToolHandlerOutcome("fixture_write", True, "completed")


@pytest.mark.parametrize(
    "failure", ["schema", "admission", "permission", "handler", "post_execution", "known_failure"],
)
def test_failed_ledger_rows_carry_an_explicit_effect(ledger, tmp_path, monkeypatch, failure):
    repo, run, store, *_ = ledger
    tool = _WriteTool(failure)
    attach = coordinator._attach_operation_facts

    @contextmanager
    def deny_admission(_label):
        yield False

    def fail_projection(*args, action, **options):
        if action == "executed":
            raise OSError("simulated receipt construction error")
        return attach(*args, action=action, **options)

    def deny_permission(_call):
        return ToolHandlerOutcome("fixture_write", False, "permission denied",
                                  error_code="TOOL_PERMISSION_DENIED")

    if failure == "admission":
        monkeypatch.setattr(coordinator, "tool_execution_admission", deny_admission)
    if failure == "post_execution":
        monkeypatch.setattr(coordinator, "_attach_operation_facts", fail_projection)
    execution = execute_canonical_test_call(
        tmp_path, tools={"fixture_write": tool}, tool_name="fixture_write",
        arguments={} if failure == "schema" else {"value": 1},
        run_id="run-m33", attempt_id=run["attempt_id"], operation_store=store,
        operation_store_required=True, operation_owner_id="owner-m33",
        write_boundary={"task_id": "task-m33"},
        pre_handler_gate=deny_permission if failure == "permission" else None,
    )
    early = failure in {"schema", "admission", "permission"}
    assert tool.invocations == (0 if early else 1)
    assert execution.result.effect_outcome == ("not_started" if early else "failed" if failure == "known_failure" else "unknown")
    with repo._runtime_connection() as conn:
        rows = conn.execute("SELECT status, outcome_json FROM tool_operations").fetchall()
    if early:
        assert not rows
    else:
        assert len(rows) == 1
        receipt = json.loads(rows[0]["outcome_json"])["result"]
        assert rows[0]["status"] == ("FAILED" if failure == "known_failure" else "UNKNOWN")
        assert receipt["handler_executed"] is True
        assert receipt["effect_outcome"] == execution.result.effect_outcome
    for row in rows:
        if row["status"] == "FAILED":
            assert json.loads(row["outcome_json"])["result"]["effect_outcome"] in {"not_started", "failed"}
