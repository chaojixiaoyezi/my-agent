# LLM: Core typed protocol primitives shared by action envelope modules.
# 模块用途: 定义协议版本、运行范围、产物/证据/路径引用和 JSON 归一化 helper。

from __future__ import annotations

"""Core refs for the typed action protocol.

给人看的解释：
这些类型不执行任何动作，只负责让工具、子代理、compact 恢复包都能带稳定的
scope 和 refs。自然语言 summary 不会在这里被解析成事实。
"""

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

ACTION_PROTOCOL_SCHEMA_VERSION = 1


# LLM: _now_iso keeps created_at deterministic in shape and timezone-aware.
# 函数用途: 生成 UTC ISO 时间字符串，供 envelope 记录创建时间。
def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# LLM: RunScope is the ownership boundary carried by every typed action envelope.
# 类用途: 保存请求、会话、任务、运行和 owner 信息，让工具/子代理动作能按范围追踪和校验。
@dataclass(frozen=True)
class RunScope:
    request_id: str = ""
    session_id: str = ""
    task_id: str = ""
    run_id: str = ""
    owner_type: str = ""
    owner_id: str = ""
    parent_run_id: str = ""
    root_task_id: str = ""
    reserved: dict[str, Any] = field(default_factory=dict)

    # LLM: to_dict gives callers JSON-safe scope data without exposing dataclass internals.
    # 函数用途: 把 RunScope 转成可写入 JSON 的字典。
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    # LLM: from_dict tolerates legacy or partial scope payloads during migration.
    # 函数用途: 从字典恢复 RunScope，缺字段时使用空值并保留 reserved。
    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> RunScope:
        data = payload if isinstance(payload, dict) else {}
        return cls(
            request_id=str(data.get("request_id") or ""),
            session_id=str(data.get("session_id") or ""),
            task_id=str(data.get("task_id") or ""),
            run_id=str(data.get("run_id") or ""),
            owner_type=str(data.get("owner_type") or ""),
            owner_id=str(data.get("owner_id") or ""),
            parent_run_id=str(data.get("parent_run_id") or ""),
            root_task_id=str(data.get("root_task_id") or ""),
            reserved=_dict_or_empty(data.get("reserved")),
        )


# LLM: ArtifactRef is a refs-first pointer to a real artifact owned by a task/run.
# 类用途: 保存产物 ID、路径、类型和摘要；后续读取产物应通过 ref resolver，而不是靠模型复述路径。
@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    path: str
    kind: str = "file"
    owner_run_id: str = ""
    hash: str = ""
    summary: str = ""
    reserved: dict[str, Any] = field(default_factory=dict)

    # LLM: to_dict serializes artifact refs for result envelopes and ledgers.
    # 函数用途: 把 ArtifactRef 转成 JSON 字典。
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    # LLM: from_dict accepts partial artifact refs while preserving future fields.
    # 函数用途: 从字典恢复 ArtifactRef；缺少可选字段时补默认值。
    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ArtifactRef:
        return cls(
            artifact_id=str(payload.get("artifact_id") or payload.get("id") or ""),
            path=str(payload.get("path") or ""),
            kind=str(payload.get("kind") or "file"),
            owner_run_id=str(payload.get("owner_run_id") or ""),
            hash=str(payload.get("hash") or ""),
            summary=str(payload.get("summary") or ""),
            reserved=_dict_or_empty(payload.get("reserved")),
        )


# LLM: PathRef is a small normalized pointer derived only from structured refs.
# 类用途: 保存可读取路径引用及来源；不会从 summary 或自然语言里猜路径。
@dataclass(frozen=True)
class PathRef:
    path: str
    kind: str = "file"
    owner_run_id: str = ""
    source: str = ""
    reserved: dict[str, Any] = field(default_factory=dict)

    # LLM: to_dict serializes path refs for result envelopes and recovery packets.
    # 函数用途: 把 PathRef 转成 JSON 字典。
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    # LLM: from_dict restores path refs from typed envelopes without reading files.
    # 函数用途: 从字典恢复 PathRef；缺失字段补默认值。
    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> PathRef:
        return cls(
            path=str(payload.get("path") or ""),
            kind=str(payload.get("kind") or "file"),
            owner_run_id=str(payload.get("owner_run_id") or ""),
            source=str(payload.get("source") or ""),
            reserved=_dict_or_empty(payload.get("reserved")),
        )


# LLM: EvidenceRef points to machine-checkable evidence instead of trusting summaries.
# 类用途: 保存验收声明和对应 evidence/artifact 引用，避免父级从自然语言 summary 猜事实。
@dataclass(frozen=True)
class EvidenceRef:
    evidence_id: str
    claim: str
    checked_scope: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    artifact_refs: list[str] = field(default_factory=list)
    confidence: float = 0.0
    reserved: dict[str, Any] = field(default_factory=dict)

    # LLM: to_dict serializes evidence refs for subagent result envelopes.
    # 函数用途: 把 EvidenceRef 转成 JSON 字典。
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    # LLM: from_dict keeps legacy evidence packet ids compatible with the new ref type.
    # 函数用途: 从 evidence packet 或 ref 字典恢复 EvidenceRef。
    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> EvidenceRef:
        return cls(
            evidence_id=str(payload.get("evidence_id") or payload.get("id") or ""),
            claim=str(payload.get("claim") or ""),
            checked_scope=str(payload.get("checked_scope") or ""),
            evidence_refs=_string_list(payload.get("evidence_refs")),
            artifact_refs=_string_list(payload.get("artifact_refs")),
            confidence=_float_or_zero(payload.get("confidence")),
            reserved=_dict_or_empty(payload.get("reserved")),
        )


# LLM: _dict_or_empty avoids passing arbitrary scalar values into reserved/args fields.
# 函数用途: 把未知输入规整为字典，非字典返回空字典。
def _dict_or_empty(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


# LLM: _dict_list keeps nested refs/tests robust during migration from legacy JSON.
# 函数用途: 从任意值里提取字典列表，过滤坏条目。
def _dict_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


# LLM: _string_list normalizes model-provided arrays without preserving non-string junk.
# 函数用途: 从任意值里提取非空字符串列表。
def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


# LLM: _float_or_zero makes evidence confidence safe to parse from loose JSON.
# 函数用途: 把 confidence 转成 float，失败时返回 0.0。
def _float_or_zero(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
