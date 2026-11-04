"""Owner数据源清单及告警去重存储；不连接端点、不解析真实凭据。"""
from __future__ import annotations

import json
import math
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import (
    _verify_directory_beneath,
    locked_json_path,
    read_bytes_beneath,
    safe_asset_id,
    write_text_atomic_beneath,
)
from .descriptor import CredentialBindingRef


class ManifestRevisionConflict(ValueError):
    """清单CAS不匹配；调用者按类型处理，不解析自由文本。"""


# LLM: 健康账后缀保留，防止两个逻辑对象映射到同一物理叶。
# 函数用途: 校验系统ID并取得固定存储路径。
def _parts(source_system, *, health=False):
    safe_asset_id(source_system)
    if source_system.casefold().endswith('.health'):
        raise ValueError('reserved source system suffix')
    return ('data', 'source_manifests', source_system + ('.health.json' if health else '.json'))


# LLM: 引用只作账本身份，不是秘密、URL认证或文件路径授权。
# 函数用途: 校验不透明引用。
def _reference(value):
    if not isinstance(value, str) or '|' in value:
        raise ValueError('invalid manifest reference')
    if value:
        CredentialBindingRef(value)
    return value


# LLM: 用户清单严格校验，不能把秘密或任意字段混进宿主管理账。
# 函数用途: 校验并复制有界来源列表。
def _sources(value, *, max_sources=256):
    if not isinstance(value, list) or len(value) > max_sources:
        raise ValueError('invalid manifest sources')
    result = {}
    for row in value:
        if not isinstance(row, Mapping) or row.keys() - {'source_id', 'core', 'credential_ref', 'endpoint_ref'}:
            raise ValueError('invalid manifest source')
        source_id = safe_asset_id(row.get('source_id'))
        core = row.get('core', False)
        if type(core) is not bool or source_id in result:
            raise ValueError('invalid manifest source identity')
        result[source_id] = {'source_id': source_id, 'core': core,
            'credential_ref': _reference(row.get('credential_ref', '')),
            'endpoint_ref': _reference(row.get('endpoint_ref', ''))}
    return [result[key] for key in sorted(result)]


# LLM: 缺失只返回None；权限和坏文件不伪装成空集合，沿W1同一nofollow读口。
# 函数用途: 读取一份受控JSON快照。
def _read(owner_home, parts, *, max_bytes=524288):
    raw = read_bytes_beneath(Path(owner_home), parts, max_bytes=max_bytes)
    return None if raw is None else json.loads(raw)


# LLM: 复用W1的portable原子写叶，调用者持同一JSON锁。
# 函数用途: 原子保存一份清单或健康账。
def _write(owner_home, parts, value):
    write_text_atomic_beneath(Path(owner_home), parts,
        json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + '\n')


# LLM: 与资产Store共用进程/线程锁；只在写操作创建父目录。
# 函数用途: 串行化一个清单或健康账的读改写。
@contextmanager
def _locked(owner_home, parts):
    home = Path(owner_home)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent = _verify_directory_beneath(home, parts[:-1], create=True)
    with locked_json_path(parent / parts[-1]):
        yield


# LLM: 缺失不创建目录或锁；读取严格核验schema和系统身份。
# 函数用途: 读取数据源清单或revision零的未登记快照。
def load_manifest(owner_home, source_system):
    raw = _read(owner_home, _parts(source_system))
    if raw is None:
        return {'schema_version': 'datasource_manifest.v1', 'source_system': source_system,
                'revision': 0, 'sources': [], 'updated_at': None}
    if (not isinstance(raw, dict) or raw.get('schema_version') != 'datasource_manifest.v1'
            or raw.get('source_system') != source_system or type(raw.get('revision')) is not int
            or raw['revision'] < 0):
        raise ValueError('invalid manifest snapshot')
    return {**raw, 'sources': _sources(raw.get('sources'))}


# LLM: None仅表示首次创建；已有清单每次必须提供精确revision，冲突零发表。
# 函数用途: 校验清单CAS前置条件。
def _check_revision(current, expected_revision):
    if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 0):
        raise ValueError('invalid manifest revision')
    if current['revision'] != (0 if expected_revision is None else expected_revision):
        raise ManifestRevisionConflict('ASSET_REVISION_CONFLICT')


# LLM: 调用者持锁且已核CAS；先原子发表完整快照，再返回新revision。
# 函数用途: 提交一次清单变更。
def _publish(owner_home, current, sources):
    value = {**current, 'revision': current['revision'] + 1, 'sources': sources,
             'updated_at': datetime.now(timezone.utc).isoformat()}
    _write(owner_home, _parts(current['source_system']), value)
    return value


# LLM: 登记仅合并显式来源，不删除其他来源；用户确认由工具入口负责。
# 函数用途: CAS增量保存已确认的清单来源。
def save_manifest(owner_home, source_system, sources, *, expected_revision=None):
    parts, additions = _parts(source_system), _sources(sources)
    with _locked(owner_home, parts):
        current = load_manifest(owner_home, source_system)
        _check_revision(current, expected_revision)
        merged = {row['source_id']: row for row in [*current['sources'], *additions]}
        return _publish(owner_home, current, _sources(list(merged.values())))


# LLM: 删除必须匹配调用者观察到的版本。
# 函数用途: 从清单移除明确来源。
def prune_sources(owner_home, source_system, source_ids, *, expected_revision):
    parts = _parts(source_system)
    if not isinstance(source_ids, list):
        raise ValueError('invalid source ids')
    removed = {safe_asset_id(value) for value in source_ids}
    with _locked(owner_home, parts):
        current = load_manifest(owner_home, source_system)
        _check_revision(current, expected_revision)
        return _publish(owner_home, current, [row for row in current['sources'] if row['source_id'] not in removed])


# LLM: 有时区时间或有限epoch秒是唯一时间输入，不接受bool和本地模糊时间。
# 函数用途: 把告警窗口时间归一成UTC时刻。
def _instant(value):
    if type(value) in (int, float) and math.isfinite(value):
        return datetime.fromtimestamp(value, timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError('invalid alert time')
    return value.astimezone(timezone.utc)


# LLM: 时间由宿主传入，状态文件只记录不透明引用。
# 函数用途: 原子判断并记录一次告警窗口。
def should_emit_alert(owner_home, source_system, source_id, credential_ref, *, now, window_hours=24):
    parts = _parts(source_system, health=True)
    safe_asset_id(source_id)
    _reference(credential_ref)
    instant = _instant(now)
    if type(window_hours) not in (int, float) or not math.isfinite(window_hours) or window_hours <= 0:
        raise ValueError('invalid alert window')
    key = source_id + '|' + credential_ref
    with _locked(owner_home, parts):
        state = _read(owner_home, parts)
        alerts = (state.get('alerts', {}) if isinstance(state, dict)
                  and state.get('schema_version') == 'datasource_health.v1' else {})
        alerts = dict(alerts) if isinstance(alerts, dict) else {}
        previous = alerts.get(key)
        try:
            recent = previous is not None and (instant - _instant(previous)).total_seconds() < window_hours * 3600
        except (ValueError, TypeError, OverflowError):
            recent = False
        if recent:
            return False
        alerts[key] = instant.isoformat()
        _write(owner_home, parts, {'schema_version': 'datasource_health.v1', 'alerts': alerts})
    return True
