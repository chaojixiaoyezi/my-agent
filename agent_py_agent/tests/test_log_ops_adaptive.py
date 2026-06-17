
from __future__ import annotations

"""log_ops 自适应层端到端 —— daemon 按 per-源 profile 切割+初筛、LLM 规则的正则安全校验。"""

import os
import time
from pathlib import Path

from agent_py_agent.agent.tooling.log_ops.daemon import run_collection_cycle
from agent_py_agent.agent.tooling.log_ops.store import LogOpsStore, build_source_specs
from agent_py_agent.agent.tooling.log_ops.triage import compile_profile_rules


def _store(tmp_path: Path) -> LogOpsStore:
    return LogOpsStore(tmp_path / ".log_ops", "m")


# ----------------------- daemon 按 profile 执行 -----------------------


def test_daemon_uses_profile_rules(tmp_path: Path) -> None:
    """daemon 按 per-源 profile.rules 初筛(用 LLM 定的规则,不是全局 DEFAULT_RULES)。"""
    src = tmp_path / "app.log"
    src.write_text("INFO normal\nPAYMENT_FAIL order=123\nINFO ok\n", encoding="utf-8")
    store = _store(tmp_path)
    specs = build_source_specs([str(src)])
    sid = specs[0].source_id
    store.write_config(specs, poll_interval_seconds=2.0)
    store.write_profile(sid, {"rules": [{"name": "pay_fail", "severity": "high", "pattern": "PAYMENT_FAIL"}]})
    run_collection_cycle(store, specs)
    cands = store.read_candidates()
    assert len(cands) == 1
    assert cands[0]["matched_rules"] == ["pay_fail"]
    assert "PAYMENT_FAIL" in cands[0]["raw_line"]


def test_daemon_falls_back_default_rules_without_profile(tmp_path: Path) -> None:
    """没 profile → 用全局 DEFAULT_RULES(reverse shell 仍被抓)。"""
    src = tmp_path / "sys.log"
    src.write_text("INFO ok\nreverse shell /dev/tcp/1.2.3.4/4444\n", encoding="utf-8")
    store = _store(tmp_path)
    specs = build_source_specs([str(src)])
    run_collection_cycle(store, specs)
    cands = store.read_candidates()
    assert any("reverse_shell" in c["matched_rules"] for c in cands)


def test_daemon_profile_rules_empty_falls_back(tmp_path: Path) -> None:
    """profile 有 splitter 但 rules 全被安全校验拦掉 → 回退 DEFAULT_RULES,不静默漏报。"""
    src = tmp_path / "s.log"
    src.write_text("sql injection union select * from users\n", encoding="utf-8")
    store = _store(tmp_path)
    specs = build_source_specs([str(src)])
    sid = specs[0].source_id
    store.write_config(specs, poll_interval_seconds=2.0)
    store.write_profile(sid, {"rules": [{"name": "bad", "pattern": "("}]})  # 唯一规则非法 → 回退
    run_collection_cycle(store, specs)
    assert any("sql_injection" in c["matched_rules"] for c in store.read_candidates())


def test_daemon_uses_profile_splitter_multiline(tmp_path: Path) -> None:
    """profile.splitter=multiline_start → daemon 把多行堆栈合并成一条记录,archive 一行一记录对账准。"""
    src = tmp_path / "stack.log"
    src.write_text("2026-01-01 ERROR boom\n  at foo\n  at bar\n2026-01-02 INFO ok\n", encoding="utf-8")
    old = time.time() - 100.0
    os.utime(src, (old, old))  # 静默 → flush 最后一条记录
    store = _store(tmp_path)
    specs = build_source_specs([str(src)])
    sid = specs[0].source_id
    store.write_config(specs, poll_interval_seconds=2.0)
    store.write_profile(
        sid,
        {
            "splitter": {"type": "multiline_start", "params": {"start_pattern": "^\\d{4}-"}},
            "rules": [{"name": "err", "severity": "high", "pattern": "ERROR"}],
        },
    )
    run_collection_cycle(store, specs)
    assert store.count_archive_lines(sid) == 2  # 2 条逻辑记录,压平成 2 行,对账准
    cands = store.read_candidates()
    assert len(cands) == 1
    assert "at foo" in cands[0]["raw_line"]  # 堆栈帧合并进同一条记录


# ----------------------- 正则安全(LLM 生成的规则防 ReDoS/崩) -----------------------


def test_compile_profile_rules_safety() -> None:
    """坏正则/ReDoS/超长/缺字段/非 dict 被安全校验拦掉,好规则保留,不崩。"""
    rules = compile_profile_rules(
        [
            {"name": "good", "severity": "high", "pattern": "attack"},
            {"name": "bad_regex", "severity": "high", "pattern": "("},  # 非法正则
            {"name": "redos", "severity": "high", "pattern": "(a+)+"},  # ReDoS 风险嵌套量词
            {"name": "", "pattern": "x"},  # 缺 name
            {"name": "noname"},  # 缺 pattern
            {"name": "toolong", "pattern": "x" * 600},  # 超长
            "not-a-dict",  # 非 dict
        ]
    )
    assert [r.name for r in rules] == ["good"]


def test_compile_profile_rules_severity_default() -> None:
    rules = compile_profile_rules([{"name": "r", "pattern": "x"}])
    assert rules[0].severity == "medium"


# ----------------------- 块C/E/F 工具端到端 -----------------------

import json  # noqa: E402

from agent_py_agent.agent.tooling.log_ops.store import source_id_for  # noqa: E402
from agent_py_agent.agent.tooling.log_ops.tools import _preinit_api_range  # noqa: E402
from agent_py_agent.agent.tooling.log_ops.tools_adaptive import (  # noqa: E402
    LogCrossQueryTool,
    LogProfileGetTool,
    LogProfileSetTool,
    LogReportTool,
)


def _seed_config(tmp_path: Path, sources: list[str]):
    store = _store(tmp_path)
    specs = build_source_specs(sources)
    store.write_config(specs, poll_interval_seconds=2.0)
    return store, specs


def test_tool_profile_set_get_roundtrip(tmp_path: Path) -> None:
    _seed_config(tmp_path, ["http://127.0.0.1:9001/poll"])
    out = json.loads(
        LogProfileSetTool(tmp_path).execute(
            {
                "source": "http://127.0.0.1:9001/poll",
                "monitor_id": "m",
                "rules": [{"name": "r", "severity": "high", "pattern": "attack"}],
                "triage_hint": "看 attack",
            }
        ).output
    )
    assert out["valid_rules"] == 1 and out["version"] == 1
    prof = json.loads(
        LogProfileGetTool(tmp_path).execute({"source": "http://127.0.0.1:9001/poll", "monitor_id": "m"}).output
    )["profile"]
    assert prof["triage_hint"] == "看 attack"
    assert prof["rules"][0]["name"] == "r"


def test_tool_report_grading_suppresses_low_level(tmp_path: Path) -> None:
    store, _ = _seed_config(tmp_path, ["/x.log"])
    store.update_config_field("report_floor", "P1")
    tool = LogReportTool(tmp_path)
    r0 = json.loads(tool.execute({"level": "P0", "title": "urgent", "monitor_id": "m"}).output)
    assert r0["pushed"] is True
    r2 = json.loads(tool.execute({"level": "P2", "title": "minor", "monitor_id": "m"}).output)
    assert r2["pushed"] is False  # P2 < 阈值 P1 → 不推送
    # 用户按阈值只看到够级别的(P0),P2 噪声被抑制。
    assert [r["level"] for r in store.read_reports(min_level="P1")] == ["P0"]
    # 但审计流全量留存(P0+P2 都在)。
    assert len(store.read_reports()) == 2


def test_tool_cross_query_correlates_across_sources(tmp_path: Path) -> None:
    store, specs = _seed_config(tmp_path, ["/a.log", "/b.log"])
    store.append_archive(specs[0].source_id, ["evil 1.2.3.4 here", "normal line"])
    store.append_archive(specs[1].source_id, ["also 1.2.3.4 there"])
    res = json.loads(
        LogCrossQueryTool(tmp_path).execute({"sources": ["*"], "pattern": "1\\.2\\.3\\.4", "monitor_id": "m"}).output
    )
    assert res["sources_with_hits"] == 2  # 同一 IP 跨两源出现
    assert res["total_matched"] == 2


def test_preinit_api_range_only_api_sources(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _preinit_api_range(store, ["http://127.0.0.1:9001/poll", "/file.log"], "range")
    api_st = store.read_state(source_id_for("api", "http://127.0.0.1:9001/poll"))
    assert api_st.get("mode") == "range" and api_st.get("cursor") == 0
    assert store.read_state(source_id_for("file", "/file.log")) == {}  # 文件源不受 range 影响


def test_daemon_high_severity_to_urgent(tmp_path: Path) -> None:
    # daemon 对 high severity 候选(reverse shell)写紧急队列(故障即时上报),medium(port scan)不进。
    src = tmp_path / "s.log"
    src.write_text("INFO ok\nreverse shell /dev/tcp/1.2.3.4/4444\nnmap port scan detected\n", encoding="utf-8")
    store = _store(tmp_path)
    run_collection_cycle(store, build_source_specs([str(src)]))
    assert store.count_urgent() >= 1
    urgent = [json.loads(line) for line in open(store.urgent_path, encoding="utf-8")]
    assert all(u["severity"] == "high" for u in urgent)  # 紧急队列只收 high
    assert any("reverse_shell" in u["matched_rules"] for u in urgent)
