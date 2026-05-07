# LLM: Agent package module; keep public imports and cross-module compatibility stable.
# 模块用途: 提供 agent 核心功能的一部分，对外暴露稳定入口或兼容转发。

from __future__ import annotations

"""compatibility facade for capability config moved to `agent.capability.config`.

给人看的解释：
能力配置已经归到 capability 目录。这里继续导出旧名字，避免命令行和测试被迁移打断。
"""

from .capability.config import CapabilityConfig, load_capability_config

__all__ = ["CapabilityConfig", "load_capability_config"]
