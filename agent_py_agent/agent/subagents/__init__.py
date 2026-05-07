# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""LLM contract: subagent domain package with focused modules.

Human version:
这个包把原来巨大的 subagent.py 拆成数据模型、渲染、解析、规则和管理器几块。
外部代码仍然可以通过 agent.subagent 兼容入口导入旧名字。
"""
