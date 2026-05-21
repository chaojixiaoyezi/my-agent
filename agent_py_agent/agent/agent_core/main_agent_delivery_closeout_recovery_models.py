# LLM: Delivery closeout recovery models keep action-generation helpers on compact request objects.
# 模块用途: 保存恢复动作生成中的 ledger/context/request 数据类，避免主 recovery 模块承载模型定义。

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


# LLM: RecoveryActionLedger bundles mutable action output and de-duplication state.
# 类用途: 在恢复动作生成链路里携带 actions/seen，避免 helper 参数持续扩散。
@dataclass
class RecoveryActionLedger:
    actions: list[dict[str, object]]
    seen: set[str]


# LLM: StagingActionContext carries one artifact staging contract plus resolved workspace facts.
# 类用途: 把 builder、source、checkpoint、列要求等结构化字段绑定为一个恢复动作上下文。
@dataclass(frozen=True)
class StagingActionContext:
    ledger: RecoveryActionLedger
    staging: dict[str, Any]
    artifact_exists: bool
    builder_tool: str
    source_ref: str
    output_ref: str
    source_path: Path | None
    required_columns: list[str]
    required_sheets_min: int
    source_shape_hint: str
    workspace_root: Path


# LLM: CheckpointQualityActionRequest bundles staged JSON quality action inputs.
# 类用途: 避免 checkpoint 质量恢复函数暴露多个并列参数。
@dataclass(frozen=True)
class CheckpointQualityActionRequest:
    ledger: RecoveryActionLedger
    checkpoint_ref: str
    checkpoint_path: Path
    required_columns: list[str] | None = None
    required_sheets_min: int = 0
    checkpoint_shape_hint: str = ""


# LLM: StagedEvidenceActionRequest bundles staged evidence validation action inputs.
# 类用途: 避免 evidence 恢复函数在 checkpoint、workspace 和 validation 合同之间继续扩参。
@dataclass(frozen=True)
class StagedEvidenceActionRequest:
    ledger: RecoveryActionLedger
    checkpoint_ref: str
    workspace_root: Path
    validation_contract: dict[str, object]


__all__ = [
    "CheckpointQualityActionRequest",
    "RecoveryActionLedger",
    "StagedEvidenceActionRequest",
    "StagingActionContext",
]
