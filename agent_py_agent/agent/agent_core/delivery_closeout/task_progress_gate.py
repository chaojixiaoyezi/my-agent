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
    r"\b(?:(?:CP|CPX|SVX|SECRET)-[A-Za-z0-9_-]{3,}|(?:DECISION|KEEP|DROP|REVIEW|ESCALATE|HOLD|DEFER)-[A-Za-z0-9_-]{4,})\b"
)
_ARTIFACT_COMPLETE_SEQUENCE_RE = re.compile(
    r"(?:完整.{0,12}章节|章节.{0,12}清单|共\s*\d+\s*(?:个|项)?\s*章节|共\s*\d+\s*项)"
)
_ARTIFACT_CHAPTER_NUMBER_RE = re.compile(r"(?:章节|chapter|section|ch)[\s#：:|-]*0*(\d{1,4})\b", re.I)
_ARTIFACT_TABLE_NUMBER_RE = re.compile(r"^\|\s*0*(\d{1,4})\s*\|.*?(?:CP-\d{3}-\w{3,}|检查点)", re.M)
_CP_SEQUENCE_NUMBER_RE = re.compile(r"\bCP-(\d{3})-\w{3,}\b")
_KEY_VALUE_FACT_RE = re.compile(r"(?P<key>[^:：=；;\n。]{1,32})\s*[:：=]\s*(?P<value>[^；;\n。]{1,120})")
_SOURCE_VALUE_KEYS = {
    "source",
    "source_ref",
    "evidence",
    "path",
    "file",
    "line",
    "lines",
    "offset",
    "range",
    "来源",
    "证据",
    "路径",
    "文件",
    "行号",
    "位置",
    "范围",
}
_OPERATIONAL_FACT_KEY_MARKERS = (
    "offset",
    "cursor",
    "covered",
    "coverage",
    "read_until",
    "read offset",
    "chars",
    "bytes",
    "读取",
    "读到",
    "已读",
    "待读",
    "覆盖",
    "游标",
    "字符",
    "剩余",
    "范围",
)
_NUMERIC_RANGE_RE = re.compile(r"^\d+\s*(?:~|-|至|到)\s*\d+$")
_REPAIR_NOTE_MARKERS = (
    "曾经",
    "错误",
    "误用",
    "误写",
    "已改",
    "改回",
    "修正",
    "删除",
    "报告",
)


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
    if not open_items:
        return _closed_progress_decision(progress, report or {}, closeout, run_id, path, summary)
    return _open_progress_decision(open_items, run_id, path, summary)


def _unchecked_progress_decision(reason: str, run_id: str = "") -> GateDecision:
    evidence = {"checked": False, "reason": reason}
    if run_id:
        evidence["run_id"] = run_id
    return GateDecision.allow(
        "task_progress_closeout",
        evidence=evidence,
    )


def _closed_progress_decision(
    progress: dict[str, Any],
    report: dict[str, Any],
    closeout: object,
    run_id: str,
    path: Path,
    summary: dict[str, Any],
) -> GateDecision:
    hard_findings, advisory_findings = _done_quality_findings(progress, report, closeout)
    if not hard_findings:
        return GateDecision(
            "task_progress_closeout",
            "ALLOW",
            True,
            tuple(advisory_findings),
            RecoveryAction.CONTINUE.value,
            _closed_progress_evidence(run_id, path, summary, advisory_findings),
        )
    return GateDecision.repair(
        "task_progress_closeout",
        hard_findings,
        recommended_action=RecoveryAction.REPAIR.value,
        evidence=_repair_progress_evidence(run_id, path, summary),
    )


def _closed_progress_evidence(
    run_id: str,
    path: Path,
    summary: dict[str, Any],
    advisory_findings: list[GateFinding],
) -> dict[str, object]:
    return {
        "checked": True,
        "run_id": run_id,
        "progress_ref": str(path),
        "counts": summary.get("counts", {}),
        "advisory_finding_codes": [finding.code for finding in advisory_findings],
    }


def _repair_progress_evidence(run_id: str, path: Path, summary: dict[str, Any]) -> dict[str, object]:
    return {
        "checked": True,
        "run_id": run_id,
        "progress_ref": str(path),
        "counts": summary.get("counts", {}),
        "required_actions": [
            "repair_task_progress_evidence_or_facts",
            "rewrite_artifact_from_task_progress_facts",
            "submit_for_acceptance_after_progress_and_artifact_match",
        ],
    }


def _open_progress_decision(
    open_items: list[dict[str, Any]],
    run_id: str,
    path: Path,
    summary: dict[str, Any],
) -> GateDecision:
    next_action = str(summary.get("next_action") or "").strip()
    return GateDecision.repair(
        "task_progress_closeout",
        [
            GateFinding(
                "TASK_PROGRESS_OPEN_ITEMS",
                "P1",
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


def _done_quality_findings(
    progress: dict[str, Any],
    report: dict[str, Any],
    closeout: object,
) -> tuple[list[GateFinding], list[GateFinding]]:
    items = [item for item in progress.get("items", []) if isinstance(item, dict)]
    done_items = [item for item in items if str(item.get("status") or "").strip() == "done"]
    if not done_items:
        return [], []
    hard_findings: list[GateFinding] = []
    advisory_findings: list[GateFinding] = []
    advisory_findings.extend(_coverage_incomplete_findings(progress))
    hard_findings.extend(_numeric_sequence_gap_findings(done_items))
    hard_findings.extend(_artifact_numeric_sequence_gap_findings(report))
    advisory_findings.extend(_empty_done_advisory_findings(done_items))
    hard_findings.extend(_missing_artifact_fact_findings(done_items, report))
    hard_findings.extend(_unbacked_progress_fact_findings(done_items, closeout))
    hard_findings.extend(_unbacked_artifact_fact_findings(report, closeout))
    return hard_findings, advisory_findings


def _empty_done_advisory_findings(done_items: list[dict[str, Any]]) -> list[GateFinding]:
    empty_done = [
        item
        for item in done_items
        if not _item_fact_tokens(item) and not _item_source_refs(item)
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


def _missing_artifact_fact_findings(done_items: list[dict[str, Any]], report: dict[str, Any]) -> list[GateFinding]:
    missing_facts = _facts_missing_from_artifacts(done_items, report)
    if not missing_facts:
        return []
    sample = missing_facts[:12]
    return [
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
    ]


def _unbacked_progress_fact_findings(done_items: list[dict[str, Any]], closeout: object) -> list[GateFinding]:
    unbacked_facts = _facts_not_source_backed(done_items, closeout)
    if not unbacked_facts:
        return []
    sample = unbacked_facts[:12]
    return [
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
    ]


def _unbacked_artifact_fact_findings(report: dict[str, Any], closeout: object) -> list[GateFinding]:
    unbacked_artifact_facts = _artifact_facts_not_source_backed(report, closeout)
    if not unbacked_artifact_facts:
        return []
    sample = unbacked_artifact_facts[:12]
    return [
        GateFinding(
            "TASK_PROGRESS_ARTIFACT_FACTS_NOT_SOURCE_BACKED",
            "P1",
            message=(
                "最终产物里出现了来源中没有的结构化事实编号；"
                f"请删除或重新读取来源核实。缺少来源示例：{', '.join(sample)}"
            )[:500],
            evidence={
                "unbacked_artifact_facts": sample,
                "unbacked_artifact_fact_count": len(unbacked_artifact_facts),
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
            "soft",
            message=(
                "覆盖账本还有未完成对象或字段；这是软提醒，建议继续补齐 checks/evidence。"
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


def _numeric_sequence_gap_findings(done_items: list[dict[str, Any]]) -> list[GateFinding]:
    identities = [identity for item in done_items if (identity := _numeric_item_identity(item))]
    non_empty_prefixes = {prefix for prefix, _, _ in identities if prefix}
    groups: dict[str, dict[str, Any]] = {}
    for identity in identities:
        prefix, number, width = identity
        if not prefix and len(non_empty_prefixes) == 1:
            prefix = next(iter(non_empty_prefixes))
        group = groups.setdefault(prefix, {"numbers": set(), "width": width})
        group["numbers"].add(number)
        group["width"] = max(int(group.get("width") or 0), width)
    findings: list[GateFinding] = []
    for prefix, group in groups.items():
        numbers = set(group.get("numbers") or set())
        if len(numbers) < 8:
            continue
        start = min(numbers)
        end = max(numbers)
        if start != 1:
            continue
        span = end - start + 1
        if span <= 0 or len(numbers) / span < 0.7:
            continue
        missing = [number for number in range(start, end + 1) if number not in numbers]
        if not missing:
            continue
        width = int(group.get("width") or 3)
        findings.append(
            GateFinding(
                "TASK_PROGRESS_NUMERIC_SEQUENCE_GAP",
                "P1",
                message=(
                    "进度账本里的编号序列有缺口；请补读/补记缺失编号后再提交验收。"
                    f"缺失示例：{', '.join(_format_numeric_item(prefix, number, width) for number in missing[:12])}"
                )[:500],
                evidence={
                    "prefix": prefix,
                    "range_start": start,
                    "range_end": end,
                    "missing_count": len(missing),
                    "missing_items": [_format_numeric_item(prefix, number, width) for number in missing[:40]],
                },
            )
        )
    return findings


def _artifact_numeric_sequence_gap_findings(report: dict[str, Any]) -> list[GateFinding]:
    text = _artifact_text(report)
    if not text or not _artifact_declares_complete_sequence(text):
        return []
    numbers = _artifact_sequence_numbers(text)
    if len(numbers) < 8:
        return []
    declared_total = _artifact_declared_total(text)
    start = 1 if declared_total or min(numbers) > 1 else min(numbers)
    end = max(max(numbers), declared_total or 0)
    if end < 8:
        return []
    missing = [number for number in range(start, end + 1) if number not in numbers]
    if not missing:
        return []
    return [
        GateFinding(
            "TASK_PROGRESS_ARTIFACT_NUMERIC_SEQUENCE_GAP",
            "P1",
            message=(
                "最终产物声明了完整编号清单，但正文编号序列有缺口；"
                f"请补齐缺失编号后重新提交。缺失示例：{', '.join(f'{number:03d}' for number in missing[:12])}"
            )[:500],
            evidence={
                "range_start": start,
                "range_end": end,
                "declared_total": declared_total,
                "missing_count": len(missing),
                "missing_items": [f"{number:03d}" for number in missing[:40]],
            },
        )
    ]


def _artifact_declares_complete_sequence(text: str) -> bool:
    return bool(_ARTIFACT_COMPLETE_SEQUENCE_RE.search(text))


def _artifact_declared_total(text: str) -> int | None:
    matches = [
        int(match.group(1))
        for match in re.finditer(r"共\s*(\d{1,5})\s*(?:个|项)?\s*(?:章节|项)", text)
    ]
    return max(matches) if matches else None


def _artifact_sequence_numbers(text: str) -> set[int]:
    numbers: set[int] = set()
    for regex in (_ARTIFACT_CHAPTER_NUMBER_RE, _ARTIFACT_TABLE_NUMBER_RE, _CP_SEQUENCE_NUMBER_RE):
        for match in regex.finditer(text):
            try:
                numbers.add(int(match.group(1)))
            except (TypeError, ValueError):
                continue
    return numbers


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
    source_text = _source_archive_text(archive_calls, workspace_root=_workspace_root(closeout))
    if not source_text:
        return []
    return [fact for fact in facts if fact not in source_text]


def _artifact_facts_not_source_backed(report: dict[str, Any], closeout: object) -> list[str]:
    artifact_text = _artifact_text(report)
    if not artifact_text:
        return []
    facts = _dedupe(_fact_tokens(artifact_text))
    if not facts:
        return []
    source_text = _source_archive_text(_archive_tool_calls(closeout), workspace_root=_workspace_root(closeout))
    if not source_text:
        return []
    return [fact for fact in facts if fact not in source_text]


def _archive_tool_calls(closeout: object) -> list[dict[str, Any]]:
    params = getattr(closeout, "params", None)
    records = getattr(params, "archive_tool_calls", None)
    return [dict(item) for item in records if isinstance(item, dict)] if isinstance(records, list) else []


def _source_archive_text(records: list[dict[str, Any]], *, workspace_root: Path | None = None) -> str:
    chunks: list[str] = []
    write_targets = _write_target_paths(records, workspace_root)
    for record in records:
        if str(record.get("tool") or "").strip() not in _SOURCE_FACT_TOOLS:
            continue
        if _is_internal_source_record(record, workspace_root, write_targets):
            continue
        preview = str(record.get("output_preview") or "")
        if preview:
            chunks.append(preview)
        for key in ("source_artifact_ref", "source_output_path", "artifact_ref", "output_path", "path"):
            text = _read_source_artifact_text(record.get(key))
            if text:
                chunks.append(text)
    return "\n".join(chunks)


def _workspace_root(closeout: object) -> Path | None:
    agent = getattr(closeout, "agent", None)
    root = getattr(getattr(agent, "tools", None), "workspace_root", None) or getattr(agent, "root", None)
    if not root:
        return None
    try:
        return Path(root).expanduser().resolve(strict=False)
    except OSError:
        return None


def _is_internal_source_record(
    record: dict[str, Any],
    workspace_root: Path | None,
    write_targets: set[Path],
) -> bool:
    path_text = _record_requested_path(record)
    if not path_text:
        return str(record.get("tool") or "").strip() in {"read_artifact", "memory_artifact_read"}
    normalized = _normalize_record_path(path_text, workspace_root)
    if normalized in write_targets:
        return True
    text = str(normalized)
    if "/.my_agent/" in text or "/.agent_delivery/" in text:
        return True
    parts = normalized.parts
    return "tasks" in parts and ("output" in parts or "work" in parts)


def _record_requested_path(record: dict[str, Any]) -> str:
    parameters = record.get("parameters")
    params = parameters if isinstance(parameters, dict) else {}
    for key in ("path", "file_path", "source_path", "artifact_ref", "source_ref"):
        text = str(params.get(key) or record.get(key) or "").strip()
        if text:
            return text
    return ""


def _write_target_paths(records: list[dict[str, Any]], workspace_root: Path | None) -> set[Path]:
    paths: set[Path] = set()
    for record in records:
        if str(record.get("tool") or "").strip() != "write_file":
            continue
        if path_text := _record_requested_path(record):
            paths.add(_normalize_record_path(path_text, workspace_root))
    return paths


def _normalize_record_path(value: str, workspace_root: Path | None) -> Path:
    path = Path(str(value or "")).expanduser()
    if not path.is_absolute() and workspace_root is not None:
        path = workspace_root / path
    try:
        return path.resolve(strict=False)
    except OSError:
        return path


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
    for key in ("result", "outcome", "conclusion", "decision", "summary"):
        value = str(item.get(key) or "").strip()
        if value:
            texts.append(value)
    notes = str(item.get("notes") or "").strip()
    if notes and not _looks_like_repair_note(notes):
        texts.append(notes)
    texts.extend(str(value) for value in item.get("evidence", []) if isinstance(value, str))
    return _dedupe(
        token
        for text in texts
        for token in [*_fact_tokens(text), *_key_value_fact_values(text)]
        if not _looks_like_placeholder_fact_token(token)
    )


def _fact_tokens(text: str) -> list[str]:
    return [token for token in _FACT_TOKEN_RE.findall(str(text or "")) if not _looks_like_placeholder_fact_token(token)]


def _looks_like_placeholder_fact_token(token: str) -> bool:
    parts = str(token or "").split("-")[1:]
    return any(part and set(part.upper()) <= {"X", "Y", "Z"} for part in parts)


def _looks_like_repair_note(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    return sum(1 for marker in _REPAIR_NOTE_MARKERS if marker in value) >= 2


def _numeric_item_identity(item: dict[str, Any]) -> tuple[str, int, int] | None:
    for key in ("id", "title"):
        text = str(item.get(key) or "").strip().lower()
        match = re.search(r"([a-z\u4e00-\u9fff_-]*?)[-_ ]?(\d{1,6})", text)
        if not match:
            continue
        prefix = _normalize_numeric_prefix(match.group(1))
        digits = match.group(2)
        return prefix, int(digits), len(digits)
    return None


def _normalize_numeric_prefix(value: str) -> str:
    text = str(value or "").strip().lower().replace("_", "-")
    aliases = {
        "chapter": "ch",
        "chap": "ch",
        "章节": "ch",
        "fragment": "fragment",
        "frag": "fragment",
        "片段": "fragment",
    }
    return aliases.get(text, text)


def _format_numeric_item(prefix: str, number: int, width: int) -> str:
    if not prefix:
        return f"{number:0{max(1, width)}d}"
    separator = "-" if prefix == "fragment" else ""
    return f"{prefix}{separator}{number:0{max(1, width)}d}"


def _key_value_fact_values(text: str) -> list[str]:
    values: list[str] = []
    for match in _KEY_VALUE_FACT_RE.finditer(text):
        key = _clean_fact_text(match.group("key")).lower()
        value = _clean_fact_text(match.group("value"))
        if _skip_key_value_fact(key, value):
            continue
        values.append(value)
    return values


def _skip_key_value_fact(key: str, value: str) -> bool:
    if not value or len(value) > 80:
        return True
    if key in _SOURCE_VALUE_KEYS:
        return True
    if _looks_like_operational_cursor_fact(key, value):
        return True
    if value.isdigit() and len(value) < 4:
        return True
    if _looks_like_source_ref(value):
        return True
    return False


def _looks_like_operational_cursor_fact(key: str, value: str) -> bool:
    key_text = str(key or "").strip().lower()
    value_text = str(value or "").strip()
    if any(marker in key_text for marker in _OPERATIONAL_FACT_KEY_MARKERS):
        return True
    return bool(_NUMERIC_RANGE_RE.fullmatch(value_text))


def _clean_fact_text(value: object) -> str:
    text = str(value or "").strip()
    return text.strip(" \t\r\n'\"`，,。")


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
