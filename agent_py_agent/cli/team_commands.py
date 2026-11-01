"""后台团队命令消费同一业务 API；不复用 /agents 规则入口。"""
from .space_commands import run_business_cli


def cmd_team(args) -> int:
    return run_business_cli(args, command='team')
