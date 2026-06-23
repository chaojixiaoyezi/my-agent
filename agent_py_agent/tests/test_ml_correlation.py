
from __future__ import annotations

"""图关联攻击链单测 —— M3-3。

核心:单步不显,连成链才危险——同攻击者跨多个 kill chain 阶段(侦察→入侵→提权→外泄)=攻击链,
链强度 → correlation_boost 抬高链上簇的评级(关联路从占位0真做)。
"""

from pathlib import Path

from agent_py_agent.agent.ml_engine import correlation, engine
from agent_py_agent.agent.ml_engine.models import SignalCluster
from agent_py_agent.agent.tooling.log_ops.tools import _store
from agent_py_agent.agent.tooling.log_ops.tools_ml import LogAttackChainsTool


def _cand(ip: str, rules: list[str], *, line: int = 1, ts: float = 1000.0) -> dict:
    return {"fingerprint": f"c-{ip}-{line}", "source_id": "api1", "entities": {"ip": ip}, "detected_at": ts, "matched_rules": rules}


def _ent_cluster(ip: str, *, boost: float = 0.0) -> SignalCluster:
    return SignalCluster(f"api1:entity:{ip}", "api1", f"entity:{ip}", "entity", 2, 5, 0.6, 0.0, 1.0, entities={"ip": ip}, correlation_boost=boost)


def test_multi_stage_forms_attack_chain() -> None:
    """同攻击者跨 4 阶段(侦察→入侵→提权→外泄)= 满强度攻击链。"""
    cands = [
        _cand("10.0.0.1", ["port_scan"], line=1, ts=1000.0),
        _cand("10.0.0.1", ["brute_force"], line=2, ts=1060.0),
        _cand("10.0.0.1", ["privilege_escalation"], line=3, ts=1120.0),
        _cand("10.0.0.1", ["exfiltration"], line=4, ts=1180.0),
    ]
    chains = correlation.build_attack_chains(cands)
    assert len(chains) == 1 and chains[0].entity == "10.0.0.1"
    assert chains[0].stages == ("recon", "access", "escalation", "exfil") and chains[0].score == 1.0


def test_single_stage_not_a_chain() -> None:
    """只一个阶段(只扫描)不成链——单步不显。"""
    assert correlation.build_attack_chains([_cand("10.0.0.2", ["port_scan"], line=i) for i in range(10)]) == []


def test_two_stages_form_chain() -> None:
    chains = correlation.build_attack_chains([_cand("10.0.0.3", ["port_scan"]), _cand("10.0.0.3", ["sql_injection"], line=2)])
    assert len(chains) == 1 and chains[0].score == 0.5  # 2/4 阶段


def test_apply_boosts_marks_only_chain_clusters() -> None:
    """攻击链上的实体簇得 correlation_boost,不在链上的不动。"""
    boosted = correlation.apply_boosts([_ent_cluster("10.0.0.1"), _ent_cluster("10.0.0.9")], {"10.0.0.1": 0.75})
    by_ip = {c.entities["ip"]: c for c in boosted}
    assert by_ip["10.0.0.1"].correlation_boost == 0.75 and by_ip["10.0.0.9"].correlation_boost == 0.0


def test_correlation_boost_lifts_fused_score() -> None:
    """链上的簇(correlation_boost 高)比同条件非链簇评分高——关联路真做。"""
    s_base = engine.score_feature(engine.build_feature(_ent_cluster("x")), engine.DEFAULT_FUSION_WEIGHTS)
    s_chain = engine.score_feature(engine.build_feature(_ent_cluster("y", boost=1.0)), engine.DEFAULT_FUSION_WEIGHTS)
    assert s_chain.correlation_boost > s_base.correlation_boost
    assert s_chain.fused_score > s_base.fused_score


def test_attack_chains_tool_end_to_end(tmp_path: Path) -> None:
    """工具:候选 → 攻击链(攻击者跨侦察+入侵+提权)。"""
    store = _store(tmp_path, "default")
    store.ensure_dirs()
    store.append_candidates([
        _cand("10.0.0.1", ["port_scan"], line=1, ts=1000.0),
        _cand("10.0.0.1", ["brute_force"], line=2, ts=1060.0),
        _cand("10.0.0.1", ["reverse_shell"], line=3, ts=1120.0),
    ])
    result = LogAttackChainsTool(tmp_path).execute({})
    assert result.ok and result.result_envelope["chains"] == 1
    chain = result.result_envelope["attack_chains"][0]
    assert chain["entity"] == "10.0.0.1" and "recon" in chain["stages"] and "escalation" in chain["stages"]
