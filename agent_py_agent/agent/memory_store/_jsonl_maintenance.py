"""仅维护路径使用的正式事件语义校验与读取证据；普通召回兼容读不受影响。"""
# LLM: 严格维护共用实际读取证据；不改普通召回的容错合同或公共JSON解析器。
# 模块用途: 验证维护源并携带是否真正读到文件的事实，保护破坏性派生重建。
import json
import math
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


# LLM: 复用唯一quota→policy→formal锁序；dry-run不创建锁或代次文件，清单已存在时幂等。
# 函数用途: 将旧正式库按字符一次性分层，旧archive只隔离保存不猜可见性。
def migrate_tiers(memory, *, dry_run=False):
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
        if root:
            return root, None, None
        events = _strict_memory_events(memory.path)
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
                return preview
            result = publish_tier_generation(memory, records, admission=admission,
                max_chars=options[0], quarantine=options[1])
            return {**preview, **result}


# LLM: 新分片、删除账及根复用原私有writer/当前C末门；不复制Curator回滚算法，未发表材料天然不可见。
# 函数用途: 提交冷热代次，并将旧单文件降为阻止旧写者混写的schema标记。
def publish_tier_generation(memory, records, *, admission=None, commit=None, redact_contents=None,
                            max_chars=None, quarantine=None):
    from ..common.nofollow_fs import read_text_beneath
    from ..user_space.managed_user_governance import managed_locked_memory_ids
    from . import jsonl
    from .generation_manifest import GENERATION_SCHEMA, MemoryGenerationStore

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
    marker = json.dumps({"schema_version": GENERATION_SCHEMA, "manifest": store.manifest_path.name}) + "\n"
    if read_text_beneath(memory.path.parent, (memory.path.name,)) != marker:
        try:
            write(memory.path, marker)
        except Exception as exc:
            if isinstance(exc, jsonl._NATIVE_MEMORY_ERRORS):
                raise
            from ._jsonl_indexing import RemovalCleanupError

            raise RemovalCleanupError("MEMORY_GENERATION_COMMITTED_CLEANUP_PENDING", formal_memory_changed=True) from exc
    return result


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
    )

    incoming = list(records)
    for record in incoming:
        require_maintenance_event(record)
        if record.action not in {"add", "replace"}:
            raise ValueError("MEMORY_BACKUP_RECORD_INVALID")
    owner = memory.path.parents[2] if len(memory.path.parents) >= 3 else memory.path.parent
    with _quota_admission(memory.quota_enforcer) as admission, _managed_policy_scope(owner):
        with memory._locked_memory():
            events = _strict_memory_events(memory.path)
            current = {r.entry_id: r for r in _materialize_memory_events(events, include_expired=True)}
            tombstones = {r.entry_id: r for r in events if r.action == "remove"}
            tombstones.update(MemoryGenerationStore(memory.path).tombstones.load())
            for record in incoming:
                if record.entry_id in tombstones:
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


# LLM: 删除屏障已提交后调用；只扫描本库固定代次目录及旧archive，失败由原REMOVE_CLEANUP_PENDING保留责任。
# 函数用途: 清除内部历史分片中的已删正文，并丢弃含旧正文的派生SQLite。
def purge_tier_copies(memory):
    from ..common.nofollow_fs import NoFollowPathError, read_text_beneath
    from . import jsonl
    from .generation_manifest import MemoryGenerationStore
    from .read_index import MemoryReadIndex

    store = MemoryGenerationStore(memory.path)
    if store.manifest() is None:
        return
    with memory._locked_memory():
        root = store.manifest()
        blocked = store.tombstones.load()
        if not blocked:
            return
        MemoryReadIndex(memory.path, coordinator_io=memory.coordinator_io).invalidate()
        current = {item["path"] for item in root["segments"]}
        files = [memory.path.with_name(memory.path.name + ".archive.jsonl")]
        for directory in store.directory.iterdir():
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
            retained = [row for row in rows if row.get("entry_id") not in blocked]
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
