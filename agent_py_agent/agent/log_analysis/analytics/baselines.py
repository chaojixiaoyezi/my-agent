from __future__ import annotations

"""Small in-memory baseline helpers for soft detectors."""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _as_set(values: Any) -> set[str]:
    if values is None:
        return set()
    if isinstance(values, str):
        return {_norm(values)} if values.strip() else set()
    if isinstance(values, Iterable):
        return {_norm(item) for item in values if _norm(item)}
    return {_norm(values)}


@dataclass
class SecurityBaselines:
    known_countries_by_user: dict[str, set[str]] = field(default_factory=dict)
    known_asns_by_user: dict[str, set[str]] = field(default_factory=dict)
    known_devices_by_user: dict[str, set[str]] = field(default_factory=dict)
    known_login_hours_by_user: dict[str, set[int]] = field(default_factory=dict)
    known_egress_destinations_by_asset: dict[str, set[str]] = field(default_factory=dict)
    known_egress_ports_by_asset: dict[str, set[int]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> SecurityBaselines:
        payload = payload or {}
        return cls(
            known_countries_by_user=_set_map(payload.get("known_countries_by_user")),
            known_asns_by_user=_set_map(payload.get("known_asns_by_user")),
            known_devices_by_user=_set_map(payload.get("known_devices_by_user")),
            known_login_hours_by_user=_int_set_map(payload.get("known_login_hours_by_user")),
            known_egress_destinations_by_asset=_set_map(payload.get("known_egress_destinations_by_asset")),
            known_egress_ports_by_asset=_int_set_map(payload.get("known_egress_ports_by_asset")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "known_countries_by_user": _sorted_map(self.known_countries_by_user),
            "known_asns_by_user": _sorted_map(self.known_asns_by_user),
            "known_devices_by_user": _sorted_map(self.known_devices_by_user),
            "known_login_hours_by_user": _sorted_int_map(self.known_login_hours_by_user),
            "known_egress_destinations_by_asset": _sorted_map(self.known_egress_destinations_by_asset),
            "known_egress_ports_by_asset": _sorted_int_map(self.known_egress_ports_by_asset),
        }

    def is_new_country(self, user: Any, country: Any) -> bool:
        return _is_new_value(self.known_countries_by_user, user, country)

    def is_new_asn(self, user: Any, asn: Any) -> bool:
        return _is_new_value(self.known_asns_by_user, user, asn)

    def is_new_device(self, user: Any, device: Any) -> bool:
        return _is_new_value(self.known_devices_by_user, user, device)

    def is_unusual_login_hour(self, user: Any, value: datetime | None) -> bool:
        key = _norm(user)
        if not key or value is None or key not in self.known_login_hours_by_user:
            return False
        hours = self.known_login_hours_by_user[key]
        return bool(hours) and value.hour not in hours

    def is_rare_egress_destination(self, asset: Any, destination: Any) -> bool:
        return _is_new_value(self.known_egress_destinations_by_asset, asset, destination)

    def is_rare_egress_port(self, asset: Any, port: Any) -> bool:
        key = _norm(asset)
        if not key or key not in self.known_egress_ports_by_asset:
            return False
        try:
            value = int(port)
        except (TypeError, ValueError):
            return False
        known = self.known_egress_ports_by_asset[key]
        return bool(known) and value not in known


def ensure_baselines(value: SecurityBaselines | dict[str, Any] | None) -> SecurityBaselines:
    if isinstance(value, SecurityBaselines):
        return value
    if isinstance(value, dict):
        return SecurityBaselines.from_dict(value)
    return SecurityBaselines()


def _is_new_value(mapping: dict[str, set[str]], entity: Any, value: Any) -> bool:
    key = _norm(entity)
    clean = _norm(value)
    if not key or not clean or key not in mapping:
        return False
    known = mapping[key]
    return bool(known) and clean not in known


def _set_map(payload: Any) -> dict[str, set[str]]:
    if not isinstance(payload, dict):
        return {}
    return {_norm(key): _as_set(value) for key, value in payload.items() if _norm(key)}


def _int_set_map(payload: Any) -> dict[str, set[int]]:
    if not isinstance(payload, dict):
        return {}
    result: dict[str, set[int]] = {}
    for key, values in payload.items():
        clean_key = _norm(key)
        if not clean_key:
            continue
        result[clean_key] = _as_int_set(values)
    return result


def _as_int_set(values: Any) -> set[int]:
    result: set[int] = set()
    raw_values = values if isinstance(values, Iterable) and not isinstance(values, str) else [values]
    for item in raw_values:
        try:
            result.add(int(item))
        except (TypeError, ValueError):
            continue
    return result


def _sorted_map(payload: dict[str, set[str]]) -> dict[str, list[str]]:
    return {key: sorted(values) for key, values in sorted(payload.items())}


def _sorted_int_map(payload: dict[str, set[int]]) -> dict[str, list[int]]:
    return {key: sorted(values) for key, values in sorted(payload.items())}


__all__ = ["SecurityBaselines", "ensure_baselines"]
