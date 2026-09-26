# LLM: 托管身份只来自 Gateway 服务进程启动时写进自身环境的进程号，工具、后台命令和 runner 子进程自然继承；
# 不解析命令文本或模型正文。只拦“停止/重启托管自己的那台 Gateway”，别的 Gateway 进程号不同照常放行。
# 已知边界：直接 kill 进程号或调用 HTTP /stop 不经过这里。需要重启时走 restart_gateway 工具 / /restart 的安全重启。
# 环境变量名的唯一定义在 agent/gateway_parts/restart_service。改动时同步 test_gateway_host_guard.py。
# 模块用途: 防止 Gateway 托管的工具进程执行 gateway stop/restart/start --force，把正在执行它的对话回合切断。
from __future__ import annotations

import os
import sys

from ..agent.gateway_parts.restart_service import HOSTING_GATEWAY_PID_ENV

_REFUSAL = (
    "拒绝执行：这条命令运行在 Gateway（pid={pid}）托管的工具进程里。停止或重启这台 Gateway "
    "会切断正在执行这条命令的对话回合，回合结果将无法确认。用 /model 或 manage_models 修改的模型配置"
    "在下一次请求时直接生效，不需要重启 Gateway。确实需要重启时，管理员会话请改用 restart_gateway 工具"
    "安排安全重启（先排空在跑的回合和工具再换进程，完成后会通知你继续）；其他会话请告诉用户由管理员用 /restart 处理。"
)


# LLM: 只在 Gateway 服务进程入口调用一次；覆盖从父进程继承的旧值，保证子进程只指向当前托管者。
# 函数用途: 把当前 Gateway 进程号写进自身环境变量，之后它启动的所有子进程都会继承这个托管标记。
def mark_hosting_gateway_process() -> None:
    os.environ[HOSTING_GATEWAY_PID_ENV] = str(os.getpid())


# LLM: 目标进程号取自调用方已确认在运行的 pid 记录；只有与继承的托管进程号完全相等才拒绝，缺失或坏值一律放行。
# 函数用途: 生命周期命令在写停止请求之前调用；返回 True 表示已拒绝并把原因写到 stderr，调用方应以失败码退出。
def refuse_stopping_hosting_gateway(target_pid: int) -> bool:
    try:
        hosting_pid = int(os.environ.get(HOSTING_GATEWAY_PID_ENV, "") or 0)
    except ValueError:
        return False
    if hosting_pid <= 0 or hosting_pid != int(target_pid or 0):
        return False
    print(_REFUSAL.format(pid=hosting_pid), file=sys.stderr)
    return True


__all__ = [
    "HOSTING_GATEWAY_PID_ENV",
    "mark_hosting_gateway_process",
    "refuse_stopping_hosting_gateway",
]
