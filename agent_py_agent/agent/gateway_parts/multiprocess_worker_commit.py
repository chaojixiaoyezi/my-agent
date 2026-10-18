# LLM: 仅C内使用原Popen、P0出生、P4登记、原controller及lease；peer_pid必须由后续内核传输适配器提供。
# 模块用途: 为一个真实W记录精确在途提交责任；不是JSON授权或已经接通的native通道。
import os
import subprocess

from .daemon_metadata import process_host_id
from .multiprocess_bootstrap_publish import read_worker_registry
from .multiprocess_coordinator import (
    CoordinatorUnavailable,
    coordinator_for_root,
    pin_coordinator_commit,
    retire_coordinator_commit,
)
from .multiprocess_executor_identity import process_birth
from .multiprocess_mode import WorkerLaunch, _valid_launch
from .multiprocess_records import validate_record
from .multiprocess_route import owner_digest, owner_worker


# LLM: 结构化失败不回显用户正文；调用方必须保留未结清责任，不能将断线或超时当成功。
# 类用途: 表示不能证实原W提交或精确结清。
class WorkerCommitRejected(RuntimeError):
    error_code = 'WORKER_COMMIT_REJECTED'


# LLM: 仅可信launcher交原Popen；本对象不由文件重建，不物化owner，不跨fork运输。
# 类用途: 把原C准入短门与原lease在途责任组合起来。
class WorkerCommitIssuer:
    # LLM: 构造独立捕获本对象及原launch/Popen引用和P0/C身份，浅拷贝/公开字段替换不能改来源；P4不符拒绝。
    # 函数用途: 固定一个真实worker实例与它自己的协调者来源。
    def __init__(self, control, launch, process):
        self.control, self.launch, self.process = control, launch, process
        self._issuer_origin = self
        self._source_origin = (launch, process)
        self._creator_pid = os.getpid()
        self._lease = coordinator_for_root(control.paths.root, context=control)
        self._controller = control.controller()
        self._pins = set()
        self._executor_records = {}
        if (type(launch) is not WorkerLaunch or not _valid_launch(launch)
                or type(process) is not subprocess.Popen or process.poll() is not None
                or launch.gateway_root != control.paths.root or launch.owner_root != control.home_root
                or launch.partition.count != control.worker_count or launch.partition.c_run_id != control.c_run_id
                or launch.parent_pid != self._creator_pid or not launch.instance_id):
            raise WorkerCommitRejected('original_worker_launch_required')
        self._identity = _process_identity(process.pid)
        self._c_identity = _process_identity(self._creator_pid)
        self._require_current()
        self._require_worker(process.pid)
        from .multiprocess_takeover_runtime import bind_current_worker_source

        bind_current_worker_source(self._controller, self)

    # LLM: 出生核验在生命周期门外；原P2 writer短门内再次核原登记/句柄，只有同lease可增加pin。
    # 函数用途: 先核原worker、owner桶及隔离，再登记本次真实责任，不持gate跑远端业务。
    def begin(self, owner, *, peer_pid):
        self._require_current()
        self._require_worker(peer_pid)
        if owner_worker(owner, self.launch.partition.count) != self.launch.partition.index:
            raise WorkerCommitRejected('owner_partition_mismatch')
        digest = owner_digest(owner)
        pinned = []

        # LLM: 原controller.launch_owner_writer最终门内再次核同一W出生/代次，pin只加入原lease和issuer集合，不授另一请求许可。
        # 函数用途: 在真实worker开始短提交前登记准确责任，异常由原调用方保留未知状态。
        def commit():
            self._require_current()
            self._require_worker(peer_pid)
            pin = pin_coordinator_commit(self._lease, (self.control.c_run_id, 'W',
                    str(self.launch.partition.index), self.launch.instance_id, digest))
            self._pins.add(pin)
            pinned.append(pin)

        decision = self._controller.launch_owner_writer(digest, commit)
        if decision.get('status') != 'started' or not pinned:
            raise WorkerCommitRejected('owner_commit_unavailable')
        return pinned[0]

    # LLM: 仅原C可信接线调用；预留与来源登记共内存门，原P1落盘成功才登记精确摘要；文件不能补造登记。
    # 函数用途: 将原C实际提交的P1预留记为本issuer来源，不给同桶未知记录授权。
    def reserve_executor(self, record):
        return self._commit_executor(record, None, None)

    # LLM: 只允许此原W的已提交精确快照推进；原P1比较/FSM/fsync仍是唯一持久权威。
    # 函数用途: 推进本issuer的P1状态并同时更新原C观察摘要。
    def advance_executor(self, record, state, registration=None):
        return self._commit_executor(record, state, registration)

    # LLM: C内短门覆盖原租约/原Popen/P4核验、规范写入和精确来源发布；不从磁盘恢复此映射，不接正文PID。
    # 函数用途: 串行执行一个受信P1提交；失败不把其它记录认作自产来源。
    def _commit_executor(self, record, state, registration):
        from .multiprocess_executor_ledger import ExecutorLedger
        from .multiprocess_records import record_digest

        self._require_current()
        self._require_worker(self.process.pid)
        ledger = ExecutorLedger(self.control.paths.root, self.launch.partition)
        source = ledger._bound_record(record)
        committed = []

        # LLM: 由原P2短门同步调用，写入成功与摘要发布不可被另一次源发现分开。
        # 函数用途: 提交唯一canonical记录并保存本原W的精确内存来源。
        def commit():
            self._require_current()
            self._require_worker(self.process.pid)
            identifier = source['reservation_id']
            if state is not None and self._executor_records.get(identifier) != record_digest(source):
                raise WorkerCommitRejected('original_executor_submission_required')
            row = (ledger.reserve(source) if state is None else ledger.advance(source, state, registration))
            self._executor_records[identifier] = record_digest(row)
            committed.append(row)

        decision = self._controller.launch_owner_writer(source['owner_sha'], commit)
        if decision.get('status') != 'started' or not committed:
            raise WorkerCommitRejected('owner_executor_commit_unavailable')
        return committed[0]

    # LLM: closing仍允许原issuer结清自己的原pin；浅拷贝须在生命周期锁前拒绝，跨issuer/重复finish亦拒绝。
    # 函数用途: 精确结束原对象的既有责任并通知原协调者撤权等待者。
    def finish(self, pin):
        if self._creator_pid != os.getpid():
            raise WorkerCommitRejected('original_coordinator_process_required')
        if self._issuer_origin is not self:
            raise WorkerCommitRejected('original_commit_issuer_required')
        with self._lease._lifecycle_gate:
            if pin not in self._pins:
                raise WorkerCommitRejected('original_commit_pin_required')
            try:
                retire_coordinator_commit(self._lease, pin)
            except CoordinatorUnavailable as error:
                raise WorkerCommitRejected('original_commit_lease_required') from error
            self._pins.remove(pin)

    # LLM: 先核原issuer自身身份再访问原controller/lease，浅拷贝、缺来源或同run_id替换C不能借用。
    # 函数用途: 每次新提交锁前确认本原对象仍属于当前原C。
    def _require_current(self):
        if self._issuer_origin is not self:
            raise WorkerCommitRejected('original_commit_issuer_required')
        if (self._creator_pid != os.getpid() or self._lease is None or self._controller is None
                or coordinator_for_root(self.control.paths.root, context=self.control) is not self._lease
                or self.control.controller() is not self._controller):
            raise WorkerCommitRejected('original_coordinator_context_required')

    # LLM: peer来自内核适配器而非请求；独立捕获的原launch/Popen引用、P0 PID和P4出生/实例必须同时相符。
    # 函数用途: 拒绝句柄/launch引用替换、PID替用、错实例、跨桶、登记替换及已退出worker。
    def _require_worker(self, peer_pid):
        launch, process = self._source_origin
        if self.launch is not launch or self.process is not process:
            raise WorkerCommitRejected('original_worker_source_required')
        if (type(peer_pid) is not int or peer_pid != self._identity['pid']
                or peer_pid != process.pid or process.poll() is not None):
            raise WorkerCommitRejected('original_worker_process_required')
        registry = read_worker_registry(self.control.paths.root)
        if (registry is None or registry['c_run_id'] != self.control.c_run_id
                or registry['worker_count'] != self.control.worker_count
                or registry['process_identity'] != self._c_identity):
            raise WorkerCommitRejected('original_worker_registry_required')
        matches = [row for row in registry['workers'] if row['worker'] == self.launch.partition.index]
        if (len(matches) != 1 or matches[0]['instance_id'] != self.launch.instance_id
                or matches[0]['process_identity'] != self._identity
                or matches[0]['executor_ledger_ref'] != f'workers/executors/w{self.launch.partition.index}'):
            raise WorkerCommitRejected('original_worker_registry_required')


# LLM: P0完整身份由当前本机内核出生观察生成；未知出生拒绝，不退回仅PID。
# 函数用途: 捕获实际进程身份，不将心跳或环境当出生证据。
def _process_identity(pid):
    return validate_record({'schema_version': 'multiprocess_process_identity.v1',
                           'host_id': process_host_id(), 'pid': pid, 'start_time': process_birth(pid)})
