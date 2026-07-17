"""测试子代理真实验收执行器。"""

from pathlib import Path

from agent_py_agent.agent.subagents.models import TestExecutor


def test_test_executor_runs_allowed_command_and_records_exit_code(tmp_path):
    executor = TestExecutor(tmp_path, timeout_seconds=10)

    record = executor.execute({
        "name": "python smoke",
        "validation_method": "command",
        "command": "python -c \"print('ok')\"",
    })

    assert record.executed is True
    assert record.exit_code == 0
    assert record.passed is True
    assert record.validation_method == "command"
    assert record.validation_result["ok"] is True
    assert "ok" in record.stdout
    assert record.duration_seconds >= 0
    assert record.executed_at


def test_test_executor_treats_pytest_validation_method_as_command(tmp_path):
    """runner may label pytest commands as validation_method=pytest."""
    (tmp_path / "test_solution.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    # Starting a nested pytest process takes about 6s on an idle macOS host and
    # can exceed 10s while the full suite is active; keep this integration check
    # bounded without making host load the behavior under test.
    executor = TestExecutor(tmp_path, timeout_seconds=30)

    record = executor.execute({
        "name": "pytest alias",
        "validation_method": "pytest",
        "command": "python3 -m pytest test_solution.py -q",
    })

    assert record.executed is True
    assert record.passed is True
    assert record.validation_method == "command"


def test_test_executor_runs_command_in_explicit_working_dir(tmp_path):
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    (package_dir / "test_smoke.py").write_text(
        "import unittest\n\n"
        "class TestSmoke(unittest.TestCase):\n"
        "    def test_ok(self):\n"
        "        self.assertTrue(True)\n",
        encoding="utf-8",
    )
    executor = TestExecutor(tmp_path, timeout_seconds=10)

    record = executor.execute({
        "name": "relative unittest",
        "validation_method": "command",
        "command": "python -m unittest discover -s . -p 'test_*.py'",
        "working_dir": "package",
    })

    assert record.passed is True
    assert record.validation_result["working_dir"] == str(package_dir.resolve())
    assert record.metadata["working_dir"] == str(package_dir.resolve())


def test_test_executor_blocks_working_dir_escape(tmp_path):
    executor = TestExecutor(tmp_path)

    record = executor.execute({
        "name": "escape",
        "validation_method": "command",
        "command": "python -c \"print('ok')\"",
        "working_dir": "../outside",
    })

    assert record.executed is False
    assert record.passed is False
    assert "working_dir 超出 workspace 边界" in record.error


def test_test_executor_blocks_high_risk_shell_characters(tmp_path):
    executor = TestExecutor(tmp_path)

    record = executor.execute({
        "name": "unsafe",
        "validation_method": "command",
        "command": "python -c \"print('ok')\"; echo unsafe",
    })

    assert record.executed is False
    assert record.passed is False
    assert record.validation_method == "command"
    assert "高风险 shell 字符" in record.error


def test_test_executor_file_check_records_existing_file_metadata(tmp_path):
    target = tmp_path / "result.json"
    target.write_text("{\"status\": \"success\"}", encoding="utf-8")
    executor = TestExecutor(tmp_path)

    record = executor.execute({
        "name": "result exists",
        "validation_method": "file_check",
        "file_path": "result.json",
    })

    assert record.executed is True
    assert record.passed is True
    assert record.command == ""
    assert record.validation_result["ok"] is True
    assert record.validation_result["exists"] is True
    assert record.validation_result["size"] == target.stat().st_size
    assert Path(record.validation_result["path"]).name == "result.json"


def test_test_executor_content_check_matches_literal_pattern(tmp_path):
    (tmp_path / "report.txt").write_text("alpha\nstatus: success\n", encoding="utf-8")
    executor = TestExecutor(tmp_path)

    record = executor.execute({
        "name": "report contains success",
        "validation_method": "content_check",
        "file_path": "report.txt",
        "content_pattern": "status: success",
    })

    assert record.executed is True
    assert record.passed is True
    assert record.validation_method == "content_check"
    assert record.validation_result["ok"] is True
    assert record.validation_result["matched"] is True


def test_test_executor_content_check_supports_explicit_negative_contains(tmp_path):
    (tmp_path / "page.html").write_text("<a href='index1.html'>首页</a>\n", encoding="utf-8")
    executor = TestExecutor(tmp_path)

    record = executor.execute({
        "name": "验证无index4.html引用",
        "validation_method": "content_check",
        "file_path": "page.html",
        "content_pattern": "index4.html",
        "match_mode": "not_contains",
    })

    assert record.executed is True
    assert record.passed is True
    assert record.validation_result["match_mode"] == "not_contains"
    assert record.validation_result["expect_absent"] is True


def test_test_executor_content_check_does_not_infer_negative_from_name(tmp_path):
    (tmp_path / "page.html").write_text("<a href='index1.html'>首页</a>\n", encoding="utf-8")
    executor = TestExecutor(tmp_path)

    record = executor.execute({
        "name": "验证无index4.html引用",
        "validation_method": "content_check",
        "file_path": "page.html",
        "content_pattern": "index4.html",
    })

    assert record.executed is True
    assert record.passed is False
    assert record.validation_result["match_mode"] == "contains"
    assert record.validation_result["expect_absent"] is False


def test_test_executor_content_check_supports_explicit_expect_absent(tmp_path):
    (tmp_path / "page.html").write_text("<a href='index1.html'>首页</a>\n", encoding="utf-8")
    executor = TestExecutor(tmp_path)

    record = executor.execute({
        "name": "检查 href=#",
        "validation_method": "content_check",
        "file_path": "page.html",
        "content_pattern": "href=\"#\"",
        "expect_absent": True,
    })

    assert record.executed is True
    assert record.passed is True
    assert record.validation_result["match_mode"] == "not_contains"
    assert record.validation_result["expect_absent"] is True


def test_test_executor_content_check_supports_exact_match(tmp_path):
    """LLM: Exact content checks keep closeout from accepting extra text."""
    (tmp_path / "proof.txt").write_text("context-lineage-ok\n", encoding="utf-8")
    executor = TestExecutor(tmp_path)

    record = executor.execute({
        "name": "proof exact",
        "validation_method": "content_check",
        "file_path": "proof.txt",
        "content_equals": "context-lineage-ok",
        "match_mode": "exact",
    })

    assert record.executed is True
    assert record.passed is False
    assert record.validation_result["match_mode"] == "exact"
    assert record.error == "内容不相等"


def test_test_executor_runs_artifact_integrity_check(tmp_path):
    (tmp_path / "index.html").write_text(
        "<!doctype html><html><body><a href='#hero'>首页</a><main id='hero'></main></body></html>",
        encoding="utf-8",
    )
    executor = TestExecutor(tmp_path)

    record = executor.execute({
        "name": "artifact integrity",
        "validation_method": "artifact_integrity",
        "file_path": "index.html",
    })

    assert record.executed is True
    assert record.passed is True
    assert record.validation_method == "artifact_integrity"
    assert record.validation_result["kind"] == "html"
    assert record.validation_result["blocker_codes"] == []
    assert record.validation_result["warning_codes"] == []


def test_test_executor_artifact_integrity_rejects_incomplete_html(tmp_path):
    (tmp_path / "index.html").write_text("<html><body><main>", encoding="utf-8")
    executor = TestExecutor(tmp_path)

    record = executor.execute({
        "name": "artifact integrity",
        "validation_method": "artifact_integrity",
        "file_path": "index.html",
    })

    assert record.executed is True
    assert record.passed is False
    assert "missing_body_close" in record.validation_result["blocker_codes"]
    assert "missing_html_close" in record.validation_result["blocker_codes"]
