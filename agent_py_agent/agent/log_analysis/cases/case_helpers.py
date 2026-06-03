
from __future__ import annotations

"""Pure helper functions for finding-to-case normalization."""

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from ..models import CaseRecord, EvidenceRef, Finding, QueryPlan


def _ensure_finding(value: Finding | Mapping[str, Any]) -> Finding:
    return value if isinstance(value, Finding) else Finding.from_dict(value)


def _ensure_case(value: CaseRecord | Mapping[str, Any]) -> CaseRecord:
    return value if isinstance(value, CaseRecord) else CaseRecord.from_dict(value)


def _title_for_finding(finding: Finding) -> str:
    victim = _primary_victim(finding) or "unknown asset"
    return f"{finding.detector_id}: {victim}"


def _primary_attacker(finding: Finding) -> str:
    values = _entity_values(finding.entities, "attacker_ip")
    return values[0] if values else ""


def _primary_victim(finding: Finding) -> str:
    values = _entity_values(finding.entities, "victim_ip", "dst_ip", "host", "asset_id")
    if values:
        return values[0]
    source_values = _entity_values(finding.entities, "src_ip")
    return source_values[0] if source_values and not _primary_attacker(finding) else ""


def _primary_account(finding: Finding) -> str:
    values = _entity_values(finding.entities, "user", "account", "principal")
    return values[0] if values else ""


def _entity_values(entities: Mapping[str, Sequence[Any]], *keys: str) -> list[str]:
    values: list[str] = []
    for key in keys:
        _append_unique_texts(values, entities.get(key, []))
    return values


def _append_unique_texts(output: list[str], values: Sequence[Any]) -> None:
    for value in values:
        text = str(value or "").strip()
        if text and text not in output:
            output.append(text)


def _merge_entities(left: Mapping[str, Sequence[Any]], right: Mapping[str, Sequence[Any]]) -> dict[str, list[str]]:
    merged: dict[str, list[str]] = {key: [str(value) for value in values] for key, values in left.items()}
    for key, values in right.items():
        merged.setdefault(key, [])
        merged[key].extend(str(value) for value in values)
    return _normalize_entities(merged)


def _normalize_entities(entities: Mapping[str, Sequence[Any]]) -> dict[str, list[str]]:
    normalized: dict[str, list[str]] = {}
    for key, values in entities.items():
        clean_key = str(key or "").strip()
        if not clean_key:
            continue
        normalized[clean_key] = _unique(str(value).strip() for value in values if str(value or "").strip())
    return {key: values for key, values in sorted(normalized.items()) if values}


def _merge_evidence_refs(left: Sequence[EvidenceRef], right: Sequence[EvidenceRef]) -> list[EvidenceRef]:
    merged: dict[str, EvidenceRef] = {}
    for ref in [*left, *right]:
        evidence = ref if isinstance(ref, EvidenceRef) else EvidenceRef.from_dict(ref)
        merged[evidence.evidence_id] = evidence
    return list(merged.values())


def _case_type_for_findings(findings: Sequence[Finding]) -> str:
    detector_ids = {finding.detector_id for finding in findings}
    if {"waf_attack_success_candidate", "web_to_process_anomaly", "rare_egress_after_alert"}.issubset(detector_ids):
        return "suspected_zero_day_intrusion"
    if "vpn_new_geo_login" in detector_ids or "bruteforce_then_success" in detector_ids:
        return "suspected_credential_intrusion"
    if "waf_attack_success_candidate" in detector_ids:
        return "web_intrusion_candidate"
    return "security_investigation"


def _unique(values: Sequence[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _unique_values(values: Sequence[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        item = _unique_marker_value(value)
        if not item:
            continue
        marker = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
        if marker in seen:
            continue
        seen.add(marker)
        result.append(item)
    return result


def _unique_marker_value(value: Any) -> Any:
    if isinstance(value, QueryPlan):
        return value.to_dict()
    if isinstance(value, Mapping):
        return dict(value)
    return str(value or "").strip()


def _time_bucket(value: str, minutes: int) -> str:
    parsed = _parse_time(value)
    if parsed is None:
        return "unknown-time"
    minute = (parsed.minute // max(minutes, 1)) * max(minutes, 1)
    bucket = parsed.astimezone(timezone.utc).replace(minute=minute, second=0, microsecond=0)
    return bucket.isoformat().replace("+00:00", "Z")


def _bucket_from_dedup_key(value: str) -> str:
    for part in value.split("|"):
        if part.startswith("bucket="):
            return part.removeprefix("bucket=")
    return ""


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _stable_case_id(seed: str) -> str:
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"case-{today}-{digest[:10]}"


def _clamp_score(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, score))
