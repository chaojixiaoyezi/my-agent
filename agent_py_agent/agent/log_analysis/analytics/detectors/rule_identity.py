# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

"""Identity and multi-source soft detector rules."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .classifiers import (
    _entities_from_events,
    _is_auth_event,
    _is_failure,
    _is_success,
    _is_vpn_event,
    _same_asset,
    _same_auth_scope,
    _same_source,
    _same_user,
    _weak_signal,
)
from .field_access import (
    EventLike,
    JsonDict,
    _canonical_time,
    _event_dict,
    _event_time,
    _field,
    _sort_time,
    _text,
    _time_bucket,
    _truthy,
    _within_after,
    _within_before,
)
from .field_extractors import _host, _source_ip, _user, _victim_ip
from .rule_helpers import MakeFindingParams, _make_finding, _query


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _VpnContext 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 _VpnContext 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class _VpnContext:
    event: JsonDict
    user: str
    country: str
    asn: str
    device: str


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 vpn_new_geo_login 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 vpn new geo login 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def vpn_new_geo_login(events: Sequence[EventLike], *, baselines: Any = None, window_minutes: int = 60) -> list[Any]:
    """Detect successful VPN logins from new geo/ASN/device or unusual hours."""
    del window_minutes
    from ..baselines import ensure_baselines

    baseline_obj = ensure_baselines(baselines)
    findings: list[Any] = []
    for event in [_event_dict(item) for item in events]:
        _append_vpn_finding(findings, event, baseline_obj)
    return findings


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _append_vpn_finding 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 append vpn finding 相关记录，集中处理目标路径、格式化和状态更新。
def _append_vpn_finding(findings: list[Any], event: JsonDict, baseline_obj: Any) -> None:
    if not (_is_vpn_event(event) and _is_success(event)):
        return
    finding = _vpn_finding_if_unusual(event, baseline_obj)
    if finding is not None:
        findings.append(finding)


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 bruteforce_then_success 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 bruteforce then success 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def bruteforce_then_success(
    events: Sequence[EventLike], *, baselines: Any = None, window_minutes: int = 15, failure_threshold: int = 5
) -> list[Any]:
    """Detect repeated auth failures followed by a successful login."""
    del baselines
    normalized = sorted((_event_dict(item) for item in events), key=_sort_time)
    auth_events = [event for event in normalized if _is_auth_event(event)]
    findings: list[Any] = []
    for success in [event for event in auth_events if _is_success(event)]:
        failures = _related_failures(auth_events, success, window_minutes)
        if len(failures) < failure_threshold:
            continue
        post_events = _post_success_events(normalized, success, window_minutes)
        findings.append(_bruteforce_finding(success, failures, post_events))
    return findings


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 multi_source_weak_signal 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 multi source weak signal 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def multi_source_weak_signal(events: Sequence[EventLike], *, baselines: Any = None, window_minutes: int = 15) -> list[Any]:
    """Detect overlapping weak signals from multiple sources on the same entity."""
    del baselines
    normalized = sorted((_event_dict(item) for item in events), key=_sort_time)
    groups = _weak_signal_groups(normalized, window_minutes)
    findings: list[Any] = []
    for signals in groups.values():
        signal_types = {str(signal["signal_type"]) for signal in signals}
        sources = {str(signal["source_product"]) for signal in signals if signal.get("source_product")}
        if len(signal_types) >= 2 or len(sources) >= 2:
            findings.append(_weak_signal_finding(signals, signal_types, sources))
    return findings


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _vpn_finding_if_unusual 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 vpn finding if unusual 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _vpn_finding_if_unusual(event: JsonDict, baseline_obj: Any) -> Any | None:
    user = _user(event)
    if not user:
        return None
    country, asn, device = _vpn_source_context(event)
    context = _VpnContext(event=event, user=user, country=country, asn=asn, device=device)
    flags = _vpn_unusual_flags(context, baseline_obj)
    if not any(flags.values()):
        return None
    return _vpn_finding(context, flags)


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _vpn_source_context 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 vpn source context 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _vpn_source_context(event: JsonDict) -> tuple[str, str, str]:
    country = _text(_field(event, "geo_country", "src_country", "country", "geo.country", "source.geo.country"))
    asn = _text(_field(event, "src_asn", "asn", "geo_asn", "source.asn"))
    device = _text(_field(event, "device_id", "device", "device_name", "client_device"))
    return country, asn, device


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _vpn_unusual_flags 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 vpn unusual flags 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _vpn_unusual_flags(context: _VpnContext, baseline_obj: Any) -> dict[str, bool]:
    return {
        "new_geo": _truthy(_field(context.event, "new_geo", "new_country", "is_new_geo")) or baseline_obj.is_new_country(context.user, context.country),
        "new_asn": _truthy(_field(context.event, "new_asn", "is_new_asn")) or baseline_obj.is_new_asn(context.user, context.asn),
        "new_device": _truthy(_field(context.event, "new_device", "is_new_device")) or baseline_obj.is_new_device(context.user, context.device),
        "unusual_hour": _truthy(_field(context.event, "unusual_hour", "is_unusual_hour")) or baseline_obj.is_unusual_login_hour(context.user, _event_time(context.event)),
    }


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _vpn_finding 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 vpn finding 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _vpn_finding(context: _VpnContext, flags: dict[str, bool]) -> Any:
    confidence = min(0.58 + (0.12 if flags["new_geo"] else 0.0) + (0.08 if flags["new_asn"] else 0.0) + (0.08 if flags["new_device"] else 0.0) + (0.05 if flags["unusual_hour"] else 0.0), 0.86)
    gaps = ["MFA result, device posture, and identity-risk context are not confirmed.", "Post-login host access is not yet correlated."]
    if not context.country:
        gaps.append("Source country is missing; GeoIP enrichment is needed.")
    if not context.asn:
        gaps.append("Source ASN is missing; ASN enrichment is needed.")
    return _make_finding(
        "vpn_new_geo_login",
        [context.event],
        params=MakeFindingParams(
            detector_id="vpn_new_geo_login",
            evidence_events=[context.event],
            hypothesis="Possible VPN credential misuse: successful login used new or unusual source context.",
            confidence=confidence,
            gaps=gaps,
            next_queries=[_query("Trace VPN session activity and assigned internal IP for this user", context.event), _query("Search host logons and admin actions by this user after VPN login", context.event), _query("Review MFA, device posture, and recent password reset events for this user", context.event)],
            features={**flags, "country": context.country, "asn": context.asn, "device": context.device},
            severity_hint="high" if confidence >= 0.7 else "medium",
            extra_entities={"user": [context.user], "attacker_ip": [_source_ip(context.event)], "src_ip": [_source_ip(context.event)], "country": [context.country], "asn": [context.asn], "device": [context.device]},
        ),
    )


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _related_failures 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 related failures 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _related_failures(auth_events: list[JsonDict], success: JsonDict, window_minutes: int) -> list[JsonDict]:
    return [event for event in auth_events if _is_failure(event) and _same_auth_scope(event, success) and _within_before(event, success, window_minutes)]


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _post_success_events 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 post success events 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _post_success_events(events: list[JsonDict], success: JsonDict, window_minutes: int) -> list[JsonDict]:
    return [event for event in events if event is not success and _within_after(success, event, window_minutes) and (_same_user(event, success) or _same_source(event, success) or _same_asset(success, event))]


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _bruteforce_finding 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 bruteforce finding 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _bruteforce_finding(success: JsonDict, failures: list[JsonDict], post_events: list[JsonDict]) -> Any:
    evidence_events = [*failures[:5], success, *post_events[:5]]
    confidence = min(0.68 + min(len(failures), 20) * 0.008 + (0.05 if post_events else 0.0), 0.88)
    return _make_finding(
        "bruteforce_then_success",
        evidence_events,
        params=MakeFindingParams(
            detector_id="bruteforce_then_success",
            evidence_events=evidence_events,
            hypothesis="Possible credential compromise: repeated failures were followed by a successful login.",
            confidence=confidence,
            gaps=["MFA result and lockout policy outcome are not confirmed.", "Credential owner confirmation is needed before treating the login as compromised."],
            next_queries=[_query("Review all authentication events for this user and source around the success", success), _query("Trace resource access and host logons after the successful authentication", success), _query("Check MFA, password reset, lockout, and impossible travel signals", success)],
            features={"failure_count": len(failures), "first_failure_time": _canonical_time(_event_time(failures[0])), "success_time": _canonical_time(_event_time(success)), "post_success_related_events": len(post_events)},
            severity_hint="high",
            extra_entities={"user": [_user(success)], "attacker_ip": [_source_ip(success)], "src_ip": [_source_ip(success)], "victim_ip": [_victim_ip(success)], "host": [_host(success)]},
        ),
    )


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _weak_signal_groups 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 weak signal groups 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _weak_signal_groups(events: list[JsonDict], window_minutes: int) -> dict[tuple[str, str], list[JsonDict]]:
    groups: dict[tuple[str, str], list[JsonDict]] = {}
    for signal in [signal for signal in (_weak_signal(event) for event in events) if signal]:
        event = signal["event"]
        key = (_weak_entity(event), _time_bucket(_event_time(event), window_minutes))
        if key[0]:
            groups.setdefault(key, []).append(signal)
    return groups


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _weak_entity 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 weak entity 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _weak_entity(event: JsonDict) -> str:
    from .classifiers import _primary_asset

    return _primary_asset(event) or _user(event) or _source_ip(event)


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _weak_signal_finding 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 weak signal finding 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _weak_signal_finding(signals: list[JsonDict], signal_types: set[str], sources: set[str]) -> Any:
    signal_events = [signal["event"] for signal in signals]
    confidence = min(0.53 + 0.06 * len(signal_types) + 0.035 * len(sources), 0.82)
    return _make_finding(
        "multi_source_weak_signal",
        signal_events[:10],
        params=MakeFindingParams(
            detector_id="multi_source_weak_signal",
            evidence_events=signal_events[:10],
            hypothesis="Possible intrusion path: multiple weak signals overlap on the same entity and time window.",
            confidence=confidence,
            gaps=["Signals are individually weak; analyst review must confirm whether they share one root cause.", "A route draft needs additional process, identity, and network context."],
            next_queries=[_query("Build a single timeline for the overlapping entity across WAF/VPN/EDR/DNS/proxy logs", signal_events[0]), _query("Expand related entities from the overlapping weak signals", signal_events[0]), _query("Check known maintenance, deployment, and vulnerability-scan windows", signal_events[0])],
            features={"signal_count": len(signals), "signal_types": sorted(signal_types), "source_products": sorted(sources)},
            severity_hint="high" if confidence >= 0.7 else "medium",
            extra_entities=_entities_from_events(signal_events),
        ),
    )


__all__ = ["bruteforce_then_success", "multi_source_weak_signal", "vpn_new_geo_login"]
