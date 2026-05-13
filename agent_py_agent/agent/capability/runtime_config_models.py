# LLM: Runtime config models are shared by patch, reload, tools, and tests.
# 模块用途: 定义 capability_config 运行期补丁、快照和结果数据结构。

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import CapabilityConfig


# LLM: CapabilityConfigPatch carries one requested field update from an agent or tool.
# 类用途: 表示一次 capability_config 字段变更请求；字段和值分开，方便校验和审计。
@dataclass(frozen=True)
class CapabilityConfigPatch:
    field: str
    value: Any
    reason: str = ""


# LLM: CapabilityConfigPatchRequest keeps config write scope, version, audit, and apply mode together.
# 类用途: 统一传递配置补丁请求，避免模型或业务层散传路径、字段和值。
@dataclass(frozen=True)
class CapabilityConfigPatchRequest:
    config_path: str | Path
    patches: list[CapabilityConfigPatch] = field(default_factory=list)
    apply: bool = False
    expected_version: str = ""
    actor: str = "agent"
    reason: str = ""
    audit_path: str | Path | None = None
    notice_path: str | Path | None = None
    scope: dict[str, object] = field(default_factory=dict)


# LLM: CapabilityConfigPatchResult is the stable response consumed by tools, docs, and tests.
# 类用途: 返回配置补丁的结果、重载后的配置、审计路径和未自动执行的建议。
@dataclass(frozen=True)
class CapabilityConfigPatchResult:
    ok: bool
    applied: bool
    version_before: str
    version_after: str
    config: CapabilityConfig
    changed_fields: list[str] = field(default_factory=list)
    suggestions: list[dict[str, object]] = field(default_factory=list)
    blocked_fields: list[str] = field(default_factory=list)
    audit_path: str = ""
    notice_path: str = ""
    message: str = ""

    # LLM: to_dict keeps the model-facing tool output JSON serializable without leaking dataclass internals.
    # 函数用途: 把补丁结果转成稳定字典，供 capability_config_patch 工具返回。
    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["config"] = asdict(self.config)
        return payload


# LLM: CapabilityConfigSnapshot is the reload token for watch/dispatch loops.
# 类用途: 保存一次 capability_config 文件快照，后续用 hash 判断是否需要热加载。
@dataclass(frozen=True)
class CapabilityConfigSnapshot:
    path: Path
    config: CapabilityConfig
    version: str
    mtime_ns: int
    size: int


# LLM: CapabilityConfigReloadResult tells callers whether new dispatch cycles should use a new config.
# 类用途: 返回热加载结果；只影响后续轮次，不直接改动已运行的子代理。
@dataclass(frozen=True)
class CapabilityConfigReloadResult:
    snapshot: CapabilityConfigSnapshot
    changed: bool
    message: str = ""
