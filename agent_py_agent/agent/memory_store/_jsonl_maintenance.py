"""仅维护路径使用的正式事件语义校验与读取证据；普通召回兼容读不受影响。"""
# LLM: 严格维护共用权威读取证据；quota→policy→formal内恢复首次根，并幂等接续GC/旧文件marker收尾。
# 模块用途: 校验维护源、迁移冷热代次及擦除遗忘副本，失败保留清理责任。
import json
import math
from copy import deepcopy
from dataclasses import dataclass


# LLM: 此快照只在一次维护调用内传递，不缓存到仓库实例；空records不代表源缺失。
# 类用途: 同时保存物化记录和本次读取确实打开过源文件的证据。
@dataclass(frozen=True)
class MaintenanceSnapshot:
    records: list
    source_opened: bool


# LLM: 不做exists预采样；复用公共LF读取边界，成功读空文件仍返回True。调用方负责权威锁。
# 函数用途: 严格读取全部JSON对象，并区分首次缺库与曾见源后的丢源。
def read_maintenance_objects(path, *, require_source=False):
    from ..common import json_io

    try:
        lines = json_io.read_jsonl_text_lines(path)
    except FileNotFoundError as exc:
        if require_source:
            raise ValueError('MEMORY_AUTHORITY_UNREADABLE') from exc
        return [], False
    records = []
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError('MEMORY_AUTHORITY_UNREADABLE') from exc
        if not isinstance(record, dict):
            raise ValueError('MEMORY_AUTHORITY_UNREADABLE')
        records.append(record)
    return records, True


# LLM: 正式add/replace拒绝空白正文；legacy v1 add兼容缺ID及完整重算一致的派生ID，不能仅按前缀放行。
# 函数用途: 阻止语义坏行覆盖旧投影；旧行省略元数据仍沿MemoryRecord默认值读取。
def require_maintenance_event(record):
    strings = ('role', 'content', 'kind', 'entry_id', 'action', 'source')
    if record is None or any(not isinstance(getattr(record, key), str) for key in strings):
        raise ValueError('MEMORY_AUTHORITY_UNREADABLE')
    action = record.action.strip().lower()
    if action not in {'add', 'replace', 'remove', 'touch'}:
        raise ValueError('MEMORY_AUTHORITY_UNREADABLE')
    if type(record.version) is not int or record.version < 1:
        raise ValueError('MEMORY_AUTHORITY_UNREADABLE')
    if record.attributes is not None and not isinstance(record.attributes, dict):
        raise ValueError('MEMORY_AUTHORITY_UNREADABLE')
    if record.tags is not None and (not isinstance(record.tags, list)
                                   or any(not isinstance(tag, str) for tag in record.tags)):
        raise ValueError('MEMORY_AUTHORITY_UNREADABLE')
    for key in ('created_at', 'updated_at', 'expires_at', 'last_accessed_at'):
        value = getattr(record, key)
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
            raise ValueError('MEMORY_AUTHORITY_UNREADABLE')
    # 与写入合同一致：负过期时间是坏事件，不能当成已经过期而清理。
    if record.expires_at is not None and record.expires_at < 0:
        raise ValueError('MEMORY_AUTHORITY_UNREADABLE')
    if action in {'remove', 'touch', 'replace'} and not record.entry_id.strip():
        raise ValueError('MEMORY_AUTHORITY_UNREADABLE')
    # 正式删除/访问信号不携带正文；不能将损坏的add静默解释成删除或孤儿。
    if action in {'remove', 'touch'} and record.content:
        raise ValueError('MEMORY_AUTHORITY_UNREADABLE')
    if action in {'add', 'replace'} and not record.content.strip():
        from .jsonl import _legacy_entry_id

        # 正常写入会持久化派生ID；只有原始v1 add及精确重算一致的旧身份兼容，绝不只看前缀。
        legacy = action == 'add' and record.version == 1 and (
            not record.entry_id or record.entry_id == _legacy_entry_id(record))
        if not legacy:
            raise ValueError('MEMORY_AUTHORITY_UNREADABLE')


# LLM: 仅标记破坏性操作前的权威读取失败；不把reset/SQLite写入故障混为零副作用失败。
# 类用途: 让维护CLI准确报告“重置尚未发生”。
class AuthorityPreflightError(ValueError):
    pass


# LLM: reset先预检逻辑权威；旧格式才复核原文件存活，已迁移库以根/分片/永久账为准，不触碰workspace。
# 函数用途: 在权威校验后清空本库派生索引，再按有界维护入口追平。
def reset_local_index(memory, *, require_source=False, rebuild_memory=True):
    from .generation_manifest import MemoryGenerationStore
    from .read_index import MemoryReadIndex

    with memory._locked_memory():
        try:
            snapshot = memory._maintenance_snapshot(require_source=require_source)
            if (require_source or snapshot.source_opened) and MemoryGenerationStore(memory.path).manifest() is None:
                memory.path.stat()
        except (OSError, ValueError, TypeError) as exc:
            raise AuthorityPreflightError('MEMORY_AUTHORITY_UNREADABLE') from exc
        MemoryReadIndex(memory.path, coordinator_io=memory.coordinator_io).invalidate()
    return memory.index_all(require_source=require_source) if rebuild_memory else 0


# LLM: quota→policy→formal锁序；已有根只补GC/marker，不重信legacy或增代，显式retier才重分层。
# 函数用途: 将旧正式库按字符一次性分层，旧archive只隔离保存不猜可见性。
def migrate_tiers(memory, *, dry_run=False, retier=False):
    from ..common.nofollow_fs import read_text_beneath
    from .generation_manifest import MemoryGenerationStore, partition_records
    from .jsonl import (
        _managed_policy_scope,
        _materialize_memory_events,
        _quota_admission,
        _strict_memory_events,
    )

    store = MemoryGenerationStore(memory.path)

    # LLM: 只从当前完整前像生成预演，不写备份或索引；正式执行在原锁内重读。
    # 函数用途: 计算同一份迁移结果及隔离材料。
    def prepare():
        root = store.manifest()
        if root and not retier:
            return root, None, None
        events = store.events(root) if root else _strict_memory_events(memory.path)
        live = _materialize_memory_events(events, include_expired=True)
        live_ids = {record.entry_id for record in live}
        tombstones = {record.entry_id: record for record in events
                      if record.action == "remove" and record.entry_id not in live_ids}
        budget = memory.memory_policy().active_library_max_chars * 4 // 5
        budget = max(1, budget)
        from ..user_space.managed_user_governance import managed_locked_memory_ids

        pinned = managed_locked_memory_ids(memory.path)
        active, cold = partition_records(live, budget, pinned_ids=pinned)
        archive_name = memory.path.name + ".archive.jsonl"
        quarantine = read_text_beneath(memory.path.parent, (archive_name,))
        if quarantine:
            for line in quarantine.split("\n"):
                if line.strip():
                    payload = json.loads(line)
                    if not isinstance(payload, dict):
                        raise ValueError("MEMORY_AUTHORITY_UNREADABLE")
        preview = {"generation": 0, "total_chars": sum(len(r.content) for r in live),
                   "active_chars": sum(len(r.content) for r in active),
                   "cold_chars": sum(len(r.content) for r in cold), "total_records": len(live),
                   "legacy_visibility_unknown": sum(bool(line.strip()) for line in (quarantine or "").split("\n"))}
        return preview, [*live, *tombstones.values()], (budget, quarantine)

    if dry_run:
        return prepare()[0]
    root = memory.path.parents[2] if len(memory.path.parents) >= 3 else memory.path.parent
    with _quota_admission(memory.quota_enforcer) as admission, _managed_policy_scope(root):
        with memory._locked_memory():
            preview, records, options = prepare()
            if records is None:
                return finish_tier_generation(memory, preview, admission=admission)
            result = publish_tier_generation(memory, records, admission=admission,
                max_chars=options[0], quarantine=options[1])
            return {**preview, **result}


# LLM: 新分片/墓碑/根复用原writer与C末门；根发表后继续marker收尾，错误准确报告本轮已提交。
# 函数用途: 提交冷热代次，并将旧单文件降为阻止旧写者混写的schema标记。
def publish_tier_generation(memory, records, *, admission=None, commit=None, redact_contents=None,
                            max_chars=None, quarantine=None):
    from ..user_space.managed_user_governance import managed_locked_memory_ids
    from . import jsonl
    from .generation_manifest import MemoryGenerationStore

    if commit is None and memory.coordinator_io:
        commit = jsonl._coordinator_memory_write

    # LLM: 仅固定单文件落盘操作进入原末门，编码、配额和目录扫描保持门外。
    # 函数用途: 为同代文件复用唯一私有原子写入口及测试故障注入点。
    def write(path, raw):
        if commit is None:
            jsonl.write_private_text_file_atomic_unlocked(path, raw)
        else:
            commit(lambda: jsonl.write_private_text_file_atomic_unlocked(path, raw, replace_once=True))

    store = MemoryGenerationStore(memory.path)
    plan = store.prepare(records, max_chars=max_chars or memory.memory_policy().active_library_max_chars,
                         pinned_ids=managed_locked_memory_ids(memory.path), quarantine=quarantine)
    result = store.publish(plan, write=write, admission=admission, commit=commit,
        redact=(lambda: memory._redact_local_tool_ledgers(redact_contents)) if redact_contents else None)
    return finish_tier_generation(memory, result, admission=admission, collect=False,
                                  formal_memory_changed=True, commit=commit)


# LLM: 调用方持quota→policy→formal；仅核当前根并补GC/marker，不生成代次或业务墓碑，native错误原样透传。
# 函数用途: 在首次发表或重启后完成同一根的收尾，重复失败不能假报迁移成功。
def finish_tier_generation(memory, root, *, admission=None, collect=True, formal_memory_changed=False, commit=None):
    from ..common.nofollow_fs import read_text_beneath
    from ..user_space.owner_quota import OwnerQuotaChange
    from . import jsonl
    from ._jsonl_indexing import RemovalCleanupError
    from .generation_manifest import GENERATION_SCHEMA, MemoryGenerationStore

    if commit is None and memory.coordinator_io:
        commit = jsonl._coordinator_memory_write
    store = MemoryGenerationStore(memory.path)
    try:
        if store.manifest() != root:
            raise ValueError("MEMORY_GENERATION_CONFLICT")
        if collect:
            # 清除旧正文前必须确认已发表权威完整，不能将损坏的新根当作擦除旧文件的许可。
            store.events(root)
            store.collect_unreferenced(root, commit=commit)
        marker = json.dumps({"schema_version": GENERATION_SCHEMA, "manifest": store.manifest_path.name}) + "\n"
        if read_text_beneath(memory.path.parent, (memory.path.name,)) != marker:
            if admission is not None:
                admission.check([OwnerQuotaChange(memory.path, len(marker.encode("utf-8")))])
            if commit is None:
                jsonl.write_private_text_file_atomic_unlocked(memory.path, marker)
            else:
                commit(lambda: jsonl.write_private_text_file_atomic_unlocked(memory.path, marker, replace_once=True))
    except Exception as exc:
        if isinstance(exc, jsonl._NATIVE_MEMORY_ERRORS):
            raise
        raise RemovalCleanupError("MEMORY_GENERATION_COMMITTED_CLEANUP_PENDING",
                                  formal_memory_changed=formal_memory_changed) from exc
    return root


# LLM: 恢复只能增加未遗忘事实，不能用旧版本覆盖当前记录；所有输入先严格验证再发表，墓碑不来自备份。
# 函数用途: 在当前库与删除屏障上叠加已授权的备份事实。
def restore_tier_records(memory, records):
    from .generation_manifest import MemoryGenerationStore
    from .jsonl import (
        _managed_policy_scope,
        _materialize_memory_events,
        _quota_admission,
        _record_payload,
        _strict_memory_events,
        _with_legacy_identity,
        memory_content_hash,
    )

    incoming = deepcopy(list(records))
    for record in incoming:
        require_maintenance_event(record)
        if record.action not in {"add", "replace"}:
            raise ValueError("MEMORY_BACKUP_RECORD_INVALID")
    incoming = [_with_legacy_identity(record) for record in incoming]
    owner = memory.path.parents[2] if len(memory.path.parents) >= 3 else memory.path.parent
    with _quota_admission(memory.quota_enforcer) as admission, _managed_policy_scope(owner):
        with memory._locked_memory():
            events = _strict_memory_events(memory.path)
            current = {r.entry_id: r for r in _materialize_memory_events(events, include_expired=True)}
            tombstones = {r.entry_id: r for r in events if r.action == "remove"}
            barrier = MemoryGenerationStore(memory.path).tombstones
            tombstones.update(barrier.load())
            for record in incoming:
                if barrier.excludes(record.entry_id, memory_content_hash(record.content), records=tombstones):
                    continue
                existing = current.get(record.entry_id)
                if existing is not None:
                    before, after = _record_payload(record), _record_payload(existing)
                    for payload in (before, after):
                        payload.pop("last_accessed_at", None)
                        payload.pop("removed_content_hashes", None)
                    if record.version == existing.version and before != after:
                        raise ValueError("MEMORY_BACKUP_CONFLICT")
                    continue
                current[record.entry_id] = record
            return publish_tier_generation(memory, [*current.values(), *tombstones.values()], admission=admission)


# LLM: quota→policy→formal锁内修复墓碑先行的无根/旧根；外层传入仍持有的准入，绝不重取quota锁。
# 函数用途: 清除内部历史分片中的已删正文，并丢弃含旧正文的派生SQLite。
def purge_tier_copies(memory, *, admission=None):
    from ..common.nofollow_fs import NoFollowPathError, read_text_beneath
    from . import jsonl
    from .generation_manifest import MemoryGenerationStore
    from .read_index import MemoryReadIndex

    store = MemoryGenerationStore(memory.path)
    owner = memory.path.parents[2] if len(memory.path.parents) >= 3 else memory.path.parent
    with jsonl._quota_admission(memory.quota_enforcer, admission) as admission, jsonl._managed_policy_scope(owner), memory._locked_memory():
        root = store.manifest()
        blocked = store.tombstones.load()
        if not blocked:
            return
        if root is None:
            events = jsonl._strict_memory_events(memory.path, require_source=True)
            publish_tier_generation(memory, events, admission=admission)
            root = store.manifest()
        elif any(store.tombstones.excludes(str(row.get("entry_id") or ""), jsonl.memory_content_hash(str(row.get("content") or "")), records=blocked)
               for segment in root["segments"] for row in store.segment_rows(segment)):
            publish_tier_generation(memory, store.events(root), admission=admission)
            root = store.manifest()
        else:
            finish_tier_generation(memory, root, admission=admission)
        MemoryReadIndex(memory.path, coordinator_io=memory.coordinator_io).invalidate()
        current = {item["path"] for item in root["segments"]}
        files = [memory.path.with_name(memory.path.name + ".archive.jsonl")]
        try:
            directories = list(store.directory.iterdir())
        except FileNotFoundError:
            directories = []
        for directory in directories:
            if directory.is_symlink():
                raise NoFollowPathError("invalid memory generation directory")
            if directory.is_dir():
                files.extend(directory.glob("*.jsonl"))
        for path in files:
            relative = path.relative_to(memory.path.parent)
            raw = read_text_beneath(memory.path.parent, tuple(relative.parts))
            if raw is None:
                continue
            rows = [json.loads(line) for line in raw.split("\n") if line.strip()]
            if any(not isinstance(row, dict) for row in rows):
                raise ValueError("MEMORY_AUTHORITY_UNREADABLE")
            retained = [row for row in rows if not store.tombstones.excludes(str(row.get("entry_id") or ""),
                        jsonl.memory_content_hash(str(row.get("content") or "")), records=blocked)]
            if len(retained) == len(rows):
                continue
            if str(relative) in current:
                raise ValueError("MEMORY_GENERATION_DELETE_INCONSISTENT")
            text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in retained)
            if memory.coordinator_io:
                jsonl._coordinator_memory_write(lambda target=path, value=text:
                    jsonl.write_private_text_file_atomic_unlocked(target, value, replace_once=True))
            else:
                jsonl.write_private_text_file_atomic_unlocked(path, text)
