from __future__ import annotations

"""Query-plan drafts for analyst and hunt agents."""

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from ..models import CaseRecord
from .correlation import RouteDraft


@dataclass
class HuntQuery:
    query_id: str
    purpose: str
    seed_type: str
    seed_value: str
    source_products: list[str] = field(default_factory=list)
    time_window: tuple[str, str] = ("", "")
    filters: dict[str, Any] = field(default_factory=dict)
    limit: int = 500

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_seed_hunt_queries(
    seed_type: str,
    seed_value: str,
    *,
    start: str = "",
    end: str = "",
) -> list[HuntQuery]:
    clean_type = str(seed_type or "").strip()
    clean_value = str(seed_value or "").strip()
    if not clean_type or not clean_value:
        return []
    products_by_seed = {
        "ip": ["waf", "vpn", "edr", "dns", "proxy", "netflow"],
        "attacker_ip": ["waf", "vpn", "firewall", "proxy"],
        "victim_ip": ["edr", "hids", "netflow", "dns", "proxy"],
        "user": ["vpn", "sso", "ad", "windows", "pam"],
        "host": ["edr", "hids", "windows", "linux", "netflow"],
        "domain": ["dns", "proxy", "waf"],
        "hash": ["edr", "hids", "sandbox"],
        "process": ["edr", "sysmon", "linux_audit"],
    }
    products = products_by_seed.get(clean_type, ["waf", "vpn", "edr", "dns", "proxy"])
    specs = (
        ("Find direct sightings of the seed.", products),
        ("Find entities related to the seed before and after the case window.", products),
        ("Search peer assets or accounts for the same behavior.", products),
    )
    seed_digest = hashlib.sha256(f"{clean_type}:{clean_value}".encode()).hexdigest()[:8]
    return [
        HuntQuery(
            query_id=f"hunt-{clean_type}-{seed_digest}-{index + 1}",
            purpose=purpose,
            seed_type=clean_type,
            seed_value=clean_value,
            source_products=sources,
            time_window=(start, end),
            filters={clean_type: clean_value},
        )
        for index, (purpose, sources) in enumerate(specs)
    ]


def build_case_hunt_plan(case: CaseRecord | Mapping[str, Any], route: RouteDraft | Mapping[str, Any] | None = None) -> list[HuntQuery]:
    case_obj = case if isinstance(case, CaseRecord) else CaseRecord.from_dict(case)
    route_dict = route.to_dict() if isinstance(route, RouteDraft) else dict(route or {})
    start, end = _case_window(case_obj, route_dict)
    queries: list[HuntQuery] = []
    for seed_type, values in case_obj.entities.items():
        if seed_type not in {"attacker_ip", "victim_ip", "src_ip", "dst_ip", "user", "host", "domain", "process"}:
            continue
        normalized_type = "ip" if seed_type in {"src_ip", "dst_ip"} else seed_type
        for value in values[:3]:
            queries.extend(build_seed_hunt_queries(normalized_type, value, start=start, end=end))
    return _dedupe_queries(queries)


def next_query_plan(case: CaseRecord | Mapping[str, Any], route: RouteDraft | Mapping[str, Any] | None = None) -> dict[str, Any]:
    case_obj = case if isinstance(case, CaseRecord) else CaseRecord.from_dict(case)
    route_dict = route.to_dict() if isinstance(route, RouteDraft) else dict(route or {})
    return {
        "case_id": case_obj.case_id,
        "next_queries": _unique([*case_obj.next_queries, *route_dict.get("next_queries", [])]),
        "hunt_queries": [query.to_dict() for query in build_case_hunt_plan(case_obj, route_dict)],
    }


def retrohunt_query_plan(seed_type: str, seed_value: str, *, days: int = 30) -> dict[str, Any]:
    return {
        "kind": "retrohunt",
        "seed_type": seed_type,
        "seed_value": seed_value,
        "lookback_days": days,
        "queries": [query.to_dict() for query in build_seed_hunt_queries(seed_type, seed_value)],
    }


def _case_window(case: CaseRecord, route: Mapping[str, Any]) -> tuple[str, str]:
    timeline = route.get("timeline") or []
    times = [str(item.get("time", "")) for item in timeline if isinstance(item, Mapping) and item.get("time")]
    if times:
        return (min(times), max(times))
    return (case.created_at, case.updated_at)


def _dedupe_queries(queries: Sequence[HuntQuery]) -> list[HuntQuery]:
    result: list[HuntQuery] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    for query in queries:
        key = (query.seed_type, query.seed_value, tuple(query.source_products))
        if key in seen:
            continue
        seen.add(key)
        result.append(query)
    return result


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


__all__ = [
    "HuntQuery",
    "build_case_hunt_plan",
    "build_seed_hunt_queries",
    "next_query_plan",
    "retrohunt_query_plan",
]
