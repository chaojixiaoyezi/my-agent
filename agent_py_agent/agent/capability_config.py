from __future__ import annotations

"""LLM: compatibility facade for capability config moved to `agent.capability.config`.

给人看的解释：
能力配置已经归到 capability 目录。这里继续导出旧名字，避免命令行和测试被迁移打断。
"""

from .capability.config import CapabilityConfig, load_capability_config

__all__ = ["CapabilityConfig", "load_capability_config"]
