"""结构化 stdout 截断门必须在 JSON 解析之前；stderr 截断不污染正常 stdout。"""
from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability import pack_verifier_runner as runner


def _outcome(stdout, *, stdout_truncated=False, stderr_truncated=False, capture=None):
    result = subprocess.CompletedProcess(["checker"], 0, stdout, "")
    result.capture = {"streams_truncated": {"stdout": stdout_truncated, "stderr": stderr_truncated},
                      "bytes_discarded": {"stdout": int(stdout_truncated), "stderr": int(stderr_truncated)}}
    result.capture.update(capture or {})
    return result, 0.01, ""


def test_truncated_valid_json_is_rejected_without_parsing(monkeypatch):
    payload = json.dumps({"schema": "pack_verifier_result.v1", "valid": True})
    calls, original = [], runner._parsed_result

    def parse(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(runner, "_parsed_result", parse)
    result = runner._run_result(_outcome(payload, stdout_truncated=True), SimpleNamespace(timeout_seconds=2),
                                {"verifier_id": "check", "package_id": "p"})
    assert result.status == "error" and result.reason_code == "verifier_output_invalid"
    assert calls == [], "截断 JSON 不能进解析器，即便保留前缀本身是合法 JSON"


@pytest.mark.parametrize("stderr_truncated", [False, True])
def test_small_stdout_is_parsed_even_when_stderr_truncated(stderr_truncated):
    payload = json.dumps({"schema": "pack_verifier_result.v1", "valid": True})
    result = runner._run_result(_outcome(payload, stderr_truncated=stderr_truncated), SimpleNamespace(timeout_seconds=2),
                                {"verifier_id": "check", "package_id": "p"})
    assert result.status == "passed" and result.valid is True


def test_one_mib_parser_guard_is_preserved():
    prefix = json.dumps({"schema": "pack_verifier_result.v1", "valid": True})
    for total, status in ((1024 * 1024, "passed"), (1024 * 1024 + 1, "error")):
        payload = prefix + " " * (total - len(prefix))
        result = runner._run_result(_outcome(payload), SimpleNamespace(timeout_seconds=2),
                                    {"verifier_id": "check", "package_id": "p"})
        assert result.status == status
        if status == "error":
            assert result.reason_code == "verifier_output_invalid"


@pytest.mark.parametrize("capture", [{"streams_invalid_utf8": {"stdout": True}},
                                  {"errors": {"stdout": "OSError"}}])
def test_corrupt_stdout_capture_is_rejected_before_parsing(monkeypatch, capture):
    payload = json.dumps({"schema": "pack_verifier_result.v1", "valid": True,
                          "warnings": [{"code": "w\ufffd1"}]})
    calls, original = [], runner._parsed_result

    def parse(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(runner, "_parsed_result", parse)
    result = runner._run_result(_outcome(payload, capture=capture), SimpleNamespace(timeout_seconds=2),
                                {"verifier_id": "check", "package_id": "p"})
    assert result.status == "error" and result.reason_code == "verifier_output_invalid"
    assert result.valid is None and result.warning_counts == {} and result.error_counts == {}
    assert calls == [], "stdout格式已坏/读取失败，不得靠解析器碰巧失败"


@pytest.mark.parametrize("capture", [{"streams_invalid_utf8": {"stderr": True}},
                                  {"errors": {"stderr": "OSError"}}])
def test_corrupt_stderr_does_not_invalidate_sound_stdout(capture):
    payload = json.dumps({"schema": "pack_verifier_result.v1", "valid": True,
                          "warnings": [{"code": "w\ufffd1"}]})
    result = runner._run_result(_outcome(payload, capture=capture), SimpleNamespace(timeout_seconds=2),
                                {"verifier_id": "check", "package_id": "p"})
    assert result.status == "passed" and result.valid is True and result.warning_counts == {"w\ufffd1": 1}


def test_stdout_without_eof_cannot_pass_even_when_prefix_is_valid_json(monkeypatch):
    payload = json.dumps({'schema': 'pack_verifier_result.v1', 'valid': True})
    monkeypatch.setattr(runner, '_parsed_result', lambda *_: pytest.fail('incomplete stdout must not be parsed'))
    result = runner._run_result(
        _outcome(payload, capture={'streams_eof': {'stdout': False, 'stderr': True}}),
        SimpleNamespace(timeout_seconds=2), {'verifier_id': 'check', 'package_id': 'p'},
    )
    assert result.status == 'error' and result.reason_code == 'verifier_output_invalid'
    assert result.valid is None
