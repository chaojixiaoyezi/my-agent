# LLM: 只投影当前owner及host ACL准入的公开节点字段；经公开元数据读口剥离凭据字段，不取用、不解密、不暴露密文或哈希，不执行推送或DNS。
# 模块用途: 提供远端别名的按需目录及work复用摘要，受限上下文在读库前返回空集。
from __future__ import annotations

import re

from .self_map_registry import SectionProjection, map_item, next_read


# LLM: 子代理与三类隔离scope均不得沿共享home查询父级节点；不用管理员资格代替范围检查。
# 函数用途: 统一判断owner级渐进目录是否必须隐藏。
def restricted_owner_scope(context):
    return context.child or context.context_scope in {"isolated", "task_local", "control_plane"}


# LLM: 沿公开元数据专用读口获取已剥离凭据键的节点字典；owner同名优先，host须严格enabled和caller ACL，不取用、不解密或暴露密文。
# 函数用途: 枚举可见节点别名，坏来源保留partial，不把读取失败当完整空库。
def remote_peers_projection(context):
    if restricted_owner_scope(context):
        return SectionProjection((), "complete", source="restricted child/isolated scope")
    from ..gateway_parts.remote_push_service import (
        RemotePushError,
        read_public_push_endpoints,
    )

    home = context.agent.home_paths
    items, seen, errors = [], set(), []
    sources = ((home.owner_home_dir / "config", "remote_endpoints.json", "owner"),
               (home.root / "remote_push", "endpoints.json", "host"))
    for root, filename, scope in sources:
        try:
            rows = read_public_push_endpoints(root, filename)
            for ref, row in rows.items():
                if not isinstance(ref, str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", ref) is None or ref in seen:
                    continue
                if scope == "owner":
                    seen.add(ref)
                enabled = row.get("enabled") is True
                if scope == "host":
                    callers = row.get("allowed_callers", ["local/main"])
                    if not enabled or not isinstance(callers, list) or home.owner_id not in callers:
                        continue
                items.append(_peer_item(ref, row, scope, enabled))
                seen.add(ref)
        except FileNotFoundError:
            continue
        except (OSError, ValueError, TypeError, RuntimeError, RemotePushError):
            errors.append("SELF_MAP_REMOTE_PEER_SOURCE_UNAVAILABLE")
    return SectionProjection(tuple(items), "partial" if errors else "complete", errors=tuple(sorted(set(errors))),
                             source="remote endpoint public metadata / current owner ACL", count_known=not errors)


# LLM: URL仅作原语法校验，不解析DNS；只列公开私网许可，绝不复制整条原记录或未知字段。
# 函数用途: 构造固定公开字段的节点条目，入口仍回到精确self_map节点。
def _peer_item(ref, row, scope, enabled):
    from .remote_agent_push_tool import validate_push_url

    url = validate_push_url(row.get("endpoint_url"))
    hosts = row.get("allowed_private_hosts", ())
    if not isinstance(hosts, (list, tuple)) or any(not isinstance(host, str) for host in hosts):
        raise ValueError("invalid endpoint public hosts")
    return map_item("peer:" + ref, ref, state="enabled" if enabled else "disabled", fields={
        "kind": "remote_peer", "peer_id": ref, "endpoint_url": url, "scope": scope, "enabled": enabled,
        "allowed_private_hosts": list(hosts), "next_read": next_read("self_map", {"section": "remote_peers", "item": "peer:" + ref})})
