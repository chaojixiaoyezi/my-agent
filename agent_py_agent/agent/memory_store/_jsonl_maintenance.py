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


# LLM: 全表reset共用正式锁；require_source来自预检实际读取，不用独立exists猜测；不重复取锁。
# 函数用途: 在锁内重读并保留已见源要求，验证完才reset及投影；锁外另做缓存回收。
def reset_local_index(memory, *, require_source=False, rebuild_memory=True):
    from ..common.json_io import locked_json_path

    with locked_json_path(memory.path):
        try:
            snapshot = memory._maintenance_snapshot(require_source=require_source)
            records = snapshot.records
            if require_source or snapshot.source_opened:
                memory.path.stat()
        except (OSError, ValueError, TypeError) as exc:
            raise AuthorityPreflightError('MEMORY_AUTHORITY_UNREADABLE') from exc
        memory.local_store.reset()
        if rebuild_memory:
            for record in records:
                memory._write_record_projection(record)
        return len(records) if rebuild_memory else 0
