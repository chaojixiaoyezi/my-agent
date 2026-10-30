# LLM: WT-M2测试只在隔离仓库操作真实消息、候选与正式记忆；故障注入不授予新写权限。
# 模块用途: 验证晋升证据缓存、批次治理和精确提交恢复。
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agent_py_agent.tests.test_memory_promotion_v2 import _explicit, _runtime


# LLM: 故障后先越过真实退避截止时间，再从原恢复和公开晋升入口核对坏权威；禁止篡改候选以绕过退避。
# 函数用途: 验证正式文件或墓碑损坏时报告精确错误码且候选与全部权威字节不变。
@pytest.mark.parametrize("source", ["formal", "tombstones"])
@pytest.mark.parametrize("action", ["replace", "merge"])
@pytest.mark.parametrize("bad", [b"{broken\n", b"[]\n", b"\xff\n"])
def test_r2_interrupted_update_rejects_corrupt_authority(tmp_path, monkeypatch, source, action, bad):
    from agent_py_agent.agent.memory_store.jsonl import MemoryAuthorityCorruptError, _tombstone_path
    from agent_py_agent.agent.memory_store.promotion_remove_recovery import recover_committed_update

    service, candidate, _target = _interrupted_update(tmp_path, monkeypatch, action)
    candidate = service.candidates.get(candidate.candidate_id)
    path = service.long_term.path if source == "formal" else _tombstone_path(service.long_term.path)
    with path.open("ab") as stream:
        stream.write(bad)
    before = path.read_bytes(), service.candidates.path.read_bytes()
    with pytest.raises(MemoryAuthorityCorruptError, match="MEMORY_AUTHORITY_UNREADABLE") as caught:
        recover_committed_update(service, candidate, "test-recovery")
    assert caught.value.code == "MEMORY_AUTHORITY_UNREADABLE"
    with pytest.raises(MemoryAuthorityCorruptError, match="MEMORY_AUTHORITY_UNREADABLE") as caught:
        service.promote(candidate.candidate_id, automatic=True)
    assert caught.value.code == "MEMORY_AUTHORITY_UNREADABLE"
    saved = service.candidates.get(candidate.candidate_id)
    assert saved.status == "approved" and saved.commit_obligation
    assert (path.read_bytes(), service.candidates.path.read_bytes()) == before


# LLM: 隔离真实仓库上的定向回归，不改变权限、原断言或生产输入。
# 函数用途: 精确引用命中缓存且文件变更会失效。
def test_m2_message_verifier_caches_exact_quote_without_retaining_body(tmp_path, monkeypatch):
    from agent_py_agent.tests.support.curator_scan_cases import observe_reads, settle_files

    service, store, thread = _runtime(tmp_path)
    candidate = _explicit(service, store, thread, content="可精确核对的原话")
    verifier = service.message_verifier
    path = store.storage.message_path(thread.thread_id)
    settle_files([path])
    counts = observe_reads(monkeypatch, [path])
    ref = candidate.source_message_refs[0]
    assert verifier.verify(ref) is not None
    first_opens = len(counts.opens)
    assert first_opens >= 1
    assert verifier.verify(ref) is not None
    assert len(counts.opens) == first_opens
    assert verifier.verify({**ref, "quote": "原文中没有的证据"}) is None
    with path.open("ab") as handle:
        handle.write(b"bad\n")
    # by_id_report保持原精确读取合同；变更必须失效缓存并回到实际Store。
    before = len(counts.opens)
    verifier.verify(ref)
    assert len(counts.opens) > before


# LLM: 隔离真实仓库上的定向回归，不改变权限、原断言或生产输入。
# 函数用途: 真实lambda批次只在全部提交后治理一次。
def test_m2_curator_lambda_batch_condenses_once_after_all_candidates(tmp_path, monkeypatch):
    from agent_py_agent.tests.test_memory_curator_v2 import _service

    service, store, thread = _runtime(tmp_path)
    rows = [_explicit(service, store, thread, content=f"批次独立事实 {i}", subject_key=f"batch.fact.{i}") for i in range(3)]
    curator = _service(tmp_path / "curator-data", SimpleNamespace(), store)
    curator.promotion_callback = lambda cid: service.promote(cid, automatic=True)
    calls = []

    # LLM: 仅观察或注入测试边界，未指定替换的行为仍委托真实实现。
    # 函数用途: 观察治理顺序并按场景注入普通失败。
    def condense():
        calls.append([row.status for row in service.candidates.list()])

    monkeypatch.setattr(service.long_term, "condense", condense)
    promoted, warnings, receipts = curator._promote_eligible([row.candidate_id for row in rows])
    assert promoted == 3 and not warnings and len(receipts) == 3
    assert calls == [["promoted"] * 3]
    marker = service.candidates.path.parent / "curator" / "pending_condense.json"
    assert json.loads(marker.read_text())["pending"] is False


# LLM: 隔离真实仓库上的定向回归，不改变权限、原断言或生产输入。
# 函数用途: 默认目标与批准一次落盘且先于正式写。
def test_m2_default_target_and_approval_share_one_precommit_write(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import candidates

    service, store, thread = _runtime(tmp_path)
    candidate = _explicit(service, store, thread, content="默认长期目标需要合并批准", promotion_target="")
    original = candidates._write_candidates_unlocked
    writes = []

    # LLM: 仅观察或注入测试边界，未指定替换的行为仍委托真实实现。
    # 函数用途: 观察候选真实写入的状态顺序。
    def write(path, rows, **kwargs):
        writes.append([(row.status, row.promotion_target) for row in rows])
        return original(path, rows, **kwargs)

    monkeypatch.setattr(candidates, "_write_candidates_unlocked", write)
    original_target = service._commit_target

    # LLM: 仅观察或注入测试边界，未指定替换的行为仍委托真实实现。
    # 函数用途: 检查耐久批准后继续原正式写入。
    def commit(row, context):
        assert writes == [[("approved", "long_term")]]
        durable = [json.loads(line) for line in service.candidates.path.read_text().splitlines()]
        assert durable[0]["status"] == "approved" and durable[0]["promotion_target"] == "long_term"
        return original_target(row, context)

    monkeypatch.setattr(service, "_commit_target", commit)
    assert service.promote(candidate.candidate_id, automatic=True).promoted
    assert len(writes) == 2
    assert writes[-1] == [("promoted", "long_term")]


# LLM: 隔离真实仓库上的定向回归，不改变权限、原断言或生产输入。
# 函数用途: 治理失败在下一空批补偿。
def test_m2_failed_condense_remains_durable_and_resumes_on_empty_ready_batch(tmp_path, monkeypatch):
    service, store, thread = _runtime(tmp_path)
    candidate = _explicit(service, store, thread, content="正式提交后的治理欠账")
    calls = []

    # LLM: 仅观察或注入测试边界，未指定替换的行为仍委托真实实现。
    # 函数用途: 观察治理顺序并按场景注入普通失败。
    def condense():
        calls.append(True)
        if len(calls) == 1:
            raise OSError("fixture condense failed")

    monkeypatch.setattr(service.long_term, "condense", condense)
    assert service.promote(candidate.candidate_id, automatic=True).promoted
    marker = service.candidates.path.parent / "curator" / "pending_condense.json"
    assert marker.exists()
    assert json.loads(marker.read_text())["pending"] is True
    assert service.promote_ready() == ()
    assert len(calls) == 2
    assert json.loads(marker.read_text())["pending"] is False


# LLM: 隔离真实仓库上的定向回归，不改变权限、原断言或生产输入。
# 函数用途: 未知批次跳过治理并保留耐久责任。
@pytest.mark.parametrize("kind", ["missing", "transport", "exit"])
def test_m2_uncertain_batch_skips_condense_and_keeps_durable_obligation(tmp_path, monkeypatch, kind):
    from agent_py_agent.tests.test_curator_uncertain_outcome import _unknown
    from agent_py_agent.tests.test_memory_curator_v2 import _service

    service, store, thread = _runtime(tmp_path)
    candidate = _explicit(service, store, thread, content="未知批次前已提交的事实")
    curator = _service(tmp_path / "curator-data", SimpleNamespace(), store)
    error = _unknown(kind)
    calls = []

    # LLM: 仅观察或注入测试边界，未指定替换的行为仍委托真实实现。
    # 函数用途: 调用原晋升或注入指定未知。
    def promote(cid):
        if cid == "uncertain":
            raise error
        return service.promote(cid, automatic=True)

    curator.promotion_callback = promote
    monkeypatch.setattr(service.long_term, "condense", lambda: calls.append(True))
    with pytest.raises(type(error)) as caught:
        curator._promote_eligible([candidate.candidate_id, "uncertain"])
    assert caught.value is error
    assert service.candidates.get(candidate.candidate_id).status == "promoted"
    assert calls == []
    marker = service.candidates.path.parent / "curator" / "pending_condense.json"
    assert json.loads(marker.read_text())["pending"] is True


# LLM: 正式写真实执行，仅在终态回执处注入IO故障；要求原服务保存履约义务并返回阻塞码，再只推进测试时钟。
# 函数用途: 得到正式写成功、候选仍批准且已越过五分钟退避的恢复案例。
def _interrupted_update(tmp_path, monkeypatch, action):
    service, store, thread = _runtime(tmp_path)
    old = _explicit(service, store, thread, content="更新之前的明确事实")
    assert service.promote(old.candidate_id, automatic=True).promoted
    target = service.long_term.all()[0]
    candidate = _explicit(service, store, thread, content="更新之后的明确事实", proposed_action=action, target_entry_id=target.entry_id)
    original = service.candidates.mark_promoted_admitted

    # LLM: 仅观察或注入测试边界，未指定替换的行为仍委托真实实现。
    # 函数用途: 模拟候选终态回执写入失败。
    def fail(*_args, **_kwargs):
        raise OSError("fixture candidate receipt write failed")

    with monkeypatch.context() as fault:
        fault.setattr(service.candidates, "mark_promoted_admitted", fail)
        result = service.promote(candidate.candidate_id, automatic=True)
        assert result.promoted is False
        assert result.reason_code == "PROMOTION_IO_BLOCKED" and result.status == "approved"
    assert service.candidates.mark_promoted_admitted == original
    saved = service.candidates.get(candidate.candidate_id)
    assert saved.status == "approved" and saved.commit_obligation
    assert saved.obligation_blocked_reason == "PROMOTION_IO_BLOCKED"
    retry_after = datetime.fromisoformat(saved.obligation_retry_after)
    assert retry_after > datetime.now(timezone.utc)
    assert service.long_term.all()[0].version == target.version + 1
    _advance_obligation_clock(monkeypatch, retry_after)
    return service, candidate, target


# LLM: 仅替换晋升模块时钟，候选与正式账本原字节保留；恢复/拒绝路径必须实际超过持久退避截止时间。
# 函数用途: 把测试时钟推进到履约重试截止后一秒，不等待墙钟或篡改责任记录。
def _advance_obligation_clock(monkeypatch, retry_after):
    from agent_py_agent.agent.memory_store import promotion

    due = retry_after + timedelta(seconds=1)

    # LLM: 继承标准日期解析且仅冻结当前时钟，生产状态机和证据校验仍真实执行。
    # 类用途: 提供超过原五分钟退避窗口的确定性测试时钟。
    class AfterRetryClock(datetime):
        # LLM: 只在本测试的晋升模块生效，支持原调用的时区参数且不改系统时钟。
        # 函数用途: 返回重试截止后一秒的当前时间。
        @classmethod
        def now(cls, tz=None):
            return due.astimezone(tz) if tz is not None else due.replace(tzinfo=None)

    monkeypatch.setattr(promotion, "datetime", AfterRetryClock)
    assert promotion.datetime.now(timezone.utc) > retry_after


# LLM: 首次阻塞和退避由真实夹具验证；到期恢复必须补终态并清义务，不重写正式条目。
# 函数用途: 验证精确提交恢复码、正式版本与文件字节均符合合同。
@pytest.mark.parametrize("action", ["replace", "merge"])
def test_m2_exact_committed_update_retry_completes_without_formal_rewrite(tmp_path, monkeypatch, action):
    service, candidate, target = _interrupted_update(tmp_path, monkeypatch, action)
    before = service.long_term.path.read_bytes()
    result = service.promote(candidate.candidate_id, automatic=True)
    assert result.promoted is True
    assert result.reason_code == "UPDATE_COMMIT_RECOVERED"
    assert result.formal_memory_changed is False
    assert service.long_term.path.read_bytes() == before
    saved = service.candidates.get(candidate.candidate_id)
    assert saved.status == "promoted" and not saved.commit_obligation
    assert not saved.obligation_retry_after and not saved.obligation_blocked_reason
    assert service.long_term.all()[0].version == target.version + 1


# LLM: 先真实提交中断并推进五分钟退避，再篡改唯一正式库；必须走身份拒绝而非退避空过。
# 函数用途: 验证不同归属或元数据不能冒认旧更新，拒绝码和冲突终态均严格核对。
@pytest.mark.parametrize("field,value", [("source", "candidate:someone-else"), ("version", 3), ("kind", "event"),
                                         ("tags", ["other"]), ("attributes", {}), ("expires_at", 99.0),
                                         ("content", "不一样的事实")])
def test_m2_update_retry_never_adopts_other_identity_or_metadata(tmp_path, monkeypatch, field, value):
    service, candidate, _target = _interrupted_update(tmp_path, monkeypatch, "replace")
    rows = [json.loads(line) for line in service.long_term.path.read_text().splitlines()]
    rows[-1][field] = value
    service.long_term.path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    before = service.long_term.path.read_bytes()
    result = service.promote(candidate.candidate_id, automatic=True)
    assert result.promoted is False
    assert result.reason_code in {"TARGET_ENTRY_STALE_OBSERVATION", "TARGET_ENTRY_NOT_IN_SAME_SUBJECT_SCOPE"}
    assert result.status == "blocked_conflict"
    assert service.candidates.get(candidate.candidate_id).status == "blocked_conflict"
    assert service.long_term.path.read_bytes() == before


# LLM: 隔离真实仓库上的定向回归，不改变权限、原断言或生产输入。
# 函数用途: 并发空批不能丢掉迟到正式提交的治理责任。
@pytest.mark.parametrize("uncertain", [False, True])
def test_m2_concurrent_empty_batch_cannot_lose_later_formal_commit_debt(tmp_path, monkeypatch, uncertain):
    from contextvars import Context

    from agent_py_agent.agent.memory_store.promotion import _register_condense_debt, promotion_batch
    from agent_py_agent.tests.test_curator_uncertain_outcome import _unknown

    service, store, thread = _runtime(tmp_path)
    candidate = _explicit(service, store, thread, content="空批早结束以后才提交的正式事实")
    calls = []

    # LLM: 仅观察或注入测试边界，未指定替换的行为仍委托真实实现。
    # 函数用途: 观察治理顺序并按场景注入普通失败。
    def condense():
        calls.append(True)
        if len(calls) == 2 and not uncertain:
            raise OSError("fixture late batch condense failed")

    # LLM: 仅观察或注入测试边界，未指定替换的行为仍委托真实实现。
    # 函数用途: 在独立上下文运行空批治理。
    def empty_batch():
        with promotion_batch(long_term=service.long_term, candidates=service.candidates):
            pass

    monkeypatch.setattr(service.long_term, "condense", condense)
    error = _unknown("exit")
    try:
        with promotion_batch(long_term=service.long_term, candidates=service.candidates):
            _register_condense_debt(service.long_term, service.candidates, False)
            Context().run(empty_batch)
            assert service.promote(candidate.candidate_id, automatic=True).promoted
            if uncertain:
                raise error
    except type(error) as caught:
        assert caught is error and uncertain
    before = len(calls)
    assert service.promote_ready() == ()
    assert len(calls) == before + 1
    service.promote_ready()
    assert len(calls) == before + 1


# LLM: 隔离真实仓库上的定向回归，不改变权限、原断言或生产输入。
# 函数用途: 捕获内层未知也不能恢复整批治理资格。
@pytest.mark.parametrize("kind", ["missing", "transport", "exit"])
def test_m2_nested_caught_unknown_still_poison_entire_condense_batch(tmp_path, monkeypatch, kind):
    from agent_py_agent.agent.memory_store.promotion import promotion_batch
    from agent_py_agent.tests.test_curator_uncertain_outcome import _unknown

    service, store, thread = _runtime(tmp_path)
    candidate = _explicit(service, store, thread, content="嵌套未知被调用方捕获也不能治理")
    error, calls = _unknown(kind), []
    monkeypatch.setattr(service.long_term, "condense", lambda: calls.append(True))
    with promotion_batch(long_term=service.long_term, candidates=service.candidates):
        with pytest.raises(type(error)) as caught:
            with promotion_batch(long_term=service.long_term, candidates=service.candidates):
                assert service.promote(candidate.candidate_id, automatic=True).promoted
                raise error
        assert caught.value is error
    assert calls == []
    marker = service.candidates.path.parent / "curator" / "pending_condense.json"
    assert json.loads(marker.read_text())["pending"] is True


# LLM: 隔离真实仓库上的定向回归，不改变权限、原断言或生产输入。
# 函数用途: 同一候选根不能静默混用两份正式库。
def test_m2_same_candidate_root_cannot_silently_mix_two_formal_files(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store.promotion import _register_condense_debt, promotion_batch

    service, _store, _thread = _runtime(tmp_path)
    monkeypatch.setattr(service.long_term, "condense", lambda: None)
    other = SimpleNamespace(path=service.long_term.path.with_name("other.jsonl"))
    with promotion_batch(long_term=service.long_term, candidates=service.candidates):
        _register_condense_debt(service.long_term, service.candidates, False)
        with pytest.raises(RuntimeError, match="FORMAL_MISMATCH"):
            _register_condense_debt(other, service.candidates, False)
