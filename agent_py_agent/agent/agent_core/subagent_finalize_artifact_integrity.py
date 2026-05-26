# LLM: Artifact integrity override blocks broken product files before closeout.
# 模块用途: 在 runner 结果落盘前检查结构化 artifact refs，避免半截 HTML 被误标成待收口。

from __future__ import annotations

import json
from pathlib import Path

from ..subagent import SubAgentParsedOutput
from ..subagents.workspace_roots import derived_workspace_roots_from_subagent_path
from ..tooling.artifact_integrity import ArtifactIntegrityCheckRequest, check_artifact_integrity


# LLM: progress_artifacts_override recovers product refs when runner output omits artifacts.
# 函数用途: 子代理写过真实产物但 output.json 漏填 artifacts 时，从 task-local progress 补标准产物引用。
def progress_artifacts_override(request: object, structured: object) -> object:
    if getattr(structured, "artifacts", []) or not looks_like_success_closeout(structured):
        return structured
    artifacts = _progress_artifact_refs(request)
    if artifacts:
        structured.artifacts = artifacts
    return structured


# LLM: artifact_integrity_override makes obvious broken product files block before closeout.
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
        codes = _closeout_blocker_codes(decision)
        if codes:
            blockers.append(f"{artifact_path}:{codes}")
    return blockers


# LLM: _progress_artifact_refs reads only the small latest_tool_progress.json control packet.
# 函数用途: 从 task-local progress 找到最近真实业务产物路径；不读取产物正文，不扫描目录。
def _progress_artifact_refs(request: object) -> list[dict[str, str]]:
    payload = _latest_tool_progress_payload(request)
    path = str(payload.get("latest_written_path") or "").strip()
    if not path or path == str(payload.get("closeout_written_path") or "").strip():
        return []
    if not isinstance(payload.get("artifact_integrity"), dict):
        return []
    return [{"path": path, "kind": "file", "summary": "artifact ref recovered from latest_tool_progress"}]


# LLM: _latest_tool_progress_payload locates the runner progress snapshot through the persisted task.
# 函数用途: 精确读取当前 run 的 agents/<run_id>/progress/latest_tool_progress.json，失败时保守返回空。
def _latest_tool_progress_payload(request: object) -> dict[str, object]:
    try:
        task = request.agent.subagents.load(request.params.run_id)
    except (AttributeError, KeyError, FileNotFoundError, json.JSONDecodeError, TypeError):
        return {}
    workspace = str(getattr(task, "agent_run_workspace_dir", "") or "").strip()
    if not workspace:
        return {}
    try:
        payload = json.loads((Path(workspace) / "progress" / "latest_tool_progress.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


# LLM: _closeout_blocker_codes promotes actionable HTML warnings before a runner can self-close.
# 函数用途: 子代理自称完成时，href="#" 和缺失锚点也必须修复；这些会让真实页面按钮失效。
def _closeout_blocker_codes(decision: object) -> str:
    codes = list(getattr(decision, "blocker_codes", []) or [])
    codes.extend(
        code
        for code in getattr(decision, "warning_codes", []) or []
        if code in {"placeholder_hash_link", "missing_hash_target"}
    )
    return ",".join(codes[:4])


# LLM: _blocked_artifact_output preserves useful runner metadata while changing lifecycle state.
# 函数用途: 把产物结构检查失败写入标准 SubAgentParsedOutput，供父级恢复和收口链路读取。
def _blocked_artifact_output(structured: object, blocker_text: str) -> SubAgentParsedOutput:
    existing_tests = list(getattr(structured, "tests", []) or [])
    return SubAgentParsedOutput(
        found=True,
        ok=True,
        status="BLOCKED",
        summary=(
            "artifact integrity check failed; repair the listed product files before closeout. "
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
    aligned = _candidate_aligned_to_root(root, candidate)
    if aligned is not None:
        return aligned
    joined = root / candidate
    return joined if joined.exists() else None


# LLM: _candidate_aligned_to_root handles refs that already include the product root suffix.
# 函数用途: 把 deliverables/site/index.html 这类相对产物 ref 对齐到 .../deliverables/site 根，避免重复拼目录。
def _candidate_aligned_to_root(root: Path, candidate: Path) -> Path | None:
    candidate_parts = candidate.parts
    if len(candidate_parts) < 2:
        return None
    root_parts = root.parts
    max_prefix = min(len(root_parts), len(candidate_parts) - 1)
    for prefix_size in range(max_prefix, 0, -1):
        if root_parts[-prefix_size:] == candidate_parts[:prefix_size]:
            return root.joinpath(*candidate_parts[prefix_size:])
    return None


# LLM: _artifact_resolution_roots lets product outputs beat internal task directories for relative refs.
# 函数用途: 从 write_boundary 中提取可检查的真实产物根。
def _artifact_resolution_roots(context: object) -> list[Path]:
    boundary = getattr(context, "write_boundary", {}) or {}
    if not isinstance(boundary, dict):
        return []
    roots: list[Path] = []
    workspace_roots = _context_workspace_roots(context)
    for raw_root in [*(boundary.get("product_write_roots") or []), *(boundary.get("allowed_write_roots") or [])]:
        _append_artifact_root_candidates(roots, raw_root, workspace_roots)
    return roots


# LLM: _append_artifact_root_candidates keeps resolution root collection flat for the strict size guard.
# 函数用途: 把一个 write root 展开并去重追加到 roots，避免主解析函数继续加深嵌套。
def _append_artifact_root_candidates(roots: list[Path], raw_root: object, workspace_roots: list[Path]) -> None:
    for path in _artifact_root_candidates(raw_root, workspace_roots):
        _append_unique_root(roots, path)


# LLM: _artifact_root_candidates mirrors the filesystem gateway's relative-root semantics for finalize checks.
# 函数用途: 绝对产物根原样使用；相对产物根按项目工作区根解析，避免误落到子代理私有 task_dir。
def _artifact_root_candidates(raw_root: object, workspace_roots: list[Path]) -> list[Path]:
    text = str(raw_root or "").strip()
    if not text:
        return []
    path = Path(text).expanduser()
    if path.is_absolute():
        return [path.resolve(strict=False)]
    return [(root / path).resolve(strict=False) for root in workspace_roots]


# LLM: _context_workspace_roots derives bounded project roots from a run-local subagent task dir.
# 函数用途: finalize 没有直接 workspace_root 字段时，从 `.my_agent/subagents/<run>` 推导真实项目根。
def _context_workspace_roots(context: object) -> list[Path]:
    roots: list[Path] = []
    for raw in getattr(context, "workspace_roots", []) or []:
        _append_unique_root(roots, Path(str(raw)).expanduser().resolve(strict=False))
    task_dir = str(getattr(context, "task_dir", "") or "").strip()
    if task_dir:
        for root in derived_workspace_roots_from_subagent_path(Path(task_dir).expanduser().resolve(strict=False)):
            _append_unique_root(roots, root.resolve(strict=False))
    return roots


# LLM: _append_unique_root keeps derived roots stable and duplicate-free.
# 函数用途: 追加路径时保持顺序，避免同一个工作区重复参与产物解析。
def _append_unique_root(roots: list[Path], root: Path) -> None:
    if str(root) and root not in roots:
        roots.append(root)


# LLM: looks_like_success_closeout recognizes model self-reports that would otherwise move a parent forward.
# 函数用途: 只拦截“我完成了/待收口”类汇报，不影响真实 capability 或业务阻塞。
def looks_like_success_closeout(structured: object) -> bool:
    if not bool(getattr(structured, "found", False)) or not bool(getattr(structured, "ok", False)):
        return False
    if getattr(structured, "capability_requests", []) or getattr(structured, "blocked_reason", ""):
        return False
    status = str(getattr(structured, "status", "") or "").strip().upper()
    return status in {"", "DONE", "COMPLETED", "SUCCESS", "OK"}
