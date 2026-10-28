# LLM: 用户资料和 token 均只访问原固定 Feishu/Lark 地址，拒绝认证跳转；失败仍不改变聊天权限或主链。
# 模块用途: 读取官方显示名并沿原 owner 人格工具种子保存，网络错误不输出认证或正文。
"""飞书用户资料:按 open_id 查显示名,首聊时种进 USER.md 的"称呼"(全程 fail-open)。

用于"新飞书用户首次私聊→自动用其飞书姓名称呼"。任何失败(无通讯录权限/网络/解析)都返回 None/不写,
绝不影响主流程;无权限时"称呼"留空,由 agent 自然询问或不带称呼。纯 stdlib urllib,无新依赖。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from ..common.authenticated_http import open_authenticated_request
from ..common.nofollow_fs import read_text_beneath

if TYPE_CHECKING:
    from ..capability.persona_repository import PersonaRepository

_OPEN_BASE = {"feishu": "https://open.feishu.cn", "lark": "https://open.larksuite.com"}
# 查名网络超时 8 秒：短超时 fail-open，不让资料读取拖慢首聊热路径。
_FEISHU_PROFILE_TIMEOUT_SECONDS = 8.0
# 匹配 USER.md 里"称呼"为空的行(- 称呼:  后面没填),半/全角冒号都认
_CALL_NAME_RE = re.compile(r"^([ \t]*-[ \t]*称呼[:：])[ \t]*$", re.MULTILINE)
# 飞书远端身份查询结果的缓存有效秒数。
_IDENTITY_TTL_SECONDS = 1800
_IDENTITY_CACHE = {}
_IDENTITY_LOCK = threading.Lock()
_IDENTITY_FLIGHTS = {}


# LLM: 凭据仅访问原Feishu/Lark官方目标并拒绝跳转；显示名不赋权，网络失败不记录正文。
# 函数用途: 为首聊获取显示名，缺权限/网络失败仍不阻塞普通对话。
# LLM: 只提取官方用户端点的有界字符串名称，畸形对象不能str化回显其它资料；失败不抛至调用链。
# 函数用途: 用可信飞书认证读取用户显示名。
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
        with open_authenticated_request(tok_req, timeout=_FEISHU_PROFILE_TIMEOUT_SECONDS) as resp:
            token = json.loads(resp.read()).get("tenant_access_token")
        if not token:
            return None
        url = (
            base + f"/open-apis/contact/v3/users/{urllib.parse.quote(open_id)}?user_id_type=open_id"
        )
        info_req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {token}"}, method="GET"
        )
        with open_authenticated_request(info_req, timeout=_FEISHU_PROFILE_TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read())
        if data.get("code") != 0:
            return None  # 无权限/无此人:code!=0
        name = ((data.get("data") or {}).get("user") or {}).get("name")
        return _identity_name(name)
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
        raw_path = Path(user_md_path)
        if raw_path.is_symlink() or raw_path.parent.is_symlink():
            return False
        path = raw_path.parent.resolve(strict=True) / raw_path.name
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


# LLM: 只请求官方固定认证/群端点且不跟随跳转；只抽取群名，失败不回显响应或认证信息。
# 函数用途: 查询飞书/Lark群显示名。
def fetch_feishu_chat_name(app_id, app_secret, chat_id, domain='feishu'):
    if not app_id or not app_secret or domain not in _OPEN_BASE:
        return None
    try:
        base = _OPEN_BASE[domain]
        request = urllib.request.Request(base + '/open-apis/auth/v3/tenant_access_token/internal',
            data=json.dumps({'app_id': app_id, 'app_secret': app_secret}).encode(),
            headers={'Content-Type': 'application/json'}, method='POST')
        with open_authenticated_request(request, timeout=_FEISHU_PROFILE_TIMEOUT_SECONDS) as response:
            token = json.loads(response.read()).get('tenant_access_token')
        if not isinstance(token, str) or not token:
            return None
        request = urllib.request.Request(base + '/open-apis/im/v1/chats/' + urllib.parse.quote(chat_id, safe=''),
            headers={'Authorization': f'Bearer {token}'}, method='GET')
        with open_authenticated_request(request, timeout=_FEISHU_PROFILE_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read())
        return _identity_name((data.get('data') or {}).get('name')) if data.get('code') == 0 else None
    except Exception:
        return None


# LLM: 名称仅允许有界单行字符串，不把上游对象str化而泄露其它个人资料。
# 函数用途: 统一筛选本地和飞书显示名。
def _identity_name(value):
    if not isinstance(value, str) or len(value) > 256 or any(ord(char) < 32 for char in value):
        return None
    return value.strip() or None


# LLM: 默认身份只依据当前可信Owner路径，不扫描其他用户；无规范身份时不冒充local/main。
# 函数用途: 从宿主规范目录取得当前用户或群的公开标识。
def _identity_owner_id(home):
    parts = home.parts
    if len(parts) >= 3 and parts[-3:-1] == ('owners', 'local'):
        return 'local/' + home.name
    if len(parts) >= 5 and parts[-5:-3] == ('owners', 'providers') and parts[-2] in ('users', 'groups'):
        return '/'.join(parts[-4:])
    return ''


# LLM: 本人USER.md称呼不用于其它目标；nofollow只读本人文件，拒绝Markdown标题，缺失/损坏保持未解析。
# 函数用途: 为本地默认本人身份读取已登记显示名。
def _identity_local_name(home):
    try:
        text = read_text_beneath(home, ('USER.md',)) or ''
    except (OSError, ValueError):
        return None
    match = re.search(r'^[ \t]*-?[ \t]*(?:称呼|显示名|display_name)[ \t]*[:：][ \t]*([^\r\n]+)', text, re.MULTILINE)
    value = _identity_name(match[1]) if match else None
    return None if value and value.startswith('#') else value


# LLM: 名称缓存按Owner/认证指纹/精确目标隔离；网络锁仅每目标持有，不跨目标串行，缓存和飞行表有独立短锁。
# 函数用途: 取得一个去重的飞书显示名并缓存至少30分钟。
def _identity_remote_name(home, target, kind, credentials):
    domain = credentials.get('domain', 'feishu')
    if domain not in _OPEN_BASE or not all(isinstance(credentials.get(key), str) and credentials[key] for key in ('app_id', 'app_secret')):
        return None
    fingerprint = hashlib.sha256(json.dumps({key: credentials.get(key, 'feishu')
        for key in ('app_id', 'app_secret', 'domain')}, sort_keys=True).encode()).hexdigest()
    key = (str(home), fingerprint, kind, target)
    with _IDENTITY_LOCK:
        lock, users = _IDENTITY_FLIGHTS.get(key, (threading.Lock(), 0))
        _IDENTITY_FLIGHTS[key] = lock, users + 1
    try:
        with lock:
            with _IDENTITY_LOCK:
                cached = _IDENTITY_CACHE.get(key)
                if cached and cached[0] > time.monotonic():
                    return cached[1]
            try:
                fetch = fetch_feishu_chat_name if kind == 'chat' else fetch_feishu_display_name
                name = _identity_name(fetch(credentials['app_id'], credentials['app_secret'], target, domain))
            except Exception:
                name = None
            with _IDENTITY_LOCK:
                if len(_IDENTITY_CACHE) >= 1024:
                    _IDENTITY_CACHE.pop(next(iter(_IDENTITY_CACHE)))
                _IDENTITY_CACHE[key] = time.monotonic() + _IDENTITY_TTL_SECONDS, name
            return name
    finally:
        with _IDENTITY_LOCK:
            _, users = _IDENTITY_FLIGHTS[key]
            if users == 1:
                del _IDENTITY_FLIGHTS[key]
            else:
                _IDENTITY_FLIGHTS[key] = lock, users - 1


# LLM: 输出固定六字段，本人实体类型来自可信Owner路径；名称仅展示，官方格式才进远端，失败稳定fail-open。
# 函数用途: 解析一个本人或飞书用户/群的显示名。
def _resolve_identity_item(home, value, kind, credentials):
    owner = _identity_owner_id(home)
    value = value or owner
    wire = value.rsplit('/', 1)[-1] if value.startswith('providers/') else value.removeprefix('feishu:').removeprefix('lark:')
    kind = ('chat' if wire.startswith('oc_') or '/groups/' in value else 'user') if kind == 'auto' else kind
    result = {'id': value, 'id_type': kind, 'source': 'local', 'resolved': False, 'display_name': None, 'reason': 'not_found'}
    if kind not in ('user', 'chat') or len(value) > 256:
        return {**result, 'reason': 'invalid_target'}
    if owner and value in (owner, home.name):
        if kind != ('chat' if '/groups/' in owner else 'user'):
            return {**result, 'reason': 'invalid_target'}
        name = _identity_local_name(home)
        if name:
            return {**result, 'resolved': True, 'display_name': name, 'reason': 'local_profile'}
    expected = 'oc_' if kind == 'chat' else 'ou_'
    if wire.startswith(expected) and re.fullmatch(r'(?:ou|oc)_[A-Za-z0-9_-]{1,128}', wire):
        name = _identity_remote_name(home, wire, kind, credentials)
        return {**result, 'source': 'feishu', 'resolved': name is not None,
                'display_name': name, 'reason': 'resolved' if name else 'unavailable'}
    return result


# LLM: 仅宿主可传认证映射，不读或回显秘密；批量有界、单项失败不打断其它项，不写USER.md或身份库。
# 函数用途: 按单目标/批量请求解析当前Owner可展示的用户或群名称。
def resolve_user_space_identity(owner_home: str | Path, *, targets: Sequence[Mapping] | None = None,
                                id_value: str = '', id_type: str = 'auto', feishu_credentials: Mapping | None = None):
    home = Path(owner_home).absolute()
    credentials = feishu_credentials if isinstance(feishu_credentials, Mapping) else {}
    if targets is None:
        return _resolve_identity_item(home, id_value if isinstance(id_value, str) else '', id_type, credentials)
    if not isinstance(targets, (list, tuple)) or len(targets) > 64:
        raise ValueError('targets必须为不超过64项的数组')
    items = []
    for target in targets:
        if not isinstance(target, Mapping) or not isinstance(target.get('id'), str) or not target['id']:
            items.append({'id': '', 'id_type': 'user', 'source': 'local', 'resolved': False, 'display_name': None, 'reason': 'invalid_target'})
        else:
            items.append(_resolve_identity_item(home, target['id'], target.get('id_type', 'auto'), credentials))
    return {'items': items}


__all__ = ['fetch_feishu_display_name', 'fetch_feishu_chat_name', 'resolve_user_space_identity', 'call_name_is_empty', 'seed_call_name']
