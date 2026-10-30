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


# LLM: 墓碑先成功而根失败后只走真实清理入口，不再次remove掩盖恢复缺口。
# 函数用途: 验证永久删除可幂等推进滞后的根并清掉正文。
def test_r1_purge_advances_root_after_tombstone_commit_failure(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import jsonl
    from agent_py_agent.agent.memory_store._jsonl_indexing import RemovalCleanupError
    from agent_py_agent.agent.memory_store.generation_manifest import MemoryGenerationStore

    memory = JsonlMemory(tmp_path / "memory.jsonl")
    rows = _legacy(memory, [12000, 12000])
    memory.migrate_tiers()
    store = MemoryGenerationStore(memory.path)
    writer = jsonl.write_private_text_file_atomic_unlocked
    before = store.manifest()["operation_id"]

    # LLM: 只故障根发表，墓碑及分片使用产品writer。
    # 函数用途: 固定删除部分提交边界。
    def fail_root(path, raw, **kwargs):
        if path == store.manifest_path:
            raise OSError("r1 root write failed")
        return writer(path, raw, **kwargs)

    monkeypatch.setattr(jsonl, "write_private_text_file_atomic_unlocked", fail_root)
    with pytest.raises(RemovalCleanupError):
        memory.remove(rows[0].entry_id, source="candidate:r1")
    monkeypatch.setattr(jsonl, "write_private_text_file_atomic_unlocked", writer)
    assert memory.committed_removal_record(rows[0].entry_id).source == "candidate:r1"
    error = None
    try:
        memory.purge_removed_indexes(rows[0].entry_id)
    except RemovalCleanupError as exc:
        error = exc
    assert error is None, "已提交墓碑的恢复不能永远卡在旧根"
    assert store.manifest()["operation_id"] != before
    assert all(rows[0].content.encode() not in p.read_bytes() for p in tmp_path.rglob("*.jsonl"))
    recovered = store.manifest()["operation_id"]
    memory.purge_removed_indexes(rows[0].entry_id)
    assert store.manifest()["operation_id"] == recovered


# LLM: 零活记录的合法根不要求存在分片目录，重复purge不得报缺目录。
# 函数用途: 验证tombstone-only空代清理。
def test_r1_purge_tombstone_only_generation(tmp_path):
    memory = JsonlMemory(tmp_path / "memory.jsonl")
    row = memory.add("user", "只有墓碑的库")
    memory.remove(row.entry_id)
    assert memory.migrate_tiers()["total_records"] == 0
    error = None
    try:
        memory.purge_removed_indexes(row.entry_id)
        memory.purge_removed_indexes(row.entry_id)
    except Exception as exc:
        error = exc
    assert error is None, f"合法空代不能视为损坏: {error}"


# LLM: 相同正文/范围/查询只改变索引维护状态，三种原有效召回必须等价。
# 函数用途: 锁定中文bigram、英文关键词及短中文混合查询。
@pytest.mark.parametrize("content,attrs,query,scoped", [
    ("祥子买了两次车。", {}, "祥子买车", True),
    ("祥子买了两次车。", {"keywords_en": ["bicycle"]}, "bicycle", True),
    ("我爱北京。", {}, "北京 travel", False),
])
def test_r1_complete_index_preserves_lexical_recall(tmp_path, content, attrs, query, scoped):
    memory = JsonlMemory(tmp_path / "memory.jsonl")
    row = memory.add_record(MemoryRecord("user", content, entry_id="lexical", attributes=attrs))
    before = (memory.search_scoped_candidates_report(query, 5, lambda r: True)[0] if scoped
              else memory.search_report(query, 5)[0])
    assert [r.entry_id for r in before] == [row.entry_id]
    memory.index_all()
    after = (memory.search_scoped_candidates_report(query, 5, lambda r: True)[0] if scoped
             else memory.search_report(query, 5)[0])
    assert [r.entry_id for r in after] == [row.entry_id]


# LLM: 备份没有ID时使用产品legacy身份，保留版本不同的独立正文且不修改输入对象。
# 函数用途: 验证恢复后仍可普通新增，不生成缺ID分片。
@pytest.mark.parametrize("count", [1, 2])
def test_r1_restore_legacy_ids_before_dedup_and_publish(tmp_path, count):
    memory = JsonlMemory(tmp_path / "memory.jsonl")
    incoming = [MemoryRecord("user", f"旧格式事实{i}", created_at=i + 1, version=i + 1) for i in range(count)]
    result = memory.restore_tier_records(incoming)
    assert result["total_records"] == count
    assert all(r.entry_id.startswith("memory-legacy-") for r in memory.all())
    assert all(r.entry_id == "" for r in incoming)
    saved = memory.add("user", "恢复后继续新增")
    assert saved.entry_id in {r.entry_id for r in memory.all()}


# LLM: 小库未迁移也必须永久记录删除；跨实例的旧ID重加不能成功回执。
# 函数用途: 验证永久屏障覆盖原小库兼容写分支。
@pytest.mark.parametrize("restart", [False, True])
def test_r1_small_store_permanent_barrier(tmp_path, restart):
    from agent_py_agent.agent.memory_store.generation_manifest import StorageTombstones

    memory = JsonlMemory(tmp_path / "memory.jsonl")
    memory.add_record(MemoryRecord("user", "明确遗忘", entry_id="A"))
    memory.remove("A")
    assert "A" in StorageTombstones(memory.path).load()
    if restart:
        memory = JsonlMemory(memory.path)
    with pytest.raises(ValueError, match="MEMORY_TOMBSTONED_ID"):
        memory.apply_batch([{"action": "add", "entry_id": "A", "content": "不许复活"}])


# LLM: 删除屏障核验要先strip ID，不能提交后静默丢弃并给保存成功回执。
# 函数用途: 验证空白等价身份被明确拒绝。
def test_r1_tombstone_id_normalized_before_barrier(tmp_path):
    memory = JsonlMemory(tmp_path / "memory.jsonl")
    row = memory.add("user", "甲" * 21000)
    memory.remove(row.entry_id)
    with pytest.raises(ValueError, match="MEMORY_TOMBSTONED_ID"):
        memory.apply_batch([{"action": "add", "entry_id": f" {row.entry_id} ", "content": "未保存的新文"}])


# LLM: 换ID不能绕过永久正文hash屏障；小库与已迁移库使用同一合同。
# 函数用途: 验证备份异ID同正文不复活。
@pytest.mark.parametrize("migrate", [False, True])
def test_r1_restore_different_id_same_forgotten_hash(tmp_path, migrate):
    memory = JsonlMemory(tmp_path / "memory.jsonl")
    row = memory.add_record(MemoryRecord("user", "永久遗忘的正文", entry_id="A"))
    if migrate:
        memory.migrate_tiers()
    memory.remove("A")
    result = memory.restore_tier_records([MemoryRecord("user", row.content, entry_id="B")])
    assert result["total_records"] == 0
    assert memory.all() == []


# LLM: 一次性迁移幂等不能吞掉降预算维护；转冷不改变正文/业务版本。
# 函数用途: 验证已迁移库condense按当前热预算重新规划。
def test_r1_condense_retiers_migrated_store_after_budget_lowering(tmp_path):
    memory = JsonlMemory(tmp_path / "memory.jsonl")
    row = memory.add("user", "甲" * 2000)
    memory.migrate_tiers()
    (tmp_path / "memory_policy.json").write_text(json.dumps({"budget": {"active_library_max_chars": 100}}))
    assert [r.entry_id for r in memory.condense()] == [row.entry_id]
    assert sum(len(r.content) for r in memory.active_records()) <= 100
    assert memory.all()[0].content == row.content


# LLM: 只改变热片时冷片路径与字节不变；成功根发表后无引用暂存必须回收。
# 函数用途: 验证不可变冷片复用和旧/失败代目录收敛。
def test_r1_cold_reuse_and_superseded_generation_gc(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import jsonl
    from agent_py_agent.agent.memory_store.generation_manifest import MemoryGenerationStore

    memory = JsonlMemory(tmp_path / "memory.jsonl")
    _legacy(memory, [12000, 12000])
    memory.migrate_tiers()
    store = MemoryGenerationStore(memory.path)
    cold = [s for s in store.manifest()["segments"] if s["tier"] == "cold"]
    writer = jsonl.write_private_text_file_atomic_unlocked

    # LLM: 故障根而保留真实暂存，用下一次成功发布验证回收。
    # 函数用途: 固定失败代残留场景。
    def fail_root(path, text, **kwargs):
        if path == store.manifest_path:
            raise OSError("failed staging")
        return writer(path, text, **kwargs)

    monkeypatch.setattr(jsonl, "write_private_text_file_atomic_unlocked", fail_root)
    with pytest.raises(OSError):
        memory.add("user", "失败的新增")
    monkeypatch.setattr(jsonl, "write_private_text_file_atomic_unlocked", writer)
    for i in range(3):
        memory.add("user", f"新的热事实{i}")
    root = store.manifest()
    assert [s for s in root["segments"] if s["tier"] == "cold"] == cold
    expected = {tmp_path / s["path"] for s in root["segments"]}
    assert set(store.directory.glob("*/*.jsonl")) == expected


# LLM: touch不改变词面身份或分片行序，持续元数据发表不能把每批索引游标归零。
# 函数用途: 验证跨touch代次的增量建索引进度。
def test_r1_index_cursor_survives_touch_generation(tmp_path):
    memory = JsonlMemory(tmp_path / "memory.jsonl")
    _legacy(memory, [21] * 1005)
    memory.migrate_tiers()
    first = memory.rebuild_read_index(batch_size=10)
    memory._note_access("fact-1004")
    memory.flush_access_events()
    second = memory.rebuild_read_index(batch_size=10)
    assert second["indexed_records"] == first["indexed_records"] + 10


# LLM: 索引缺失时不得调用无界all；按真实条数预算报告partial和实际扫描范围。
# 函数用途: 验证scoped/普通检索共用有界权威降级。
@pytest.mark.parametrize("scoped", [False, True])
def test_r1_fallback_search_is_bounded_and_partial(tmp_path, monkeypatch, scoped):
    from agent_py_agent.agent.memory_store import jsonl

    memory = JsonlMemory(tmp_path / "memory.jsonl")
    _legacy(memory, [30] * 6)
    memory.migrate_tiers()
    monkeypatch.setattr(jsonl, "_AUTHORITY_SCAN_MAX_RECORDS", 3, raising=False)
    monkeypatch.setattr(jsonl, "_AUTHORITY_SCAN_MAX_BYTES", 1024 * 1024, raising=False)

    # LLM: 观察权威入口，不替换检索结果。
    # 函数用途: 阻止把全库读取伪装成有限降级。
    def no_all():
        pytest.fail("缺索引降级仍调用无界all")

    monkeypatch.setattr(memory, "all", no_all)
    rows, facts = (memory.search_scoped_candidates_report("一一", 5, lambda r: True) if scoped
                   else memory.search_report("一一", 5))
    coverage = facts["coverage"] if scoped else facts[-1]["coverage"]
    assert coverage["partial"] is True
    assert coverage["scanned_records"] == 3
    assert len(rows) <= 3


# LLM: 一个巨大片不能绕过字节预算，预算在读正文之前执行而不是全量读后截断。
# 函数用途: 验证有界降级拒绝读超预算分片并如实partial。
def test_r1_fallback_byte_budget_precedes_segment_read(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import jsonl
    from agent_py_agent.agent.memory_store.generation_manifest import MemoryGenerationStore

    memory = JsonlMemory(tmp_path / "memory.jsonl")
    memory.add("user", "巨" * 21000)
    monkeypatch.setattr(jsonl, "_AUTHORITY_SCAN_MAX_BYTES", 64, raising=False)

    # LLM: 被超预算拦下时不得打开整片读取函数。
    # 函数用途: 记录实际I/O入口是否被错误调用。
    def no_segment(*args):
        pytest.fail("字节预算生效前已经整读分片")

    monkeypatch.setattr(MemoryGenerationStore, "segment_rows", no_segment)
    rows, facts = memory.search_report("巨", 5)
    assert rows == [] and facts[-1]["coverage"]["partial"] is True


# LLM: 固定旧根读者与真实删除/purge交错，根变时有限重读，根不变损坏仍拒绝。
# 函数用途: 验证并发发表不被误报为损坏，也不吞真损坏。
def test_r1_reader_retries_after_concurrent_generation_purge(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store.generation_manifest import MemoryGenerationStore

    memory = JsonlMemory(tmp_path / "memory.jsonl")
    rows = _legacy(memory, [12000, 12000])
    memory.migrate_tiers()
    original = MemoryGenerationStore.segment_rows
    fired = False

    # LLM: 同步触发一次真正删除；重入读取不再次触发删除。
    # 函数用途: 在旧根已固定后删除旧分片里的正文。
    def concurrent(store, segment):
        nonlocal fired
        if not fired:
            fired = True
            JsonlMemory(memory.path).remove(rows[0].entry_id)
        return original(store, segment)

    monkeypatch.setattr(MemoryGenerationStore, "segment_rows", concurrent)
    error = None
    try:
        records = memory.all()
    except ValueError as exc:
        error, records = exc, []
    assert error is None, f"根已推进应重试最新代: {error}"
    assert [r.entry_id for r in records] == [rows[1].entry_id]
    monkeypatch.setattr(MemoryGenerationStore, "segment_rows", original)
    store = MemoryGenerationStore(memory.path)
    (tmp_path / store.manifest()["segments"][0]["path"]).write_text("corrupt")
    with pytest.raises(ValueError, match="MEMORY_GENERATION_UNREADABLE"):
        memory.all()


# LLM: 真实发表与GC插入降级reader的旧分片读取点；根未变真损坏必须继续拒绝。
# 函数用途: 验证索引不可用时也遵守换代有限重读合同。
def test_r1_bounded_fallback_retries_concurrent_gc(tmp_path, monkeypatch):
    from agent_py_agent.agent.common import nofollow_fs
    from agent_py_agent.agent.memory_store._jsonl_indexing import bounded_authority_records
    from agent_py_agent.agent.memory_store.generation_manifest import MemoryGenerationStore

    memory = JsonlMemory(tmp_path / "memory.jsonl")
    _legacy(memory, [12000, 12000])
    memory.migrate_tiers()
    reader = nofollow_fs.read_bytes_beneath
    fired = False

    # LLM: 只在旧分片实际打开前发表新代，所有数据仍由产品代码读写。
    # 函数用途: 固定无锁降级读者撞上合法GC的窗口。
    def concurrent(root, parts, **kwargs):
        nonlocal fired
        if not fired and str(parts[-1]).endswith(".jsonl") and "generations" in str(root):
            fired = True
            JsonlMemory(memory.path).add("user", "并发的新事实")
        return reader(root, parts, **kwargs)

    monkeypatch.setattr(nofollow_fs, "read_bytes_beneath", concurrent)
    error = None
    try:
        records, facts = bounded_authority_records(memory, max_records=1000, max_bytes=1024 * 1024)
    except (OSError, ValueError) as exc:
        error, records, facts = exc, [], {}
    assert fired and error is None, f"合法换代应有限重读而非报损坏: {error}"
    assert {r.content for r in records} == {r.content for r in memory.all()}
    assert facts["partial"] is False
    monkeypatch.setattr(nofollow_fs, "read_bytes_beneath", reader)
    store = MemoryGenerationStore(memory.path)
    (tmp_path / store.manifest()["segments"][0]["path"]).write_text("corrupt")
    with pytest.raises(ValueError, match="MEMORY_GENERATION_UNREADABLE"):
        bounded_authority_records(memory, max_records=1000, max_bytes=1024 * 1024)


# LLM: 根已成功而GC失败必须报告本轮正式状态已改变，不允许调用方误认为零副作用失败。
# 函数用途: 验证失败回执和只补GC的幂等恢复。
def test_r1_gc_failure_after_root_commit_reports_changed(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store._jsonl_indexing import RemovalCleanupError
    from agent_py_agent.agent.memory_store.generation_manifest import MemoryGenerationStore

    memory = JsonlMemory(tmp_path / "memory.jsonl")
    _legacy(memory, [12000, 12000])
    memory.migrate_tiers()
    original = MemoryGenerationStore.collect_unreferenced

    # LLM: 仅在提交后的清理叶抛IO错误，不影响根写入及事实读取。
    # 函数用途: 构造正式已提交但历史材料待清理的准确状态。
    def fail_gc(self, root, **kwargs):
        raise OSError("injected post-commit gc failure")

    monkeypatch.setattr(MemoryGenerationStore, "collect_unreferenced", fail_gc)
    failure = None
    try:
        memory.add_record(MemoryRecord("user", "已经正式保存", entry_id="gc-committed"))
    except Exception as exc:
        failure = exc
    assert isinstance(failure, RemovalCleanupError)
    assert failure.formal_memory_changed is True
    assert "gc-committed" in {r.entry_id for r in memory.all()}
    monkeypatch.setattr(MemoryGenerationStore, "collect_unreferenced", original)
    store = MemoryGenerationStore(memory.path)
    committed = store.manifest()
    with memory._locked_memory():
        store.collect_unreferenced(committed)
        store.collect_unreferenced(committed)
    assert store.manifest() == committed
    assert set(store.directory.glob("*/*.jsonl")) == {tmp_path / s["path"] for s in committed["segments"]}


# LLM: 控制文件在预算预检后合法增长也不能漏成裸ValueError，不能返回未核全墓碑的正文。
# 函数用途: 验证读取途中字节超限安全返回空partial。
def test_r1_fallback_control_growth_returns_partial(tmp_path, monkeypatch):
    from agent_py_agent.agent.common import nofollow_fs
    from agent_py_agent.agent.memory_store import generation_manifest
    from agent_py_agent.agent.memory_store._jsonl_indexing import bounded_authority_records
    from agent_py_agent.agent.memory_store.generation_manifest import MemoryGenerationStore

    memory = JsonlMemory(tmp_path / "memory.jsonl")
    memory.add("user", "事实保留")
    memory.migrate_tiers()
    store = MemoryGenerationStore(memory.path)
    reader = nofollow_fs.read_bytes_beneath
    fired = False

    # LLM: 在最终硬限读取前替换为合法但更大的JSON，不制造格式损坏。
    # 函数用途: 固定stat与读之间的正常原子替换窗口。
    def grow(root, parts, **kwargs):
        nonlocal fired
        if not fired and parts[-1] == store.tombstones.path.name:
            fired = True
            payload = json.loads(store.tombstones.path.read_text())
            payload["future"] = "x" * 10000
            store.tombstones.path.write_text(json.dumps(payload))
        return reader(root, parts, **kwargs)

    monkeypatch.setattr(nofollow_fs, "read_bytes_beneath", grow)
    monkeypatch.setattr(generation_manifest, "read_bytes_beneath", grow)
    failure = None
    try:
        records, facts = bounded_authority_records(memory, max_records=100, max_bytes=4000)
    except ValueError as exc:
        failure, records, facts = exc, [], {}
    assert fired and failure is None, f"预算耗尽不能冒充数据错误: {failure}"
    assert records == [] and facts["partial"] is True


# LLM: 墓碑也是权威记录，未验证全账时不得继续召回正文；字节量小也受条数硬限。
# 函数用途: 验证新永久账及旧墓碑都计入同一记录预算。
@pytest.mark.parametrize("legacy", [False, True])
def test_r1_fallback_counts_tombstones_in_record_budget(tmp_path, legacy):
    from agent_py_agent.agent.memory_store._jsonl_indexing import bounded_authority_records
    from agent_py_agent.agent.memory_store.generation_manifest import StorageTombstones
    from agent_py_agent.agent.memory_store.jsonl import _tombstone_path

    memory = JsonlMemory(tmp_path / "memory.jsonl")
    rows = [MemoryRecord("user", "", action="remove", entry_id=f"gone-{i}") for i in range(4)]
    if legacy:
        _tombstone_path(memory.path).write_text("".join(row.to_json() + "\n" for row in rows))
    else:
        ledger = StorageTombstones(memory.path)
        ledger.path.write_text(ledger.prepare(rows)[1])
    records, facts = bounded_authority_records(memory, max_records=2, max_bytes=1024 * 1024)
    assert records == [] and facts["partial"] is True
    assert facts["scanned_records"] == 2


# LLM: 使用真实候选/正式库/恢复服务；只故障根writer，恢复不得再次提交业务remove或增业务版本。
# 函数用途: 验证产品prepare/recover入口能收敛墓碑先行的删除。
def test_r1_prepared_remove_recovery_repairs_root_without_new_delete(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.learning_source import user_dialogue_projection
    from agent_py_agent.agent.memory_store import jsonl
    from agent_py_agent.agent.memory_store.generation_manifest import MemoryGenerationStore
    from agent_py_agent.agent.memory_store.promotion_remove_recovery import (
        prepare_committed_remove,
        recover_prepared_remove,
    )
    from agent_py_agent.tests.memory_confirmation_fixtures import ConfirmedRememberWorkflow
    from agent_py_agent.tests.test_remember_learning_source_review import _agent

    agent = _agent(tmp_path, user_dialogue_projection(), {}, {})
    tool = ConfirmedRememberWorkflow(agent)
    added = tool.execute(dict(content="项目使用 UTC 保存时间", kind="project",
                              origin="user_explicit", subject_key="source.review"))
    assert added.ok and json.loads(added.output)["active_memory_changed"]
    [target] = agent.memory.all()
    agent.memory.migrate_tiers()
    store = MemoryGenerationStore(agent.memory.path)
    before = store.manifest()
    params = agent._current_run_params
    params.request_id, params.run_id = "req-r1-remove", "run-r1-remove"
    agent.conversation_store.messages.append(dict(thread_id=params.task_attributes["conversation_thread_id"],
        role="user", content="删除项目保存时间规则", metadata={"gateway_request_id": params.request_id}))
    writer = jsonl.write_private_text_file_atomic_unlocked

    # LLM: 只阻断最终根清单，候选验证、墓碑落盘均保留真实执行。
    # 函数用途: 让真实工具产生可由恢复入口识别的部分删除。
    def fail_root(path, text, **kwargs):
        if path == store.manifest_path:
            raise OSError("r1 prepared removal root failure")
        return writer(path, text, **kwargs)

    monkeypatch.setattr(jsonl, "write_private_text_file_atomic_unlocked", fail_root)
    failed = tool.execute(dict(action="remove", entry_id=target.entry_id, origin="user_explicit"))
    assert not json.loads(failed.output)["saved"]
    assert store.manifest() == before
    monkeypatch.setattr(jsonl, "write_private_text_file_atomic_unlocked", writer)
    plan = prepare_committed_remove(agent.memory_promotion, target.entry_id)
    assert plan is not None
    tombstone = agent.memory.committed_removal_record(target.entry_id)
    outcome = recover_prepared_remove(agent.memory_promotion, plan)
    assert outcome.promoted and outcome.reason_code == "REMOVE_CLEANUP_RECOVERED", outcome
    assert outcome.formal_memory_changed is False
    assert store.manifest()["operation_id"] != before["operation_id"]
    assert agent.memory.committed_removal_record(target.entry_id) == tombstone
    assert all(not row.content for row in agent.memory_candidates.list())
    assert all(target.content.encode() not in p.read_bytes() for p in store.directory.glob("*/*.jsonl"))
    repaired = store.manifest()
    retry = prepare_committed_remove(agent.memory_promotion, target.entry_id)
    assert recover_prepared_remove(agent.memory_promotion, retry).promoted
    assert store.manifest() == repaired


# LLM: 每次读旧片前都真实换代；有限三次尝试后必须终止，重读不能隐式重置I/O预算。
# 函数用途: 验证完整及降级读取在持续并发发表下不会无限循环。
@pytest.mark.parametrize("bounded", [False, True])
def test_r1_reader_retry_count_is_finite(tmp_path, monkeypatch, bounded):
    from agent_py_agent.agent.common import nofollow_fs
    from agent_py_agent.agent.memory_store._jsonl_indexing import bounded_authority_records
    from agent_py_agent.agent.memory_store.generation_manifest import MemoryGenerationStore

    memory = JsonlMemory(tmp_path / "memory.jsonl")
    memory.add("user", "初始事实")
    memory.migrate_tiers()
    reader = nofollow_fs.read_bytes_beneath if bounded else MemoryGenerationStore.segment_rows
    changing, changes = False, 0

    # LLM: 重入的写者读取不触发新写入，只针对外层读者的三次旧快照制造冲突。
    # 函数用途: 可重复地产生每次尝试均过期的分片坐标。
    def concurrent(root, parts, **kwargs):
        nonlocal changing, changes
        eligible = not bounded or (str(parts[-1]).endswith(".jsonl") and "generations" in str(root))
        if eligible and not changing:
            changing = True
            try:
                JsonlMemory(memory.path).add("user", f"并发事实{changes}")
                changes += 1
            finally:
                changing = False
        return reader(root, parts, **kwargs)

    monkeypatch.setattr(nofollow_fs if bounded else MemoryGenerationStore,
        "read_bytes_beneath" if bounded else "segment_rows", concurrent)
    with pytest.raises(ValueError, match="MEMORY_GENERATION_CHANGED|MEMORY_GENERATION_UNREADABLE"):
        if bounded:
            bounded_authority_records(memory, max_records=1000, max_bytes=1024 * 1024)
        else:
            memory.all()
    assert changes == 3


# LLM: 子进程内运行真实确认/晋升/恢复；启用正数配额，故障只注入最终根writer。
# 函数用途: 同时验证普通Remember删除和墓碑已提交后的prepare/recover不会重入配额锁。
def _r2_quota_remove_worker(root, recovery):
    from agent_py_agent.agent.conversation.learning_source import user_dialogue_projection
    from agent_py_agent.agent.memory_store import jsonl
    from agent_py_agent.agent.memory_store.generation_manifest import MemoryGenerationStore
    from agent_py_agent.agent.memory_store.promotion_remove_recovery import (
        prepare_committed_remove,
        recover_prepared_remove,
    )
    from agent_py_agent.agent.user_space.owner_quota import OwnerQuotaEnforcer
    from agent_py_agent.tests.memory_confirmation_fixtures import ConfirmedRememberWorkflow
    from agent_py_agent.tests.test_remember_learning_source_review import _agent

    agent = _agent(root, user_dialogue_projection(), {}, {})
    tool = ConfirmedRememberWorkflow(agent)
    assert tool.execute(dict(content="项目使用 UTC 保存时间", kind="project", origin="user_explicit",
                             subject_key="source.review")).ok
    [target] = agent.memory.all()
    agent.memory.migrate_tiers()
    store = MemoryGenerationStore(agent.memory.path)
    before = store.manifest()
    agent.memory.quota_enforcer = OwnerQuotaEnforcer(agent.memory.path.parents[2], max_bytes=1048576)
    params = agent._current_run_params
    params.request_id, params.run_id = "req-r2-remove", "run-r2-remove"
    agent.conversation_store.messages.append(dict(thread_id=params.task_attributes["conversation_thread_id"],
        role="user", content="删除项目保存时间规则", metadata={"gateway_request_id": params.request_id}))
    writer = jsonl.write_private_text_file_atomic_unlocked

    # LLM: 不伪造墓碑或候选；失败前其它正式字节均由生产writer提交。
    # 函数用途: 制造可真实恢复的墓碑先行故障。
    def fail_root(path, text, **kwargs):
        if path == store.manifest_path:
            raise OSError("r2 quota removal root failure")
        return writer(path, text, **kwargs)

    if recovery:
        jsonl.write_private_text_file_atomic_unlocked = fail_root
    try:
        outcome = tool.execute(dict(action="remove", entry_id=target.entry_id, origin="user_explicit"))
    finally:
        jsonl.write_private_text_file_atomic_unlocked = writer
    if recovery:
        assert not json.loads(outcome.output)["saved"]
        assert store.manifest() == before
        plan = prepare_committed_remove(agent.memory_promotion, target.entry_id)
        assert plan is not None
        tombstone = agent.memory.committed_removal_record(target.entry_id)
        result = recover_prepared_remove(agent.memory_promotion, plan)
        assert result.promoted and result.reason_code == "REMOVE_CLEANUP_RECOVERED", result
        assert not result.formal_memory_changed
        assert agent.memory.committed_removal_record(target.entry_id) == tombstone
        repaired = store.manifest()
        assert recover_prepared_remove(agent.memory_promotion,
            prepare_committed_remove(agent.memory_promotion, target.entry_id)).promoted
        assert store.manifest() == repaired
    else:
        assert outcome.ok and json.loads(outcome.output)["saved"], outcome
    assert agent.memory.all() == []
    assert all(not row.content for row in agent.memory_candidates.list())
    assert all(target.content.encode() not in p.read_bytes() for p in store.directory.glob("*/*"))


# LLM: 限时独立解释器防止锁缺陷挂住pytest；三秒后取真实栈，十五秒硬超时不当成功。
# 函数用途: 保护正数配额删除回归，同时保留实际同线程锁重入证据。
@pytest.mark.parametrize("recovery", [False, True])
def test_r2_positive_quota_remember_remove_and_prepared_recovery(tmp_path, recovery):
    import subprocess
    import sys

    script = ("import faulthandler; from pathlib import Path; "
              "from agent_py_agent.tests.test_opt_memory_generation import _r2_quota_remove_worker; "
              "faulthandler.dump_traceback_later(3); "
              f"_r2_quota_remove_worker(Path({str(tmp_path)!r}), {recovery!r}); "
              "faulthandler.cancel_dump_traceback_later()")
    try:
        result = subprocess.run([sys.executable, "-B", "-c", script], capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"正数配额真实删除未退出，调用栈：{exc.stderr!r}")
    assert result.returncode == 0, result.stdout + result.stderr


# LLM: 校验被遗忘ID/hash的普通add/replace及混合批次；坏批不动权威、ops或访问前像。
# 函数用途: 防止成功回执描述被末端过滤的写入，以及replace误删原有活记录。
@pytest.mark.parametrize("action", ["add", "replace"])
@pytest.mark.parametrize("mixed", [False, True])
def test_r2_hash_barrier_rejects_before_mutating_live_preimage(tmp_path, action, mixed):
    from agent_py_agent.agent.memory_store.generation_manifest import MemoryGenerationStore

    memory = JsonlMemory(tmp_path / "memory.jsonl")
    memory.add_record(MemoryRecord("user", "永久遗忘的正文", entry_id="forgotten"))
    memory.remove("forgotten")
    if action == "replace":
        memory.add_record(MemoryRecord("user", "必须保留的活前像", entry_id="B"))
    before = {r.entry_id: asdict(r) for r in memory.all()}
    root = MemoryGenerationStore(memory.path).manifest()
    files = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    memory._pending_access["B"] = 123.0
    operations = ([dict(action="add", entry_id="good", content="独立有效事实")] if mixed else [])
    operations.append(dict(action=action, entry_id="B", content=" 永久遗忘的正文 "))
    with pytest.raises(ValueError, match="MEMORY_TOMBSTONED_CONTENT"):
        memory.apply_batch(operations)
    assert {r.entry_id: asdict(r) for r in JsonlMemory(memory.path).all()} == before
    assert MemoryGenerationStore(memory.path).manifest() == root
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == files
    assert memory._pending_access == {"B": 123.0}


# LLM: 同批删除生成的新hash也要约束写入，否则generation过滤仍能制造假成功。
# 函数用途: 禁止混合批次用新ID重加本批明确遗忘的同文。
def test_r2_same_batch_remove_and_readd_cannot_claim_discarded_commit(tmp_path):
    memory = JsonlMemory(tmp_path / "memory.jsonl")
    row = memory.add_record(MemoryRecord("user", "同批遗忘", entry_id="A"))
    with pytest.raises(ValueError, match="MEMORY_TOMBSTONED_CONTENT"):
        memory.apply_batch([dict(action="remove", entry_id="A"),
                            dict(action="add", entry_id="B", content=row.content)])
    assert [r.entry_id for r in memory.all()] == ["A"]
    assert memory.committed_removal_record("A") is None


# LLM: 首次小库删除只故障根writer；重开后连做两次真实purge，身份和版本必须不变。
# 函数用途: 抓住无root时假清理成功及旧单文件明文残留。
def test_r2_first_small_remove_root_failure_restart_purge_converges(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import jsonl
    from agent_py_agent.agent.memory_store._jsonl_indexing import RemovalCleanupError
    from agent_py_agent.agent.memory_store.generation_manifest import (
        GENERATION_SCHEMA,
        MemoryGenerationStore,
    )

    memory = JsonlMemory(tmp_path / "memory.jsonl")
    row = memory.add("user", "PRIVATE-R2-FIRST-DELETE")
    store = MemoryGenerationStore(memory.path)
    assert store.manifest() is None
    writer = jsonl.write_private_text_file_atomic_unlocked

    # LLM: 永久账先行仍真实写入，根失败不能洗成零副作用。
    # 函数用途: 中断首次根发表。
    def fail_root(path, raw, **kwargs):
        if path == store.manifest_path:
            raise OSError("r2 first root failure")
        return writer(path, raw, **kwargs)

    monkeypatch.setattr(jsonl, "write_private_text_file_atomic_unlocked", fail_root)
    with pytest.raises(RemovalCleanupError) as error:
        memory.remove(row.entry_id)
    assert error.value.formal_memory_changed
    monkeypatch.setattr(jsonl, "write_private_text_file_atomic_unlocked", writer)
    memory = JsonlMemory(memory.path)
    tombstone = memory.committed_removal_record(row.entry_id)
    ledger = store.tombstones.path.read_bytes()
    assert memory.all() == [] and tombstone is not None
    memory.purge_removed_indexes(row.entry_id)
    assert store.manifest() is not None
    assert json.loads(memory.path.read_text())["schema_version"] == GENERATION_SCHEMA
    repaired = store.manifest()
    memory.purge_removed_indexes(row.entry_id)
    assert store.manifest() == repaired
    assert store.tombstones.path.read_bytes() == ledger
    assert memory.committed_removal_record(row.entry_id) == tombstone
    assert all(row.content.encode() not in p.read_bytes() for p in tmp_path.rglob("*") if p.is_file())


# LLM: 子进程在真实私有writer已fsync而未replace时退出；不能手造暂存冒充进程中断证据。
# 函数用途: 验证重跑迁移和正式remove都回收实际.tmp分片与空代目录。
@pytest.mark.parametrize("resume", ["migrate", "remove"])
def test_r2_writer_interruption_temporary_shard_is_physically_collected(tmp_path, resume):
    import subprocess
    import sys

    from agent_py_agent.agent.memory_store.generation_manifest import MemoryGenerationStore

    memory = JsonlMemory(tmp_path / "memory.jsonl")
    row = memory.add("user", "PRIVATE-R2-INTERRUPTED-SHARD")
    script = f'''
import os
from pathlib import Path
from agent_py_agent.agent.common import json_io
from agent_py_agent.agent.memory_store.jsonl import JsonlMemory
original = json_io._replace_with_retry
def interrupted(src, dst, **kwargs):
    if ".generations" in str(dst) and Path(dst).suffix == ".jsonl":
        os._exit(73)
    return original(src, dst, **kwargs)
json_io._replace_with_retry = interrupted
JsonlMemory(Path({str(memory.path)!r})).migrate_tiers()
'''
    process = subprocess.run([sys.executable, "-B", "-c", script], capture_output=True, text=True, timeout=15)
    assert process.returncode == 73, process.stderr
    store = MemoryGenerationStore(memory.path)
    [temporary] = list(store.directory.glob("*/*.tmp"))
    assert row.content.encode() in temporary.read_bytes()
    directory = temporary.parent
    assert store.manifest() is None
    if resume == "migrate":
        memory.migrate_tiers()
        assert [r.content for r in memory.all()] == [row.content]
    else:
        memory.remove(row.entry_id)
        assert memory.all() == []
    assert not temporary.exists(), "真实writer的中断暂存仍残留"
    assert not directory.exists(), "已清空失败代目录未回收"
    if resume == "migrate":
        memory.remove(row.entry_id)
    assert all(row.content.encode() not in p.read_bytes() for p in tmp_path.rglob("*") if p.is_file())


# LLM: GC只处理生产writer精确命名；真根引用及未知名字不得被误删，不安全链接必须报错。
# 函数用途: 验证暂存回收的命名、nofollow和单链接边界。
@pytest.mark.parametrize("shape", ["regular", "symlink", "hardlink"])
def test_r2_temporary_gc_keeps_current_and_unknown_and_rejects_links(tmp_path, monkeypatch, shape):
    import os

    from agent_py_agent.agent.memory_store.generation_manifest import MemoryGenerationStore

    memory = JsonlMemory(tmp_path / "memory.jsonl")
    memory.add("user", "current authority")
    memory.migrate_tiers()
    store = MemoryGenerationStore(memory.path)
    root = store.manifest()
    current = tmp_path / root["segments"][0]["path"]
    before = current.read_bytes()
    unknown = current.parent / ".active-000000.jsonl.not-a-uuid.tmp"
    unknown.write_text("unknown retain")
    temporary = current.parent / (".active-000000.jsonl." + "b" * 32 + ".tmp")
    if shape == "regular":
        temporary.write_text("staging discard")
    elif shape == "symlink":
        temporary.symlink_to(current)
    else:
        temporary.write_text("multiply-linked staging")
        original_stat = os.stat

        # LLM: 本机真实硬链接创建返回EPERM；仅注入该暂存叶的多链接元数据，不冒称物理硬链接实测。
        # 函数用途: 验证GC必须拒绝nlink=2，所有其它stat仍读取真实文件系统。
        def multiple_links(path, *args, **kwargs):
            info = original_stat(path, *args, **kwargs)
            if path == temporary.name and kwargs.get("dir_fd") is not None:
                fields = list(info)
                fields[3] = 2
                return os.stat_result(fields)
            return info

        # 包装函数保留原stat的真实dir_fd能力，不能让能力探测提前挡掉本例的nlink断言。
        monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {multiple_links})
        monkeypatch.setattr(os, "stat", multiple_links)
    with memory._locked_memory():
        if shape == "regular":
            store.collect_unreferenced(root)
            assert not temporary.exists()
        else:
            with pytest.raises(ValueError, match="MEMORY_GENERATION_UNREADABLE"):
                store.collect_unreferenced(root)
            assert temporary.is_symlink() or temporary.exists()
    assert current.read_bytes() == before
    assert unknown.read_text() == "unknown retain"
    assert store.manifest() == root


# LLM: 仅故障root之后的GC或marker写；移除故障后重试只能补清理，不能重信legacy或增代。
# 函数用途: 验证迁移真正完成的判据和收尾失败继续诚实报错。
@pytest.mark.parametrize("failure_point", ["gc", "marker"])
def test_r2_migration_retries_post_root_cleanup_without_new_generation(tmp_path, monkeypatch, failure_point):
    from agent_py_agent.agent.memory_store import jsonl
    from agent_py_agent.agent.memory_store._jsonl_indexing import RemovalCleanupError
    from agent_py_agent.agent.memory_store.generation_manifest import (
        GENERATION_SCHEMA,
        MemoryGenerationStore,
    )

    memory = JsonlMemory(tmp_path / "memory.jsonl")
    rows = _legacy(memory, [100, 100])
    store = MemoryGenerationStore(memory.path)
    orphan_dir = store.directory / ("a" * 32)
    orphan_dir.mkdir(parents=True)
    orphan = orphan_dir / "active-000000.jsonl"
    orphan.write_text(rows[0].to_json() + "\n")
    gc, writer = MemoryGenerationStore.collect_unreferenced, jsonl.write_private_text_file_atomic_unlocked

    # LLM: 错误只发生于提交后的清理，不替换发表或逻辑读取。
    # 函数用途: 固定GC失败边界。
    def fail_gc(self, root, **kwargs):
        raise OSError("r2 post-root gc failure")

    # LLM: 分片/墓碑/根真实写入，仅拒绝旧单文件marker的最后写叶。
    # 函数用途: 固定迁移已发表但旧正文未擦除的边界。
    def fail_marker(path, raw, **kwargs):
        if path == memory.path:
            raise OSError("r2 post-root marker failure")
        return writer(path, raw, **kwargs)

    if failure_point == "gc":
        monkeypatch.setattr(MemoryGenerationStore, "collect_unreferenced", fail_gc)
    else:
        monkeypatch.setattr(jsonl, "write_private_text_file_atomic_unlocked", fail_marker)
    with pytest.raises(RemovalCleanupError) as error:
        memory.migrate_tiers()
    assert error.value.formal_memory_changed
    root, ledger = store.manifest(), store.tombstones.path.read_bytes()
    assert root is not None
    memory = JsonlMemory(memory.path)
    with pytest.raises(RemovalCleanupError) as retry_error:
        memory.migrate_tiers()
    assert not retry_error.value.formal_memory_changed
    assert store.manifest() == root
    monkeypatch.setattr(MemoryGenerationStore, "collect_unreferenced", gc)
    monkeypatch.setattr(jsonl, "write_private_text_file_atomic_unlocked", writer)
    # 已有根后即使legacy不是合法事件，也不能重新信任它来生成事实。
    memory.path.write_text("obsolete plaintext is not authority\n")
    for _ in range(2):
        assert memory.migrate_tiers() == root
        assert store.manifest() == root
        assert json.loads(memory.path.read_text())["schema_version"] == GENERATION_SCHEMA
        assert not orphan.exists() and not orphan_dir.exists()
        assert {r.entry_id: r.content for r in memory.all()} == {r.entry_id: r.content for r in rows}
        assert store.tombstones.path.read_bytes() == ledger


# LLM: 公共search含语义臂的零命中也只消耗同一次有界权威扫描；本地hash嵌入无网络，观察真实扫描。
# 函数用途: 防止report有界但公共调用为了判空再执行all全库读取。
@pytest.mark.parametrize("semantic,indexed", [(False, False), (True, False), (True, True)])
def test_r2_public_search_empty_hits_respects_authority_scan_budget(tmp_path, monkeypatch, semantic, indexed):
    from agent_py_agent.agent.memory_store import _jsonl_indexing, jsonl
    from agent_py_agent.tests._hashing_embedder import LocalHashingEmbedder

    memory = JsonlMemory(tmp_path / "memory.jsonl", embedder=LocalHashingEmbedder(dim=64) if semantic else None)
    if semantic:
        # 通过真实写入口创建合法向量身份；其旧候选不属于下面的正式样本，仍必须经权威复核。
        memory.add("user", "synthetic vector identity")
        assert memory._vector_store().identity_status()[0]
    _legacy(memory, [30] * 1005)
    memory.migrate_tiers()
    if indexed:
        assert memory.rebuild_read_index(batch_size=1000)["status"] == "partial"
        assert memory.rebuild_read_index(batch_size=1000)["status"] == "complete"
    monkeypatch.setattr(jsonl, "_AUTHORITY_SCAN_MAX_RECORDS", 3)
    scan = _jsonl_indexing.bounded_authority_records
    all_records = memory.all
    scans, all_counts = [], []

    # LLM: 只记录生产扫描回执，实际字节与解析仍由真实reader执行。
    # 函数用途: 证明总扫描次数和预算实际消耗。
    def observe_scan(*args, **kwargs):
        records, facts = scan(*args, **kwargs)
        scans.append(facts)
        return records, facts

    # LLM: 不伪造all结果，错误全库读取会留下准确记录数。
    # 函数用途: 捕获公共零命中分支的无界旁路。
    def observe_all():
        records = all_records()
        all_counts.append(len(records))
        return records

    monkeypatch.setattr(_jsonl_indexing, "bounded_authority_records", observe_scan)
    monkeypatch.setattr(memory, "all", observe_all)
    assert memory.search("火星葡萄") == []
    assert all_counts == [], f"公共入口额外全库读取：{all_counts}"
    assert len(scans) == 1 and scans[0]["partial"]
    assert scans[0]["scanned_records"] == 3


# LLM: 使用真实add/replace历史和仅根writer失败的remove；双次重启purge核全文、元数据、hash和永久账身份。
# 函数用途: 防止无根恢复将多个活版本当快照发表，或为同一删除重写永久账与增加代次。
def test_r3_rootless_purge_compacts_survivor_history(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import jsonl
    from agent_py_agent.agent.memory_store._jsonl_indexing import RemovalCleanupError
    from agent_py_agent.agent.memory_store.generation_manifest import MemoryGenerationStore

    memory = JsonlMemory(tmp_path / "memory.jsonl")
    deleted = memory.add_record(MemoryRecord("user", "PRIVATE-R3-DELETE-A", entry_id="A"))
    first = memory.add_record(MemoryRecord("user", "R3-LIVE-B-v1", entry_id="B",
        tags=["保留标签"], attributes={"origin": "user_explicit", "extra": {"keep": True}},
        _extra_fields={"future_field": ["保留扩展"]}))
    memory.apply_batch([dict(action="replace", entry_id="B", expected_version=first.version,
        content="R3-LIVE-B-v2", source="test:r3-replace")])
    expected = asdict(next(row for row in memory.all() if row.entry_id == "B"))
    assert expected["version"] == 2
    assert jsonl.memory_content_hash(first.content) in expected["removed_content_hashes"]
    store = MemoryGenerationStore(memory.path)
    assert store.manifest() is None
    writer = jsonl.write_private_text_file_atomic_unlocked

    # LLM: 分片与永久账仍真实落盘，只中断首次根发表，观察正式删除的部分提交。
    # 函数用途: 留下必须从legacy历史恢复的合法故障前像。
    def fail_root(path, raw, **kwargs):
        if path == store.manifest_path:
            raise OSError("r3 root writer failure")
        return writer(path, raw, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(jsonl, "write_private_text_file_atomic_unlocked", fail_root)
        with pytest.raises(RemovalCleanupError) as error:
            memory.remove("A", source="test:r3-forget")
        assert error.value.formal_memory_changed
    assert store.manifest() is None
    ledger = store.tombstones.path.read_bytes()
    ledger_stat = store.tombstones.path.stat()
    tombstone = asdict(JsonlMemory(memory.path).committed_removal_record("A"))
    repaired = None
    for _ in range(2):
        reopened = JsonlMemory(memory.path)
        assert [asdict(row) for row in reopened.all()] == [expected]
        reopened.purge_removed_indexes("A")
        root = store.manifest()
        assert root is not None
        if repaired is None:
            repaired = root
        assert root == repaired and root["generation"] == 1
        assert [asdict(row) for row in JsonlMemory(memory.path).all()] == [expected]
        assert [row.entry_id for row in store.events(root) if row.action != "remove"] == ["B"]
        assert store.tombstones.path.read_bytes() == ledger
        stamp = store.tombstones.path.stat()
        assert (stamp.st_dev, stamp.st_ino) == (ledger_stat.st_dev, ledger_stat.st_ino)
        assert asdict(reopened.committed_removal_record("A")) == tombstone
        assert all(deleted.content.encode() not in path.read_bytes()
                   for path in tmp_path.rglob("*") if path.is_file())


# LLM: 真实发表后仅令权威校验失败；此时根已提交但legacy原文必须仍在，不伪造读回成功。
# 函数用途: 保证collect=False的首次发表也在覆盖marker之前校验正式根。
def test_r3_first_publish_validates_authority_before_legacy_marker(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store._jsonl_indexing import RemovalCleanupError
    from agent_py_agent.agent.memory_store.generation_manifest import MemoryGenerationStore

    memory = JsonlMemory(tmp_path / "memory.jsonl")
    memory.add("user", "R3-KEEP-LEGACY-UNTIL-VALIDATED")
    before = memory.path.read_bytes()
    events = MemoryGenerationStore.events

    # LLM: 仍允许prepare读取无根旧库，只拒绝已经发表的根，隔离marker前的必要验证点。
    # 函数用途: 模拟新根不可读且保留可恢复的legacy原文。
    def reject_published(self, root=None, **kwargs):
        if self.manifest() is not None:
            raise ValueError("MEMORY_GENERATION_UNREADABLE")
        return events(self, root, **kwargs)

    monkeypatch.setattr(MemoryGenerationStore, "events", reject_published)
    with pytest.raises(RemovalCleanupError) as error:
        memory.migrate_tiers()
    assert error.value.formal_memory_changed
    assert memory.path.read_bytes() == before


# LLM: 向量命中由真实VectorStore产生；交错真实写入及墓碑超预算，公开search不得回传旧正文或记旧访问。
# 函数用途: 覆盖legacy/generation与词面融合两路，同时约束漂移重读有界且没有all旁路。
@pytest.mark.parametrize("action", ["remove", "replace", "remove_root_failure"])
@pytest.mark.parametrize("migrated,lexical,limited", [
    (False, False, False), (True, False, False), (True, True, False), (True, False, True)])
def test_r3_public_search_revalidates_after_vector_hits(tmp_path, monkeypatch, action, migrated, lexical, limited):
    from agent_py_agent.agent.memory_store import _jsonl_indexing, jsonl
    from agent_py_agent.agent.retrieval.vector_store import VectorStore

    # LLM: 固定二维本地向量以获得真实命中，不访问远程嵌入或伪造VectorHit。
    # 类用途: 让确定性交错测试只改变权威事实而非相似度。
    class Embedder:
        # LLM: 无网络与磁盘副作用，所有输入共享一个合法向量空间。
        # 函数用途: 为写入和查询提供可重复的单位向量。
        def embed(self, texts):
            return [[1.0, 0.0] for _ in texts]

    memory = JsonlMemory(tmp_path / "memory.jsonl", embedder=Embedder())
    original = memory.add_record(MemoryRecord("user", "target old apple orchard", entry_id="target"))
    if limited:
        for index in range(4):
            memory.add_record(MemoryRecord("user", f"budget filler {index}", entry_id=f"filler-{index}"))
        monkeypatch.setattr(jsonl, "_AUTHORITY_SCAN_MAX_RECORDS", 3)
    if migrated:
        memory.migrate_tiers()
        assert memory.rebuild_read_index()["status"] == "complete"
    query = "apple" if lexical else "火星葡萄"
    if not lexical:
        assert memory.search_report(query)[0] == []
    store = memory._vector_store()
    search, scan = VectorStore.search, _jsonl_indexing.bounded_authority_records
    scans, observed_hits = [], []
    writer = JsonlMemory(memory.path)
    atomic_write = jsonl.write_private_text_file_atomic_unlocked

    # LLM: 只拒绝manifest写，永久账真实先行；用来验证根指纹未变也不能漏掉永久屏障漂移。
    # 函数用途: 制造已生效删除但根未发表的合法部分提交。
    def fail_manifest(path, raw, **kwargs):
        if path.name == memory.path.name + ".manifest.json":
            raise OSError("r3 semantic removal root failure")
        return atomic_write(path, raw, **kwargs)

    # LLM: 不替换真实检索结果；仅在其返回与产品核验之间执行同owner合法正式写入。
    # 函数用途: 精确重现语义快照在查询期间失效的窗口。
    def interleave(self, vector, **kwargs):
        hits = search(self, vector, **kwargs)
        if self is store:
            assert any(hit.metadata.get("entry_id") == "target" for hit in hits)
            observed_hits.extend(hits)
            if limited:
                # 永久账新增四个真实墓碑，严格超过同一个三条预算；屏障未核全必须拒绝一切旧候选。
                writer.apply_batch([dict(action="remove", entry_id=f"filler-{index}") for index in range(4)])
            if action == "remove_root_failure":
                from agent_py_agent.agent.memory_store._jsonl_indexing import RemovalCleanupError

                with monkeypatch.context() as fault:
                    fault.setattr(jsonl, "write_private_text_file_atomic_unlocked", fail_manifest)
                    with pytest.raises(RemovalCleanupError) as error:
                        writer.remove("target")
                    assert error.value.formal_memory_changed
            elif action == "remove":
                writer.remove("target")
            else:
                writer.apply_batch([dict(action="replace", entry_id="target", expected_version=original.version,
                    content="target fresh apple harvest")])
        return hits

    # LLM: 只观察实际reader回执，不伪造扫描结果；每次读取须遵守原条数和字节硬限。
    # 函数用途: 检查一次初始扫描及最多一次漂移重读。
    def observe_scan(*args, **kwargs):
        records, facts = scan(*args, **kwargs)
        scans.append(facts)
        return records, facts

    # LLM: 公共search不允许通过all取得无限快照；写者为独立实例，不受此断言影响。
    # 函数用途: 将无界语义复核旁路变为明确失败。
    def forbid_all():
        pytest.fail("公共search不能恢复无界all")

    monkeypatch.setattr(VectorStore, "search", interleave)
    monkeypatch.setattr(_jsonl_indexing, "bounded_authority_records", observe_scan)
    monkeypatch.setattr(memory, "all", forbid_all)
    result = memory.search(query)
    assert observed_hits
    if action != "replace" or limited:
        assert result == []
        assert "target" not in memory._pending_access
    else:
        [current] = writer.all()
        assert [asdict(row) for row in result] == [asdict(current)]
        assert current.content != original.content and current.version == original.version + 1
    assert len(scans) == 2
    if limited:
        assert scans[-1]["partial"] and scans[-1]["scanned_records"] == 3
    assert all(facts["scanned_records"] <= jsonl._AUTHORITY_SCAN_MAX_RECORDS
               and facts["scanned_bytes"] <= jsonl._AUTHORITY_SCAN_MAX_BYTES for facts in scans)
