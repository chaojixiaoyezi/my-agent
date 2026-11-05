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


# LLM: 复用P1发现入口的strict nofollow字节/codec及摘要文件名绑定；缺count不猜分区权限，只核死亡，坏行整组拒绝。
# 函数用途: 读取所有执行者身份，包括已released或abandoned的记录。
def _executor_identities(root):
    identities = []
    try:
        for name in list_names_beneath(root, ('workers', 'executors')):
            if re.fullmatch(r'w(0|[1-9][0-9]*)', name) is None:
                raise ValueError('executor_partition_invalid')
            parts = ('workers', 'executors', name)
            for filename in list_names_beneath(root, parts):
                if filename == 'ledger.lock':
                    read_bytes_beneath(root, (*parts, filename), max_bytes=16384, require_dir_fd=True)
                    continue
                raw, _file_identity, _directory_identity = read_executor_source(root, (*parts, filename))
                row = deserialize_record(raw)
                if (row['schema_version'] not in {'multiprocess_executor_reservation.v1', 'multiprocess_executor_reservation.v2'}
                        or hashlib.sha256(row['reservation_id'].encode()).hexdigest() + '.json' != filename
                        or (row.get('execution_domain') is not None
                            and row['execution_domain']['source_worker_index'] != int(name[1:]))):
                    raise ValueError('executor_binding_invalid')
                if row['process_identity'] is not None:
                    identities.append({'role': 'executor', 'pid': row['process_identity']['pid'],
                                       'identity': row['process_identity']})
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise StandaloneSourceUnproven('retirement_executor_unreadable') from error
    return identities


# LLM: C缺完整身份只消费原锁，所有P4和W marker身份分别核，不因重复PID省略不同出生；不读取请求正文。
# 函数用途: 汇总本次必须证明已退出的角色和执行者。
def _collect_identities(root):
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
    identities.extend(_executor_identities(root))
    return identities


# LLM: 根锁证明C不再拥有设施，完整C身份仍不能live；W/P1只认False，unknown绝不当dead，不发信号。
# 函数用途: 为清单生产死亡结论，发现活或未知进程就拒绝整次移动。
def _prove(identities, lock_mode):
    if lock_mode not in {'standalone', 'coordinator'}:
        raise ValueError('retirement_lock_required')
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
        elif live is not False:
            raise StandaloneSourceUnproven('retirement_executor_alive_or_unknown')


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


# LLM: 只枚举固定白名单设施；请求、控制回执、ingress、responses、conversations与coordinator.lock永不进入移动集。
# 函数用途: 按合同顺序列出全部待移设施，gateway_state始终最后。
def _source_paths(root, *, include_state=True):
    names = list_names_beneath(root, ('workers',))
    known = {'registry.json', 'registry.json.lock', 'control.json', 'control.json.lock',
             'executors', 'generations', 'takeover_evidence'}
    if any(name not in known and re.fullmatch(r'w(0|[1-9][0-9]*)\.json(?:\.lock)?', name) is None
           for name in names):
        raise StandaloneSourceUnproven('retirement_unknown_source')
    result = [('workers', name) for name in names if name in {'control.json', 'control.json.lock'}]
    result.extend(('workers', name) for name in names if re.fullmatch(r'w(0|[1-9][0-9]*)\.json(?:\.lock)?', name))
    for directory in ('takeover_evidence', 'executors', 'generations'):
        result.extend(('workers', directory, name) for name in list_names_beneath(root, ('workers', directory)))
    result.extend(('workers', name) for name in names if name in {'registry.json', 'registry.json.lock'})
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


# LLM: 原锁内整组双读并证明死亡才创建manifest/移动；永不写closure，MP拒绝复用CoordinatorUnavailable，Count0复用既有source码。
# 函数用途: 启动之前退役所有已证明死亡且阻塞当前拓扑的旧多进程设施。
def retire_dead_multiprocess_generations(paths, *, target_count: int, current_c_run_id: str | None = None,
                                       lifecycle=None) -> dict | None:
    lock_mode = _require_lock(paths, target_count, lifecycle)
    identities = []
    try:
        generations = _old_generations(paths.root, current_c_run_id)
        facts = _retirement_required(paths.root, generations, target_count)
        if facts is None:
            return None
        sources = _source_paths(paths.root)
        before = [(parts, _tree_stamp(paths.root, parts)) for parts in sources]
        identities = _collect_identities(paths.root)
        _prove(identities, lock_mode)
        _require_lock(paths, target_count, lifecycle)
        if (sources != _source_paths(paths.root)
                or before != [(parts, _tree_stamp(paths.root, parts)) for parts in sources]
                or generations != _old_generations(paths.root, current_c_run_id)):
            raise StandaloneSourceUnproven('retirement_sources_changed')
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
                'moved': [], 'reason': 'multiprocess_to_standalone' if target_count == 0 else 'old_generation_incompatible'}
    archive = _archive_dir(paths.root, generations)
    _write_manifest(archive, manifest)
    _move_sources(paths.root, archive, manifest)
    return manifest
