# LLM: P6生产组合根只保原C/control/lease和出生资源，停止准入→门外join→原源稳定双读→六类评估；count0不构造，不造权限/账，原native整树由独立回执消费，同步held/复制/旧代/生命周期测试。
#   retain_admission只运输原gate/tuple引用；ordinary新责任末发表与本Root关闭共门，旧责任不靠停止洗掉。
# 模块用途: 汇集实际资源及原责任，停止新准入后核完整排空才封存v2；未结束/缺证据或提交失败仍保阻塞。
import hashlib
import os
import threading
import time

from .control_plane import GatewayControlPlane
from .multiprocess_drain_resources import DrainResource
from .multiprocess_drain_sources import durable_drain_snapshot, durable_items, same_durable_snapshot
from .multiprocess_stop_drain import STOP_DRAIN_KINDS, DrainInventory, InFlightItem, assess_drain
from .multiprocess_stop_scope import CoordinatorStopScope

# 关闭仅使用此门外等待预算（秒），到期保留原资源，不把期限当死亡事实。
_GATEWAY_RESOURCE_DRAIN_SECONDS = 2.0


# LLM: 原control/controller/lease/self/PID同源才观察；内部封存token只保本次原引用，复制/重绑/旧C不签关闭或P2权。
#   原P3gate登记后保同Root反向引用，与既有_gate线性化新责任，不能跨Root重绑或关闭后补登。
# 类用途: 在本次原C生命周期内保留实际设施资源和准入门，统一停止观察与新责任末发表。
class GatewayDrainRuntime:
    # LLM: 正count真实组合根纯内存捕原source及内部关闭token槽；不建线程/目录或给Count0假control签权。
    # 函数用途: 捕获本次原C及其停止角色，准备资源出生登记。
    def __init__(self, control):
        if type(control) is not GatewayControlPlane or type(control.worker_count) is not int or control.worker_count < 1:
            raise ValueError("drain_original_control_required")
        controller = control.controller()
        if controller is None:
            raise ValueError("drain_original_coordinator_required")
        self.control = self._control_origin = control
        self.controller = self._controller_origin = controller
        self._lease_origin = controller._lease
        self._origin, self._creator_pid = self, os.getpid()
        self.scope = self._scope_origin = CoordinatorStopScope(control.worker_count, control.c_run_id)
        self._gate = threading.RLock()
        self._resources = {}
        self._resource_origins = []
        self._closing = False
        self._workers = self._workers_origin = None
        self._admission_origins = []
        self._recheck_origin = controller._recheck_thread_origin
        self._fault = None
        self._generation_observation = None
        self._generation_seal_origin = self._generation_seal_bytes = None
        self._closed_generation_ref = None
        self._generation_closing_sources = None
        self._generation_assessment_attempted = self._generation_consume_attempted = False

    # LLM: 原producer必须在pool ctor前取本准确group，关闭之后不能新建；只限制/观察，不签线程或业务权。
    # 函数用途: 为尚未出生的原池准备初始化观察者。
    def prepare_pool(self, kind, resource_ref):
        self.require_original()
        with self._gate:
            if self._closing or kind not in STOP_DRAIN_KINDS or resource_ref in self._resources:
                raise ValueError("drain_resource_registration_closed")
            group = DrainResource(kind, resource_ref)
            self._resources[resource_ref] = group
            self._resource_origins.append((kind, resource_ref, group))
            return group

    # LLM: exact actualThread在start之前登记，不接名字/ident替身；未启动ACK或start异常将保unknown，默认0不调用。
    # 函数用途: 保存原设施线程，让后续关闭仍能找到准确对象。
    def retain_thread(self, kind, resource_ref, thread):
        group = self.prepare_pool(kind, resource_ref)
        if type(thread) is not threading.Thread:
            group.retain_fault(ValueError("drain_original_thread_required"))
            raise ValueError("drain_original_thread_required")
        group.owner = group._owner_origin = thread
        return group

    # LLM: 原C producer创建的workers仅运输原Popen/出生observer；单次绑定，不从P4恢复句柄，v2封存仍需原完整出生及退出观察。
    # 函数用途: 保存本次正式worker组合根，供准确内核退出与native范围观察使用。
    def retain_workers(self, workers):
        self.require_original()
        if self._workers is not None or workers.control is not self.control:
            raise ValueError("drain_original_workers_required")
        self._workers = self._workers_origin = workers

    # LLM: 保存实际dispatcher原P3gate和同一原tuple引用，ordinary末写只消费该Root绑定；不从文件/newlease重建，不另立closing状态。
    # 函数用途: 保留原规范终态释放消费者，让停止之后仍能完成既有P3收尾。
    def retain_admission(self, gate):
        from .multiprocess_admission import PersistentAdmission
        self.require_original()
        if (type(gate) is not PersistentAdmission or gate.paths is not self.control.paths
                or gate.count != self.scope.count or gate.c_run_id != self.scope.c_run_id
                or getattr(gate, '_runtime_takeover', None) is not self.controller):
            raise ValueError('drain_original_admission_required')
        with self._gate:
            if (self._closing or getattr(gate, '_ordinary_admission_origin', None) is not None
                    or any(row[0] is gate for row in self._admission_origins)):
                raise ValueError('drain_original_admission_rebind')
            original = (gate, gate.paths, gate.agent, gate.count, gate.c_run_id)
            self._admission_origins.append(original)
            gate._ordinary_admission_origin = (self, original)

    # LLM: 停止只是不可逆关闭producer登记/submit；仍活请求保原claim可原terminal，不能先撤lease或持T/controller等join。
    # 函数用途: 停止本root的新资源出生，并保留实际池等待收尾。
    def stop_producers(self):
        self.require_original()
        with self._gate:
            self._closing = True
            groups = tuple(self._resources.values())
        for group in groups:
            try:
                group.stop()
            except (OSError, RuntimeError, ValueError) as error:
                group.retain_fault(error)

    # LLM: 完整P1分旧/当前原scope，旧域须captured v2及pre-stop本次fresh P2；成功一次观察，开始/失败清槽，不用startup缓存。
    # 函数用途: 给本C和全部原W评估六类收口，返回结构化阻塞而不释放任何责任。
    def assess(self):
        self.require_original()
        self._generation_observation = None
        self._generation_assessment_attempted = True
        if not self._closing or self._generation_consume_attempted:
            raise ValueError("drain_original_assessment_unavailable")
        deadline = time.monotonic() + _GATEWAY_RESOURCE_DRAIN_SECONDS
        with self._gate:
            groups = tuple(self._resources.values())
        items = tuple(InFlightItem(g.kind, self.scope, g.resource_ref, g.observe(deadline)) for g in groups)
        try:
            calls = _local_call_items(self.scope)
            if not calls and all(item.state == 'settled' for item in items):
                self._finish_original_admission()
            before = durable_drain_snapshot(self.control.paths, self.scope.count)
            trees, old, sources, closed_sources = _observe_generation_trees(self, before[2], deadline)
            items += durable_items(self.scope, before, closed_sources=closed_sources) + calls
            with self.controller._mutex:
                if self.controller._all_quarantined or self.controller._unproven_owners or self.controller._quarantined:
                    items += (InFlightItem('control', self.scope, 'original-takeover-quarantine', 'unknown'),)
            assessments = [_role_assessment(self.scope, items, trees)]
            if self._workers is not None:
                assessments.extend(self._workers.drain_assessments(before, trees, closed_sources=closed_sources))
            if all(result.drained for result in assessments):
                items += self._stop_original_recheck(deadline)
                assessments[0] = _role_assessment(self.scope, items, trees)
            if not same_durable_snapshot(before, durable_drain_snapshot(self.control.paths, self.scope.count)):
                raise ValueError("drain_original_sources_changed")
            self.require_original()
            result = tuple(assessments)
            if all(item.drained for item in result):
                self._generation_observation = (result, before, trees, sources, old,
                    tuple(self._workers._created_origins) if self._workers is not None else ())
                _require_generation_observation(self, self._generation_observation)
            return result
        except (OSError, RuntimeError, ValueError, TypeError, KeyError) as error:
            self._generation_observation = None
            unknown = InFlightItem("foreground", self.scope, "original-sources", type(error).__name__)
            return (assess_drain(DrainInventory(self.scope, True, (), (*items, unknown), None)),)

    # LLM: 仅原gate.release_settled消费规范terminal，原T/P2/lease末核全部保；线程/future/观察者不能直接释放，原C已撤权则源仍occupied/unknown。
    # 函数用途: 用停止前保存的唯一原消费者完成已终态请求的准入收尾。
    def _finish_original_admission(self):
        self.require_original()
        with self._gate:
            originals = tuple(self._admission_origins)
        for gate, paths, agent, count, generation in originals:
            if (gate.paths is not paths or paths is not self.control.paths or gate.agent is not agent
                    or gate.count != count or count != self.scope.count or gate.c_run_id != generation
                    or generation != self.scope.c_run_id or getattr(gate, '_runtime_takeover', None) is not self.controller):
                raise ValueError('drain_original_admission_changed')
            gate.release_settled()

    # LLM: 只消费本closing成功原观察一次后亲自close_native；失败/消费过不得回落重assess，不接受外传true或复制观察。
    # 函数用途: 在原正常收尾顺序中关闭全部native资源，失败保原未知和未封存历史。
    def prepare_generation_closure(self):
        _prepare_original_generation_closure(self)

    # LLM: 只从本原封口引用构造一次关闭事实；正常收尾后先CAS历史再发表精确C末态，写后未知不追补旧代。
    # 函数用途: 在正常停止全部收尾结束时一次封存本代范围，任何缺槽/变化/发布失败均抛错。
    def seal_generation(self):
        from .multiprocess_generation_metadata import seal_generation
        from .multiprocess_records import serialize_record

        self.require_original()
        if self._closed_generation_ref is not None:
            raise ValueError('generation_original_closing_required')
        if self._generation_closing_sources is None:
            self.prepare_generation_closure()
        observation, origins = self._generation_closing_sources
        token = self._generation_material(observation, origins)
        self._generation_seal_origin = token
        self._generation_seal_bytes = serialize_record(token['record'])
        self._closed_generation_ref = seal_generation(self, token)
        return self._closed_generation_ref

    # LLM: source字节、原P4、原完整W对象与collector观察交叉绑定；只保存摘要与原证据引用，不复制请求/凭据正文。
    # 函数用途: 准备唯一v2完整raw CAS候选，来源缺失或变化即拒绝。
    def _generation_material(self, observation, origins):
        from ..common.nofollow_fs import read_bytes_beneath
        from .multiprocess_generation_metadata import (
            generation_role_files,
            generation_source_files,
            read_generation_metadata,
        )
        from .multiprocess_records import deserialize_record, validate_record

        self.require_original()
        _require_generation_observation(self, observation)
        root = self.control.paths.root
        registry_raw = read_bytes_beneath(root, ('workers', 'registry.json'),
                                          max_bytes=1024 * 1024, require_dir_fd=True)
        registry = deserialize_record(registry_raw)
        roles_before = generation_role_files(root, self.scope.count)
        roles = _generation_final_roles(self, roles_before)
        sources = generation_source_files(self.control.paths)
        _require_generation_roles(self, registry, roles, origins)
        if not same_durable_snapshot(observation[1], durable_drain_snapshot(self.control.paths, self.scope.count)):
            raise ValueError('generation_original_sources_changed')
        evidence = _generation_tree_evidence(root, observation[2],
            (*[raw for _generation, raw in observation[3][3]], *[old[4][1] for old in observation[4]]))
        expected = read_generation_metadata(root, self.scope.c_run_id)
        if (expected is None or expected[0] != {'schema_version': 'multiprocess_c_generation.v2',
                'c_run_id': self.scope.c_run_id, 'worker_count': self.scope.count, 'closure': None}):
            raise ValueError('generation_initial_v2_required')
        record = validate_record({'schema_version': 'multiprocess_c_generation.v2', 'c_run_id': self.scope.c_run_id,
            'worker_count': self.scope.count, 'closure': {'registry': registry,
            'registry_sha256': hashlib.sha256(registry_raw).hexdigest(),
            'role_markers': _generation_digests(roles), 'source_files': _generation_digests(sources),
            'tree_evidence': _generation_digests(evidence)}})
        return {'record': record, 'expected': expected, 'roles': roles, 'roles_before': roles_before, 'sources': sources,
                'evidence': evidence, 'registry_raw': registry_raw, 'observation': observation, 'origins': origins}

    # LLM: 仅本次内部token同一且记录字节未变才可末门签发；不等待资源，不从JSON恢复原native对象或权限。
    # 函数用途: 在原publisher原子提交前后复核封存来源与完整原字节。
    def require_generation_seal(self, token):
        from ..common.nofollow_fs import read_bytes_beneath
        from .multiprocess_generation_metadata import generation_role_files, generation_source_files
        from .multiprocess_records import serialize_record

        self.require_original()
        _require_generation_observation(self, token['observation'])
        if (token is not self._generation_seal_origin or not self._closing
                or self._generation_observation is not token['observation']
                or serialize_record(token['record']) != self._generation_seal_bytes
                or len(token['origins']) != self.scope.count
                or any(a is not b for a, b in zip(tuple(self._workers._created_origins), token['origins'], strict=True))):
            raise ValueError('generation_original_seal_required')
        root = self.control.paths.root
        if (generation_role_files(root, self.scope.count) not in (token['roles_before'], token['roles'])
                or generation_source_files(self.control.paths) != token['sources']
                or read_bytes_beneath(root, ('workers', 'registry.json'), max_bytes=1024 * 1024,
                                     require_dir_fd=True) != token['registry_raw']):
            raise ValueError('generation_original_seal_sources_changed')
        for name, content in token['evidence'].items():
            if read_bytes_beneath(root, tuple(name.split('/')), max_bytes=4 * 1024 * 1024,
                                 require_dir_fd=True) != content:
                raise ValueError('generation_original_tree_changed')
        _require_generation_roles(self, token['record']['closure']['registry'], token['roles'], token['origins'])

    # LLM: 原P3终态消费之后才置原recheck_stop，不先撤lease；仅原producer首次Thread引用/成功start ACK可真实join，换ref/未知启动永不按空表结清。
    # 函数用途: 停止已出生的本C只读复核设施，并保实际线程退出观察。
    def _stop_original_recheck(self, deadline):
        from .multiprocess_takeover_recovery import stop_takeover_rechecks
        self.require_original()
        original = self.controller._recheck_thread_origin
        if self._recheck_origin is None and original is not None:
            self._recheck_origin = original
        if original is not self._recheck_origin or self.controller._recheck_thread is not original:
            raise ValueError('drain_original_recheck_changed')
        if original is not None and type(original) is not threading.Thread:
            raise ValueError('drain_original_recheck_required')
        stop_takeover_rechecks(self.controller)
        if original is None:
            return ()
        unknown = self.controller._recheck_thread_start_unknown or original.ident is None
        if not unknown:
            original.join(timeout=max(0.0, deadline - time.monotonic()))
        state = 'unknown' if unknown else 'running' if original.is_alive() else 'settled'
        return (InFlightItem('control', self.scope, 'coordinator/readonly-recheck', state),)

    # LLM: 原树观察已门外完成；本原C/controller/lease末短门只包内容寻址同步I/O，撤权/换源拒写，不授新model/tool/P3权。
    # 函数用途: 在当前真实租约仍同一时保存原P2核实明细。
    def persist_tree_evidence(self, root, parts, content):
        from .multiprocess_takeover import _persist_evidence
        with self.controller._mutex, self._lease_origin._lifecycle_gate:
            self.require_original()
            expected = ('workers', 'takeover_evidence', hashlib.sha256(content).hexdigest() + '.json')
            if root != self.control.paths.root or parts != expected or not self._closing:
                raise ValueError('drain_original_evidence_source_required')
            _persist_evidence(root, parts, content)

    # LLM: 同一原C租约/control/controller仍实际登记才消费观察，closing本地登记不撤业务门，旧引用不能借新租约复活。
    # 函数用途: 检查排空观察仍属于最初那个真实C。
    def require_original(self):
        if (type(self) is not GatewayDrainRuntime or self._origin is not self or self._creator_pid != os.getpid()
                or self.control is not self._control_origin or self.controller is not self._controller_origin
                or self.control.controller() is not self.controller or self.controller._lease is not self._lease_origin
                or self.scope is not self._scope_origin or self.scope.count != self.control.worker_count
                or self.scope.c_run_id != self.control.c_run_id or self.scope.c_run_id != self._lease_origin.c_run_id
                or self._workers is not self._workers_origin):
            raise ValueError("drain_original_coordinator_required")
        with self._gate:
            if (len(self._resources) != len(self._resource_origins)
                    or any(self._resources.get(ref) is not group or group.kind != kind or group.resource_ref != ref
                           for kind, ref, group in self._resource_origins)):
                raise ValueError('drain_original_resource_coverage_changed')


# LLM: 六类已完整collector才observed；树项来自独立P1全集，不能从proof覆盖反推，未知tree不会消失。
# 函数用途: 评估一个准确角色的实际资源及执行树。
def _role_assessment(scope, items, trees):
    observation, tree_items = trees.get(scope, (None, ()))
    return assess_drain(DrainInventory(scope, True, STOP_DRAIN_KINDS, (*items, *tree_items), observation))


# LLM: 原全进程模型registry和bounded_Call表是事实源；purpose只消费原record.metadata结构字段，logical terminal/timeout保retain/worker/cleanup责任，不建影子字段或线程，不解析正文。
# 函数用途: 把本C仍保留的模型与取消清理调用列入停止阻塞。
def _local_call_items(scope):
    from ..backends.bounded_call import bounded_call_drain_snapshot
    from ..contracts.model_call_ledger import model_call_drain_snapshot
    items = []
    for ledger, record, retained in model_call_drain_snapshot():
        kind = "compression" if record.metadata.get('purpose') in {"conversation_compact_summary", "compact_carried_summary",
                                                  "compact_live_tool_summary"} else "foreground"
        state = "model_retained" if retained else "model_open"
        items.append(InFlightItem(kind, scope, f"model/{ledger.ledger_id}/{record.call_id}", state))
    items.extend(InFlightItem("foreground", scope, call.call_id, "worker_or_cleanup_retained")
                 for call in bounded_call_drain_snapshot())
    return tuple(items)


# LLM: 只检查本次原完整W出生/观察与当前P4/规范心跳，已确认waitstatus是必要条件但不能替代P1/P2。
# 函数用途: 拒绝漏W、错身份/instance/代次、仍ready或观察者被替换的封存。
def _require_generation_roles(runtime, registry, roles, origins, *, closing=False):
    from ..common.strict_json import load_strict_json
    from .daemon_metadata import process_host_id
    from .multiprocess_executor_identity import process_birth
    from .multiprocess_records import deserialize_record, serialize_record

    if (registry['c_run_id'] != runtime.scope.c_run_id or registry['worker_count'] != runtime.scope.count
            or registry['process_identity']['pid'] != os.getpid()
            or registry['process_identity']['start_time'] != process_birth(os.getpid())
            or registry['process_identity']['host_id'] != process_host_id()
            or len(origins) != runtime.scope.count
            or {row['worker'] for row in registry['workers']} != set(range(runtime.scope.count))):
        raise ValueError('generation_original_role_coverage_required')
    state = load_strict_json(roles['gateway_state.json'])
    if (state.get('pid') != os.getpid() or state.get('c_run_id') != runtime.scope.c_run_id
            or not closing and (state.get('status') != 'stopped' or state.get('drain_complete') is not True)):
        raise ValueError('generation_normal_stop_required')
    rows = {row['worker']: row for row in registry['workers']}
    from .multiprocess_takeover_runtime import original_drain_sources
    workers = dict(original_drain_sources(runtime.controller)[0])
    if (len(runtime._workers.processes) != len(origins) or len(runtime._workers.native_lanes) != len(origins)
            or any(process is not runtime._workers.processes[i] or lane is not runtime._workers.native_lanes[i]
                   for i, (lane, process, _observer) in enumerate(origins))):
        raise ValueError('generation_original_worker_coverage_required')
    for lane, process, observer in origins:
        index = lane.launch.partition.index
        row = rows[index]
        heartbeat = deserialize_record(roles[f'workers/w{index}.json'])
        captured = workers.get(index)
        lane._require_original()
        if (captured is None or captured[0] is not lane.issuer or captured[1] is not lane.launch
                or captured[2] is not process or observer.process is not process or not observer.confirmed or row['instance_id'] != lane.launch.instance_id
                or serialize_record(row['process_identity']) != observer.identity_bytes
                or heartbeat['process_identity'] != row['process_identity'] or heartbeat['instance_id'] != row['instance_id']
                or heartbeat['c_run_id'] != runtime.scope.c_run_id or heartbeat['worker'] != index
                or heartbeat['of'] != runtime.scope.count or heartbeat['ready'] or heartbeat['loop_health'] != 'stopped'):
            raise ValueError('generation_original_worker_exit_required')


# LLM: 只摘原完整tree observation里的已验证内容寻址引用，不序列化活验证对象或将历史证据再授新权。
# 函数用途: 保留非空原P1/P2关闭证据原字节，文件缺失或摘要不匹配即失败。
def _generation_tree_evidence(root, trees, historical=()):
    from ..common.nofollow_fs import read_bytes_beneath
    from ..common.strict_json import load_strict_json

    result = {}
    for observation, _items in trees.values():
        if observation is None:
            continue
        value = load_strict_json(observation.result_bytes)
        name = value['barrier']['ledger_ref']
        content = read_bytes_beneath(root, tuple(name.split('/')), max_bytes=4 * 1024 * 1024, require_dir_fd=True)
        if content is None or name != 'workers/takeover_evidence/' + hashlib.sha256(content).hexdigest() + '.json':
            raise ValueError('generation_tree_evidence_unconfirmed')
        result[name] = content
    from .multiprocess_records import deserialize_record
    for raw in historical:
        for name, digest in deserialize_record(raw)['closure']['tree_evidence'].items():
            content = read_bytes_beneath(root, tuple(name.split('/')), max_bytes=4 * 1024 * 1024, require_dir_fd=True)
            if content is None or hashlib.sha256(content).hexdigest() != digest:
                raise ValueError('generation_original_history_evidence_changed')
            result[name] = content
    return result


# LLM: 仅对已由原reader完整读取的字节取摘要，不把摘要当存活/物理退出证明。
# 函数用途: 为唯一generation关闭记录构造不含正文的清单。
def _generation_digests(values):
    return {name: hashlib.sha256(content).hexdigest() for name, content in values.items()}


# LLM: 只计算将由原publisher CAS发表的完整C marker字节，不先写成功投影；原stopped/PID/gen必须准确。
# 函数用途: 固定历史将绑定的最终marker，防止generation写后未读回窗口被当成关闭成功。
def _generation_final_roles(runtime, roles):
    from ..common.json_io import serialize_private_json_no_newline
    from ..common.strict_json import load_strict_json
    from .multiprocess_generation_metadata import generation_metadata_parts

    state = load_strict_json(roles['gateway_state.json'])
    if (state.get('status') != 'stopped' or state.get('c_run_id') != runtime.scope.c_run_id
            or state.get('pid') != os.getpid()):
        raise ValueError('generation_normal_stop_required')
    value = {**state, 'drain_complete': True,
             'generation_closure_ref': '/'.join(generation_metadata_parts(runtime.scope.c_run_id))}
    return {**roles, 'gateway_state.json': serialize_private_json_no_newline(value, sort_keys=True).encode('utf-8')}


# LLM: 每个旧目标须同代原captured v2及本次原prepare/P2，范围保旧C/W坐标；完整目标不丢，当前仍交原native collector。
# 函数用途: 在stop rechecks前观察全体旧树并保原receipt，不能从JSON补来源或重用启动结果。
def _observe_generation_trees(runtime, targets, deadline):
    from .multiprocess_drain_trees import original_tree_observations
    from .multiprocess_generation_metadata import (
        captured_generation_sources,
        read_generation_metadata,
        require_closed_generation,
    )
    from .multiprocess_stop_tree import freeze_tree_observation, require_stopped_tree
    from .multiprocess_takeover_runtime import (
        _current_receipt,
        _current_receipt_sources,
        _source_snapshot,
    )
    from .multiprocess_takeover_sources import confirmed_evidence

    controller = runtime.controller
    with controller._mutex:
        _require_drain_role_sources(runtime)
        sources = _source_snapshot(controller)
        closed_sources = tuple(controller._closed_role_sources)
    coverage = captured_generation_sources(runtime.control.paths.root, closed_sources)
    current, old, observations = [], [], {}
    for target in targets:
        if target.scope['c_run_id'] == runtime.scope.c_run_id:
            current.append(target)
            continue
        captured = dict(closed_sources).get(target.scope['c_run_id'])
        if captured is None:
            _require_inherited_target(runtime, target, coverage)
            prior = read_generation_metadata(runtime.control.paths.root, target.scope['c_run_id'])
            captured = prior[1] if prior is not None else None
        history = require_closed_generation(runtime.control.paths, target.scope['c_run_id'], runtime.scope.count,
            runtime.scope.c_run_id, captured, committed=_current_receipt_sources(controller), covered=coverage)
        remaining = deadline - time.monotonic()
        if remaining <= 0 or controller._recheck_stop.is_set():
            raise ValueError('drain_old_verification_window_unavailable')
        started = time.monotonic()
        result = controller.prepare_target(target, budget={'schema_version': 'multiprocess_takeover_budget.v1',
            'max_attempts': 5, 'deadline_seconds': min(1.0, remaining)})
        window = started, time.monotonic()
        if window[1] > deadline or not _current_receipt(controller, target, result, window):
            raise ValueError('drain_old_verification_not_current')
        confirmed_evidence(target, result)
        from .multiprocess_takeover_ledger import read_ledger_scope
        records = read_ledger_scope(target.ledger, target.scope)
        if records['failures']:
            raise ValueError('drain_old_p1_unconfirmed')
        scope = _original_target_stop_scope(target)
        observation = freeze_tree_observation(target.scope, result, records['records'])
        require_stopped_tree(scope, observation)
        if scope in observations:
            raise ValueError('drain_old_scope_duplicated')
        observations[scope] = observation, ()
        old.append((target, result, window, observation, history))
    # current和old逐项来自同一完整targets，不按代次丢弃；两个方向的独立scope必须互不覆盖。
    trees = original_tree_observations(runtime, current)
    if set(trees).intersection(observations) or len(current) + len(old) != len(targets):
        raise ValueError('drain_original_scope_coverage_changed')
    trees.update(observations)
    return trees, tuple(old), sources, closed_sources


# LLM: 坐标只取已独立发现的原target，不从proof反推；旧C没有W编号，旧N改变仍原保守拒绝。
# 函数用途: 保留旧域自身停止范围，避免同一物理桶跨代混合。
def _original_target_stop_scope(target):
    from .multiprocess_route import WorkerPartition
    scope = target.scope
    return (CoordinatorStopScope(scope['worker_count'], scope['c_run_id']) if scope['role'] == 'C'
            else WorkerPartition(scope['worker_index'], scope['worker_count'], scope['c_run_id']))


# LLM: 原观察只在本次closing内消费；完整raw/P1与captured closure及同次receipt均末核，不能从历史JSON复活authority。
# 函数用途: 检查内部关闭观察没有在native close或最终CAS前漂移。
def _require_generation_observation(runtime, observation):
    from .multiprocess_stop_tree import require_stopped_tree
    from .multiprocess_takeover_runtime import _current_receipt, _source_snapshot
    from .multiprocess_takeover_sources import confirmed_evidence

    runtime.require_original()
    if (observation is None or runtime._generation_observation is not observation
            or not same_durable_snapshot(observation[1], durable_drain_snapshot(runtime.control.paths, runtime.scope.count))):
        raise ValueError('generation_original_observation_changed')
    with runtime.controller._mutex:
        _require_drain_role_sources(runtime)
        if _source_snapshot(runtime.controller) != observation[3]:
            raise ValueError('generation_original_observation_sources_changed')
    from .multiprocess_generation_metadata import (
        captured_generation_sources,
        require_closed_generation,
    )
    from .multiprocess_takeover_runtime import _current_receipt_sources
    coverage = captured_generation_sources(runtime.control.paths.root, runtime.controller._closed_role_sources)
    for target, result, window, tree, history in observation[4]:
        require_closed_generation(runtime.control.paths, target.scope['c_run_id'], runtime.scope.count,
            runtime.scope.c_run_id, history[1], committed=_current_receipt_sources(runtime.controller), covered=coverage)
        if not _current_receipt(runtime.controller, target, result, window):
            raise ValueError('generation_original_old_receipt_changed')
        confirmed_evidence(target, result)
        require_stopped_tree(_original_target_stop_scope(target), tree)


# LLM: 一次真实assess成功对象由本runtime消费；尝试即不可重放，close_native失败不回落再assess或重开rechecks。
# 函数用途: 复用shutdown已有观察，独立seal尚未观察时内部只观察一次，然后准确关闭原native。
def _prepare_original_generation_closure(runtime):
    runtime.require_original()
    if (not runtime._closing or runtime._workers is None or runtime._generation_consume_attempted
            or runtime._generation_closing_sources is not None or runtime._closed_generation_ref is not None):
        raise ValueError('generation_original_closing_required')
    if runtime._generation_observation is None and not runtime._generation_assessment_attempted:
        runtime.assess()
    runtime._generation_consume_attempted = True
    observation = runtime._generation_observation
    try:
        if (observation is None or len(observation[0]) != runtime.scope.count + 1
                or not all(item.drained for item in observation[0])):
            raise ValueError('generation_original_drain_unconfirmed')
        _require_generation_observation(runtime, observation)
        origins = observation[5]
        _require_generation_closing_roles(runtime, origins)
        runtime._workers.close_native()
        _require_generation_closing_roles(runtime, origins)
        _require_generation_observation(runtime, observation)
        runtime._generation_closing_sources = observation, origins
    except BaseException:
        runtime._generation_observation = None
        raise


# LLM: 关闭只核旧角色原范围和当前固定回执raw，不调用要求活W的业务launch刷新；已退出原W的P1责任仍由完整collector核。
# 函数用途: 在原停止观察窗口复核角色来源，不以业务写许可代替退出后的物理责任。
def _require_drain_role_sources(runtime):
    from .multiprocess_takeover_runtime import _current_receipt_sources
    from .multiprocess_takeover_sources import require_role_coverage
    controller = runtime.controller
    actual = require_role_coverage(runtime.control.paths, runtime.scope.c_run_id,
        count=runtime.scope.count, captured=controller._closed_role_sources,
        committed=_current_receipt_sources(controller))
    if actual != controller._closed_role_sources:
        raise ValueError('generation_original_role_sources_changed')


# LLM: 更老P1须已在本controller捕获的后继完整v2范围逐raw存在，随后仍核其原同代v2及fresh P2；不以缺marker猜无角色。
# 函数用途: 多代停止时定位已由原关闭范围保留的旧目标，不丢历史或从JSON造proof。
def _require_inherited_target(runtime, target, coverage):
    from ..common.nofollow_fs import read_bytes_beneath
    expected = dict(coverage)
    if not expected:
        raise ValueError('drain_old_role_lifecycle_unproven')
    for identifier in target.scope['executor_reservation_ids']:
        name = f"workers/executors/w{target.ledger.partition.index}/{hashlib.sha256(identifier.encode()).hexdigest()}.json"
        raw = read_bytes_beneath(runtime.control.paths.root, tuple(name.split('/')), max_bytes=16384, require_dir_fd=True)
        if raw is None or expected.get(name) != hashlib.sha256(raw).hexdigest():
            raise ValueError('drain_old_role_lifecycle_unproven')


# LLM: close_native前后须同assessment时的完整原W/observer/P4；预关闭只核C当前身份，最终封存另必须stopped+drain。
# 函数用途: 避免观察成功后替换退出来源或角色还能先关闭native资源。
def _require_generation_closing_roles(runtime, origins):
    from ..common.nofollow_fs import read_bytes_beneath
    from .multiprocess_generation_metadata import generation_role_files
    from .multiprocess_records import deserialize_record
    if (len(runtime._workers._created_origins) != len(origins)
            or any(a is not b for a, b in zip(runtime._workers._created_origins, origins, strict=True))):
        raise ValueError('generation_original_worker_coverage_changed')
    root = runtime.control.paths.root
    registry = deserialize_record(read_bytes_beneath(root, ('workers', 'registry.json'),
                                  max_bytes=1024 * 1024, require_dir_fd=True))
    _require_generation_roles(runtime, registry, generation_role_files(root, runtime.scope.count), origins, closing=True)
