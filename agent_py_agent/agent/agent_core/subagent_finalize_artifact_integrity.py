# LLM: Artifact integrity override blocks broken product files before parent acceptance.
# 模块用途: 在 runner 结果落盘前检查结构化 artifact refs，避免半截 HTML 被误标成待验收。

from __future__ import annotations

from pathlib import Path

from ..subagent import SubAgentParsedOutput
from ..tooling.artifact_integrity import ArtifactIntegrityCheckRequest, check_artifact_integrity


# LLM: artifact_integrity_override makes obvious broken product files block before parent acceptance.
# 函数用途: runner 自称完成但 HTML 产物半截或闭合后被追加时，改成 BLOCKED 并给父级明确修复动作。
def artifact_integrity_override(request: object, structured: object) -> object:
    if not looks_like_success_closeout(structured):
        return structured
    blockers = _artifact_integrity_blockers(request, structured)
    if not blockers:
        return structured
    return _blocked_artifact_output(structured, "; ".join(blockers[:4]))


# LLM: _artifact_integrity_blockers checks each local artifact ref with the shared integrity tool.
# 函数用途: 只返回短 blocker 摘要，不读取 artifact 正文进 prompt。
def _artifact_integrity_blockers(request: object, structured: object) -> list[str]:
    blockers: list[str] = []
    for artifact_path in _structured_artifact_paths(request, structured):
        decision = check_artifact_integrity(ArtifactIntegrityCheckRequest(path=artifact_path, require_complete=True))
        if not decision.ok:
            codes = ",".join(decision.blocker_codes[:4]) or "unknown"
            blockers.append(f"{artifact_path}:{codes}")
    return blockers


# LLM: _blocked_artifact_output preserves useful runner metadata while changing lifecycle state.
# 函数用途: 把产物结构检查失败写入标准 SubAgentParsedOutput，供父级恢复和验收链路读取。
def _blocked_artifact_output(structured: object, blocker_text: str) -> SubAgentParsedOutput:
    existing_tests = list(getattr(structured, "tests", []) or [])
    return SubAgentParsedOutput(
        found=True,
        ok=True,
        status="BLOCKED",
        summary=(
            "artifact integrity check failed; repair the listed product files before parent acceptance. "
            f"issues={blocker_text}"
        ),
        blocked_reason=f"artifact_integrity_failed:{blocker_text}",
        failure_type="artifact_integrity_failed",
        used_skills=list(getattr(structured, "used_skills", []) or []),
        used_tools=list(getattr(structured, "used_tools", []) or []),
        evidence=list(getattr(structured, "evidence", []) or []),
        evidence_packets=list(getattr(structured, "evidence_packets", []) or []),
        findings=list(getattr(structured, "findings", []) or []),
        capability_requests=list(getattr(structured, "capability_requests", []) or []),
        artifacts=list(getattr(structured, "artifacts", []) or []),
        tests=[
            *existing_tests,
            {
                "name": "artifact integrity",
                "validation_method": "artifact_integrity",
                "ok": False,
                "summary": blocker_text,
            },
        ],
        patches=list(getattr(structured, "patches", []) or []),
        lessons=list(getattr(structured, "lessons", []) or []),
        next_actions=["repair_artifacts", "rerun_artifact_integrity_check"],
    )


# LLM: _structured_artifact_paths resolves local artifact refs without reading artifact bodies into prompts.
# 函数用途: 从 structured artifacts 中提取本地文件路径；相对路径按 task_dir 或 product root 解析。
def _structured_artifact_paths(request: object, structured: object) -> list[Path]:
    paths: list[Path] = []
    for artifact in getattr(structured, "artifacts", []) or []:
        raw_path = _artifact_path_text(artifact)
        if raw_path:
            paths.append(_resolve_artifact_path(request.params.context, raw_path))
    return paths


# LLM: _artifact_path_text supports the artifact dict shapes already used by subagent outputs.
# 函数用途: 兼容 path/file_path/artifact_path/ref 等常见字段，避免模型字段小差异导致漏检。
def _artifact_path_text(artifact: object) -> str:
    if not isinstance(artifact, dict):
        return str(artifact).strip()
    for key in ("path", "file_path", "artifact_path", "ref"):
        value = artifact.get(key)
        if value:
            return str(value).strip()
    return ""


# LLM: _resolve_artifact_path keeps relative artifact refs tied to task or product output roots.
# 函数用途: 解析产物路径；绝对路径原样检查，相对路径优先落到真实产物根。
def _resolve_artifact_path(context: object, raw_path: str) -> Path:
    candidate = Path(raw_path)
    if candidate.is_absolute():
        return candidate
    for root in _artifact_resolution_roots(context):
        resolved = _artifact_path_under_root(root, candidate)
        if resolved is not None:
            return resolved
    task_dir = str(getattr(context, "task_dir", "") or "").strip()
    return Path(task_dir) / candidate if task_dir else candidate


# LLM: _artifact_path_under_root resolves candidate refs under one product or allowed root.
# 函数用途: 支持 write_boundary 里既给目录也给具体文件的情况。
def _artifact_path_under_root(root: Path, candidate: Path) -> Path | None:
    if root.is_file() or root.suffix:
        return root if root.name == candidate.name else None
    joined = root / candidate
    return joined if joined.exists() else None


# LLM: _artifact_resolution_roots lets product outputs beat internal task directories for relative refs.
# 函数用途: 从 write_boundary 中提取可检查的真实产物根。
def _artifact_resolution_roots(context: object) -> list[Path]:
    boundary = getattr(context, "write_boundary", {}) or {}
    if not isinstance(boundary, dict):
        return []
    roots: list[Path] = []
    for raw_root in [*(boundary.get("product_write_roots") or []), *(boundary.get("allowed_write_roots") or [])]:
        path = Path(str(raw_root or "").strip())
        if str(path) and path.is_absolute() and path not in roots:
            roots.append(path)
    return roots


# LLM: looks_like_success_closeout recognizes model self-reports that would otherwise move a parent forward.
# 函数用途: 只拦截“我完成了/待验收”类汇报，不影响真实 capability 或业务阻塞。
def looks_like_success_closeout(structured: object) -> bool:
    if not bool(getattr(structured, "found", False)) or not bool(getattr(structured, "ok", False)):
        return False
    if getattr(structured, "capability_requests", []) or getattr(structured, "blocked_reason", ""):
        return False
    status = str(getattr(structured, "status", "") or "").strip().upper()
    return status in {"", "AWAITING_ACCEPTANCE", "DONE", "COMPLETED", "SUCCESS", "OK"}
