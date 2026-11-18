"""飞书设备授权协议。仅向固定官方端点发认证请求，原任务输入负责调度、持久化与唤醒。"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request

from ..common.authenticated_http import open_authenticated_request

_ACCOUNTS = 'https://accounts.feishu.cn'
_DEVICE_GRANT = 'urn:ietf:params:oauth:grant-type:device_code'


class DeviceOAuthError(ValueError):
    def __init__(self, http_status: int, code: str):
        self.http_status = http_status
        self.code = code
        super().__init__(f'飞书设备授权接口 HTTP {http_status}，原因 {code}。')


@dataclass(frozen=True, repr=False)
class DeviceGrant:
    code: str
    url: str
    user_code: str
    expires: int
    interval: int


def _post(path, fields, *, basic='', empty_response_ok=False):
    headers = {'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'}
    if basic:
        headers['Authorization'] = 'Basic ' + basic
    req = Request(_ACCOUNTS + path, data=urlencode(fields).encode(), headers=headers)
    try:
        try:
            response = open_authenticated_request(req, timeout=8)
        except HTTPError as error:
            response = error
        with response as stream:
            status = stream.status
            raw = stream.read(65537)
    except (OSError, URLError):
        raise DeviceOAuthError(0, 'transport_unavailable') from None
    if empty_response_ok and 200 <= status < 300 and not raw:
        return {}
    try:
        data = json.loads(raw) if len(raw) <= 65536 else None
    except (UnicodeError, ValueError):
        data = None
    if not isinstance(data, dict):
        raise DeviceOAuthError(status, 'invalid_response')
    if not 200 <= status < 300 or data.get('error') or data.get('code', 0) != 0:
        code = str(data.get('error') or data.get('code') or 'request_failed')
        raise DeviceOAuthError(status, code if re.fullmatch(r'[a-z0-9_]{1,64}', code) else 'invalid_response')
    return data


def start_device_authorization(app_id, secret, scopes):
    basic = base64.b64encode(f'{app_id}:{secret}'.encode()).decode('ascii')
    result = _post('/oauth/v1/device_authorization', {'client_id': app_id, 'scope': ' '.join(scopes)}, basic=basic)
    link = result.get('verification_uri_complete') or result.get('verification_uri')
    parts = urlsplit(link) if isinstance(link, str) and len(link) <= 4096 else None
    code, user_code = result.get('device_code'), result.get('user_code')
    ttl, interval = result.get('expires_in'), result.get('interval', 5)
    if (not parts or parts.scheme != 'https' or parts.netloc != 'accounts.feishu.cn'
            or not isinstance(code, str) or not 1 <= len(code) <= 8192 or any(c.isspace() for c in code)
            or not isinstance(user_code, str) or not re.fullmatch(r'[A-Za-z0-9 -]{1,64}', user_code)
            or type(ttl) is not int or not 1 <= ttl <= 1800
            or type(interval) is not int or not 1 <= interval <= ttl):
        raise DeviceOAuthError(200, 'invalid_device_grant')
    return DeviceGrant(code, link, user_code, ttl, interval)


def exchange_device_token(app_id, secret, device_code):
    return _post('/oauth/v3/token', {'grant_type': _DEVICE_GRANT, 'client_id': app_id,
                                     'client_secret': secret, 'device_code': device_code})


def refresh_device_token(app_id, secret, refresh_token):
    return _post('/oauth/v3/token', {'grant_type': 'refresh_token', 'client_id': app_id,
                                     'client_secret': secret, 'refresh_token': refresh_token})


def revoke_device_token(app_id, secret, token, token_type):
    """飞书撤销成功可返回空正文。账户、目的地址和 token 均只由宿主提供。"""
    _post('/oauth/v1/revoke', {'client_id': app_id, 'client_secret': secret,
                              'token': token, 'token_type_hint': token_type}, empty_response_ok=True)
