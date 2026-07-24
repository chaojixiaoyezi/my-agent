# LLM: authorize_network_host 工具——把【用户明确指定的】内网/私网目标主机加入 owner 出站白名单,
#   解除 NETWORK_PRIVATE_HOST_BLOCKED(跨机监控数据源等合法内网访问的授权通路)。设计原则:
#   不放松 network_safety 闸本身,只把「属主确认过的主机」灌进闸已内置的 allowed_private_hosts;
#   云 metadata / link-local 永久拦截段闸内优先级更高,grant 时就拒绝。确认闸沿用 update_persona
#   的 confirmed 先例(APPROVAL_REQUIRED 打回,让模型先取得用户明确指定/同意)。
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from ..contracts.gates.network_safety import always_blocked_host
from ..tooling.models import BaseTool, ToolExecutionResult, ToolSpec
from ..user_space.network_grants import (
    CreateNetworkHostGrant,
    create_network_host_grant,
    list_network_host_grants,
    normalized_grant_host,
    revoke_network_host_grant,
)

if TYPE_CHECKING:
    from ..core import SimpleAgent

_TOOL_NAME = "authorize_network_host"


def build_authorize_network_host_spec() -> ToolSpec:
    return ToolSpec(
        name=_TOOL_NAME,
        category="capability",
        effect="mutating",
        idempotency_scope="operation",
        description=(
            "把用户明确指定的内网/私网目标主机加入出站访问白名单,解除 NETWORK_PRIVATE_HOST_BLOCKED。"
            "授权按主机粒度、写进 owner 的授权存储,对你和你的全部子代理的 web_fetch **立即生效**"
            "(其余内网地址仍默认拦截;云 metadata/link-local 永久拦截段不可授权)。"
            "**这是出站安全边界,只有【用户明确指定过要访问/监控该内网地址】(任务里点名了目标,"
            "或你问过用户且用户同意)才能授权——满足后带 confirmed=true 调用;来路不明的内网地址先问用户。**"
        ),
        use_cases=[
            "用户任务点名要监控/访问某内网地址(如 192.168.x.x:端口 的数据源接口)→ 派子代理前先授权,子代理才够得着",
            "web_fetch 撞 NETWORK_PRIVATE_HOST_BLOCKED、且目标正是用户指定的内网端点 → 授权后重试",
            "子代理提交了 capability_type=network 的能力申请 → 先把主机授权进白名单,再 grant 工具",
        ],
        avoid_when=[
            "用户从没提过这个内网地址(来路不明)→ 先在回复里问用户,别自作主张开口子",
            "目标是公网地址 → 出站本来就放行,不需要授权",
            "目标是云 metadata / link-local(169.254.x.x)凭证端点 → 永久拦截,授权也无效",
        ],
        keywords=["内网", "私网", "局域网", "192.168", "NETWORK_PRIVATE_HOST_BLOCKED", "白名单", "授权", "数据源", "监控目标"],
        parameters={
            "action": "grant(默认,授权)/ revoke(吊销)/ list(查看当前授权)。",
            "hosts": "grant/revoke 必填。目标主机,可传裸主机、host:端口 或完整 URL(只取主机部分);字符串或数组。",
            "reason": "grant 必填。为什么要访问(引用用户的原始要求,审计用)。",
            "confirmed": "grant 必填=true,且只能在【用户明确指定过该内网目标】(任务点名/问过用户)之后才置 true。",
        },
        parameter_schema={
            "action": {"type": "string", "enum": ["grant", "revoke", "list"]},
            "hosts": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string"},
            "confirmed": {"type": "boolean"},
        },
        examples=[
            '{"tool":"authorize_network_host","action":"grant","hosts":["192.168.1.50"],"reason":"用户要求监控 http://192.168.1.50:8901/pull 数据源","confirmed":true}',
            '{"tool":"authorize_network_host","action":"list"}',
        ],
    )


class AuthorizeNetworkHostTool(BaseTool):
    # 类用途: 把「owner 确认过的内网主机 → 出站白名单」暴露成模型工具;落盘 owner 的 network_grants
    #   授权存储,write_boundary 构造每次工具调用新鲜读取 → network_safety/path_url_command 两道闸放行。
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_authorize_network_host_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        action = str(params.get("action") or "grant").strip().lower()
        owner_home = self._owner_home()
        if owner_home is None:
            return _err("无 owner home 上下文,网络授权存储不可用", "TOOL_UNAVAILABLE")
        if action == "list":
            return self._list(owner_home)
        hosts, invalid = _normalized_hosts(params.get("hosts"))
        if invalid:
            return _err(f"这些值解析不出主机名: {invalid}(可传裸主机、host:端口 或完整 URL)", "TOOL_INVALID_ARGUMENTS")
        if not hosts:
            return _err("hosts 必填:要授权/吊销的内网主机(字符串或数组)", "TOOL_INVALID_ARGUMENTS")
        if action == "revoke":
            return self._revoke(owner_home, hosts)
        if action != "grant":
            return _err("action 须为 grant/revoke/list", "TOOL_INVALID_ARGUMENTS")
        return self._grant(owner_home, hosts, params)

    def _grant(self, owner_home: Path, hosts: list[str], params: dict[str, object]) -> ToolExecutionResult:
        blocked = [host for host in hosts if always_blocked_host(host)]
        if blocked:
            return _err(
                f"这些主机属于云 metadata/link-local 永久拦截段,不可授权: {blocked}",
                "NETWORK_ALWAYS_BLOCKED_HOST",
                hint="这类凭证端点无论授权与否都不放行;确认你要的是用户指定的业务内网地址。",
            )
        reason = str(params.get("reason") or "").strip()
        if not reason:
            return _err("reason 必填:为什么要访问该内网目标(引用用户的原始要求,审计用)", "TOOL_INVALID_ARGUMENTS")
        if not bool(params.get("confirmed")):
            return _err(
                "内网主机白名单是出站安全边界,不能自动开。只有【用户明确指定过要访问/监控该内网地址】"
                "(任务里点名了目标,或你问过用户且用户明确同意)才算确认;满足后带 confirmed=true 重调。"
                "用户没提过这个地址就先在回复里问,别自作主张。",
                "APPROVAL_REQUIRED",
            )
        granted = [
            create_network_host_grant(
                owner_home,
                CreateNetworkHostGrant(
                    host=host,
                    reason=reason,
                    granted_by=self._owner_id(),
                    source_run_id=self._run_id(),
                ),
            )
            for host in hosts
        ]
        payload = {
            "ok": True,
            "granted": [{"host": grant.host, "grant_id": grant.grant_id} for grant in granted],
            "hint": "立即生效:本轮后续 web_fetch 与所有子代理对这些主机放行(仅这些主机,其余内网仍默认拦截)。",
        }
        return ToolExecutionResult(_TOOL_NAME, True, json.dumps(payload, ensure_ascii=False))

    def _revoke(self, owner_home: Path, hosts: list[str]) -> ToolExecutionResult:
        revoked = [grant.host for host in hosts for grant in revoke_network_host_grant(owner_home, host)]
        payload = {"ok": True, "revoked": revoked, "not_found": [host for host in hosts if host not in revoked]}
        return ToolExecutionResult(_TOOL_NAME, True, json.dumps(payload, ensure_ascii=False))

    def _list(self, owner_home: Path) -> ToolExecutionResult:
        rows = [
            {"host": grant.host, "status": grant.status, "reason": grant.reason, "created_at": grant.created_at}
            for grant in list_network_host_grants(owner_home)
        ]
        return ToolExecutionResult(_TOOL_NAME, True, json.dumps({"ok": True, "grants": rows}, ensure_ascii=False))

    def _owner_home(self) -> Path | None:
        raw = str(getattr(getattr(self.agent, "home_paths", None), "owner_home_dir", "") or "").strip()
        return Path(raw) if raw else None

    def _owner_id(self) -> str:
        return str(getattr(getattr(self.agent, "home_paths", None), "owner_id", "") or "")

    def _run_id(self) -> str:
        return str(getattr(getattr(self.agent, "_current_run_params", None), "run_id", "") or "")


def _normalized_hosts(value: object) -> tuple[list[str], list[str]]:
    raw_items = value if isinstance(value, (list, tuple)) else [value]
    texts = [str(item or "").strip() for item in raw_items]
    pairs = [(text, normalized_grant_host(text)) for text in texts if text]
    hosts = list(dict.fromkeys(host for _text, host in pairs if host))
    invalid = [text for text, host in pairs if not host]
    return hosts, invalid


def _err(msg: str, code: str, hint: str = "") -> ToolExecutionResult:
    body = {"error": msg}
    if hint:
        body["hint"] = hint
    return ToolExecutionResult(_TOOL_NAME, False, json.dumps(body, ensure_ascii=False), error_code=code)


__all__ = ["AuthorizeNetworkHostTool", "build_authorize_network_host_spec"]
