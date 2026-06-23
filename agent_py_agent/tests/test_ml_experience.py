
from __future__ import annotations

"""领域经验包单测 —— M2-3。

核心验证(回应用户"经验复用不重训"):经验脱敏沉淀成领域包 → 跨环境(模拟跨用户/跨任务)加载,
规则就绪不从零;脱敏=不含原始 IP(entity:1.2.3.4 → entity)。外加 merge 并集、save 累积、工具端到端。
"""

from pathlib import Path

from agent_py_agent.agent.ml_engine import detection, experience, rule_store
from agent_py_agent.agent.ml_engine.detection import DetectionRule
from agent_py_agent.agent.tooling.log_ops.tools import _store
from agent_py_agent.agent.tooling.log_ops.tools_ml import (
    LogExperienceExportTool,
    LogExperienceLoadTool,
    LogRuleAuthorTool,
)


def _rule(rid: str) -> DetectionRule:
    parsed = detection.parse_rule({"rule_id": rid, "name": rid, "group_by": ["ip"], "aggregate": "count", "threshold": 5})
    assert isinstance(parsed, DetectionRule)
    return parsed


def test_pack_cross_environment_reuse_and_desensitized(tmp_path: Path) -> None:
    """经验跨环境复用:导出到共享 root,另一环境加载,规则+权重就绪不从零;脱敏不含原始 IP。"""
    shared = tmp_path / "shared_experience"
    scan = detection.parse_rule({"name": "扫描", "group_by": ["ip"], "aggregate": "distinct", "agg_field": "dst", "threshold": 50})
    assert isinstance(scan, DetectionRule)
    pack = experience.export_pack(
        "web_api_logs", [scan],
        {"supervised": 0.4, "unsupervised": 0.4, "correlation": 0.2},
        ["api1:sql_injection", "api1:entity:1.2.3.4"],
    )
    experience.save_pack(shared, pack)
    loaded = experience.load_pack(shared, "web_api_logs")
    assert loaded is not None
    rules = experience.rules_from_pack(loaded)
    assert len(rules) == 1 and rules[0].name == "扫描"  # 规则迁移过来
    assert loaded.fusion_weights["supervised"] == 0.4   # 权重迁移
    assert "entity" in loaded.known_fingerprints         # 脱敏:entity:IP → entity
    assert not any("1.2.3.4" in fp for fp in loaded.known_fingerprints)  # 不含原始 IP
    assert not any("api1" in fp for fp in loaded.known_fingerprints)     # 不含 domain 源标识


def test_merge_packs_unions_rules_and_takes_new_weights() -> None:
    base = experience.export_pack("d", [_rule("r1")], {"supervised": 0.5}, ["d:a"])
    overlay = experience.export_pack("d", [_rule("r2")], {"supervised": 0.3}, ["d:b"])
    merged = experience.merge_packs(base, overlay)
    assert len(merged.detection_rules) == 2          # 规则并集
    assert merged.fusion_weights["supervised"] == 0.3  # overlay 取新
    assert merged.version == 2


def test_save_pack_accumulates_not_overwrite(tmp_path: Path) -> None:
    root = tmp_path / "exp"
    experience.save_pack(root, experience.export_pack("d", [_rule("r1")], {}, []))
    experience.save_pack(root, experience.export_pack("d", [_rule("r2")], {}, []))
    loaded = experience.load_pack(root, "d")
    assert loaded is not None and len(loaded.detection_rules) == 2  # 累积不覆盖


def test_load_missing_pack_returns_none(tmp_path: Path) -> None:
    assert experience.load_pack(tmp_path / "exp", "nope") is None


def test_experience_export_load_tools_cross_task(tmp_path: Path) -> None:
    """工具端到端:现编规则→export领域包→新任务load→规则就绪(同用户跨任务复用不重训)。"""
    store = _store(tmp_path, "default")
    store.ensure_dirs()
    LogRuleAuthorTool(tmp_path).execute({"rule": {"name": "扫描", "group_by": ["ip"], "aggregate": "distinct", "agg_field": "dst", "threshold": 50}, "backtest": False})
    exp = LogExperienceExportTool(tmp_path).execute({"domain": "web_api_logs"})
    assert exp.ok and exp.result_envelope["exported_rules"] == 1
    load = LogExperienceLoadTool(tmp_path).execute({"domain": "web_api_logs", "monitor_id": "newtask"})
    assert load.ok and load.result_envelope["loaded"] and load.result_envelope["applied_rules"] == 1
    new_store = _store(tmp_path, "newtask")
    assert len(rule_store.read_rules(new_store.root)) == 1  # 新任务私有规则库就绪


def test_experience_load_dedupes_on_repeat(tmp_path: Path) -> None:
    """重复 load 不重复 append 规则(按 rule_id 去重)。"""
    store = _store(tmp_path, "default")
    store.ensure_dirs()
    LogRuleAuthorTool(tmp_path).execute({"rule": {"rule_id": "r-scan", "name": "扫描", "group_by": ["ip"], "aggregate": "count", "threshold": 50}, "backtest": False})
    LogExperienceExportTool(tmp_path).execute({"domain": "d"})
    LogExperienceLoadTool(tmp_path).execute({"domain": "d", "monitor_id": "t2"})
    second = LogExperienceLoadTool(tmp_path).execute({"domain": "d", "monitor_id": "t2"})
    assert second.result_envelope["applied_rules"] == 0  # 第二次去重,不再加
