
from __future__ import annotations

"""compatibility facade for capability config moved to `agent.capability.config`.

能力配置已经归到 capability 目录。这里继续导出旧名字，避免命令行和测试被迁移打断。
"""

from .capability import (
    CapabilityConfig,
    CapabilityConfigPatch,
    CapabilityConfigPatchRequest,
    CapabilityConfigPatchResult,
    CapabilityConfigReloadResult,
    CapabilityConfigSnapshot,
    apply_capability_config_patch,
    capability_config_version,
    default_capability_config_path,
    load_capability_config,
    load_capability_config_snapshot,
    reload_capability_config_if_changed,
)

__all__ = [
    "CapabilityConfig",
    "CapabilityConfigPatch",
    "CapabilityConfigPatchRequest",
    "CapabilityConfigPatchResult",
    "CapabilityConfigReloadResult",
    "CapabilityConfigSnapshot",
    "apply_capability_config_patch",
    "capability_config_version",
    "default_capability_config_path",
    "load_capability_config",
    "load_capability_config_snapshot",
    "reload_capability_config_if_changed",
]
