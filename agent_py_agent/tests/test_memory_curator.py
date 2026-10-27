# LLM: WT-M2回归使用真实隔离仓库和既有fake模型，只在精确故障边界注入异常；不替代线上验收。
# 模块用途: 覆盖策展性能优化、游标恢复和未结执行责任。
from __future__ import annotations

from dataclasses import replace

import pytest

from agent_py_agent.agent.memory_store.curator import CuratorInputError
from agent_py_agent.tests.test_curator_uncertain_outcome import _unknown
from agent_py_agent.tests.test_memory_curator_v2 import (
    _conversation,
    _service,
    _StaticStructuredBackend,
    _valid_output,
)


# LLM: 与生产共用ConversationStore、candidate/daily/state和租约，仅模型使用既有确定性输出。
# 函数用途: 创建可以实际推进游标的隔离策展服务。
def _m2_service(tmp_path):
    store, thread, message = _conversation(tmp_path)
    backend = _StaticStructuredBackend(_valid_output(thread.thread_id, message.message_id, message.content))
    return _service(tmp_path, backend, store), thread, message


# LLM: 隔离真实仓库上的定向回归，不改变权限、原断言或生产输入。
# 函数用途: 未知提交必须保原异常、pending与租约。
@pytest.mark.parametrize("kind", ["missing", "transport", "exit"])
def test_m2_commit_unknown_preserves_original_error_and_lease(tmp_path, monkeypatch, kind):
    service, _thread, _message = _m2_service(tmp_path)
    service.request("admin")
    before = service.state_store.load()
    error = _unknown(kind)

    # LLM: 仅观察或注入测试边界，未指定替换的行为仍委托真实实现。
    # 函数用途: 抛出预设未结异常供上层核对对象身份。
    def reject(_commit):
        raise error

    monkeypatch.setattr(service.committer, "commit", reject)
    with pytest.raises(type(error)) as caught:
        service.run(reason="admin")
    assert caught.value is error
    state = service.state_store.load()
    assert state.active_lease.get("lease_id")
    assert state.per_thread_cursors == before.per_thread_cursors
    assert state.pending_reasons == before.pending_reasons
    assert state.last_failure_code == before.last_failure_code
    assert service.run_log.list() == []


# LLM: 隔离真实仓库上的定向回归，不改变权限、原断言或生产输入。
# 函数用途: 自动晋升不能吞掉执行未知。
@pytest.mark.parametrize("kind", ["missing", "transport", "exit"])
def test_m2_promotion_unknown_is_not_success_warning(tmp_path, kind):
    service, _thread, _message = _m2_service(tmp_path)
    error = _unknown(kind)

    # LLM: 仅观察或注入测试边界，未指定替换的行为仍委托真实实现。
    # 函数用途: 抛出预设未结异常供上层核对对象身份。
    def reject(_candidate_id):
        raise error

    service.promotion_callback = reject
    with pytest.raises(type(error)) as caught:
        service._promote_eligible(["candidate-example"])
    assert caught.value is error


# LLM: 隔离真实仓库上的定向回归，不改变权限、原断言或生产输入。
# 函数用途: 候选读取损坏不能当作空列表。
def test_m2_pending_candidate_corruption_is_diagnostic_not_empty(tmp_path, monkeypatch):
    service, _thread, _message = _m2_service(tmp_path)

    # LLM: 仅观察或注入测试边界，未指定替换的行为仍委托真实实现。
    # 函数用途: 模拟候选读取损坏。
    def corrupt(**_kwargs):
        raise OSError("fixture candidate storage unavailable")

    monkeypatch.setattr(service.candidate_service, "list", corrupt)
    with pytest.raises(CuratorInputError, match="CURATOR_CANDIDATE_READ_FAILED"):
        service._pending_promotable_candidate_ids()


# LLM: 隔离真实仓库上的定向回归，不改变权限、原断言或生产输入。
# 函数用途: 待晋升扫描使用只读借用行。
def test_m2_pending_scan_borrows_readonly_candidates(tmp_path, monkeypatch):
    service, _thread, _message = _m2_service(tmp_path)
    calls = []
    monkeypatch.setattr(service.candidate_service, "readonly_rows", lambda **_kwargs: calls.append("readonly") or ())
    assert service._pending_promotable_candidate_ids() == []
    assert calls == ["readonly"]


# LLM: 隔离真实仓库上的定向回归，不改变权限、原断言或生产输入。
# 函数用途: 到期批次只采集一次且正式上下文只读一次。
def test_m2_due_run_collects_inputs_once_and_formal_once(tmp_path, monkeypatch):
    from agent_py_agent.tests.test_memory_curator_v2 import _FormalMemorySourceProbe

    service, thread, message = _m2_service(tmp_path)
    formal = _FormalMemorySourceProbe()
    service.formal_memory_source = formal
    original = service._collect
    scans = []

    # LLM: 仅观察或注入测试边界，未指定替换的行为仍委托真实实现。
    # 函数用途: 委托真实采集并记录次数。
    def collect(*args, **kwargs):
        scans.append(kwargs["include_formal"])
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "_collect", collect)
    assert service.run_if_due().status == "succeeded"
    assert scans == [False]
    assert formal.calls == 1
    assert service.state_store.load().per_thread_cursors[thread.thread_id] == message.message_id


# LLM: 隔离真实仓库上的定向回归，不改变权限、原断言或生产输入。
# 函数用途: 领取后名单变化必须丢弃旧批。
def test_m2_precollected_batch_is_discarded_if_exclusions_change_under_lease(tmp_path, monkeypatch):
    service, thread, _message = _m2_service(tmp_path)
    reads = []

    # LLM: 仅观察或注入测试边界，未指定替换的行为仍委托真实实现。
    # 函数用途: 制造两次读取之间的名单变化。
    def exclusions():
        reads.append(True)
        return (frozenset() if len(reads) == 1 else frozenset({"chat-1"})), False

    service.learning_exclusions = exclusions
    result = service.run_if_due()
    assert result.status == "succeeded"
    assert service.backend.calls == 0
    assert thread.thread_id not in service.state_store.load().per_thread_cursors


# LLM: 隔离真实仓库上的定向回归，不改变权限、原断言或生产输入。
# 函数用途: 冷线程免读仍能发现追加消息。
def test_m2_cold_threads_skip_incremental_reader_but_invalidate_on_append(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store.curator_inputs import (
        CuratorMessageSource,
        _collect_messages,
    )
    from agent_py_agent.tests.support.curator_scan_cases import settle_files

    service, thread, message = _m2_service(tmp_path)
    store = service.conversation_store
    path = store.storage.message_path(thread.thread_id)
    settle_files([path])
    original = store.messages.after_report
    calls = []

    # LLM: 仅观察或注入测试边界，未指定替换的行为仍委托真实实现。
    # 函数用途: 记录真实增量读取次数。
    def after(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(store.messages, "after_report", after)
    source = CuratorMessageSource(store)
    cursors = {thread.thread_id: message.message_id}
    args = dict(source=source, cursors=cursors, limit=10, max_chars=20_000)
    assert _collect_messages(**args) == ([], [])
    calls.clear()
    assert _collect_messages(**args) == ([], [])
    assert calls == []
    new = store.messages.append(dict(thread_id=thread.thread_id, role="user", content="后续新消息"))
    selected, errors = _collect_messages(**args)
    assert not errors and [row.message_id for row in selected] == [new.message_id]
    assert calls == [thread.thread_id]


# LLM: 隔离真实仓库上的定向回归，不改变权限、原断言或生产输入。
# 函数用途: 同一快照的多个请求只扫描一次。
@pytest.mark.parametrize("stable", [True, False])
@pytest.mark.parametrize("alias", ["gateway_request_id", "request_id", "conversation_request_id"])
def test_m2_request_sources_scan_one_snapshot_for_all_request_ids(tmp_path, monkeypatch, alias, stable):
    from agent_py_agent.agent.memory_store.learning_source_authority import (
        ConversationLearningSources,
    )
    from agent_py_agent.tests.support.curator_scan_cases import observe_reads, settle_files
    from agent_py_agent.tests.test_memory_promotion_v2 import _runtime

    _promo, store, thread = _runtime(tmp_path)
    for number in range(4):
        store.messages.append(dict(thread_id=thread.thread_id, role="user", content="不保留的长正文" * 1000,
                                   metadata={alias: f"request-{number}"}))
    path = store.storage.message_path(thread.thread_id)
    if stable:
        settle_files([path, store.storage.thread_path(thread.thread_id)])
    else:
        from agent_py_agent.tests.test_curator_cursor_cache import hold_fresh_clock

        hold_fresh_clock(monkeypatch, path)
    counts = observe_reads(monkeypatch, [path])
    sources = ConversationLearningSources(store, "local/test")
    assert all(sources.request(thread.thread_id, f"request-{number}")["can_learn"] for number in range(4))
    assert len(counts.opens) == 1
    assert "不保留的长正文" * 100 not in repr(vars(sources))


# LLM: 隔离真实仓库上的定向回归，不改变权限、原断言或生产输入。
# 函数用途: 文件变更重新核对来源与重复请求。
def test_m2_request_source_stamp_invalidation_rechecks_duplicates_and_thread(tmp_path):
    from agent_py_agent.agent.memory_store.learning_source_authority import (
        ConversationLearningSources,
    )
    from agent_py_agent.tests.test_memory_promotion_v2 import _runtime

    _promo, store, thread = _runtime(tmp_path)
    message = dict(thread_id=thread.thread_id, role="user", content="来源事实", metadata={"request_id": "one"})
    store.messages.append(message)
    sources = ConversationLearningSources(store, "local/test")
    assert sources.request(thread.thread_id, "one")["can_learn"]
    store.messages.append(message)
    assert not sources.request(thread.thread_id, "one")["can_learn"]


# LLM: 隔离真实仓库上的定向回归，不改变权限、原断言或生产输入。
# 函数用途: 命中之后的坏行仍使来源关闭。
@pytest.mark.parametrize("bad", [b"{}\n", b"broken\n", b"[]\n"])
def test_m2_request_index_checks_corruption_after_early_match(tmp_path, bad):
    from agent_py_agent.agent.memory_store.learning_source_authority import (
        ConversationLearningSources,
    )
    from agent_py_agent.tests.test_memory_promotion_v2 import _runtime

    _promo, store, thread = _runtime(tmp_path)
    store.messages.append(dict(thread_id=thread.thread_id, role="user", content="来源事实", metadata={"request_id": "one"}))
    with store.storage.message_path(thread.thread_id).open("ab") as handle:
        handle.write(bad)
    assert not ConversationLearningSources(store, "local/test").request(thread.thread_id, "one")["can_learn"]


# LLM: 隔离真实仓库上的定向回归，不改变权限、原断言或生产输入。
# 函数用途: 滚动指纹覆盖超过512个历史分片。
def test_m2_audit_prefix_over_512_shards_stays_cacheable_and_checks_history(tmp_path, monkeypatch):
    import json

    from agent_py_agent.agent.io.cursor_cache import CursorCache, cursor_key
    from agent_py_agent.agent.memory_store import curator_audit_cursor as audit
    from agent_py_agent.agent.memory_store.curator_inputs import _collect_audit
    from agent_py_agent.tests.support.curator_scan_cases import (
        encode_row,
        observe_reads,
        settle_files,
    )

    root = tmp_path / "audit"
    root.mkdir()
    paths = [root / f"{number:04}.jsonl" for number in range(515)]
    for number, path in enumerate(paths):
        path.write_bytes(encode_row(dict(event_id=f"event-{number}")))
    settle_files(paths)
    cache = CursorCache(4)
    monkeypatch.setattr(audit, "_AUDIT_CURSORS", cache)
    assert _collect_audit(root, after_event_id="event-514", limit=1, max_chars=1000) == ([], [])
    cached = cache.get(cursor_key(root, "event-514"))
    assert cached is not None
    assert len(cached.prefix) <= 512
    assert cached.prefix_digest
    counts = observe_reads(monkeypatch, paths)
    assert _collect_audit(root, after_event_id="event-514", limit=1, max_chars=1000) == ([], [])
    assert counts.opens == []
    paths[0].write_text(json.dumps({}) + "\n")
    rows, errors = _collect_audit(root, after_event_id="event-514", limit=1, max_chars=1000)
    assert rows == [] and errors and counts.opens


# LLM: 隔离真实仓库上的定向回归，不改变权限、原断言或生产输入。
# 函数用途: 达到预算后不构造业务行但继续检查损坏。
def test_m2_audit_budget_bounds_row_objects_but_still_validates_tail(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import curator_audit_cursor as audit
    from agent_py_agent.agent.memory_store.curator_inputs import _collect_audit
    from agent_py_agent.tests.support.curator_scan_cases import make_audit

    paths, _rows = make_audit(tmp_path / "audit", days=1, rows_per_day=200)
    with paths[0].open("ab") as handle:
        handle.write(b"{}\nbad\n[]\n")
    original = audit.AuditRow
    constructions = []

    # LLM: 仅观察或注入测试边界，未指定替换的行为仍委托真实实现。
    # 函数用途: 计数后返回原业务行对象。
    def row(*args, **kwargs):
        constructions.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(audit, "AuditRow", row)
    selected, errors = _collect_audit(tmp_path / "audit", after_event_id="", limit=1, max_chars=1000)
    assert len(selected) == 1 and len(errors) == 3
    assert len(constructions) <= 1


# LLM: 仅在测试state放入真实大游标字典，冷线程无需伪造消息；产品实际处理活动线程并写运行审计。
# 函数用途: 建立两次真实提交，验证首检查点与后续增量记录的完整恢复。
def _m2_cursor_runs(tmp_path):
    import json

    service, thread, message = _m2_service(tmp_path)
    state = replace(service.state_store.load(), per_thread_cursors={f"cold-{i}": f"message-{i}" for i in range(45)})
    service.state_store.path.write_text(json.dumps(state.to_dict()))
    assert service.run(reason="admin").status == "succeeded"
    second = service.conversation_store.messages.append(dict(thread_id=thread.thread_id, role="user", content="第二次新增经历"))
    service.backend.payload = _valid_output(thread.thread_id, second.message_id, second.content)
    assert service.run(reason="admin").status == "succeeded"
    path = next(service.run_log.runs_dir.glob("*.jsonl"))
    return service, thread, message, second, path


@pytest.mark.parametrize("existing_chain", [False, True])
def test_r2_post_commit_report_failure_keeps_committed_cursor_chain(tmp_path, monkeypatch, existing_chain):
    import json

    if existing_chain:
        service, thread, _, _, _ = _m2_cursor_runs(tmp_path)
        message = service.conversation_store.messages.append(dict(thread_id=thread.thread_id, role="user", content="提交后回执失败的新事实"))
        service.backend.payload = _valid_output(thread.thread_id, message.message_id, message.content)
    else:
        service, thread, message = _m2_service(tmp_path)
        state = replace(service.state_store.load(), per_thread_cursors={f"cold-{i}": f"message-{i}" for i in range(45)})
        service.state_store.path.write_text(json.dumps(state.to_dict()))

    def fail_report(**_kwargs):
        raise CuratorInputError("CURATOR_CANDIDATE_READ_FAILED")

    monkeypatch.setattr(service, "_pending_promotable_candidate_ids", fail_report)
    assert service.run(reason="admin").status == "failed"
    committed = service.state_store.load().per_thread_cursors
    assert committed[thread.thread_id] == message.message_id and len(committed) > 32
    rows = service.run_log.list()
    assert [row.status for row in rows[-2:]] == ["succeeded", "failed"]
    assert rows[-1].cursor_before == rows[-2].cursor_after
    assert rows[-1].cursor_before["per_thread_cursors"] == committed
    recent = service.run_log.recent_finished(2)
    assert {row.record_id for row in recent} == {row.record_id for row in rows[-2:]}
    service.state_store.path.write_text("{broken-state")
    assert service.run(reason="admin").failure_code == "CURATOR_STATE_CORRUPT"
    assert service.recover()["status"] == "recovered"
    assert service.state_store.load().per_thread_cursors == committed


# LLM: 隔离真实仓库上的定向回归，不改变权限、原断言或生产输入。
# 函数用途: 增量运行账仍能恢复全部冷游标。
def test_m2_cursor_delta_has_checkpoint_and_recovers_all_cold_cursors(tmp_path):
    import json

    service, thread, first, second, path = _m2_cursor_runs(tmp_path)
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert records[-1]["cursor_before"]["per_thread_cursors"] == {thread.thread_id: first.message_id}
    assert records[-1]["cursor_after"]["per_thread_cursors"] == {thread.thread_id: second.message_id}
    assert set(records[-1]["cursor_before"]) == {"per_thread_cursors", "last_audit_event_id"}
    full = service.state_store.load().per_thread_cursors
    assert service.run_log.list()[-1].cursor_after["per_thread_cursors"] == full
    service.state_store.path.write_text("{broken-state")
    assert service.run(reason="admin").failure_code == "CURATOR_STATE_CORRUPT"
    assert service.recover()["status"] == "recovered"
    assert service.state_store.load().per_thread_cursors == full


# LLM: 隔离真实仓库上的定向回归，不改变权限、原断言或生产输入。
# 函数用途: 缺链或篡改必须拒绝恢复。
@pytest.mark.parametrize("damage", ["missing_base", "changed_delta", "missing_delta_header"])
def test_m2_cursor_chain_damage_refuses_recovery(tmp_path, damage):
    import json

    service, thread, _first, _second, path = _m2_cursor_runs(tmp_path)
    records = [json.loads(line) for line in path.read_text().splitlines()]
    if damage == "missing_base":
        records = records[1:]
    elif damage == "changed_delta":
        records[-1]["cursor_after"]["per_thread_cursors"][thread.thread_id] = "corrupt-cursor"
    else:
        records[-1]["warnings"] = []
    path.write_text("".join(json.dumps(row) + "\n" for row in records))
    service.state_store.path.write_text("{broken-state")
    assert service.run(reason="admin").failure_code == "CURATOR_STATE_CORRUPT"
    result = service.recover()
    assert result["status"] == "refused"
    assert result["reason"] == "cursor_audit_chain_invalid"
    assert service.state_store.corrupt_evidence() is not None


# LLM: 隔离真实仓库上的定向回归，不改变权限、原断言或生产输入。
# 函数用途: 配置变化后不能使用旧预算租约。
def test_m2_config_changed_while_claiming_does_not_use_old_lease_budget(tmp_path, monkeypatch):
    service, _thread, _message = _m2_service(tmp_path)
    original = service.committer.recover_and_acquire

    # LLM: 仅观察或注入测试边界，未指定替换的行为仍委托真实实现。
    # 函数用途: 在领取边界变更输入后委托真实领取。
    def acquire(request):
        service.config = replace(service.config, enabled=False)
        return original(request)

    monkeypatch.setattr(service.committer, "recover_and_acquire", acquire)
    result = service.run_if_due()
    assert result.status in {"not_due", "disabled"}
    assert service.backend.calls == 0
    state = service.state_store.load()
    assert state.active_lease == {} and state.per_thread_cursors == {}


# LLM: 隔离真实仓库上的定向回归，不改变权限、原断言或生产输入。
# 函数用途: 游标变化后不能重用旧消息批。
def test_m2_state_cursor_changed_under_lease_discards_precollected_messages(tmp_path, monkeypatch):
    import json

    service, thread, message = _m2_service(tmp_path)
    original = service.committer.recover_and_acquire

    # LLM: 仅观察或注入测试边界，未指定替换的行为仍委托真实实现。
    # 函数用途: 在领取边界变更输入后委托真实领取。
    def acquire(request):
        state = service.state_store.load()
        changed = replace(state, per_thread_cursors={thread.thread_id: message.message_id})
        service.state_store.path.write_text(json.dumps(changed.to_dict()))
        return original(request)

    monkeypatch.setattr(service.committer, "recover_and_acquire", acquire)
    assert service.run_if_due().status == "succeeded"
    assert service.backend.calls == 0


# LLM: 隔离真实仓库上的定向回归，不改变权限、原断言或生产输入。
# 函数用途: 大型游标重放幂等且同ID异内容拒绝。
@pytest.mark.parametrize("which", [0, -1])
def test_m2_large_cursor_run_append_remains_idempotent(tmp_path, which):
    service, _thread, _first, _second, path = _m2_cursor_runs(tmp_path)
    record = service.run_log.list()[which]
    before = path.read_bytes()
    service.run_log.append(record)
    assert path.read_bytes() == before
    with pytest.raises(RuntimeError, match="collision"):
        service.run_log.append(replace(record, promoted=record.promoted + 1))


# LLM: 隔离真实仓库上的定向回归，不改变权限、原断言或生产输入。
# 函数用途: 大字典缩回小字典后版本链仍完整。
def test_m2_cursor_chain_survives_shrink_then_small_full_record(tmp_path):
    from datetime import datetime, timedelta

    service, thread, _first, second, _path = _m2_cursor_runs(tmp_path)
    previous = service.run_log.list()[-1]
    finished = datetime.fromisoformat(previous.finished_at) + timedelta(seconds=1)
    small = {"per_thread_cursors": {thread.thread_id: second.message_id}, "last_audit_event_id": ""}
    shrink = replace(previous, run_id="m2-shrink", record_id="", finished_at=finished.isoformat(),
                     cursor_before=previous.cursor_after, cursor_after=small, warnings=())
    service.run_log.append(shrink)
    service.run_log.append(replace(shrink, run_id="m2-small", record_id="",
                                  finished_at=(finished + timedelta(seconds=1)).isoformat(), cursor_before=small))
    assert service.run_log.list()[-1].cursor_after == small


# LLM: 实际注册临时受管用户并装配原治理/记忆/会话仓库；不访问真实用户。
# 函数用途: 为H04建立带真实宿主策略的晋升环境。
def _holistic_memory(tmp_path):
    from agent_py_agent.agent.conversation import learning_source as ls
    from agent_py_agent.agent.user_space.local_user_lifecycle import LocalUserLifecycle
    from agent_py_agent.agent.user_space.local_user_store import LocalUserStore
    from agent_py_agent.agent.user_space.managed_user_governance import (
        ManagedUserGovernanceService,
        _managed_conversations,
        _managed_memory_stores,
        _managed_promotion,
    )

    root = tmp_path / "host"
    LocalUserLifecycle(LocalUserStore(root)).create("alice", idempotency_key="holistic-memory-alice")
    home = LocalUserStore(root).owner_home("alice")
    store = _managed_conversations(home, initialize=True)
    thread = store.threads.get_or_create(dict(canonical_user_id=home.owner_id, channel="internal",
        channel_conversation_id="chat", channel_user_id="alice", owner_id=home.owner_id,
        owner_home=str(home.home_dir), learning_source=ls.user_dialogue_projection()))
    memory, candidates = _managed_memory_stores(home)
    return root, ManagedUserGovernanceService(root), home, _managed_promotion(home, memory, candidates), store, thread


# LLM: 候选来源引用回到真实消息，target/type只取当前参数；不伪造证据核验结果。
# 函数用途: 建立可以通过原来源和证据门的受管候选。
def _holistic_candidate(env, target="long_term"):
    import hashlib

    from agent_py_agent.agent.conversation import learning_source as ls
    from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope

    _root, _admin, home, service, store, thread = env
    message = store.messages.append(dict(thread_id=thread.thread_id, role="user", content="开发电脑使用 macOS。"))
    ref = dict(thread_id=thread.thread_id, message_id=message.message_id, role="user", quote=message.content,
               content_hash="sha256:" + hashlib.sha256(message.content.encode()).hexdigest())
    kinds = {"long_term": "long_term_fact", "user": "user_preference", "lesson": "lesson", "hot": "hot_rule"}
    return service.candidates.observe(CandidateObservation(candidate_type=kinds[target], content=message.content,
        subject_key="holistic.device", scope=MemoryScope("personal", "personal"), origin="user_explicit",
        source_message_refs=(ref,), learning_source=ls.merge_message_sources([ls.facts_for_message(message.metadata,
            owner=home.owner_id, thread_id=thread.thread_id, thread_source=thread.learning_source)]),
        promotion_target=target, proposed_action="add", promotion_mode="auto_eligible", confidence=0.99))


# LLM: USER/lesson/HOT用失败探针观察提交边界；长期库保留真实提交路径。
# 函数用途: 每个治理开关都必须在自动批准或任何目标写入之前阻断。
@pytest.mark.parametrize("approved", [False, True])
@pytest.mark.parametrize("target,patch,code", [
    *[(target, {"memory_curate_mode": "admin_only"}, "AUTO_GOVERNANCE_ADMIN_ONLY")
      for target in ("long_term", "user", "lesson", "hot")],
    *[(target, {"memory_governance": {"allow_self_learning": False}}, "AUTO_GOVERNANCE_SELF_LEARNING_DISABLED")
      for target in ("long_term", "user", "lesson", "hot")],
    *[(target, {"memory_governance": {"require_admin_review_for_long_term": True}}, "AUTO_GOVERNANCE_ADMIN_REVIEW_REQUIRED")
      for target in ("long_term", "user")],
    *[(target, {"memory_governance": {"require_admin_review_for_lesson": True}}, "AUTO_GOVERNANCE_ADMIN_REVIEW_REQUIRED")
      for target in ("lesson", "hot")],
])
def test_h04_automatic_governance_blocks_and_registers_review(tmp_path, monkeypatch, target, patch, code, approved):
    from agent_py_agent.agent.user_space.governance_review_store import GovernanceReviewStore

    env = _holistic_memory(tmp_path)
    root, admin, home, service, _store, _thread = env
    candidate = _holistic_candidate(env, target)
    if approved:
        candidate = service.review(candidate, approved=True, reviewer="owner")
    admin.set_policy("alice", patch)
    if target in {"user", "lesson", "hot"}:
        monkeypatch.setattr(service, "_commit_target", lambda *_args: pytest.fail("governance allowed formal commit"))
    result = service.promote(candidate.candidate_id, automatic=True)
    assert not result.promoted and result.reason_code == code
    assert service.candidates.get(candidate.candidate_id).status == candidate.status
    assert service.long_term.all() == []
    reviews = GovernanceReviewStore(root).list_reviews(home.owner_id, status_filter="pending_admin_review")
    assert reviews["count"] == 1
    assert reviews["items"][0]["review_type"] == "memory_promotion"
    assert reviews["items"][0]["requested_action"]["candidate_id"] == candidate.candidate_id
    assert service.promote(candidate.candidate_id, automatic=True).reason_code == code
    assert GovernanceReviewStore(root).list_reviews(home.owner_id, status_filter="pending_admin_review")["count"] == 1


# LLM: 走真实管理员批复和正式库写入，不用reviewer/source字符串冒充管理员活栈。
# 函数用途: 待审候选仍能由原管理员服务核证据后正式批准。
def test_h04_review_queue_can_apply_real_admin_approval(tmp_path):
    from agent_py_agent.agent.user_space.governance_review_store import GovernanceReviewStore

    env = _holistic_memory(tmp_path)
    root, admin, home, service, _store, _thread = env
    candidate = _holistic_candidate(env)
    admin.set_policy("alice", {"memory_governance": {"require_admin_review_for_long_term": True}})
    assert not service.promote(candidate.candidate_id, automatic=True).promoted
    reviews = GovernanceReviewStore(root)
    item = reviews.list_reviews(home.owner_id, status_filter="pending_admin_review")["items"][0]
    result = reviews.decide_review(item["review_id"], "approve", expected_revision=item["revision"])
    assert result["status"] == "applied"
    assert service.candidates.get(candidate.candidate_id).status == "promoted"
    assert [record.content for record in service.long_term.all()] == [candidate.content]


# LLM: 只开启不适用于长期事实的lesson审核，验证治理不能把所有学习一刀切。
# 函数用途: 无适用阻断策略时仍沿原自动晋升链落库。
def test_h04_nonapplicable_review_flag_preserves_autonomous_promotion(tmp_path):
    env = _holistic_memory(tmp_path)
    _root, admin, _home, service, _store, _thread = env
    candidate = _holistic_candidate(env)
    admin.set_policy("alice", {"memory_governance": {"require_admin_review_for_lesson": True}})
    assert service.promote(candidate.candidate_id, automatic=True).promoted
    assert len(service.long_term.all()) == 1


# LLM: 策略在原自动批准返回后收紧，真实服务必须在最终写口前重新核对，而不是复用早期结果。
# 函数用途: 覆盖自动批准与正式提交之间的治理变更。
@pytest.mark.parametrize("target", ["long_term", "user", "lesson", "hot"])
def test_h04_governance_change_after_auto_approval_blocks_commit(tmp_path, monkeypatch, target):
    env = _holistic_memory(tmp_path)
    _root, admin, _home, service, _store, _thread = env
    candidate = _holistic_candidate(env, target)
    original = service._apply_automatic_policy

    # LLM: 只在原批准完成之后更新真实宿主策略，正式提交仍委托原服务。
    # 函数用途: 构造批准与正式写入之间的策略收紧。
    def tighten(row, reviewer):
        approved = original(row, reviewer)
        admin.set_policy("alice", {"memory_governance": {"allow_self_learning": False}})
        return approved

    monkeypatch.setattr(service, "_apply_automatic_policy", tighten)
    if target != "long_term":
        monkeypatch.setattr(service, "_commit_target", lambda *_args: pytest.fail("stale governance allowed commit"))
    result = service.promote(candidate.candidate_id, automatic=True)
    assert not result.promoted and result.reason_code == "AUTO_GOVERNANCE_SELF_LEARNING_DISABLED"
    assert service.long_term.all() == []


# LLM: 真正的policy flock由原实现取得；仅观察锁调用，复制的Context不能延寿已经释放的锁。
# 函数用途: 治理锁同栈重入可复用，过期copy_context必须重新取得原锁。
def test_h04_copied_context_cannot_reuse_released_governance_lock(tmp_path, monkeypatch):
    from contextvars import copy_context

    from agent_py_agent.agent.memory_store.jsonl import _managed_policy_scope
    from agent_py_agent.agent.user_space.managed_user_governance import _GovernanceControl

    _root, _admin, home, _service, _store, _thread = _holistic_memory(tmp_path)
    original = _GovernanceControl.locked
    calls = []

    # LLM: 不替代文件锁，只记录实际进入原治理锁的次数。
    # 函数用途: 观察重入是否取得新的真实policy锁。
    def observe(control, name="policy"):
        calls.append(name)
        return original(control, name)

    # LLM: 保存的上下文只携带历史活栈，不能成为持锁权威。
    # 函数用途: 从原锁已退出的复制上下文重新进入治理门。
    def reenter():
        with _managed_policy_scope(home.home_dir):
            pass

    monkeypatch.setattr(_GovernanceControl, "locked", observe)
    with _managed_policy_scope(home.home_dir):
        reenter()
        assert calls == ["policy"]
        saved = copy_context()
    saved.run(reenter)
    assert calls == ["policy", "policy"]


# LLM: 原正式JSONL批量口不能信source或已缓存策略；管理员原活栈仍有合法写入路径。
# 函数用途: 下层写口阻断绕过并保持受信管理员写入。
@pytest.mark.parametrize("flag,value", [("allow_self_learning", False), ("require_admin_review_for_long_term", True)])
def test_h04_jsonl_governance_rechecks_and_rejects_forged_admin_source(tmp_path, flag, value):
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    _root, admin, _home, service, _store, _thread = _holistic_memory(tmp_path)
    admin.set_policy("alice", {"memory_governance": {flag: value}})
    with pytest.raises(HostAPIError):
        service.long_term.add("system", "unreviewed fact", source="admin_managed_governance", kind="fact")
    assert service.long_term.all() == []
    result = admin.mutate_memories("alice", "add_memory", content="admin approved fact", locked=False)
    assert result["ok"]
    assert [row.content for row in service.long_term.all()] == ["admin approved fact"]


# LLM: 线程标记通过原update_atomic持久化；断言实际消息读取器完全没有被调用。
# 函数用途: 只读/排除线程在Curator输入收集前跳过消息及审计。
@pytest.mark.parametrize("metadata", [{"learning_excluded": True}, {"bot_mode": "query_readonly"}])
def test_h16_excluded_thread_never_enters_curator_inputs(tmp_path, monkeypatch, metadata):
    import json

    from agent_py_agent.agent.memory_store.curator_inputs import (
        CuratorMessageSource,
        collect_curator_inputs,
    )
    from agent_py_agent.agent.memory_store.curator_models import (
        MemoryCuratorConfig,
        MemoryCuratorState,
    )

    service, thread, _message = _m2_service(tmp_path)
    store = service.conversation_store
    store.threads.update_atomic(thread.thread_id, lambda row: replace(row, metadata=metadata))
    audit = tmp_path / "h16-audit"
    audit.mkdir()
    (audit / "2026-10-26.jsonl").write_text(json.dumps(dict(event_id="excluded-audit", thread_id=thread.thread_id)) + "\n")
    monkeypatch.setattr(store.messages, "after_report", lambda *_a, **_k: pytest.fail("excluded thread was read"))
    batch = collect_curator_inputs(message_source=CuratorMessageSource(store), audit_dir=audit,
                                   state=MemoryCuratorState(), config=MemoryCuratorConfig())
    assert batch.messages == () and batch.audit_events == ()
    assert batch.skipped_audit_cursor == "excluded-audit"


# LLM: 用真实存储中的消息元数据过滤，严格True而不是按字符串/数字猜排除。
# 函数用途: 消息级排除不污染模型输入，也不丢掉正常消息。
@pytest.mark.parametrize("excluded", [True, False, "true", 1])
def test_h16_message_learning_excluded_is_filtered_before_model(tmp_path, excluded):
    from agent_py_agent.agent.memory_store.curator_inputs import (
        CuratorMessageSource,
        _collect_messages,
    )

    service, thread, first = _m2_service(tmp_path)
    store = service.conversation_store
    flagged = store.messages.append(dict(thread_id=thread.thread_id, role="user", content="message flag",
                                        metadata={"learning_excluded": excluded}))
    normal = store.messages.append(dict(thread_id=thread.thread_id, role="user", content="normal after flag"))
    rows, errors = _collect_messages(source=CuratorMessageSource(store), cursors={thread.thread_id: first.message_id},
                                     limit=20, max_chars=20_000)
    assert not errors
    assert [row.message_id for row in rows] == ([normal.message_id] if excluded is True else [flagged.message_id, normal.message_id])


# LLM: 真实同inode等长原地改写并恢复mtime，不能通过刷新实例或改版本绕开旧缓存。
# 函数用途: ctime变化必须让confirm_scoped_access拒绝缓存中的旧正文。
def test_h16_jsonl_ctime_invalidates_scoped_access_cache(tmp_path):
    import os

    from agent_py_agent.agent.memory_store.jsonl import JsonlMemory

    path = tmp_path / "memory.jsonl"
    memory = JsonlMemory(path)
    memory.add("system", "safe-alpha", kind="fact", attributes={"origin": "user_explicit"})
    candidates = memory.all()
    assert len(candidates) == 1
    before = path.stat()
    raw = path.read_bytes()
    changed = raw.replace(b"safe-alpha", b"safe-omega")
    assert changed != raw and len(changed) == len(raw)
    with path.open("r+b") as handle:
        handle.write(changed)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert (before.st_ino, before.st_size, before.st_mtime_ns) == (after.st_ino, after.st_size, after.st_mtime_ns)
    assert before.st_ctime_ns != after.st_ctime_ns
    assert memory.confirm_scoped_access(candidates, lambda _record: True) == []
    assert memory.all()[0].content == "safe-omega"
