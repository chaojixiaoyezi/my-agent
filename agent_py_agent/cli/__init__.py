from __future__ import annotations

"""LLM: package for CLI command modules split by operational domain.

给人看的解释：
命令行入口已经按领域拆开：common/local/subagents/daemon/gateway/adapter/scenario/chat/parser。
`agent_py_agent.__main__` 仍然保留对外兼容导入。
"""
