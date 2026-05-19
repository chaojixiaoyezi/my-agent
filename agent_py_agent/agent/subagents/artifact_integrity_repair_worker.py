# LLM: Deterministic artifact repair handles small structural fixes without another long model run.
# 模块用途: 为 artifact_integrity 修复任务提供有边界的本地 worker，避免简单 HTML 结构问题反复消耗 runner。

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..tooling.artifact_integrity import ArtifactIntegrityCheckRequest, check_artifact_integrity
from .manager_runner_result_payload import RecordRunnerResultParams
from .models import SubAgentParsedOutput, SubAgentRunnerResult

_MAX_REPAIR_BYTES = 2 * 1024 * 1024
_HTML_REF_RE = re.compile(r"(?P<path>(?:[A-Za-z]:)?[/\\][^\"'\n\r]+?\.html?)", re.IGNORECASE)
_DOCTYPE_RE = re.compile(r"<!doctype[^>]*>", re.IGNORECASE)
_HTML_OPEN_RE = re.compile(r"<html\b(?P<attrs>[^>]*)>", re.IGNORECASE)
_HEAD_BLOCK_RE = re.compile(r"<head\b[^>]*>(?P<inner>.*?)</head>", re.IGNORECASE | re.DOTALL)
_HEAD_TAG_RE = re.compile(r"</?head\b[^>]*>", re.IGNORECASE)
_BODY_OPEN_RE = re.compile(r"<body\b(?P<attrs>[^>]*)>", re.IGNORECASE)
_BODY_CLOSE_RE = re.compile(r"</body>", re.IGNORECASE)
_HTML_CLOSE_RE = re.compile(r"</html>", re.IGNORECASE)
_ID_RE = re.compile(r"\bid\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
_HASH_HREF_RE = re.compile(r"\bhref\s*=\s*(['\"])(?P<href>#[^'\"]*)\1", re.IGNORECASE)


# LLM: maybe_run_artifact_integrity_repair_worker is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def maybe_run_artifact_integrity_repair_worker(
    manager: Any,
    run_id: str,
    *,
    attempt_id: str = "",
) -> SubAgentRunnerResult | None:
    task = manager.load(run_id)
    if not _is_artifact_integrity_repair(task):
        return None
    paths = _target_artifact_paths(task)
    if not paths:
        return _record_blocked(
            manager,
            run_id,
            attempt_id,
            "artifact_integrity_repair:no_target_artifacts",
            tests=[],
            artifacts=[],
        )
    artifacts: list[dict[str, object]] = []
    tests: list[dict[str, object]] = []
    blockers: list[str] = []
    repaired: list[str] = []
    for path in paths:
        outcome = _repair_one_html(path)
        artifacts.append({"path": str(path), "kind": "html", "role": "repaired_artifact"})
        tests.append(outcome["test"])
        if outcome["changed"]:
            repaired.append(str(path))
        if outcome["blocker"]:
            blockers.append(str(outcome["blocker"]))
    if blockers:
        return _record_blocked(
            manager,
            run_id,
            attempt_id,
            ";".join(blockers),
            tests=tests,
            artifacts=artifacts,
        )
    return manager.record_runner_result(
        RecordRunnerResultParams(
            run_id=run_id,
            attempt_id=attempt_id,
            dry_run=False,
            ok=True,
            message=f"artifact_integrity deterministic repair completed: repaired={len(repaired)} checked={len(paths)}",
            backend="artifact_integrity_repair",
            tool_rounds=0,
            status="AWAITING_ACCEPTANCE",
            verification_status="NEEDS_ACCEPTANCE",
            structured_output=SubAgentParsedOutput(
                found=True,
                ok=True,
                status="AWAITING_ACCEPTANCE",
                summary="Artifact integrity repair completed with bounded deterministic edits.",
                artifacts=artifacts,
                tests=tests,
                evidence=[
                    {
                        "kind": "artifact_integrity_repair",
                        "summary": f"artifact_integrity passed after deterministic repair: {path}",
                        "path": path,
                        "ok": True,
                    }
                    for path in repaired
                ],
                next_actions=["parent_acceptance"],
            ),
        )
    )


# LLM: _is_artifact_integrity_repair is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _is_artifact_integrity_repair(task: Any) -> bool:
    attrs = getattr(task, "attributes", {}) or {}
    return attrs.get("repair_kind") == "artifact_integrity"


# LLM: _target_artifact_paths is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _target_artifact_paths(task: Any) -> list[Path]:
    roots = _allowed_roots(task)
    paths: list[Path] = []
    for ref in _target_refs(task):
        for path in _candidate_paths(ref, roots):
            if _path_allowed(path, roots) and path.exists() and path.is_file() and path.suffix.lower() in {".html", ".htm"}:
                if path not in paths:
                    paths.append(path)
    return paths


# LLM: _allowed_roots is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _allowed_roots(task: Any) -> list[Path]:
    roots: list[Path] = []
    attrs = getattr(task, "attributes", {}) or {}
    values: list[object] = [
        getattr(task, "task_dir", ""),
        getattr(task, "output_dir", ""),
        getattr(task, "task_workspace_artifacts_dir", ""),
        getattr(task, "agent_run_artifacts_dir", ""),
        *(getattr(task, "allowed_write_roots", []) or []),
    ]
    for signal in attrs.get("failure_refs", []) or []:
        if isinstance(signal, dict):
            values.extend(signal.get("allowed_write_roots", []) or [])
    for value in values:
        try:
            path = Path(str(value or "")).expanduser().resolve(strict=False)
        except OSError:
            continue
        if path and path not in roots:
            roots.append(path if path.suffix == "" else path.parent)
    return roots


# LLM: _target_refs is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _target_refs(task: Any) -> list[str]:
    attrs = getattr(task, "attributes", {}) or {}
    refs: list[str] = [str(item or "") for item in attrs.get("target_artifact_refs", []) or []]
    for signal in attrs.get("failure_refs", []) or []:
        if isinstance(signal, dict):
            refs.extend(str(item or "") for item in signal.get("artifact_refs", []) or [])
    return [ref for ref in refs if ref.strip()]


# LLM: _candidate_paths is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _candidate_paths(ref: str, roots: list[Path]) -> list[Path]:
    cleaned = _clean_ref(ref)
    candidates: list[Path] = []
    if cleaned:
        try:
            path = Path(cleaned).expanduser()
        except OSError:
            path = Path()
        if str(path):
            candidates.append(path.resolve(strict=False) if path.is_absolute() else path)
            if not path.is_absolute():
                candidates.extend((root / path).resolve(strict=False) for root in roots)
    match = _HTML_REF_RE.search(ref)
    if match:
        try:
            candidates.append(Path(match.group("path")).expanduser().resolve(strict=False))
        except OSError:
            pass
    unique: list[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve(strict=False)
        if resolved not in unique:
            unique.append(resolved)
    return unique


# LLM: _clean_ref is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _clean_ref(ref: str) -> str:
    text = str(ref or "").strip().strip("\"'")
    if "artifact_integrity_failed:" in text:
        text = text.split("artifact_integrity_failed:", 1)[-1]
    match = _HTML_REF_RE.search(text)
    if match:
        return match.group("path").rstrip(" ,")
    return text


# LLM: _path_allowed is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _path_allowed(path: Path, roots: list[Path]) -> bool:
    if not roots:
        return False
    resolved = path.resolve(strict=False)
    for root in roots:
        root_resolved = root.resolve(strict=False)
        try:
            resolved.relative_to(root_resolved)
        except ValueError:
            continue
        return True
    return False


# LLM: _repair_one_html is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _repair_one_html(path: Path) -> dict[str, object]:
    if path.stat().st_size > _MAX_REPAIR_BYTES:
        return _repair_outcome(path, changed=False, blocker=f"artifact_integrity_repair:{path}:artifact_too_large")
    try:
        original = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return _repair_outcome(path, changed=False, blocker=f"artifact_integrity_repair:{path}:not_utf8_text")
    repaired = _repair_html_text(original)
    changed = repaired != original
    if changed:
        path.write_text(repaired, encoding="utf-8")
    decision = check_artifact_integrity(ArtifactIntegrityCheckRequest(path=path, require_complete=True))
    codes = _blocking_codes(decision)
    blocker = f"artifact_integrity_failed:{path}:{','.join(codes)}" if codes else ""
    return _repair_outcome(path, changed=changed, blocker=blocker, decision=decision)


# LLM: _repair_html_text is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _repair_html_text(text: str) -> str:
    repaired = _truncate_after_html_close(text)
    repaired = _balance_style_tags(repaired)
    repaired = _balance_script_tags(repaired)
    repaired = _collapse_duplicate_html_skeleton(repaired)
    repaired = _ensure_body_anchor(repaired)
    repaired = _repair_hash_hrefs(repaired)
    repaired = _append_missing_closers(repaired)
    return repaired


# LLM: _truncate_after_html_close is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _truncate_after_html_close(text: str) -> str:
    lowered = text.lower()
    idx = lowered.rfind("</html>")
    return text[: idx + len("</html>")] if idx >= 0 and text[idx + len("</html>") :].strip() else text


# LLM: _balance_style_tags is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _balance_style_tags(text: str) -> str:
    return _balance_paired_html_tag(
        text,
        tag="style",
        missing_markers=("</head>", "<body", "</body>", "</html>"),
        extra_close_markers=("</header>", "</script>", "</style>", "</head>", "<body"),
    )


# LLM: _balance_script_tags is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _balance_script_tags(text: str) -> str:
    return _balance_paired_html_tag(
        text,
        tag="script",
        missing_markers=("</body>", "</html>"),
        extra_close_markers=("</style>", "</main>", "</section>", "</body>", "</html>"),
    )


# LLM: _balance_paired_html_tag is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _balance_paired_html_tag(
    text: str,
    *,
    tag: str,
    missing_markers: tuple[str, ...],
    extra_close_markers: tuple[str, ...],
) -> str:
    lowered = text.lower()
    opens = [match.start() for match in re.finditer(rf"<{tag}\b", lowered)]
    closes = [match.start() for match in re.finditer(rf"</{tag}>", lowered)]
    if len(closes) == len(opens):
        return text
    if len(opens) > len(closes):
        return _close_missing_html_tags(text, tag=tag, missing=len(opens) - len(closes), markers=missing_markers)
    repaired = text
    for _ in range(len(closes) - len(opens)):
        close_idx = repaired.lower().rfind(f"</{tag}>")
        insert_at = _html_fragment_start(repaired, close_idx, markers=extra_close_markers)
        repaired = f"{repaired[:insert_at]}<{tag}>\n{repaired[insert_at:]}"
    return repaired


# LLM: _close_missing_html_tags is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _close_missing_html_tags(text: str, *, tag: str, missing: int, markers: tuple[str, ...]) -> str:
    repaired = text
    for _ in range(missing):
        insert_at = _missing_html_close_insert_at(repaired, tag=tag, markers=markers)
        repaired = f"{repaired[:insert_at]}</{tag}>{repaired[insert_at:]}"
    return repaired


# LLM: _missing_html_close_insert_at is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _missing_html_close_insert_at(text: str, *, tag: str, markers: tuple[str, ...]) -> int:
    lowered = text.lower()
    open_idx = lowered.rfind(f"<{tag}")
    if open_idx < 0:
        return len(text)
    positions = [idx for marker in markers if (idx := lowered.find(marker, open_idx)) >= 0]
    return min(positions) if positions else len(text)


# LLM: _html_fragment_start is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _html_fragment_start(text: str, close_idx: int, *, markers: tuple[str, ...]) -> int:
    lowered = text.lower()
    starts: list[int] = []
    for marker in markers:
        idx = lowered.rfind(marker, 0, close_idx)
        if idx >= 0:
            end = lowered.find(">", idx)
            starts.append(end + 1 if end >= 0 else idx + len(marker))
    return max(starts) if starts else close_idx


# LLM: _ensure_body_anchor is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _ensure_body_anchor(text: str) -> str:
    if 'id="top"' in text or "id='top'" in text:
        return text
    match = _BODY_OPEN_RE.search(text)
    if not match:
        return text
    attrs = match.group("attrs") or ""
    if _ID_RE.search(attrs):
        return text
    return text[: match.start()] + f"<body{attrs} id=\"top\">" + text[match.end() :]


# LLM: _collapse_duplicate_html_skeleton is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _collapse_duplicate_html_skeleton(text: str) -> str:
    lowered = text.lower()
    duplicate_counts = (
        len(re.findall(r"<html\b", lowered)) > 1,
        len(re.findall(r"<head\b", lowered)) > 1,
        len(re.findall(r"<body\b", lowered)) > 1,
        lowered.count("</head>") > 1,
        lowered.count("</body>") > 1,
        lowered.count("</html>") > 1,
    )
    if not any(duplicate_counts):
        return text
    body_parts = _body_content_parts(text)
    if not body_parts:
        return text
    doctype = _first_match_text(_DOCTYPE_RE, text) or "<!doctype html>"
    html_attrs = _first_group_text(_HTML_OPEN_RE, text, "attrs")
    body_attrs = _first_group_text(_BODY_OPEN_RE, text, "attrs")
    head_parts = [part for part in (match.group("inner").strip() for match in _HEAD_BLOCK_RE.finditer(text)) if part]
    head_text = "\n".join(head_parts)
    body_text = "\n".join(body_parts)
    return (
        f"{doctype}\n"
        f"<html{html_attrs}>\n"
        "<head>\n"
        f"{head_text}\n"
        "</head>\n"
        f"<body{body_attrs}>\n"
        f"{body_text}\n"
        "</body>\n"
        "</html>\n"
    )


# LLM: _body_content_parts is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _body_content_parts(text: str) -> list[str]:
    matches = list(_BODY_OPEN_RE.finditer(text))
    parts: list[str] = []
    for index, match in enumerate(matches):
        start = match.end()
        next_body_start = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        close_match = _BODY_CLOSE_RE.search(text, start)
        html_close_match = _HTML_CLOSE_RE.search(text, start)
        end_candidates = [next_body_start]
        if close_match:
            end_candidates.append(close_match.start())
        if html_close_match:
            end_candidates.append(html_close_match.start())
        segment = text[start : min(end_candidates)]
        cleaned = _strip_html_skeleton_tags(segment).strip()
        if cleaned:
            parts.append(cleaned)
    return parts


# LLM: _strip_html_skeleton_tags is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _strip_html_skeleton_tags(text: str) -> str:
    cleaned = _DOCTYPE_RE.sub("", text)
    cleaned = _HEAD_BLOCK_RE.sub("", cleaned)
    cleaned = _HTML_OPEN_RE.sub("", cleaned)
    cleaned = _HTML_CLOSE_RE.sub("", cleaned)
    cleaned = _HEAD_TAG_RE.sub("", cleaned)
    cleaned = _BODY_OPEN_RE.sub("", cleaned)
    cleaned = _BODY_CLOSE_RE.sub("", cleaned)
    return cleaned


# LLM: _first_match_text is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _first_match_text(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text)
    return match.group(0) if match else ""


# LLM: _first_group_text is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _first_group_text(pattern: re.Pattern[str], text: str, group: str) -> str:
    match = pattern.search(text)
    return match.group(group) if match else ""


# LLM: _repair_hash_hrefs is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _repair_hash_hrefs(text: str) -> str:
    ids = set(_ID_RE.findall(text))

    # LLM: replace is part of this module's structured runtime path; keep callers and tests aligned before changing it.
    # 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
    def replace(match: re.Match[str]) -> str:
        href = match.group("href").strip()
        quote = match.group(1)
        if href == "#":
            target = "#top" if "top" in ids else ""
            return f"href={quote}{target}{quote}" if target else match.group(0)
        target_id = href[1:]
        if target_id and target_id not in ids:
            return f"href={quote}#top{quote}" if "top" in ids else match.group(0)
        return match.group(0)

    return _HASH_HREF_RE.sub(replace, text)


# LLM: _append_missing_closers is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _append_missing_closers(text: str) -> str:
    lowered = text.lower()
    suffix = ""
    if "</body>" not in lowered:
        suffix += "\n</body>"
    if "</html>" not in lowered:
        suffix += "\n</html>"
    return f"{text.rstrip()}{suffix}\n" if suffix else text


# LLM: _blocking_codes is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _blocking_codes(decision: Any) -> list[str]:
    codes = list(getattr(decision, "blocker_codes", []) or [])
    codes.extend(
        code
        for code in getattr(decision, "warning_codes", []) or []
        if code in {"placeholder_hash_link", "missing_hash_target"}
    )
    return codes


# LLM: _repair_outcome is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _repair_outcome(
    path: Path,
    *,
    changed: bool,
    blocker: str,
    decision: Any | None = None,
) -> dict[str, object]:
    decision = decision or check_artifact_integrity(ArtifactIntegrityCheckRequest(path=path, require_complete=True))
    codes = _blocking_codes(decision)
    test = {
        "name": "artifact_integrity",
        "validation_method": "artifact_integrity",
        "path": str(path),
        "passed": not codes,
        "blocker_codes": list(getattr(decision, "blocker_codes", []) or []),
        "warning_codes": list(getattr(decision, "warning_codes", []) or []),
    }
    return {"changed": changed, "blocker": blocker or (";".join(codes) if codes else ""), "test": test}


# LLM: _record_blocked is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _record_blocked(
    manager: Any,
    run_id: str,
    attempt_id: str,
    blocker: str,
    *,
    tests: list[dict[str, object]],
    artifacts: list[dict[str, object]],
) -> SubAgentRunnerResult:
    return manager.record_runner_result(
        RecordRunnerResultParams(
            run_id=run_id,
            attempt_id=attempt_id,
            dry_run=False,
            ok=False,
            message=blocker,
            backend="artifact_integrity_repair",
            tool_rounds=0,
            status="BLOCKED",
            verification_status="UNVERIFIED",
            failure_type="artifact_integrity_failed",
            structured_output=SubAgentParsedOutput(
                found=True,
                ok=True,
                status="BLOCKED",
                summary="Artifact integrity deterministic repair could not clear all blockers.",
                blocked_reason=blocker,
                failure_type="artifact_integrity_failed",
                artifacts=artifacts,
                tests=tests,
                next_actions=["repair_artifacts", "rerun_artifact_integrity_check"],
            ),
        )
    )
