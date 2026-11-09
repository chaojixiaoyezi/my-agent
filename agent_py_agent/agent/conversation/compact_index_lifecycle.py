"""Compact 节点仓的提交 pin、可续迁移与有界回收；不以时间猜测提交链。"""
from __future__ import annotations

import hashlib
import json
from itertools import islice
from pathlib import Path

from .immutable_node_store import ImmutableNodeStore, node_children, verify_node_bytes
from .store_io import ImmutableJsonIndex

_INDEX_FIELDS = ('messages', 'tools', 'scope_heads', 'readable_bases', 'checkpoint_lineage')
# 常规写入与维护只接触固定数量 checkpoint/节点，迁移继续位置保存在事务仓。
_MIGRATION_BATCH_COUNT = 128


def _stores(root, tid):
    return tuple(ImmutableNodeStore(Path(root), ('evidence', tid, name)) for name in ('indexes', 'timeline'))


def pin_checkpoint_indexes(root, row, *, committed=False):
    meta = row.get('carried_view', {})
    stores = _stores(root, row['thread_id'])
    payloads = [('payload', row[name]['ref']) for name in ('operation_evidence_ref', 'provider_compaction_ref') if row.get(name)]
    payloads.append(('copy', hashlib.sha256(row['checkpoint_id'].encode()).hexdigest() + '.checkpoint.json'))
    def validate_object(namespace, name):
        from .hamt_reachability import _Directory, _regular

        directory = Path(root) / ('archive' if namespace == 'copy' else 'evidence') / row['thread_id']
        with _Directory(directory) as opened:
            if not _regular(opened.stamp(name)):
                raise OSError('compact object disappeared before checkpoint publication')

    stores[0].pin(row['checkpoint_id'], row['generation'], [meta.get(name) for name in _INDEX_FIELDS],
                  committed=committed, objects=payloads, object_validator=validate_object)
    stores[1].pin(row['checkpoint_id'], row['generation'], [meta.get('thread_timeline')], committed=committed)


def commit_index_checkpoint(root, thread):
    """CAS 后只登记确切获胜行；异常由调用者记为维护待恢复，不撤销原提交。"""
    from .compact_checkpoint import _cold_checkpoint

    stores = _stores(root, thread.thread_id)
    if not any(store.exists() for store in stores):
        return
    row = _cold_checkpoint(Path(root), thread.thread_id, thread.compact_checkpoint_id)
    if row is None or row['generation'] != thread.compact_generation:
        raise OSError('committed compact index checkpoint is unavailable')
    pin_checkpoint_indexes(root, row, committed=True)
    for store in stores:
        if store.state('sealed_head') == row.get('previous_checkpoint_id', ''):
            store.set_state('sealed_head', row['checkpoint_id'])


def _recover_chain(work, root, thread, stores):
    from .compact_checkpoint import _cold_checkpoint

    for store in stores:
        if not store.exists():
            if not work.request.run_write(lambda: (store.set_state('sealed_head', ''), True)[1]):
                return False
    sealed = stores[0].state('sealed_head')
    # 两个命名空间必须同时覆盖同一个提交前缀；部分登记不能放行回收。
    if any(store.state('sealed_head') != sealed for store in stores):
        sealed = ''
    if sealed == thread.compact_checkpoint_id:
        return True
    saved = stores[0].state('chain_progress')
    progress = json.loads(saved) if saved else {
        'target': thread.compact_checkpoint_id, 'next': thread.compact_checkpoint_id,
        'generation': thread.compact_generation, 'sealed': sealed,
    }
    if progress['sealed'] != sealed:
        progress = {'target': thread.compact_checkpoint_id, 'next': thread.compact_checkpoint_id,
                    'generation': thread.compact_generation, 'sealed': sealed}
    count = 0
    while progress['next'] != sealed:
        if work.exhausted() or count >= _MIGRATION_BATCH_COUNT:
            return False
        current = progress['next']
        if not current:
            raise OSError('compact index migration chain is incomplete')
        row = _cold_checkpoint(root, thread.thread_id, current)
        if row is None or row['generation'] != progress['generation']:
            raise OSError('compact index migration chain is incomplete')
        # 原冷读取器核验身份、摘要及完整 source；持久地址始终只是读投影。
        work.read(len(json.dumps(row, ensure_ascii=False).encode()))
        carried = row.get('carried_view', {})
        work.read(sum(ref['ref']['byte_length'] for name in (*_INDEX_FIELDS, 'thread_timeline')
                      if (ref := carried.get(name)) is not None))
        next_progress = {**progress, 'next': row.get('previous_checkpoint_id', ''),
                         'generation': progress['generation'] - 1}

        def publish():
            pin_checkpoint_indexes(root, row, committed=True)
            stores[0].set_state('chain_progress', json.dumps(next_progress))
            return True

        if not work.request.run_write(publish):
            return False
        progress = next_progress
        count += 1
        work.items += 1
    for store in stores:
        if not work.request.run_write(lambda: (store.set_state('sealed_head', progress['target']), True)[1]):
            return False
    if not work.request.run_write(lambda: (stores[0].set_state('chain_progress', ''), True)[1]):
        return False
    return progress['target'] == thread.compact_checkpoint_id


def _migrate_files(work, root, tid, stores):
    from .hamt_reachability import (
        HAMT_SWEEP_GRACE_SECONDS,
        _bounded_read,
        _Directory,
        _quarantine_one,
        _regular,
    )

    for store in stores:
        with _Directory(store.path.parent) as directory:
            names = list(islice((name for name in directory.names() if name.endswith('.json')), _MIGRATION_BATCH_COUNT + 1))
            window = names[:_MIGRATION_BATCH_COUNT]
            for name in window:
                if work.exhausted():
                    return False
                info = directory.stamp(name)
                if not _regular(info) or info.st_size > 1024 * 1024:
                    raise OSError('untrusted legacy immutable node')
                raw = _bounded_read(work, root, (*store.parts, name))
                verify_node_bytes(name, raw)
                reference = {'ref': {'ref': name, 'sha256': name[:-5], 'byte_length': len(raw)}, 'maximum': None}
                ImmutableJsonIndex(root, store.parts)._load(reference)

                def migrate():
                    current = directory.stamp(name)
                    if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (
                            info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns):
                        return False
                    store.save(name, raw, node_children(raw), touched=info.st_mtime)
                    if store.read(name) != raw:
                        raise OSError('immutable node migration readback mismatches')
                    directory.unlink(name)
                    return True

                if not work.request.run_write(migrate):
                    return False
                work.items += 1
        if len(names) > _MIGRATION_BATCH_COUNT:
            return False
    for store in stores:
        with store.connection() as conn:
            if conn.execute('SELECT name FROM nodes WHERE raw IS NULL LIMIT 1').fetchone():
                raise OSError('immutable node migration has missing nodes')
    return True


def _sweep_objects(work, root, tid, store):
    from .hamt_reachability import HAMT_SWEEP_GRACE_SECONDS, _Directory, _quarantine_one, _regular

    cutoff = work.request.now - HAMT_SWEEP_GRACE_SECONDS
    candidates = store.garbage_files(cutoff, _MIGRATION_BATCH_COUNT)
    for namespace, name in candidates:
        if work.exhausted():
            return False
        path = root / ('evidence' if namespace == 'payload' else 'archive') / tid
        try:
            with _Directory(path) as directory:
                info = directory.stamp(name)
                if not _regular(info):
                    raise OSError('untrusted compact object')
                if info.st_mtime < cutoff:
                    def erase():
                        # 零引用复核与物理删除持有同一事务，不能删掉刚取得pin的载荷。
                        with store.connection(write=True) as conn:
                            row = conn.execute('SELECT refs FROM files WHERE namespace=? AND name=?', (namespace, name)).fetchone()
                            if row is None or row[0] != 0:
                                return 0, 0, 0
                            result = _quarantine_one(directory, name, info, cutoff)
                            if result[0]:
                                conn.execute('DELETE FROM files WHERE namespace=? AND name=? AND refs=0', (namespace, name))
                            return result

                    result = work.request.run_write(erase)
                    if result is False:
                        return False
                    deleted, freed, _ = result
                    work.deleted += deleted
                    work.freed += freed
        except FileNotFoundError:
            work.request.run_write(lambda: (store.forget_file(namespace, name), True)[1])
        work.items += 1
    return len(candidates) < _MIGRATION_BATCH_COUNT


def maintain_index_store(work, root, thread):
    """账本锁内执行；历史链只迁移一次，常规回收使用有索引的零入边队列。"""
    from .hamt_reachability import HAMT_SWEEP_GRACE_SECONDS

    stores = _stores(root, thread.thread_id)
    if not _recover_chain(work, root, thread, stores):
        return False
    if not _migrate_files(work, root, thread.thread_id, stores):
        return False
    for store in stores:
        if work.exhausted():
            return False
        limit = min(_MIGRATION_BATCH_COUNT, max(0, work._active_budget().max_items - work.items))
        result = work.request.run_write(lambda: store.collect(generation=thread.compact_generation,
                                                              cutoff=work.request.now - HAMT_SWEEP_GRACE_SECONDS, limit=limit))
        if result is False:
            return False
        items, deleted, freed = result
        work.items += items
        work.deleted += deleted
        work.freed += freed
    return _sweep_objects(work, root, thread.thread_id, stores[0])


def index_store_due(work, root, thread, stores):
    """只读预筛：未发现迁移或到期对象时不出生维护锁、不写游标。"""
    from types import SimpleNamespace

    from .compact_checkpoint import (
        _validate_hot_checkpoint_row,
        load_latest_committed_compact_checkpoint,
    )
    from .hamt_reachability import HAMT_SWEEP_GRACE_SECONDS, _bounded_read, _Directory

    raw = _bounded_read(work, root, ('conversations', thread.thread_id + '.jsonl'))
    for line in raw.split(b'\n'):
        if line.strip():
            row = json.loads(line)
            if type(row) is not dict or row.get('thread_id') != thread.thread_id:
                raise OSError('invalid compact ledger row')
            _validate_hot_checkpoint_row(row)
    current = load_latest_committed_compact_checkpoint(SimpleNamespace(home_paths=work.request.home), thread)
    if current is None:
        raise OSError('compact checkpoint is unavailable')
    meta = current.get('carried_view', {})
    for store, fields in zip(stores, (_INDEX_FIELDS, ('thread_timeline',))):
        if not store.exists() or store.state('sealed_head') != thread.compact_checkpoint_id:
            return True
        index = ImmutableJsonIndex(root, store.parts)
        for name in fields:
            if meta.get(name) is not None:
                work.read(meta[name]['ref']['byte_length'])
                index._load(meta[name])
        if store.has_due(thread.compact_generation, work.request.now - HAMT_SWEEP_GRACE_SECONDS):
            return True
        with _Directory(store.path.parent) as directory:
            if next((name for name in directory.names() if name.endswith('.json')), None) is not None:
                return True
    return False
