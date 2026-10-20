# LLM: 默认P6完整目录/raw不变；generation仅完整末核证实的typed名称集冲突可有限重读；首读None须确认名称已离开，叶异常/旧expected仍硬拒。
# 模块用途: 读取停止持久责任，并为generation区分原辅助流/暂存；两者均不授C/W、物理或封存权。
import hashlib
import os
import re
import stat

from ..common.nofollow_fs import (
    _openat_directory,
    _openat_file,
    _read_descriptor,
    open_directory_beneath,
    read_bytes_beneath,
)
from ..common.strict_json import load_strict_json
from .multiprocess_records import deserialize_record
from .multiprocess_route import validate_published_route
from .multiprocess_stop_drain import InFlightItem
from .multiprocess_stop_scope import CoordinatorStopScope
from .multiprocess_takeover_sources import discover_takeover_targets, takeover_namespace_state

# 原请求/控制可带结构化输入；完整有界读取，超限未知而不截断。
_DRAIN_SOURCE_READ_MAX_BYTES = 4 * 1024 * 1024


# LLM: 默认原FD完整names/raw路径保持；仅generation显式True可选固定责任投影，不改变P6消费者/expected或发权，同步两种模式回归。
# 函数用途: 默认冻结全部名称和字节；显式模式先区分原协议设施，避免把JSONL/半写暂存读成责任。
def original_directory_snapshot(root, parts, *, published_sources=False):
    if published_sources:
        return _published_directory_snapshot(root, parts)
    try:
        descriptor = open_directory_beneath(root, parts, require_dir_fd=True)
    except FileNotFoundError:
        return None, ()
    try:
        info = os.fstat(descriptor)
        names = _descriptor_names(descriptor)
        rows = tuple((name, _descriptor_child_bytes(descriptor, name)) for name in names)
        again = open_directory_beneath(root, parts, require_dir_fd=True)
        try:
            current = os.fstat(again)
            if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
                raise ValueError("drain_namespace_changed")
        finally:
            os.close(again)
        if _descriptor_names(descriptor) != names:
            raise ValueError("drain_namespace_changed")
        return (info.st_dev, info.st_ino), rows
    finally:
        os.close(descriptor)


# LLM: True仅接受原四类parts，缺目录前也先拒未知；此分类不是controller/role/permission来源，不新增namespace注册表。
# 函数用途: 检查显式投影是否定位规范责任目录，保持executor分区原格式。
def _generation_namespace(parts):
    if parts == ('admission', 'reservations'):
        return 'admission'
    if parts == ('requests', 'processing'):
        return 'processing'
    if parts == ('control_operations',):
        return 'control'
    if (type(parts) is tuple and len(parts) == 3 and parts[:2] == ('workers', 'executors')
            and type(parts[2]) is str and re.fullmatch(r'w(0|[1-9][0-9]*)', parts[2])):
        return 'executor'
    raise ValueError('generation_source_namespace_invalid')


# LLM: 名称只选需继续原codec核验的责任；request无统一强ID正则，不偷加前缀；control复用原公开locator，P1保原hash格式。
# 函数用途: 判断单层名称是否为本目录的原责任候选，不读取或认证记录内容。
def _generation_canonical_name(root, parts, name):
    from .control_operation_service import gateway_control_operation_path
    from .multiprocess_admission import reservation_file
    from .paths import gateway_paths_from_root

    if not name.endswith('.json') or not name[:-5] or '/' in name or '\\' in name:
        return False
    namespace = _generation_namespace(parts)
    paths = gateway_paths_from_root(root)
    if namespace == 'executor':
        return re.fullmatch(r'[0-9a-f]{64}\.json', name) is not None
    if namespace == 'control':
        try:
            return gateway_control_operation_path(paths, name[:-5]).name == name
        except ValueError:
            return False
    if namespace == 'admission':
        return reservation_file(paths, name[:-5]).name == name
    return (paths.processing / name).name == name


# LLM: 精确复用原claimed chunk locator往返，不检查流正文猜来源；孤儿/归档窗口仍是设施，不代表对应请求已完成。
# 函数用途: 检查processing原JSONL辅助流名称，保留历史非gwreq前缀ID。
def _generation_chunk_name(root, name):
    from .paths import claimed_request_chunk_path, gateway_paths_from_root

    suffix = '.chunks.jsonl'
    if not name.endswith(suffix) or not name[:-len(suffix)] or '/' in name or '\\' in name:
        return False
    paths = gateway_paths_from_root(root)
    request_id = name[:-len(suffix)]
    return claimed_request_chunk_path(paths.processing / (request_id + '.json'), request_id).name == name


# LLM: 只按原namespace真实writer两种精确stage和typed辅助设施分类；禁止泛suffix/内容猜测/任意predicate，所有未知名称拒绝。
# 函数用途: 在读raw前区分责任候选、普通设施和固定辅助目录，不把合法名字当写者证明。
def _generation_source_entry_kind(root, parts, name):
    namespace = _generation_namespace(parts)
    if _generation_canonical_name(root, parts, name):
        return 'source'
    if namespace == 'processing':
        if name in {'.commands', '.approvals'}:
            return 'directory'
        if _generation_chunk_name(root, name):
            return 'file'
    if namespace == 'executor' and name == 'ledger.lock':
        return 'file'
    if namespace in {'processing', 'control'} and name.endswith('.lock'):
        target = name[:-5]
        if (_generation_canonical_name(root, parts, target)
                or (namespace == 'processing' and _generation_chunk_name(root, target))):
            return 'file'
    pattern = r'\.(.+\.json)\.tmp-[0-9a-f]{32}' if namespace == 'executor' else r'\.(.+\.json)\.[0-9a-f]{32}\.tmp'
    stage = re.fullmatch(pattern, name)
    if stage is not None and _generation_canonical_name(root, parts, stage[1]):
        return 'file'
    raise ValueError('generation_source_name_invalid')


# LLM: 设施不读raw但仍沿同parent FD原nofollow opener核真实类型/单link及当前leaf身份；只精确设施自然消失可返回None，其它错误不吞。
# 函数用途: 取得原设施本次实际身份，禁止借辅助名称隐藏链接、FIFO或目录替换。
def _generation_facility_identity(descriptor, name, kind):
    try:
        child = (_openat_directory(descriptor, name) if kind == 'directory'
                 else _openat_file(descriptor, name, os.O_RDONLY, 0o600))
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(child)
        try:
            current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return None
        identity = info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)
        current_identity = current.st_dev, current.st_ino, stat.S_IFMT(current.st_mode)
        if identity != current_identity or (kind == 'file' and current.st_nlink != 1):
            raise ValueError('generation_source_facility_changed')
        return kind, *identity
    finally:
        os.close(child)


# LLM: 每次完整列名后都先分类/核形状，再取责任集合；设施生灭不进入raw，重复观察的设施身份另由snapshot比较，不刷新权限基线。
# 函数用途: 在准确父FD中投影已发表责任名称并保留设施身份供末次核对。
def _generation_source_names(root, parts, descriptor):
    published, facilities = [], {}
    for name in _descriptor_names(descriptor):
        kind = _generation_source_entry_kind(root, parts, name)
        if kind == 'source':
            published.append(name)
        else:
            facilities[name] = _generation_facility_identity(descriptor, name, kind)
    return tuple(published), facilities


# LLM: strict重开每个原prefix只比较本次身份，禁止late root/祖先替换后把同叶目录搬回洗成功；不创建路径或声明历史连续。
# 函数用途: 冻结显式投影目录从root到parent的完整身份链。
def _generation_directory_chain(root, parts):
    identities = []
    for depth in range(len(parts) + 1):
        descriptor = open_directory_beneath(root, parts[:depth], require_dir_fd=True)
        try:
            info = os.fstat(descriptor)
            identities.append((info.st_dev, info.st_ino))
        finally:
            os.close(descriptor)
    return tuple(identities)


# LLM: 仅本模块完整读取后证实的名称集冲突可触发有界重读；链/设施/叶异常不能转成此类型，也不授来源权。
# 类用途: 区分普通并发发表造成的名称变化与不可重读的安全或完整性失败。
class _PublishedNamesChanged(ValueError):
    pass


# LLM: 同次观察跨全部重读累积，消失不能擦除已见身份；自然设施生灭仍可接受，但同名再次出现必须原identity。
# 函数用途: 比较并记住本次快照中观察到的设施身份，不把重读当作新的设施授权基线。
def _remember_generation_facilities(observed, current):
    for name, identity in current.items():
        if identity is None:
            continue
        if name in observed and observed[name] != identity:
            raise ValueError('generation_source_facility_changed')
        observed[name] = identity


# LLM: 同原FD/首全链读取；仅reader返回None延后到末核，硬异常不捕；缺叶仍列名硬拒，确认离开才typed重读，旧expected不变。
# 函数用途: 完整核其它责任、链及设施后区分合法归档窗口；名称变化丢弃本轮全部字节，不混拼或补造责任。
def _published_source_rows(root, parts, descriptor, chain, facilities):
    if _generation_directory_chain(root, parts) != chain:
        raise ValueError('drain_namespace_changed')
    names, current_facilities = _generation_source_names(root, parts, descriptor)
    _remember_generation_facilities(facilities, current_facilities)
    rows, missing = [], []
    for name in names:
        content = read_bytes_beneath(root, (*parts, name), max_bytes=_DRAIN_SOURCE_READ_MAX_BYTES,
                                     require_dir_fd=True, snapshot_retries=2)
        if content is None:
            missing.append(name)
        else:
            rows.append((name, content))
    after, current_facilities = _generation_source_names(root, parts, descriptor)
    if _generation_directory_chain(root, parts) != chain:
        raise ValueError('drain_namespace_changed')
    _remember_generation_facilities(facilities, current_facilities)
    if any(name in after for name in missing):
        raise FileNotFoundError(next(name for name in missing if name in after))
    if after != names:
        raise _PublishedNamesChanged('drain_namespace_changed')
    return tuple(rows)


# LLM: generation只捕完整末核的typed名称冲突，最多3读；首次链/设施不刷新，同名仍在的None及reader异常/同inode/raw硬拒，默认P6不调用。
# 函数用途: 在固定原目录身份下有限重读并发发表，持续变化仍拒绝，不清隔离、改账或绕旧历史来源校验。
def _published_directory_snapshot(root, parts):
    _generation_namespace(parts)
    try:
        descriptor = open_directory_beneath(root, parts, require_dir_fd=True)
    except FileNotFoundError:
        return None, ()
    try:
        info = os.fstat(descriptor)
        identity = info.st_dev, info.st_ino
        chain = _generation_directory_chain(root, parts)
        if chain[-1] != identity:
            raise ValueError('drain_namespace_changed')
        facilities = {}
        for attempt in range(3):
            try:
                rows = _published_source_rows(root, parts, descriptor, chain, facilities)
            except _PublishedNamesChanged:
                if attempt == 2:
                    raise
                continue
            return identity, rows
    finally:
        os.close(descriptor)


# LLM: 名称来自同一sealed原目录FD，只是定位数据；后续原openat另核普通文件/no-follow与inode，不能按名字授信。
# 函数用途: 从准确已打开目录列完整子项，不借路径重开。
def _descriptor_names(descriptor):
    with os.scandir(descriptor) as entries:
        return tuple(sorted(entry.name for entry in entries))


# LLM: 成熟原openat leaf核普通单链接身份；完整有界read前后fstat及原父FD当前leaf相同，漂移不截断/忽略，不创建任何文件。
# 函数用途: 在准确原目录引用下读取稳定的一个完整原文件。
def _descriptor_child_bytes(parent, name):
    descriptor = _openat_file(parent, name, os.O_RDONLY, 0o600)
    try:
        before = os.fstat(descriptor)
        content = _read_descriptor(descriptor, _DRAIN_SOURCE_READ_MAX_BYTES)
        after = os.fstat(descriptor)
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
        if any(getattr(before, field) != getattr(after, field) or getattr(after, field) != getattr(current, field)
               for field in fields):
            raise ValueError('drain_source_changed')
        return content
    finally:
        os.close(descriptor)


# LLM: processing只取原GatewayPaths canonical字段，绝不拼Adapter旧root/processing；P1/P3/control各读唯一源，坏业务项不忽略。
# 函数用途: 从原准确路径取完整六类责任观察，不创建或回退第二队列。
def durable_drain_snapshot(paths, count):
    from .control_operation_service import gateway_control_operation_path
    processing_parts = tuple(paths.processing.relative_to(paths.root).parts)
    # 只借原格式合法的定位值取得原目录，不读取/创建这条操作，也不将其当责任来源。
    control_parts = tuple(gateway_control_operation_path(paths, 'gwctl-msg-' + '0' * 32).parent.relative_to(paths.root).parts)
    directories = tuple(original_directory_snapshot(paths.root, parts) for parts in (
        ("admission", "reservations"), processing_parts, control_parts))
    namespace = takeover_namespace_state(paths)
    targets = tuple(discover_takeover_targets(paths, count))
    return directories, namespace, targets


# LLM: 默认旧代严拒；closed_sources仅调用runtime本次核过的captured v2完整raw分类资料，completed须原件精确覆盖，不签物理/封存权。
# 函数用途: 把完整原持久责任转成六类事项，已关闭旧控制保原代且不伪装成当前settled。
def durable_items(scope, snapshot, *, closed_sources=()):
    directories, _namespace, _targets = snapshot
    items = []
    for filename, content in directories[0][1]:
        row = deserialize_record(content)
        if (row.get("schema_version") not in {"multiprocess_admission_reservation.v1", "multiprocess_admission_reservation.v2"}
                or row["request_id"] + ".json" != filename):
            raise ValueError("drain_admission_source_invalid")
        if _scope_route(scope, row["route"]):
            state = "settled" if row["state"] == "released" else "occupied"
            items.append(InFlightItem("foreground", scope, "admission/reservations/" + filename, state))
    for filename, content in directories[1][1]:
        if filename.endswith(".lock"):
            continue
        row = load_strict_json(content)
        if type(row) is not dict or row.get("id") != filename.removesuffix(".json"):
            raise ValueError("drain_request_source_invalid")
        if 'route' in row and not validate_published_route(row, scope.count, scope.c_run_id)['ok']:
            raise ValueError('drain_request_route_invalid')
        if _scope_route(scope, row.get("route")):
            items.append(InFlightItem("foreground", scope, "requests/processing/" + filename, "processing"))
    for filename, content in directories[2][1]:
        if filename.endswith(".lock"):
            continue
        row = load_strict_json(content)
        if type(row) is not dict or row.get("operation_id") != filename.removesuffix(".json"):
            raise ValueError("drain_control_source_invalid")
        from .control_operation_service import GatewayControlOperationReceipt
        receipt = GatewayControlOperationReceipt.from_dict(row)
        dispatch = receipt.dispatch
        if dispatch is not None and (dispatch['count'] != scope.count or dispatch['c_run_id'] != scope.c_run_id):
            if not _closed_control_source(scope, filename, content, receipt, closed_sources):
                raise ValueError('drain_control_generation_unconfirmed')
            continue
        if _scope_route(scope, dispatch):
            state = "settled" if row.get("state") == "completed" else "control_pending"
            items.append(InFlightItem("control", scope, "control_operations/" + filename, state))
    return tuple(items)


# LLM: 只比较原runtime本次核过的captured真实producer v2完整raw；准确源摘要可传递旧责任，保持原dispatch坐标，不推导祖代角色或生成proof。
# 函数用途: 判断旧completed控制完整原件是否已被原producer封存，默认空/错count/非terminal/缺项/摘要差异均保原拒绝。
def _closed_control_source(scope, filename, content, receipt, closed_sources):
    dispatch = receipt.dispatch
    if (receipt.state != 'completed' or dispatch['count'] != scope.count
            or dispatch['c_run_id'] == scope.c_run_id):
        return False
    worker = dispatch['worker']
    if worker != 'C' and (type(worker) is not int or not 0 <= worker < dispatch['count']):
        return False
    for generation, raw in closed_sources:
        value = deserialize_record(raw)
        if (generation == scope.c_run_id or value['c_run_id'] != generation
                or value['schema_version'] != 'multiprocess_c_generation.v2'
                or value['worker_count'] != dispatch['count'] or value['closure'] is None):
            continue
        expected = value['closure']['source_files'].get('control_operations/' + filename)
        if expected == hashlib.sha256(content).hexdigest():
            return True
    return False


# LLM: P3/Control各自原schema先核；无route默认C保留，空/未知/越界worker不能匹配失败后丢弃，旧代active由调用者保持unknown。
# 函数用途: 判断一条原责任是否落在本次准确停止角色。
def _scope_route(scope, route):
    if type(route) is not dict:
        if route is not None:
            raise ValueError('drain_original_route_invalid')
        return type(scope) is CoordinatorStopScope
    worker = route.get("worker")
    if worker != 'C' and (type(worker) is not int or not 0 <= worker < scope.count):
        raise ValueError('drain_original_route_invalid')
    return worker == ("C" if type(scope) is CoordinatorStopScope else scope.index)


# LLM: 目标自身dict不可作为来源印记；全部scope和P1原摘要须稳定，目录身份另由原snapshot约束。
# 函数用途: 对比前后完整持久来源是否仍是同一份规范事实。
def same_durable_snapshot(before, after):
    return (before[:2] == after[:2] and tuple((t.scope, t.digests) for t in before[2])
            == tuple((t.scope, t.digests) for t in after[2]))
