# LLM: Top-level package module; keep package entry points and import compatibility stable.
# 模块用途: 组织 Python 包入口，让 CLI 和模块导入能找到 agent 代码。

"""Installable package for the my-agent CLI."""

__all__ = ["__version__"]

__version__ = "0.3.0"
