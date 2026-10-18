# LLM: 进程模式只在最早CLI建立；导入把同一冻结查询接到executor及global-write拒门，不授C/管理员权，不构造Home/Agent或增加Count0 I/O；同步两拒门与worker启动测试。
# 模块用途: 保存C核验的worker分区，供执行登记和全局写入口拒绝受限进程。
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from ..common.executor_registration import bind_worker_requirement
from ..common.global_write_guard import bind_global_write_restriction

_WORKER_PARTITION = None


# LLM: C固定队列/home/父进程及已解析constructor roots；roots只是原构造参数运输，不授owner访问权，空tuple只供纯旧合同不可实际构造。
# 类用途: 保存一个worker的原启动身份和业务工作根，防止把数据home替代工作区。
@dataclass(frozen=True)
class WorkerLaunch:
    partition: object
    gateway_root: Path
    owner_root: Path
    parent_pid: int
    instance_id: str = ""
    workspace_roots: tuple[Path, ...] = ()


# LLM: 环境由C注入；先核载体结构再完整匹配roots等原事实才冻结模式，坏载体不进入编码，不写Home/配置/索引。
# 函数用途: 在 CLI 普通引导之前确立只读 worker 身份，缺标记/错参数一律拒绝。
def establish_worker_mode(launch: WorkerLaunch) -> dict:
    global _WORKER_PARTITION
    raw = os.environ.get("MY_AGENT_GATEWAY_WORKER")
    if not raw:
        return {"ok": False, "error_code": "WORKER_MODE_REQUIRED"}
    try:
        supplied = json.loads(raw)
    except (TypeError, ValueError):
        return {"ok": False, "error_code": "WORKER_MODE_INVALID"}
    if not _valid_launch(launch) or supplied != _launch_payload(launch):
        return {"ok": False, "error_code": "WORKER_MODE_INVALID"}
    if _WORKER_PARTITION is not None and launch.partition != _WORKER_PARTITION:
        return {"ok": False, "error_code": "WORKER_MODE_INVALID"}
    _WORKER_PARTITION = launch.partition
    return {"ok": True}


# LLM: 只由可信 C/隔离测试设施调用，复制环境而不修改 C 本身；不提供用户配置入口。
# 函数用途: 为一个固定 worker 构造进程启动环境。
def worker_environment(launch: WorkerLaunch, environment: dict[str, str]) -> dict[str, str]:
    if not _valid_launch(launch):
        raise ValueError("invalid worker launch")
    return {**environment, "MY_AGENT_GATEWAY_WORKER": json.dumps(_launch_payload(launch), sort_keys=True)}


# LLM: roots沿C同次解析顺序编码，argv与环境必须完整一致；缺省仅兼容纯launch校验，不为实际W补home或配置根。
# 函数用途: 编码需要逐字段核对的原启动事实，不创建HomePaths。
def _launch_payload(launch: WorkerLaunch) -> dict:
    return {"partition": asdict(launch.partition), "gateway_root": str(launch.gateway_root),
            "owner_root": str(launch.owner_root), "parent_pid": launch.parent_pid,
            **({"instance_id": launch.instance_id} if launch.instance_id else {}),
            **({"workspace_roots": [str(path) for path in launch.workspace_roots]} if launch.workspace_roots else {})}


# LLM: 非法分区/非tuple或非规范roots不能确立模式；旧空roots不授权构造，父进程仍须同C。
# 函数用途: 校验可信启动上下文的结构和值。
def _valid_launch(launch: WorkerLaunch) -> bool:
    partition = launch.partition
    return (type(partition.count) is int and type(partition.index) is int and 0 <= partition.index < partition.count
            and isinstance(partition.c_run_id, str) and bool(partition.c_run_id)
            and launch.gateway_root.is_absolute() and launch.owner_root.is_absolute()
            and type(launch.parent_pid) is int and launch.parent_pid > 0
            and type(launch.workspace_roots) is tuple
            and all(isinstance(path, Path) and path.is_absolute() and ".." not in path.parts for path in launch.workspace_roots)
            and len(set(launch.workspace_roots)) == len(launch.workspace_roots))


# LLM: 返回进程启动冻结的分区，不把请求或运行中的配置当成模式权威。
# 函数用途: 为 worker 拒绝门提供当前分区；默认单进程为空。
def current_worker_partition():
    return _WORKER_PARTITION


# 同一纯内存分区同时限制执行登记和全局写；缺请求上下文的W仍受限，默认无新增I/O。
bind_worker_requirement(current_worker_partition)
bind_global_write_restriction(current_worker_partition)
