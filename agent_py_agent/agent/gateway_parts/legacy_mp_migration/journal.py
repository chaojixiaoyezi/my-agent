"""按名称持久提交迁移阶段；源消失必须由原归档字节说明，不能靠空目录完成。"""
import copy
import json
from contextlib import contextmanager

from ...common.nofollow_fs import write_bytes_atomic_beneath
from .. import multiprocess_generation_retirement as legacy
from ..io import validated_gateway_request_fingerprint
from ..standalone_lifecycle import _HELD, standalone_lifecycle_guard
from ..standalone_sources import StandaloneSourceUnproven
from .requests import candidates, close_requests
from .sources import read_journal, snapshot, verify_identities, verify_inventory


@contextmanager
def _lifecycle(paths):
    """启动沿用实际已持对象；离线命令才争取同一 canonical 生命周期锁。"""
    for held in tuple(_HELD):
        if held.path.parent == paths.root:
            held.require_held()
            yield held
            return
    with standalone_lifecycle_guard(paths.root) as held:
        yield held


def _write_journal(paths, row):
    """使用现有 no-follow 原子/fsync 写叶；每个下一阶段先见完整前阶段。"""
    write_bytes_atomic_beneath(paths.root, ('single_process_migration.json',),
                               json.dumps(row, sort_keys=True, allow_nan=False).encode())


def _finish(paths, row, name, result):
    """阶段只追加，不回退或由目录是否为空推断。"""
    row['phases'].append({'name': name, 'result': result})
    _write_journal(paths, row)


def _verify_complete(paths, row):
    """已完成仍复核全部原归档和新活跃来源；完成回执不授执行权。"""
    expected = ['drain', 'prove-exit', 'retire-generations', 'close-out-unroutable', 'archive']
    if [p['name'] for p in row['phases']] != expected:
        raise StandaloneSourceUnproven('migration_journal_incomplete')
    verify_inventory(paths, row, complete=True)
    verify_identities(row['identities'])
    generations = legacy._old_generations(paths.root, None)
    if legacy._retirement_required(paths.root, generations, 0) is not None or candidates(paths):
        raise StandaloneSourceUnproven('migration_new_sources_present')


def _retire(paths, held, row):
    """原移动器只增加同步观察口；intent 先落盘，manifest 未落盘也能精确续搬。"""
    held.require_held()
    verify_identities(row['identities'])
    if not row['sources']:
        return []
    if row['sources'] and not row.get('archive'):
        archive = legacy._archive_dir(paths.root, {r['c_run_id']: set() for r in row['generations']})
        row['archive'] = str(archive.relative_to(paths.root))
        _write_journal(paths, row)
    remaining, moved = verify_inventory(paths, row)
    archive = paths.root / row['archive']
    manifest = {'schema_version': 'multiprocess_generation_retirement.v1', 'target_count': 0,
                'generations': row['generations'], 'moved': moved, 'reason': 'multiprocess_to_standalone'}
    legacy._write_manifest(archive, manifest)

    def observe(parts, committed):
        held.require_held()
        name = '/'.join(parts)
        if name not in remaining:
            raise StandaloneSourceUnproven('migration_sources_changed')
        row['intent'] = None if committed else name
        row['moved'] = list(manifest['moved'])
        _write_journal(paths, row)

    legacy._move_sources(paths.root, archive, manifest, observe=observe)
    remaining, moved = verify_inventory(paths, row)
    if remaining:
        raise StandaloneSourceUnproven('migration_sources_unsettled')
    row['moved'], row['intent'] = moved, None
    return moved


def run_migration(paths, *, dry_run):
    """预览完全只读；实际执行与启动共用同一 journal 和原生命周期锁。"""
    row = read_journal(paths)
    if dry_run:
        candidates(paths)
        return {'dry_run': True, 'state': row['state'] if row else 'not_started',
                'phases': copy.deepcopy(row['phases']) if row else []}
    if row is not None and row['state'] == 'complete':
        _verify_complete(paths, row)
        return row
    candidates(paths)
    with _lifecycle(paths) as held:
        row = read_journal(paths)
        if row is None:
            row = {'schema': 'gateway_single_process_migration.v1', 'root': str(paths.root),
                   'state': 'running', 'phases': [], 'sources': [], 'digests': {},
                   'identities': [], 'generations': [], 'archive': None, 'moved': [], 'intent': None}
        names = [p['name'] for p in row['phases']]
        expected = ['drain', 'prove-exit', 'retire-generations', 'close-out-unroutable', 'archive']
        if names != expected[:len(names)]:
            raise StandaloneSourceUnproven('migration_phase_order_invalid')
        candidates(paths)
        if 'drain' not in names:
            row['requests'] = [{'id': request['id'],
                                'fingerprint': validated_gateway_request_fingerprint(request, request['id'])}
                               for _path, request in candidates(paths)]
            _finish(paths, row, 'drain', {'mode': 'offline', 'new_work_started': False})
        if 'prove-exit' not in names:
            row.update(snapshot(paths, held))
            verify_identities(row['identities'])
            _finish(paths, row, 'prove-exit', {'identities': len(row['identities'])})
        if 'retire-generations' not in names:
            _finish(paths, row, 'retire-generations', {'moved': _retire(paths, held, row)})
        else:
            verify_inventory(paths, row, complete=True)
            verify_identities(row['identities'])
        if 'close-out-unroutable' not in names:
            _finish(paths, row, 'close-out-unroutable', {'requests': close_requests(paths, held, row['requests'])})
        if 'archive' not in names:
            _finish(paths, row, 'archive', {'archive': row['archive']})
        row['state'] = 'complete'
        _verify_complete(paths, row)
        _write_journal(paths, row)
        return row
