# LLM: 原控制回执是唯一状态机；W登记/冻结分区是权威，三池保原出生refs及submit故障；主线pin固定实际Agent而非owner，短锁认领后锁外运行，同步stop独立槽及回收合同。
# 模块用途: 消费所属分区原控制和规范私卡点击（不恢复执行源），不占前台模型槽，不重放已跨副作用边界的命令。
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from ..common.nofollow_fs import read_bytes_beneath
from ..conversation.control_commands import parse_conversation_control
from ..runtime_errors import DataCorruptionError, runtime_error_report
from . import control_operation_service as ops
from .daemon_metadata import process_host_id
from .io import write_json_file_atomic
from .multiprocess_bootstrap import WorkerBootstrapError, require_worker_bootstrap
from .multiprocess_bootstrap_publish import read_worker_registry
from .multiprocess_bootstrap_ready import require_current_worker
from .multiprocess_drain_resources import DrainResource
from .multiprocess_executor_identity import process_birth
from .multiprocess_records import deserialize_record
from .paths import gateway_paths_from_root


# LLM: 不创建owner；必须原登记同代/实例/精确出生，认领额外核健康，排空中旧认领可结算。
# 函数用途: 拒绝撤权、错误分区、未知出生与未登记的执行者。
def control_worker_authority(frozen, *, claiming):
    require_worker_bootstrap(frozen, frozen.launch)
    launch = frozen.launch
    registry = read_worker_registry(launch.gateway_root)
    partition = launch.partition
    if registry is None or registry['c_run_id'] != partition.c_run_id or registry['worker_count'] != partition.count:
        raise WorkerBootstrapError('control_registry_generation', 'registry')
    entry = next((item for item in registry['workers'] if item['worker'] == partition.index), None)
    birth = process_birth(os.getpid())
    identity = {'schema_version': 'multiprocess_process_identity.v1', 'host_id': process_host_id(),
                'pid': os.getpid(), 'start_time': birth}
    if (not birth or entry is None or not launch.instance_id or entry['instance_id'] != launch.instance_id
            or entry['process_identity'] != identity):
        raise WorkerBootstrapError('control_executor_not_current', 'process_identity')
    if claiming:
        raw = read_bytes_beneath(launch.gateway_root, ('workers', f'w{partition.index}.json'),
                                 max_bytes=1024 * 1024, require_dir_fd=True)
        heartbeat = None if raw is None else deserialize_record(raw)
        require_current_worker(registry, heartbeat, partition, time.time())
    return identity


# LLM: active表只保留线程责任，不替代原回执；poll串行化允许独立快控制轮询与组合根并用。
# 类用途: 用独立执行槽处理远端控制，退出前等已认领的副作用结清。
class WorkerControlPump:
    # LLM: 冻结上下文先校验；原两个池绑定准确出生initializer，stop独立槽数量不变，不加池/线程表；坏记录指纹仅去重，观察者不授权。
    # 函数用途: 保存控制队列根、owner池和独立控制槽。
    def __init__(self, frozen, pool):
        require_worker_bootstrap(frozen, frozen.launch)
        self.frozen, self.pool = frozen, pool
        self.paths = gateway_paths_from_root(frozen.launch.gateway_root)
        self._drain_resources = self._drain_origins = (DrainResource('control', 'original-pool/1'),
                                                       DrainResource('control', 'original-pool/2'))
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='gateway-worker-control',
                                           initializer=self._drain_resources[0].remember_current_thread)
        self._stop_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='gateway-worker-stop',
                                                initializer=self._drain_resources[1].remember_current_thread)
        self._drain_resources[0].bind_executor(self._executor)
        self._drain_resources[1].bind_executor(self._stop_executor)
        self._active = {}
        self._lock = threading.RLock()
        self._stopped = False
        self._read_faults = {}
        self._observation_cursor = ''

    # LLM: 包括返回但未核收的future；不能作为全部六类排空证明。
    # 函数用途: 返回尚承担结算责任的控制数。
    @property
    def inflight(self):
        with self._lock:
            return len(self._active)

    # LLM: 原控制锁内只读完整准确原池/group/source，claimed/executing/unknown不因future返回清成功，收口消费出生时actualThread而非后读空表。
    # 函数用途: 给P6保留准确控制池和在途责任对象。
    def drain_snapshot(self):
        with self._lock:
            if (self._drain_resources is not self._drain_origins or len(self._drain_resources) != 2
                    or tuple(g.owner for g in self._drain_resources) != (self._executor, self._stop_executor)):
                raise ValueError('drain_original_worker_pool_changed')
            for group in self._drain_resources:
                group._require_original()
            return self._stopped, self._drain_resources, tuple(self._active.items()), tuple(self._read_faults)

    # LLM: 原操作短锁下重复读取认领；私卡写只try、prepared未首写无Future，prepared先于纯观察，普通槽满仍扫描stop，跨分区/旧代不写。
    #   单条坏记录只保留并诊断，不关闭其它分区；bootstrap/撤权失败仍向组合根传播。
    # 函数用途: 将本分区prepared控制交给独立槽。
    def poll(self):
        with self._lock:
            self._reap()
            if self._stopped:
                return 0
            submitted, observations = 0, []
            for path in sorted((self.paths.root / 'control_operations').glob('*.json')):
                receipt = self._read_receipt(path)
                if not self._matches(receipt):
                    continue
                if receipt.state == 'completed' or (receipt.private_approval is not None and receipt.state in {'executing', 'terminal_unknown'}):
                    observations.append(receipt)
                    continue
                if receipt.state != 'prepared':
                    continue
                stopping = receipt.command_kind == 'stop'
                occupied = sum((item.command_kind == 'stop') == stopping for item in self._active.values())
                if occupied >= (1 if stopping else 4):
                    continue
                identity = control_worker_authority(self.frozen, claiming=True)
                with ops.try_gateway_control_operation_transition(self.paths, path.stem) as acquired:
                    if not acquired:
                        continue
                    receipt = self._read_receipt(path)
                    if not self._matches(receipt) or receipt.state != 'prepared':
                        continue
                    control_worker_authority(self.frozen, claiming=True)
                    claimed = replace(receipt, state='claimed', updated_at=time.time(),
                                      dispatch={**receipt.dispatch, 'executor': identity,
                                                'instance_id': self.frozen.launch.instance_id})
                    if not _write_control_claim(self, claimed):
                        continue
                executor = self._stop_executor if stopping else self._executor
                self._active[self._submit(executor, self._execute, claimed)] = claimed
                submitted += 1
            for receipt in sorted(observations, key=lambda row: (
                    row.operation_id <= self._observation_cursor, row.operation_id)):
                self._offer_reconciliation(receipt)
            return submitted

    # LLM: 全kind观察沿原read_only codec读取原子发布快照，不取得JSON写锁；末次认领仍在原op锁内重读，不由快照授信。
    #   仅隔离读取原件的结构/IO故障，不捕获执行权/写入/线程异常；不删除或修复坏原件。
    #   指纹缓存有界且只去重同一原件的诊断，修复后立即恢复读取，不作为第二权威账。
    # 函数用途: 读取单条控制回执，损坏时报告一次并继续处理其它健康操作。
    def _read_receipt(self, path):
        try:
            receipt = ops.read_gateway_control_operation(self.paths, path.stem, read_only=True)
        except (DataCorruptionError, OSError, ValueError) as error:
            try:
                stat = path.lstat()
                fingerprint = (stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
            except OSError:
                fingerprint = (type(error).__name__, str(error))
            if self._read_faults.get(path.name) != fingerprint:
                from .logging import _report_gateway_side_effect_error

                _report_gateway_side_effect_error('read_worker_control_operation', path.stem, error)
                if len(self._read_faults) >= 128:
                    self._read_faults.pop(next(iter(self._read_faults)))
                self._read_faults[path.name] = fingerprint
            return None
        self._read_faults.pop(path.name, None)
        return receipt

    # LLM: from_dict已校验完整冻结输入及确定性分区；这里只比原launch而非热配置。
    # 函数用途: 排除别人的控制和旧代记录。
    def _matches(self, receipt):
        route = None if receipt is None else receipt.dispatch
        partition = self.frozen.launch.partition
        return isinstance(route, dict) and (route['worker'], route['count'], route['c_run_id']) == (
            partition.index, partition.count, partition.c_run_id)

    # LLM: 原快照/登记/pin实际Agent保持；私卡op/JSON只try，输入后Future退出，不搬TLS/长等用户；忙且未结仍保原Future，普通命令原执行。
    # 函数用途: 推进executing，pin原池实际实例；显式私卡决定重核该实例的原scope，普通slash仍原分派，再封同一认领。
    def _execute(self, claimed):
        committing = True
        try:
            executing = replace(claimed, state='executing', updated_at=time.time())
            transitioned = (_transition_private_control(self, claimed, executing) if claimed.private_approval is not None
                            else self._transition(claimed, executing))
            if not transitioned:
                return
            committing = False
            command = None
            if executing.private_approval is None:
                command = parse_conversation_control(executing.command_text, reject_unknown_slash=True)
                if command is None or not command.valid or command.kind != executing.command_kind:
                    raise ValueError('control_command_invalid')
            owner = ops._receipt_owner_identity(executing)
            control_worker_authority(self.frozen, claiming=False)
            agent = self.pool.get(owner)
            with self.pool.pin(agent):
                control_worker_authority(self.frozen, claiming=False)
                scope = ops._receipt_scope(executing, dispatched_execution=True)
                if executing.private_approval is not None:
                    self._submit_private_input(agent, executing)
                    return
                else:
                    executed = ops._with_control_conversation_thread(agent, executing, scope)
                    result = ops.execute_gateway_conversation_control(agent, self.paths, command, scope)
            completed = replace(executed, state='completed', result=result.to_dict(), error={}, updated_at=time.time())
            committing = True
            if self._transition(executing, completed):
                ops.command_completed(agent, completed)
        except Exception as error:  # noqa: BLE001 - 任何部分副作用都不能自动重放。
            unknown = replace(claimed, state='terminal_unknown', updated_at=time.time(),
                              error=runtime_error_report(error, context='gateway.worker.control'))
            private_transport_error = False
            if claimed.private_approval is not None:
                from .private_approval_control import is_private_approval_transport_error
                private_transport_error = is_private_approval_transport_error(error)
            try:
                settled = self._fail_attempt(claimed, unknown)
            except BaseException:
                if private_transport_error:
                    error.add_note('private_approval_settlement_unconfirmed')
                    raise error from None
                raise
            if (private_transport_error or committing or (claimed.private_approval is not None and not settled)
                    or isinstance(error, (WorkerBootstrapError, PermissionError))):
                raise

    # LLM: 原W登记/P5/pin已由调用者持有；op只try，锁内重核同executing再提交input，不等锁/用户、不恢复控制线程执行scope。
    # 函数用途: 有界提交这一条原私卡输入，拆开独立锁责任而不增加执行通道。
    def _submit_private_input(self, agent, executing):
        from .multiprocess_global_write import GlobalWriteError
        from .private_approval_control import execute_worker_private_approval
        with ops.try_gateway_control_operation_transition(self.paths, executing.operation_id) as acquired:
            if not acquired:
                raise GlobalWriteError('private_approval_transition_busy', 'private_interaction')
            if ops.read_gateway_control_operation(self.paths, executing.operation_id, read_only=True) != executing:
                return
            control_worker_authority(self.frozen, claiming=False)
            try:
                execute_worker_private_approval(agent, executing)
            except BlockingIOError as error:
                raise GlobalWriteError('private_approval_permission_busy', 'private_interaction') from error

    # LLM: 状态与完整dispatch均须等于本线程原快照；重新核登记先于写，不把旧W结果盖回新代。
    # 函数用途: 在原控制锁下条件推进同一认领。
    def _transition(self, expected, updated):
        with ops.gateway_control_operation_transition(self.paths, expected.operation_id):
            current = ops.read_gateway_control_operation(self.paths, expected.operation_id)
            if current != expected:
                return False
            control_worker_authority(self.frozen, claiming=False)
            self._write(updated)
            return True

    # LLM: 原steer与待私卡executing/UNKNOWN只排一次有界事实观察，待用户卡不持Future长poll；复用原槽/登记/pin，不批准或重发。
    # 函数用途: 为既有插话结果安排只读对账，不重执行任何命令。
    def _offer_reconciliation(self, receipt):
        private = receipt.private_approval is not None and receipt.state in {'executing', 'terminal_unknown'}
        if ((not private and (receipt.command_kind != 'steer' or receipt.result.get('delivery_status') != 'unknown'
                or not receipt.result.get('guidance_dedupe_key')))
                or any(item.operation_id == receipt.operation_id for item in self._active.values())
                or any(item.state == 'completed' for item in self._active.values())
                or sum(item.command_kind != 'stop' for item in self._active.values()) >= 4):
            return
        identity = control_worker_authority(self.frozen, claiming=True)
        if (receipt.dispatch.get('executor') != identity
                or receipt.dispatch.get('instance_id') != self.frozen.launch.instance_id):
            return
        self._active[self._submit(self._executor, self._reconcile_delivery, receipt)] = receipt
        self._observation_cursor = receipt.operation_id

    # LLM: 每次只读原steer/私卡决定事实，核同登记/出生/Agent pin及完整CAS；私卡op→permission只try，JSON忙保原回执，不批准/投递或占槽长等。
    # 函数用途: 把原插话已消费/已拒绝投影回同一操作，其它结果保持未知。
    def _reconcile_delivery(self, receipt):
        identity = control_worker_authority(self.frozen, claiming=False)
        if (receipt.dispatch.get('executor') != identity
                or receipt.dispatch.get('instance_id') != self.frozen.launch.instance_id):
            return
        owner = ops._receipt_owner_identity(receipt)
        control_worker_authority(self.frozen, claiming=False)
        agent = self.pool.get(owner)
        with self.pool.pin(agent):
            control_worker_authority(self.frozen, claiming=False)
            if receipt.private_approval is not None:
                _reconcile_private_control(self, agent, receipt)
                return
            result = self._observe_delivery(agent, receipt)
        if result is not None:
            self._transition(receipt, replace(receipt, result=result.to_dict(), updated_at=time.time()))

    # LLM: 只在领域读取边界隔离结构/IO错误；owner物化、撤权与最终CAS错误仍向组合根传播。
    # 函数用途: 坏插话留原unknown；健康观察清除旧诊断指纹。
    def _observe_delivery(self, agent, receipt):
        from .multiprocess_control_reconcile import read_original_steer_delivery

        path = agent.conversation_store.guidance.storage.guidance_dedupe_path(
            receipt.result['guidance_dedupe_key'])
        key = ('guidance', str(path))
        try:
            result = read_original_steer_delivery(
                agent, receipt, ops._receipt_scope(receipt, dispatched_execution=True))
        except (DataCorruptionError, OSError, ValueError) as error:
            self._report_guidance_read_fault(key, path, receipt.operation_id, error)
            return None
        with self._lock:
            self._read_faults.pop(key, None)
        return result

    # LLM: 指纹只去重领域诊断；与poll共用锁、有界128项，不改坏原件或产生执行许可。
    # 函数用途: 同一坏插话只报一次，原件变化后重新报告。
    def _report_guidance_read_fault(self, key, path, operation_id, error):
        try:
            stat = path.lstat()
            fingerprint = (stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
        except OSError:
            fingerprint = (type(error).__name__, str(error))
        with self._lock:
            if self._read_faults.get(key) == fingerprint:
                return
            if len(self._read_faults) >= 128:
                self._read_faults.pop(next(iter(self._read_faults)))
            self._read_faults[key] = fingerprint
        from .logging import _report_gateway_side_effect_error

        _report_gateway_side_effect_error('read_worker_steer_guidance', operation_id, error)

    # LLM: 原op锁内只结同executor；私卡已决定事实先精确对账，未知先赢后不能迟到批准，撤权仍拒绝，不释放P3。
    # 函数用途: 保存无法确认的控制执行失败，不吞掉撤权失败。
    def _fail_attempt(self, claimed, unknown):
        if claimed.private_approval is not None:
            return self._fail_private_attempt(claimed, unknown)
        with ops.gateway_control_operation_transition(self.paths, claimed.operation_id):
            current = ops.read_gateway_control_operation(self.paths, claimed.operation_id)
            if current is None or current.dispatch != claimed.dispatch or current.state not in {'claimed', 'executing'}:
                return
            control_worker_authority(self.frozen, claiming=False)
            self._write(unknown)

    # LLM: 不重投input；同dispatch/opid/digest及原permission决定先对账，UNKNOWN后写。try忙只保Future，不能认空或丢claimed责任。
    # 函数用途: 为原私卡失败立即尝试落同一未结事实，返回False表示原Future还必须留表等待结算。
    def _fail_private_attempt(self, claimed, unknown):
        from .private_approval_control import reconcile_private_approval_locked
        with ops.try_gateway_control_operation_transition(self.paths, claimed.operation_id) as acquired:
            if not acquired:
                return False
            current = ops.read_gateway_control_operation(self.paths, claimed.operation_id, read_only=True)
            if (current is None or current.dispatch != claimed.dispatch or current.operation_id != claimed.operation_id
                    or current.input_digest != claimed.input_digest):
                return False
            control_worker_authority(self.frozen, claiming=False)
            if current.state in {'completed', 'terminal_unknown'}:
                return True
            if current.state not in {'claimed', 'executing'}:
                return False
            agent = self.pool.get(ops._receipt_owner_identity(current))
            with self.pool.pin(agent):
                observed = reconcile_private_approval_locked(self.paths, current, agent=agent)
            updated = observed if observed.state == 'completed' else unknown
            return _try_private_control_write(self, updated)

    # LLM: 只写原操作规范文件；私卡显式False沿原writer try锁，不造第二队列，默认True保普通控制原调用缝。
    # 函数用途: 原子保存控制状态，私卡锁忙立即拒绝而不假写成功。
    def _write(self, receipt, *, blocking=True):
        write_json_file_atomic(ops.gateway_control_operation_path(self.paths, receipt.operation_id), receipt.to_dict(),
                               **({} if blocking else {'blocking': False}))

    # LLM: 私卡失败仅重试同receipt UNKNOWN/决定证据落盘，不重投输入；try忙原Future留_active而其它控制/stop继续，close忙仍抛原异常不虚称结清。
    # 函数用途: 核收已返回的执行future，避免锁忙留下无人观察的claimed或把部分写入当成功。
    def _reap(self, *, closing=False):
        for future in tuple(self._active):
            if future.done():
                claimed = self._active[future]
                error = future.exception()
                if error is not None and claimed.private_approval is not None:
                    _reap_private_control(self, future, claimed, error, closing=closing)
                    continue
                future.result()
                del self._active[future]

    # LLM: 同一个原Future的错误负责同一claim；新结算故障不得盖掉actual运输未知，保原对象给组合根且不删除责任。
    # 函数用途: 有界补写私卡未结事实，原typed未知不被后续撤权或IO异常洗成普通失败。
    def _settle_private_future(self, claimed, error):
        from .private_approval_control import raise_private_approval_transport_error
        unknown = replace(claimed, state='terminal_unknown', updated_at=time.time(),
                          error=runtime_error_report(error, context='gateway.worker.control'))
        try:
            return self._fail_private_attempt(claimed, unknown)
        except BaseException:
            raise_private_approval_transport_error(error)
            raise

    # LLM: 两个原池逐对象匹配，实际submit异常保相应出生fault并传播；不加槽/重试或另建池，未进入业务也保unknown。
    # 函数用途: 在原控制执行/对账提交点保存准确设施故障。
    def _submit(self, executor, operation, receipt):
        index = 0 if executor is self._executor else 1 if executor is self._stop_executor else None
        if index is None:
            raise ValueError('drain_original_worker_pool_changed')
        try:
            return executor.submit(operation, receipt)
        except BaseException as error:
            self._drain_resources[index].retain_fault(error)
            raise

    # LLM: 单向停止新认领，原执行继续结算。
    # 函数用途: 阻止排空时增加控制责任。
    def stop_claiming(self):
        with self._lock:
            self._stopped = True

    # LLM: 任何shutdown前核本原pool/group源，换源拒且不碰foreign；不持poll锁join，私卡结算忙保原Future并抛未结，不以池已关冒充责任清空。
    # 函数用途: 等已认领控制结清再关闭槽。
    def close(self):
        self.drain_snapshot()
        self.stop_claiming()
        try:
            self._executor.shutdown(wait=True)
        finally:
            self._stop_executor.shutdown(wait=True)
        with self._lock:
            self._reap(closing=True)


# LLM: 原writer的False模式只尝试原写锁，失败不声明未进入或已完成；调用者必须仍保prepared或同一个Future责任。
# 函数用途: 尝试原私卡回执写入，锁忙时返回未确认，不能把它当空行或可重复执行许可。
def _try_private_control_write(pump, receipt):
    try:
        pump._write(receipt, blocking=False)
    except BlockingIOError:
        return False
    return True


# LLM: 原W登记/pin已由调用者核实；同op try锁内只读原决定，permission核完整opid/digest，写忙保原行供下次事实观察。
# 函数用途: 有界对账一条私卡，不批准、不重新提交输入或另开副作用通道。
def _reconcile_private_control(pump, agent, receipt):
    from .private_approval_control import reconcile_private_approval_locked
    with ops.try_gateway_control_operation_transition(pump.paths, receipt.operation_id) as acquired:
        if not acquired:
            return
        current = ops.read_gateway_control_operation(pump.paths, receipt.operation_id, read_only=True)
        if current != receipt:
            return
        control_worker_authority(pump.frozen, claiming=False)
        updated = reconcile_private_approval_locked(pump.paths, current, agent=agent)
        if updated != current:
            _try_private_control_write(pump, updated)


# LLM: 原Future在try忙时留原_active，后续poll只补UNKNOWN/决定证据；close仍忙须抛原错，不能宣称池关闭就结清。
# 函数用途: 在同一个原Future实际退出后保留未结责任，落盘成功才移除并同对象报告异常。
def _reap_private_control(pump, future, claimed, error, *, closing):
    if not pump._settle_private_future(claimed, error):
        if closing:
            raise error
        return
    del pump._active[future]
    raise error


# LLM: 只供私卡原claim推进；op与JSON均非阻塞，失败不冒充未执行，原Future负责UNKNOWN结算；普通控制原_transition不变。
# 函数用途: 在原同回执CAS内立即尝试推进私卡，锁忙把原责任交还核收点。
def _transition_private_control(pump, expected, updated):
    from .multiprocess_global_write import GlobalWriteError
    with ops.try_gateway_control_operation_transition(pump.paths, expected.operation_id) as acquired:
        if not acquired:
            raise GlobalWriteError('private_approval_transition_busy', 'private_interaction')
        if ops.read_gateway_control_operation(pump.paths, expected.operation_id, read_only=True) != expected:
            return False
        control_worker_authority(pump.frozen, claiming=False)
        try:
            pump._write(updated, blocking=False)
        except BlockingIOError as error:
            raise GlobalWriteError('private_approval_receipt_busy', 'private_interaction') from error
        return True


# LLM: 普通控制沿原默认writer与异常传播，只有规范private字段选择非阻塞原writer；未确认首写不得发布Future。
# 函数用途: 在原op认领门内保存同一claimed记录，私卡写忙时保prepared继续其它控制扫描。
def _write_control_claim(pump, receipt):
    if receipt.private_approval is None:
        pump._write(receipt)
        return True
    return _try_private_control_write(pump, receipt)
