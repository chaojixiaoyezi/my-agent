# LLM: 本地C主体背景host只走原CoordinatorExecutorSource/T/P3/P5、P1/bootstrap/kernel sender；root仅留原refs无第二权威，设施不误进用户namespace，不能legacy裸ledger，同步C工具/P5 tests。
# 模块用途: 让原C已批准后台工具复用原native host协议，并给原controller保留真实宿主与责任收尾引用。
import os
from dataclasses import replace

from .multiprocess_executor_start import ExecutorStartObserver
from .multiprocess_native_host import start_native_host
from .multiprocess_worker_commit import WorkerCommitRejected


# LLM: 只在精确C source与原claim短门内注册，角色不来自JSON；host/process映射仅原实际引用，P2不得用新的root替旧对象证明。
# 类用途: 保存本地主体的一次native host启动根，协议与端点沿原设施共用。
class CoordinatorNativeHostRoot:
    # LLM: 保存原来源/creator/表，不复制lease/owner/authority；注册只进原source._commit，无模型或长等待。
    # 函数用途: 将实际后台host组合根挂在原controller供P2和关闭收集。
    def __init__(self, source, observer):
        from .multiprocess_coordinator_executor_source import CoordinatorExecutorSource
        if type(source) is not CoordinatorExecutorSource:
            raise WorkerCommitRejected('original_coordinator_native_host_source_required')
        source._require_source_identity()
        self._origin, self.creator_pid = self, os.getpid()
        self.source = self._source_origin = source
        self._observer = observer if observer is not None else ExecutorStartObserver()
        self._hosts, self._processes = {}, {}

        # LLM: 只在原source._commit的本次T/C末门把同一活对象加入原controller，不从P1文件重建或另造权限表。
        # 函数用途: 登记实际host根，供原C物理收尾保留引用，不启动进程。
        def remember():
            self._require_original()
            controller = source._sources[4]
            if not hasattr(controller, '_current_coordinator_native_host_roots'):
                controller._current_coordinator_native_host_roots = []
            controller._current_coordinator_native_host_roots.append(self)
        source._commit(remember)

    # LLM: 在任何原gate/host副作用前拒copy/fork/来源换壳；closing只核身份不复活原claim。
    # 函数用途: 固定同C和同Coordinator source，表字段不是新许可。
    def _require_original(self):
        if self._origin is not self or self.creator_pid != os.getpid() or self.source is not self._source_origin:
            raise WorkerCommitRejected('original_coordinator_native_host_required')
        self.source._require_source_identity()

    # LLM: 完成回送不再授权新动作；未知/活host保持原端点/pin，精确completed host可关闭；wait/join在原T/C外，P1保留不删除。
    # 函数用途: 在原controller收尾轮里关闭真正已结清的消费者，不能借函数返回推断全树停止。
    def collect_completed_hosts(self):
        self._require_original()
        for host in tuple(self._hosts.values()):
            host._require_original()
            if host.session is not None and host.session.completed:
                host.close()
                if host.process is not None:
                    host.process.poll()

    # LLM: root中每个原host必须保startup/实际域/末回送完整责任，缺口不清表或关闭；原lease pin由host精确receipt结清。
    # 函数用途: 给原C停止/退出检查实际仍待结清的宿主，不默认自动signal。
    def require_drained(self):
        self._require_original()
        for host in tuple(self._hosts.values()):
            host.require_drained()


# LLM: 精确C源/已批准host固定命令，原bootstrap/directC Popen/P1/kernel sender不换链；成功仅包原C不reap句柄保末回送出生，失败不fallback/重放，核短host真实竞态。
# 函数用途: 为原C主体工具启动强来源native host，返回原真实两阶段结果供原工具处理。
def launch_coordinator_native_host(source, row, command, observer=None):
    from .multiprocess_coordinator_executor_source import CoordinatorExecutorSource
    if (type(source) is not CoordinatorExecutorSource or row['kind'] != 'background_host'
            or row['schema_version'] != 'multiprocess_executor_reservation.v1'):
        raise WorkerCommitRejected('original_coordinator_native_host_source_required')
    source._bound_record(row)
    root = CoordinatorNativeHostRoot(source, observer)
    result = start_native_host(root, source, row, command)
    if result is None or not result.ok:
        return result
    from .multiprocess_native_coordinator_process import CoordinatorNativeHostProcess
    return replace(result, process=CoordinatorNativeHostProcess(root, result, command.argv))
