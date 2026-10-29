# LLM: 只使用临时正式库，验证同代冷热分层、墓碑恢复和派生索引，不读取生产数据。
# 模块用途: 为长期记忆库存预算替代拒写硬帽提供回归证据。
import json
from dataclasses import asdict

import pytest

from agent_py_agent.agent.memory_store.jsonl import JsonlMemory, MemoryRecord


# LLM: 固定身份、时间和未知扩展；输入直接模拟升级前的存量文件，不绕过写权限。
# 函数用途: 创建可重复比较的旧库样本。
def _legacy(memory, sizes):
    rows = [MemoryRecord("user", chr(0x4e00 + i) * size, entry_id=f"fact-{i:04d}",
                         kind="fact", created_at=i + 1, updated_at=i + 1,
                         attributes={"origin": "user_explicit", "scope_type": "personal",
                                     "scope_key": "personal"}) for i, size in enumerate(sizes)]
    memory.path.write_text("".join(json.dumps(asdict(r), ensure_ascii=False) + "\n" for r in rows))
    return rows


# LLM: 不以异常作为红灯；旧实现抛拒写错误时，明确断言观察到的不符合新合同。
# 函数用途: 覆盖热层字符预算的三个边界与超长单条保存。
@pytest.mark.parametrize("size", [19000, 20000, 21000])
def test_memory_tier_budget_never_refuses_write(tmp_path, size):
    memory = JsonlMemory(tmp_path / "memory.jsonl")
    error = None
    try:
        memory.add("user", "甲" * size)
    except Exception as exc:
        error = exc
    assert error is None, f"库存热预算不能拒写: {type(error).__name__}"
    assert [r.content for r in memory.all()] == ["甲" * size]
    active = getattr(memory, "active_records", None)
    assert callable(active), "必须提供与全逻辑库区分的热层视图"
    assert sum(len(r.content) for r in active()) <= 20000


# LLM: 合成样本精确76300字；验证迁移前后逐字段相等、幂等及热层16000字目标。
# 函数用途: 检验一次性迁移而不接触真实主账号库。
def test_memory_tier_migration_76300_lossless_and_idempotent(tmp_path):
    memory = JsonlMemory(tmp_path / "memory.jsonl")
    before = _legacy(memory, [10000] * 7 + [6300])
    migrate = getattr(memory, "migrate_tiers", None)
    assert callable(migrate), "缺少可预演的幂等分层迁移入口"
    raw = memory.path.read_bytes()
    preview = migrate(dry_run=True)
    assert preview["total_chars"] == 76300
    assert preview["active_chars"] <= 16000
    assert memory.path.read_bytes() == raw
    assert not memory.path.with_name("memory.jsonl.manifest.json").exists()
    result = migrate()
    assert result["active_chars"] <= 16000
    assert sorted(map(asdict, memory.all()), key=lambda r: r["entry_id"]) == list(map(asdict, before))
    generation = result["generation"]
    assert migrate()["generation"] == generation
    assert [r.content for r in JsonlMemory(memory.path).all()] == [r.content for r in before]


# LLM: 原condense参数仅保留调用兼容，不能再把存储搬迁作为业务删除。
# 函数用途: 防止先archive后remove的双阶段算法回归。
def test_memory_tier_condense_does_not_forget_or_increment_version(tmp_path):
    memory = JsonlMemory(tmp_path / "memory.jsonl")
    before = _legacy(memory, [12000, 12000])
    before[0].attributes["origin"] = "model_inferred"
    memory.path.write_text("".join(r.to_json() + "\n" for r in before))
    memory.condense(max_records=1, target_records=1)
    assert {r.entry_id: (r.content, r.version) for r in memory.all()} == {
        r.entry_id: (r.content, r.version) for r in before}
    assert memory.committed_removal_record(before[0].entry_id) is None


# LLM: 新增后同一代内按结构化热度下沉；user_explicit只保证不遗忘，不强占热层。
# 函数用途: 验证冷热选择稳定、置顶保留和超帽继续学习。
def test_memory_tier_lru_and_pinned_placement(tmp_path):
    memory = JsonlMemory(tmp_path / "memory.jsonl")
    rows = _legacy(memory, [9000, 9000])
    rows[0].attributes["pinned"] = True
    memory.path.write_text("".join(r.to_json() + "\n" for r in rows))
    memory.add("user", "新" * 9000)
    active = getattr(memory, "active_records", None)
    assert callable(active), "缺少热层视图"
    assert rows[0].entry_id in {r.entry_id for r in active()}
    assert rows[1].entry_id not in {r.entry_id for r in active()}
    assert len(memory.all()) == 3


# LLM: 在真实根清单写入点注入中断；未提交分片必须不可见，旧代不能被半代覆盖。
# 函数用途: 检验commit-last的崩溃边界。
def test_memory_tier_manifest_failure_keeps_old_generation(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import jsonl

    memory = JsonlMemory(tmp_path / "memory.jsonl")
    _legacy(memory, [12000, 12000])
    migrate = getattr(memory, "migrate_tiers", None)
    assert callable(migrate), "缺少同代发布入口"
    migrate()
    before = {r.entry_id: r.content for r in memory.all()}
    writer = jsonl.write_private_text_file_atomic_unlocked

    # LLM: 仅阻断根清单发表；其它文件仍走真实原子写入。
    # 函数用途: 模拟新分片已落盘而最终提交失败。
    def fail_manifest(path, raw, **kwargs):
        if path.name == "memory.jsonl.manifest.json":
            raise OSError("injected manifest failure")
        return writer(path, raw, **kwargs)

    monkeypatch.setattr(jsonl, "write_private_text_file_atomic_unlocked", fail_manifest)
    with pytest.raises(OSError, match="injected manifest failure"):
        memory.add("user", "uncommitted")
    assert {r.entry_id: r.content for r in JsonlMemory(memory.path).all()} == before


# LLM: 恢复旧代时依然读取当前独立墓碑，且陈旧索引/向量不能重新授予可见性。
# 函数用途: 验证冷事实删除、备份恢复和重建的永久屏障。
def test_memory_tombstone_blocks_cold_backup_restore(tmp_path):
    memory = JsonlMemory(tmp_path / "memory.jsonl")
    rows = _legacy(memory, [12000, 12000])
    migrate = getattr(memory, "migrate_tiers", None)
    assert callable(migrate), "缺少分层迁移入口"
    migrate()
    memory.remove(rows[0].entry_id, expected_version=1, source="test:forget")
    restore = getattr(memory, "restore_tier_records", None)
    assert callable(restore), "恢复必须显式叠加不可回退墓碑"
    restore(rows)
    assert [r.entry_id for r in memory.all()] == [rows[1].entry_id]
    assert memory.search(rows[0].content[:10]) == []
    assert memory.committed_removal_record(rows[0].entry_id).source == "test:forget"


# LLM: 冷检索经同一owner和scope谓词；独立目录相同ID也不得混用派生索引。
# 函数用途: 验证冷层自然召回及跨owner拒绝。
def test_memory_tier_cold_retrieval_and_owner_isolation(tmp_path):
    memory = JsonlMemory(tmp_path / "a" / "memory.jsonl")
    _legacy(memory, [12000, 12000])
    migrate = getattr(memory, "migrate_tiers", None)
    assert callable(migrate), "缺少冷检索来源"
    migrate()
    assert [r.entry_id for r in memory.search_scoped("一一一", 5, lambda r: True)] == ["fact-0000"]
    assert memory.search_scoped("一一一", 5, lambda r: False) == []
    other = JsonlMemory(tmp_path / "b" / "memory.jsonl")
    assert other.search("一一一") == []


# LLM: 删除SQLite后只推进有界批次并持久化水位，实例重建不能让游标回零或谎称完整。
# 函数用途: 验证派生冷读索引的显式有界恢复和实际coverage。
def test_memory_tier_read_index_resumes_with_coverage(tmp_path):
    memory = JsonlMemory(tmp_path / "memory.jsonl")
    _legacy(memory, [21] * 1005)
    memory.migrate_tiers()
    step = getattr(memory, "rebuild_read_index", None)
    assert callable(step), "缺少有界派生索引维护入口"
    first = step(batch_size=100)
    assert first["status"] == "partial"
    assert 0 < first["indexed_records"] <= 100
    memory = JsonlMemory(memory.path)
    progress = first
    for _ in range(12):
        progress = memory.rebuild_read_index(batch_size=100)
        if progress["status"] == "complete":
            break
    assert progress["status"] == "complete"
    assert progress["indexed_records"] == 1005
    assert (tmp_path / "memory_read_index.sqlite3").exists()


# LLM: 政策未知扩展与旧排除名单必须保留；库存0非法，注入0只关闭本轮信封。
# 函数用途: 验证类型化政策、派生工作约定预算和真实库存配置读取。
def test_memory_tier_policy_and_context_budget(tmp_path):
    from agent_py_agent.agent.memory_store.generation_manifest import ContextBudget, MemoryPolicyV2

    original = {"schema_version": "memory-policy.v1", "enabled": False,
                "learning_exclusions": ["source"], "learning_exclusions_revision": 7, "future": {"x": 1},
                "budget": {"active_library_max_chars": 100, "recall_max_chars": 111,
                           "hot_injection_max_chars": 0, "future_budget": 9}}
    policy = MemoryPolicyV2.from_payload(original)
    context = ContextBudget.from_policy(policy, effective_recall_chars=101)
    assert context.working_agreement_chars == 60
    assert context.hot_injection_max_chars == 0
    encoded = policy.to_payload()
    assert encoded["learning_exclusions"] == original["learning_exclusions"]
    assert encoded["future"] == original["future"]
    assert encoded["budget"]["future_budget"] == 9
    with pytest.raises(ValueError, match="MEMORY_POLICY_INVALID"):
        MemoryPolicyV2.from_payload({"budget": {"active_library_max_chars": 0}})
    (tmp_path / "memory_policy.json").write_text(json.dumps(original))
    memory = JsonlMemory(tmp_path / "memory.jsonl")
    memory.add("user", "长" * 101)
    assert memory.active_records() == []
    assert len(memory.all()[0].content) == 101


# LLM: 真实SQLite候选必须回读分片；当前水位已覆盖时不应重扫全逻辑库。
# 函数用途: 验证索引真实接入关键词及scope检索，而非只建一个未使用的库。
def test_memory_tier_read_index_is_used_and_revalidates_scope(tmp_path, monkeypatch):
    memory = JsonlMemory(tmp_path / "memory.jsonl")
    _legacy(memory, [12000, 12000])
    memory.migrate_tiers()
    memory.rebuild_read_index()

    # LLM: 只禁止全库路径，允许真实SQLite与分片回读；失败表明索引未接主链。
    # 函数用途: 捕获索引已追平后的无谓全库扫描。
    def no_full_scan():
        pytest.fail("完整索引命中不应重扫all")

    monkeypatch.setattr(memory, "all", no_full_scan)
    assert memory.search_report("一一一", 3)[0][0].entry_id == "fact-0000"
    records, facts = memory.search_scoped_candidates_report("一一一", 3, lambda r: True)
    assert records[0].entry_id == "fact-0000"
    assert facts["coverage"]["status"] == "complete"
    assert memory.search_scoped_candidates("一一一", 3, lambda r: False) == []


# LLM: 永久屏障不仅挡召回，旧代和派生索引里的已删正文也必须执行清理责任。
# 函数用途: 防止同库内部历史分片残留已遗忘正文。
def test_memory_tombstone_purges_old_generation_plaintext(tmp_path):
    memory = JsonlMemory(tmp_path / "memory.jsonl")
    rows = _legacy(memory, [12000, 12000])
    memory.migrate_tiers()
    memory.rebuild_read_index()
    memory.add("user", "new generation")
    memory.remove(rows[0].entry_id, expected_version=1)
    secret = rows[0].content.encode()
    for path in tmp_path.rglob("*.jsonl"):
        assert secret not in path.read_bytes(), f"删除正文残留在{path.name}"
    database = tmp_path / "memory_read_index.sqlite3"
    assert not database.exists() or secret not in database.read_bytes()


# LLM: 旧LocalStore只保留隐私清理依赖，任何正式小库也不得继续双写或双读旧投影。
# 函数用途: 固定DEL-MR-07/08的真实生产调用边界。
def test_memory_tier_never_reads_or_writes_workspace_projection(tmp_path, monkeypatch):
    from agent_py_agent.agent.local_storage import LocalStore

    store = LocalStore(tmp_path / "workspace" / "local.db")
    memory = JsonlMemory(tmp_path / "memory.jsonl", local_store=store)
    saved = memory.add("user", "只在正式库保存的事实")
    assert store.search("只在正式库", source_type="memory") == []

    # LLM: 禁止旧查询入口，正常关键词仍必须由正式源返回。
    # 函数用途: 防止无索引时暗中回退workspace记忆搜索。
    def no_workspace_search(*args, **kwargs):
        pytest.fail("正式召回仍调用旧LocalStore")

    monkeypatch.setattr(store, "search", no_workspace_search)
    assert memory.search_report("正式库", 3)[0][0].entry_id == saved.entry_id


# LLM: 显式memory索引重建只改变owner内派生库，不清workspace消息/工具数据。
# 函数用途: 固定重建与旧LocalStore隔离的裁剪边界。
def test_memory_tier_rebuild_does_not_reset_workspace(tmp_path):
    from agent_py_agent.agent.local_storage import LocalStore

    store = LocalStore(tmp_path / "workspace" / "local.db")
    store.upsert_record(source_type="message", source_id="keep", content="workspace消息保留", title="message")
    memory = JsonlMemory(tmp_path / "memory.jsonl", local_store=store)
    memory.add("user", "正式事实")
    assert memory.index_all(reset=True) == 1
    assert store.search("workspace消息保留", source_type="message")
    assert store.search("正式事实", source_type="memory") == []


# LLM: 已迁移库缺少永久删除账不得当空集，避免损坏/恢复后屏障静默消失。
# 函数用途: 验证丢失删除账时拒绝继续召回。
def test_memory_tombstone_missing_ledger_fails_closed(tmp_path):
    from agent_py_agent.agent.memory_store.generation_manifest import StorageTombstones

    memory = JsonlMemory(tmp_path / "memory.jsonl")
    rows = _legacy(memory, [12000, 12000])
    memory.migrate_tiers()
    memory.remove(rows[0].entry_id)
    StorageTombstones(memory.path).path.unlink()
    with pytest.raises(ValueError, match="MEMORY_TOMBSTONES_UNREADABLE"):
        JsonlMemory(memory.path).all()


# LLM: 公共batch先规范化action再核墓碑，不能通过大小写或空白生成虚假保存回执。
# 函数用途: 防止已删除身份经别名形状重新写入。
@pytest.mark.parametrize("action", ["ADD", " add "])
def test_memory_tombstone_cannot_bypass_normalized_action(tmp_path, action):
    memory = JsonlMemory(tmp_path / "memory.jsonl")
    rows = _legacy(memory, [12000, 12000])
    memory.migrate_tiers()
    memory.remove(rows[0].entry_id)
    with pytest.raises(ValueError, match="MEMORY_TOMBSTONED_ID"):
        memory.apply_batch([{"action": action, "entry_id": rows[0].entry_id, "content": "forged new life"}])


# LLM: 健康入口必须读取同代逻辑库，不能把旧文件的schema标记当坏正文。
# 函数用途: 验证迁移后的真实健康状态与冷热计数。
def test_memory_tier_runtime_snapshot_is_generation_aware(tmp_path):
    memory = JsonlMemory(tmp_path / "memory.jsonl")
    _legacy(memory, [12000, 12000])
    memory.migrate_tiers()
    snapshot = memory.runtime_snapshot()
    assert snapshot["state"] == "available"
    assert snapshot["active_total"] == 2
    assert snapshot["cold_total"] == 1


# LLM: 旧workspace副本只由受控删除路径打开；使用手工播种的旧行，不依赖已经移除的新双写。
# 函数用途: 验证忘记冷事实会清旧投影，但保留无关workspace消息。
def test_memory_tombstone_cleans_seeded_legacy_projection(tmp_path):
    from agent_py_agent.agent.local_storage import LocalStore

    store = LocalStore(tmp_path / "workspace" / "local.db")
    memory = JsonlMemory(tmp_path / "memory.jsonl", local_store=store)
    record = memory.add("user", "私" * 21000)
    store.upsert_record(source_type="memory", source_id=record.entry_id,
                        title="old projection", content=record.content)
    old_id = store.make_record_id("memory", record.entry_id)
    store.upsert_record(source_type="message", source_id="keep", title="message", content="无关消息保留")
    assert store.get_record(old_id) is not None
    memory.remove(record.entry_id, expected_version=1)
    assert memory.all() == []
    assert store.get_record(old_id) is None
    assert store.search("无关消息保留", source_type="message")


# LLM: 物理quota仍可拒写，检查必须先于分片/根/删除账落盘，热预算本身不能触发拒绝。
# 函数用途: 验证同代暂存峰值受真实准入控制，拒绝不产生半批。
def test_memory_tier_quota_refusal_preserves_preimage(tmp_path):
    from agent_py_agent.agent.memory_store.generation_manifest import MemoryGenerationStore

    memory = JsonlMemory(tmp_path / "memory.jsonl")
    memory.add("user", "旧" * 15000)
    before = memory.path.read_bytes()

    # LLM: 仅模拟原quota准入回执，实际事务与文件路径均走生产代码。
    # 类用途: 捕获峰值计划并在任何发表前拒绝准入。
    class Admission:
        # LLM: 验证包含新分片、根及独立墓碑，不允许只检查热文件净增长。
        # 函数用途: 返回确定的物理空间不足错误。
        def check(self, changes):
            names = {change.path.name for change in changes}
            assert "memory.jsonl.manifest.json" in names
            assert "memory.jsonl.storage_tombstones.json" in names
            raise ValueError("physical quota full")

    with pytest.raises(ValueError, match="physical quota full"):
        memory.apply_batch([{"action": "add", "content": "新" * 15000}], _admission=Admission())
    assert memory.path.read_bytes() == before
    assert MemoryGenerationStore(memory.path).manifest() is None
    assert len(memory.all()) == 1


# LLM: 旧候选只携带旧代坐标，当前代不同必须返回显式冲突，不能伪装完整空命中。
# 函数用途: 复现独立复核指出的查询/解析换代窗口。
def test_memory_read_index_rejects_cross_generation_coordinates(tmp_path):
    from agent_py_agent.agent.memory_store.read_index import MemoryReadIndex

    memory = JsonlMemory(tmp_path / "memory.jsonl")
    _legacy(memory, [12000, 12000])
    memory.migrate_tiers()
    memory.rebuild_read_index()
    index = MemoryReadIndex(memory.path)
    candidates, coverage = index.query("一一一")
    assert candidates and coverage["status"] == "complete"
    memory.add("user", "新一代事实")
    failure = None
    try:
        index.resolve(candidates)
    except ValueError as exc:
        failure = str(exc)
    assert failure == "MEMORY_READ_INDEX_GENERATION_CONFLICT"


# LLM: 水位与候选必须来自同一SQLite读事务；只包装连接观测，不伪造任何SQL或返回行。
# 函数用途: 防止先关闭水位连接再打开已换代/部分重建索引。
def test_memory_read_index_watermark_and_query_share_connection(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store.read_index import MemoryReadIndex

    memory = JsonlMemory(tmp_path / "memory.jsonl")
    _legacy(memory, [12000, 12000])
    memory.migrate_tiers()
    memory.rebuild_read_index()
    index = MemoryReadIndex(memory.path)
    connect = index._connect
    connections, statements = [], []

    # LLM: 保留真实连接与SQL跟踪，仅记录本次只读连接生命周期。
    # 函数用途: 观测水位与全文查询是否共享同一BEGIN事务。
    def observe(**kwargs):
        connection = connect(**kwargs)
        connections.append(connection)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(index, "_connect", observe)
    assert index.query("一一一")[0]
    assert len(connections) == 1
    assert "BEGIN" in statements


# LLM: 根未变不等于永久账未变；旧计划即使串行重试也不能覆盖已发表删除屏障。
# 函数用途: 复现根发表失败后旧计划回滚墓碑导致复活的条件风险。
def test_memory_tombstone_stale_plan_cannot_erase_committed_barrier(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import jsonl
    from agent_py_agent.agent.memory_store.generation_manifest import MemoryGenerationStore

    memory = JsonlMemory(tmp_path / "memory.jsonl")
    rows = _legacy(memory, [12000, 12000])
    memory.migrate_tiers()
    store = MemoryGenerationStore(memory.path)
    stale = store.prepare(jsonl._strict_memory_events(memory.path), max_chars=20000)
    writer = jsonl.write_private_text_file_atomic_unlocked

    # LLM: 墓碑仍走真实writer，仅阻断最后根替换，使永久账已经变化而根保持原代。
    # 函数用途: 构造旧根仍可通过CAS的真实中断前像。
    def fail_root(path, raw, **kwargs):
        if path == store.manifest_path:
            raise OSError("injected deletion root failure")
        return writer(path, raw, **kwargs)

    monkeypatch.setattr(jsonl, "write_private_text_file_atomic_unlocked", fail_root)
    with pytest.raises(Exception, match="injected deletion root failure|REMOVE_CLEANUP_PENDING"):
        memory.remove(rows[0].entry_id)
    monkeypatch.setattr(jsonl, "write_private_text_file_atomic_unlocked", writer)
    try:
        store.publish(stale, write=writer)
    except ValueError as exc:
        assert str(exc) == "MEMORY_GENERATION_CONFLICT"
    assert rows[0].entry_id not in {r.entry_id for r in memory.all()}


# LLM: 永久屏障先于根生效时，失败回执也必须承认本轮删除生效并保留清理责任。
# 函数用途: 验证中断删除的partial回执及幂等重试擦除。
def test_memory_tombstone_root_failure_reports_partial_and_retry_cleans(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import jsonl
    from agent_py_agent.agent.memory_store._jsonl_indexing import RemovalCleanupError

    memory = JsonlMemory(tmp_path / "memory.jsonl")
    rows = _legacy(memory, [12000, 12000])
    memory.migrate_tiers()
    writer = jsonl.write_private_text_file_atomic_unlocked

    # LLM: 只阻断根发表，不伪造独立墓碑写入结果。
    # 函数用途: 观察真实永久屏障与异常回执的先后顺序。
    def fail_root(path, raw, **kwargs):
        if path.name == "memory.jsonl.manifest.json":
            raise OSError("injected deletion root failure")
        return writer(path, raw, **kwargs)

    monkeypatch.setattr(jsonl, "write_private_text_file_atomic_unlocked", fail_root)
    failure = None
    try:
        memory.remove(rows[0].entry_id)
    except Exception as exc:
        failure = exc
    assert isinstance(failure, RemovalCleanupError)
    assert failure.formal_memory_changed is True
    assert rows[0].entry_id not in {r.entry_id for r in memory.all()}
    monkeypatch.setattr(jsonl, "write_private_text_file_atomic_unlocked", writer)
    memory.remove(rows[0].entry_id)
    assert all(rows[0].content.encode() not in path.read_bytes() for path in tmp_path.rglob("*.jsonl"))


# LLM: 迁移后的唯一权威是同代根与分片，旧schema标记缺失不得阻断有效库的索引维护。
# 函数用途: 固定DEL-MR-09在reset维护入口也不依赖旧单文件。
def test_memory_tier_reset_uses_manifest_when_legacy_marker_missing(tmp_path):
    memory = JsonlMemory(tmp_path / "memory.jsonl")
    _legacy(memory, [12000, 12000])
    memory.migrate_tiers()
    memory.path.unlink()
    error = None
    try:
        indexed = memory.index_all(reset=True, require_source=True)
    except Exception as exc:
        error = exc
        indexed = None
    assert error is None, f"同代正式库仍完整却依赖旧标记: {error}"
    assert indexed == 2
