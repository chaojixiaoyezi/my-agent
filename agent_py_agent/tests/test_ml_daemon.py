
from __future__ import annotations

"""daemon 自动集成单测 —— M2-4。

验证 ml_engine_enabled gate:关(默认)时 daemon 零影响走现有采集;开时每拍自动跑确定性声明式检测
pass(agent 现编的规则),命中写报告,同命中去重不重复淹没。daemon 不碰 LLM、不碰原始采集流程。
"""

from pathlib import Path

from agent_py_agent.agent.ml_engine import detection, rule_store
from agent_py_agent.agent.tooling.log_ops import daemon as daemon_mod
from agent_py_agent.agent.tooling.log_ops.tools import _store


def _scan_cands(n: int) -> list[dict]:
    return [{"fingerprint": f"c{i}", "source_id": "api1", "entities": {"ip": "10.0.0.1", "dst": f"t{i}"},
             "detected_at": 1000.0, "matched_rules": ["port_scan"]} for i in range(n)]


def _deploy(store, spec: dict) -> None:
    rule = detection.parse_rule(spec)
    assert isinstance(rule, detection.DetectionRule)
    rule_store.append_rule(store.root, rule)


def test_ml_detection_pass_reports_and_dedupes(tmp_path: Path) -> None:
    """检测 pass:现编扫描规则 + 候选(60 dst)→ 命中写 report;再跑去重不重复。"""
    store = _store(tmp_path, "default")
    store.ensure_dirs()
    _deploy(store, {"rule_id": "r-scan", "name": "扫描", "group_by": ["ip"], "aggregate": "distinct", "agg_field": "dst", "threshold": 50})
    store.append_candidates(_scan_cands(60))
    from agent_py_agent.agent.ml_engine.detection import MetricBaseline
    seen, baseline = set(), MetricBaseline()
    daemon_mod._run_ml_detection_pass(store, seen, baseline)
    ml_reports = [r for r in store.read_reports() if r.get("ml_detection")]
    assert len(ml_reports) == 1 and "扫描" in ml_reports[0]["title"]
    daemon_mod._run_ml_detection_pass(store, seen, baseline)  # 再跑
    assert len([r for r in store.read_reports() if r.get("ml_detection")]) == 1  # 去重,不重复报


def test_serve_gate_off_skips_ml(tmp_path: Path) -> None:
    """ml_engine_enabled 关(默认未设):serve 不跑检测 pass,零影响。"""
    store = _store(tmp_path, "default")
    store.write_config([], poll_interval_seconds=0.05)
    _deploy(store, {"rule_id": "r", "name": "突增", "group_by": ["ip"], "aggregate": "count", "threshold": 5})
    store.append_candidates(_scan_cands(10))
    daemon_mod.serve(store, poll_interval_seconds=0.05, max_cycles=1)
    assert [r for r in store.read_reports() if r.get("ml_detection")] == []  # 关:无 ML report


def test_serve_gate_on_runs_ml(tmp_path: Path) -> None:
    """ml_engine_enabled 开:serve 每拍跑检测 pass,命中写 report。"""
    store = _store(tmp_path, "default")
    store.write_config([], poll_interval_seconds=0.05)
    store.update_config_field("ml_engine_enabled", True)
    _deploy(store, {"rule_id": "r", "name": "突增", "group_by": ["ip"], "aggregate": "count", "window_seconds": 60, "threshold": 5})
    store.append_candidates(_scan_cands(10))
    daemon_mod.serve(store, poll_interval_seconds=0.05, max_cycles=1)
    assert len([r for r in store.read_reports() if r.get("ml_detection")]) == 1  # 开:命中写 report
