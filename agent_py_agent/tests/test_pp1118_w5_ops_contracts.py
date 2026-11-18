"""W5 运维合同回归单测：SQLite 损坏归类、治理工具说明、gateway_status 分页提示与重启开关注释对齐。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agent_py_agent.agent.conversation.wake_poison import _error_reason_code
from agent_py_agent.agent.runtime_errors import runtime_error_report
from agent_py_agent.agent.settings.parameter_registry import parameter_registry
from agent_py_agent.agent.tooling.gateway_status import GatewayStatusTool
from agent_py_agent.agent.tooling.input_schema import validate_tool_input
from agent_py_agent.agent.tooling.runtime_governance_tool import (
    WRITES,
    RuntimeGovernanceTool,
)
from agent_py_agent.tests.test_gateway_status_tool import _running_gateway


def test_malformed_sqlite_file_is_data_corruption_with_native_code(tmp_path: Path) -> None:
    error = sqlite3.DatabaseError("database disk image is malformed")
    error.sqlite_errorcode, error.sqlite_errorname = 11, "SQLITE_CORRUPT"
    report = runtime_error_report(error)
    assert report["category"] == "data_corruption" and report["recoverable"] is False
    assert report["sqlite_error_code"] == 11 and report["sqlite_error_name"] == "SQLITE_CORRUPT"
    assert _error_reason_code(error) == "error:data_corruption:DatabaseError"

    # 真实损坏库文件触发原生 SQLITE_NOTADB 同样落入 data_corruption。
    corrupt_db = tmp_path / "corrupt.sqlite3"
    corrupt_db.write_bytes(b"not a valid sqlite database header")
    conn = sqlite3.connect(corrupt_db)
    try:
        with pytest.raises(sqlite3.DatabaseError) as exc_info:
            conn.execute("SELECT * FROM sqlite_master")
    finally:
        conn.close()
    real_report = runtime_error_report(exc_info.value)
    assert real_report["category"] == "data_corruption" and real_report["recoverable"] is False
    assert isinstance(real_report.get("sqlite_error_code"), int)
    assert real_report.get("sqlite_error_name") == "SQLITE_NOTADB"
    assert _error_reason_code(exc_info.value) == "error:data_corruption:DatabaseError"

    # 无原生码的裸 DatabaseError 仍归类 data_corruption，不填充空码。
    plain_report = runtime_error_report(sqlite3.DatabaseError("plain"))
    assert plain_report["category"] == "data_corruption"
    assert "sqlite_error_code" not in plain_report and "sqlite_error_name" not in plain_report

    # 子类（编程错误、运行时库错、约束冲突）保持原分类（per test_wake_poison.py:198-199）。
    assert runtime_error_report(sqlite3.ProgrammingError("x"))["category"] == "programmer_bug"
    op_error = sqlite3.OperationalError("database is locked")
    op_error.sqlite_errorcode = 5
    assert runtime_error_report(op_error)["category"] == "programmer_bug"
    assert _error_reason_code(op_error) == "error:programmer_bug:OperationalError"
    assert runtime_error_report(sqlite3.IntegrityError("unique"))["category"] == "programmer_bug"


def test_gateway_status_metadata_only_reports_ignored_log_paging_without_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_py_agent.agent.gateway_parts import status_rendering

    agent, paths = _running_gateway(tmp_path)
    paths.log.write_text("line-1\nline-2\n", encoding="utf-8")
    monkeypatch.setattr(
        status_rendering,
        "gateway_log_page",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("metadata_only must not read log pages")),
    )
    monkeypatch.setattr(
        status_rendering,
        "gateway_log_diagnostics",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("metadata_only must not read log diagnostics")),
    )

    single = GatewayStatusTool(agent).execute({"metadata_only": True, "log_offset_bytes": 0})
    assert single.ok, single.output
    payload = json.loads(single.output)
    assert "log_pages" not in payload
    assert payload["ignored_parameters"] == ["log_offset_bytes"]
    assert payload["ignored_reason"] == "METADATA_ONLY_SUPPRESSES_LOG_PAGES"
    assert "log_pages.sources.<source>.lines" in payload["next_step"]

    multi = GatewayStatusTool(agent).execute(
        {
            "metadata_only": True,
            "log_until": 20.0,
            "log_cursor": "cursor-token",
            "log_offset_bytes": 0,
            "log_since": 10.0,
        }
    )
    assert multi.ok, multi.output
    multi_payload = json.loads(multi.output)
    assert "log_pages" not in multi_payload
    assert multi_payload["ignored_parameters"] == [
        "log_cursor",
        "log_offset_bytes",
        "log_since",
        "log_until",
    ]
    assert multi_payload["ignored_reason"] == "METADATA_ONLY_SUPPRESSES_LOG_PAGES"

    plain = GatewayStatusTool(agent).execute({"metadata_only": True})
    assert plain.ok, plain.output
    plain_payload = json.loads(plain.output)
    assert "ignored_parameters" not in plain_payload
    assert "ignored_reason" not in plain_payload

    props = GatewayStatusTool.model_spec.input_schema["properties"]
    for key in ("log_cursor", "log_offset_bytes"):
        assert "正文位于 log_pages.sources.<source>.lines" in props[key]["description"]


def test_runtime_governance_descriptions_and_examples_match_gates() -> None:
    spec = RuntimeGovernanceTool.model_spec
    assert "对账预览" in spec.description and "隔离唤醒预览" in spec.description
    assert "admin_authorized=true" in spec.description

    action_desc = spec.input_schema["properties"]["action"]["description"]
    for action_name in ("status", "reconcile_preview", "quarantined_wakes_preview", *WRITES):
        assert action_name in action_desc, f"action.description missing {action_name}"

    examples = [json.loads(raw) for raw in spec.hints.examples]
    for parsed in examples:
        validation = validate_tool_input(parsed, spec.input_schema)
        assert validation.ok, f"invalid example {parsed}: {validation.issues}"

    assert any(ex.get("action") == "reconcile_preview" and "cursor" in ex for ex in examples)
    assert any(ex.get("action") == "reconcile_preview" and "run_ids" in ex for ex in examples)
    assert any(ex.get("action") == "quarantined_wakes_preview" for ex in examples)


def test_enable_gateway_restart_tool_yaml_comment_matches_no_approval_contract() -> None:
    yaml_path = Path(__file__).resolve().parents[1] / "config" / "agent_config.yaml"
    yaml_text = yaml_path.read_text(encoding="utf-8")
    block = yaml_text.split("# 代理自助安排 Gateway 安全重启", 1)[1].split("enable_gateway_restart_tool:", 1)[0]
    assert "先弹确认" not in block
    assert "不会再弹审批" in block
    assert "本机管理员的主会话" in block
    assert "/restart" in block

    desc = parameter_registry()["enable_gateway_restart_tool"].description
    assert "先弹确认" not in desc
    assert "不会再弹审批" in desc
    assert "本机管理员的主会话" in desc
