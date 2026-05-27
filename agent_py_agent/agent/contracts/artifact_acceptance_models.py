# LLM: Shared artifact acceptance data models stay independent from format-specific validators.
# 模块用途: 定义产物验收请求、报告、finding 和 ArtifactRef 生成逻辑，避免 validator 文件继续膨胀。

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from ..action_protocol_core import ArtifactRef
from .contract_validation_recovery import recovery_for_findings


# LLM: ArtifactAcceptanceRequest bundles one artifact validation request.
# 类用途: 描述要验收的产物路径和可选根目录，后续扩展更多格式时继续走 bundle。
@dataclass(frozen=True)
class ArtifactAcceptanceRequest:
    path: Path
    workspace_root: Path | None = None
    validation_contract: dict[str, object] | None = None


# LLM: ArtifactFinding is a machine-readable issue for repair prompts and QA reports.
# 类用途: 保存产物验收发现的问题代码、严重级别、位置和说明，避免只靠自然语言自检。
@dataclass(frozen=True)
class ArtifactFinding:
    code: str
    severity: str
    message: str
    location: str = ""
    value: str = ""

    # LLM: to_dict keeps findings easy to serialize into reports or repair packets.
    # 函数用途: 转成普通 dict，供 JSON 报告、前端或修复提示使用。
    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "location": self.location,
            "value": self.value,
        }


# LLM: advisory_artifact_finding downgrades quality findings without hiding them.
# 函数用途: 将主观质量或覆盖度问题变成 warning，让 closeout 给模型返工提示而非硬停任务。
def advisory_artifact_finding(finding: ArtifactFinding) -> ArtifactFinding:
    """Return a non-blocking copy of a quality or coverage finding."""

    return ArtifactFinding(
        code=finding.code,
        severity="warning",
        message=finding.message,
        location=finding.location,
        value=finding.value,
    )


# LLM: advisory_artifact_findings applies non-blocking semantics to a finding list.
# 函数用途: 批量把产物质量 findings 转成 warning，保留代码、位置和值给后续修复。
def advisory_artifact_findings(findings: list[ArtifactFinding]) -> list[ArtifactFinding]:
    """Downgrade subjective/content contract findings to closeout warnings."""

    return [advisory_artifact_finding(item) for item in findings]


# LLM: ArtifactAcceptanceReport is the refs-first outcome for one artifact validation.
# 类用途: 保存产物验收是否通过和结构化 findings；不复制产物正文。
@dataclass(frozen=True)
class ArtifactAcceptanceReport:
    ok: bool
    artifact_ref: str
    artifact_kind: str = "generic"
    findings: list[ArtifactFinding] = field(default_factory=list)
    recovery: dict[str, object] | None = None

    # LLM: to_dict keeps acceptance reports stable across CLI, docs, and future QA agents.
    # 函数用途: 输出机器可读报告，方便 repair worker 或主代理按 findings 修复。
    def to_dict(self) -> dict[str, object]:
        payload = {
            "ok": self.ok,
            "artifact_ref": self.artifact_ref,
            "artifact_ref_payload": artifact_ref_payload(self.artifact_ref, self.artifact_kind).to_dict(),
            "artifact_kind": self.artifact_kind,
            "findings": [item.to_dict() for item in self.findings],
        }
        recovery = self.recovery or recovery_for_findings("artifact_acceptance", payload["findings"])
        if recovery is not None:
            payload["recovery"] = recovery
        return payload


# LLM: artifact_ref_payload turns a validated file into the shared ArtifactRef contract.
# 函数用途: 根据产物路径生成 artifact_id、kind、hash 和 size，后续恢复/QA 不再解析自然语言路径。
def artifact_ref_payload(path: str | Path, kind: str = "") -> ArtifactRef:
    artifact_path = Path(path)
    digest = _artifact_hash(artifact_path)
    suffix_kind = kind or kind_for_path(artifact_path)
    return ArtifactRef(
        artifact_id=_artifact_id(artifact_path, digest),
        path=str(artifact_path),
        kind=suffix_kind,
        hash=digest,
        reserved={"size_bytes": _artifact_size(artifact_path)},
    )


# LLM: kind_for_path gives reports a stable kind even for unknown suffixes.
# 函数用途: 根据后缀生成 artifact_kind；无后缀时返回 generic。
def kind_for_path(path: Path) -> str:
    return path.suffix.lower().lstrip(".") or "generic"


# LLM: _artifact_hash keeps artifact refs content-addressable when the file exists.
# 函数用途: 生成 sha256；缺失、目录或不可读时返回空字符串，让 report 仍可序列化。
def _artifact_hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


# LLM: _artifact_size records size as ref metadata without reading bodies into prompt.
# 函数用途: 返回文件或目录的 stat size；缺失时为 0。
def _artifact_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


# LLM: _artifact_id is stable across runs for the same resolved path and content hash.
# 函数用途: 生成短 artifact_id，便于 ledger/UI 展示和去重。
def _artifact_id(path: Path, digest: str) -> str:
    seed = f"{path.resolve(strict=False)}:{digest}"
    return f"artifact:{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:16]}"


__all__ = [
    "ArtifactAcceptanceReport",
    "ArtifactAcceptanceRequest",
    "ArtifactFinding",
    "advisory_artifact_finding",
    "advisory_artifact_findings",
    "artifact_ref_payload",
    "kind_for_path",
]
