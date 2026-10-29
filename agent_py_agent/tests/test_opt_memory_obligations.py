# LLM: 仅使用临时owner及真实候选仓库；不读生产记忆，不以替身结果外推正式链路。
# 模块用途: 验证候选履约保留、观察计龄、内存预算和自动提交闭环。
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from agent_py_agent.agent.memory_store import candidates as module
from agent_py_agent.tests.test_memory_candidates import candidate


# LLM: 使用真实JSONL解析及进程缓存，预算故意介于磁盘字节和驻留对象树之间。
# 函数用途: 防止将候选文件大小误当作128MiB内存预算。
def test_candidate_cache_accounts_retained_tree_not_file_bytes(tmp_path, monkeypatch):
    path = tmp_path / "candidates.jsonl"
    path.write_text(module._serialize_candidates([candidate()]), encoding="utf-8")
    monkeypatch.setattr(module, "_CANDIDATE_LOAD_CACHE_MAX_BYTES", path.stat().st_size + 128)
    module._drop_candidate_load_cache(path)
    service = module.CandidateService(path)
    assert service.readonly_rows()[0].candidate_id
    assert str(path) not in module._CANDIDATE_LOAD_CACHE


# LLM: 写侧冷解析返回独占模型，缓存不得再为入驻复制整个账本；公开读取仍保持隔离。
# 函数用途: 验证取消额外的整账本缓存deepcopy而不污染调用者。
def test_cold_write_load_does_not_duplicate_ledger_for_cache(tmp_path):
    path = tmp_path / "candidates.jsonl"
    path.write_text(module._serialize_candidates([candidate()]), encoding="utf-8")
    module._drop_candidate_load_cache(path)
    service = module.CandidateService(path)
    with patch.object(module, "deepcopy", wraps=module.deepcopy) as copier:
        rows = service._load_unlocked()
    assert rows and copier.call_count == 0


# LLM: 陈旧创建/更新时间不能覆盖最近独立观察；仅按最后观察决定未终态草稿年龄。
# 函数用途: 防止反复出现的经验在凑够晋升证据前被保留清理删除。
def test_retention_uses_last_observed_not_review_updated_time(tmp_path):
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=90)).isoformat()
    row = replace(candidate(), created_at=old, updated_at=old, last_observed_at=now.isoformat())
    path = tmp_path / "candidates.jsonl"
    module._write_candidates_unlocked(path, [row], admission=module._NullAdmission())
    service = module.CandidateService(path)
    assert row.candidate_id not in service.retention_candidates(before=now - timedelta(days=30))


# LLM: 三个独立过期目标共用同阶段pin视图；实际删除仍走指纹/引用重核，禁止按目标全量重建。
# 函数用途: 验证DEL-MR-17不会为每个Daily文件重读全部记忆和会话。
def test_retention_reuses_pin_snapshot_for_unchanged_sources(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import retention_apply
    from agent_py_agent.tests.test_memory_retention_wave5 import NOW, old_file, runtime

    home, _candidates, service = runtime(tmp_path)
    paths = [old_file(home.owner_memory_daily_dir / f"expired-{i}.jsonl") for i in range(3)]
    with patch.object(retention_apply, "current_retention_pins", wraps=retention_apply.current_retention_pins) as scan:
        report = service.apply(now=NOW)
    assert report.ok and all(not path.exists() for path in paths)
    assert scan.call_count <= 1


# LLM: 实际CandidateService发表新引用后旧投影必须失效；坏源不得复用旧complete结果，不假装跨进程原子。
# 函数用途: 验证阶段内新增pin与损坏来源不会被快照缓存吞掉。
def test_pin_snapshot_reloads_real_candidate_publication_and_rejects_damage(tmp_path):
    from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope
    from agent_py_agent.agent.memory_store.reference_pins import ReferencePinSnapshot
    from agent_py_agent.agent.memory_store.retention_scan import current_retention_pins
    from agent_py_agent.tests.test_memory_retention_wave5 import NOW, old_file, runtime

    home, candidates, _service = runtime(tmp_path)
    evidence = old_file(home.owner_memory_daily_dir / "newly-pinned.jsonl")
    snapshot = ReferencePinSnapshot(home, (), NOW, current_retention_pins)
    errors = []
    assert not snapshot.current(errors)[0].protects(evidence, errors)
    candidates.observe(CandidateObservation(candidate_type="lesson", content="阶段内新增引用",
        subject_key="pin.new", scope=MemoryScope("personal", "personal"), origin="model_inferred",
        promotion_target="lesson", evidence_refs=({"path": str(evidence)},)))
    assert snapshot.current(errors)[0].protects(evidence, errors)
    candidates.path.write_text('{broken\n', encoding="utf-8")
    with pytest.raises(OSError, match="PIN_SOURCE_UNREADABLE"):
        snapshot.current(errors)
