from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...contracts.gates.models import GateDecision, GateFinding
from ...contracts.recovery import RecoveryAction
from ...task_progress import (
    progress_path,
    read_task_progress,
    task_progress_status_is_closed,
    task_progress_status_is_done,
    task_progress_summary,
)


@dataclass(frozen=True)
class ArtifactEvidenceProjectionRequest:
    report: dict[str, Any]
    progress: dict[str, Any]
    run_id: str
    path: Path
    summary: dict[str, Any]


def task_progress_has_open_items(closeout: object) -> bool:
    """模型自己列的 task_progress 清单里是否还有开放(未 done/skipped)项。

    供交付前的"别提前放行"判据复用(completion 自动收口 / 出口续航同源):只认模型
    自己声明的待办,清单空 / 没建过账本一律 False(不误伤没用清单的普通任务)。
    """
    root = _progress_root(closeout)
    run_id = _run_id(closeout)
    if not root or not run_id:
        return False
    path = progress_path(root, run_id)
    if not path.exists():
        return False
    return bool(_open_items(read_task_progress(root, run_id)))


# LLM: 假标 done 形态判据(A3-u3 真机实锤:模型卡住后把 7 个待办全标 done 但 0 产出,
#   账本 open=0 绕过 todo 守门,只剩空交付闸兜成 ok=False 静默失败、无人对质无返工)。
#   全部客观事实:账本存在、全部 closed、done 项 ≥ min_done_items(3,防 1-2 项小账本
#   误伤)、且所有 done 项的 evidence 都解析不出任何实存文件。命中返回证据 payload,
#   由 uncontracted 早退分支决定"一次性返工"(R9-safe:对的是模型自己声明的 done,
#   幂等一次、给双出口,不是外部配额)。
# 函数用途: 回答"账本是不是全标了 done 却拿不出一个实存证据文件"。
def task_progress_all_done_without_artifact_evidence(
    closeout: object,
    *,
    workspace_root: Path | None = None,
    min_done_items: int = 3,
) -> dict[str, Any] | None:
    root = _progress_root(closeout)
    run_id = _run_id(closeout)
    if not root or not run_id:
        return None
    path = progress_path(root, run_id)
    if not path.exists():
        return None
    progress = read_task_progress(root, run_id)
    if _open_items(progress):
        return None
    items = [item for item in progress.get("items", []) if isinstance(item, dict)] if isinstance(progress.get("items"), list) else []
    done_items = [item for item in items if task_progress_status_is_done(item.get("status"))]
    if len(done_items) < min_done_items:
        return None
    if _any_evidence_file_exists(done_items, workspace_root):
        return None
    return {
        "run_id": run_id,
        "progress_ref": str(path),
        "done_count": len(done_items),
        "done_items": [_compact_item(item) for item in done_items[:12]],
    }


# 函数用途: done 项的 evidence 引用里是否有任何一个解析为实存文件(有=不是假 done)。
def _any_evidence_file_exists(done_items: list[dict[str, Any]], workspace_root: Path | None) -> bool:
    return any(
        _evidence_token_file_exists(token, workspace_root)
        for item in done_items
        for token in _evidence_candidate_paths(item.get("evidence"))
    )


# 函数用途: 从 evidence 字符串提取候选路径。与 _evidence_tokens 不同,这里保留前导
#   "/"(绝对路径要原样查存在性,不做投影 strip),并额外把整串与"path:line"的 path
#   头当候选。
def _evidence_candidate_paths(value: object) -> list[str]:
    candidates: list[str] = []
    for item in _list(value):
        text = str(item or "").strip()
        if not text:
            continue
        candidates.append(text)
        head = text.split(":", 1)[0].strip()
        if head and head != text:
            candidates.append(head)
        for match in _EVIDENCE_TOKEN_RE.finditer(text):
            candidates.append(match.group("token"))
    return list(dict.fromkeys([item for item in candidates if item]))


def _evidence_token_file_exists(token: str, workspace_root: Path | None) -> bool:
    try:
        candidate = Path(token).expanduser()
        if candidate.is_absolute():
            return candidate.is_file()
        if workspace_root is None:
            return False
        return (workspace_root / candidate).is_file()
    except OSError:
        return False


def evaluate_task_progress_closeout_gate(closeout: object, report: dict[str, Any] | None = None) -> GateDecision:
    root = _progress_root(closeout)
    run_id = _run_id(closeout)
    if not root or not run_id:
        return _unchecked_progress_decision("progress_scope_missing")
    path = progress_path(root, run_id)
    if not path.exists():
        return _unchecked_progress_decision("progress_file_missing", run_id=run_id)
    progress = read_task_progress(root, run_id)
    summary = task_progress_summary({**progress, "ref": str(path)})
    open_items = _open_items(progress)
    if open_items:
        return _open_progress_decision(open_items, run_id, path, summary)
    evidence_decision = _artifact_evidence_projection_decision(
        ArtifactEvidenceProjectionRequest(report or {}, progress, run_id, path, summary)
    )
    if evidence_decision is not None:
        return evidence_decision
    return _closed_progress_decision(run_id, path, summary, progress)


def task_progress_repair_message(report: dict[str, Any]) -> str:
    payload = report.get("task_progress_closeout_gate")
    if not isinstance(payload, dict) or payload.get("allowed") is True:
        return ""
    message = str(payload.get("model_message") or "").strip()
    if message:
        return message
    return _first_finding_message(payload.get("findings"))


def _first_finding_message(findings: object) -> str:
    if not isinstance(findings, list):
        return ""
    return next(
        (
            text
            for finding in findings
            if isinstance(finding, dict)
            and (text := str(finding.get("message") or "").strip())
        ),
        "",
    )


def _unchecked_progress_decision(reason: str, run_id: str = "") -> GateDecision:
    evidence: dict[str, object] = {"checked": False, "reason": reason}
    if run_id:
        evidence["run_id"] = run_id
    return GateDecision.allow("task_progress_closeout", evidence=evidence)


def _closed_progress_decision(
    run_id: str,
    path: Path,
    summary: dict[str, Any],
    progress: dict[str, Any],
) -> GateDecision:
    advisory_findings = _advisory_findings(progress)
    return GateDecision(
        "task_progress_closeout",
        "ALLOW",
        True,
        tuple(advisory_findings),
        RecoveryAction.CONTINUE.value,
        {
            "checked": True,
            "run_id": run_id,
            "progress_ref": str(path),
            "counts": summary.get("counts", {}),
            "advisory_finding_codes": [finding.code for finding in advisory_findings],
        },
    )


def _open_progress_decision(
    open_items: list[dict[str, Any]],
    run_id: str,
    path: Path,
    summary: dict[str, Any],
) -> GateDecision:
    next_action = str(summary.get("next_action") or "").strip()
    finding = GateFinding(
        "TASK_PROGRESS_OPEN_ITEMS",
        "soft",
        message=_open_items_repair_message(open_items, next_action),
        evidence={
            "run_id": run_id,
            "progress_ref": str(path),
            "open_count": len(open_items),
            "open_items": [_compact_item(item) for item in open_items[:12]],
            "counts": summary.get("counts", {}),
            "summary": summary.get("summary", ""),
            "next_action": next_action,
        },
    )
    return GateDecision.repair(
        "task_progress_closeout",
        (finding,),
        recommended_action=RecoveryAction.CONTINUE.value,
        evidence={
            "checked": True,
            "run_id": run_id,
            "progress_ref": str(path),
            "open_count": len(open_items),
            "counts": summary.get("counts", {}),
            "summary": summary.get("summary", ""),
            "next_action": next_action,
            "required_actions": [
                "continue_open_task_progress_items",
                "read_or_finish_remaining_sources",
                "submit_for_acceptance_after_open_items_are_done_or_skipped",
            ],
        },
    )


def _artifact_evidence_projection_decision(request: ArtifactEvidenceProjectionRequest) -> GateDecision | None:
    item_tokens = _done_item_evidence_tokens(request.progress)
    if len(_unique_projected_tokens(item_tokens)) < 3:
        return None
    artifact_text = _artifact_text(request.report)
    if not artifact_text.strip():
        return None
    missing = _missing_projected_evidence_items(item_tokens, artifact_text)
    if not missing:
        return None
    return _artifact_evidence_projection_repair(request, item_tokens, missing)


def _missing_projected_evidence_items(item_tokens: list[tuple[str, list[str]]], artifact_text: str) -> list[dict[str, Any]]:
    return [
        {"id": item_id, "evidence_tokens": tokens[:8]}
        for item_id, tokens in item_tokens
        if not _any_token_present(artifact_text, tokens)
    ]


def _artifact_evidence_projection_repair(
    request: ArtifactEvidenceProjectionRequest,
    item_tokens: list[tuple[str, list[str]]],
    missing: list[dict[str, Any]],
) -> GateDecision:
    finding = GateFinding(
        "TASK_PROGRESS_EVIDENCE_NOT_IN_ARTIFACT",
        "medium",
        message=(
            f"进度账本里有 {len(missing)} 个 done 项的证据没有出现在最终交付物中；"
            "请把对应文件、模块或来源引用写进最终报告后再提交。"
        ),
        evidence={
            "run_id": request.run_id,
            "progress_ref": str(request.path),
            "missing_count": len(missing),
            "checked_done_items": len(item_tokens),
            "missing_items": missing[:20],
            "artifact_paths": _artifact_paths(request.report)[:12],
            "counts": request.summary.get("counts", {}),
        },
    )
    return GateDecision.repair(
        "task_progress_closeout",
        (finding,),
        recommended_action=RecoveryAction.CONTINUE.value,
        evidence={
            "checked": True,
            "run_id": request.run_id,
            "progress_ref": str(request.path),
            "missing_evidence_projection_count": len(missing),
            "checked_done_items": len(item_tokens),
            "required_actions": [
                "copy_task_progress_evidence_refs_into_final_artifact",
                "rewrite_or_append_final_artifact_with_source_file_refs",
                "submit_for_acceptance_after_final_artifact_mentions_evidence",
            ],
        },
    )


def _progress_root(closeout: object) -> Path | None:
    agent = getattr(closeout, "agent", None)
    home_paths = getattr(agent, "home_paths", None)
    owner_home = getattr(home_paths, "owner_home_dir", None)
    if owner_home:
        return Path(owner_home).expanduser().resolve(strict=False)
    root = getattr(agent, "root", None)
    return Path(root).expanduser().resolve(strict=False) if root else None


def _run_id(closeout: object) -> str:
    params = getattr(closeout, "params", None)
    for value in (
        getattr(params, "run_id", ""),
        getattr(getattr(closeout, "agent", None), "_main_agent_run_id", ""),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _open_items(progress: dict[str, Any]) -> list[dict[str, Any]]:
    items = progress.get("items") if isinstance(progress, dict) else []
    if not isinstance(items, list):
        return []
    return [
        item
        for item in items
        if isinstance(item, dict) and not task_progress_status_is_closed(item.get("status") or "pending")
    ]


def _done_item_evidence_tokens(progress: dict[str, Any]) -> list[tuple[str, list[str]]]:
    rows: list[tuple[str, list[str]]] = []
    for item in progress.get("items", []) if isinstance(progress.get("items"), list) else []:
        if not isinstance(item, dict) or not task_progress_status_is_done(item.get("status")):
            continue
        tokens = _evidence_tokens(item.get("evidence"))
        if tokens:
            rows.append((_item_id(item), tokens))
    return rows


_EVIDENCE_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_.@/-])"
    r"(?P<token>[A-Za-z0-9_.@/-]+"
    r"(?:\.(?:py|pyi|ts|tsx|js|jsx|mjs|cjs|rs|go|md|mdx|toml|json|yaml|yml|txt|java|kt|swift|c|cc|cpp|h|hpp|cs|rb|php|scala|sh|sql|html|css|vue|svelte)|/[A-Za-z0-9_.@/-]+))"
    r"(?![A-Za-z0-9_.@/-])"
)


def _evidence_tokens(value: object) -> list[str]:
    tokens: list[str] = []
    for item in _list(value):
        text = str(item or "")
        for match in _EVIDENCE_TOKEN_RE.finditer(text):
            tokens.extend(_token_variants(match.group("token").strip("/")))
    return _dedupe_tokens(tokens)


def _token_variants(token: str) -> list[str]:
    variants = [token]
    if "/" in token:
        variants.extend(part for part in token.split("/") if "." in part)
    return variants


def _dedupe_tokens(tokens: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for token in tokens:
        clean = token.strip()
        if len(clean) < 4 or clean in seen:
            continue
        seen.add(clean)
        deduped.append(clean)
    return deduped


def _unique_projected_tokens(item_tokens: list[tuple[str, list[str]]]) -> list[str]:
    return _dedupe_tokens([token for _item_id, tokens in item_tokens for token in tokens])


def _artifact_text(report: dict[str, Any]) -> str:
    parts: list[str] = []
    for path in _artifact_paths(report):
        text = _artifact_file_text(Path(path))
        if text:
            parts.append(text)
    return "\n".join(parts)


def _artifact_file_text(path: Path) -> str:
    try:
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8", errors="ignore")[:200_000]
    except OSError:
        return ""


def _artifact_paths(report: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for item in report.get("artifacts", []) if isinstance(report.get("artifacts"), list) else []:
        if not isinstance(item, dict) or item.get("ok") is not True:
            continue
        path = str(item.get("path") or "").strip()
        if path:
            paths.append(path)
    return paths


def _any_token_present(text: str, tokens: list[str]) -> bool:
    return any(token and token in text for token in tokens)


def _advisory_findings(progress: dict[str, Any]) -> list[GateFinding]:
    return [
        *_empty_done_advisory_findings([item for item in progress.get("items", []) if isinstance(item, dict)]),
        *_coverage_incomplete_findings(progress),
    ]


def _empty_done_advisory_findings(items: list[dict[str, Any]]) -> list[GateFinding]:
    empty_done = [
        item
        for item in items
        if task_progress_status_is_done(item.get("status"))
        and not str(item.get("notes") or "").strip()
        and not str(item.get("result") or item.get("outcome") or item.get("conclusion") or "").strip()
        and not _list(item.get("evidence"))
    ]
    if not empty_done:
        return []
    return [
        GateFinding(
            "TASK_PROGRESS_DONE_WITHOUT_EVIDENCE",
            "soft",
            message=(
                f"进度账本里有 {len(empty_done)} 个 done 项没有事实或证据；"
                "建议补上读到的关键值、来源行；这是软提醒，不阻断验收。"
            ),
            evidence={
                "item_ids": [_item_id(item) for item in empty_done[:20]],
                "empty_done_count": len(empty_done),
            },
        )
    ]


def _coverage_incomplete_findings(progress: dict[str, Any]) -> list[GateFinding]:
    coverage = progress.get("coverage")
    counts = coverage.get("counts") if isinstance(coverage, dict) else {}
    if not isinstance(counts, dict):
        return []
    incomplete_targets = int(counts.get("targets_incomplete") or 0)
    incomplete_checks = int(counts.get("checks_incomplete") or 0)
    if incomplete_targets <= 0 and incomplete_checks <= 0:
        return []
    active = []
    for target in coverage.get("targets", []) if isinstance(coverage, dict) else []:
        if not isinstance(target, dict):
            continue
        target_counts = dict(target.get("checks") or {})
        checks_open = [name for name, status in target_counts.items() if not task_progress_status_is_closed(status)]
        if checks_open or not task_progress_status_is_closed(target.get("status") or "pending"):
            active.append(
                {
                    "id": str(target.get("id") or target.get("title") or ""),
                    "checks_open": checks_open[:8],
                    "next": str(target.get("next") or ""),
                }
            )
    return [
        GateFinding(
            "TASK_PROGRESS_COVERAGE_INCOMPLETE",
            "soft",
            message="覆盖账本还有未完成对象或字段；这是软提醒，建议继续补齐 checks/evidence。",
            evidence={
                "targets_incomplete": incomplete_targets,
                "checks_incomplete": incomplete_checks,
                "active_targets": active[:12],
            },
        )
    ]


def _compact_item(item: dict[str, Any]) -> dict[str, str]:
    return {
        "id": str(item.get("id") or ""),
        "title": str(item.get("title") or ""),
        "status": str(item.get("status") or ""),
        "next": str(item.get("next") or ""),
    }


def _item_id(item: dict[str, Any]) -> str:
    return str(item.get("id") or item.get("title") or "").strip()


def _list(value: object) -> list:
    return list(value) if isinstance(value, list | tuple) else []


def _open_items_repair_message(open_items: list[dict[str, Any]], next_action: str) -> str:
    first = str(open_items[0].get("id") or open_items[0].get("title") or "").strip() if open_items else ""
    suffix = f"；下一步：{next_action}" if next_action else f"；先继续处理 {first}" if first else ""
    return f"进度账本还有 {len(open_items)} 个未完成项，建议提交前处理或在最终说明里解释{suffix}。"


__all__ = [
    "evaluate_task_progress_closeout_gate",
    "task_progress_all_done_without_artifact_evidence",
    "task_progress_has_open_items",
    "task_progress_repair_message",
]
