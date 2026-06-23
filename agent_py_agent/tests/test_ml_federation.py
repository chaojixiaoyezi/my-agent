
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
