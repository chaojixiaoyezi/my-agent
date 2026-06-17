
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
