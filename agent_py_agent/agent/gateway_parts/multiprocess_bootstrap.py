# LLM: P4 只冻结已有 WorkerLaunch，不建立第二份进程模式；接线由启动组合根后续完成。
# 模块用途: 在任何 Agent/owner 初始化前核对受信启动合同，不做文件初始化。
from dataclasses import dataclass
from pathlib import Path

from .multiprocess_mode import WorkerLaunch, current_worker_partition
from .multiprocess_records import MultiprocessRecordError, validate_record
from .multiprocess_route import WorkerPartition


# LLM: 沿 P0 结构化错误载体，调用方必须失败关闭，不能退回普通 Agent。
# 类用途: 报告启动快照缺失或漂移，不回显输入正文。
class WorkerBootstrapError(MultiprocessRecordError):
    error_code = "WORKER_BOOTSTRAP_INVALID"


# LLM: 嵌套对象必须是原不可变 WorkerLaunch/WorkerPartition，不保存可变配置引用。
# 类用途: 保存本次 worker 启动的模式及根/分区/代次。
@dataclass(frozen=True)
class WorkerBootstrap:
    mode: str
    launch: WorkerLaunch


# LLM: 由 worker 早期入口调用，不创建 HomePaths 或读取 owner 数据。
# 函数用途: 核对已建立的模式并冻结启动参数。
def freeze_worker_bootstrap(launch: WorkerLaunch) -> WorkerBootstrap:
    validate_worker_launch(launch)
    frozen = WorkerBootstrap("worker", launch)
    require_worker_bootstrap(frozen, launch)
    return frozen


# LLM: Agent构造/owner物化重复核对同次workspace tuple，不能从home、热配置或当前请求补齐；无写入/模式更新。
# 函数用途: 拒绝冻结后被配置或调用参数改变的启动事实。
def require_worker_bootstrap(frozen: WorkerBootstrap, launch: WorkerLaunch) -> None:
    if type(frozen) is not WorkerBootstrap or frozen.mode != "worker":
        raise WorkerBootstrapError("worker_mode_mismatch", "mode")
    validate_worker_launch(frozen.launch)
    validate_worker_launch(launch)
    for field in ("owner_root", "gateway_root", "parent_pid", "partition", "instance_id", "workspace_roots"):
        if getattr(frozen.launch, field) != getattr(launch, field):
            raise WorkerBootstrapError("frozen_launch_mismatch", field)
    if current_worker_partition() != frozen.launch.partition:
        raise WorkerBootstrapError("worker_mode_mismatch", "mode")


# LLM: 严格原不可变类型及roots tuple，P0校唯一代次；不resolve路径/读环境/初始化，实际构造另拒空roots。
# 函数用途: 阻止可变伪分区、相对路径和 bool 数值进入启动快照。
def validate_worker_launch(launch: WorkerLaunch) -> None:
    if type(launch) is not WorkerLaunch or type(launch.partition) is not WorkerPartition:
        raise WorkerBootstrapError("invalid_launch", "partition")
    partition = launch.partition
    try:
        validate_record({"schema_version": "multiprocess_c_generation.v1", "c_run_id": partition.c_run_id,
                         "worker_count": partition.count})
    except MultiprocessRecordError as error:
        raise WorkerBootstrapError(error.reason, error.field) from error
    if type(partition.index) is not int or not 0 <= partition.index < partition.count:
        raise WorkerBootstrapError("invalid_launch", "partition")
    if type(launch.parent_pid) is not int or launch.parent_pid <= 0:
        raise WorkerBootstrapError("invalid_launch", "parent_pid")
    for field in ("owner_root", "gateway_root"):
        path = getattr(launch, field)
        if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
            raise WorkerBootstrapError("invalid_launch", field)
    roots = launch.workspace_roots
    if (type(roots) is not tuple
            or any(not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts for path in roots)
            or len(set(roots)) != len(roots)):
        raise WorkerBootstrapError("invalid_launch", "workspace_roots")
