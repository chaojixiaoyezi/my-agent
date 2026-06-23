
from __future__ import annotations

"""规则自适应优化单测 —— M3-2。

co-teaming 闭环延伸到规则层:标注误报 → 统计误报率 → 误报率高自调升阈值产新版本(version+1),
规则越用越准。含 rule_store 版本化(同 rule_id 取最新)、工具端到端。
"""

from pathlib import Path

from agent_py_agent.agent.ml_engine import detection, rule_feedback, rule_store
from agent_py_agent.agent.ml_engine.detection import DetectionRule
from agent_py_agent.agent.ml_engine.rule_feedback import RuleFeedback
from agent_py_agent.agent.tooling.log_ops.tools import _store
from agent_py_agent.agent.tooling.log_ops.tools_ml import LogRuleAuthorTool, LogRuleFeedbackTool, LogRuleTuneTool


def _rule(rid: str = "r", threshold: float = 50.0, version: int = 1) -> DetectionRule:
    parsed = detection.parse_rule({"rule_id": rid, "name": rid, "group_by": ["ip"], "aggregate": "count", "threshold": threshold, "version": version})
    assert isinstance(parsed, DetectionRule)
    return parsed


def _fb(rid: str, fp: bool, gk: str = "ip=x") -> RuleFeedback:
    return RuleFeedback(rid, gk, fp, 1.0)


def test_assess_high_fp_rate_suggests_raise() -> None:
    """误报率 ≥0.5 → 建议升阈值(阈值太松)。"""
    fbs = [_fb("r", True) for _ in range(6)] + [_fb("r", False) for _ in range(2)]  # 6/8=0.75
    eff = rule_feedback.assess_rule(_rule("r"), fbs)
    assert eff.false_positive_rate == 0.75 and eff.suggested_action == "raise_threshold"


def test_assess_insufficient_feedback_collecting() -> None:
    """反馈 <5 → collecting(样本太少不建议)。"""
    assert rule_feedback.assess_rule(_rule("r"), [_fb("r", True) for _ in range(3)]).suggested_action == "collecting"


def test_assess_low_fp_ok() -> None:
    fbs = [_fb("r", False) for _ in range(9)] + [_fb("r", True)]  # 0.1
    assert rule_feedback.assess_rule(_rule("r"), fbs).suggested_action == "ok"


def test_tune_raises_threshold_and_version() -> None:
    """误报率高 → 阈值×1.5 + version+1。"""
    rule = _rule("r", threshold=50.0, version=1)
    eff = rule_feedback.assess_rule(rule, [_fb("r", True) for _ in range(6)])
    tuned = rule_feedback.tune_rule(rule, eff)
    assert tuned.threshold == 75.0 and tuned.version == 2


def test_tune_no_change_when_ok() -> None:
    rule = _rule("r")
    eff = rule_feedback.assess_rule(rule, [_fb("r", False) for _ in range(6)])
    assert rule_feedback.tune_rule(rule, eff) is rule  # 不变原样返回


def test_rule_store_versioning(tmp_path: Path) -> None:
    """同 rule_id append 新版本,read_rules 取最新 version(自适应版本化)。"""
    rule_store.append_rule(tmp_path, _rule("r", threshold=50.0, version=1))
    rule_store.append_rule(tmp_path, _rule("r", threshold=75.0, version=2))
    rules = rule_store.read_rules(tmp_path)
    assert len(rules) == 1 and rules[0].version == 2 and rules[0].threshold == 75.0


def test_feedback_roundtrip(tmp_path: Path) -> None:
    rule_feedback.append_feedback(tmp_path, _fb("r", True))
    fbs = rule_feedback.read_feedbacks(tmp_path)
    assert len(fbs) == 1 and fbs[0].false_positive


def test_feedback_and_tune_tools_end_to_end(tmp_path: Path) -> None:
    """工具端到端:现编规则 → 标 6 次误报 → tune auto_apply → 规则升阈值产 v2。"""
    store = _store(tmp_path, "default")
    store.ensure_dirs()
    assert LogRuleAuthorTool(tmp_path).execute({"rule": {"rule_id": "r-scan", "name": "扫描", "group_by": ["ip"], "aggregate": "count", "threshold": 50}, "backtest": False}).ok
    for _ in range(6):
        LogRuleFeedbackTool(tmp_path).execute({"rule_id": "r-scan", "false_positive": True})
    tune = LogRuleTuneTool(tmp_path).execute({"auto_apply": True})
    assert tune.ok and tune.result_envelope["auto_applied"] == 1
    rules = {r.rule_id: r for r in rule_store.read_rules(store.root)}
    assert rules["r-scan"].version == 2 and rules["r-scan"].threshold == 75.0  # 自调到 v2/阈值75
