"""Owner 授权的内网/私网主机白名单存储(跨机监控 N1 接线的数据层)。

一条授权 = 一个 JSON 文件,落在 `<owner_home>/network_grants/`(与 temporary_grants 同款
file-per-grant 惯用法)。写入方 = `authorize_network_host` 工具(经属主确认);读取方 =
write_boundary 构造(每次工具调用把 active 主机灌进 `allowed_private_hosts`,network_safety
出站闸 + path_url_command 预检闸同时放行)。只放行 owner 明确授权过的主机;metadata /
link-local 等「永久拦截」段在闸内优先级更高,写进来也不放行(授权工具在 grant 时就拒绝)。

授权发生在 run 中途(用户任务点名内网目标 → 主代理确认后落 grant → 同轮子代理立即生效),
所以读取必须新鲜读盘,不能走 init 期缓存——grant 文件只有几个小 JSON,逐次读为微秒级。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from ..common.json_io import read_json_object_report

_SCHEMA_VERSION = "owner-network-host-grant.v1"
_DIR_NAME = "network_grants"


@dataclass(frozen=True)
class NetworkHostGrant:
    grant_id: str
    path: Path
    status: str
    host: str
    reason: str
    granted_by: str
    source_run_id: str
    expires_at: str
    created_at: str = ""


@dataclass(frozen=True)
class CreateNetworkHostGrant:
    host: str
    reason: str
    granted_by: str = ""
    source_run_id: str = ""
    expires_at: str = ""


def network_grants_dir(owner_home: Path) -> Path:
    return Path(owner_home) / _DIR_NAME


def normalized_grant_host(value: object) -> str:
    """把模型给的目标(裸 host / host:port / 完整 URL / [v6]:port)归一成闸用的小写主机名。
    解析不出主机 → 空串。归一规则对齐 network_safety 闸(小写、去方括号、去尾点)。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" in text:
        host = urlsplit(text).hostname
    else:
        # urlsplit("//host:port") 复用标准库的 host:port / [v6]:port 拆分,不自己数冒号
        try:
            host = urlsplit(f"//{text}").hostname
        except ValueError:
            return ""
    return str(host or "").lower().strip("[]").rstrip(".")


def create_network_host_grant(owner_home: Path, request: CreateNetworkHostGrant) -> NetworkHostGrant:
    """落一条主机授权(同主机已有 active 授权则幂等返回既有,不堆重复文件)。"""
    normalized = normalized_grant_host(request.host)
    if not normalized:
        raise ValueError(f"无法从 {request.host!r} 解析出主机名")
    existing = [grant for grant in list_network_host_grants(owner_home, status="active") if grant.host == normalized]
    if existing:
        return existing[0]
    grant_id = f"nethost_{uuid4().hex[:12]}"
    path = network_grants_dir(owner_home) / f"{grant_id}.json"
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "grant_id": grant_id,
        "status": "active",
        "host": normalized,
        "reason": str(request.reason or "").strip(),
        "granted_by": str(request.granted_by or "").strip(),
        "source_run_id": str(request.source_run_id or "").strip(),
        "expires_at": str(request.expires_at or "").strip(),
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_payload(path, payload)
    return _grant_from_payload(path, payload)


def list_network_host_grants(owner_home: Path, *, status: str = "") -> list[NetworkHostGrant]:
    wanted = str(status or "").strip()
    grants: list[NetworkHostGrant] = []
    directory = network_grants_dir(owner_home)
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError:
        return grants
    for path in paths:
        report = read_json_object_report(path, context="network_grants.read")
        if report.load_error is not None:
            continue
        grant = _grant_from_payload(path, report.payload)
        if not wanted or grant.status == wanted:
            grants.append(grant)
    return grants


def revoke_network_host_grant(owner_home: Path, host: str) -> list[NetworkHostGrant]:
    """吊销该主机的全部 active 授权,返回被吊销列表(无匹配 → 空列表)。"""
    normalized = normalized_grant_host(host)
    revoked: list[NetworkHostGrant] = []
    if not normalized:
        return revoked
    for grant in list_network_host_grants(owner_home, status="active"):
        if grant.host != normalized:
            continue
        report = read_json_object_report(grant.path, context="network_grants.revoke")
        if report.load_error is not None:
            continue
        payload = report.payload
        payload["status"] = "revoked"
        payload["updated_at"] = _now_iso()
        _write_payload(grant.path, payload)
        revoked.append(_grant_from_payload(grant.path, payload))
    return revoked


def active_private_hosts(owner_home: Path, *, now: str | None = None) -> tuple[str, ...]:
    """当前生效(active 且未过期)的授权主机,供 write_boundary → 出站闸消费。绝不抛异常
    (授权目录坏了不能拖垮工具执行,坏文件按无授权处理=安全侧默认)。"""
    try:
        current = _parse_time(now or _now_iso())
        grants = list_network_host_grants(owner_home, status="active")
        live = [grant.host for grant in grants if grant.host and not _grant_expired(grant, current)]
        return tuple(sorted(dict.fromkeys(live)))
    except Exception:  # noqa: BLE001 - 授权读取失败按「无授权」处理,不拦真正的工具执行
        return ()


def _grant_expired(grant: NetworkHostGrant, current: datetime | None) -> bool:
    expires = _parse_time(grant.expires_at)
    return expires is not None and current is not None and expires <= current


def _grant_from_payload(path: Path, payload: dict[str, Any]) -> NetworkHostGrant:
    return NetworkHostGrant(
        grant_id=str(payload.get("grant_id") or path.stem),
        path=path,
        status=str(payload.get("status") or "active"),
        host=str(payload.get("host") or ""),
        reason=str(payload.get("reason") or ""),
        granted_by=str(payload.get("granted_by") or ""),
        source_run_id=str(payload.get("source_run_id") or ""),
        expires_at=str(payload.get("expires_at") or ""),
        created_at=str(payload.get("created_at") or ""),
    )


def _write_payload(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "CreateNetworkHostGrant",
    "NetworkHostGrant",
    "active_private_hosts",
    "create_network_host_grant",
    "list_network_host_grants",
    "network_grants_dir",
    "normalized_grant_host",
    "revoke_network_host_grant",
]
