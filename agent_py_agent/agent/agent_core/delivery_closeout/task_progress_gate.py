from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ...contracts.gates.models import GateDecision, GateFinding
from ...contracts.recovery_actions import RecoveryAction
from ...task_progress import progress_path, read_task_progress, task_progress_summary

_CLOSED_STATUSES = {"done", "skipped"}
_SOURCE_FACT_TOOLS = {"read_file", "read_artifact", "memory_artifact_read", "web_fetch", "http_get"}
_FACT_TOKEN_RE = re.compile(
    r"\b(?:CPX|SVX|CP|SECRET|DECISION|KEEP|DROP|REVIEW|ESCALATE|HOLD|DEFER)-[A-Za-z0-9_-]{4,}\b"
)


def evaluate_task_progress_closeout_gate(closeout: object, report: dict[str, Any] | None = None) -> GateDecision:
    root = _progress_root(closeout)
    run_id = _run_id(closeout)
    if not root or not run_id:
        return GateDecision.allow(
            "task_progress_closeout",
            evidence={"checked": False, "reason": "progress_scope_missing"},
        )
    path = progress_path(root, run_id)
    if not path.exists():
        return GateDecision.allow(
            "task_progress_closeout",
            evidence={"checked": False, "reason": "progress_file_missing", "run_id": run_id},
        )
    progress = read_task_progress(root, run_id)
    summary = task_progress_summary({**progress, "ref": str(path)})
    open_items = _open_items(progress)
    findings: list[GateFinding] = []
    if not open_items:
        findings.extend(_done_quality_findings(progress, report or {}, closeout))
        if not findings:
            return GateDecision.allow(
                "task_progress_closeout",
                evidence={
                    "checked": True,
                    "run_id": run_id,
                    "progress_ref": str(path),
                    "counts": summary.get("counts", {}),
                },
            )
        return GateDecision.repair(
            "task_progress_closeout",
            findings,
            recommended_action=RecoveryAction.REPAIR.value,
            evidence={
                "checked": True,
                "run_id": run_id,
                "progress_ref": str(path),
                "counts": summary.get("counts", {}),
                "required_actions": [
                    "repair_task_progress_evidence_or_facts",
                    "rewrite_artifact_from_task_progress_facts",
                    "submit_for_acceptance_after_progress_and_artifact_match",
                ],
            },
        )
    next_action = str(summary.get("next_action") or "").strip()
    message = _open_items_repair_message(open_items, next_action)
    return GateDecision.repair(
        "task_progress_closeout",
        [
            GateFinding(
                "TASK_PROGRESS_OPEN_ITEMS",
                "P1",
                message=message,
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
        ],
        recommended_action=RecoveryAction.REPAIR.value,
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


def task_progress_repair_message(report: dict[str, Any]) -> str:
    payload = report.get("task_progress_closeout_gate")
    if not isinstance(payload, dict) or payload.get("allowed") is True:
        return ""
    message = str(payload.get("model_message") or "").strip()
    if message:
        return message
    findings = payload.get("findings")
    if isinstance(findings, list):
        for finding in findings:
            if isinstance(finding, dict):
                text = str(finding.get("message") or "").strip()
                if text:
                    return text
    return ""


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
        if isinstance(item, dict) and str(item.get("status") or "pending").strip() not in _CLOSED_STATUSES
    ]


def _done_quality_findings(progress: dict[str, Any], report: dict[str, Any], closeout: object) -> list[GateFinding]:
    items = [item for item in progress.get("items", []) if isinstance(item, dict)]
    done_items = [item for item in items if str(item.get("status") or "").strip() == "done"]
    if not done_items:
        return []
    findings: list[GateFinding] = []
    findings.extend(_coverage_incomplete_findings(progress))
    empty_done = [
        item
        for item in done_items
        if not _item_fact_tokens(item) and not _item_source_refs(item)
    ]
    if empty_done:
        findings.append(
            GateFinding(
                "TASK_PROGRESS_DONE_WITHOUT_EVIDENCE",
                "P1",
                message=(
                    f"进度账本里有 {len(empty_done)} 个 done 项没有事实或证据；"
                    "请补上读到的关键值、来源行或把这些项改回 pending 后继续读取。"
                ),
                evidence={
                    "item_ids": [_item_id(item) for item in empty_done[:20]],
                    "empty_done_count": len(empty_done),
                },
            )
        )
    missing_facts = _facts_missing_from_artifacts(done_items, report)
    if missing_facts:
        sample = missing_facts[:12]
        findings.append(
            GateFinding(
                "TASK_PROGRESS_FACTS_MISSING_FROM_ARTIFACT",
                "P1",
                message=(
                    "最终产物没有包含进度账本里已经记录的事实值；"
                    f"请按 task_progress 重写或补齐最终产物。缺失示例：{', '.join(sample)}"
                )[:500],
                evidence={
                    "missing_facts": sample,
                    "missing_fact_count": len(missing_facts),
                },
            )
        )
    unbacked_facts = _facts_not_source_backed(done_items, closeout)
    if unbacked_facts:
        sample = unbacked_facts[:12]
        findings.append(
            GateFinding(
                "TASK_PROGRESS_FACTS_NOT_SOURCE_BACKED",
                "P1",
                message=(
                    "进度账本里的事实值没有在本轮读取来源中找到；"
                    f"请重新读取来源并修正 progress/最终产物。缺少来源示例：{', '.join(sample)}"
                )[:500],
                evidence={
                    "unbacked_facts": sample,
                    "unbacked_fact_count": len(unbacked_facts),
                },
            )
        )
    return findings


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
        checks_open = [name for name, status in target_counts.items() if str(status or "").strip().lower() not in _CLOSED_STATUSES]
        if checks_open or str(target.get("status") or "").strip() not in _CLOSED_STATUSES:
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
            "P1",
            message=(
                "覆盖账本还有未完成对象或字段，不能直接提交验收；"
                "请继续读取来源、补齐 checks/evidence，并更新最终产物。"
            ),
            evidence={
                "targets_incomplete": incomplete_targets,
                "checks_incomplete": incomplete_checks,
                "active_targets": active[:12],
            },
        )
    ]


def _facts_missing_from_artifacts(done_items: list[dict[str, Any]], report: dict[str, Any]) -> list[str]:
    facts = _dedupe([fact for item in done_items for fact in _item_fact_tokens(item)])
    if not facts:
        return []
    artifact_text = _artifact_text(report)
    if not artifact_text:
        return facts[:40]
    return [fact for fact in facts if fact not in artifact_text]


def _artifact_text(report: dict[str, Any]) -> str:
    chunks: list[str] = []
    artifacts = report.get("artifacts")
    for artifact in artifacts if isinstance(artifacts, list) else []:
        if not isinstance(artifact, dict):
            continue
        path = Path(str(artifact.get("path") or "")).expanduser()
        if not path.is_absolute():
            root = Path(str(report.get("workspace_root") or "")).expanduser()
            path = root / path
        if path.suffix.lower() not in {"", ".txt", ".md", ".json", ".jsonl", ".csv", ".tsv"}:
            continue
        try:
            chunks.append(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            continue
    return "\n".join(chunks)


def _facts_not_source_backed(done_items: list[dict[str, Any]], closeout: object) -> list[str]:
    facts = _dedupe([fact for item in done_items for fact in _item_fact_tokens(item)])
    if not facts:
        return []
    archive_calls = _archive_tool_calls(closeout)
    source_text = _source_archive_text(archive_calls)
    if not source_text:
        return []
    return [fact for fact in facts if fact not in source_text]


def _archive_tool_calls(closeout: object) -> list[dict[str, Any]]:
    params = getattr(closeout, "params", None)
    records = getattr(params, "archive_tool_calls", None)
    return [dict(item) for item in records if isinstance(item, dict)] if isinstance(records, list) else []


def _source_archive_text(records: list[dict[str, Any]]) -> str:
    chunks: list[str] = []
    for record in records:
        if str(record.get("tool") or "").strip() not in _SOURCE_FACT_TOOLS:
            continue
        preview = str(record.get("output_preview") or "")
        if preview:
            chunks.append(preview)
        for key in ("source_artifact_ref", "source_output_path", "artifact_ref", "output_path"):
            text = _read_source_artifact_text(record.get(key))
            if text:
                chunks.append(text)
    return "\n".join(chunks)


def _read_source_artifact_text(value: object) -> str:
    text = str(value or "").strip()
    if not text or "://" in text:
        return ""
    path = Path(text).expanduser()
    if not path.exists() or not path.is_file():
        return ""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    payload = _json_object(raw)
    if isinstance(payload, dict) and isinstance(payload.get("content"), str):
        return str(payload.get("content") or "")
    return raw


def _json_object(raw: str) -> dict[str, Any] | None:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _item_fact_tokens(item: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    for key in ("notes", "result", "outcome", "conclusion", "decision", "summary"):
        value = str(item.get(key) or "").strip()
        if value:
            texts.append(value)
    texts.extend(str(value) for value in item.get("evidence", []) if isinstance(value, str))
    return _dedupe(token for text in texts for token in _FACT_TOKEN_RE.findall(text))


def _item_source_refs(item: dict[str, Any]) -> list[str]:
    refs = []
    evidence = item.get("evidence")
    for value in evidence if isinstance(evidence, list) else []:
        text = str(value or "").strip()
        if text and not _FACT_TOKEN_RE.fullmatch(text) and _looks_like_source_ref(text):
            refs.append(text)
    return refs


def _looks_like_source_ref(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    if "://" in value:
        return True
    if value.startswith(("run-", "artifact:", "memory_archive/", "blobs/", "/", "./", "../")):
        return True
    if re.search(r"\.(?:md|txt|json|jsonl|py|ts|tsx|js|jsx|yaml|yml|toml|csv|tsv|log)(?::|\b|#)", value, re.I):
        return True
    if re.search(r"\b(?:line|lines|L)\s*\d+", value, re.I):
        return True
    return False


def _compact_item(item: dict[str, Any]) -> dict[str, str]:
    return {
        "id": str(item.get("id") or ""),
        "title": str(item.get("title") or ""),
        "status": str(item.get("status") or ""),
        "next": str(item.get("next") or ""),
    }


def _item_id(item: dict[str, Any]) -> str:
    return str(item.get("id") or item.get("title") or "").strip()


def _dedupe(values) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _open_items_repair_message(open_items: list[dict[str, Any]], next_action: str) -> str:
    first = str(open_items[0].get("id") or open_items[0].get("title") or "").strip() if open_items else ""
    suffix = f"；下一步：{next_action}" if next_action else f"；先继续处理 {first}" if first else ""
    return f"进度账本还有 {len(open_items)} 个未完成项，不能直接提交验收{suffix}。"


__all__ = ["evaluate_task_progress_closeout_gate", "task_progress_repair_message"]
