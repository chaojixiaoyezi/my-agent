
from __future__ import annotations

"""联邦聚合单测 —— M3-4。

核心隐私护栏:k-匿名(≥k 贡献者的规则/指纹才入共享,单用户独有的不入→不可反推出"谁")+ 中位数权重
(抗投毒:单个恶意贡献拉不动中位数)。外加贡献存储、工具端到端(A/B 贡献 → C 拉聚合,opt-in env)。
"""

from pathlib import Path

import pytest

from agent_py_agent.agent.ml_engine import experience, federation
from agent_py_agent.agent.ml_engine.federation import Contribution


def _rule(group: str, agg: str = "count", field: str = "") -> dict:
    return {"rule_id": f"r-{group}-{agg}", "name": "x", "group_by": [group], "aggregate": agg, "agg_field": field, "baseline_deviation": False}


def _contrib(cid: str, rules: list[dict], weights: dict, fps: list[str]) -> Contribution:
    return Contribution(cid, "web", tuple(rules), weights, tuple(fps))


def test_k_anonymity_rules() -> None:
    """≥k(默认2)贡献者都有的规则签名才入共享;单用户独有的不入(不可反推)。"""
    c1 = _contrib("u1", [_rule("ip"), _rule("user")], {"supervised": 0.5}, [])
    c2 = _contrib("u2", [_rule("ip")], {"supervised": 0.5}, [])
    pack = federation.aggregate_contributions([c1, c2], k_anonymity=2)
    groups = [r["group_by"][0] for r in pack.detection_rules]
    assert "ip" in groups       # 2 人都有 → 入
    assert "user" not in groups  # 只 u1 有 → k-匿名挡掉


def test_median_weights_resists_poison() -> None:
    """中位数权重抗投毒:1 个极端恶意贡献拉不动中位数。"""
    normal = [_contrib(f"u{i}", [], {"supervised": 0.5, "unsupervised": 0.3, "correlation": 0.2}, []) for i in range(4)]
    poison = _contrib("evil", [], {"supervised": 0.99, "unsupervised": 0.0, "correlation": 0.01}, [])
    pack = federation.aggregate_contributions(normal + [poison])
    assert pack.fusion_weights["supervised"] == 0.5  # 中位数=0.5,投毒拉不动


def test_k_anonymity_fingerprints() -> None:
    c1 = _contrib("u1", [], {}, ["sql_injection", "entity"])
    c2 = _contrib("u2", [], {}, ["sql_injection"])
    pack = federation.aggregate_contributions([c1, c2], k_anonymity=2)
    assert "sql_injection" in pack.known_fingerprints  # 2 人有
    assert "entity" not in pack.known_fingerprints      # 只 1 人


def test_contribution_store_roundtrip(tmp_path: Path) -> None:
    fed_root = tmp_path / "federation"
    federation.append_contribution(fed_root, _contrib("u1", [_rule("ip")], {"supervised": 0.5}, ["sql_injection"]))
    contribs = federation.read_contributions(fed_root, "web")
    assert len(contribs) == 1 and contribs[0].contributor_id == "u1"
    assert federation.read_contributions(fed_root, "other") == []  # domain 过滤


def test_contribution_from_pack() -> None:
    pack = experience.DomainExperiencePack("web", 1, ({"rule_id": "r", "group_by": ["ip"], "aggregate": "count"},), {"supervised": 0.5}, ("sql_injection",))
    contrib = federation.contribution_from_pack("c-anon", pack)
    assert contrib.contributor_id == "c-anon" and contrib.domain_id == "web" and len(contrib.detection_rules) == 1


def test_federation_tools_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """工具端到端:配 env 共享路径 → A/B 两用户贡献 → C sync 拉聚合(k=2 的规则入)。"""
    from agent_py_agent.agent.tooling.log_ops.tools import _store
    from agent_py_agent.agent.tooling.log_ops.tools_ml import (
        LogExperienceExportTool,
        LogFederationContributeTool,
        LogFederationSyncTool,
        LogRuleAuthorTool,
    )

    monkeypatch.setenv("MY_AGENT_FEDERATION_ROOT", str(tmp_path / "shared_fed"))
    for user in ("userA", "userB"):
        ws = tmp_path / user
        _store(ws, "default").ensure_dirs()
        LogRuleAuthorTool(ws).execute({"rule": {"rule_id": "r-scan", "name": "扫描", "group_by": ["ip"], "aggregate": "distinct", "agg_field": "dst", "threshold": 50}, "backtest": False})
        LogExperienceExportTool(ws).execute({"domain": "web"})
        assert LogFederationContributeTool(ws).execute({"domain": "web"}).result_envelope["contributed"]
    ws_c = tmp_path / "userC"
    _store(ws_c, "default").ensure_dirs()
    sync = LogFederationSyncTool(ws_c).execute({"domain": "web", "k_anonymity": 2})
    assert sync.ok and sync.result_envelope["synced"]
    assert sync.result_envelope["contributors"] == 2 and sync.result_envelope["applied_rules"] == 1


def test_k_anon_rules_count_distinct_contributors_not_instances() -> None:
    """k-匿名必须数独立贡献者:同一贡献者的 Contribution 在列表里重复出现时,不能被当成多个贡献者
    绕过 k-匿名、泄露其私有规则(回归:dogfooding 压 ML 引擎逮到 _k_anon_rules 误数实例数)。"""
    c = _contrib("u1", [_rule("secret")], {}, [])
    pack = federation.aggregate_contributions([c, c], k_anonymity=2)  # 同 u1 重复 2 次
    assert [r["group_by"][0] for r in pack.detection_rules] == []  # 只 1 个独立贡献者,不泄露其私有规则
    pack2 = federation.aggregate_contributions(
        [_contrib("u1", [_rule("ip")], {}, []), _contrib("u2", [_rule("ip")], {}, [])], k_anonymity=2)
    assert [r["group_by"][0] for r in pack2.detection_rules] == ["ip"]  # 2 个独立贡献者,正常入


def test_aggregate_survives_type_poisoned_contribution() -> None:
    """跨用户共享层鲁棒:坏贡献写脏数据(非 dict 规则/非数字权重/非字符串指纹)绝不能崩掉整个联邦聚合
    ——一个坏苹果不能毁整筐(类型投毒 DoS;回归:dogfooding 顺'假设输入格式良好'共性根因审计逮到。
    federation 原只抗数值投毒(中位数),没抗类型投毒)。"""
    good = [_contrib("u1", [_rule("ip")], {"supervised": 0.5}, ["sql_injection"]),
            _contrib("u2", [_rule("ip")], {"supervised": 0.5}, ["sql_injection"])]
    evil = _contrib("evil", ["不是dict"], {"supervised": "high", "unsupervised": None}, [123])  # type: ignore[list-item]
    pack = federation.aggregate_contributions(good + [evil], k_anonymity=2)
    assert pack.fusion_weights["supervised"] == 0.5  # 脏权重被过滤,不崩 median
    assert [r["group_by"][0] for r in pack.detection_rules] == ["ip"]  # 脏规则跳过,好规则正常入
    assert pack.known_fingerprints == ("sql_injection",)  # 好指纹入,脏指纹规范化不崩 sorted
