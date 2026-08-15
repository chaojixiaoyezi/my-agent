"""测试子代理真实验收执行记录模型。"""

from agent_py_agent.agent.subagents.models import TestExecutionRecord


def test_test_execution_record_serializes_core_fields():
    record = TestExecutionRecord(
        test_name="focused pytest",
        command="python -m pytest -q tests/test_demo.py",
        executed=True,
        exit_code=0,
        stdout="ok",
        stderr="",
        duration_seconds=1.25,
        executed_at="2026-05-08T12:00:00Z",
        validation_method="command",
        validation_result={"ok": True, "exit_code": 0},
        metadata={"run_id": "run-1"},
    )

    payload = record.to_dict()
    restored = TestExecutionRecord.from_dict(payload)

    assert payload["test_name"] == "focused pytest"
    assert payload["validation_result"] == {"ok": True, "exit_code": 0}
    assert payload["metadata"] == {"run_id": "run-1"}
    assert restored == record


def test_test_execution_record_truncates_large_stdout_and_stderr():
    stdout = "a" * 4100
    stderr = "b" * 4101

    record = TestExecutionRecord(stdout=stdout, stderr=stderr)

    assert len(record.stdout) == TestExecutionRecord.MAX_CAPTURE_CHARS
    assert record.stdout == stdout[-TestExecutionRecord.MAX_CAPTURE_CHARS:]
    assert len(record.stderr) == TestExecutionRecord.MAX_CAPTURE_CHARS
    assert record.stderr == stderr[-TestExecutionRecord.MAX_CAPTURE_CHARS:]


def test_test_execution_record_passed_requires_execution_and_ok_result():
    passed = TestExecutionRecord(
        test_name="unit",
        executed=True,
        exit_code=0,
        validation_result={"ok": True},
    )
    failed = TestExecutionRecord(
        test_name="unit",
        executed=True,
        exit_code=1,
        validation_result={"ok": False},
    )
    not_executed = TestExecutionRecord(
        test_name="unit",
        executed=False,
        exit_code=0,
        validation_result={"ok": True},
    )

    assert passed.passed is True
    assert failed.passed is False
    assert not_executed.passed is False
