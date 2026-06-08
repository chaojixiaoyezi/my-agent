
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ...contracts.error_taxonomy import error_contract
from ...contracts.gates import GateDecision, GateFinding, evaluate_fact_evidence_gate
from ...contracts.recovery import RecoveryAction
from ...contracts.staged_checkpoint_acceptance import (
    StagedEvidenceRequest,
    staged_json_evidence_findings,
)
from .quality import workspace_relative_json_path
from .recovery_models import (
    RecoveryActionLedger,
    StagedEvidenceActionRequest,
)

_INLINE_CODE_SPAN_RE = re.compile(r"`([^`\n]{1,220})`")
_CALL_IDENTIFIER_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{3,})\s*\(")
_IDENTIFIER_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{3,}\b")
_SOURCE_FACT_MAX_SOURCE_CHARS = 1_000_000
_SOURCE_FACT_MAX_ARTIFACT_CHARS = 300_000
_SOURCE_FACT_MAX_FINDINGS = 20
_SOURCE_FACT_IDENTIFIER_STOPLIST = {
    "API",
    "ASCII",
    "CLI",
    "CSS",
    "CSV",
    "DOM",
    "GUI",
    "HTML",
    "HTTP",
    "HTTPS",
    "IDE",
    "JSON",
    "JWT",
    "JWK",
    "JWKS",
    "LLM",
    "MCP",
    "ORM",
    "PDF",
    "README",
    "SDK",
    "SQL",
    "SQLite",
    "TOML",
    "TSX",
    "UI",
    "URL",
    "UUID",
    "XML",
    "YAML",
}


def append_staged_evidence_actions(request: StagedEvidenceActionRequest) -> bool:
    evidence_contract = request.validation_contract.get("evidence_contract")
    if not isinstance(evidence_contract, dict):
        return False
    findings = staged_json_evidence_findings(
        StagedEvidenceRequest(request.checkpoint_ref, request.workspace_root, evidence_contract)
    )
    for action in _evidence_actions(request.checkpoint_ref, findings):
        _append_evidence_action(request.ledger, action)
    return bool(findings)


def _append_evidence_action(ledger: RecoveryActionLedger, action: dict[str, object]) -> None:
    action_code = str(action.get("code") or "")
    if not action_code or action_code in ledger.seen:
        return
    ledger.seen.add(action_code)
    ledger.actions.append(action)


def _evidence_actions(checkpoint_ref: str, findings: list[dict[str, Any]]) -> list[dict[str, object]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for finding in findings:
        code = str(finding.get("code") or "")
        if code:
            grouped.setdefault(code, []).append(finding)
    return [_one_grouped_evidence_action(checkpoint_ref, code, items) for code, items in grouped.items()]


def _one_grouped_evidence_action(
    checkpoint_ref: str,
    action_code: str,
    findings: list[dict[str, Any]],
) -> dict[str, object]:
    contract = error_contract(action_code)
    action: dict[str, object] = {
        "code": contract.code,
        "category": contract.category,
        "retryable": contract.retryable,
        "recommended_action": contract.recommended_action,
        "recovery_hint": contract.recovery_hint,
        "checkpoint_ref": checkpoint_ref,
    }
    fields = sorted({field for item in findings if (field := str(item.get("field") or "").strip())})
    claim_ids = sorted({claim_id for item in findings if (claim_id := str(item.get("claim_id") or "").strip())})
    if fields:
        action["required_fields"] = fields
    if claim_ids:
        action["claim_ids"] = claim_ids
    action.update(_evidence_writer_fields(checkpoint_ref))
    return action


def _evidence_writer_fields(checkpoint_ref: str) -> dict[str, object]:
    if not checkpoint_ref.lower().endswith(".json"):
        return {}
    return {
        "writer_tool": "write_file",
        "write_tools": ["write_file"],
        "evidence_shape_hint": (
            '{"source_refs":[{"source_id":"src-1","uri":"https://...","retrieved_at":"..."}],'
            '"claims":[{"field":"...","value":"...","source_ids":["src-1"],'
            '"verification_status":"VERIFIED","value_type":"exact","confidence":1.0,'
            '"methodology":"how this value was obtained"}]}'
        ),
    }


def fact_evidence_decision(
    *,
    contract: dict[str, Any],
    workspace_root: Path,
    archive_tool_calls: list[dict[str, Any]],
) -> GateDecision:
    fact_contract = fact_evidence_contract(contract)
    if not fact_contract:
        return GateDecision.allow("fact_evidence", evidence={"declared": False})
    payload = load_fact_evidence_payload(contract, workspace_root)
    if payload is None:
        return GateDecision.allow(
            "fact_evidence",
            recommended_action=RecoveryAction.RECORD_FACT_EVIDENCE_PAYLOAD.value,
            evidence={"declared": True, "warning_codes": ["FACT_EVIDENCE_PAYLOAD_MISSING"]},
        )
    decision = evaluate_fact_evidence_gate(payload, fact_contract, archive_tool_calls=archive_tool_calls)
    if not decision.allowed and not _fact_evidence_enforcement_required(fact_contract):
        return GateDecision.allow(
            "fact_evidence",
            recommended_action=RecoveryAction.REVIEW_FACT_EVIDENCE_FINDINGS.value,
            evidence={
                "declared": True,
                "warning_codes": list(decision.finding_codes),
                "advisory_status": decision.status,
                **decision.evidence,
            },
        )
    return decision


def target_coverage_projection_decision(report: dict[str, Any]) -> GateDecision:
    status = report.get("target_coverage_status")
    if not isinstance(status, dict):
        return GateDecision.allow("target_coverage_projection", evidence={"checked": False, "reason": "coverage_status_missing"})
    if str(status.get("enforcement") or "").strip().lower() != "required":
        return GateDecision.allow("target_coverage_projection", evidence={"checked": False, "reason": "coverage_not_required"})
    items = _coverage_projection_items(status.get("coverage_records"), status.get("target_items"))
    if len(items) < 5:
        return GateDecision.allow(
            "target_coverage_projection",
            evidence={"checked": True, "reason": "too_few_projectable_coverage_items", "checked_items": len(items)},
        )
    artifact_text = _artifact_text(report)
    if not artifact_text.strip():
        return GateDecision.allow(
            "target_coverage_projection",
            evidence={"checked": False, "reason": "artifact_text_missing", "checked_items": len(items)},
        )
    missing = [item for item in items if not _any_projection_token_present(artifact_text, item["tokens"])]
    required_missing = [item for item in missing if item.get("required_projection") is True]
    block_threshold = max(3, (len(items) + 2) // 3)
    evidence = {
        "checked": True,
        "checked_items": len(items),
        "missing_count": len(missing),
        "required_missing_count": len(required_missing),
        "block_threshold": block_threshold,
        "artifact_paths": _artifact_paths(report)[:12],
        "missing_items": missing[:20],
        "required_missing_items": required_missing[:20],
    }
    if not required_missing and len(missing) < block_threshold:
        findings = ()
        if missing:
            findings = (
                GateFinding(
                    "TARGET_COVERAGE_EVIDENCE_PARTIALLY_MISSING_FROM_ARTIFACT",
                    "soft",
                    message="最终交付物没有呈现一部分已读取源码证据；这是软提醒，不阻断验收。",
                    evidence=evidence,
                ),
            )
        return GateDecision(
            "target_coverage_projection",
            "ALLOW",
            True,
            findings,
            RecoveryAction.CONTINUE.value,
            evidence,
        )
    finding = GateFinding(
        "TARGET_COVERAGE_EVIDENCE_NOT_IN_ARTIFACT",
        "medium",
        message=(
            f"coverage ledger 里有 {len(missing)} 个目标或已读取源码证据没有出现在最终交付物中；"
            "请把对应文件、模块或来源引用写进最终报告后再提交。"
        ),
        evidence=evidence,
    )
    return GateDecision.repair(
        "target_coverage_projection",
        (finding,),
        recommended_action=RecoveryAction.CONTINUE.value,
        evidence={
            **evidence,
            "required_actions": [
                "copy_coverage_source_refs_into_final_artifact",
                "rewrite_or_append_final_artifact_with_source_file_refs",
                "submit_for_acceptance_after_final_artifact_mentions_coverage_evidence",
            ],
        },
    )


def source_fact_consistency_decision(report: dict[str, Any], *, workspace_root: Path) -> GateDecision:
    status = report.get("target_coverage_status")
    if not isinstance(status, dict):
        return GateDecision.allow("source_fact_consistency", evidence={"checked": False, "reason": "coverage_status_missing"})
    if str(status.get("enforcement") or "").strip().lower() != "required":
        return GateDecision.allow("source_fact_consistency", evidence={"checked": False, "reason": "coverage_not_required"})
    if status.get("should_block") is True:
        return GateDecision.allow("source_fact_consistency", evidence={"checked": False, "reason": "coverage_incomplete"})
    records = _source_fact_records(status.get("source_fact_records") or status.get("coverage_records"))
    if not records:
        return GateDecision.allow("source_fact_consistency", evidence={"checked": False, "reason": "read_file_coverage_missing"})
    artifact_text = _artifact_text(report)[:_SOURCE_FACT_MAX_ARTIFACT_CHARS]
    claimed = _artifact_code_identifiers(artifact_text)
    if not claimed:
        return GateDecision.allow(
            "source_fact_consistency",
            evidence={"checked": True, "reason": "no_code_identifier_claims", "source_count": len(records)},
        )
    evidence_tokens = _source_identifier_evidence(records, workspace_root)
    if not evidence_tokens:
        return GateDecision.allow(
            "source_fact_consistency",
            evidence={
                "checked": False,
                "reason": "source_identifier_evidence_unavailable",
                "claim_count": len(claimed),
                "source_count": len(records),
            },
        )
    unsupported = [token for token in claimed if token not in evidence_tokens]
    evidence = {
        "checked": True,
        "claim_count": len(claimed),
        "supported_count": len(claimed) - len(unsupported),
        "unsupported_count": len(unsupported),
        "unsupported_identifiers": unsupported[:_SOURCE_FACT_MAX_FINDINGS],
        "source_count": len(records),
        "artifact_paths": _artifact_paths(report)[:12],
    }
    if not unsupported:
        return GateDecision.allow("source_fact_consistency", evidence=evidence)
    finding = GateFinding(
        "SOURCE_CODE_IDENTIFIER_NOT_READ",
        "medium",
        message=(
            "最终交付物写入了本轮源码读取证据中没有出现的代码标识符；"
            "请回到对应源码文件核对，删除无证据的类/函数/模块名，或补充读取真实来源后再提交。"
        ),
        evidence=evidence,
    )
    return GateDecision.repair(
        "source_fact_consistency",
        (finding,),
        recommended_action=RecoveryAction.CONTINUE.value,
        evidence={
            **evidence,
            "required_actions": [
                "read_source_file_for_unsupported_identifiers",
                "remove_or_correct_unbacked_code_identifier_claims",
                "rewrite_final_artifact_with_source_backed_identifiers",
                "submit_for_acceptance_after_source_fact_repair",
            ],
        },
    )


def target_coverage_projection_repair_message(report: dict[str, Any]) -> str:
    payload = report.get("target_coverage_projection_gate")
    if not isinstance(payload, dict) or payload.get("allowed") is True:
        return ""
    message = str(payload.get("model_message") or "").strip()
    if message:
        return message
    return "当前最终交付物没有呈现已读取源码证据；请把 coverage ledger 中的文件/模块引用写进报告后再提交。"


def fact_evidence_contract(contract: dict[str, Any]) -> dict[str, Any]:
    value = contract.get("fact_evidence_contract")
    if isinstance(value, dict):
        return dict(value)
    quality = contract.get("delivery_quality_contract")
    if isinstance(quality, dict) and bool(quality.get("require_tool_backed_sources")):
        return dict(quality)
    return {}


def load_fact_evidence_payload(contract: dict[str, Any], workspace_root: Path) -> dict[str, Any] | None:
    ref = fact_evidence_payload_ref(contract)
    if not ref:
        return None
    path = workspace_relative_json_path(ref, workspace_root)
    if path is None or not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return dict(value) if isinstance(value, dict) else None


def fact_evidence_payload_ref(contract: dict[str, Any]) -> str:
    return str(contract.get("fact_evidence_payload_ref") or "").strip()


def _fact_evidence_enforcement_required(contract: dict[str, Any]) -> bool:
    value = str(contract.get("enforcement") or contract.get("mode") or "").strip().lower()
    return value == "required"


def _coverage_projection_items(value: object, target_items: object = None) -> list[dict[str, object]]:
    records = value if isinstance(value, list) else []
    target_roots = _coverage_target_roots(target_items)
    items: list[dict[str, object]] = []
    seen: set[tuple[str, ...]] = set()

    def append_item(item: dict[str, object]) -> None:
        tokens = [token for token in item.get("tokens", []) if isinstance(token, str) and token.strip()]
        key = tuple(_dedupe_projection_tokens(tokens))
        if not key or key in seen:
            return
        seen.add(key)
        items.append({**item, "tokens": list(key)[:8]})

    for item in _coverage_target_projection_items(target_items):
        append_item(item)

    for record in records:
        if not isinstance(record, dict):
            continue
        if str(record.get("tool") or "").strip() != "read_file":
            continue
        if str(record.get("status") or "").strip() not in {"covered", "partial"}:
            continue
        source = str(record.get("source_ref") or record.get("target_id") or "").strip()
        if target_roots and not _source_ref_under_any_root(source, target_roots):
            continue
        tokens = _coverage_source_tokens(source)
        append_item({"kind": "source_ref", "source_ref": source, "tokens": tokens, "status": str(record.get("status") or "")})
    return items


def _coverage_target_projection_items(value: object) -> list[dict[str, object]]:
    targets = value if isinstance(value, list) else []
    items: list[dict[str, object]] = []
    for target in targets:
        if not isinstance(target, dict):
            continue
        target_id = str(target.get("target_id") or "").strip()
        source = str(target.get("source_ref") or target.get("source_path") or target_id).strip()
        label = str(target.get("label") or target_id).strip()
        tokens = _target_projection_tokens(label=label, source=source, target_id=target_id)
        if not tokens:
            continue
        items.append({
            "kind": "target_label",
            "target_id": target_id,
            "source_ref": source,
            "tokens": tokens,
            "required_projection": _target_projection_required(label=label, source=source, target_id=target_id),
        })
    return items


def _target_projection_tokens(*, label: str, source: str, target_id: str) -> list[str]:
    tokens: list[str] = []
    for value in (label, target_id):
        if _is_plain_projection_label(value):
            tokens.append(value)
    parts = _path_projection_parts(source)
    if parts:
        tokens.append(parts[-1])
        if len(parts) >= 2 and _source_basename_projectable(parts[-1]):
            tokens.append("/".join(parts[-2:]))
    return _dedupe_projection_tokens(tokens)


def _target_projection_required(*, label: str, source: str, target_id: str) -> bool:
    if _is_plain_projection_label(label):
        return True
    if _is_plain_projection_label(target_id):
        return True
    parts = _path_projection_parts(source)
    return bool(parts and "." not in parts[-1])


def _is_plain_projection_label(value: str) -> bool:
    text = str(value or "").strip()
    if len(text) < 2:
        return False
    if "://" in text or "/" in text or "\\" in text:
        return False
    return True


def _path_projection_parts(source: str) -> list[str]:
    return [part for part in re.split(r"[\\/]+", source.strip()) if part]


def _coverage_target_roots(value: object) -> set[str]:
    items = value if isinstance(value, list) else []
    roots: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source_ref") or item.get("source_path") or item.get("target_id") or "").strip()
        if source:
            roots.add(_canonical_projection_ref(source))
    return roots


def _source_ref_under_any_root(source: str, roots: set[str]) -> bool:
    if not source:
        return False
    canonical = _canonical_projection_ref(source)
    return any(canonical == root or canonical.startswith(root.rstrip("/\\") + "/") for root in roots)


def _canonical_projection_ref(value: str) -> str:
    if "://" in value:
        return value
    try:
        return str(Path(value).expanduser().resolve(strict=False))
    except OSError:
        return value


_COMMON_SOURCE_BASENAMES = {
    "__init__.py",
    "readme.md",
    "package.json",
    "pyproject.toml",
    "cargo.toml",
    "tsconfig.json",
    "bun.lock",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
}


def _coverage_source_tokens(source: str) -> list[str]:
    parts = [part for part in re.split(r"[\\/]+", source.strip()) if part]
    if not parts:
        return []
    tokens: list[str] = []
    basename = parts[-1]
    if _source_basename_projectable(basename):
        tokens.append(basename)
        if len(parts) >= 2:
            tokens.append("/".join(parts[-2:]))
        if len(parts) >= 3:
            tokens.append("/".join(parts[-3:]))
    return _dedupe_projection_tokens(tokens)


def _source_basename_projectable(basename: str) -> bool:
    clean = basename.strip()
    if len(clean) < 4:
        return False
    if clean.lower() in _COMMON_SOURCE_BASENAMES:
        return False
    return "." in clean


def _dedupe_projection_tokens(tokens: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for token in tokens:
        clean = token.strip().replace("\\", "/")
        if not clean or clean in seen:
            continue
        seen.add(clean)
        deduped.append(clean)
    return deduped


def _artifact_text(report: dict[str, Any]) -> str:
    parts: list[str] = []
    for path in _artifact_paths(report):
        try:
            item = Path(path)
            if item.is_file():
                parts.append(item.read_text(encoding="utf-8", errors="ignore")[:200_000])
        except OSError:
            continue
    return "\n".join(parts)


def _artifact_paths(report: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for item in report.get("artifacts", []) if isinstance(report.get("artifacts"), list) else []:
        if not isinstance(item, dict) or item.get("ok") is not True:
            continue
        path = str(item.get("path") or "").strip()
        if path:
            paths.append(path)
    return paths


def _any_projection_token_present(text: str, tokens: list[object]) -> bool:
    return any(isinstance(token, str) and token and token in text for token in tokens)


def _source_fact_records(value: object) -> list[dict[str, object]]:
    records = value if isinstance(value, list) else []
    return [
        dict(record)
        for record in records
        if isinstance(record, dict)
        and str(record.get("tool") or "").strip() == "read_file"
        and str(record.get("status") or "").strip() == "covered"
        and str(record.get("source_ref") or record.get("target_id") or "").strip()
    ]


def _artifact_code_identifiers(text: str) -> list[str]:
    tokens: list[str] = []
    for match in _INLINE_CODE_SPAN_RE.finditer(text):
        span = match.group(1)
        if _looks_like_path_or_url(span):
            continue
        tokens.extend(_identifier for _identifier in _IDENTIFIER_RE.findall(span) if _source_fact_identifier(_identifier))
    tokens.extend(_identifier for _identifier in _CALL_IDENTIFIER_RE.findall(text) if _source_fact_identifier(_identifier))
    return _dedupe_projection_tokens(tokens)


def _source_identifier_evidence(records: list[dict[str, object]], workspace_root: Path) -> set[str]:
    tokens: set[str] = set()
    consumed = 0
    for record in records:
        if consumed >= _SOURCE_FACT_MAX_SOURCE_CHARS:
            break
        text = _source_text_for_record(record, workspace_root, remaining_chars=_SOURCE_FACT_MAX_SOURCE_CHARS - consumed)
        consumed += len(text)
        tokens.update(token for token in _IDENTIFIER_RE.findall(text) if _source_fact_identifier(token))
    return tokens


def _source_text_for_record(record: dict[str, object], workspace_root: Path, *, remaining_chars: int) -> str:
    path = _source_path_for_record(record, workspace_root)
    if path is None or not path.is_file():
        return ""
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    if str(record.get("coverage_kind") or "") == "line_window":
        return _line_window_text(content, record)[:remaining_chars]
    if str(record.get("coverage_kind") or "") == "char_window":
        return _char_window_text(content, record)[:remaining_chars]
    return content[:remaining_chars]


def _source_path_for_record(record: dict[str, object], workspace_root: Path) -> Path | None:
    source = str(record.get("source_ref") or record.get("target_id") or "").strip()
    if not source:
        return None
    candidate = Path(source).expanduser()
    try:
        if candidate.is_absolute():
            return candidate.resolve(strict=False)
        return (workspace_root / candidate).resolve(strict=False)
    except OSError:
        return None


def _line_window_text(content: str, record: dict[str, object]) -> str:
    lines = content.splitlines()
    ranges = _read_ranges(record)
    if not ranges:
        covered_until = _int_object(record.get("covered_until_line"))
        if covered_until <= 0:
            return content
        ranges = [{"start": 1, "end": covered_until}]
    parts: list[str] = []
    for item in ranges:
        start = max(1, _int_object(item.get("start"))) - 1
        end = _int_object(item.get("end"))
        if end <= 0:
            continue
        parts.append("\n".join(lines[start:end]))
    return "\n".join(parts)


def _char_window_text(content: str, record: dict[str, object]) -> str:
    ranges = _read_ranges(record)
    if not ranges:
        end = _int_object(record.get("covered_until_offset"))
        if end <= 0:
            end = _int_object(record.get("total_chars"))
        return content[:end] if end > 0 else content
    parts: list[str] = []
    for item in ranges:
        start = max(0, _int_object(item.get("start")))
        end = _int_object(item.get("end"))
        if end <= start:
            continue
        parts.append(content[start:end])
    return "\n".join(parts)


def _read_ranges(record: dict[str, object]) -> list[dict[str, object]]:
    value = record.get("read_ranges")
    return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _int_object(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _source_fact_identifier(token: str) -> bool:
    if len(token) < 5 or len(token) > 100:
        return False
    if token in _SOURCE_FACT_IDENTIFIER_STOPLIST or token.upper() in _SOURCE_FACT_IDENTIFIER_STOPLIST:
        return False
    if token.startswith("_"):
        return False
    if token.islower() or token.isupper():
        return "_" in token and any(char.isalpha() for char in token)
    has_lower = any(char.islower() for char in token)
    has_upper = any(char.isupper() for char in token)
    return has_lower and has_upper


def _looks_like_path_or_url(value: str) -> bool:
    text = value.strip()
    return "://" in text or "/" in text or "\\" in text or bool(re.search(r"\.[A-Za-z0-9]{1,8}\b", text))


__all__ = [
    "append_staged_evidence_actions",
    "fact_evidence_contract",
    "fact_evidence_decision",
    "fact_evidence_payload_ref",
    "load_fact_evidence_payload",
    "source_fact_consistency_decision",
    "target_coverage_projection_decision",
    "target_coverage_projection_repair_message",
]
