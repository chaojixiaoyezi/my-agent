# LLM: worker 就绪只证明当前代 C 状态及本进程端口登记；坏时间含不可转float的巨大数均不健康，投影不授予执行权，更不是死亡证明。
# 模块用途: 校验就绪门、构造心跳和查询可盖章分区，不认领请求、不加载 owner 数据。
from __future__ import annotations

import math
import os

from ..attempt import sandbox
from .daemon_metadata import build_process_identity, process_host_id
from .io import read_json_file_report
from .multiprocess_mode import WorkerLaunch
from .multiprocess_route import WorkerPartition

# 心跳周期 2 秒，允许短暂停顿；超过 15 秒只停止盖章，绝不据此接手或宣布进程死亡。
WORKER_HEARTBEAT_SECONDS = 2.0
# 超过 15 秒停止盖章，容忍短暂停顿；这是可用性阈值而非进程死亡证明。
WORKER_STALE_SECONDS = 15.0


# LLM: 先读取当前代状态，再登记并读回进程端口表；任何失败不就绪，不做 HomePaths/Agent 初始化。
# 函数用途: 校验 C 的身份和实际端口，确认命令沙箱构造能拿到拒绝端口。
def prepare_worker_readiness(launch: WorkerLaunch) -> dict:
    report = read_json_file_report(launch.gateway_root / "gateway_state.json", context="gateway.worker.coordinator")
    reason = "coordinator_state_unreadable" if report.load_error else _coordinator_rejection(report.payload, launch)
    if reason:
        return _not_ready(reason)
    port = report.payload["http_port"]
    sandbox.register_gateway_bound_port(port)
    ports = sandbox.gateway_bound_ports()
    if not ports or any(type(p) is not int or not 0 < p <= 65535 for p in ports) or port not in ports:
        return _not_ready("gateway_port_readback")
    return {"ready": True, "gateway_ports": list(ports)}


# LLM: worker 必须是 C 的直接子进程；直接父 PID 加代次是启动合同，启动指纹原样保存但不足时不宣称死亡。
# 函数用途: 验证 C 的代次、身份字段和绑定端口，返回拒绝原因。
def _coordinator_rejection(payload: dict, launch: WorkerLaunch) -> str:
    if payload.get("c_run_id") != launch.partition.c_run_id:
        return "coordinator_generation"
    identity = payload.get("process_identity")
    if not isinstance(identity, dict) or not {"host_id", "pid", "start_time"} <= identity.keys():
        return "coordinator_identity"
    if type(payload.get("pid")) is not int or payload["pid"] != launch.parent_pid or os.getppid() != launch.parent_pid:
        return "coordinator_parent"
    if identity.get("pid") != launch.parent_pid or identity.get("host_id") != process_host_id():
        return "coordinator_identity"
    if identity["start_time"] != build_process_identity(launch.parent_pid)["start_time"]:
        return "coordinator_identity"
    port = payload.get("http_port")
    return "coordinator_port" if type(port) is not int or not 0 < port <= 65535 else ""


# LLM: 不就绪原因必须结构化可观察，不能吞成空心跳或推断网络/权限原因。
# 函数用途: 构造统一的就绪失败结果。
def _not_ready(reason: str) -> dict:
    return {"ready": False, "error_code": "WORKER_NOT_READY", "reason": reason}


# LLM: inflight由当前调度泵观察传入；拒绝坏计数，启动无请求可为零；启动身份沿原权威函数。
# 函数用途: 为原子写入构造当前worker的真实健康投影，不用心跳替代死亡证明。
def worker_heartbeat(launch: WorkerLaunch, readiness: dict, now: float) -> dict:
    inflight = readiness.get("inflight", 0)
    if type(inflight) is not int or inflight < 0:
        raise ValueError("worker_inflight_invalid")
    return {"worker": launch.partition.index, "of": launch.partition.count, "pid": os.getpid(),
            "process_identity": build_process_identity(), "c_run_id": launch.partition.c_run_id,
            "ready": readiness.get("ready") is True, "loop_health": "healthy" if readiness.get("ready") else "not_ready",
            "inflight": inflight, "updated_at": now, "readiness": dict(readiness)}


# LLM: 只返回当前代健康心跳；未知/不可读/旧代/陈旧均不盖章，不把拒绝列表当死亡清单。
# 函数用途: 给 C 提供可以盖章的分区编号集合。
def stampable_workers(heartbeats: list[dict], partition: WorkerPartition, now: float) -> list[int]:
    return sorted({h["worker"] for h in heartbeats if _stampable(h, partition, now)})


# LLM: 时间异常、超float范围的整数与bool均保守拒绝；15秒只是健康窗口，不能变成死亡或接手证明。
# 函数用途: 对一份心跳做最小健康和当前代校验。
def _stampable(heartbeat: dict, partition: WorkerPartition, now: float) -> bool:
    if not isinstance(heartbeat, dict) or type(heartbeat.get("worker")) is not int:
        return False
    updated = heartbeat.get("updated_at")
    try:
        valid_time = type(updated) in {int, float} and math.isfinite(updated)
    except OverflowError:
        valid_time = False
    if not valid_time:
        return False
    return (0 <= heartbeat["worker"] < partition.count and heartbeat.get("of") == partition.count
            and heartbeat.get("c_run_id") == partition.c_run_id and heartbeat.get("ready") is True
            and heartbeat.get("loop_health") == "healthy" and 0 <= now - updated <= WORKER_STALE_SECONDS)


# LLM: 控制事实只认同代的布尔字段，旧代或自然语言不触发排空/退出。
# 函数用途: 读取当前代排空或退出指令。
def worker_control_action(payload: dict, c_run_id: str) -> str:
    if payload.get("c_run_id") != c_run_id:
        return ""
    if payload.get("exit") is True:
        return "exit"
    if payload.get("drain") is True:
        return "drain"
    return ""
