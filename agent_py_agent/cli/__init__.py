# LLM: CLI surface module; keep argparse/Typer wiring, stdout text, and service-call boundaries stable.
# 模块用途: 提供命令行入口或辅助函数，把用户命令转换成 agent 服务调用。

from __future__ import annotations

"""package for CLI command modules split by operational domain.

给人看的解释：
命令行入口已经按领域拆开：common/local/subagents/daemon/gateway/adapter/scenario/chat/parser。
`agent_py_agent.__main__` 仍然保留对外兼容导入。
"""
