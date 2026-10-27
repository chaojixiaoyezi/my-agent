# LLM: M1回归只操作临时owner账本；观察真实读写、缓存与事务，不访问生产记忆或在线模型。
# 模块用途: 验证原行复用、写后缓存、提交跳写、增量预演及冷归档安全闭环。
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_py_agent.agent.memory_store import candidates as module
from agent_py_agent.agent.memory_store.candidate_models import MemoryCandidate, validate_candidate
from agent_py_agent.tests.test_memory_candidate_daily_v2 import _user_observation


# LLM: 使用真实观察与模型构造器准备独占临时记录，不绕过schema或来源校验。
# 函数用途: 生成一条可用于内存和磁盘测试的合法候选。
def candidate(index=0):
    observation = replace(_user_observation(message_id=f"message-{index}"), subject_key=f"subject.{index}")
    normalized, cid, key = module._prepare_observations([observation])[0]
    return module._new_candidate(normalized, cid, key)


# LLM: 原行/摘要仅实例缓存，不属于dataclass字段和asdict；保留构造拒绝、比较无关及replace清空语义。
# 函数用途: 验证原行复用不会把缓存全文复制到对外候选或新快照。
def test_raw_line_is_private_and_replace_clears_it():
    row = candidate()
    assert not {"_raw_json_line", "_raw_state_digest"}.intersection(row.__dataclass_fields__)
    row._raw_json_line = "old raw"
    row._raw_state_digest = "old digest"
    assert not {"_raw_json_line", "_raw_state_digest"}.intersection(asdict(row))
    assert "old raw" not in repr(row) and "old digest" not in repr(row)
    assert candidate(1)._raw_json_line == "" and candidate(1)._raw_state_digest == ""
    assert row == replace(row)
    assert replace(row)._raw_json_line == ""
    assert replace(row)._raw_state_digest == ""
    assert "_raw_json_line" not in row.to_record()
    with pytest.raises(TypeError, match="_raw_json_line"):
        replace(row, _raw_json_line="cannot construct cache")


# LLM: 直接字段变更和嵌套字典改写均不能让旧JSON复活；复用行仍可重新读取为同一完整模型。
# 函数用途: 验证未改行零to_record、已改行重新序列化以及嵌套失效。
def test_serializer_reuses_unchanged_lines_but_not_changed_state():
    serialize = getattr(module, "_serialize_candidates", None)
    assert callable(serialize), "缺少共用原行序列化入口"
    row = candidate()
    first = serialize([row])
    with patch.object(MemoryCandidate, "to_record", side_effect=AssertionError("unchanged row reserialized")):
        assert serialize([row]) == first
    row.promotion_exclusion = "LEARNING_SOURCE_EXCLUDED"
    assert row._raw_json_line == ""
    assert json.loads(serialize([row]))["promotion_exclusion"] == "LEARNING_SOURCE_EXCLUDED"
    row.status_history[-1]["note"] = "changed nested note"
    assert json.loads(serialize([row]))["status_history"][-1]["note"] == "changed nested note"
    row.source_message_refs[0]["quote"] = "changed quote"
    with pytest.raises(ValueError, match="coverage"):
        serialize([row])


# LLM: 冷加载只流式构造模型；保留坏行和归一化的失败关闭，不调用整表字典读取器。
# 函数用途: 验证原行保留与旧schema归一化后不复用过时权限字段。
def test_stream_parser_preserves_only_normalized_raw_lines(tmp_path):
    row = candidate()
    raw = json.dumps(row.to_record(), ensure_ascii=False)
    path = tmp_path / "candidates.jsonl"
    path.write_text(raw + "\n")
    service = module.CandidateService(path)
    with patch.object(Path, "read_text", side_effect=AssertionError("whole ledger read")):
        loaded = service._parse_candidate_ledger_unlocked()[1][0]
    assert loaded._raw_json_line == raw
    payload = row.to_record()
    payload["promotion_mode"] = "manual_required"
    path.write_text(json.dumps(payload) + "\n")
    normalized = service._parse_candidate_ledger_unlocked()[1][0]
    assert normalized.promotion_mode == "auto_eligible" and normalized._raw_json_line == ""


# LLM: 真实原子写之后只读/get必须复用新代缓存；返回值和输入的嵌套对象都不能污染缓存。
# 函数用途: 验证大账本写后零重读、独立返回副本和readonly内部借用。
def test_successful_write_publishes_owned_cache_without_disk_reread(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "_CANDIDATE_FAST_STAT_PROBE_MIN_BYTES", 1)
    path = tmp_path / "candidates.jsonl"
    rows = [candidate()]
    service = module.CandidateService(path)
    module._write_candidates_unlocked(path, rows, admission=module._NullAdmission())
    expected = deepcopy(rows[0])
    rows[0].source_message_refs[0]["quote"] = "caller poison"
    with patch.object(Path, "read_bytes", side_effect=AssertionError("write then disk reread")):
        first = service.get(expected.candidate_id)
        assert first == expected
        first.scope["scope_key"] = "caller poison"
        assert service.get(expected.candidate_id) == expected
        assert service.readonly_rows()[0] is module._CANDIDATE_LOAD_CACHE[str(path)][2][0]


# LLM: 原子写失败后不得发表准备态；即使writer在replace后抛错，缓存也必须失效并重新读取。
# 函数用途: 验证失败及不确定结果不会留下旧代或未提交缓存。
def test_failed_write_invalidates_both_caches(tmp_path):
    path = tmp_path / "candidates.jsonl"
    service = module.CandidateService(path)
    service.observe(_user_observation())
    with patch.object(module, "write_private_text_file_atomic_unlocked", side_effect=OSError("disk failed")):
        with pytest.raises(OSError, match="disk failed"):
            module._write_candidates_unlocked(path, [candidate(2)], admission=module._NullAdmission())
    assert str(path) not in module._CANDIDATE_LOAD_CACHE
    assert str(path) not in module._CANDIDATE_STAMP_DIGEST_CACHE


# LLM: 归一化可改变模型权限与引用，写入只能修改owned副本，不得污染调用者或既有缓存。
# 函数用途: 验证写前归一化与缓存所有权在公开返回以外也成立。
def test_writer_normalizes_owned_copy(tmp_path):
    row = candidate()
    row.promotion_mode = "manual_required"
    module._write_candidates_unlocked(tmp_path / "candidates.jsonl", [row], admission=module._NullAdmission())
    assert row.promotion_mode == "manual_required"
    loaded = module.CandidateService(tmp_path / "candidates.jsonl").get(row.candidate_id)
    assert loaded.promotion_mode == "auto_eligible"
    validate_candidate(loaded)


# LLM: 合并预备必须沿原CAS与状态机，不能因一个快捷入口绕过用户确认或正式提交。
# 函数用途: 验证补默认落点和批准只写一次，同时拒绝陈旧快照。
def test_automatic_preparation_combines_default_target_and_approval(tmp_path):
    service = module.CandidateService(tmp_path / "candidates.jsonl")
    original = service.observe(replace(_user_observation(), promotion_target="none"))
    prepare = getattr(service, "prepare_automatic_promotion", None)
    assert callable(prepare), "缺少单写晋升准备入口"
    with patch.object(module, "_write_candidates_unlocked", wraps=module._write_candidates_unlocked) as writer:
        prepared = prepare(original, reviewer="promotion-service", reason="verified")
    assert writer.call_count == 1
    assert prepared.status == "approved" and prepared.promotion_target == "long_term"
    with pytest.raises(module.CandidateStateChangedError):
        prepare(original, reviewer="promotion-service")


# LLM: 仅spy索引构建次数，实际合并/冲突算法照跑；拒绝观察不得留下来源版本约束污染下一条。
# 函数用途: 验证冲突预演一次建索引且拒绝项没有半合并副作用。
def test_split_conflicts_builds_indices_once_and_never_mutates_rejected_state():
    original = replace(_user_observation(), observation_id="same-source")
    existing, _ = module.merge_candidate_observations([], [original])
    bad = replace(original, subject_key="wrong.subject", target_entry_id="entry-1", proposed_action="replace", target_entry_version=1)
    good = replace(bad, observation_id="fresh-source", target_entry_version=2,
        source_message_refs=({"message_id": "fresh", "thread_id": "thread-1"},))
    later = replace(_user_observation(message_id="other"), subject_key="later")
    with patch.object(module, "TargetEvidenceVersionBindings", wraps=module.TargetEvidenceVersionBindings) as index:
        kept, rejected = module.split_identity_conflicts(existing, [bad, good, later])
    assert index.call_count == 1
    assert kept == (good, later) and rejected == 1
    assert existing[0].observation_keys == [module._prepare_observations([original])[0][2]]


# LLM: 使用真实四仓库和租约构造批次；不替换事务门、锁或state最后提交协议。
# 函数用途: 准备可直接提交的Curator批次和候选仓库。
def batch_case(tmp_path, observations=()):
    from agent_py_agent.agent.memory_store.curator_commit import (
        CuratorBatchCommit,
        CuratorBatchCommitter,
    )
    from agent_py_agent.agent.memory_store.curator_run_log import CuratorRunLog, CuratorRunRecord
    from agent_py_agent.agent.memory_store.curator_state import (
        CuratorSuccessCommit,
        MemoryCuratorStateStore,
    )
    from agent_py_agent.agent.memory_store.daily import DailyMemoryStore

    root = tmp_path / "memory"
    service = module.CandidateService(root / "candidates.jsonl")
    state = MemoryCuratorStateStore(root / "curator/state.json")
    _current, lease = state.acquire(reason="admin", config_revision="test", lease_seconds=120)
    committer = CuratorBatchCommitter(candidate_service=service, daily_store=DailyMemoryStore(root / "daily"),
        state_store=state, run_log=CuratorRunLog(root / "curator/runs"))
    now = module.utc_now_iso()
    record = CuratorRunRecord(lease["run_id"], lease["lease_id"], "succeeded", "admin", "test", "test", now, now,
        candidates=len(observations))
    success = CuratorSuccessCommit(lease["lease_id"], lease["run_id"], "admin", {}, "", 0, 0, len(observations), 0)
    return service, committer, CuratorBatchCommit(record, success, candidate_observations=tuple(observations))


# LLM: 文件不变时既不读取/备份也不替换，锁集合仍含候选路径，state仍最后写。
# 函数用途: 验证空观察批次的真正提交路径完全跳过候选大账本。
def test_curator_empty_batch_skips_candidate_load_backup_and_rewrite(tmp_path):
    service, committer, request = batch_case(tmp_path)
    service.observe(_user_observation())
    before = service.path.read_bytes()
    with patch.object(service, "_load_unlocked", side_effect=AssertionError("unchanged ledger loaded")), patch.object(
        committer, "_prepare_transaction", wraps=committer._prepare_transaction) as prepared, patch.object(
        committer, "_write_target", wraps=committer._write_target) as writes:
        committer.commit(request)
    assert service.path in committer._target_paths(request)
    assert service.path not in prepared.call_args.args[1]
    assert service.path not in [call.args[0] for call in writes.call_args_list]
    assert writes.call_args_list[-1].args[0] == committer.state.path
    assert service.path.read_bytes() == before


# LLM: 有观察不等于有逻辑变化；同源重放完全归一后相等时不能产生候选备份/重写。
# 函数用途: 验证非空幂等观察也走真正的无变更跳写。
def test_curator_replayed_observation_skips_unchanged_ledger(tmp_path):
    observation = _user_observation()
    service, committer, request = batch_case(tmp_path, [observation])
    service.observe(observation)
    with patch.object(committer, "_write_target", wraps=committer._write_target) as writes:
        committer.commit(request)
    assert service.path not in [call.args[0] for call in writes.call_args_list]


# LLM: 缺失候选源仍须创建合法空账本，不能把“无变化”误当初始化已完成。
# 函数用途: 验证缺失账本在空批次里正常进入事务清单。
def test_curator_creates_missing_empty_candidate_ledger(tmp_path):
    service, committer, request = batch_case(tmp_path)
    committer.commit(request)
    assert service.path.is_file() and service.path.read_bytes() == b""


# LLM: 冷热模型由真实观察/审核生成，只有测试显式改旧时间，紧接审核的记录必须保留完整详情。
# 函数用途: 准备带历史和证据的老终态记录与一条新终态。
def terminal_case(tmp_path):
    service = module.CandidateService(tmp_path / "memory/candidates.jsonl")
    row = service.observe(_user_observation())
    old = service.transition(row.candidate_id, "rejected", reviewer="test", review_note="full history")
    aged = replace(old, updated_at="2020-02-03T00:00:00+00:00")
    with service._locked_candidates():
        module._write_candidates_unlocked(service.path, [aged], admission=module._NullAdmission())
    fresh = service.observe(replace(_user_observation(message_id="fresh"), subject_key="fresh"))
    fresh = service.transition(fresh.candidate_id, "rejected", reviewer="test")
    compact = getattr(service, "compact_terminal_candidates", None)
    assert callable(compact), "缺少终态冷热分离维护入口"
    return service, aged, fresh


# LLM: 热行保全部观察去重和版本绑定，详情必须由get完整回填；新终态不能立即瘦身。
# 函数用途: 验证真实冷热分离、私有归档及两个校验器完整通过。
def test_terminal_archive_slims_only_old_rows_and_hydrates_details(tmp_path):
    from agent_py_agent.agent.memory_store.candidate_source_coverage import validate_source_coverage

    service, aged, fresh = terminal_case(tmp_path)
    assert service.compact_terminal_candidates() == 1
    rows = {row.candidate_id: row for row in service.readonly_rows()}
    hot = rows[aged.candidate_id]
    assert hot.cold_detail_ref and not hot.evidence_refs and not hot.source_message_refs
    assert hot.observation_keys == aged.observation_keys and hot.occurrence_count == len(hot.observation_keys)
    assert hot.status_history == [aged.status_history[-1]] and hot.learning_source_coverage == {}
    validate_candidate(hot)
    validate_source_coverage(hot)
    detail = service.get(aged.candidate_id)
    assert detail.source_message_refs == aged.source_message_refs and detail.status_history == aged.status_history
    assert service.get(fresh.candidate_id).source_message_refs == fresh.source_message_refs
    assert not rows[fresh.candidate_id].cold_detail_ref
    archives = list((service.path.parent / "candidates_archive").glob("*.jsonl"))
    assert len(archives) == 1 and archives[0].stat().st_mode & 0o777 == 0o600
    assert service.compact_terminal_candidates() == 0


def test_r2_cold_archive_verification_without_python311_file_digest(tmp_path, monkeypatch):
    service, aged, _ = terminal_case(tmp_path)
    monkeypatch.delattr(module.hashlib, "file_digest", raising=False)
    assert service.compact_terminal_candidates() == 1
    assert service.get(aged.candidate_id).source_message_refs == aged.source_message_refs


# LLM: 冷引用不是任意路径读权；归档不存在、内容篡改或目录链接都必须明确损坏而非返回残缺详情。
# 函数用途: 验证冷详情校验与no-follow边界。
@pytest.mark.parametrize("damage", ["missing", "tamper", "symlink"])
def test_cold_detail_damage_fails_closed(tmp_path, damage):
    service, aged, _ = terminal_case(tmp_path)
    service.compact_terminal_candidates()
    archive = next((service.path.parent / "candidates_archive").glob("*.jsonl"))
    if damage == "missing":
        archive.unlink()
    elif damage == "tamper":
        archive.write_text(archive.read_text().replace("macOS", "LINUX"))
    else:
        real = archive.parent.with_name("external")
        archive.parent.rename(real)
        archive.parent.symlink_to(real, target_is_directory=True)
    with pytest.raises(module.CandidateStoreCorruptError):
        service.get(aged.candidate_id)


# LLM: 正式删除的隐私责任覆盖全部归档版本，不能仅清热行或留下可直接读的冷正文/quote。
# 函数用途: 验证候选脱敏同时清理冷热记录并保持后续详情可读。
def test_redaction_removes_plaintext_from_hot_and_cold(tmp_path):
    service, aged, _ = terminal_case(tmp_path)
    service.compact_terminal_candidates()
    assert service.redact_content(content_hashes={aged.content_hash}) >= 1
    for path in [service.path, *(service.path.parent / "candidates_archive").glob("*.jsonl")]:
        assert "macOS" not in path.read_text()
    detail = service.get(aged.candidate_id)
    assert detail.content == "" and not detail.source_message_refs
    validate_candidate(detail)


# LLM: 原状态机不允许同状态参数更新绕过终态，新增晋升准备不能顺带放开此权限。
# 函数用途: 验证终态重复审核保持原幂等无修改语义。
def test_same_status_terminal_transition_does_not_change_target(tmp_path):
    service = module.CandidateService(tmp_path / "candidates.jsonl")
    row = service.observe(_user_observation())
    terminal = service.transition(row.candidate_id, "rejected", reviewer="test")
    repeated = service.transition(terminal, "rejected", reviewer="test", promotion_target="none")
    assert repeated == terminal


# LLM: 单写默认目标修复不能放开已批准候选的其它同状态改写；原来幂等的调用继续幂等。
# 函数用途: 验证批准态不会借promotion_target参数更换其它已审内容。
def test_same_status_approved_transition_preserves_unrelated_idempotency(tmp_path):
    service = module.CandidateService(tmp_path / "candidates.jsonl")
    row = service.observe(_user_observation())
    approved = service.transition(row.candidate_id, "approved", reviewer="test")
    repeated = service.transition(approved, "approved", reviewer="test", promotion_target="hot", target_entry_id="other")
    assert repeated == approved


# LLM: retention_candidates是规划读口，不能把新维护挂到只读规划上；执行delete_terminal时才允许归档其它老终态。
# 函数用途: 验证retention执行集成而非dry-run偷偷写冷文件。
def test_retention_planning_is_readonly_and_execution_compacts_other_terminal_rows(tmp_path):
    service, aged, fresh = terminal_case(tmp_path)
    before = service.path.read_bytes()
    service.retention_candidates(before=datetime.now(timezone.utc))
    assert service.path.read_bytes() == before and not (service.path.parent / "candidates_archive").exists()
    assert service.delete_terminal([fresh.candidate_id]) == 1
    assert service.readonly_rows()[0].cold_detail_ref
    assert service.get(aged.candidate_id).status_history == aged.status_history


# LLM: 授权删除不先hydrate，冷写成功/热写失败后的重试仍可从热元数据完成，普通get继续fail-closed。
# 函数用途: 验证脱敏在中途失败后可以安全重试，不将旧ref悄悄映射到其它版本。
def test_redaction_retry_recovers_after_cold_commit_and_hot_write_failure(tmp_path):
    service, aged, _ = terminal_case(tmp_path)
    service.compact_terminal_candidates()
    with patch.object(module, "write_private_text_file_atomic_unlocked", side_effect=OSError("hot failed")):
        with pytest.raises(OSError, match="hot failed"):
            service.redact_content(content_hashes={aged.content_hash})
    with pytest.raises(module.CandidateStoreCorruptError):
        service.get(aged.candidate_id)
    assert service.redact_content(content_hashes={aged.content_hash}) >= 1
    assert service.get(aged.candidate_id).content == ""
    assert all("macOS" not in p.read_text() for p in (service.path.parent / "candidates_archive").glob("*.jsonl"))


# LLM: 归档成功而热写失败会产生孤儿详情；显式脱敏在热行已不存在时仍必须清理全部月份。
# 函数用途: 验证没有待写redacted热行也不会遗漏冷隐私。
def test_redaction_cleans_orphan_archive_without_hot_rows(tmp_path):
    service, aged, _ = terminal_case(tmp_path)
    service.compact_terminal_candidates()
    module._write_candidates_unlocked(service.path, [], admission=module._NullAdmission())
    assert service.redact_content(content_hashes={aged.content_hash}) == 0
    assert all("macOS" not in p.read_text() for p in (service.path.parent / "candidates_archive").glob("*.jsonl"))


# LLM: cold full详情只对实际命中观察回填，合并后不得把新证据藏在旧ref后，也不得丢归档历史。
# 函数用途: 验证冷行再观察时正确回热及观察去重。
def test_observation_of_cold_candidate_preserves_old_and_new_details(tmp_path):
    service, aged, _ = terminal_case(tmp_path)
    service.compact_terminal_candidates()
    merged = service.observe(_user_observation(message_id="additional"))
    assert merged.candidate_id == aged.candidate_id and merged.occurrence_count == 2
    assert not merged.cold_detail_ref
    assert len(merged.source_message_refs) == 2
    assert merged.status_history == aged.status_history
    assert service.get(merged.candidate_id) == merged


# LLM: no-follow并不授权另一个owner的同ID/hash记录；owner根进入归档hash且必须对应当前仓库。
# 函数用途: 验证跨owner复制的冷文件被拒绝。
def test_cold_archive_cannot_be_reused_by_another_owner(tmp_path):
    service, aged, _ = terminal_case(tmp_path / "owner-a")
    service.compact_terminal_candidates()
    other = module.CandidateService(tmp_path / "owner-b/memory/candidates.jsonl")
    other.path.write_bytes(service.path.read_bytes())
    target = other.path.parent / "candidates_archive"
    target.mkdir()
    for path in (service.path.parent / "candidates_archive").glob("*.jsonl"):
        (target / path.name).write_bytes(path.read_bytes())
    with pytest.raises(module.CandidateStoreCorruptError):
        other.get(aged.candidate_id)


# LLM: 冷冷热版本相互独立，终态去重键和来源目标版本绑定必须在热stub中继续生效。
# 函数用途: 验证归档后旧证据不能刷新正式目标版本。
def test_cold_candidate_keeps_target_evidence_version_bindings(tmp_path):
    service = module.CandidateService(tmp_path / "memory/candidates.jsonl")
    observation = replace(_user_observation(), proposed_action="replace", target_entry_id="entry-1", target_entry_version=1)
    row = service.observe(observation)
    row = service.transition(row.candidate_id, "rejected", reviewer="test")
    row = replace(row, updated_at="2020-01-01T00:00:00+00:00")
    module._write_candidates_unlocked(service.path, [row], admission=module._NullAdmission())
    service.compact_terminal_candidates()
    assert service.readonly_rows()[0].target_evidence_bindings
    with pytest.raises(module.CandidateIdentityConflictError):
        service.observe(replace(observation, target_entry_version=2))


# LLM: 事务失败后既恢复磁盘旧代也清除内存半提交代；不能靠下一次恰好重hash才纠正缓存。
# 函数用途: 验证Curator写候选后、state提交前失败的缓存回滚。
def test_curator_rollback_invalidates_published_intermediate_candidate_cache(tmp_path):
    from agent_py_agent.agent.memory_store.curator_commit import CuratorBatchCommitError

    service, committer, request = batch_case(tmp_path, [_user_observation(message_id="new")])
    old = service.observe(replace(_user_observation(), subject_key="old"))
    write = committer._write_target

    # LLM: 仅在真实候选替换后预热缓存，state末写处注入磁盘错误，保留真实回滚链。
    # 函数用途: 复现已被其它只读入口观察到的中间代缓存。
    def fail_state(path, text):
        if path == committer.state.path:
            service._load_unlocked()
            raise OSError("state failed")
        write(path, text)

    with patch.object(committer, "_write_target", side_effect=fail_state):
        with pytest.raises(CuratorBatchCommitError):
            committer.commit(request)
    assert str(service.path) not in module._CANDIDATE_LOAD_CACHE
    assert service.list() == [old]


# LLM: 同一维护批次可既清旧冷详情又归档新行；两个计划不能按月份字典覆盖导致新ref丢失。
# 函数用途: 验证同月归档与删除组合时两种变更都被保留。
def test_retention_combines_archive_and_redaction_in_the_same_month(tmp_path):
    service, aged, _ = terminal_case(tmp_path)
    service.compact_terminal_candidates()
    other = service.observe(replace(_user_observation(message_id="other"), subject_key="another.old"))
    other = service.transition(other.candidate_id, "rejected", reviewer="test")
    with service._locked_candidates():
        rows = service._load_unlocked()
        rows = [replace(row, updated_at=aged.updated_at) if row.candidate_id == other.candidate_id else row for row in rows]
        module._write_candidates_unlocked(service.path, rows, admission=module._NullAdmission())
    assert service.delete_terminal([aged.candidate_id]) == 1
    assert service.get(aged.candidate_id).content == ""
    assert service.get(other.candidate_id).source_message_refs == other.source_message_refs


# LLM: 每次回热/归档会留下旧不可变版本；正式硬删除必须扫描所有月份和旧版本而非只追当前ref。
# 函数用途: 验证多月多版本归档删除不残留plaintext。
def test_redaction_covers_all_months_and_versions(tmp_path):
    service, aged, _ = terminal_case(tmp_path)
    service.compact_terminal_candidates()
    updated = service.observe(_user_observation(message_id="another-observation"))
    with service._locked_candidates():
        rows = service._load_unlocked()
        rows = [replace(row, updated_at="2020-03-04T00:00:00+00:00") if row.candidate_id == aged.candidate_id else row for row in rows]
        module._write_candidates_unlocked(service.path, rows, admission=module._NullAdmission())
    service.compact_terminal_candidates()
    assert len(list((service.path.parent / "candidates_archive").glob("*.jsonl"))) == 2
    service.redact_content(content_hashes={aged.content_hash})
    assert service.get(aged.candidate_id).occurrence_count == updated.occurrence_count
    assert all("macOS" not in p.read_text() for p in (service.path.parent / "candidates_archive").glob("*.jsonl"))


# LLM: 候选回热后ref为空不代表没有历史冷版本，retention必须在丢行前捕获显式清理ID。
# 函数用途: 复现独立审阅指出的“旧归档失去热引用后仍残留正文”。
def test_retention_cleans_old_cold_versions_after_candidate_returned_hot(tmp_path):
    service, aged, _ = terminal_case(tmp_path)
    service.compact_terminal_candidates()
    current = service.observe(_user_observation(message_id="additional"))
    assert not current.cold_detail_ref and not module.binding_history(current)
    assert service.delete_terminal([current.candidate_id]) == 1
    assert all("macOS" not in p.read_text() for p in (service.path.parent / "candidates_archive").glob("*.jsonl"))


# LLM: schema允许已空正文但仍有审核备注的终态；删除责任不能因正文/refs都空而忽略原备注。
# 函数用途: 验证独立审阅提出的note-only合法记录也被脱敏。
def test_redaction_clears_note_only_candidate_accepted_by_schema(tmp_path):
    row = candidate()
    row = replace(row, content="", content_redacted_at=module.utc_now_iso(), status="rejected", evidence_refs=[],
        source_message_refs=[], source_tool_refs=[], source_artifact_refs=[], source_task_ids=[], source_run_ids=[],
        learning_source={}, learning_source_coverage={}, review_note="secret macOS note",
        status_history=[{"status": "rejected", "note": "secret macOS note", "actor": "test", "at": row.updated_at}])
    row = MemoryCandidate.from_record(row.to_record())
    service = module.CandidateService(tmp_path / "candidates.jsonl")
    module._write_candidates_unlocked(service.path, [row], admission=module._NullAdmission())
    assert service.redact_content(content_hashes={row.content_hash}) == 1
    assert "macOS" not in service.path.read_text()
    assert service.redact_content(content_hashes={row.content_hash}) == 0


# LLM: 模拟合法旧冷版本被不完整恢复重新留下的输入，不宣称已复现真实宿主崩溃。
# 函数用途: 验证已promoted的remove幂等重试仍显式清理冷孤儿。
def test_promoted_remove_retry_still_cleans_orphan_cold_versions(tmp_path):
    service, aged, _ = terminal_case(tmp_path)
    service.compact_terminal_candidates()
    archive = next((service.path.parent / "candidates_archive").glob("*.jsonl"))
    old_cold = archive.read_text()
    row = service.observe(replace(_user_observation(message_id="remove"), proposed_action="remove",
        target_entry_id="entry-1", target_entry_version=1))
    service.transition(row.candidate_id, "approved", reviewer="test")
    service.mark_promoted(row.candidate_id, reviewer="test", promotion_ref="entry-1")
    module._write_cold_changes(service.path, {archive.name: old_cold}, coordinator_io=False)
    service.mark_promoted(row.candidate_id, reviewer="test", promotion_ref="entry-1")
    assert "macOS" not in archive.read_text()


# LLM: 已批准但未补默认落点的合法旧候选，也应由同一个快照CAS入口一次写盘完成准备。
# 函数用途: 验证同状态只放行受限默认目标修复，且不是第二次全表写入。
def test_automatic_preparation_repairs_only_missing_target_of_approved_candidate(tmp_path):
    service = module.CandidateService(tmp_path / "candidates.jsonl")
    row = service.observe(replace(_user_observation(), promotion_target="none"))
    approved = service.transition(row.candidate_id, "approved", reviewer="test")
    with patch.object(module, "_write_candidates_unlocked", wraps=module._write_candidates_unlocked) as writer:
        updated = service.prepare_automatic_promotion(approved, reviewer="promotion-service")
    assert writer.call_count == 1 and updated.promotion_target == "long_term" and updated.status == "approved"


# LLM: 数量溢出不能无视一天冷却，新终态即使超限仍保完整；未达年龄阈值的冷却行才按溢出压实。
# 函数用途: 验证数量阈值及新终态保护，而非只覆盖30天年龄分支。
def test_terminal_count_threshold_respects_fresh_cooldown(tmp_path):
    service, aged, fresh = terminal_case(tmp_path)
    now = datetime.now(timezone.utc)
    with service._locked_candidates():
        rows = service._load_unlocked()
        rows = [replace(row, updated_at=(now - timedelta(days=2)).isoformat()) if row.candidate_id == aged.candidate_id else row for row in rows]
        module._write_candidates_unlocked(service.path, rows, admission=module._NullAdmission())
    assert service.compact_terminal_candidates(now=now) == 0
    assert service.compact_terminal_candidates(now=now, max_hot_terminal=1) == 1
    hot = {row.candidate_id: row for row in service.readonly_rows()}
    assert hot[aged.candidate_id].cold_detail_ref and not hot[fresh.candidate_id].cold_detail_ref


# LLM: get返回完整冷详情，retention扫描也可传热stub；两种真实快照都必须通过同一精确CAS，伪造详情不能获准。
# 函数用途: 验证冷热分离不破坏公开get快照的保留清理CAS。
def test_retention_cas_accepts_exact_hydrated_snapshot_and_rejects_changed_detail(tmp_path):
    service, aged, _ = terminal_case(tmp_path)
    service.compact_terminal_candidates()
    snapshot = service.get(aged.candidate_id)
    changed = deepcopy(snapshot)
    changed.status_history[0]["note"] = "forged detail"
    assert service.delete_terminal([aged.candidate_id], expected_snapshots={aged.candidate_id: changed}) == 0
    assert service.delete_terminal([aged.candidate_id], expected_snapshots={aged.candidate_id: snapshot}) == 1
