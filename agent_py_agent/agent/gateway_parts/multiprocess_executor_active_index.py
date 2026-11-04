"""原C活期的责任投影；canonical P1/P3和原lease仍是唯一权威。

冷读仅发现责任，磁盘结清hint不能复活活对象或签P2。热门只核命名空间
revision及active点读；已结清P1保留给P6，绝不删除原证据。
"""
import copy
import hashlib
import json
import os
import re
import stat
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager

from ..common.nofollow_fs import (
    open_directory_beneath,
    read_bytes_beneath,
    write_bytes_atomic_beneath,
)
from .multiprocess_executor_ledger import ExecutorLedger
from .multiprocess_records import record_digest, validate_record
from .multiprocess_route import WorkerPartition


# LLM: 仅原活动C在原mutex内创建或取得本PID投影；不从磁盘hint恢复可签发能力。
# 函数用途: 给原提交者取得同一个责任索引。
def index_for(controller):
    from .multiprocess_takeover_runtime import CoordinatorTakeover
    if type(controller) is not CoordinatorTakeover or not controller.active():
        raise ValueError('original_active_controller_required')
    with controller._mutex:
        index = getattr(controller, '_executor_active_index', None)
        if index is None:
            index = ExecutorActiveIndex(controller)
            controller._executor_active_index = index
        index.require_original()
        return index


# LLM: 只沿原root租约定位已存在投影；任何来源失效均返回未知，不新建替代权威。
# 函数用途: 让准入与核算共享当前C已经建立的索引。
def current_index(paths):
    from .multiprocess_coordinator import coordinator_for_root
    try:
        lease = coordinator_for_root(paths.root)
        controller = lease.controller if lease is not None else None
        index = getattr(controller, '_executor_active_index', None)
        if index is not None:
            index.require_original()
        return index
    except (OSError, ValueError, RuntimeError):
        return None


# LLM: nofollow目录FD只读取得身份或版本；缺失与替换不等同空目录，始终关闭FD。
# 函数用途: 固定成本检查原责任命名空间有没有变化。
def _directory(root, parts, *, revision):
    try:
        descriptor = open_directory_beneath(root, parts, require_dir_fd=True)
    except FileNotFoundError:
        return None
    try:
        stat = os.fstat(descriptor)
        return ((stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns) if revision
                else (stat.st_dev, stat.st_ino))
    finally:
        os.close(descriptor)


# LLM: 原目录FD与scandir同生命周期交给冷读取者；设施核验必须复用该FD，正常结束及异常均回收。
# 函数用途: 为冷回填提供可暂停、可严格核验设施的原目录流。
@contextmanager
def _names(root, parts):
    """保留严格目录FD的流式游标；每次只消费固定数量，不排序全历史。"""
    try:
        descriptor = open_directory_beneath(root, parts, require_dir_fd=True)
    except FileNotFoundError:
        yield None, iter(())
        return
    try:
        with os.scandir(descriptor) as iterator:
            yield descriptor, iterator
    finally:
        os.close(descriptor)


# LLM: 仅为原C活期可丢弃投影；原P1/P3、退出回执、pin事件与lease仍是授权依据。
# 函数用途: 分开管理尚未结清的责任和只供恢复核对的历史。
class ExecutorActiveIndex:
    # LLM: 只初始化固定设施和冷游标，不把历史hint加载成活对象或授予提交权。
    # 函数用途: 捕获原控制器、租约和命名空间初像。
    def __init__(self, controller):
        self._origin = self
        self.controller, self.lease = controller, controller._lease
        self.root, self.generation, self.count = controller.paths.root, controller.c_run_id, controller.count
        self.creator = os.getpid()
        from .multiprocess_executor_ledger import _durable_namespace
        # 固定设施在冷初始化建立；后续陌生目录变化不能被hint或空缓存收编。
        for parts in (('workers', 'executors'), ('admission', 'reservations')):
            _durable_namespace(self.root, parts)
        self.ready, self.failed = False, False
        self.executors, self.reservations = OrderedDict(), {}
        self._settled_p3 = {}
        # 原短门内先保留精确写责任；恢复只认本次旧像或尝试像，不重捕外部来源。
        self._write_attempts = {}
        self._cold_visited = 0
        self._groups, self._unresolved = {}, []
        self._partitions = set()
        self._closed_roles, self._metadata_stamps = {}, {}
        # 冷分类只是非授权历史投影；不恢复holder/descriptor/pin或原P3提交身份。
        self._closed_history = {}
        self._history_metadata = {}
        self._cold_lock = threading.RLock()
        self._cold_closed = False
        self._revision = self._revisions()
        self._cold = self._cold_rows()

    # LLM: 每次消费重核原对象、本PID、root、count、generation及未关闭lease，换代即拒绝。
    # 函数用途: 防止旧索引借用新控制器继续操作。
    def require_original(self):
        c = self.controller
        if (self._origin is not self or getattr(c, '_executor_active_index', None) is not self
                or self.creator != os.getpid() or c._lease is not self.lease or self.lease.controller is not c
                or c.paths.root != self.root or c.c_run_id != self.generation or c.count != self.count
                or not c.active() or getattr(self.lease, '_closing', False)):
            raise ValueError('original_active_controller_required')

    # LLM: 父级只核身份，规范叶及已发现分区核目录版本，不枚举P1/P3历史。
    # 函数用途: 取得当前责任目录的轻量版本向量。
    def _revisions(self):
        identity = ((), ('workers',), ('admission',))
        leaves = (('workers', 'executors'), ('admission', 'reservations'),
                  *(('workers', 'executors', name) for name in sorted(self._partitions)))
        return {parts: _directory(self.root, parts, revision=parts in leaves) for parts in (*identity, *leaves)}

    # LLM: 未经原提交登记的命名空间变化保持未知，不能由空缓存或过期时间放行。
    # 函数用途: 拒绝索引覆盖不了的新来源或目录换位。
    def _require_revision(self):
        if self._revisions() != self._revision:
            raise ValueError('responsibility_namespace_changed')

    # LLM: 只接收调用方已验证的规范写叶版本；补确认末采样必须等于冻结版本，其余目录及原目录身份仍不得变化。
    # 函数用途: 在原末提交后更新受影响目录的版本点，不接纳补确认期间的陌生追加。
    def _accept_write(self, changed, *, expected_revision=None):
        actual = self._revisions()
        if expected_revision is not None:
            confirmed = dict(actual)
            # 新分区在写前失败时可能尚未进入已知集合，仍按原尝试的准确路径点核。
            for parts in expected_revision.keys() - confirmed.keys():
                confirmed[parts] = _directory(self.root, parts, revision=True)
            if confirmed != expected_revision:
                raise ValueError('responsibility_namespace_changed')
        for parts, previous in self._revision.items():
            if parts in changed:
                if previous is not None and (actual[parts] is None or actual[parts][:2] != previous[:2]):
                    raise ValueError('responsibility_namespace_replaced')
            elif actual.get(parts) != previous:
                raise ValueError('responsibility_namespace_changed')
        self._revision = actual

    # LLM: 原C热门只点读active规范记录及原holder，并在前后核命名空间；未知来源仍保留。
    # 函数用途: 让短提交成本不再取决于已结清执行者历史。
    def check_sources(self):
        self.require_original()
        if not self.ready or self.failed:
            raise ValueError('responsibility_index_unknown')
        for key in tuple(self._write_attempts):
            self._reconcile_write(key)
        self._require_revision()
        for entry in self.executors.values():
            if entry['ledger'].read(entry['row']['reservation_id']) != entry['row']:
                raise ValueError('active_executor_canonical_changed')
            holder = entry['holder']
            if holder is not None:
                _require_holder(holder)
                if holder._executor_records.get(entry['row']['reservation_id']) != record_digest(entry['row']):
                    raise ValueError('active_executor_original_source_changed')
        for row in self.reservations.values():
            if self._p3(row['request_id']) != row:
                raise ValueError('active_p3_canonical_changed')
        for original in self.controller._unresolved_sources:
            original.require_unchanged()
        self._require_revision()

    # LLM: 保留原关闭角色核验与metadata原文件身份，不让历史投影替代原P6闭包。
    # 函数用途: 确认当前角色覆盖仍对应已核实的旧代记录。
    def check_roles(self, migration=None):
        from .multiprocess_takeover_sources import indexed_role_coverage
        for parts, original in self._metadata_stamps.items():
            if _file_stamp(self.root, parts) != original:
                raise ValueError('generation_original_metadata_changed')
        indexed_role_coverage(self.controller.paths, self.generation, self.count, self._closed_roles,
                              standalone=migration)

    # LLM: 只读准确request命名的规范P3；解析与验证沿用原来源读取，不消费hint。
    # 函数用途: 点读一个请求的真实预留记录。
    def _p3(self, identifier):
        from .multiprocess_takeover_sources import _read_admission_source
        return _read_admission_source(self.root, ('admission', 'reservations'), identifier + '.json')

    # LLM: 原T→C→lease区先冻结规范前像，随后锁设施创建或写后确认失败也保留同一责任。
    # 函数用途: 将本次P3写入及索引确认纳入可精确读回的同步作用域。
    @contextmanager
    def p3_write(self, record):
        row = validate_record(record)
        if row['state'] != 'occupied' or row['route']['c_run_id'] != self.generation:
            raise ValueError('original_p3_commit_required')
        attempt = {'kind': 'p3', 'row': copy.deepcopy(row),
                   'previous': copy.deepcopy(self.reservations.get(row['request_id']))}
        with self._write_scope(('p3', row['request_id']), attempt):
            yield

    # LLM: 仅原holder及完整CAS/FSM尝试可记责任；未确认发表不等同执行许可或耐久成功。
    # 函数用途: 覆盖P1规范写入到holder摘要及责任索引确认之间的窗口。
    @contextmanager
    def executor_write(self, ledger, record, holder, descriptor=None):
        row = ledger._bound_record(record)
        _require_holder(holder)
        previous = self.executors.get(_executor_key(row))
        if previous is not None and previous['holder'] is not holder:
            raise ValueError('original_executor_holder_changed')
        attempt = {'kind': 'executor', 'row': copy.deepcopy(row), 'ledger': ledger, 'holder': holder,
                   'previous': copy.deepcopy(previous['row']) if previous is not None else None,
                   'descriptor': previous['descriptor'] if previous is not None else descriptor}
        with self._write_scope(('executor', _executor_key(row)), attempt):
            yield

    # LLM: 保存的是本进程原来源引用，不是新权限账；错误路径读回失败仍保留尝试，下一次原检查先补确认。
    # 函数用途: 在任何命名空间副作用前登记精确责任，并保留原始写入异常。
    @contextmanager
    def _write_scope(self, key, attempt):
        self.require_original()
        if (not self.controller._mutex._is_owned() or not self.lease._lifecycle_gate._is_owned()
                or key in self._write_attempts):
            raise ValueError('original_write_scope_required')
        self._require_revision()
        if self._attempt_current(attempt) != attempt['previous']:
            raise ValueError('original_write_canonical_changed')
        self._write_attempts[key] = attempt
        try:
            yield
        except BaseException:
            try:
                attempt['revision'] = self._attempt_revisions(attempt)
                self._reconcile_write(key)
            except (OSError, ValueError, TypeError, KeyError):
                pass
            raise
        else:
            # 原写者已在同一短门内接受此版本；确认读取失败不能留下缺revision的责任。
            attempt['revision'] = dict(self._revision)
            try:
                if self._attempt_revisions(attempt) != attempt['revision']:
                    raise ValueError('responsibility_namespace_changed')
                self._reconcile_write(key)
            except OSError:
                # 只按已冻结版本补核一次，绝不重捕后来的目录变化；原I/O异常仍原样返回。
                try:
                    self._reconcile_write(key)
                except (OSError, ValueError, TypeError, KeyError):
                    pass
                raise

    # LLM: 原写门内固定本次副作用后的版本；新分区尚未登记时也单独核验，不枚举历史目录。
    # 函数用途: 防止延后读回期间把同目录的陌生追加当成本次写入。
    def _attempt_revisions(self, attempt):
        actual = self._revisions()
        if attempt['kind'] == 'executor':
            namespace = ('workers', 'executors', f"w{attempt['ledger'].partition.index}")
            actual[namespace] = _directory(self.root, namespace, revision=True)
        return actual

    # LLM: 缺文件仅是本次新建的允许前像；沿原nofollow点读，不创建目录、锁或规范文件。
    # 函数用途: 读取一次尝试对应的唯一canonical路径。
    def _attempt_current(self, attempt):
        if attempt['kind'] == 'executor':
            return attempt['ledger'].read(attempt['row']['reservation_id'])
        parts = ('admission', 'reservations', attempt['row']['request_id'] + '.json')
        raw = read_bytes_beneath(self.root, parts, max_bytes=16384, require_dir_fd=True)
        return validate_record(json.loads(raw)) if raw is not None else None

    # LLM: 只容原前像/本次完整后像与已知锁设施；末接纳也绑定尝试冻结版本，仍由原_accept_write核验目录身份。
    # 函数用途: 对已知写入补观察，不声明耐久成功、不发布路由，也不清任何隔离标记。
    def _reconcile_write(self, key):
        self.require_original()
        attempt = self._write_attempts[key]
        if self._attempt_revisions(attempt) != attempt.get('revision'):
            raise ValueError('responsibility_namespace_changed')
        row = self._attempt_current(attempt)
        if row not in (attempt['previous'], attempt['row']):
            raise ValueError('original_write_canonical_changed')
        if self._attempt_revisions(attempt) != attempt['revision']:
            raise ValueError('responsibility_namespace_changed')
        if attempt['kind'] == 'p3':
            self._accept_write({('admission',), ('admission', 'reservations')},
                               expected_revision=attempt['revision'])
            if row is not None:
                self.reservations[row['request_id']] = copy.deepcopy(row)
        else:
            ledger, holder = attempt['ledger'], attempt['holder']
            _require_holder(holder)
            namespace = ('workers', 'executors', f'w{ledger.partition.index}')
            if _directory(self.root, namespace, revision=False) is not None:
                self._partitions.add(namespace[-1])
            self._accept_write({('workers',), ('workers', 'executors'), namespace},
                               expected_revision=attempt['revision'])
            if row is not None:
                holder._executor_records[row['reservation_id']] = record_digest(row)
                self.executors[_executor_key(row)] = {
                    'ledger': ledger, 'row': copy.deepcopy(row), 'holder': holder,
                    'descriptor': attempt['descriptor'], 'revision': object()}
        del self._write_attempts[key]

    # LLM: 原C与lease末门内核对当前代canonical原像，登记不产生第二份预留权限。
    # 函数用途: 把刚成功发布的P3纳入同一个活跃责任集合。
    def remember_p3(self, record, request=None):
        """只从原C末提交记当前代canonical；request仅保调用合同，不产生权限。"""
        self.require_original()
        c, row = self.controller, validate_record(record)
        if (not c._mutex._is_owned() or not self.lease._lifecycle_gate._is_owned()
                or row['route']['c_run_id'] != self.generation or row['state'] == 'released'
                or self._p3(row['request_id']) != row):
            raise ValueError('original_p3_commit_required')
        previous = self.reservations.get(row['request_id'])
        if previous is not None and (previous['owner_sha'] != row['owner_sha']
                or previous['request_id'] != row['request_id']):
            raise ValueError('original_p3_binding_changed')
        self.reservations[row['request_id']] = copy.deepcopy(row)
        self._accept_write({('admission',), ('admission', 'reservations')})

    # LLM: 只返回本代原索引尚待结清的规范原像，包含写入已完成而hint尚未确认的released；不授释放权。
    # 函数用途: 为原回收循环提供活跃及失败待重放责任，不扫描历史目录。
    def pending_p3(self):
        with self.controller._mutex:
            self.check_sources()
            return tuple(copy.deepcopy(row) for row in self.reservations.values())

    # LLM: 原P3状态仍是占用权威；dirty released仅留给原重放者，不伪装成另一份occupied记录。
    # 函数用途: 给准入和额度提供同一已核的当前占用集合。
    def active_p3(self):
        return tuple(row for row in self.pending_p3() if row['state'] == 'occupied')

    # LLM: 只在原T→C→lease写入失败窗口调用；必须核原索引、规范终态和精确old/new原像，不声称耐久或退役。
    # 函数用途: 留住释放写入结果不确定时的真实原像，供同身份补同步及结清。
    def retain_release_attempt(self, record, terminal):
        self.require_original()
        row, c = validate_record(record), self.controller
        if (not c._mutex._is_owned() or not self.lease._lifecycle_gate._is_owned()
                or row['state'] != 'released' or not isinstance(terminal, dict)
                or terminal.get('id') != row['request_id']):
            raise ValueError('original_p3_settlement_required')
        old = self.reservations.get(row['request_id'])
        if old is None and self._settled_p3.get(row['request_id']) != row:
            raise ValueError('original_p3_settlement_unknown')
        if old is not None and {k: v for k, v in old.items() if k not in {'state', 'terminal_fact'}} != {
                k: v for k, v in row.items() if k not in {'state', 'terminal_fact'}}:
            raise ValueError('original_p3_settlement_conflict')
        from .multiprocess_records import release_admission

        parts = (c.paths.terminal / (row['request_id'] + '.json')).relative_to(self.root).parts
        raw = read_bytes_beneath(self.root, parts, max_bytes=16 * 1024 * 1024, require_dir_fd=True)
        actual = self._p3(row['request_id'])
        if (raw is None or json.loads(raw) != terminal or release_admission(old or row, terminal) != row
                or actual not in (old, row)):
            raise ValueError('original_p3_terminal_required')
        self.reservations[row['request_id']] = copy.deepcopy(actual)
        self._accept_write({('admission',), ('admission', 'reservations')})

    # LLM: 调用方先完成原计账与P3耐久写；hint失败只留dirty原像，不删除原P3、不重复计账。
    # 函数用途: 在原末门持久结清同一个P3索引成员。
    def settle_p3(self, record, terminal):
        row = validate_record(record)
        self.retain_release_attempt(row, terminal)
        if self.reservations[row['request_id']] != row:
            raise ValueError('original_p3_settlement_required')
        self._persist('p3', row['request_id'], {'record': row, 'terminal_sha256': _json_digest(terminal)})
        self._settled_p3[row['request_id']] = copy.deepcopy(row)
        self.reservations.pop(row['request_id'], None)

    # LLM: 原C锁内仅接收已耐久P1及原holder；精确descriptor随首次提交捕获，不事后补证。
    # 函数用途: 登记真实执行者的当前责任和退出观察接点。
    def remember_executor(self, ledger, row, holder, descriptor=None):
        self.require_original()
        if (not self.controller._mutex._is_owned() or row['c_run_id'] != self.generation
                or ledger.root != self.root or ledger.read(row['reservation_id']) != row):
            raise ValueError('original_executor_commit_required')
        _require_holder(holder)
        key = _executor_key(row)
        previous = self.executors.get(key)
        if previous is not None:
            if previous['holder'] is not holder:
                raise ValueError('original_executor_holder_changed')
            descriptor = previous['descriptor']
        self._partitions.add(f'w{ledger.partition.index}')
        self.executors[key] = {'ledger': ledger, 'row': copy.deepcopy(row), 'holder': holder,
                               'descriptor': descriptor, 'revision': object()}
        self._accept_write({('workers',), ('workers', 'executors'),
                            ('workers', 'executors', f'w{ledger.partition.index}')})

    # LLM: hint只作不可变结清证据；写失败保active，重放不接受不同原像，不授P2能力。
    # 函数用途: 原末门把同一条结清事实可靠留在磁盘上。
    def _persist(self, kind, identifier, value):
        """每条独立不可变hint；失败保active，重放只接受完全相同原件。"""
        key = hashlib.sha256((self.generation + ':' + kind + ':' + identifier).encode()).hexdigest()
        parts = ('responsibility_index', 'settlements', key + '.json')
        content = json.dumps({'schema': 'executor_settlement.v1', 'generation': self.generation,
                              'kind': kind, **value}, sort_keys=True, separators=(',', ':')).encode()
        old = read_bytes_beneath(self.root, parts, max_bytes=65536, require_dir_fd=True)
        if old is not None and old != content:
            raise ValueError('immutable_settlement_conflict')
        if old is None:
            write_bytes_atomic_beneath(self.root, parts, content)

    # LLM: 门外逐项发现原P1/P3；每项含设施均计入预算，设施复用P6严格分类及同父FD核验，未知仍拒绝。
    # 函数用途: 按后台预算恢复真实责任，不把规范锁和暂存文件误读成业务记录。
    def _cold_rows(self):
        from ..user_space.owner_resolver import OwnerIdentity
        from .multiprocess_drain_sources import (
            _generation_facility_identity,
            _generation_source_entry_kind,
        )
        from .multiprocess_route import owner_digest
        from .multiprocess_takeover_sources import (
            _discover_record,
            _discovery_registry,
            _read_admission_source,
        )
        from .multiprocess_takeover_unresolved_source import UnresolvedExecutorSource
        registry, generations = _discovery_registry(self.root), {}
        with _names(self.root, ('workers', 'executors')) as (_, partitions):
            for item in partitions:
                name = item.name
                if re.fullmatch(r'w(0|[1-9][0-9]*)', name) is None:
                    raise ValueError('executor_namespace_unconfirmed')
                self._partitions.add(name)
                # 新增已发现的分区只能来自本轮固定初像，不把热写变化收编。
                self._revision[('workers', 'executors', name)] = _directory(
                    self.root, ('workers', 'executors', name), revision=True)
                yield
                lock_seen = False
                parts = ('workers', 'executors', name)
                with _names(self.root, parts) as (descriptor, records):
                    for record in records:
                        kind = _generation_source_entry_kind(self.root, parts, record.name)
                        if kind != 'source':
                            _generation_facility_identity(descriptor, record.name, kind)
                            lock_seen = lock_seen or record.name == 'ledger.lock'
                            yield
                            continue
                        row = _discover_record(self.root, self.count, ('workers', 'executors', name),
                                               record.name, self.generation, registry, generations)
                        if type(row) is UnresolvedExecutorSource:
                            self._unresolved.append(row)
                        else:
                            role = 'C' if row['owner_sha'] == owner_digest(OwnerIdentity.local_main()) else 'W'
                            key = row['c_run_id'], role, None if role == 'C' else int(name[1:])
                            self._groups.setdefault(key, []).append(row)
                            ledger = ExecutorLedger(self.root, WorkerPartition(int(name[1:]), self.count, row['c_run_id']))
                            self.executors[_executor_key(row)] = {'ledger': ledger, 'row': row, 'holder': None,
                                                                  'descriptor': None, 'revision': object()}
                        yield
                if not lock_seen:
                    raise ValueError('executor_namespace_unconfirmed')
        parts = ('admission', 'reservations')
        with _names(self.root, parts) as (descriptor, records):
            for item in records:
                kind = _generation_source_entry_kind(self.root, parts, item.name)
                if kind == 'source':
                    row = _read_admission_source(self.root, parts, item.name)
                    if row['state'] != 'released':
                        self.reservations[row['request_id']] = row
                else:
                    _generation_facility_identity(descriptor, item.name, kind)
                yield
        if _discovery_registry(self.root) != registry:
            raise ValueError('executor_registry_changed')
        from .multiprocess_generation_metadata import read_generation_metadata
        if any(read_generation_metadata(self.root, generation) != original for generation, original in generations.items()):
            raise ValueError('executor_generation_metadata_changed')
        self._audited_roles = yield from self._audit_closed_roles()
        yield from self._classify_closed_executors()

    # LLM: 门外沿原v2闭包逐项核文件、角色和进程身份，未齐全前不发表ready状态。
    # 函数用途: 分批复核旧代已经关闭的完整证据。
    def _audit_closed_roles(self):
        """旧v2完整闭包也分批审计；不在最后一次tick重扫所有P1/P3。"""
        from .daemon_metadata import process_identity_is_live
        from .multiprocess_generation_metadata import (
            _require_closed_files,
            read_generation_metadata,
        )
        from .multiprocess_takeover_sources import (
            _discovery_registry,
            _require_closed_markers,
            _role_snapshot,
        )
        from .standalone_migration import current_standalone_migration
        c = self.controller
        migration = current_standalone_migration(c)
        roles, registry = _role_snapshot(self.root), _discovery_registry(self.root)
        if migration is not None:
            migration.require_role_scope(c.paths, self.generation, self.count)
            roles = migration.filter_roles(roles)
        generations = {json.loads(raw).get('c_run_id') for raw in roles.values()}
        if registry is not None:
            generations.add(registry[0]['c_run_id'])
        generations.discard(self.generation)
        if None in generations:
            raise ValueError('old_role_lifecycle_unproven')
        captured, expected = [], {}
        for generation in sorted(generations):
            history = read_generation_metadata(self.root, generation)
            if (history is None or history[0]['schema_version'] != 'multiprocess_c_generation.v2'
                    or history[0]['closure'] is None or history[0]['worker_count'] != self.count):
                raise ValueError('old_role_lifecycle_unproven')
            closure = history[0]['closure']
            registered = closure['registry']
            identities = [registered['process_identity'], *(r['process_identity'] for r in registered['workers'])]
            if any(process_identity_is_live(identity) is not False for identity in identities):
                raise ValueError('old_role_lifecycle_unproven')
            if set(roles) != set(closure['role_markers']):
                raise ValueError('old_role_lifecycle_unproven')
            _require_closed_markers(roles, closure, generation, self.generation)
            if registry is not None and registry[0]['c_run_id'] == generation and registry[1] != closure['registry_sha256']:
                raise ValueError('generation_closed_registry_changed')
            captured.append((generation, history[1]))
            for name, digest in closure['source_files'].items():
                if name in expected and expected[name] != digest:
                    raise ValueError('generation_captured_source_conflict')
                expected[name] = digest
                yield
            for name, digest in closure['tree_evidence'].items():
                raw = read_bytes_beneath(self.root, tuple(name.split('/')), max_bytes=16 * 1024 * 1024,
                                         require_dir_fd=True)
                if raw is None or hashlib.sha256(raw).hexdigest() != digest:
                    raise ValueError('generation_tree_evidence_changed')
                yield
        if captured:
            roots = [('admission', 'reservations'), ('requests', 'processing'), ('control_operations',)]
            roots.extend(('workers', 'executors', name) for name in sorted(self._partitions))
            stamps = {parts: _directory(self.root, parts, revision=True) for parts in roots}
            first, facilities = {}, {}
            for pass_number in range(2):
                missing = set(expected)
                seen = set()
                for parts in roots:
                    if _directory(self.root, parts, revision=True) != stamps[parts]:
                        raise ValueError('generation_closure_sources_changed')
                    from .multiprocess_drain_sources import (
                        _generation_facility_identity,
                        _generation_source_entry_kind,
                    )
                    if stamps[parts] is None:
                        continue
                    descriptor = open_directory_beneath(self.root, parts, require_dir_fd=True)
                    try:
                        with os.scandir(descriptor) as entries:
                            for entry in entries:
                                kind = _generation_source_entry_kind(self.root, parts, entry.name)
                                name = '/'.join((*parts, entry.name))
                                if kind != 'source':
                                    identity = _generation_facility_identity(descriptor, entry.name, kind)
                                    if identity is not None:
                                        if name in facilities and facilities[name] != identity:
                                            raise ValueError('generation_source_facility_changed')
                                        facilities[name] = identity
                                    yield
                                    continue
                                raw = read_bytes_beneath(self.root, (*parts, entry.name), max_bytes=4 * 1024 * 1024,
                                                         require_dir_fd=True, snapshot_retries=2)
                                if raw is None:
                                    raise ValueError('generation_closed_source_changed')
                                _require_closed_files({name: expected[name]} if name in expected else {},
                                                      {name: raw}, self.generation)
                                digest = hashlib.sha256(raw).hexdigest()
                                if pass_number == 0:
                                    first[name] = digest
                                elif first.get(name) != digest:
                                    raise ValueError('generation_closure_sources_changed')
                                seen.add(name)
                                missing.discard(name)
                                yield
                    finally:
                        os.close(descriptor)
                    if _directory(self.root, parts, revision=True) != stamps[parts]:
                        raise ValueError('generation_closure_sources_changed')
                if missing or (pass_number == 1 and len(first) != len(seen)):
                    raise ValueError('generation_closed_source_changed')
            for parts, original in stamps.items():
                if _directory(self.root, parts, revision=True) != original:
                    raise ValueError('generation_closure_sources_changed')
        for generation, raw in captured:
            if read_generation_metadata(self.root, generation)[1] != raw:
                raise ValueError('generation_closure_sources_changed')
            yield
        self._cold_source_digests = expected
        return tuple(captured)

    # LLM: 只把完整原v2闭包、继承覆盖和本次fresh P2共同核齐的旧代归非授权历史；不补原pin或信hint。
    # 函数用途: 在门外按游标逐项分类已关闭旧P1，缺任一逐请求证据仍保active。
    def _classify_closed_executors(self):
        from . import multiprocess_takeover as p2
        from .daemon_metadata import process_identity_is_live
        from .multiprocess_generation_metadata import (
            generation_metadata_parts,
            read_generation_metadata,
        )
        from .multiprocess_takeover_recovery import RECHECK_BUDGET
        from .multiprocess_takeover_runtime import _current_receipt
        from .multiprocess_takeover_sources import _target, confirmed_evidence

        coverage = self._cold_source_digests
        for group, rows in sorted(self._groups.items()):
            generation = group[0]
            if generation == self.generation:
                continue
            history = read_generation_metadata(self.root, generation)
            if (history is None or history[0]['schema_version'] != 'multiprocess_c_generation.v2'
                    or history[0]['closure'] is None or history[0]['worker_count'] != self.count):
                yield
                continue
            metadata_parts = generation_metadata_parts(generation)
            metadata_stamp = _file_stamp(self.root, metadata_parts)
            if read_generation_metadata(self.root, generation) != history:
                yield
                continue
            closure = history[0]['closure']
            registry = closure['registry']
            identities = [registry['process_identity'], *(r['process_identity'] for r in registry['workers'])]
            if any(process_identity_is_live(identity) is not False for identity in identities):
                yield
                continue
            # 多代继承按最新原闭包已审计的全集逐摘要覆盖，不能只按当前marker代次丢更老P1。
            complete = True
            for name, digest in closure['source_files'].items():
                raw = read_bytes_beneath(self.root, tuple(name.split('/')), max_bytes=4 * 1024 * 1024,
                                         require_dir_fd=True)
                if (coverage.get(name) != digest or raw is None or hashlib.sha256(raw).hexdigest() != digest):
                    complete = False
                yield
            for name, digest in closure['tree_evidence'].items():
                raw = read_bytes_beneath(self.root, tuple(name.split('/')), max_bytes=16 * 1024 * 1024,
                                         require_dir_fd=True)
                if raw is None or hashlib.sha256(raw).hexdigest() != digest:
                    complete = False
                yield
            if not complete or read_generation_metadata(self.root, generation) != history:
                continue
            target = _target(self.root, self.count, group, rows)
            try:
                started = time.monotonic()
                result = p2.verify_takeover(target.ledger, target.scope, budget=dict(RECHECK_BUDGET))
                if not _current_receipt(self.controller, target, result, (started, time.monotonic())):
                    raise ValueError('cold_original_receipt_required')
                detail = confirmed_evidence(target, result)
                if detail['record_digests'] != target.digests:
                    raise ValueError('cold_original_p1_changed')
            except (OSError, ValueError, TypeError, KeyError):
                yield
                continue
            yield
            for row in rows:
                key = _executor_key(row)
                entry = self.executors[key]
                try:
                    projection = self._closed_request_projection(entry, closure)
                    if (_file_stamp(self.root, metadata_parts) != metadata_stamp
                            or entry['ledger'].read(row['reservation_id']) != row):
                        raise ValueError('cold_original_source_changed')
                    self.require_original()
                    self._require_revision()
                    if self.controller._recheck_stop.is_set():
                        raise ValueError('cold_original_source_stopped')
                    self._closed_history[key] = projection
                    self._history_metadata[generation] = history[1]
                    del self.executors[key]
                except (OSError, ValueError, TypeError, KeyError):
                    pass
                yield

    # LLM: 原closure必须覆盖准确P1/P3，canonical终态受released摘要绑定；owner/attempt/epoch/route逐字段核齐。
    # 函数用途: 点读旧请求结清事实，只返回无权限历史数据，不填写_settled_p3或伪造原pin。
    def _closed_request_projection(self, entry, closure):
        from .multiprocess_records import release_admission

        row, ledger = entry['row'], entry['ledger']
        if entry['holder'] is not None or entry['descriptor'] is not None or row['c_run_id'] == self.generation:
            raise ValueError('cold_history_only_required')
        p3_raw = None
        for parts in (ledger._parts(row['reservation_id']),
                      ('admission', 'reservations', row['request_id'] + '.json')):
            raw = read_bytes_beneath(self.root, parts, max_bytes=4 * 1024 * 1024, require_dir_fd=True)
            if raw is None or closure['source_files'].get('/'.join(parts)) != hashlib.sha256(raw).hexdigest():
                raise ValueError('cold_closed_source_required')
            p3_raw = raw
        p3 = self._p3(row['request_id'])
        parts = (self.controller.paths.terminal / (row['request_id'] + '.json')).relative_to(self.root).parts
        raw = read_bytes_beneath(self.root, parts, max_bytes=16 * 1024 * 1024, require_dir_fd=True)
        terminal = json.loads(raw) if raw is not None else None
        if (json.loads(p3_raw) != p3 or p3['state'] != 'released' or p3['request_id'] != row['request_id']
                or p3['owner_sha'] != row['owner_sha'] or p3['route']['c_run_id'] != row['c_run_id']
                or p3['route']['worker'] != ('C' if row['owner_sha'] == _main_owner_sha() else ledger.partition.index)
                or not isinstance(terminal, dict) or terminal.get('route') != p3['route']
                or terminal.get('execution_attempt_id') != row['execution_attempt_id']
                or terminal.get('lease_epoch') != row['lease_epoch'] or release_admission(p3, terminal) != p3):
            raise ValueError('cold_closed_request_required')
        if (self._p3(row['request_id']) != p3 or read_bytes_beneath(
                self.root, parts, max_bytes=16 * 1024 * 1024, require_dir_fd=True) != raw):
            raise ValueError('cold_closed_request_changed')
        return {'record': copy.deepcopy(row), 'p3_sha256': record_digest(p3),
                'terminal_sha256': _json_digest(terminal), 'kind': 'closed_generation_history'}

    # LLM: 每拍最多消费指定数量冷来源，失败锁定unknown并关闭游标，进度hint不能单独授权。
    # 函数用途: 推进一次可重放的冷回填维护。
    def backfill(self, limit):
        with self._cold_lock:
            if self._cold_closed:
                raise ValueError('responsibility_cold_cursor_closed')
            return self._backfill_locked(limit)

    # LLM: 调用者独占冷游标锁；关闭与next/StopIteration收尾串行，不并发操作同一生成器。
    # 函数用途: 在唯一游标窗口内推进固定批并发表完成状态。
    def _backfill_locked(self, limit):
        self.require_original()
        if self.failed or self.ready:
            return 0
        visited = 0
        try:
            self._require_revision()
            try:
                for _ in range(limit):
                    next(self._cold)
                    visited += 1
            except StopIteration:
                self._finish_cold()
            self._require_revision()
            self._cold_visited += visited
            write_bytes_atomic_beneath(self.root, ('responsibility_index', 'cold_cursor.json'),
                                       json.dumps({'generation': self.generation, 'visited': self._cold_visited,
                                                   'ready': self.ready}, sort_keys=True).encode())
            return visited
        except (OSError, ValueError, TypeError, KeyError):
            self.failed, self.ready = True, False
            self.close()
            raise

    # LLM: 仅门外冷审计完成后在原C锁内发表投影；quarantine和原关闭来源仍完整保留。
    # 函数用途: 将已经核实的冷发现结果交给原接管状态机。
    def _finish_cold(self):
        from .multiprocess_takeover_runtime import (
            _queue_target_recheck,
            _refresh_unresolved_sources,
            _scope_keys,
        )
        from .multiprocess_takeover_sources import _target
        from .standalone_migration import current_standalone_migration
        c = self.controller
        # 完整旧闭包审计只在冷维护门外；未完成之前所有owner热门明确unknown。
        migration = current_standalone_migration(c)
        if migration is not None:
            migration.require_unchanged()
        roles = self._audited_roles
        from .multiprocess_generation_metadata import generation_metadata_parts
        for generation, raw in {**dict(roles), **self._history_metadata}.items():
            parts = generation_metadata_parts(generation)
            before = _file_stamp(self.root, parts)
            if read_bytes_beneath(self.root, parts, max_bytes=16 * 1024 * 1024, require_dir_fd=True) != raw:
                raise ValueError('generation_original_metadata_changed')
            if generation in dict(roles):
                self._closed_roles[generation] = json.loads(raw)['closure']
            if _file_stamp(self.root, parts) != before:
                raise ValueError('generation_original_metadata_changed')
            self._metadata_stamps[parts] = before
        targets = [_target(self.root, self.count, key, rows) for key, rows in sorted(self._groups.items())]
        covered = {(t.scope['c_run_id'], t.scope['worker_index'] if t.scope['role'] == 'W' else 'C', owner)
                   for t in targets for owner in t.scope['owner_shas']}
        from .multiprocess_processing_recovery import original_claim_covers_reservation
        unproven = {row['owner_sha'] for row in self.reservations.values()
                    if (row['route']['c_run_id'], row['route']['worker'], row['owner_sha']) not in covered
                    and not original_claim_covers_reservation(c.paths, row, targets)}
        self._require_revision()
        with c._mutex:
            self.require_original()
            c.targets, c._unproven_owners, c._closed_role_sources = targets, unproven, roles
            _refresh_unresolved_sources(c, tuple(self._unresolved))
            for target in targets:
                c._quarantined.update(_scope_keys(target.scope))
                _queue_target_recheck(c, target)
            self.ready = True
            from .multiprocess_takeover_runtime import _source_snapshot
            c._bootstrap_sources = _source_snapshot(c)
        self._groups.clear()

    # LLM: 只关闭可丢弃冷扫描资源，不删除P1/P3或原执行者证明对象；可重复收尾。
    # 函数用途: 回收责任回填尚未走完的目录句柄。
    def close(self):
        with self._cold_lock:
            self._cold.close()
            self._cold_closed = True
            if not self.ready:
                self.failed = True


# LLM: 只复用规范local/main身份摘要，不从P1角色字段推导权限。
# 函数用途: 给冷历史逐请求核验取得C角色的准确owner。
def _main_owner_sha():
    from ..user_space.owner_resolver import OwnerIdentity
    from .multiprocess_route import owner_digest
    return owner_digest(OwnerIdentity.local_main())


# LLM: 键保留准确generation、owner和reservation，不跨代或跨Owner合并责任。
# 函数用途: 生成原执行者在活跃投影中的唯一定位键。
def _executor_key(row):
    return row['c_run_id'], row['owner_sha'], row['reservation_id']


# LLM: 只点读原普通单链接文件与父目录身份；同字节inode替换也视为版本变化，始终关FD。
# 函数用途: 给原代次元数据建立准确的文件版本凭据。
def _file_stamp(root, parts):
    descriptor = open_directory_beneath(root, parts[:-1], require_dir_fd=True)
    try:
        value = os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
        if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
            raise ValueError('original_metadata_regular_file_required')
        parent = os.fstat(descriptor)
        return parent.st_dev, parent.st_ino, value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns
    finally:
        os.close(descriptor)


# LLM: 仅接受原WorkerCommitIssuer或CoordinatorExecutorSource，并重新核原出生绑定。
# 函数用途: 排除替身、旧代及断链的执行者提交来源。
def _require_holder(holder):
    from .multiprocess_coordinator_executor_source import CoordinatorExecutorSource
    from .multiprocess_worker_commit import WorkerCommitIssuer
    if type(holder) is WorkerCommitIssuer:
        holder._require_current()
    elif type(holder) is CoordinatorExecutorSource:
        holder._require_source_identity()
    else:
        raise ValueError('original_executor_holder_required')


# LLM: 后台门外固定批观察；候选snapshot与末CAS短持原C，内核等待绝不占T/C/lease门。
# 函数用途: 持续回填未知来源并尝试结清已退出的原执行者。
def maintain_controller_index(controller, limit=64):
    """后台固定批；内核观察不占T/C门，末CAS仍沿T→C→lease。"""
    if type(limit) is not int or not 1 <= limit <= 64:
        raise ValueError('responsibility_batch_limit_invalid')
    index = index_for(controller)
    if controller._mutex._is_owned() or controller._lease._lifecycle_gate._is_owned():
        raise ValueError('responsibility_observation_outside_gate_required')
    visited = index.backfill(limit)
    if index.ready:
        with controller._mutex:
            candidates = []
            for _ in range(min(limit, len(index.executors))):
                key, entry = index.executors.popitem(last=False)
                index.executors[key] = entry
                candidates.append((key, entry))
        for key, entry in candidates:
            _retire_candidate(index, key, entry)
    return {'ready': index.ready, 'visited': visited, 'active': len(index.executors)}


# LLM: 仅捕获原C Linux域、规范P1及当时精确pin；不支持的域保持未知，不倒推缺失事件。
# 函数用途: 在C执行者出生时保留后续退出观察需要的原对象。
def capture_coordinator_descriptor(holder, row):
    from .multiprocess_native_linux_domain import NativePidNamespaceDomain
    domain = holder._domains[-1] if holder._domains else None
    if (type(domain) is not NativePidNamespaceDomain or domain.original is not holder
            or domain.source is not holder or domain.reserved != row or domain.pin is None):
        return None
    return {'domain': domain, 'domain_pin': domain.pin, 'startup_pin': None, 'session': None,
            'original': domain.original, 'reserved': copy.deepcopy(row)}


# LLM: 调用者已经持原session锁；沿原runtime/lane绑定取得准确seq和pin，绝不反向取session锁。
# 函数用途: 在W执行者提交时捕获其原域与启动责任。
def capture_worker_descriptor(holder, row):
    """已持原session锁时捕获seq；绝不在C门反向acquire session。"""
    from .multiprocess_drain_runtime import GatewayDrainRuntime
    from .multiprocess_native_executor_lane import NativeExecutorLane
    from .multiprocess_native_executor_session import NativeExecutorSession
    from .multiprocess_native_linux_domain import NativePidNamespaceDomain
    c = holder._controller
    gate = getattr(c, '_native_terminal_admission_origin', None)
    binding = getattr(gate, '_ordinary_admission_origin', None)
    if type(binding) is not tuple or len(binding) != 2 or type(binding[0]) is not GatewayDrainRuntime:
        return None
    runtime, original = binding
    runtime.require_original()
    if (type(original) is not tuple or len(original) != 5
            or not any(item is original for item in runtime._admission_origins)
            or original[0] is not gate or original[1] is not gate.paths or original[2] is not gate.agent
            or original[3] != gate.count or original[4] != gate.c_run_id
            or runtime.controller is not c or runtime._lease_origin is not holder._lease
            or runtime.control.paths is not gate.paths or runtime._workers is not runtime._workers_origin):
        return None
    workers = runtime._workers
    if workers is None or holder.launch.partition.index >= len(workers.native_lanes):
        return None
    lane = getattr(workers.native_lanes[holder.launch.partition.index], 'executor_lane', None)
    if type(lane) is not NativeExecutorLane:
        return None
    lane._require_original()
    session = lane.session
    if (type(session) is not NativeExecutorSession or not session._lock._is_owned()
            or session._sources[0] is not holder):
        return None
    startup = session._handles.get(session._last_seq)
    domain = session._domain_handles.get(session._last_seq)
    if (type(domain) is not NativePidNamespaceDomain or startup is None or startup[0] is not holder
            or domain.reserved != row or domain.original.issuer is not holder or domain.pin is None):
        return None
    return {'domain': domain, 'domain_pin': domain.pin, 'startup_pin': startup[1], 'session': session,
            'original': domain.original, 'reserved': copy.deepcopy(row)}


# LLM: 原物理退出、末回执、P3/attempt、显式pin结清和generation齐全后才耐久CAS摘active。
# 函数用途: 尝试退役一条原执行者责任；任何不确定都保留原成员。
def _retire_candidate(index, key, entry):
    from .io import gateway_turn_transition, read_json_file_report
    from .multiprocess_worker_commit import WorkerCommitRejected
    descriptor = entry['descriptor']
    if descriptor is None:
        return False
    domain, holder, row = descriptor['domain'], entry['holder'], entry['row']
    try:
        # 1/2 原内核域退出+原release receipt；startup finish不充最终结果消费回执。
        session = descriptor['session']
        if session is None:
            receipt = _observe_domain(descriptor)
        else:
            with session._lock:
                session._require_original()
                receipt = _observe_domain(descriptor)
        if receipt is None:
            return False
        with domain._gate:
            if domain.pin is not descriptor['domain_pin'] and (
                    domain.pin is not None or descriptor['domain_pin'] not in holder._finished_pins):
                return False
        # 先核P3/attempt，离开T后才结清原domain pin，不能持C反向争域/端点锁。
        with gateway_turn_transition(index.controller.paths, row['request_id']):
            p3, payload = _retirement_request(index, row, read_json_file_report)
        with domain._gate:
            if domain.pin is not descriptor['domain_pin'] and (
                    domain.pin is not None or descriptor['domain_pin'] not in holder._finished_pins):
                return False
            domain.retire_lifetime()
        with gateway_turn_transition(index.controller.paths, row['request_id']):
            current, request = _retirement_request(index, row, read_json_file_report)
            if current != p3 or request != payload:
                return False
            with index.controller._mutex, index.lease._lifecycle_gate:
                index.require_original()
                _require_holder(holder)
                if index.executors.get(key) is not entry or entry['ledger'].read(row['reservation_id']) != row:
                    return False
                if (domain.original is not descriptor['original'] or domain.reserved != descriptor['reserved']
                        or domain._release_receipt is not receipt
                        or receipt is not domain._reference_origin._receipt_origin):
                    return False
                # 4 显式producer事件；缺pin或None永不补造历史。5 原generation/lease。
                pins = [descriptor['domain_pin']]
                if descriptor['startup_pin'] is not None:
                    pins.append(descriptor['startup_pin'])
                if (any(pin not in holder._finished_pins for pin in pins)
                        or row['c_run_id'] != index.generation or entry['ledger'].partition.c_run_id != index.generation):
                    return False
                index._persist('executor', row['reservation_id'], {
                    'record': row, 'p3_sha256': record_digest(current),
                    'request_sha256': _json_digest(request), 'receipt': 'original_kernel_release',
                    'kernel_receipt': {'pid': receipt[2], 'start_time': receipt[3],
                                       'namespace': dict(receipt[4]), 'waitstatus': receipt[5]},
                    'pin_events': len(pins)})
                del index.executors[key]
                return True
    except (OSError, ValueError, TypeError, KeyError, WorkerCommitRejected):
        return False


# LLM: 只消费原域empty核实与原reference同一release receipt，不以句柄消失或pin为空补证。
# 函数用途: 在提交门外取得原内核域的准确退出回执。
def _observe_domain(descriptor):
    from .multiprocess_native_linux_domain import NativePidNamespaceDomain
    domain = descriptor['domain']
    if (type(domain) is not NativePidNamespaceDomain or domain.original is not descriptor['original']
            or domain.reserved != descriptor['reserved'] or domain.process is None
            or not domain.empty(domain.process, domain.row)):
        return None
    reference, receipt = domain._reference_origin, domain._release_receipt
    if reference is None or receipt is None or reference._receipt_origin is not receipt:
        return None
    return receipt


# LLM: 原T内点读原P3和规范processing/terminal，准确核owner、attempt、epoch与route。
# 函数用途: 证明要结清的执行者仍属于同一次原请求责任。
def _retirement_request(index, row, reader):
    p3 = index._p3(row['request_id'])
    c = index.controller
    original = (index._settled_p3.get(row['request_id']) == p3 if p3['state'] == 'released'
                else c._accepted.get(row['request_id']) == record_digest(p3))
    if (p3['owner_sha'] != row['owner_sha'] or p3['route']['c_run_id'] != row['c_run_id'] or not original):
        raise ValueError('retirement_original_p3_required')
    # P3 released可由原终态文件复核；occupied仍可逐executor退active。
    path = (c.paths.terminal if p3['state'] == 'released' else c.paths.processing) / (row['request_id'] + '.json')
    parts = path.relative_to(index.root).parts
    if read_bytes_beneath(index.root, parts, max_bytes=16 * 1024 * 1024, require_dir_fd=True) is None:
        raise ValueError('retirement_original_request_required')
    payload = reader(path, context='executor.retirement').payload
    if (payload.get('id') != row['request_id'] or payload.get('execution_attempt_id') != row['execution_attempt_id']
            or payload.get('lease_epoch') != row['lease_epoch'] or payload.get('route') != p3['route']):
        raise ValueError('retirement_original_attempt_required')
    return p3, payload


# LLM: 只对已验证JSON原像作稳定摘要，拒绝非有限数值；摘要本身不代表授权。
# 函数用途: 为不可变结清hint记录规范请求内容指纹。
def _json_digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
