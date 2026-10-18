# LLM: v1只提供count；v2仅原正常producer一次封存关闭范围，消费仍需当前身份/P1/fresh P2；原P4唯一current，Count0与旧字节不变。
# 模块用途: 沿唯一generation路径保存旧count与显式关闭事实，原raw CAS/读回失败不宣称完成，不给JSON执行权。
import hashlib
import os
import secrets
from contextlib import contextmanager

from ..common.nofollow_fs import (
    _openat_file,
    open_directory_beneath,
    read_bytes_beneath,
    write_bytes_atomic_beneath,
)
from .multiprocess_executor_ledger import _durable_namespace
from .multiprocess_records import _identifier, deserialize_record, serialize_record, validate_record

# v2只保存原范围摘要与P4，不复制业务正文；完整读回上限4MiB，超限拒绝，不截断或视为缺失。
_GENERATION_METADATA_MAX_BYTES = 4 * 1024 * 1024


# LLM: c_run_id先经原P0 identifier校验，文件名只用安全摘要；这不是新的generation/current/owner权限字段。
# 函数用途: 定位唯一代次元数据的规范相对引用。
def generation_metadata_parts(c_run_id):
    _identifier(c_run_id, 'c_run_id')
    return ('workers', 'generations', hashlib.sha256(c_run_id.encode()).hexdigest() + '.json')


# LLM: nofollow有界完整v1/v2+exact代次双读，v1仍纯count，关闭范围不是P2；坏字节/错代/漂移不转正常缺失。
# 函数用途: 读取历史count元数据和原字节，不创建目录、不授予接手权。
def read_generation_metadata(root, c_run_id):
    parts = generation_metadata_parts(c_run_id)
    content = read_bytes_beneath(root, parts, max_bytes=_GENERATION_METADATA_MAX_BYTES, require_dir_fd=True)
    if content is None:
        return None
    value = deserialize_record(content)
    # v1仍保原16KiB接受边界，扩展读回上限仅属于显式v2范围。
    if value['schema_version'] == 'multiprocess_c_generation.v1' and len(content) > 16384:
        raise ValueError('generation_metadata_v1_too_large')
    if (value['schema_version'] not in {'multiprocess_c_generation.v1', 'multiprocess_c_generation.v2'}
            or value['c_run_id'] != c_run_id):
        raise ValueError('generation_metadata_binding_conflict')
    if read_bytes_beneath(root, parts, max_bytes=_GENERATION_METADATA_MAX_BYTES, require_dir_fd=True) != content:
        raise ValueError('generation_metadata_readback_changed')
    return value, content


# LLM: 只受信原C实际lease可开短门；None旧纯格式publisher不增加archive I/O，复制/关闭/错source在锁/目录副作用前拒绝。
# 函数用途: 让原P4发布与实际撤权共门，保原lease→资源锁顺序。
@contextmanager
def generation_publication_scope(root, registry, source):
    if source is None:
        yield
        return
    require_generation_source(root, registry, source)
    with source._lifecycle_gate:
        require_generation_source(root, registry, source)
        yield


# LLM: 登记对象/原FD/root/count/本C PID/gen与实际P0身份全核；不用新current source替旧引用，不给metadata生命或执行语义。
# 函数用途: 在实际提交前核捕获的原C来源仍合法。
def require_generation_source(root, registry, source):
    from .multiprocess_coordinator import CoordinatorLease, current_coordinator
    from .multiprocess_executor_identity import process_birth

    if (type(source) is not CoordinatorLease or source.root != root
            or current_coordinator(root, registry['worker_count']) is not source
            or source.pid != os.getpid() or source.c_run_id != registry['c_run_id']
            or registry['process_identity']['pid'] != os.getpid()
            or registry['process_identity']['start_time'] != process_birth(os.getpid())):
        raise ValueError('generation_original_coordinator_required')


# LLM: 原C短门/registry锁内只读实际旧P4提取count；本代建v2初始，既有历史原字节保全，不降级或由新C补旧closure。
# 函数用途: 在覆盖当前登记前保存旧代及本次实际代次的唯一原P0元数据。
def archive_registry_generations(root, previous, candidate, source):
    require_generation_source(root, candidate, source)
    for registry in (previous, candidate):
        if registry is None:
            continue
        value = {'schema_version': 'multiprocess_c_generation.v1',
                 'c_run_id': registry['c_run_id'], 'worker_count': registry['worker_count']}
        if registry['c_run_id'] == source.c_run_id:
            value.update(schema_version='multiprocess_c_generation.v2', closure=None)
        value = validate_record(value)
        _publish_generation_once(root, value)
    require_generation_source(root, candidate, source)


# LLM: 仅创建纯count或v2初始，不接收closed字典；已有v2字节原样保全、count冲突拒绝，真实封存只由原runtime另一末门一次CAS。
# 函数用途: 幂等耐久创建本代初始记录，或保留既有历史；异常不宣称完成。
def _publish_generation_once(root, record):
    if record.get('schema_version') == 'multiprocess_c_generation.v2' and record.get('closure') is not None:
        raise ValueError('generation_original_closing_required')
    parts, content = generation_metadata_parts(record['c_run_id']), serialize_record(record)
    previous = read_generation_metadata(root, record['c_run_id'])
    if previous is not None:
        if previous[0]['worker_count'] != record['worker_count']:
            raise ValueError('generation_metadata_immutable_conflict')
        if previous[0]['schema_version'] == 'multiprocess_c_generation.v2':
            return
        if previous[1] != content:
            raise ValueError('generation_metadata_immutable_conflict')
        return
    _durable_namespace(root, parts[:-1])
    directory = open_directory_beneath(root, parts[:-1], require_dir_fd=True)
    temporary = '.' + parts[-1] + '.tmp-' + secrets.token_hex(16)
    descriptor = None
    try:
        descriptor = _openat_file(directory, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        _write_complete_generation(descriptor, content)
        os.close(descriptor)
        descriptor = None
        try:
            os.link(temporary, parts[-1], src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
        except FileExistsError:
            if read_generation_metadata(root, record['c_run_id']) != (record, content):
                raise ValueError('generation_metadata_immutable_conflict') from None
        os.unlink(temporary, dir_fd=directory)
        os.fsync(directory)
        if read_generation_metadata(root, record['c_run_id']) != (record, content):
            raise ValueError('generation_metadata_readback_changed')
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


# LLM: 原exclusive临时descriptor完整写+fsync后才可link；短写异常保持未知，不发布半条元数据。
# 函数用途: 完成同一个原文件的实际耐久写，不改current登记。
def _write_complete_generation(descriptor, content):
    remaining = memoryview(content)
    while remaining:
        count = os.write(descriptor, remaining)
        if count <= 0:
            raise OSError('generation_metadata_short_write')
        remaining = remaining[count:]
    os.fsync(descriptor)


# LLM: 只显式投影原四类已发表责任；辅助流/精确暂存不授关闭权，原完整expected/raw及生产者封口仍独立，同步namespace与生命周期测试。
# 函数用途: 为关闭或消费读取规范责任原字节，不把JSONL/原atomic暂存当单JSON，不创建锁或目录。
def generation_source_files(paths):
    import re

    from ..common.nofollow_fs import list_names_beneath
    from .multiprocess_drain_sources import original_directory_snapshot

    roots = [('admission', 'reservations'), ('requests', 'processing'), ('control_operations',)]
    names = list_names_beneath(paths.root, ('workers', 'executors'))
    if any(re.fullmatch(r'w(0|[1-9][0-9]*)', name) is None for name in names):
        raise ValueError('generation_executor_namespace_invalid')
    roots.extend(('workers', 'executors', name) for name in names)
    result = {}
    for parts in roots:
        _identity, rows = original_directory_snapshot(paths.root, parts, published_sources=True)
        for name, content in rows:
            result['/'.join((*parts, name))] = content
    if list_names_beneath(paths.root, ('workers', 'executors')) != names:
        raise ValueError('generation_executor_namespace_changed')
    return result


# LLM: 规范C和全W完整末态需与原P4逐项绑定；目录完整集合变化或缺marker保持未知，不读用户配置。
# 函数用途: 读回本代关闭角色原字节和唯一当前P4，供原producer核对。
def generation_role_files(root, count):
    from .multiprocess_takeover_sources import _role_marker_names

    expected = [f'w{i}.json' for i in range(count)]
    if sorted(_role_marker_names(root)) != sorted(expected):
        raise ValueError('generation_role_coverage_missing')
    roles = {}
    for name in ('gateway_state.json', *(f'workers/{name}' for name in expected)):
        content = read_bytes_beneath(root, tuple(name.split('/')), max_bytes=16384, require_dir_fd=True)
        if content is None:
            raise ValueError('generation_role_coverage_missing')
        roles[name] = content
    if sorted(_role_marker_names(root)) != sorted(expected):
        raise ValueError('generation_role_coverage_changed')
    return roles


# LLM: 仅接受本原runtime封口token，原lease→registry→原state锁均不等待；历史CAS/读回后才CAS最终marker，失败保未知。
# 函数用途: 将本代初始v2一次封存；任何写/读回失败均保未知，不回滚或补封旧代。
def seal_generation(runtime, token):
    from ..common.json_io import locked_json_path
    from .io import locked_file_transition

    runtime.require_generation_seal(token)
    root, source = runtime.control.paths.root, runtime._lease_origin
    record, expected = token['record'], token['expected']
    registry = record['closure']['registry']
    content = serialize_record(record)
    if len(content) > _GENERATION_METADATA_MAX_BYTES:
        raise ValueError('generation_closure_too_large')
    with source._lifecycle_gate:
        require_generation_source(root, registry, source)
        with locked_file_transition(root / 'workers' / 'registry.json', blocking=False):
            runtime.require_generation_seal(token)
            require_generation_source(root, registry, source)
            if read_generation_metadata(root, source.c_run_id) != expected:
                raise ValueError('generation_closure_compare_conflict')
            parts = generation_metadata_parts(source.c_run_id)
            write_bytes_atomic_beneath(root, parts, content)
            if read_generation_metadata(root, source.c_run_id) != (record, content):
                raise ValueError('generation_closure_readback_unconfirmed')
            runtime.require_generation_seal(token)
            with locked_json_path(root / 'gateway_state.json', blocking=False):
                actual = read_bytes_beneath(root, ('gateway_state.json',), max_bytes=16384, require_dir_fd=True)
                if actual != token['roles_before']['gateway_state.json']:
                    raise ValueError('generation_marker_compare_conflict')
                write_bytes_atomic_beneath(root, ('gateway_state.json',), token['roles']['gateway_state.json'])
                runtime.require_generation_seal(token)
                if read_bytes_beneath(root, ('gateway_state.json',), max_bytes=16384, require_dir_fd=True) != token['roles']['gateway_state.json']:
                    raise ValueError('generation_marker_readback_unconfirmed')
    return '/'.join(parts)


# LLM: 历史不授P2；旧字节优先immutable，committed仅原controller固定writer同门raw观察，不收编JSON自称或外部无dispatch。
# 函数用途: 对一个已封存旧代核对关闭来源，缺旧v2或任何漂移拒绝。
def require_closed_generation(paths, generation, count, current, captured=None, *, committed=(), covered=()):
    from .daemon_metadata import process_identity_is_live

    history = read_generation_metadata(paths.root, generation)
    if (history is None or history[0]['schema_version'] != 'multiprocess_c_generation.v2'
            or history[0]['closure'] is None or history[0]['worker_count'] != count
            or (captured is not None and history[1] != captured)):
        raise ValueError('old_role_lifecycle_unproven')
    closure = history[0]['closure']
    registry = closure['registry']
    identities = [registry['process_identity'], *(row['process_identity'] for row in registry['workers'])]
    if any(process_identity_is_live(identity) is not False for identity in identities):
        raise ValueError('old_role_lifecycle_unproven')
    files = generation_source_files(paths)
    _require_closed_files(closure['source_files'], files, current, committed, covered)
    for name, digest in closure['tree_evidence'].items():
        content = read_bytes_beneath(paths.root, tuple(name.split('/')),
                                     max_bytes=_GENERATION_METADATA_MAX_BYTES, require_dir_fd=True)
        if content is None or hashlib.sha256(content).hexdigest() != digest:
            raise ValueError('generation_tree_evidence_changed')
    if generation_source_files(paths) != files or read_generation_metadata(paths.root, generation) != history:
        raise ValueError('generation_closure_sources_changed')
    return history


# LLM: 旧关闭范围优先逐raw摘要；额外无dispatch控制只认原固定writer登记的完整raw，观察不授业务权限或覆盖旧项。
# 函数用途: 比较历史责任清单与当前规范目录，不修改P1/P3或重放请求。
def _require_closed_files(expected, actual, current, committed=(), covered=()):
    from ..common.strict_json import load_strict_json

    if any(name not in actual or hashlib.sha256(actual[name]).hexdigest() != digest
           for name, digest in expected.items()):
        raise ValueError('generation_closed_source_changed')
    original_commits, original_coverage = dict(committed), dict(covered)
    for name in actual.keys() - expected.keys():
        if original_coverage.get(name) == hashlib.sha256(actual[name]).hexdigest():
            continue
        if name.startswith('control_operations/') and original_commits.get(name) == actual[name]:
            continue
        value = load_strict_json(actual[name])
        if type(value) is not dict:
            raise ValueError('generation_closed_source_added')
        route = value.get('route') or value.get('dispatch') or {}
        if type(route) is not dict:
            raise ValueError('generation_closed_source_added')
        generation = value.get('c_run_id') if name.startswith('workers/executors/') else route.get('c_run_id')
        if generation != current:
            raise ValueError('generation_closed_source_added')


# LLM: 只合并原captured v2完整raw中的摘要，实际每代身份/源仍由require_closed_generation核；不从任意额外JSON收编。
# 函数用途: 在多代原关闭范围重叠时保留完整旧来源联合，冲突摘要不能互相覆盖。
def captured_generation_sources(root, captured):
    result = {}
    for generation, raw in captured:
        history = read_generation_metadata(root, generation)
        if (history is None or history[1] != raw or history[0]['schema_version'] != 'multiprocess_c_generation.v2'
                or history[0]['closure'] is None):
            raise ValueError('generation_captured_history_changed')
        for name, digest in history[0]['closure']['source_files'].items():
            if name in result and result[name] != digest:
                raise ValueError('generation_captured_source_conflict')
            result[name] = digest
    return tuple(sorted(result.items()))
