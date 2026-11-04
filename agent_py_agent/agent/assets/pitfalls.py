"""按资产ID存储已知pitfall侧账本；不修改描述符、发布版本或提示前缀。"""
from __future__ import annotations

import hashlib

from .revision_history import (
    _HISTORY_PARTS,
    _asset_lock,
    _identity,
    _integer,
    _iso_time,
    _read_json,
    _single_line,
    _write_json,
)


# LLM: 缺失侧账本是空列表，损坏账本不静默覆盖；读取整份原子快照。
# 函数用途: 读取原始插入顺序的pitfall条目。
def _entries(owner_home, kind, asset_id):
    value = _read_json(owner_home, (*_HISTORY_PARTS, 'pitfalls', kind, f'{asset_id}.json'))
    if value is None:
        return []
    if (not isinstance(value, dict) or value.get('schema_version') != 'asset_pitfalls.v1'
            or value.get('kind') != kind or value.get('asset_id') != asset_id
            or not isinstance(value.get('entries'), list)
            or any(not isinstance(entry, dict) or not isinstance(entry.get('pitfall_id'), str)
                   or not isinstance(entry.get('text'), str) for entry in value['entries'])):
        raise ValueError('invalid pitfall ledger')
    return value['entries']


# LLM: 只读侧账本，返回有界倒序窗口；不写descriptor或Tier1。
# 函数用途: 读取指定资产最新几条pitfall。
def read_known_pitfalls(owner_home, kind, asset_id, *, limit: int = 5) -> list[dict]:
    _identity(kind, asset_id)
    _integer(limit)
    return list(reversed(_entries(owner_home, kind, asset_id)[-limit:])) if limit else []


# LLM: 资产锁内重读去重并FIFO截断；文本仅作展示，不解释为控制或权限。
# 函数用途: 新增一条pitfall，重复或空文本返回None。
def add_known_pitfall(owner_home, kind, asset_id, *, text: str, evidence_ref: str = '',
                      source: str = 'route3', recorded_at: str | None = None) -> dict | None:
    _identity(kind, asset_id)
    if source not in ('route3', 'user') or not isinstance(evidence_ref, str):
        raise ValueError('invalid pitfall source or evidence')
    clean = _single_line(text, limit=240)
    if not clean:
        return None
    pitfall_id = hashlib.sha256(clean.casefold().encode('utf-8')).hexdigest()
    entry = {'pitfall_id': pitfall_id, 'text': clean, 'recorded_at': _iso_time(recorded_at),
             'evidence_ref': evidence_ref, 'source': source}
    with _asset_lock(owner_home, kind, asset_id):
        entries = _entries(owner_home, kind, asset_id)
        if any(old['pitfall_id'] == pitfall_id for old in entries):
            return None
        value = {'schema_version': 'asset_pitfalls.v1', 'kind': kind, 'asset_id': asset_id,
                 'entries': [*entries, entry][-20:]}
        _write_json(owner_home, (*_HISTORY_PARTS, 'pitfalls', kind, f'{asset_id}.json'), value)
    return entry
