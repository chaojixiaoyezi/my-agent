# LLM: 原C/P2启动W并消费同次原门回执和同步出生；P4/generation只定位，关闭仍消费原Popen退出观察，禁止凭返回标签补ready；同步启动/关闭/Count0测试。
# 模块用途: 在正式Gateway生命周期中创建、登记并回收本代worker子进程。
import os
import subprocess
import sys
import threading
import time
import uuid

from ..agent.gateway_parts.daemon_metadata import process_host_id
from ..agent.gateway_parts.io import write_json_file_atomic
from ..agent.gateway_parts.multiprocess_bootstrap import WorkerBootstrapError
from ..agent.gateway_parts.multiprocess_bootstrap_publish import (
    publish_worker_registry,
    read_worker_registry,
)
from ..agent.gateway_parts.multiprocess_executor_identity import process_birth
from ..agent.gateway_parts.multiprocess_mode import WorkerLaunch, worker_environment
from ..agent.gateway_parts.multiprocess_records import validate_record
from ..agent.gateway_parts.multiprocess_route import WorkerPartition
from ..agent.gateway_parts.multiprocess_takeover_runtime import spawn_coordinator_worker


# LLM: 内核出生未知即失败，不回退PID；仅查询本次Popen返回的进程，不枚举其它进程。
# 函数用途: 为原P4登记生成可验证的真实进程身份。
def worker_process_identity(pid):
    return validate_record({'schema_version': 'multiprocess_process_identity.v1',
                            'host_id': process_host_id(), 'pid': pid, 'start_time': process_birth(pid)})


# LLM: Popen句柄只属于当前C；原registry保留真实身份和P1引用，局部列表不授予接手或判死权。
# 类用途: 管理当前Gateway亲自创建的worker及正常退出等待。
class CoordinatorWorkers:
    # LLM: 构造纯内存，control/self/PID/出生集合保持；首次start才捕原generation lease，P6只消费实际Popen/waitstatus，不从metadata补历史退出。
    # 函数用途: 保存本代原worker启动依赖与实际出生/退出观察。
    def __init__(self, control, config_path, http_port):
        self.control, self.config_path, self.http_port = control, config_path, http_port
        self.processes = []
        self.native_lanes = []
        self._origin, self._creator_pid = self, os.getpid()
        self._control_origin = control
        self._created_origins = []
        self._generation_source = self._generation_source_origin = None

    # LLM: 原P2唯一启动门；先核C已解析roots与agent_root再运输原launch，不借home补根；同调用只准原线程一次出生，原对象齐才门外attach。
    # 函数用途: 启动获准分区并核本次真实出生；失败保原资源，由关闭观察处理，握手不授权W写。
    def start(self):
        from ..agent.gateway_parts.multiprocess_native_runtime import NativeWorkerLane

        control = self.control
        from ..agent.gateway_parts.multiprocess_coordinator import current_coordinator

        if self._generation_source is not self._generation_source_origin:
            raise ValueError('generation_original_coordinator_required')
        if self._generation_source is None:
            self._generation_source = self._generation_source_origin = current_coordinator(
                control.paths.root, control.worker_count)
        if control.worker_count > 0 and self._generation_source is None:
            raise ValueError('generation_original_coordinator_required')
        previous = read_worker_registry(control.paths.root)
        roots = getattr(control, "workspace_roots", ())
        if control.worker_count > 0 and (type(roots) is not tuple or not roots
                or getattr(control, "agent_root", None) != roots[0]):
            raise WorkerBootstrapError("worker_workspace_required", "workspace_roots")
        record = {'schema_version': 'multiprocess_worker_registry.v1', 'c_run_id': control.c_run_id,
                  'worker_count': control.worker_count, 'process_identity': worker_process_identity(os.getpid()),
                  'http_port': self.http_port, 'workers': []}
        try:
            for index in range(control.worker_count):
                launch = WorkerLaunch(WorkerPartition(index, control.worker_count, control.c_run_id),
                                      control.paths.root, control.home_root, os.getpid(), uuid.uuid4().hex, roots)
                created = []
                call_window = [True, threading.get_ident(), False]

                # LLM: 每个slot独立闭包cell，禁止旧回调在下一slot原门内借到新窗口；只运输原引用，不另造许可。
                # 函数用途: 为当前slot固定一次性出生回调，循环前进不改变已交出的原来源。
                def spawn_for(launch, index, call_window, created):
                    # LLM: 回调窗口只作限制，不签许可；原P2同步门内先拒晚到/异线程/重复，再捕实际Popen/P4/退出观察，握手仍在门外。
                    # 函数用途: 在本次原调用中保存唯一worker出生并发布P4；不让保存的回调稍后再生副作用。
                    def spawn():
                        nonlocal previous, record
                        if not call_window[0] or call_window[1] != threading.get_ident() or call_window[2]:
                            raise WorkerBootstrapError("worker_spawn_callback_outside_window", "worker")
                        call_window[2] = True
                        lane = NativeWorkerLane(launch)
                        self.native_lanes.append(lane)
                        process = self._spawn(launch)
                        self.processes.append(process)
                        from ..agent.gateway_parts.multiprocess_drain_process import (
                            OriginalWorkerExitObservation,
                        )

                        identity = worker_process_identity(process.pid)
                        observation = OriginalWorkerExitObservation(process, identity)
                        if observation.supported:
                            observation.install()
                        self._created_origins.append((lane, process, observation))
                        created.append((lane, process))
                        record = {**record, 'workers': [*record['workers'], {
                            'worker': index, 'instance_id': launch.instance_id,
                            'process_identity': identity,
                            'executor_ledger_ref': f'workers/executors/w{index}'}]}
                        previous = publish_worker_registry(control.paths.root, record, previous,
                                                           generation_source=self._generation_source)
                    return spawn

                spawn = spawn_for(launch, index, call_window, created)
                try:
                    decision = spawn_coordinator_worker(launch, spawn)
                finally:
                    call_window[0] = False
                if (type(decision) is not dict or decision.get("status") != "started"
                        or type(decision.get("worker_index")) is not int or decision["worker_index"] != index
                        or decision.get("c_run_id") != control.c_run_id or len(created) != 1):
                    error = WorkerBootstrapError("worker_start_unconfirmed", "worker")
                    # 原结构返回仅供拒绝诊断；既不重签P2，也不把它当P4/ready或物理退出凭据。
                    error.decision = decision
                    raise error
                for lane, process in created:
                    lane.attach(control, process)
            return self
        except BaseException:
            self.close()
            raise

    # LLM: 正式W继承同一解释器；argv与env同载荷运输原roots，不更改PATH/命令执行权；stdout沿C日志，不持未消费PIPE。
    # 函数用途: 启动一个尚未执行业务的worker，W须等自己的原登记后才创建池。
    def _spawn(self, launch):
        command = [sys.executable, '-m', 'agent_py_agent', '--config', str(self.config_path), 'gateway', 'worker',
                   '--index', str(launch.partition.index), '--of', str(launch.partition.count),
                   '--run', launch.partition.c_run_id, '--gateway-root', str(launch.gateway_root),
                   '--owner-root', str(launch.owner_root), '--parent-pid', str(launch.parent_pid),
                   '--instance', launch.instance_id]
        for root in launch.workspace_roots:
            command.extend(['--workspace-root', str(root)])
        return subprocess.Popen(command, stdin=subprocess.DEVNULL, env=worker_environment(launch, dict(os.environ)))

    # LLM: 实际observer在reap前核同P4birth/原Popen，cached returncode不能结账；defer_native给P6保端点/树refs，退出不等整树，门外等。
    # 函数用途: 停止本代worker并保准确内核退出，完整排空前可保留原native来源。
    def close(self, *, defer_native=False):
        if not self.processes:
            if not defer_native:
                self.close_native()
            return
        write_json_file_atomic(self.control.paths.root / 'workers/control.json',
                               {'c_run_id': self.control.c_run_id, 'exit': True})
        deadline = time.monotonic() + 15
        pending = []
        for _lane, process, observation in self._created_origins:
            if observation.supported:
                if not observation.wait_until(deadline):
                    pending.append(process.pid)
            else:
                try:
                    process.wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    pending.append(process.pid)
        if len(self._created_origins) != len(self.processes):
            pending.extend(p.pid for p in self.processes if not any(p is row[1] for row in self._created_origins))
        if pending:
            raise RuntimeError('worker_exit_unconfirmed')
        if not defer_native:
            self.close_native()

    # LLM: 只在已有完整P6证明后关原endpoint；lane.close仍逐域/host/原pin核和真实join，不能用本函数返回代替六类观察。
    # 函数用途: 关闭已结清的本代原native消费者，未知仍抛错保引用。
    def close_native(self):
        for lane in self.native_lanes:
            lane.close()

    # LLM: 原created/Popen/observer与controller首绑定issuer/P4集合精确一致；只透传同次runtime已核closed_sources作分类，默认空，不改W退出/观察或授信。
    # 函数用途: 用直接W内核退出和完整原持久观察评估每个worker，沿同一分类资料保留旧控制范围。
    def drain_assessments(self, durable, trees, *, closed_sources=()):
        from ..agent.gateway_parts.multiprocess_drain_runtime import _role_assessment
        from ..agent.gateway_parts.multiprocess_drain_sources import durable_items
        from ..agent.gateway_parts.multiprocess_stop_drain import InFlightItem
        from ..agent.gateway_parts.multiprocess_takeover_runtime import original_drain_sources
        if (self._origin is not self or self._creator_pid != os.getpid() or self.control is not self._control_origin
                or len(self._created_origins) != self.control.worker_count):
            raise ValueError('drain_original_worker_coverage_missing')
        original, _sources, _hosts = original_drain_sources(self.control.controller())
        origins = dict(original)
        results = []
        for lane, process, observation in self._created_origins:
            lane._require_original()
            scope = lane.launch.partition
            captured = origins.get(scope.index)
            if (captured is None or captured[0] is not lane.issuer or captured[1] is not lane.launch
                    or captured[2] is not process or observation.process is not process):
                raise ValueError('drain_original_worker_source_changed')
            state = 'settled' if observation.confirmed else 'worker_exit_unconfirmed'
            items = (*durable_items(scope, durable, closed_sources=closed_sources), InFlightItem('foreground', scope, f'worker/{scope.index}', state))
            results.append(_role_assessment(scope, items, trees))
        return tuple(results)
