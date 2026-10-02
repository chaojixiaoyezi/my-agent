"""文件语法观察只描述候选字节，不执行文件、不决定写入或任务是否成功。"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from agent_py_agent.agent.common.cancellation import ToolCancelled
from agent_py_agent.agent.tooling import file_syntax_diagnostics as diagnostics
from agent_py_agent.agent.tooling.models import ToolHandlerOutcome


# LLM: 纯解析测试使用完整内存字节流；真实同 fd 发布边界在文件工具测试验证。
# 函数用途: 取得一份候选观察，不创建文件或把解析结果冒充写入成功。
def observe(data: bytes, name: str = "value.json"):
    budget = diagnostics.FileSyntaxDiagnostics()
    return budget.observe_candidate(Path(name), io.BytesIO(data))


@pytest.mark.parametrize("value", [{}, [], "中文", 42, -2.5, True, False, None])
@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "utf-32"])
def test_json_accepts_all_top_level_values_and_original_encodings(value, encoding):
    result = observe(json.dumps(value, ensure_ascii=False).encode(encoding))
    assert result.status == "valid"
    assert result.code == "JSON_VALID"


def test_invalid_json_reports_positions_without_source_text():
    result = observe(b'{\n "secret-source": ]\n}')
    assert result.status == "invalid"
    assert (result.line, result.column, result.offset) == (2, 19, 20)
    assert "secret-source" not in json.dumps(result.to_dict())


@pytest.mark.parametrize("constant", [b"NaN", b"Infinity", b"-Infinity"])
def test_nonstandard_constants_are_not_reported_as_valid_json(constant):
    result = observe(constant)
    assert result.status == "invalid"
    assert result.code == "JSON_NONSTANDARD_CONSTANT"


def test_unknown_format_does_not_read_or_consume_a_check():
    class Unreadable(io.BytesIO):
        def read(self, _size=-1):
            raise AssertionError("未知格式不能读候选")

    budget = diagnostics.FileSyntaxDiagnostics()
    assert budget.observe_candidate(Path("future.custom"), Unreadable()) is None
    assert budget.bytes_read == budget.attempt_count == 0


def test_bounded_read_never_parses_truncated_json(monkeypatch):
    monkeypatch.setattr(diagnostics, "MAX_FILE_SYNTAX_BYTES", 4)
    monkeypatch.setattr(diagnostics, "MAX_CALL_SYNTAX_BYTES", 7)
    reads = []

    class Counted(io.BytesIO):
        def read(self, size=-1):
            reads.append(size)
            return super().read(size)

    budget = diagnostics.FileSyntaxDiagnostics()
    first = budget.observe_candidate(Path("one.json"), Counted(b"[1,2]"))
    second = budget.observe_candidate(Path("two.json"), Counted(b"[1]"))
    third = budget.observe_candidate(Path("three.json"), Counted(b"{}"))
    assert [first.status, second.status, third.status] == ["not_checked"] * 3
    assert reads == [5, 2]
    assert budget.bytes_read == 7


def test_exact_byte_limit_is_checked(monkeypatch):
    monkeypatch.setattr(diagnostics, "MAX_FILE_SYNTAX_BYTES", 2)
    result = observe(b"{}")
    assert result.status == "valid"


@pytest.mark.parametrize("error", [OSError("private detail"), RecursionError(), MemoryError()])
def test_observation_failures_are_not_checked_and_hide_exception_text(monkeypatch, error):
    def fail(_data):
        raise error

    monkeypatch.setattr(diagnostics, "_parse_json", fail)
    result = observe(b"{}")
    assert result.status == "not_checked"
    assert "private detail" not in json.dumps(result.to_dict())


def test_deep_json_is_not_misreported_as_syntax_invalid():
    result = observe(b"[" * 2000 + b"]" * 2000)
    # 新版 Python 可用迭代解析接受深嵌套；旧解析器遇到资源限制应未检查，不能误称语法无效。
    assert result.status in {"valid", "not_checked"}


@pytest.mark.parametrize("error", [ToolCancelled("stop"), KeyboardInterrupt(), SystemExit()])
def test_control_exceptions_are_never_soft_diagnostics(monkeypatch, error):
    def stop(_data):
        raise error

    monkeypatch.setattr(diagnostics, "_parse_json", stop)
    with pytest.raises(type(error)):
        observe(b"{}")


def test_count_and_feedback_limits_do_not_claim_complete_coverage(monkeypatch):
    monkeypatch.setattr(diagnostics, "MAX_SYNTAX_OBSERVATION_COUNT", 2)
    monkeypatch.setattr(diagnostics, "MAX_SYNTAX_FEEDBACK_CHARS", 280)
    budget = diagnostics.FileSyntaxDiagnostics()
    for index in range(5):
        budget.record(budget.observe_candidate(Path(f"{index}.json"), io.BytesIO(b"{}")))
    result = diagnostics.attach_syntax_diagnostics(ToolHandlerOutcome("apply_patch", True, "written"), budget)
    payload = result.result_envelope["syntax_diagnostics"]
    assert len(payload["observations"]) == 2
    assert payload["omitted_receipts"] == 3
    assert len(result.output) <= len("written\n") + 280
    assert "未附观察" in result.output


def test_feedback_render_failure_cannot_change_write_success(monkeypatch):
    budget = diagnostics.FileSyntaxDiagnostics()
    budget.record(budget.observe_candidate(Path("value.json"), io.BytesIO(b"{}")))

    def fail(_payload):
        raise RuntimeError("private")

    monkeypatch.setattr(diagnostics, "_render_feedback", fail)
    result = diagnostics.attach_syntax_diagnostics(ToolHandlerOutcome("write_file", True, "written"), budget)
    assert result.ok and result.effect_outcome == "" and result.error_code == ""
    assert "not_checked" in result.output and "private" not in result.output


def test_feedback_control_cancellation_is_not_converted_to_not_checked(monkeypatch):
    budget = diagnostics.FileSyntaxDiagnostics()
    budget.record(budget.observe_candidate(Path("value.json"), io.BytesIO(b"{}")))

    def stop(_payload):
        raise ToolCancelled("stop")

    monkeypatch.setattr(diagnostics, "_render_feedback", stop)
    with pytest.raises(ToolCancelled):
        diagnostics.attach_syntax_diagnostics(ToolHandlerOutcome("write_file", True, "written"), budget)


@pytest.mark.parametrize("method", ["record", "discard"])
@pytest.mark.parametrize("error", [ToolCancelled("stop"), KeyboardInterrupt(), SystemExit()])
def test_collection_control_exceptions_propagate(monkeypatch, method, error):
    def stop(*_args):
        raise error

    monkeypatch.setattr(diagnostics.FileSyntaxDiagnostics, method, stop)
    budget = diagnostics.FileSyntaxDiagnostics()
    options = {"deleted_path": Path("value.json")} if method == "discard" else {
        "observation": observe(b"{}"),
    }
    with pytest.raises(type(error)):
        diagnostics.update_syntax_diagnostics(budget, **options)
    assert not budget.unavailable


@pytest.mark.parametrize("error_type", [OSError, MemoryError])
def test_collection_failure_skips_further_parsing_and_collection(monkeypatch, error_type):
    budget = diagnostics.FileSyntaxDiagnostics()
    calls = []
    observation = observe(b"{}")

    def fail(*_args):
        calls.append("record")
        raise error_type("diagnostic failure")

    def forbidden(*_args):
        raise AssertionError("收集失效后不能继续解析或清理观察")

    monkeypatch.setattr(diagnostics.FileSyntaxDiagnostics, "record", fail)
    monkeypatch.setattr(diagnostics.FileSyntaxDiagnostics, "discard", forbidden)
    monkeypatch.setattr(diagnostics, "_parse_json", forbidden)
    diagnostics.update_syntax_diagnostics(budget, observation)
    diagnostics.update_syntax_diagnostics(budget, observation)
    diagnostics.update_syntax_diagnostics(budget, deleted_path=Path("value.json"))
    assert budget.observe_candidate(Path("value.json"), io.BytesIO(b"{}")) is None
    assert budget.unavailable and calls == ["record"] and budget.bytes_read == 0
