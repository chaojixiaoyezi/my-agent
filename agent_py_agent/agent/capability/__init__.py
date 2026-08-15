
from __future__ import annotations

"""public API for capability routing, capability config, and skill cards.

这里是'能力治理'目录。skill 和 tool 都会先变成能力卡，再由父代理判断该给谁、给多少、
什么时候上抛缺口。以后 resource、MCP、remote agent 也应该接到这里。
"""

from .config import CapabilityConfig, load_capability_config
from .router import (
    CapabilityCard,
    CapabilityRouter,
    CapabilitySearchHit,
    classify_tool_model_risk,
    default_capability_cards,
    from_skill_card,
    from_tool_model_spec,
    score_card,
    tokenize,
)
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
from .skill_service import SkillsService
from .skill_snapshot import SkillLoadError, SkillSnapshot, SkillSnapshotEntry, SkillSnapshotError
from .skills import SkillCard, parse_skill_file

__all__ = [
    "CapabilityCard",
    "CapabilityConfig",
    "CapabilityConfigPatch",
    "CapabilityConfigPatchRequest",
    "CapabilityConfigPatchResult",
    "CapabilityConfigReloadResult",
    "CapabilityRouter",
    "CapabilitySearchHit",
    "CapabilityConfigSnapshot",
    "SkillCard",
    "SkillLoadError",
    "SkillSnapshot",
    "SkillSnapshotEntry",
    "SkillSnapshotError",
    "SkillsService",
    "apply_capability_config_patch",
    "capability_config_version",
    "classify_tool_model_risk",
    "default_capability_cards",
    "default_capability_config_path",
    "from_skill_card",
    "from_tool_model_spec",
    "load_capability_config",
    "load_capability_config_snapshot",
    "parse_skill_file",
    "reload_capability_config_if_changed",
    "score_card",
    "tokenize",
]
