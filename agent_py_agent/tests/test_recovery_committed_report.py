"""只读恢复报告：合成回调提交真实canonical，不代作真实P2/权限测试。"""

from __future__ import annotations

import pytest

from agent_py_agent.agent.backends.bounded_call import BoundedCallExitUnconfirmedError
from agent_py_agent.agent.common.request_execution_ports import (
    RequestExecutionContextUnavailable,
    RequestExecutionOutcomeUncertainError,
)
from agent_py_agent.agent.common.tool_write_context import ToolWriteContextUnavailable
from agent_py_agent.agent.gateway_parts import recovery
from agent_py_agent.agent.gateway_parts.io import (
    GATEWAY_REQUEST_FINGERPRINT_SCHEMA,
    gateway_request_fingerprint,
    write_json_file_atomic,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.runtime_errors import runtime_error_report


# LLM: 隔离输入通过原writer保存；只给报告提供事实，不构造P2/P3许可或工具操作。
# 函数用途: 为只读报告验证准备身份完整的临时processing请求。
def _case(tmp_path):
    paths = gateway_paths_from_root(tmp_path / "gateway")
    recovery._ensure_recovery_dirs(paths)
    request = {
        "id": "report-one",
        "kind": "ask",
        "goal": "合成报告",
        "source": "synthetic",
        "metadata": {},
        "execution_attempt_id": "attempt-one",
        "lease_owner": "attempt-one",
        "lease_epoch": 1,
        "status": "processing",
    }
    request["request_fingerprint_schema"] = GATEWAY_REQUEST_FINGERPRINT_SCHEMA
    request["request_fingerprint"] = gateway_request_fingerprint(request, request["id"])
    path = paths.processing / (request["id"] + ".json")
    write_json_file_atomic(path, request)
    return paths, path, request


@pytest.mark.parametrize("kind", ["outcome", "context", "tool_context", "bounded"])
def test_actual_transport_types_keep_structured_state(tmp_path, kind):
    _, _, request = _case(tmp_path)
    error = {
        "outcome": RequestExecutionOutcomeUncertainError(request, "post"),
        "context": RequestExecutionContextUnavailable("source required"),
        "tool_context": ToolWriteContextUnavailable("claim required"),
        "bounded": BoundedCallExitUnconfirmedError("call-one", "worker_exit_unconfirmed"),
    }[kind]
    row = runtime_error_report(error, context="gateway.recovery.request")
    assert row["category"] in {"state", "contract"} and row["error_code"] == error.error_code
    assert row["recoverable"] is False and row["error_type"] == type(error).__name__
    if kind == "bounded":
        assert row["call_id"] == "call-one" and "operation_entered" not in row
    if kind == "outcome":
        assert (
            row["execution_attempt_id"] == request["execution_attempt_id"]
            and row["operation_entered"] is False
        )


def test_code_spoof_does_not_acquire_typed_state():
    error = RuntimeError("ordinary")
    error.error_code = "ACTIVE_TURN_OUTCOME_UNCERTAIN"
    row = runtime_error_report(error, context="gateway.recovery.request")
    assert row["category"] == "programmer_bug" and "operation_entered" not in row


@pytest.mark.parametrize("ok", [False, True])
def test_actual_canonical_count_survives_post_refusal(tmp_path, monkeypatch, ok):
    paths, path, request = _case(tmp_path)
    error = RequestExecutionOutcomeUncertainError(request, "post_input")

    def commit_then_refuse(*_):
        response = {"id": request["id"], "ok": ok, "status": "done" if ok else "failed"}
        terminal = recovery._terminal_gateway_request_payload(request["id"], request, response)
        recovery._commit_gateway_terminal_request(
            paths, path, paths.done if ok else paths.failed, request["id"], terminal
        )
        write_json_file_atomic(
            recovery._terminal_projection_marker_path(paths, request["id"]),
            {"settlement_pending": True},
        )
        raise error

    monkeypatch.setattr(recovery, "_recover_one_processing_request", commit_then_refuse)
    report = recovery.recover_gateway_processing_requests_report(paths)
    assert report.summary["archived" if ok else "failed"] == 1
    assert len(report.load_errors) == 1
    row = report.load_errors[0]
    assert row["error_code"] == error.error_code and row["category"] == "state"
    assert row["terminal_committed"] is True and row["settlement_pending"] is True
    assert not path.exists() and (paths.terminal / path.name).exists()
    assert not (paths.root / "admission").exists()


@pytest.mark.parametrize(
    "damage", ["missing", "wrong_id", "wrong_fingerprint", "wrong_schema", "missing_response"]
)
def test_unrelated_or_corrupt_canonical_never_counts(tmp_path, monkeypatch, damage):
    paths, path, request = _case(tmp_path)

    def refuse(*_):
        if damage != "missing":
            row = recovery._terminal_gateway_request_payload(
                request["id"], request, {"id": request["id"], "ok": False}
            )
            if damage == "wrong_id":
                row["id"] = "another"
            elif damage == "wrong_fingerprint":
                row["goal"] = "other input"
            elif damage == "wrong_schema":
                row["schema_version"] = "other.v1"
            else:
                row.pop("terminal_response")
            write_json_file_atomic(paths.terminal / path.name, row)
        raise RequestExecutionOutcomeUncertainError(request, "pre")

    monkeypatch.setattr(recovery, "_recover_one_processing_request", refuse)
    before = path.read_bytes()
    report = recovery.recover_gateway_processing_requests_report(paths)
    assert (
        report.summary["failed"] == report.summary["archived"] == 0 and len(report.load_errors) == 1
    )
    assert (
        path.read_bytes() == before and report.load_errors[0].get("terminal_committed") is not True
    )


@pytest.mark.parametrize("stage", ["canonical", "marker"])
def test_observation_read_failure_preserves_original_error(tmp_path, monkeypatch, stage):
    paths, path, request = _case(tmp_path)
    canonical = paths.terminal / path.name
    marker = recovery._terminal_projection_marker_path(paths, request["id"])
    row = recovery._terminal_gateway_request_payload(
        request["id"], request, {"id": request["id"], "ok": False}
    )
    write_json_file_atomic(canonical, row)
    write_json_file_atomic(marker, {"settlement_pending": True})
    original = RequestExecutionOutcomeUncertainError(request, "original-post-commit")
    read = recovery.read_json_file_report
    fault_path = canonical if stage == "canonical" else marker

    def read_with_failure(target, **kwargs):
        if target == fault_path:
            raise OSError("synthetic observation read failure")
        return read(target, **kwargs)

    def refuse(*_):
        raise original

    monkeypatch.setattr(recovery, "read_json_file_report", read_with_failure)
    monkeypatch.setattr(recovery, "_recover_one_processing_request", refuse)
    before = {p: p.read_bytes() for p in (canonical, marker, path)}
    result = recovery.recover_gateway_processing_requests_report(paths)
    assert result.summary["failed"] == result.summary["archived"] == 0
    assert len(result.load_errors) == 1
    report = result.load_errors[0]
    assert report["error_type"] == type(original).__name__
    assert report["error_code"] == original.error_code
    assert report["execution_attempt_id"] == original.execution_attempt_id
    assert report["phase"] == original.phase
    assert report.get("terminal_committed") is not True
    assert all(p.read_bytes() == content for p, content in before.items())
