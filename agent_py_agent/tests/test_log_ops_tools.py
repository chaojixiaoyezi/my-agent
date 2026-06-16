
from __future__ import annotations

"""log_ops 6 个 LLM 工具的 schema/行为 + 存档查询 + poll 已读游标 + 不丢对账。

工具行为用确定性 store(直接 run_collection_cycle 灌数据)验证,不依赖真起 daemon 进程
(daemon 进程启停在 test_log_ops_daemon.py 用真子进程覆盖)。CI 友好、快、自包含。
"""

import json
from pathlib import Path

import pytest

from agent_py_agent.agent.tooling.log_ops.daemon import run_collection_cycle
from agent_py_agent.agent.tooling.log_ops.store import LogOpsStore, build_source_specs
from agent_py_agent.agent.tooling.log_ops.tools import (
    LOG_OPS_TOOL_NAMES,
    LogAlertPollTool,
    LogArchiveStatsTool,
    LogMonitorStartTool,
    LogMonitorStatusTool,
    LogMonitorStopTool,
    LogSourceQueryTool,
    log_ops_tools,
)


def _alert_line(idx: int, desc: str) -> str:
    return f"2026-06-16T00:00:{idx:02d} ALERT ALERT-{idx:06d} {desc}\n"


@pytest.fixture
def seeded(tmp_path: Path):
    """在 .log_ops/default 下灌好一批含 ALERT 的存档+候选,返回 (workspace_root, sid)。"""
    src = tmp_path / "secure.log"
    src.write_text(
        _alert_line(1, "reverse shell /dev/tcp/9.9.9.9/4444")
        + "2026-06-16T00:00:02 INFO ok\n"
        + _alert_line(3, "brute force ssh from 9.9.9.9")
        + _alert_line(4, "privilege escalation sudo su -"),
        encoding="utf-8",
    )
    store = LogOpsStore(tmp_path / ".log_ops", "default")
    store.write_config(build_source_specs([str(src)]), poll_interval_seconds=2.0)
    specs = build_source_specs([str(src)])
    run_collection_cycle(store, specs)
    return tmp_path, specs[0].source_id, str(src)


# ----------------------- schema 完整性 -----------------------


def test_all_tools_have_precise_schema_and_examples(tmp_path: Path) -> None:
    tools = log_ops_tools(tmp_path)
    assert {t.spec.name for t in tools} == set(LOG_OPS_TOOL_NAMES)
    for tool in tools:
        spec = tool.spec
        assert spec.category == "log_ops"
        assert spec.effect in {"read_only", "mutating"}
        assert spec.examples, spec.name
        # 每个声明的 required 参数都有精确 schema 片段。
        for req in spec.required_parameters:
            assert req in spec.parameter_schema, (spec.name, req)
        # 每个 parameter_schema 键都在 parameters 文档里。
        for key in spec.parameter_schema:
            assert key in spec.parameters, (spec.name, key)


def test_native_anthropic_schema_renders(tmp_path: Path) -> None:
    from agent_py_agent.agent.backends.tool_schema import tool_specs_to_anthropic_tools

    specs = [t.spec for t in log_ops_tools(tmp_path)]
    tools = tool_specs_to_anthropic_tools(specs)
    by_name = {t["name"]: t for t in tools}
    start = by_name["log_monitor_start"]
    assert start["input_schema"]["properties"]["sources"]["type"] == "array"
    assert start["input_schema"]["required"] == ["sources"]
    poll = by_name["log_alert_poll"]
    assert poll["input_schema"]["properties"]["limit"]["type"] == "integer"


# ----------------------- log_archive_stats / status 不丢对账 -----------------------


def test_archive_stats_reports_no_loss(seeded) -> None:
    workspace, sid, _src = seeded
    result = LogArchiveStatsTool(workspace).execute({})
    assert result.ok
    payload = json.loads(result.output)
    assert payload["no_loss"] is True
    assert payload["total_archive_file_lines"] == 4
    assert payload["total_candidates"] == 3
    assert sid in payload["per_source"]
    assert payload["per_source"][sid]["collected_lines"] == payload["per_source"][sid]["archive_file_lines"]
    # 规则库随统计一起暴露(便于模型知道初筛口径)。
    assert any(r["name"] == "reverse_shell" for r in payload["triage_rules"])


def test_status_reports_reconciliation_and_unread(seeded) -> None:
    workspace, _sid, _src = seeded
    result = LogMonitorStatusTool(workspace).execute({})
    assert result.ok
    payload = json.loads(result.output)
    assert payload["no_loss"] is True
    assert payload["candidates_total"] == 3
    assert payload["candidates_unread"] == 3  # 还没 poll 过
    # daemon 没真起 → 不存活,但对账仍可用(读的是落盘事实)。
    assert payload["daemon_alive"] is False


# ----------------------- log_alert_poll 已读游标 -----------------------


def test_alert_poll_advances_cursor_no_repeat(seeded) -> None:
    workspace, _sid, _src = seeded
    tool = LogAlertPollTool(workspace)

    first = json.loads(tool.execute({"limit": 2}).output)
    assert first["returned"] == 2
    assert first["remaining_unread"] == 1
    ids1 = [a["alert_id"] for a in first["alerts"]]

    second = json.loads(tool.execute({"limit": 10}).output)
    assert second["returned"] == 1  # 只剩 1 条新的
    ids2 = [a["alert_id"] for a in second["alerts"]]
    # 不重复:两次拿到的指纹不相交。
    assert set(ids1).isdisjoint(set(ids2))

    third = json.loads(tool.execute({}).output)
    assert third["returned"] == 0  # 读到队尾


def test_alert_poll_peek_does_not_advance(seeded) -> None:
    workspace, _sid, _src = seeded
    tool = LogAlertPollTool(workspace)
    peeked = json.loads(tool.execute({"peek": True, "limit": 5}).output)
    assert peeked["returned"] == 3
    assert peeked["cursor_after"] == 0  # peek 不推进
    # 之后正常 poll 仍能拿到全部 3 条。
    full = json.loads(tool.execute({"limit": 5}).output)
    assert full["returned"] == 3


def test_alert_poll_candidate_has_evidence_fields(seeded) -> None:
    workspace, _sid, _src = seeded
    payload = json.loads(LogAlertPollTool(workspace).execute({"limit": 1}).output)
    alert = payload["alerts"][0]
    for field in ("raw_line", "matched_rules", "source_id", "line_no", "fingerprint", "alert_id"):
        assert field in alert, field
    assert alert["raw_line"]  # 原始行全文在场(研判证据)


# ----------------------- log_source_query 确定性 grep -----------------------


def test_source_query_returns_real_matches(seeded) -> None:
    workspace, _sid, src = seeded
    tool = LogSourceQueryTool(workspace)
    # 按 locator 查 IP(交叉验证),命中 2 行(reverse shell + brute force 都带 9.9.9.9)。
    result = tool.execute({"source": src, "pattern": "9.9.9.9"})
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["matched"] == 2
    for m in payload["matches"]:
        assert "9.9.9.9" in m["line"]
        assert m["line_no"] >= 1


def test_source_query_by_alert_id(seeded) -> None:
    workspace, sid, _src = seeded
    # 也支持用 source_id 指定源。
    payload = json.loads(LogSourceQueryTool(workspace).execute({"source": sid, "pattern": "ALERT-000003"}).output)
    assert payload["matched"] == 1
    assert "ALERT-000003" in payload["matches"][0]["line"]


def test_source_query_time_range_filter(seeded) -> None:
    workspace, _sid, src = seeded
    # 只要 00:00:01 这一秒的行。
    payload = json.loads(
        LogSourceQueryTool(workspace)
        .execute({"source": src, "pattern": "ALERT", "time_range": ["2026-06-16T00:00:01", "2026-06-16T00:00:01"]})
        .output
    )
    assert payload["matched"] == 1
    assert payload["matches"][0]["line_no"] == 1


def test_source_query_invalid_regex_falls_back_to_substring(seeded) -> None:
    workspace, _sid, src = seeded
    # 非法正则 '[' → 退化为字面子串匹配,不报错。
    payload = json.loads(LogSourceQueryTool(workspace).execute({"source": src, "pattern": "["}).output)
    assert payload["ok"] is True


def test_source_query_unknown_source_no_archive(tmp_path: Path) -> None:
    payload = json.loads(
        LogSourceQueryTool(tmp_path).execute({"source": "/nonexistent.log", "pattern": "x"}).output
    )
    assert payload["ok"] is True
    assert payload["matched"] == 0


# ----------------------- 参数校验 -----------------------


def test_start_requires_sources(tmp_path: Path) -> None:
    result = LogMonitorStartTool(tmp_path).execute({})
    assert result.ok is False
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"


def test_query_requires_source_and_pattern(tmp_path: Path) -> None:
    result = LogSourceQueryTool(tmp_path).execute({"source": "x"})
    assert result.ok is False
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"


def test_stop_when_not_running_is_harmless(tmp_path: Path) -> None:
    result = LogMonitorStopTool(tmp_path).execute({})
    assert result.ok is True
    payload = json.loads(result.output)
    assert payload["status"] == "not_running"


def test_sources_coercion_from_json_string(tmp_path: Path) -> None:
    # 模型可能把 sources 发成 JSON 字符串;应被规整(这里源不存在但 daemon 仍会起)。
    from agent_py_agent.agent.tooling.log_ops.tools import _coerce_sources

    assert _coerce_sources('["/a.log", "/b.log"]') == ["/a.log", "/b.log"]
    assert _coerce_sources("/a.log, /b.log") == ["/a.log", "/b.log"]
    assert _coerce_sources(["/a.log"]) == ["/a.log"]
