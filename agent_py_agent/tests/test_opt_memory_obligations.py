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


# LLM: 使用真实消息来源和仓库构造非用户义务候选，避免用户来源默认义务掩盖批准准备缺陷。
# 函数用途: 为批准准备故障测试提供具备自动资格但尚无提交义务的工作约定。
def _pending_automatic_candidate(tmp_path):
    from agent_py_agent.tests.test_memory_promotion_v2 import _explicit, _runtime

    service, conversations, thread = _runtime(tmp_path)
    row = _explicit(service, conversations, thread, content="提交前核对原始证据。",
        candidate_type="working_agreement", subject_key="evidence.before")
    row = replace(row, origin="model_inferred", commit_obligation=False)
    module._write_candidates_unlocked(service.candidates.path, [row], admission=module._NullAdmission())
    assert row.status == "pending_review" and not row.commit_obligation
    return service, row


# LLM: 只在批准准备写入点注入一次已知失败，退避必须真正写入候选而不能靠内存回执自证。
# 函数用途: 验证准备失败保留义务、自动退避以及退避过期后的真实正式提交闭环。
@pytest.mark.parametrize("failure,reason", [
    ("io", "PROMOTION_IO_BLOCKED"),
    ("lock", "PROMOTION_LOCK_BLOCKED"),
    ("quota", "PROMOTION_CAPACITY_BLOCKED"),
    ("cas", "CANDIDATE_CHANGED_AFTER_VERIFICATION"),
])
def test_pending_approval_failure_persists_obligation(tmp_path, monkeypatch, failure, reason):
    from agent_py_agent.agent.user_space.owner_quota import (
        OwnerQuotaExceeded,
        OwnerQuotaLockContention,
        OwnerQuotaProjection,
    )

    service, row = _pending_automatic_candidate(tmp_path)
    original = service.candidates.transition
    errors = {"io": OSError("disk full"), "lock": OwnerQuotaLockContention("busy"),
        "quota": OwnerQuotaExceeded(OwnerQuotaProjection(1, 2, 1)),
        "cas": module.CandidateStateChangedError("pending_review")}

    # LLM: 故障仅发生一次，不在准备失败时触碰正式库；后续持久化仍使用真实候选服务。
    # 函数用途: 模拟批准写入前的可恢复失败。
    def fail_approval(*args, **kwargs):
        monkeypatch.setattr(service.candidates, "transition", original)
        raise errors[failure]

    monkeypatch.setattr(service.candidates, "transition", fail_approval)
    result = service.promote(row.candidate_id, automatic=True)
    assert not result.promoted and result.reason_code == reason
    saved = module.CandidateService(service.candidates.path).get(row.candidate_id)
    assert saved.status == "pending_review" and saved.commit_obligation
    assert saved.obligation_blocked_reason == reason
    assert datetime.fromisoformat(saved.obligation_retry_after) > datetime.now(timezone.utc)
    assert not service.long_term.all()
    assert service.promote(row.candidate_id, automatic=True).reason_code == "COMMIT_OBLIGATION_BACKOFF"
    assert saved.candidate_id not in service.candidates.retention_candidates(before=datetime.now(timezone.utc))
    due = replace(saved, obligation_retry_after=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
    module._write_candidates_unlocked(service.candidates.path, [due], admission=module._NullAdmission())
    assert service.promote(row.candidate_id, automatic=True).promoted
    assert [formal.content for formal in service.long_term.all()] == [row.content]
    completed = module.CandidateService(service.candidates.path).get(row.candidate_id)
    assert completed.status == "promoted" and not completed.commit_obligation
    assert not completed.obligation_retry_after and not completed.obligation_blocked_reason


# LLM: 真正发布并发元数据后触发完整快照CAS失败；新快照须重新核门，只登记义务而不自动提交。
# 函数用途: 验证CAS竞争后的待审核候选不会继续没有义务，也不会覆盖并发元数据。
def test_pending_approval_cas_rechecks_current_snapshot(tmp_path, monkeypatch):
    service, row = _pending_automatic_candidate(tmp_path)
    original = service.candidates.transition

    # LLM: 在原transition前替换真实账本，让CAS失败来自存储而非纯异常替身。
    # 函数用途: 建立与批准准备交错的同内容新快照。
    def race_approval(*args, **kwargs):
        monkeypatch.setattr(service.candidates, "transition", original)
        raced = replace(row, review_note="concurrent metadata")
        module._write_candidates_unlocked(service.candidates.path, [raced], admission=module._NullAdmission())
        return original(*args, **kwargs)

    monkeypatch.setattr(service.candidates, "transition", race_approval)
    assert service.promote(row.candidate_id, automatic=True).reason_code == "CANDIDATE_CHANGED_AFTER_VERIFICATION"
    saved = service.candidates.get(row.candidate_id)
    assert saved.commit_obligation and saved.obligation_retry_after
    assert saved.review_note == "concurrent metadata" and saved.status == "pending_review"
    assert not service.long_term.all()


# LLM: 准备失败时终态已由另一审核者持久化；不得用旧已核快照复活或加回提交义务。
# 函数用途: 验证可恢复错误收口仍尊重并发终态。
@pytest.mark.parametrize("status", ["rejected", "withdrawn", "expired", "superseded"])
def test_pending_approval_failure_preserves_concurrent_terminal(tmp_path, monkeypatch, status):
    service, row = _pending_automatic_candidate(tmp_path)
    original = service.candidates.transition

    # LLM: 先经真实状态机落并发终态，再报告旧准备失败，测试不直接伪造终态字段。
    # 函数用途: 精确建立批准与撤回等终态的交错。
    def race_terminal(*args, **kwargs):
        monkeypatch.setattr(service.candidates, "transition", original)
        original(row, status, reviewer="concurrent-owner")
        raise OSError("approval blocked")

    monkeypatch.setattr(service.candidates, "transition", race_terminal)
    result = service.promote(row.candidate_id, automatic=True)
    saved = service.candidates.get(row.candidate_id)
    assert result.reason_code == "CANDIDATE_CHANGED_AFTER_VERIFICATION"
    assert saved.status == status and saved.reviewer == "concurrent-owner"
    assert not saved.commit_obligation and not saved.obligation_retry_after
    assert not service.long_term.all()


# LLM: 未确认执行异常必须原对象抛出，不能被IO恢复路径变成可自动重放的义务。
# 函数用途: 验证批准准备遇到未知执行结果时不登记退避或触碰正式库。
def test_pending_approval_uncertain_outcome_is_not_deferred(tmp_path, monkeypatch):
    from agent_py_agent.agent.common.global_write_commit import GlobalWriteCommitUnavailable

    service, row = _pending_automatic_candidate(tmp_path)
    failure = GlobalWriteCommitUnavailable("unknown outcome")

    # LLM: 只注入原类型异常，不伪造成功或把执行未知改成普通超时。
    # 函数用途: 暴露未知结果到晋升入口。
    def uncertain(*args, **kwargs):
        raise failure

    monkeypatch.setattr(service.candidates, "transition", uncertain)
    with patch.object(service.candidates, "defer_commit", wraps=service.candidates.defer_commit) as deferred:
        with pytest.raises(GlobalWriteCommitUnavailable) as raised:
            service.promote(row.candidate_id, automatic=True)
    assert raised.value is failure and not deferred.called
    assert service.candidates.get(row.candidate_id) == row
    assert not service.long_term.all()


# LLM: CAS后的新内容或新来源不能继承旧核验；重新核门发现排除或证据失效时只返回竞争结果。
# 函数用途: 防止在错误收口路径绕过来源、证据或绝对权威门新增义务。
@pytest.mark.parametrize("change", ["excluded", "missing_source", "bad_evidence", "no_authority"])
def test_pending_approval_cas_does_not_defer_unqualified_snapshot(tmp_path, monkeypatch, change):
    service, row = _pending_automatic_candidate(tmp_path)
    original = service.candidates.transition

    # LLM: 只在旧批准之前写入新的不合格快照；新状态仍pending，以证明拒绝来自重核而非终态短路。
    # 函数用途: 模拟核验通过后另一写入者改变晋升资格。
    def race_unqualified(*args, **kwargs):
        monkeypatch.setattr(service.candidates, "transition", original)
        changes = {"excluded": {"promotion_exclusion": "LEARNING_SOURCE_EXCLUDED"},
            "missing_source": {"learning_source": {}, "learning_source_coverage": {}},
            "bad_evidence": {"source_tool_refs": [{"operation_id": "failed"}], "learning_source_coverage": {}},
            "no_authority": {"candidate_type": "long_term_fact"}}
        raced = replace(row, **changes[change])
        module._write_candidates_unlocked(service.candidates.path, [raced], admission=module._NullAdmission())
        return original(*args, **kwargs)

    monkeypatch.setattr(service.candidates, "transition", race_unqualified)
    result = service.promote(row.candidate_id, automatic=True)
    saved = service.candidates.get(row.candidate_id)
    assert result.reason_code == "CANDIDATE_CHANGED_AFTER_VERIFICATION"
    assert saved.status == "pending_review" and not saved.commit_obligation
    assert not saved.obligation_retry_after and not service.long_term.all()


# LLM: 故障从真实批准写入原语抛出而非替换状态机；退避必须在原锁释放后沿同一账本耐久落下。
# 函数用途: 覆盖批准准备原子写IO失败的真实仓库边界。
def test_pending_approval_atomic_io_failure_is_deferred(tmp_path, monkeypatch):
    service, row = _pending_automatic_candidate(tmp_path)
    original = module.write_private_text_file_atomic_unlocked

    # LLM: 只令第一次候选原子写失败，随后恢复真实原语，避免故障替身帮助保存义务。
    # 函数用途: 注入批准准备落盘失败。
    def fail_once(*args, **kwargs):
        monkeypatch.setattr(module, "write_private_text_file_atomic_unlocked", original)
        raise OSError("approval write failed")

    monkeypatch.setattr(module, "write_private_text_file_atomic_unlocked", fail_once)
    assert service.promote(row.candidate_id, automatic=True).reason_code == "PROMOTION_IO_BLOCKED"
    saved = module.CandidateService(service.candidates.path).get(row.candidate_id)
    assert saved.status == "pending_review" and saved.commit_obligation and saved.obligation_retry_after
    assert not service.long_term.all()


# LLM: 已批准后的正式写入口同样保留执行未知原对象，不让异常分类或治理收尾伪造可重放结果。
# 函数用途: 验证正式提交未知时不会登记普通IO退避。
def test_formal_commit_uncertain_outcome_is_not_deferred(tmp_path, monkeypatch):
    from agent_py_agent.agent.common.global_write_commit import GlobalWriteCommitUnavailable

    service, row = _pending_automatic_candidate(tmp_path)
    failure = GlobalWriteCommitUnavailable("unknown formal commit")

    # LLM: 未知失败不构造目标提交回执，也不声称已有正式副作用。
    # 函数用途: 在正式提交入口暴露未知状态。
    def uncertain(*args, **kwargs):
        raise failure

    monkeypatch.setattr(service, "_commit_target", uncertain)
    with patch.object(service.candidates, "defer_commit", wraps=service.candidates.defer_commit) as deferred:
        with pytest.raises(GlobalWriteCommitUnavailable) as raised:
            service.promote(row.candidate_id, automatic=True)
    saved = service.candidates.get(row.candidate_id)
    assert raised.value is failure and not deferred.called
    assert saved.status == "approved" and saved.commit_obligation and not saved.obligation_retry_after
    assert not service.long_term.all()


# LLM: 首拒草稿经真实证据拒绝入口创建，嵌套未知字段既要保真也不能与重建结果共享容器。
# 函数用途: 验证blocked_missing_evidence再次observe及纯重建均保留未来合同。
def test_rejected_draft_retry_preserves_unknown_fields(tmp_path):
    from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope

    service = module.CandidateService(tmp_path / "candidates.jsonl")
    observation = CandidateObservation(candidate_type="lesson", content="重试前补齐证据", subject_key="retry.proof",
        scope=MemoryScope("personal", "personal"), origin="model_inferred", promotion_target="lesson")
    service.observe_evidence_batch([observation], ["TOOL_EVIDENCE_NOT_SUCCEEDED"])
    blocked = service.list()[0]
    blocked._extra_fields = {"future_contract": {"proofs": ["original"], "flags": {"enabled": True}}}
    module._write_candidates_unlocked(service.path, [blocked], admission=module._NullAdmission())
    restored = service.observe(observation)
    assert restored.status == "pending_review" and restored.candidate_id == blocked.candidate_id
    assert restored.to_record()["future_contract"] == blocked.to_record()["future_contract"]
    assert module.CandidateService(service.path).get(blocked.candidate_id)._extra_fields == blocked._extra_fields
    rebuilt = module._retry_rejected_draft(blocked, observation, "observation-retry")
    rebuilt._extra_fields["future_contract"]["proofs"].append("caller")
    assert blocked._extra_fields["future_contract"]["proofs"] == ["original"]


# LLM: dataclass替换、冷热stub和回填都是重建路径；未来字段必须完整且每个新模型拥有独立嵌套容器。
# 函数用途: 验证冷热归档、缓存读写及普通转换不会丢失或共享未知顶层字段。
def test_unknown_fields_are_isolated_across_cold_hot_rebuilds(tmp_path):
    row = candidate()
    row._extra_fields = {"future_contract": {"proofs": ["original"]}}
    converted = replace(row, review_note="metadata")
    converted._extra_fields["future_contract"]["proofs"].append("converted")
    assert row._extra_fields["future_contract"]["proofs"] == ["original"]
    path = tmp_path / "candidates.jsonl"
    service = module.CandidateService(path)
    module._write_candidates_unlocked(path, [row], admission=module._NullAdmission())
    rejected = service.transition(row.candidate_id, "rejected", reviewer="owner")
    now = datetime.now(timezone.utc)
    assert service.compact_terminal_candidates(now=now + timedelta(days=40)) == 1
    module._drop_candidate_load_cache(path)
    stub = service.list()[0]
    assert stub.cold_detail_ref and stub._extra_fields == row._extra_fields
    restored = service.get(row.candidate_id)
    assert restored._extra_fields == rejected._extra_fields
    assert restored.source_message_refs == rejected.source_message_refs
    restored._extra_fields["future_contract"]["proofs"].append("caller")
    assert service.get(row.candidate_id)._extra_fields == row._extra_fields
    replay = service.observe(_unknown_field_replay_observation())
    assert replay._extra_fields == row._extra_fields
    assert not replay.cold_detail_ref


# LLM: 重放使用现有candidate夹具的同一结构化身份，不能借正文近似触发另一条候选。
# 函数用途: 构造冷热回填测试的合法同身份观察。
def _unknown_field_replay_observation():
    from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation

    row = candidate()
    return CandidateObservation(candidate_type=row.candidate_type, content=row.content, subject_key=row.subject_key,
        scope=row.scope, origin=row.origin, promotion_target=row.promotion_target,
        source_message_refs=tuple(row.source_message_refs))


# LLM: 稳定别名补全不能加票，含artifact_id的细粒度引用也不能退化成run级身份吞掉独立产物。
# 函数用途: 验证正反补全、同operation补run及不同artifact的计数，并跨实例读取持久结果。
@pytest.mark.parametrize("first_ref,second_ref,expected", [
    ({"artifact_id": "a"}, {"artifact_id": "a", "run_id": "r", "operation_id": "op"}, 1),
    ({"artifact_id": "a", "run_id": "r", "operation_id": "op"}, {"artifact_id": "a"}, 1),
    ({"artifact_id": "a", "run_id": "r"}, {"artifact_id": "b", "run_id": "r"}, 2),
    ({"operation_id": "op"}, {"operation_id": "op", "run_id": "r"}, 1),
    ({"operation_id": "op", "run_id": "r"}, {"operation_id": "op"}, 1),
    ({"tool_call_id": "call"}, {"call_id": "call", "run_id": "r"}, 1),
    ({"artifact_ref": "artifact"}, {"artifact_ref": "artifact", "run_id": "r"}, 1),
    ({"call_id": "call", "run_id": "r1"}, {"call_id": "call", "run_id": "r2"}, 2),
])
def test_source_alias_enrichment_does_not_add_votes(tmp_path, first_ref, second_ref, expected):
    from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope

    service = module.CandidateService(tmp_path / "candidates.jsonl")
    first = CandidateObservation(candidate_type="lesson", content="先核对来源", subject_key="source.identity",
        scope=MemoryScope("personal", "personal"), origin="model_inferred", promotion_target="lesson",
        source_artifact_refs=(first_ref,), source_run_ids=("r",))
    row = service.observe(first)
    second = replace(first, source_artifact_refs=(second_ref,), observation_id="changed-host-observation")
    merged = service.observe(second)
    assert merged.candidate_id == row.candidate_id and merged.occurrence_count == expected
    assert module.CandidateService(service.path).get(row.candidate_id).occurrence_count == expected


# LLM: 保留原始缺run证据不能让通配别名永久吞掉后续明确异run；各步都跨实例读取真实JSONL。
# 函数用途: 验证元数据补全收敛后，第三次独立执行恰好增加一票。
@pytest.mark.parametrize("alias", ["operation_id", "tool_call_id", "call_id", "artifact_ref", "artifact_id"])
def test_enriched_alias_does_not_hide_later_independent_run(tmp_path, alias):
    from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope

    path = tmp_path / "candidates.jsonl"
    observation = CandidateObservation(candidate_type="lesson", content="先核对来源", subject_key="source.identity",
        scope=MemoryScope("personal", "personal"), origin="model_inferred", promotion_target="lesson",
        source_artifact_refs=({alias: "same-local-id"},))
    row = module.CandidateService(path).observe(observation)
    assert row.occurrence_count == 1
    enriched = replace(observation, source_artifact_refs=({alias: "same-local-id", "run_id": "r1"},))
    assert module.CandidateService(path).observe(enriched).occurrence_count == 1
    independent = replace(observation, source_artifact_refs=({alias: "same-local-id", "run_id": "r2"},))
    assert module.CandidateService(path).observe(independent).occurrence_count == 2
    assert module.CandidateService(path).observe(enriched).occurrence_count == 2
    assert module.CandidateService(path).observe(observation).occurrence_count == 2


# LLM: 同组别名只补其中一项run时须收敛整组；保留原始引用，并跨重读验证反向补全、异run和旧run重放。
# 函数用途: 复现混合operation/call/artifact历史空run吞掉第三次独立执行的问题。
@pytest.mark.parametrize("left,right", [("operation_id", "tool_call_id"), ("artifact_id", "call_id"),
    ("artifact_ref", "operation_id")])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("source_field", ["source_tool_refs", "source_artifact_refs"])
def test_mixed_alias_group_enrichment_survives_reload(tmp_path, left, right, reverse, source_field):
    from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope

    path = tmp_path / "candidates.jsonl"
    base = CandidateObservation(candidate_type="lesson", content="先核对来源", subject_key="mixed.identity",
        scope=MemoryScope("personal", "personal"), origin="model_inferred", promotion_target="lesson")
    raw = {left: "left", right: "right"}
    qualified = {left: "left", "run_id": "r1"}
    initial = [qualified, raw] if reverse else [raw, qualified]
    for ref, expected in [*[(ref, 1) for ref in initial],
            ({**raw, "run_id": "r2"}, 2), ({right: "right", "run_id": "r1"}, 2), (raw, 2)]:
        row = module.CandidateService(path).observe(replace(base, **{source_field: (ref,)}))
        assert row.occurrence_count == expected
    persisted = module.CandidateService(path).get(row.candidate_id)
    assert raw in getattr(persisted, source_field), "原始来源不能因补全而丢失"


# LLM: 同次执行的缺run组可能跨工具/产物引用形成链；只投影计数，不改证据，绝不只靠共同run关联不同产物。
# 函数用途: 验证别名组的间接上下文在重读后也不会误吞新的执行。
def test_chained_alias_group_enrichment_survives_reload(tmp_path):
    from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope

    path = tmp_path / "candidates.jsonl"
    base = CandidateObservation(candidate_type="lesson", content="先核对来源", subject_key="chained.identity",
        scope=MemoryScope("personal", "personal"), origin="model_inferred", promotion_target="lesson",
        source_tool_refs=({"operation_id": "op", "tool_call_id": "call"},),
        source_artifact_refs=({"tool_call_id": "call", "artifact_id": "artifact"},))
    assert module.CandidateService(path).observe(base).occurrence_count == 1
    qualified = replace(base, source_tool_refs=({"operation_id": "op", "run_id": "r1"},), source_artifact_refs=())
    assert module.CandidateService(path).observe(qualified).occurrence_count == 1
    independent = replace(base, source_tool_refs=(), source_artifact_refs=({"artifact_id": "artifact", "run_id": "r2"},))
    assert module.CandidateService(path).observe(independent).occurrence_count == 2


# LLM: 缺run链的两端已知run冲突时只能保守计数投影，不能把某一端归属写入真实证据或放宽学习覆盖。
# 函数用途: 沿真实observe及重读验证有歧义关联组保留原始来源，不持久化补猜run。
def test_ambiguous_alias_chain_does_not_guess_run(tmp_path):
    from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope

    refs = [{"operation_id": "op", "run_id": "r1"}, {"artifact_id": "artifact", "run_id": "r2"},
        {"operation_id": "op", "tool_call_id": "call"}, {"tool_call_id": "call", "artifact_id": "artifact"}]
    path = tmp_path / "candidates.jsonl"
    observation = CandidateObservation(candidate_type="lesson", content="先核对来源", subject_key="ambiguous.identity",
        scope=MemoryScope("personal", "personal"), origin="model_inferred", promotion_target="lesson",
        source_tool_refs=tuple(refs))
    row = module.CandidateService(path).observe(observation)
    replay = replace(observation, source_tool_refs=({"tool_call_id": "call", "run_id": "r1"},))
    assert module.CandidateService(path).observe(replay).occurrence_count == 1
    persisted = module.CandidateService(path).get(row.candidate_id)
    assert persisted.source_tool_refs == [*refs, *replay.source_tool_refs]
    assert not persisted.source_artifact_refs
