"""后台团队命令消费同一业务 API；不复用 /agents 规则入口。"""
from __future__ import annotations

# LLM: 团队命令统一经 run_business_cli 分发给 BusinessClient；
# 命令参数与选项通过冻结 Schema 校验，不自行构造业务 Agent。
# 模块用途: 提供 my-agent team 命令入口（list/show/create/pause/resume/retire/restore/model）。
from .space_commands import run_business_cli


# LLM: 执行 team 子命令，将 CLI 参数转送给统一业务客户端。
# 函数用途: 处理 my-agent team 命令。
def cmd_team(args) -> int:
    return run_business_cli(args, command='team')
