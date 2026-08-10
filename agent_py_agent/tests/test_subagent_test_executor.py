"""测试子代理真实验收执行器(审计 R0:command 只保留 inert evidence)。"""

from pathlib import Path

from agent_py_agent.agent.subagents.models import TestExecutor


def test_test_executor_never_executes_command(tmp_path):
    """审计 R0:模型验收 command 永不执行——executed=False、ok=False、原文留档。"""
    executor = TestExecutor(tmp_path)

    record = executor.execute({
        "name": "python smoke",
        "validation_method": "command",
        "command": "python -c \"print('ok')\"",
    })

    assert record.executed is False
    assert record.passed is False
    assert record.exit_code != 0
    assert record.validation_method == "command"
    assert record.validation_result["ok"] is False
    assert record.validation_result["reason"] == "command_execution_disabled"
    # command 原文保留为 inert evidence,供审计回溯。
    assert record.validation_result["command"] == "python -c \"print('ok')\""
    assert record.command == "python -c \"print('ok')\""
    assert record.executed_at


def test_test_executor_treats_pytest_validation_method_as_inert_command(tmp_path):
    """pytest/unittest 标签的验收命令同样进 inert,不触发任何执行。"""
    (tmp_path / "test_solution.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    executor = TestExecutor(tmp_path)

    record = executor.execute({
        "name": "pytest alias",
        "validation_method": "pytest",
        "command": "python3 -m pytest test_solution.py -q",
    })

    assert record.executed is False
    assert record.passed is False
    assert record.validation_method == "command"
    assert record.validation_result["reason"] == "command_execution_disabled"


def test_test_executor_records_working_dir_as_inert_evidence(tmp_path):
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    executor = TestExecutor(tmp_path)

    record = executor.execute({
        "name": "relative unittest",
        "validation_method": "command",
        "command": "python -m unittest discover -s . -p 'test_*.py'",
        "working_dir": "package",
    })

    # command 不执行,working_dir 只作证据原样记录,不做路径解析/边界检查。
    assert record.executed is False
    assert record.passed is False
    assert record.validation_result["working_dir"] == "package"


def test_test_executor_does_not_resolve_working_dir_escape(tmp_path):
    """command 不执行后,working_dir 不再参与任何路径解析,越界值也仅留档。"""
    executor = TestExecutor(tmp_path)

    record = executor.execute({
        "name": "escape",
        "validation_method": "command",
        "command": "python -c \"print('ok')\"",
        "working_dir": "../outside",
    })

    assert record.executed is False
    assert record.passed is False
    assert record.validation_result["working_dir"] == "../outside"
    assert "超出可执行边界" not in record.error


def test_test_executor_records_high_risk_command_as_inert(tmp_path):
    """高风险 shell 字符不再需要拦截——命令整体不执行,原文留档。"""
    executor = TestExecutor(tmp_path)

    record = executor.execute({
        "name": "unsafe",
        "validation_method": "command",
        "command": "python -c \"print('ok')\"; echo unsafe",
    })

    assert record.executed is False
    assert record.passed is False
    assert record.validation_method == "command"
    assert "高风险" not in record.error
    assert record.validation_result["command"] == "python -c \"print('ok')\"; echo unsafe"


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
