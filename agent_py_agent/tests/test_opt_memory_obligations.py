# LLM: 仅使用临时owner及真实候选仓库；不读生产记忆，不以替身结果外推正式链路。
# 模块用途: 验证候选履约保留、观察计龄、内存预算和自动提交闭环。
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from agent_py_agent.agent.memory_store import candidates as module
from agent_py_agent.tests.test_memory_candidates import candidate


# LLM: 通过真实模型读改写未来字段，副本必须隔离并参与原行失效。
# 函数用途: 锁住向前兼容字段在审核和嵌套修改后的保真。
def test_unknown_fields_survive_read_modify_write(tmp_path):
    from agent_py_agent.agent.memory_store.candidate_models import MemoryCandidate

    payload = candidate().to_record()
    payload["future_contract"] = {"proofs": ["original"]}
    row = MemoryCandidate.from_record(payload)
    payload["future_contract"]["proofs"].append("caller")
    assert row.to_record()["future_contract"] == {"proofs": ["original"]}
    path = tmp_path / "candidates.jsonl"
    module._write_candidates_unlocked(path, [row], admission=module._NullAdmission())
    service = module.CandidateService(path)
    approved = service.transition(row.candidate_id, "approved", reviewer="owner")
    assert approved.to_record()["future_contract"] == {"proofs": ["original"]}
    approved._extra_fields["future_contract"]["proofs"].append("new")
    assert '"new"' in module._serialize_candidates([approved])


# LLM: v2缺字段必须恢复义务；approved即使显式false也不能被retention清理。
# 函数用途: 验证旧候选兼容、终态清除和仓库末门的不可误删约束。
@pytest.mark.parametrize("status,origin,expected", [
    ("approved", "model_inferred", True),
    ("pending_review", "user_explicit", True),
    ("pending_review", "model_inferred", False),
    ("rejected", "user_explicit", False),
])
def test_legacy_obligation_defaults_and_retention_guard(tmp_path, status, origin, expected):
    from agent_py_agent.agent.memory_store.candidate_models import MemoryCandidate

    row = candidate()
    payload = row.to_record()
    payload.update(schema_version="my-agent.memory-candidate.v2", status=status, origin=origin)
    payload["status_history"][-1]["status"] = status
    payload.pop("commit_obligation", None)
    restored = MemoryCandidate.from_record(payload)
    assert getattr(restored, "commit_obligation", None) is expected
    assert restored.to_record()["schema_version"] == "my-agent.memory-candidate.v3"
    old = "2020-01-01T00:00:00+00:00"
    restored = replace(restored, updated_at=old, last_observed_at=old)
    path = tmp_path / "candidates.jsonl"
    module._write_candidates_unlocked(path, [restored], admission=module._NullAdmission())
    service = module.CandidateService(path)
    ids = service.retention_candidates(before=datetime.now(timezone.utc))
    assert (restored.candidate_id not in ids) is expected
    if expected:
        with pytest.raises(module.CandidateTransitionError, match="retention"):
            service.delete_terminal([restored.candidate_id])


# LLM: plan/apply均使用真实服务；伪造旧动作也必须在执行末门保护义务候选。
# 函数用途: 验证义务并非只在列表展示时过滤。
def test_retention_plan_and_apply_preserve_old_approved_candidate(tmp_path):
    from agent_py_agent.tests.test_memory_retention_wave5 import NOW, runtime

    home, service, retention = runtime(tmp_path)
    row = replace(candidate(), status="approved", updated_at="2020-01-01T00:00:00+00:00",
        last_observed_at="2020-01-01T00:00:00+00:00")
    row.status_history[-1]["status"] = "approved"
    module._write_candidates_unlocked(service.path, [row], admission=module._NullAdmission())
    assert not [a for a in retention.plan(now=NOW).actions if a.record_id == row.candidate_id]
    retention.apply(now=NOW)
    assert service.get(row.candidate_id).status == "approved"


# LLM: 只以来源ref判独立观察，不让模型改述或修改observation_id虚增次数。
# 函数用途: 测试同范围显式链接收敛、重放去重及跨范围拒绝。
def test_link_accumulates_only_independent_source_refs(tmp_path):
    from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope

    service = module.CandidateService(tmp_path / "candidates.jsonl")
    observation = CandidateObservation(candidate_type="lesson", content="先核实再判断", subject_key="proof.first",
        scope=MemoryScope("personal", "personal"), origin="model_inferred", promotion_target="lesson",
        source_message_refs=({"message_id": "one", "thread_id": "t"},))
    first = service.observe(observation)
    assert "link_candidate_id" in CandidateObservation.__dataclass_fields__
    linked = replace(observation, content="同一个经验的另一表述", link_candidate_id=first.candidate_id,
        observation_id="model-selected-different-key", observed_at="2026-10-28T00:00:00+00:00")
    replay = service.observe(linked)
    assert replay.occurrence_count == 1
    new = service.observe(replace(linked, source_message_refs=({"message_id": "two", "thread_id": "t"},)))
    assert new.candidate_id == first.candidate_id and new.content == first.content
    assert new.occurrence_count == 2 and len(service.list()) == 1
    assert new.last_observed_at >= linked.observed_at
    with pytest.raises(module.CandidateIdentityConflictError):
        service.observe(replace(linked, scope=MemoryScope("project", "project:other")))


# LLM: 使用真正消息库和正式仓库；长期事实仍必须取得真实user/tool权威而非次数自证。
# 函数用途: 验证model_inferred补充明确来源后无需人工确认即可正式落库。
def test_inferred_candidate_reenters_auto_pipeline_with_user_corroboration(tmp_path):
    from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope
    from agent_py_agent.tests.test_memory_promotion_v2 import _explicit, _runtime

    promotion, conversations, thread = _runtime(tmp_path)
    inferred = promotion.candidates.observe(CandidateObservation(candidate_type="long_term_fact",
        content="个人电脑使用 macOS。", subject_key="device.os", scope=MemoryScope("personal", "personal"),
        origin="model_inferred", promotion_target="long_term"))
    row = _explicit(promotion, conversations, thread, content=inferred.content)
    assert row.candidate_id == inferred.candidate_id
    assert row.commit_obligation
    assert module.CandidateService(promotion.candidates.path).get(row.candidate_id).commit_obligation
    result = promotion.promote(row.candidate_id, automatic=True)
    assert result.promoted and promotion.long_term.all()[0].content == inferred.content
    assert not promotion.candidates.get(row.candidate_id).commit_obligation


# LLM: 正式写失败后不丢批准、证据或义务；自动重试使用耐久退避，人工尝试不受退避隐式授权。
# 函数用途: 验证故障义务跨服务读取可恢复并最终清除。
def test_promotion_io_failure_retains_obligation_with_backoff(tmp_path, monkeypatch):
    from agent_py_agent.tests.test_memory_promotion_v2 import _explicit, _runtime

    promotion, conversations, thread = _runtime(tmp_path)
    row = _explicit(promotion, conversations, thread, content="个人电脑使用 macOS。")
    original = promotion._commit_target
    monkeypatch.setattr(promotion, "_commit_target", lambda *args: (_ for _ in ()).throw(OSError("disk full")))
    result = promotion.promote(row.candidate_id, automatic=True)
    assert not result.promoted and result.reason_code == "PROMOTION_IO_BLOCKED"
    saved = module.CandidateService(promotion.candidates.path).get(row.candidate_id)
    assert saved.commit_obligation and saved.obligation_retry_after
    assert promotion.promote(row.candidate_id, automatic=True).reason_code == "COMMIT_OBLIGATION_BACKOFF"
    monkeypatch.setattr(promotion, "_commit_target", original)
    assert promotion.promote(row.candidate_id, automatic=False).promoted
    assert not promotion.candidates.get(row.candidate_id).commit_obligation


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


# LLM: 读取归一化不能允许显式false取消用户待提交义务；owner撤回则是合法终止。
# 函数用途: 覆盖非approved来源修复和撤回义务清除。
def test_user_obligation_is_enforced_on_read_and_can_be_withdrawn(tmp_path):
    from agent_py_agent.agent.memory_store.candidate_models import MemoryCandidate

    payload = candidate().to_record()
    payload.update(origin="user_explicit", commit_obligation=False)
    row = MemoryCandidate.from_record(payload)
    assert row.commit_obligation
    path = tmp_path / "candidates.jsonl"
    module._write_candidates_unlocked(path, [row], admission=module._NullAdmission())
    service = module.CandidateService(path)
    withdrawn = service.transition(row.candidate_id, "withdrawn", reviewer="owner")
    assert not withdrawn.commit_obligation
    assert not service.get(row.candidate_id).commit_obligation


# LLM: 同一次执行的引用补全是别名，不是第二票；新执行不受复用observation_id影响。
# 函数用途: 验证工具/产物别名、无link重放和真正新来源的计票。
def test_tool_source_aliases_and_reused_observation_id(tmp_path):
    from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope

    service = module.CandidateService(tmp_path / "candidates.jsonl")
    observation = CandidateObservation(candidate_type="lesson", content="提交前先核对证据", subject_key="evidence.before",
        scope=MemoryScope("personal", "personal"), origin="model_inferred", promotion_target="lesson",
        source_tool_refs=({"run_id": "r", "tool_call_id": "t"},), observation_id="first")
    first = service.observe(observation)
    enriched = replace(observation, source_tool_refs=({"run_id": "r", "tool_call_id": "t", "operation_id": "o"},),
        observation_id="enriched")
    assert service.observe(enriched).occurrence_count == 1
    linked = replace(enriched, link_candidate_id=first.candidate_id, content="相同经验的改述",
        source_tool_refs=(), source_artifact_refs=({"run_id": "r", "tool_call_id": "t", "artifact_ref": "artifact"},),
        observation_id="artifact")
    assert service.observe(linked).occurrence_count == 1
    second = replace(linked, source_artifact_refs=(), source_tool_refs=({"run_id": "r2", "tool_call_id": "t2"},))
    assert service.observe(second).occurrence_count == 2
    third = replace(second, source_tool_refs=({"run_id": "r3", "tool_call_id": "t3"},))
    assert service.observe(third).occurrence_count == 3
