"""复用原退役证明，保存与重验同一组物理来源；不创建业务执行权。"""
import hashlib
import json
import os
from contextlib import ExitStack

from ...common.nofollow_fs import read_bytes_beneath
from ...common.strict_json import load_strict_json
from .. import daemon_metadata
from .. import multiprocess_generation_retirement as legacy
from ..standalone_sources import StandaloneSourceUnproven


def digest(root, parts):
    """保留原 no-follow/单链接校验，内容摘要不随 rename 的 ctime 变化。"""
    def content(stamp):
        value = stamp[1]
        return value if isinstance(value, str) else [(name, content(child)) for name, child in value]
    value = content(legacy._tree_stamp(root, tuple(parts)))
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def snapshot(paths, lifecycle):
    """只在真实原锁与 ledger 锁内收集；首次完整证明沿原实现执行。"""
    lifecycle.require_held()
    with ExitStack() as retained:
        generations = legacy._old_generations(paths.root, None)
        facts = legacy._retirement_required(paths.root, generations, 0)
        if facts is None:
            return {'sources': [], 'digests': {}, 'identities': [], 'generations': []}
        parts = legacy._source_paths(paths.root)
        anchors = legacy._retained_directories(paths.root, parts, retained)
        before = {p: legacy._tree_stamp(paths.root, p) for p in parts}
        identities = legacy._collect_identities(paths.root, retained)
        _worker_coverage(paths.root, generations)
        legacy._prove(identities, 'standalone')
        legacy._require_snapshot(paths.root, parts, before, generations, None, identities, anchors)
        saved = [{key: row[key] for key in ('role', 'pid', 'identity', 'record', 'parts', 'scope') if key in row}
                 for row in identities]
        return {'sources': ['/'.join(p) for p in parts],
                'digests': {'/'.join(p): digest(paths.root, p) for p in parts},
                'identities': saved, 'generations': facts}


def _worker_coverage(root, generations):
    """有 N 个分区就必须找到 N 个身份；缺记录不是空槽。"""
    registry = legacy._discovery_registry(root)
    roles = [load_strict_json(raw) for raw in legacy._role_snapshot(root).values()]
    for generation in generations:
        metadata = legacy.read_generation_metadata(root, generation)
        count = metadata[0]['worker_count'] if metadata else None
        workers = {}
        if registry is not None and registry[0]['c_run_id'] == generation:
            count = registry[0]['worker_count']
            workers.update((row['worker'], row['process_identity']) for row in registry[0]['workers'])
        for row in roles:
            if row.get('c_run_id') == generation and 'worker' in row:
                count = row['of'] if count is None else count
                workers[row['worker']] = row.get('process_identity')
        if count is not None and (set(workers) != set(range(count)) or any(not row for row in workers.values())):
            raise StandaloneSourceUnproven('migration_worker_identity_missing')


def verify_identities(identities):
    """每次续跑重新观察原身份与执行树，不把已写 proof 当成当前停止证明。"""
    executors = []
    for row in identities:
        live = daemon_metadata.process_identity_is_live(row['identity'])
        if row['role'] == 'C':
            if live is True:
                raise StandaloneSourceUnproven('retirement_coordinator_alive')
        elif live is not False or row['identity'] is None:
            raise StandaloneSourceUnproven('retirement_executor_alive_or_unknown')
        if row['role'] == 'W' and live is False:
            if os.name == "nt":
                from ...common.heartbeat import windows_process_alive

                observed = windows_process_alive(row['identity']['pid'])
                if observed is not False:
                    reason = 'migration_worker_pid_reused' if observed is True else 'migration_worker_liveness_unknown'
                    raise StandaloneSourceUnproven(reason)
                continue
            try:
                os.kill(row['identity']['pid'], 0)
            except ProcessLookupError:
                pass
            except OSError as error:
                raise StandaloneSourceUnproven('migration_worker_liveness_unknown') from error
            else:
                raise StandaloneSourceUnproven('migration_worker_pid_reused')
        if row['role'] == 'executor':
            executors.append(row)
    if executors:
        import time

        from .. import multiprocess_takeover as proof
        budget = proof._takeover_budget(None)
        deadline = time.monotonic() + budget['deadline_seconds']
        tables = proof._bounded_observations(proof.observe_processes, [r['record'] for r in executors], budget, deadline)
        for row in executors:
            evidence = proof._verify_record(row['record'], tables)
            if (time.monotonic() >= deadline or any(table['failure'] for table in tables)
                    or not evidence['enumeration_complete'] or evidence['status'] != 'stopped'
                    or evidence['observed_live_members']
                    or any(item['status'] != 'stopped' for item in evidence['observations'])):
                raise StandaloneSourceUnproven('retirement_executor_alive_or_unknown')


def verify_inventory(paths, journal, *, complete=False):
    """逐一核对原来源或原归档；两边都缺失、变化或意外重现均拒绝。"""
    archive = paths.root / journal['archive'] if journal.get('archive') else None
    remaining, moved = [], []
    for name in journal['sources']:
        parts = tuple(name.split('/'))
        if (not parts or any(p in ('', '.', '..') for p in parts)
                or parts[0] not in {'workers', 'admission', 'responsibility_index', 'gateway_state.json', 'gateway_state.json.lock'}):
            raise StandaloneSourceUnproven('migration_source_path_invalid')
        present = (paths.root / name).exists() or (paths.root / name).is_symlink()
        archived = archive is not None and ((archive / name).exists() or (archive / name).is_symlink())
        if archived:
            if digest(archive, parts) != journal['digests'][name] or present and not complete:
                raise StandaloneSourceUnproven('migration_sources_changed')
            moved.append(name)
        elif present and not complete:
            if digest(paths.root, parts) != journal['digests'][name]:
                raise StandaloneSourceUnproven('migration_sources_changed')
            remaining.append(name)
        else:
            raise StandaloneSourceUnproven('migration_source_missing')
    if not complete and set(remaining) != {'/'.join(p) for p in legacy._source_paths(paths.root)}:
        raise StandaloneSourceUnproven('migration_sources_changed')
    return remaining, moved


def read_journal(paths):
    """缺失才是首次；坏 JSON、跨根或不完整协议不能当成新迁移。"""
    if not paths.root.exists() and not paths.root.is_symlink():
        return None
    raw = read_bytes_beneath(paths.root, ('single_process_migration.json',), max_bytes=16 * 1024 * 1024)
    if raw is None:
        return None
    row = load_strict_json(raw)
    if (type(row) is not dict or row.get('schema') != 'gateway_single_process_migration.v1'
            or row.get('root') != str(paths.root) or row.get('state') not in {'running', 'complete'}
            or type(row.get('phases')) is not list):
        raise StandaloneSourceUnproven('migration_journal_invalid')
    if row.get('archive') and (not row['archive'].startswith('multiprocess_retired/')
                               or len(row['archive'].split('/')) != 2):
        raise StandaloneSourceUnproven('migration_archive_invalid')
    return row
