"""飞书用户资料:按 open_id 查显示名,首聊时种进 USER.md 的"称呼"(全程 fail-open)。

用于"新飞书用户首次私聊→自动用其飞书姓名称呼"。任何失败(无通讯录权限/网络/解析)都返回 None/不写,
绝不影响主流程;无权限时"称呼"留空,由 agent 自然询问或不带称呼。纯 stdlib urllib,无新依赖。
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..capability.persona_repository import PersonaRepository

_OPEN_BASE = {"feishu": "https://open.feishu.cn", "lark": "https://open.larksuite.com"}
_TIMEOUT_S = 8.0  # 查名网络超时(短,fail-open;不进首聊热路径慢太久)
# 匹配 USER.md 里"称呼"为空的行(- 称呼:  后面没填),半/全角冒号都认
_CALL_NAME_RE = re.compile(r"^([ \t]*-[ \t]*称呼[:：])[ \t]*$", re.MULTILINE)


def fetch_feishu_display_name(
    app_id: str, app_secret: str, open_id: str, domain: str = "feishu"
) -> str | None:
    """按 open_id 查飞书/Lark 显示名;任何失败(无权限/网络/解析)返回 None(fail-open)。"""
    if not (app_id and app_secret and open_id):
        return None
    base = _OPEN_BASE.get(domain, _OPEN_BASE["feishu"])
    try:
        tok_req = urllib.request.Request(
            base + "/open-apis/auth/v3/tenant_access_token/internal",
            data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(tok_req, timeout=_TIMEOUT_S) as resp:
            token = json.loads(resp.read()).get("tenant_access_token")
        if not token:
            return None
        url = (
            base + f"/open-apis/contact/v3/users/{urllib.parse.quote(open_id)}?user_id_type=open_id"
        )
        info_req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {token}"}, method="GET"
        )
        with urllib.request.urlopen(info_req, timeout=_TIMEOUT_S) as resp:
            data = json.loads(resp.read())
        if data.get("code") != 0:
            return None  # 无权限/无此人:code!=0
        name = ((data.get("data") or {}).get("user") or {}).get("name")
        return str(name).strip() or None if name else None
    except Exception:
        return None


def call_name_is_empty(user_md_path: str | Path) -> bool:
    """USER.md 的"称呼"行是否存在且为空(没填)。读不到/无该行→False(不去乱写)。"""
    try:
        text = Path(user_md_path).read_text(encoding="utf-8")
    except OSError:
        return False
    return bool(_CALL_NAME_RE.search(text))


def seed_call_name(
    user_md_path: str | Path,
    name: str,
    *,
    repository: PersonaRepository | None = None,
) -> bool:
    """把显示名填进 USER.md 空的"称呼"行;已填/无该行→不动(幂等)。成功写回返回 True。"""
    try:
        path = Path(user_md_path)
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    match = _CALL_NAME_RE.search(text)
    if not match:
        return False
    safe = str(name).strip().replace("\n", " ")[:64]
    if not safe:
        return False
    try:
        from ..capability.persona_repository import (
            PersonaMutationRequest,
            PersonaRepository,
            persona_entry_id,
        )

        repo = repository or PersonaRepository(
            owner_home=path.parent,
            soul_path=path.parent / "SOUL.md",
            user_path=path,
            agents_path=path.parent / "AGENTS.md",
        )
        result = repo.mutate(
            PersonaMutationRequest(
                target="user",
                action="replace",
                content=f"称呼: {safe}",
                entry_id=persona_entry_id("user", "称呼:"),
                confirmed=True,
                source="feishu_profile",
            )
        )
    except (OSError, RuntimeError, ValueError):
        return False
    return bool(result.get("changed"))


__all__ = ["fetch_feishu_display_name", "call_name_is_empty", "seed_call_name"]
