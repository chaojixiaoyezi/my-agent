# LLM: 启动前持原生命周期锁逐项证明旧C/W/P1死亡；只移动设施，不写closure或业务请求，未知来源整组拒绝。
# 模块用途: 把已死的多进程代次移到私有退役目录，让两种拓扑可以重新启动。
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import time
from contextlib import ExitStack
from datetime import datetime, timezone

from ..common.nofollow_fs import (
    list_names_beneath,
    open_directory_beneath,
    read_bytes_beneath,
    write_bytes_atomic_beneath,
)
from ..common.strict_json import load_strict_json
from . import daemon_metadata
from .multiprocess_coordinator import CoordinatorUnavailable, current_coordinator
from .multiprocess_generation_metadata import generation_metadata_parts, read_generation_metadata
from .multiprocess_records import _identifier, deserialize_record
from .multiprocess_takeover_sources import _discovery_registry, _role_snapshot
from .multiprocess_takeover_unresolved_source import read_executor_source
from .standalone_lifecycle import StandaloneLifecycle
from .standalone_sources import StandaloneSourceUnproven


# LLM: 只读规范角色、P4与全部generation叶；排除本次代次，不把缺历史或失败state补成闭包。
# 函数用途: 收集需要一起评估的旧代次及其来源。
def _old_generations(root, current):
    generations = {}
    for name, raw in _role_snapshot(root).items():
        row = load_strict_json(raw)
        if type(row) is not dict:
            raise ValueError('retirement_role_invalid')
        if 'c_run_id' in row:
            _identifier(row['c_run_id'], 'c_run_id')
            generations.setdefault(row['c_run_id'], set()).add(name)
    registry = _discovery_registry(root)
    if registry is not None:
        generations.setdefault(registry[0]['c_run_id'], set()).add('workers/registry.json')
    for name in list_names_beneath(root, ('workers', 'generations')):
        if not name.endswith('.json'):
            continue
        raw = read_bytes_beneath(root, ('workers', 'generations', name),
                                 max_bytes=4 * 1024 * 1024, require_dir_fd=True)
        row = deserialize_record(raw)
        if (row['schema_version'] not in {'multiprocess_c_generation.v1', 'multiprocess_c_generation.v2'}
                or generation_metadata_parts(row['c_run_id'])[-1] != name):
            raise ValueError('retirement_generation_invalid')
        read_generation_metadata(root, row['c_run_id'])
        generations.setdefault(row['c_run_id'], set()).add('workers/generations/' + name)
    generations.pop(current, None)
    return generations


# LLM: MP只处理缺历史/v1/未封存/数量不符；匹配的sealed代保持原successor路径，Count0空设施目录不算遗留。
# 函数用途: 判定是否必须退役，并形成不修改原元数据的清单。
def _retirement_required(root, generations, target_count):
    facts = []
    for generation, sources in sorted(generations.items()):
        history = read_generation_metadata(root, generation)
        state, count = 'none', None
        if history is None:
            if sources == {'gateway_state.json'}:
                state = 'state_only'
        else:
            row, _raw = history
            count = row['worker_count']
            if row['schema_version'] == 'multiprocess_c_generation.v1':
                state = 'v1'
            elif row['closure'] is None:
                state = 'unsealed'
            else:
                state = 'sealed' if count == target_count else 'count_mismatch'
        facts.append({'c_run_id': generation, 'state': state, 'worker_count': count})
    if target_count > 0:
        return facts if any(row['state'] != 'sealed' for row in facts) else None
    facilities = [parts for parts in _source_paths(root, include_state=False)
                  if parts != ('admission', 'owner_capacity.transition.lock')]
    return facts if facts or facilities else None


# LLM: 保留完整P1/原文件目录身份和原旧N；ledger短锁由调用栈持到移动结束，暂存只分类不解码成责任。
# 函数用途: 读取全部原执行责任，不把缺身份或终态行丢成空账。
def _executor_identities(root, retained):
    import fcntl

    from ..common.nofollow_fs import open_readonly_file_beneath
    from .multiprocess_drain_sources import _generation_source_entry_kind
    from .multiprocess_executor_ledger import ExecutorLedger
    from .multiprocess_route import WorkerPartition
    from .multiprocess_takeover_sources import _original_generation_count, _target

    identities, groups, observed = [], {}, {}
    registry = _discovery_registry(root)
    try:
        for name in list_names_beneath(root, ('workers', 'executors')):
            if re.fullmatch(r'w(0|[1-9][0-9]*)', name) is None:
                raise ValueError('executor_partition_invalid')
            parts = ('workers', 'executors', name)
            names = list_names_beneath(root, parts)
            descriptor = open_readonly_file_beneath(root, (*parts, 'ledger.lock'))
            retained.callback(os.close, descriptor)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for filename in names:
                if _generation_source_entry_kind(root, parts, filename) != 'source':
                    _tree_stamp(root, (*parts, filename))
                    continue
                source = read_executor_source(root, (*parts, filename))
                row = deserialize_record(source[0])
                if (row['schema_version'] not in {'multiprocess_executor_reservation.v1', 'multiprocess_executor_reservation.v2'}
                        or hashlib.sha256(row['reservation_id'].encode()).hexdigest() + '.json' != filename):
                    raise ValueError('executor_binding_invalid')
                count = _original_generation_count(root, row['c_run_id'], registry, observed)
                if count is None:
                    raise ValueError('executor_original_scope_unknown')
                ledger = ExecutorLedger(root, WorkerPartition(int(name[1:]), count, row['c_run_id']))
                ledger._bound_record(row)
                if ledger.read(row['reservation_id']) != row:
                    raise ValueError('executor_source_changed')
                from ..user_space.owner_resolver import OwnerIdentity
                from .multiprocess_route import owner_digest

                role = 'C' if row['owner_sha'] == owner_digest(OwnerIdentity.local_main()) else 'W'
                key = (row['c_run_id'], role, None if role == 'C' else int(name[1:]), count)
                groups.setdefault(key, []).append(row)
                identity = row['process_identity']
                identities.append({'role': 'executor', 'pid': None if identity is None else identity['pid'],
                    'identity': identity, 'record': row, 'parts': (*parts, filename), 'source': source,
                    'group': key, 'ledger': ledger})
            if names != list_names_beneath(root, parts):
                raise ValueError('executor_namespace_changed')
        for item in identities:
            generation, role, index, count = item['group']
            target = _target(root, count, (generation, role, index), groups[item['group']])
            from .multiprocess_takeover_ledger import _validate_scope

            _validate_scope(target.ledger, target.scope)
            item['scope'] = target.scope
        if registry != _discovery_registry(root):
            raise ValueError('executor_registry_changed')
        for generation, metadata in observed.items():
            if metadata != read_generation_metadata(root, generation):
                raise ValueError('executor_generation_changed')
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise StandaloneSourceUnproven('retirement_executor_unreadable') from error
    return identities


# LLM: C缺完整身份只消费原锁，所有P4和W marker身份分别核，完整P1及其原scope独立保留。
# 函数用途: 汇总本次必须证明已退出的角色和执行责任。
def _collect_identities(root, retained):
    identities = []
    registry = _discovery_registry(root)
    if registry is not None:
        identities.append({'role': 'C', 'pid': registry[0]['process_identity']['pid'],
                           'identity': registry[0]['process_identity']})
        identities.extend({'role': 'W', 'pid': row['process_identity']['pid'],
                           'identity': row['process_identity']} for row in registry[0]['workers'])
    for name, raw in _role_snapshot(root).items():
        row = load_strict_json(raw)
        if name == 'gateway_state.json':
            identities.append({'role': 'C', 'pid': row.get('pid'), 'identity': row.get('process_identity')})
        else:
            row = deserialize_record(raw)
            if (row['schema_version'] != 'multiprocess_worker_heartbeat.v1'
                    or name != f"workers/w{row['worker']}.json"):
                raise ValueError('retirement_worker_binding_invalid')
            identities.append({'role': 'W', 'pid': row['process_identity']['pid'],
                               'identity': row['process_identity']})
    if not any(row['role'] == 'C' for row in identities):
        identities.append({'role': 'C', 'pid': None, 'identity': None})
    identities.extend(_executor_identities(root, retained))
    return identities


# LLM: 根锁只证明C；P1保原scope/整树且任次live/unknown拒绝；复用P2有界核验，不铸造native能力或完整域receipt。
# 函数用途: 为全部原责任取得整树停止观察；父退出及缺身份都不能单独放行。
def _prove(identities, lock_mode):
    from . import multiprocess_takeover as p2

    if lock_mode not in {'standalone', 'coordinator'}:
        raise ValueError('retirement_lock_required')
    executors = []
    for row in identities:
        live = daemon_metadata.process_identity_is_live(row['identity'])
        row['verdict'] = 'alive' if live is True else 'dead' if live is False else 'unknown'
        if row['role'] == 'C':
            if live is True:
                raise StandaloneSourceUnproven('retirement_coordinator_alive')
            row['verdict'] = 'lock_held'
        elif row['role'] == 'W':
            if live is not False:
                raise StandaloneSourceUnproven('retirement_worker_alive' if live is True
                                              else 'retirement_worker_liveness_unknown')
        else:
            # 空槽虽合法，却可能处于spawn后、登记前；没有原未执行证据时永久保守，不根据state猜。
            if row['identity'] is None or live is not False:
                raise StandaloneSourceUnproven('retirement_executor_alive_or_unknown')
            executors.append(row)
    if not executors:
        return
    started = time.monotonic()
    budget = p2._takeover_budget(None)
    deadline = started + budget['deadline_seconds']
    tables = p2._bounded_observations(p2.observe_processes, [row['record'] for row in executors], budget, deadline)
    for row in executors:
        evidence = p2._verify_record(row['record'], tables)
        row['exit_evidence'] = evidence
        if (time.monotonic() >= deadline or any(table['failure'] for table in tables)
                or not evidence['enumeration_complete'] or evidence['status'] != 'stopped'
                or evidence['observed_live_members']
                or any(item['status'] != 'stopped' for item in evidence['observations'])):
            row['verdict'] = 'unknown'
            raise StandaloneSourceUnproven('retirement_executor_alive_or_unknown')
        _require_executor_source(row)
        row['verdict'] = 'dead'


# LLM: 原P1完整字节/文件/目录身份、原绑定及scope均末核；不凭原JSON或观察结果生成执行权。
# 函数用途: 阻止证明期间换行、换桶或更换文件洗掉责任。
def _require_executor_source(item):
    from .multiprocess_takeover_ledger import _validate_scope

    ledger, record = item['ledger'], item['record']
    _validate_scope(ledger, item['scope'])
    if (read_executor_source(ledger.root, item['parts']) != item['source']
            or ledger._bound_record(record) != record or ledger.read(record['reservation_id']) != record
            or record['reservation_id'] not in item['scope']['executor_reservation_ids']
            or record['owner_sha'] not in item['scope']['owner_shas']):
        raise StandaloneSourceUnproven('retirement_sources_changed')


# LLM: 逐段no-follow遍历设施，目录可原样移动但内部链接/特殊文件不可收编；目录身份与叶内容纳入前后比较。
# 函数用途: 为移动前的整组复核取得完整设施指纹。
def _tree_stamp(root, parts):
    parent = open_directory_beneath(root, parts[:-1], require_dir_fd=True)
    try:
        info = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
    finally:
        os.close(parent)
    identity = (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    if stat.S_ISDIR(info.st_mode):
        return identity, tuple((name, _tree_stamp(root, (*parts, name)))
                               for name in list_names_beneath(root, parts))
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise StandaloneSourceUnproven('retirement_unknown_source')
    raw = read_bytes_beneath(root, parts, require_dir_fd=True)
    if raw is None:
        raise StandaloneSourceUnproven('retirement_sources_changed')
    return identity, hashlib.sha256(raw).hexdigest()


# LLM: 仅认registry的token暂存和heartbeat的UUID暂存；名称只绑定原canonical目标，不认证写者或授执行权。
# 函数用途: 分类两种已实现writer的准确未发表设施名称。
def _worker_staging_target(name):
    registry = re.fullmatch(r'\.(registry\.json)\.tmp-[0-9a-f]{32}', name)
    heartbeat = re.fullmatch(r'\.(w(?:0|[1-9][0-9]*)\.json)\.[0-9a-f]{32}\.tmp', name)
    match = registry or heartbeat
    return None if match is None else match[1]


# LLM: 只枚举固定原设施及精确暂存；nofollow普通单link门先行，业务请求/控制/ingress/responses永不移动。
# 函数用途: 按合同顺序列出待保全设施，canonical state始终最后。
def _source_paths(root, *, include_state=True):
    from ..common.nofollow_fs import open_readonly_file_beneath

    names = list_names_beneath(root, ('workers',))
    known = {'registry.json', 'registry.json.lock', 'control.json', 'control.json.lock',
             'executors', 'generations', 'takeover_evidence'}
    stages = {}
    for name in names:
        target = _worker_staging_target(name)
        if target is not None:
            descriptor = open_readonly_file_beneath(root, ('workers', name))
            os.close(descriptor)
            stages[name] = target
        elif name not in known and re.fullmatch(r'w(0|[1-9][0-9]*)\.json(?:\.lock)?', name) is None:
            raise StandaloneSourceUnproven('retirement_unknown_source')
    result = [('workers', name) for name in names if name in {'control.json', 'control.json.lock'}]
    result.extend(('workers', name) for name in names if re.fullmatch(r'w(0|[1-9][0-9]*)\.json(?:\.lock)?', name)
                  or stages.get(name, '').startswith('w'))
    for directory in ('takeover_evidence', 'executors', 'generations'):
        result.extend(('workers', directory, name) for name in list_names_beneath(root, ('workers', directory)))
    result.extend(('workers', name) for name in names if name in {'registry.json', 'registry.json.lock'}
                  or stages.get(name) == 'registry.json')
    result.extend(('admission', 'reservations', name)
                  for name in list_names_beneath(root, ('admission', 'reservations')))
    admission = list_names_beneath(root, ('admission',))
    if 'owner_capacity.transition.lock' in admission:
        result.append(('admission', 'owner_capacity.transition.lock'))
    result.extend(('responsibility_index', name) for name in list_names_beneath(root, ('responsibility_index',)))
    if include_state:
        names = list_names_beneath(root, ())
        if 'gateway_state.json.lock' in names:
            result.append(('gateway_state.json.lock',))
        if 'gateway_state.json' in names:
            result.append(('gateway_state.json',))
    return result


# LLM: 每次分配独占0700目录；同秒重启不覆盖前次manifest，UTC秒碰撞只顺延目录标签，不碰receipt archive。
# 函数用途: 给本次退役分配同文件系统的私有归档目录。
def _archive_dir(root, generations):
    parent = open_directory_beneath(root, ('multiprocess_retired',), create=True, require_dir_fd=True)
    try:
        second = int(time.time())
        suffix = sorted(generations)[0][:8] if generations else 'sources'
        while True:
            name = datetime.fromtimestamp(second, timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + suffix
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent)
                os.fsync(parent)
                return root / 'multiprocess_retired' / name
            except FileExistsError:
                second += 1
    finally:
        os.close(parent)


# LLM: 复用原atomic/fsync writer，manifest不含身份正文；移动前及每步成功后持久化，异常时仍保留已完成的准确移动。
# 函数用途: 保存本次退役的审计清单。
def _write_manifest(archive, manifest):
    write_bytes_atomic_beneath(archive, ('manifest.json',),
                               json.dumps(manifest, sort_keys=True, allow_nan=False).encode())


# LLM: 源父与目标父均固定nofollow FD，同盘os.replace后刷盘；已被其它进程flock的sidecar留原地，不移动锁到新inode。
# 函数用途: 按固定顺序搬走全部设施，并在每步之后更新清单。
def _move_sources(root, archive, manifest):
    import fcntl

    for parts in _source_paths(root):
        with ExitStack() as stack:
            source = open_directory_beneath(root, parts[:-1], require_dir_fd=True)
            stack.callback(os.close, source)
            if parts[-1].endswith('.lock'):
                descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=source)
                stack.callback(os.close, descriptor)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
            destination = open_directory_beneath(archive, parts[:-1], create=True, require_dir_fd=True)
            stack.callback(os.close, destination)
            os.replace(parts[-1], parts[-1], src_dir_fd=source, dst_dir_fd=destination)
            os.fsync(source)
            os.fsync(destination)
            manifest['moved'].append('/'.join(parts))
            _write_manifest(archive, manifest)
    _write_manifest(archive, manifest)


# LLM: Count0核原held对象及同root，MP核本进程原coordinator；缺锁/坏锁在任何退休读取或写之前拒绝。
# 函数用途: 确认调用者真的持有本Gateway的生命周期锁。
def _require_lock(paths, target_count, lifecycle):
    if type(target_count) is not int or target_count < 0:
        raise ValueError('retirement_target_count_invalid')
    if target_count == 0:
        if type(lifecycle) is StandaloneLifecycle and lifecycle.path == paths.root / 'coordinator.lock':
            try:
                lifecycle.require_held()
                return 'standalone'
            except RuntimeError:
                pass
    elif current_coordinator(paths.root, target_count) is not None:
        return 'coordinator'
    raise ValueError('retirement_lock_required')


# LLM: 固定原活跃来源的全部父目录FD，不遍历退役历史；ExitStack确保拒绝/错误/成功时同样释放。
# 函数用途: 为整次退役保留不会因路径替换而漂移的目录锚点。
def _retained_directories(root, sources, retained):
    anchors = {}
    prefixes = {parts[:depth] for parts in sources for depth in range(len(parts))}
    for parts in sorted(prefixes):
        descriptor = open_directory_beneath(root, parts, require_dir_fd=True)
        retained.callback(os.close, descriptor)
        info = os.fstat(descriptor)
        anchors[parts] = info.st_dev, info.st_ino
    return anchors


# LLM: 不刷新初始源/角色/代次/目录或完整P1基线；manifest写入之后、首个move之前仍执行同一末门。
# 函数用途: 在原锁内复核本次证明仍对应全部原来源。
def _require_snapshot(root, sources, before, generations, current, identities, anchors):
    for parts, identity in anchors.items():
        descriptor = open_directory_beneath(root, parts, require_dir_fd=True)
        try:
            info = os.fstat(descriptor)
            if (info.st_dev, info.st_ino) != identity:
                raise StandaloneSourceUnproven('retirement_sources_changed')
        finally:
            os.close(descriptor)
    if (sources != _source_paths(root)
            or before != {parts: _tree_stamp(root, parts) for parts in sources}
            or generations != _old_generations(root, current)):
        raise StandaloneSourceUnproven('retirement_sources_changed')
    for item in identities:
        if item['role'] == 'executor':
            _require_executor_source(item)


# LLM: 原生命周期锁及原ledger锁内固定整组来源；完整P2树观察通过且原源末核才归档，不补closure或授接班执行权。
# 函数用途: 启动之前只退役本次已证明停止的旧代活跃设施，所有未知责任原地保留。
def retire_dead_multiprocess_generations(paths, *, target_count: int, current_c_run_id: str | None = None,
                                       lifecycle=None) -> dict | None:
    lock_mode = _require_lock(paths, target_count, lifecycle)
    identities = []
    with ExitStack() as retained:
        try:
            generations = _old_generations(paths.root, current_c_run_id)
            facts = _retirement_required(paths.root, generations, target_count)
            if facts is None:
                return None
            sources = _source_paths(paths.root)
            anchors = _retained_directories(paths.root, sources, retained)
            before = {parts: _tree_stamp(paths.root, parts) for parts in sources}
            identities = _collect_identities(paths.root, retained)
            _prove(identities, lock_mode)
            _require_lock(paths, target_count, lifecycle)
            _require_snapshot(paths.root, sources, before, generations, current_c_run_id, identities, anchors)
        except (OSError, ValueError, TypeError, KeyError, StandaloneSourceUnproven) as error:
            reason = str(error) if isinstance(error, StandaloneSourceUnproven) else 'retirement_source_unreadable'
            observed = [{key: row.get(key, 'unknown') for key in ('role', 'pid', 'verdict')} for row in identities]
            print(json.dumps({'event': 'gateway_generation_retirement_refused', 'reason': reason,
                              'identities': observed}, sort_keys=True), file=sys.stderr)
            if target_count > 0:
                refusal = CoordinatorUnavailable(reason)
                refusal.reason = reason
                raise refusal from error
            raise StandaloneSourceUnproven(reason) from error
        manifest = {'schema_version': 'multiprocess_generation_retirement.v1', 'retired_at': time.time(),
                    'target_count': target_count, 'generations': facts,
                    'identities': [{key: row[key] for key in ('role', 'pid', 'verdict')} for row in identities],
                    'execution_proofs': [{'source': '/'.join(row['parts']), 'scope': row['scope'],
                        'record_sha256': hashlib.sha256(row['source'][0]).hexdigest(),
                        'evidence': row['exit_evidence']} for row in identities if row['role'] == 'executor'],
                    'moved': [], 'reason': 'multiprocess_to_standalone' if target_count == 0 else 'old_generation_incompatible'}
        archive = _archive_dir(paths.root, generations)
        _write_manifest(archive, manifest)
        _require_lock(paths, target_count, lifecycle)
        _require_snapshot(paths.root, sources, before, generations, current_c_run_id, identities, anchors)
        _move_sources(paths.root, archive, manifest)
        return manifest
