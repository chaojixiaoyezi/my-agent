# LLM: 只在临时owner测试独立安全审计保留期；原audit/策展来源不允许被该策略删除。
# 模块用途: 覆盖安全审计天数的默认、严格解析、关闭和与原始学习材料的隔离。
from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.memory_store.retention_models import MemoryRetentionPolicy
from agent_py_agent.agent.user_space.owner_policy_seed_payloads import default_retention_payload
from agent_py_agent.tests.test_memory_retention_v2 import NOW, _runtime, _set_policy


# LLM: seed与typed合同必须一致，旧策略缺新增字段按明确默认迁移，不误用audit_days。
# 函数用途: 验证独立security_audit_days可往返且不改原审计策略。
def test_security_audit_policy_round_trip_and_legacy_default():
    payload = default_retention_payload()
    assert payload.get("security_audit_days") == 180
    payload["security_audit_days"] = 12
    assert MemoryRetentionPolicy.from_payload(payload).to_dict()["security_audit_days"] == 12
    payload.pop("security_audit_days")
    assert MemoryRetentionPolicy.from_payload(payload).security_audit_days == 180


# LLM: 布尔/负数/文本不作为天数，损坏策略必须失败而非扩大删除。
# 函数用途: 验证新增安全审计保留期拒绝非法配置。
@pytest.mark.parametrize("value", [True, -1, "30"])
def test_security_audit_policy_rejects_invalid_days(value):
    payload = default_retention_payload()
    payload["security_audit_days"] = value
    with pytest.raises(ValueError):
        MemoryRetentionPolicy.from_payload(payload)


# LLM: 实际AuditLogger生产格式来自logs/audit/audit.jsonl；混合新旧记录必须按记录年龄清理，原audit/逐字保留。
# 函数用途: 验证真实写入器的安全审计保留期、关闭及与原始策展来源隔离。
def test_security_audit_retention_never_cleans_raw_curator_audit(tmp_path):
    from agent_py_agent.agent.audit.logger import AuditLogger, LogParams

    home, _candidates, _memory, retention = _runtime(tmp_path)
    logger = AuditLogger(SimpleNamespace(audit_log_path=str(home.owner_logs_dir / "audit"), audit_enabled=True))
    old = replace(logger._entry_from_params(LogParams(action="TOOL_EXECUTE", user_id="main", channel="local",
                  target_type="test", target_id="synthetic")),
                  timestamp=(NOW - timedelta(days=200)).timestamp())
    fresh = replace(old, entry_id="new", timestamp=NOW.timestamp())
    logger._write_to_file(old)
    logger._write_to_file(fresh)
    path = home.owner_logs_dir / "audit" / "audit.jsonl"
    raw = home.owner_audit_dir / "2020-01-01.jsonl"
    raw.write_text('{}\n', encoding="utf-8")
    stamp = (NOW - timedelta(days=200)).timestamp()
    os.utime(raw, (stamp, stamp))
    before = path.read_bytes()
    _set_policy(home, security_audit_days=180)
    actions = retention.plan(now=NOW).actions
    assert any(action.path == path and action.category == "security_audit" for action in actions)
    assert all(action.path != raw for action in actions)
    assert path.read_bytes() == before
    _set_policy(home, security_audit_days=0)
    assert all(action.path != path for action in retention.plan(now=NOW).actions)
    _set_policy(home, security_audit_days=180)
    report = retention.apply(now=NOW)
    assert report.ok
    assert [json.loads(line)["entry_id"] for line in path.read_text().splitlines()] == ["new"]
    assert raw.read_bytes() == b'{}\n'


# LLM: 真实追加发生在规划后，执行必须用原指纹拒绝旧计划；坏行与未知时间行不能被默认为过期。
# 函数用途: 验证安全审计整理不会覆盖新追加，且仅移除明确可判定的旧记录。
def test_security_audit_compaction_preserves_append_and_unknown_lines(tmp_path):
    from agent_py_agent.agent.common.json_io import append_private_text
    from agent_py_agent.agent.memory_store.retention_apply import execute_retention_plan

    home, candidates, _memory, retention = _runtime(tmp_path)
    path = home.owner_logs_dir / "audit" / "audit.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    old = json.dumps({"timestamp": (NOW - timedelta(days=200)).timestamp(), "id": "old"}) + "\n"
    unknown = '{broken\n' + json.dumps({"id": "unknown", "future_field": {"a": 1}}) + "\n"
    path.write_text(old + unknown, encoding="utf-8")
    plan = retention.plan(now=NOW)
    assert any(action.path == path for action in plan.actions)
    fresh = json.dumps({"timestamp": NOW.timestamp(), "id": "new"}) + "\n"
    append_private_text(path, fresh)
    result = execute_retention_plan(home=home, candidates=candidates, plan=plan, now=NOW)
    assert any(action.path == path and action.status == "state_changed" for action in result.actions)
    assert path.read_text() == old + unknown + fresh
    assert retention.apply(now=NOW).ok
    assert path.read_text() == unknown + fresh
