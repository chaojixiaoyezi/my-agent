# LLM: Compatibility facade for runtime capability config patching and hot reload.
# 模块用途: 汇总 capability_config 运行期补丁、版本、快照和热加载公开入口。

from __future__ import annotations

from .runtime_config_models import (
    CapabilityConfigPatch,
    CapabilityConfigPatchRequest,
    CapabilityConfigPatchResult,
    CapabilityConfigReloadResult,
    CapabilityConfigSnapshot,
)
from .runtime_config_patch import apply_capability_config_patch
from .runtime_config_reload import (
    capability_config_version,
    default_capability_config_path,
    load_capability_config_snapshot,
    reload_capability_config_if_changed,
)

__all__ = [
    "CapabilityConfigPatch",
    "CapabilityConfigPatchRequest",
    "CapabilityConfigPatchResult",
    "CapabilityConfigReloadResult",
    "CapabilityConfigSnapshot",
    "apply_capability_config_patch",
    "capability_config_version",
    "default_capability_config_path",
    "load_capability_config_snapshot",
    "reload_capability_config_if_changed",
]
