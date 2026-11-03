"""从原生模型、MCP、插件和用量事实发现资产；治理决定不改变执行权限。"""
from __future__ import annotations

import hashlib
import ipaddress
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from ..plugin_host.errors import HostAPIError
from .v13_w9_datasources import w9_list, w9_now, w9_page, w9_write


# LLM: 只公开 hostname；路径、查询、端口和 URL 内用户名密码永不进入投影。
# 函数用途: 提取结构化端点的主机名。
def _endpoint_host(endpoint):
    if not isinstance(endpoint, str) or not endpoint:
        return None
    try:
        parsed = urlsplit(endpoint)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return None
        return parsed.hostname.encode("idna").decode("ascii").lower()[:256]
    except (ValueError, UnicodeError):
        return None


# LLM: 不做 DNS 解析；localhost/回环地址来自字面事实，私网不等于本机。
# 函数用途: 判断一个已提取的主机名是否为本机回环。
def _loopback(host):
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# LLM: 只依据端点与显式能力算风险，不把名称、说明或审批标签当成安全保证。
# 函数用途: 按冻结规则生成可解释的风险投影。
def _risk(row, sanctioned):
    host = row.get("endpoint_host")
    if host and not _loopback(host) and not sanctioned:
        return {"level": "high", "reasons": ["external_endpoint_not_sanctioned"]}
    if row["kind"] in ("mcp_server", "agent_plugin") and row.get("network_or_exec") is True:
        return {"level": "medium", "reasons": ["declared_network_or_exec"]}
    if row["kind"] == "model_endpoint" and host and _loopback(host):
        return {"level": "low", "reasons": ["loopback_model_endpoint"]}
    return {"level": "unknown", "reasons": ["insufficient_structured_facts"]}


# LLM: 原目录 list_providers 已经脱敏；这里进一步白名单提取，不回传完整 provider 对象。
# 函数用途: 读取当前 owner 的全部原生模型档案。
def _model_rows(call):
    from ..plugin_host.model_settings_v2_handlers import ModelSettingsV2Handlers

    providers = ModelSettingsV2Handlers(call.context.host.home_paths).list_providers({"capability_filter": "all"})["providers"]
    rows = []
    for provider in providers:
        for profile in provider["models"]:
            rows.append({"native_id": profile["profile_id"], "kind": "model_endpoint",
                "name": (profile.get("name") or profile["model_name"])[:200],
                "provider": provider["provider_id"][:128], "endpoint_host": _endpoint_host(provider.get("api_base")),
                "discovered_from": "model_profiles", "first_seen_at": None, "last_seen_at": None})
    return rows


# LLM: HTTP 冷 owner 可能共享主宿主 config；仅身份一致的配置可以投影，不能泄漏主用户声明。
# 函数用途: 从同 owner 原生配置投影 MCP 声明且不启动进程。
def _mcp_rows(call):
    from ..user_space.owner_resolver import owner_identity_from_config, resolve_owner_home

    host = call.context.host
    configured_owner = resolve_owner_home(host.home_paths.root, owner_identity_from_config(host.config))
    if configured_owner.owner_id != call.owner_id:
        return [], False
    declarations = getattr(host.config, "mcp_servers", {})
    if not isinstance(declarations, dict):
        raise ValueError("无效 MCP 配置")
    rows = []
    for name, declaration in sorted(declarations.items()):
        if not isinstance(declaration, dict):
            raise ValueError("无效 MCP 声明")
        endpoint = _endpoint_host(declaration.get("url"))
        rows.append({"native_id": name, "kind": "mcp_server", "name": name[:200],
            "provider": None, "endpoint_host": endpoint, "discovered_from": "mcp_config",
            "first_seen_at": None, "last_seen_at": None,
            "network_or_exec": bool(endpoint or declaration.get("command"))})
    return rows, True


# LLM: 复用 PluginManagement.catalog 的可见目录与原安装元数据，不启用插件、不读取插件私有配置。
# 函数用途: 冷读当前 owner 可见插件及明确声明的网络/执行能力。
def _plugin_rows(call):
    from ..path_access_policy import PathAccessPolicy
    from ..plugin_management import PluginManagement, PluginManagementContext, _visible_entries
    from ..user_space.owner_resolver import OwnerIdentity, resolve_owner_home

    home = call.context.host.home_paths
    parts = call.owner_id.split("/")
    if parts[:1] == ["local"] and len(parts) == 2:
        identity = OwnerIdentity.local_main() if parts[1] == "main" else OwnerIdentity.local_user(parts[1])
    elif len(parts) == 4 and parts[0] == "providers" and parts[2] in ("users", "groups"):
        identity = OwnerIdentity(parts[1], "user" if parts[2] == "users" else "group", parts[3])
    else:
        raise HostAPIError("ACTOR_REQUIRED", 403)
    owner = resolve_owner_home(home.root, identity)
    if Path(owner.home_dir).resolve() != Path(call.owner_home).resolve():
        raise HostAPIError("ACTOR_REQUIRED", 403)
    context = PluginManagementContext(owner=owner, actor_id=call.subject_id, channel="web", conversation_id="",
        threads=None, workspace=Path(call.owner_home),
        path_policy=PathAccessPolicy.from_values(owner_scope_root=call.owner_home, agent_home_root=home.root),
        is_admin=False, enabled=False, tool_allowed=False, business_allowed=False)
    manager = PluginManagement(context)
    entries = {entry.manifest.plugin_id: entry for entry in _visible_entries(manager)}
    rows = []
    for plugin in manager.catalog().plugins:
        entry = entries.get(plugin.plugin_id)
        if entry is None:
            raise ValueError("插件目录在读取期间变更")
        manifest = entry.manifest
        network = getattr(manifest.permissions, "network", None) is True
        executable = any(name.startswith("execute.") for name in manifest.host_api)
        rows.append({"native_id": plugin.plugin_id, "kind": "agent_plugin", "name": plugin.plugin_id,
            "provider": None, "endpoint_host": None, "discovered_from": "plugins",
            "first_seen_at": None, "last_seen_at": None, "network_or_exec": network or executable})
    return rows


# LLM: decision_usage_summary 原函数只返回计数而没有服务商身份；发现必须读取同一原账的 backends 字段。
# 函数用途: 有界扫描本 owner 原用量事件，投影真实出现过的后端身份。
def _usage_rows(call):
    store = getattr(call.context.host, "conversation_store", None)
    if store is None:
        return [], False
    threads, errors = store.threads.list_report(limit=501)
    complete = not errors and len(threads) <= 500
    seen = {}
    for thread in threads[:500]:
        if thread.owner_id != call.owner_id:
            continue
        events, failures = store.model_usage.events_report(thread.thread_id)
        complete = complete and not failures
        for event in events:
            backends = event.model_calls.get("backends", [])
            if not isinstance(backends, list):
                complete = False
                continue
            for backend in backends:
                if not isinstance(backend, str) or not backend or len(backend) > 128:
                    complete = False
                    continue
                endpoint = _endpoint_host(backend)
                # URL 身份只保留主机名；普通 backend 标签不猜成 URL，也不带出凭据。
                identifier = endpoint or backend
                if "://" in backend and endpoint is None:
                    complete = False
                    continue
                at = datetime.fromtimestamp(event.created_at, timezone.utc).isoformat()
                previous = seen.get(identifier)
                seen[identifier] = {"native_id": identifier, "kind": "external_service", "name": identifier,
                    "provider": identifier, "endpoint_host": endpoint, "discovered_from": "usage",
                    "first_seen_at": min(at, previous["first_seen_at"]) if previous else at,
                    "last_seen_at": max(at, previous["last_seen_at"]) if previous else at}
    return list(seen.values()), complete


# LLM: ID 与 native identity 绑定，跨类型碰撞才加 kind；二次碰撞用完整身份摘要，不按顺序覆盖。
# 函数用途: 为一次真实来源快照分配无歧义稳定资产 ID。
def _asset_ids(rows):
    groups = {}
    for row in rows:
        native = str(row["native_id"])
        base = "ai-" + native
        if len(base.encode("utf-8")) > 128:
            base = "ai-" + hashlib.sha256(native.encode()).hexdigest()
        groups.setdefault(base, {})[(row["kind"], native)] = row
    candidates = []
    for base, identities in sorted(groups.items()):
        for identity, row in sorted(identities.items()):
            prefix = base.encode("utf-8")[:100].decode("utf-8", errors="ignore")
            candidate = base if len(identities) == 1 else prefix + "-" + row["kind"]
            candidates.append((candidate, identity, row))
    counts = {}
    for candidate, identity, row in candidates:
        counts[candidate] = counts.get(candidate, 0) + 1
    result = []
    for candidate, identity, row in candidates:
        if counts[candidate] > 1:
            candidate = "ai-" + hashlib.sha256((identity[0] + "\0" + identity[1]).encode()).hexdigest() + "-" + identity[0]
        result.append({**row, "asset_id": candidate})
    return sorted(result, key=lambda row: row["asset_id"])


# LLM: 决定只改变治理投影，不作为工具授权、原生管理员或运行时权限。
# 函数用途: 将原生资产和当前 owner 决定合成闭合公开视图。
def _view(row, decision=None):
    decision = decision or {}
    sanctioned = decision.get("sanction_state") == "sanctioned"
    return {"asset_id": row["asset_id"], "kind": row["kind"], "name": row["name"],
        "provider": row.get("provider"), "endpoint_host": row.get("endpoint_host"),
        "discovered_from": row["discovered_from"], "first_seen_at": row.get("first_seen_at"),
        "last_seen_at": row.get("last_seen_at"), "sanction_state": decision.get("sanction_state", "unreviewed"),
        "sanctioned_by": decision.get("decided_by"), "sanctioned_at": decision.get("updated_at"),
        "reason": decision.get("reason"), "risk": _risk(row, sanctioned)}


# LLM: 每次从权威源重新发现，不建影子库存；某来源缺失时明确 partial。
# 函数用途: 组合四类原生只读来源。
def _discover(call):
    mcp, mcp_complete = _mcp_rows(call)
    usage, usage_complete = _usage_rows(call)
    return _asset_ids(_model_rows(call) + mcp + _plugin_rows(call) + usage), mcp_complete and usage_complete


# LLM: 原存储游标绑定脱敏投影快照，资产变动或过滤变更不能沿用旧游标。
# 函数用途: 返回当前 owner 的 AI 资产清单与结构化风险。
def list_assets(call):
    rows, complete = _discover(call)
    decisions = {row["asset_id"]: row for row in w9_list(call, "ai_asset_decisions")}
    items = [_view(row, decisions.get(row["asset_id"])) for row in rows]
    items = [row for row in items if all(row[key] == call.params[key] for key in ("kind", "sanction_state") if key in call.params)]
    page = w9_page(call, "ai_asset_decisions", items)
    if not complete:
        page["coverage"] = "partial"
    return page


# LLM: 即使领域层被直接调用仍核原 native admin；RBAC 的角色或授权不能放宽这个门。
# 函数用途: 为已发现资产追加管理员治理决定和有界历史。
def sanction(call):
    from ..gateway_parts.business_api_v13 import v13_require_admin

    v13_require_admin(call)

    # LLM: 存在性、历史合并均在锁内完成；幂等重放不重复追加。
    # 函数用途: 保存本 owner 的最后一次决定并保留历史。
    def mutate(records):
        v13_require_admin(call)
        rows, _complete = _discover(call)
        row = next((item for item in rows if item["asset_id"] == call.params["asset_id"]), None)
        if row is None:
            raise HostAPIError("RESOURCE_NOT_FOUND", 404)
        identifier = row["asset_id"]
        previous = records.get(identifier, {})
        if previous and previous.get("owner_id") != call.owner_id:
            raise HostAPIError("RESOURCE_NOT_FOUND", 404)
        now = w9_now(call)
        event = {"sanction_state": "sanctioned" if call.params["sanctioned"] else "restricted",
            "decided_by": call.subject_id, "reason": call.params.get("reason"), "decided_at": now}
        decision = {"asset_id": identifier, "owner_id": call.owner_id,
            **{key: event[key] for key in ("sanction_state", "decided_by", "reason")},
            "history": (previous.get("history", []) + [event])[-100:],
            "revision": previous.get("revision", 0) + 1,
            "created_at": previous.get("created_at", now), "updated_at": now}
        records[identifier] = decision
        return {"asset": _view(row, decision)}

    return w9_write(call, "ai_asset_decisions", mutate)
