
from __future__ import annotations

"""通用声明式检测引擎单测 —— M2-2。

核心验证(回应用户"通用监控几十上百种情况"):**同一个 evaluate_rule**,只换声明(group_by×aggregate×
window×threshold),就表达横向扫描/流量突增/数据外泄/暴力破解 —— 零专门代码。外加时窗分桶、声明校验、
回测、规则私有存储(owner 隔离)、工具端到端。
"""

from pathlib import Path
from typing import Any

from agent_py_agent.agent.ml_engine import detection, rule_store
from agent_py_agent.agent.ml_engine.detection import DetectionRule
from agent_py_agent.agent.tooling.log_ops.tools import _store
from agent_py_agent.agent.tooling.log_ops.tools_ml import LogRuleAuthorTool, LogRuleEvalTool


def _rec(ip: str, *, dst: str = "", nbytes: str = "", rules: list[str] | None = None, ts: float = 1000.0) -> dict[str, Any]:
    ents: dict[str, str] = {"ip": ip}
    if dst:
        ents["dst"] = dst
    if nbytes:
        ents["bytes"] = nbytes
    return {"fingerprint": f"c-{ip}-{dst}-{ts}", "entities": ents, "detected_at": ts, "matched_rules": rules or []}


def test_fanout_scan_via_distinct() -> None:
    """横向扫描:一个 ip distinct dst > 50。"""
    rule = detection.parse_rule({"name": "扫描", "group_by": ["ip"], "aggregate": "distinct", "agg_field": "dst", "window_seconds": 60, "threshold": 50})
    assert isinstance(rule, DetectionRule)
    records = [_rec("10.0.0.1", dst=f"t{i}") for i in range(60)] + [_rec("10.0.0.2", dst=f"t{i}") for i in range(10)]
    hits = detection.evaluate_rule(rule, records)
    assert len(hits) == 1 and hits[0].group_key == "ip=10.0.0.1" and hits[0].agg_value == 60


def test_traffic_spike_via_count() -> None:
    """流量突增:一个 ip count > 100。同引擎,换声明。"""
    rule = detection.parse_rule({"name": "突增", "group_by": ["ip"], "aggregate": "count", "window_seconds": 60, "threshold": 100})
    hits = detection.evaluate_rule(rule, [_rec("10.0.0.1") for _ in range(150)])
    assert len(hits) == 1 and hits[0].agg_value == 150


def test_exfiltration_via_sum() -> None:
    """数据外泄:一个 ip sum(bytes) > 1e9。同引擎。"""
    rule = detection.parse_rule({"name": "外泄", "group_by": ["ip"], "aggregate": "sum", "agg_field": "bytes", "window_seconds": 300, "threshold": 1000000000})
    hits = detection.evaluate_rule(rule, [_rec("10.0.0.1", nbytes="600000000") for _ in range(3)])
    assert len(hits) == 1 and hits[0].agg_value == 1800000000


def test_brute_force_via_count_with_filter() -> None:
    """暴力破解:一个 ip 的 auth_failure_burst count > 20(match_any 过滤,无关的不计)。"""
    rule = detection.parse_rule({"name": "暴破", "group_by": ["ip"], "match_any": ["auth_failure_burst"], "aggregate": "count", "window_seconds": 60, "threshold": 20})
    records = [_rec("10.0.0.1", rules=["auth_failure_burst"]) for _ in range(25)]
    records += [_rec("10.0.0.1", rules=["xss"]) for _ in range(50)]
    hits = detection.evaluate_rule(rule, records)
    assert len(hits) == 1 and hits[0].agg_value == 25


def test_window_separates_buckets() -> None:
    """时窗分桶:同 ip 跨两个 60s 窗各 30 条,threshold 50 → 不命中(每窗 30<50)。"""
    rule = detection.parse_rule({"name": "窗", "group_by": ["ip"], "aggregate": "count", "window_seconds": 60, "threshold": 50})
    records = [_rec("10.0.0.1", ts=1000.0) for _ in range(30)] + [_rec("10.0.0.1", ts=2000.0) for _ in range(30)]
    assert detection.evaluate_rule(rule, records) == []


def test_parse_rule_validation() -> None:
    """非法声明返回可读错误串(给 agent 改)。"""
    assert isinstance(detection.parse_rule({"group_by": [], "aggregate": "count", "threshold": 5}), str)
    assert isinstance(detection.parse_rule({"group_by": ["ip"], "aggregate": "xxx", "threshold": 5}), str)
    assert isinstance(detection.parse_rule({"group_by": ["ip"], "aggregate": "distinct", "threshold": 5}), str)
    assert isinstance(detection.parse_rule({"group_by": ["ip"], "aggregate": "count", "threshold": 0}), str)


def test_backtest_counts_hits() -> None:
    rule = detection.parse_rule({"name": "扫描", "group_by": ["ip"], "aggregate": "distinct", "agg_field": "dst", "threshold": 50})
    bt = detection.backtest_rule(rule, [_rec("10.0.0.1", dst=f"t{i}") for i in range(60)])
    assert bt.tested_records == 60 and bt.hit_count == 1 and bt.distinct_groups_hit == 1


def test_rule_store_roundtrip_and_owner_isolation(tmp_path: Path) -> None:
    rule = detection.parse_rule({"name": "r", "group_by": ["ip"], "aggregate": "count", "threshold": 5})
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    rule_store.append_rule(root_a, rule)
    assert len(rule_store.read_rules(root_a)) == 1
    assert rule_store.read_rules(root_b) == []  # owner 隔离


def test_log_rule_author_and_eval_tools(tmp_path: Path) -> None:
    """工具端到端:现编规则(回测)→ 部署 → 评估命中。"""
    store = _store(tmp_path, "default")
    store.ensure_dirs()
    cands = [{"fingerprint": f"c{i}", "source_id": "api1", "entities": {"ip": "10.0.0.1", "dst": f"t{i}"},
              "detected_at": 1000.0, "matched_rules": ["port_scan"], "severity": "medium"} for i in range(60)]
    store.append_candidates(cands)
    author = LogRuleAuthorTool(tmp_path).execute({"rule": {"name": "扫描", "group_by": ["ip"], "aggregate": "distinct", "agg_field": "dst", "threshold": 50}})
    assert author.ok and author.result_envelope["backtest"]["hits"] == 1
    ev = LogRuleEvalTool(tmp_path).execute({})
    assert ev.ok and ev.result_envelope["rules"] == 1 and ev.result_envelope["hit_count"] == 1
