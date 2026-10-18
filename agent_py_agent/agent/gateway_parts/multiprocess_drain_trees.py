# LLM: P6只消费原C首绑定source/actualDomain/HostLane与独立完整P1，Linux用户全域沿原pidfd/ns屏障；无原历史/旧v1/旧代或Mac无匹配P0方法保持unknown，不从Popen缓存/JSON重建，同步全树与遗漏/换壳测试。
# 模块用途: 汇集原内核排空证据；启动缺会话保留未知，绝不以空来源替代原P2证明。
import hashlib
import json
import sys
import time

from .multiprocess_records import record_digest
from .multiprocess_route import WorkerPartition
from .multiprocess_stop_drain import InFlightItem
from .multiprocess_stop_scope import CoordinatorStopScope
from .multiprocess_stop_tree import freeze_tree_observation
from .multiprocess_takeover import _proof
from .multiprocess_takeover_ledger import read_ledger_scope
from .multiprocess_takeover_recheck import _verified_result
from .multiprocess_takeover_runtime import original_drain_sources
from .multiprocess_takeover_sources import confirmed_evidence


# LLM: 原controller/C来源完整快照与当前W native lanes原对象同一；握手未建session须交原unknown收口，不签空树或吞二次AttributeError；同步启动失败与完整排空测试。
# 函数用途: 收集本次原执行域；部分启动缺会话时明确保留排空阻塞，不遮住原启动失败。
def _original_objects(runtime):
    _workers, sources, roots = original_drain_sources(runtime.controller)
    domains = [domain for source, _origin in sources for domain in tuple(source._domains)]
    hosts = [host for root in roots for host in tuple(root._hosts.values())]
    if runtime._workers is not None:
        from .multiprocess_native_runtime import NativeWorkerLane
        for lane in tuple(runtime._workers.native_lanes):
            if type(lane) is not NativeWorkerLane:
                raise ValueError('drain_original_native_lane_required')
            lane._require_original()
            session = lane.executor_lane.session
            if session is None:
                raise ValueError('drain_native_executor_session_unavailable')
            domains.extend(tuple(session._execution_domains))
            hosts.extend(tuple(session._hosts.values()))
    for host in hosts:
        host._require_original()
        if host.session is not None:
            domains.extend(tuple(host.session._command_domains))
    return tuple(domains), tuple(hosts)


# LLM: 唯一原domain必须与准确P1/source/actualPopen绑定；empty只消费原kernel屏障，v1 host须本原C出生waitstatus和末回送两者，不读缓存。
# 函数用途: 对一条独立P1记录判断是否已有真实完整停止来源。
def _record_stopped(row, domains, hosts):
    from .multiprocess_native_linux_domain import NativePidNamespaceDomain
    for domain in domains:
        reserved = domain.reserved if type(domain) is NativePidNamespaceDomain else domain.row
        if reserved['reservation_id'] != row['reservation_id']:
            continue
        if type(domain) is not NativePidNamespaceDomain or sys.platform != 'linux':
            return False
        domain._require_original()
        if domain.row is None:
            return (row == domain.reserved and not domain.creation_attempted and domain.process is None)
        if row != domain.row:
            return False
        return domain.empty(domain.process, domain.row)
    for host in hosts:
        if host.row is None or host.row['reservation_id'] != row['reservation_id']:
            continue
        host._require_original()
        host.require_drained()
        observation = getattr(host, '_exit_observation', None)
        if observation is None or host.row != row or host.process is not observation.process:
            return False
        return observation.observe() and observation.confirmed
    return False


# LLM: 独立P1前后与scope全部准确覆盖，旧代无原对象仍unknown；明细沿原内容寻址写入/读回，写失败不得生成stopped，证据不授接手权。
# 函数用途: 将已存在的真实内核回执合成为本次P2停止观察。
def _tree_observation(runtime, target, domains, hosts):
    started = time.monotonic()
    before = read_ledger_scope(target.ledger, target.scope)
    instances = []
    for row in before['records']:
        stopped = _record_stopped(row, domains, hosts)
        instances.append({'reservation_id': row['reservation_id'], 'status': 'stopped' if stopped else 'unknown',
                          'evidence_kinds': ['original_native_kernel_receipt'] if stopped else ['original_history_missing']})
    after = read_ledger_scope(target.ledger, target.scope)
    failures = [*before['failures'], *after['failures']]
    if before != after:
        failures.append('executor_ledger_changed')
    detail = {'schema_version': 'multiprocess_takeover_evidence.v1', 'c_run_id': target.scope['c_run_id'],
              'covered_scope': target.scope, 'platform': sys.platform,
              'method': 'pid_namespace' if sys.platform == 'linux' else 'unavailable',
              'instances': instances, 'failures': sorted(set(failures)), 'residual_risks': [],
              'record_digests': {r['reservation_id']: record_digest(r) for r in before['records']}}
    unresolved = [row['reservation_id'] for row in instances if row['status'] != 'stopped']
    if failures:
        unresolved.append('ledger_unconfirmed')
    content = json.dumps(detail, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    parts = ('workers', 'takeover_evidence', hashlib.sha256(content).hexdigest() + '.json')
    runtime.persist_tree_evidence(target.ledger.root, parts, content)
    barrier = {'schema_version': 'multiprocess_takeover_barrier.v1', 'c_run_id': target.scope['c_run_id'],
               'stopped': not unresolved, 'ledger_ref': '/'.join(parts), 'unresolved_instances': sorted(set(unresolved))}
    result = _verified_result({'barrier': barrier, 'proof': _proof(detail, barrier),
                              'error': None if not unresolved else {'error_code': 'OWNER_PARTITION_QUARANTINED',
                                                                   'reason': 'original_kernel_tree_unconfirmed'}},
                             started, time.monotonic())
    if not unresolved:
        confirmed_evidence(target, result)
    return freeze_tree_observation(target.scope, result, before['records']), before['records'], not unresolved


# LLM: 每个角色完整P1目标独立提供，C不映射W0；同角色旧代不可被当前域结清，缺scope/遗漏目标始终是阻塞。
# 函数用途: 给六类collector生成按原角色分开的树观察和全部执行者事项。
def original_tree_observations(runtime, targets):
    runtime.require_original()
    domains, hosts = _original_objects(runtime)
    observations = {}
    for target in targets:
        index = target.scope['worker_index']
        scope = (CoordinatorStopScope(runtime.scope.count, runtime.scope.c_run_id) if index is None
                 else WorkerPartition(index, runtime.scope.count, runtime.scope.c_run_id))
        if scope in observations or target.scope['c_run_id'] != runtime.scope.c_run_id:
            rows = read_ledger_scope(target.ledger, target.scope)['records']
            old_items = observations.get(scope, (None, ()))[1]
            observations[scope] = None, (*old_items, *(InFlightItem('executor_tree', scope, r['reservation_id'],
                                                                  'original_history_missing') for r in rows))
            continue
        observation, rows, stopped = _tree_observation(runtime, target, domains, hosts)
        observations[scope] = observation, tuple(InFlightItem('executor_tree', scope, r['reservation_id'],
                                                              'settled' if stopped else 'unknown') for r in rows)
    runtime.require_original()
    return observations
