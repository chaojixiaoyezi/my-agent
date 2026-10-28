# LLM: 认证底座仅使用标准库；账号/session沿原权威，SSO只接受宿主注入的验签器，不信callback自报claims。
# 模块用途: 提供私有TOTP、防重放恢复码、SSO配置及有期限单次握手；不实现厂商或门户专属路由。
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import struct
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

from ..common.directory_lock import locked_private_directory
from ..common.nofollow_fs import read_bytes_beneath, write_text_atomic_beneath
from ..common.strict_json import load_strict_json
from ..plugin_host.errors import HostAPIError
from ..plugin_host.owner_context import validate_local_user_id
from .local_user_store import LocalUserStore

_MASK = '********'


# LLM: counter来自宿主时钟，不接受客户端改变周期或算法；只返回六位SHA1/RFC6238结果，无IO。
# 函数用途: 按30秒计数计算标准六位动态码，供验证和标准向量测试共用。
def totp_code(secret: str, counter: int) -> str:
    if type(counter) is not int or not 0 <= counter < 2**64:
        raise ValueError('TOTP计数无效')
    digest = hmac.digest(base64.b32decode(secret, casefold=False), struct.pack('>Q', counter), 'sha1')
    offset = digest[-1] & 15
    return f'{(struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7fffffff) % 1000000:06d}'


# LLM: 私有JSON统一nofollow读取，坏内容失败关闭；不存在返回独立空对象，不创建目录。
# 函数用途: 为MFA、配置和握手读取一个有界权威记录。
def _read(root, name):
    try:
        raw = read_bytes_beneath(root, ('local_users_auth', name), max_bytes=262144, require_dir_fd=True)
        result = load_strict_json(raw) if raw is not None else {}
        if not isinstance(result, dict):
            raise ValueError('auth state')
        return result
    except (OSError, ValueError, TypeError):
        raise HostAPIError('STORE_UNAVAILABLE', 503) from None


# LLM: 只在本模块跨进程锁内调用，原子私有写不回显secret；有界JSON防配置或状态持续膨胀。
# 函数用途: 保存一个认证记录，失败不报告验证或消费成功。
def _write(root, name, value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    if len(raw.encode()) > 262144:
        raise HostAPIError('PAYLOAD_TOO_LARGE', 413)
    write_text_atomic_beneath(root, ('local_users_auth', name), raw + '\n')


# LLM: epoch与SSO配置只进入原session指纹，零配置仍保留旧token口径；损坏状态不能当作未启用。
# 函数用途: 为唯一会话验证器提供当前认证版本，不泄露TOTP密钥或SSO秘密。
def authentication_stamp(root, uid):
    row = _read(root, validate_local_user_id(uid) + '.json')
    config = _read(root, '.sso-config.json')
    return str(row.get('auth_epoch', '')) + (':' + str(config['revision']) if config else '')


# LLM: 此结构只能来自可信构造时注入的协议验签器；JSON同形dict或verified字段不构成认证来源。
# 类用途: 运输经IdP协议验证的issuer、稳定subject、nonce和角色，不接受本地owner或管理员标记。
@dataclass(frozen=True)
class VerifiedSSOIdentity:
    issuer_url: str
    subject: str
    nonce: str
    roles: tuple[str, ...] = ()


# LLM: 构造不物化账号或连接IdP；私有写按原目录锁，session只经user_auth签发；端口不可来自callback正文。
# 类用途: 管理宿主通用MFA与SSO握手，缺少真实协议验签器时拒绝SSO登录。
class TotpSSOAuthService:
    # LLM: clock/verifier仅可信宿主注入；不接受插件指定回调或可执行路径，实例不缓存权限。
    # 函数用途: 绑定宿主根和可选IdP协议实现，默认只有配置/状态底座而没有伪造IdP验证。
    def __init__(self, host_root, *, clock=None, assertion_verifier=None):
        self.root = Path(host_root).absolute()
        self.clock = clock or time.time
        self.assertion_verifier = assertion_verifier

    # LLM: 所有MFA和握手写共一跨进程锁；持锁区不能调用外部协议验签/网络或模型。
    # 函数用途: 打开私有认证目录的短提交门，保证码和state并发最多消费一次。
    def _locked(self):
        return locked_private_directory(self.root, relative_parts=('local_users_auth',), lock_name='.auth.lock')

    # LLM: 必须是真实已启用普通账号，不创建用户，不允许main或路径选择器。
    # 函数用途: 取得安全UID并检查当前账号可认证。
    def _user(self, user_id):
        uid = validate_local_user_id(user_id)
        LocalUserStore(self.root).require_enabled(uid)
        return uid

    # LLM: 不能覆盖已启用MFA；pending十分钟过期，恢复码只以高熵哈希保存，响应secret仅用于安全交互。
    # 函数用途: 生成160位种子和8个恢复码，写私有待激活设置，不提前启用MFA。
    def setup_totp(self, user_id):
        uid = self._user(user_id)
        with self._locked():
            row = _read(self.root, uid + '.json')
            if row.get('enabled'):
                raise HostAPIError('TOTP_ALREADY_ENABLED', 409)
            secret = base64.b32encode(secrets.token_bytes(20)).decode('ascii')
            recovery = [secrets.token_hex(8) for _ in range(8)]
            _write(self.root, uid + '.json', {'enabled': False, 'secret': secret,
                'pending_until': int(self.clock()) + 600, 'last_verified_counter': -1,
                'auth_epoch': secrets.token_hex(16), 'recovery_hashes': [hashlib.sha256(code.encode()).hexdigest() for code in recovery]})
        return {'ok': True, 'user_id': uid, 'enabled': False, 'secret': secret,
            'otpauth_uri': 'otpauth://totp/' + quote('my-agent:' + uid, safe='') + '?' + urlencode({
                'secret': secret, 'issuer': 'my-agent', 'algorithm': 'SHA1', 'digits': 6, 'period': 30}),
            'recovery_codes': recovery}

    # LLM: 调用方持同一锁；常量时间比对完整窗口，成功计数须递增；恢复码不能激活pending，失败持久限速。
    # 函数用途: 原子消费动态码或单次恢复码，启用时也记录同一计数防重放。
    def _verify_locked(self, uid, code):
        row = _read(self.root, uid + '.json')
        now = int(self.clock())
        if not row.get('secret') or not row.get('enabled') and now >= row.get('pending_until', 0):
            raise HostAPIError('TOTP_NOT_CONFIGURED', 409)
        if type(code) is not str or not code.isascii() or len(code) > 64:
            raise HostAPIError('TOTP_INVALID', 401)
        if row.get('failure_until', 0) > now and row.get('failures', 0) >= 10:
            raise HostAPIError('TOTP_RATE_LIMITED', 429)
        counter = now // 30
        matches = [candidate for candidate in range(max(0, counter - 1), counter + 2)
                   if hmac.compare_digest(totp_code(row['secret'], candidate), code)] if re.fullmatch(r'[0-9]{6}', code) else []
        if matches:
            selected = max(matches)
            if selected <= row.get('last_verified_counter', -1):
                raise HostAPIError('TOTP_CODE_REPLAYED', 409)
            row['last_verified_counter'] = selected
        else:
            hashed = hashlib.sha256(code.encode('ascii')).hexdigest()
            recoveries = row.get('recovery_hashes', [])
            index = next((i for i, value in enumerate(recoveries) if hmac.compare_digest(value, hashed)), None)
            if not row.get('enabled') or index is None:
                row['failures'] = row.get('failures', 0) + 1 if row.get('failure_until', 0) > now else 1
                row['failure_until'] = now + 300
                _write(self.root, uid + '.json', row)
                raise HostAPIError('TOTP_INVALID', 401)
            row['recovery_hashes'] = recoveries[:index] + recoveries[index + 1:]
        if not row.get('enabled'):
            row['enabled'], row['auth_epoch'] = True, secrets.token_hex(16)
        row['failures'], row['failure_until'] = 0, 0
        _write(self.root, uid + '.json', row)
        return {'ok': True, 'user_id': uid, 'enabled': True}

    # LLM: 写前后均核原账号，锁内消费并持久化才返回成功；不签发session或把恢复码当第一因素。
    # 函数用途: 校验并单次消费TOTP或恢复码，首次正确动态码完成激活。
    def verify_totp(self, user_id, code):
        uid = self._user(user_id)
        with self._locked():
            result = self._verify_locked(uid, code)
            self._user(uid)
            return result

    # LLM: admin必须由宿主核当前操作者后调用，公开Host schema不允许布尔绕过；停用保留epoch以撤销旧session。
    # 函数用途: 验证有效第二因素后停用MFA，或由明确管理员执行重置。
    def disable_totp(self, user_id, code_or_admin=None, *, admin=False):
        uid = self._user(user_id)
        with self._locked():
            if admin is not True:
                self._verify_locked(uid, code_or_admin)
            _write(self.root, uid + '.json', {'enabled': False, 'auth_epoch': secrets.token_hex(16)})
        return {'ok': True, 'user_id': uid, 'enabled': False}

    # LLM: 只在密码/IdP主因素已验证后调用；没有MFA不写文件，pending不算已启用。
    # 函数用途: 在唯一session签发前检查当前MFA并消费本次第二因素。
    def require_second_factor(self, user_id, code=None):
        uid = self._user(user_id)
        if not _read(self.root, uid + '.json').get('enabled'):
            return
        with self._locked():
            if _read(self.root, uid + '.json').get('enabled'):
                if code is None:
                    raise HostAPIError('TOTP_REQUIRED', 401)
                self._verify_locked(uid, code)

    # LLM: 默认关闭；只给出secret掩码，原配置revision用于state和session撤销，不返回内部密钥或文件位置。
    # 函数用途: 读取当前通用SSO配置的公开投影。
    def get_sso_config(self):
        value = _read(self.root, '.sso-config.json')
        return {**value, 'enabled': value.get('enabled', False),
                'client_secret': _MASK if value.get('client_secret') else ''}

    # LLM: 配置由管理员入口授权；不发网络，issuer/redirect必须HTTPS，无userinfo；映射只认既有普通UID和USER角色。
    # 函数用途: 原子更新OIDC/SAML连接配置，区分secret保留、替换与清除，变更废弃旧握手和会话。
    def update_sso_config(self, patch):
        allowed = {'protocol', 'issuer_url', 'client_id', 'client_secret', 'redirect_uri',
                   'role_mapping', 'subject_mapping', 'enabled'}
        if not isinstance(patch, dict) or set(patch) - allowed:
            raise HostAPIError('INVALID_SCHEMA')
        with self._locked():
            old = _read(self.root, '.sso-config.json')
            incoming = dict(patch)
            if incoming.get('client_secret') == _MASK:
                incoming.pop('client_secret')
            value = {**old, **incoming}
            if value.get('protocol') not in ('oidc', 'saml') or type(value.get('enabled', False)) is not bool:
                raise HostAPIError('INVALID_SCHEMA')
            for key in ('issuer_url', 'redirect_uri'):
                if type(value.get(key)) is not str or len(value[key]) > 4096:
                    raise HostAPIError('INVALID_SCHEMA')
                parsed = urlsplit(value[key])
                if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                    raise HostAPIError('INVALID_SCHEMA')
            if any(type(value.get(key, '')) is not str or len(value.get(key, '')) > 8192 for key in ('client_id', 'client_secret')):
                raise HostAPIError('INVALID_SCHEMA')
            if not value.get('client_id'):
                raise HostAPIError('INVALID_SCHEMA')
            for key in ('role_mapping', 'subject_mapping'):
                values = value.setdefault(key, {})
                if (not isinstance(values, dict) or len(values) > 256
                        or any(type(k) is not str or not k or len(k) > 256 or type(v) is not str for k, v in values.items())):
                    raise HostAPIError('INVALID_SCHEMA')
            if any(role != 'USER' for role in value['role_mapping'].values()):
                raise HostAPIError('CAPABILITY_DENIED', 403)
            for uid in value['subject_mapping'].values():
                validate_local_user_id(uid)
            if {k: v for k, v in value.items() if k != 'revision'} != {k: v for k, v in old.items() if k != 'revision'}:
                value['revision'] = old.get('revision', 0) + 1
                _write(self.root, '.sso-config.json', value)
        return self.get_sso_config()

    # LLM: 只签固定用途、绑定浏览器相关量和配置revision；状态表有界，未过期记录不通过静默驱逐变为可重放。
    # 函数用途: 创建300秒state/nonce握手，不做IdP认证、不签发用户session。
    def initiate_sso_login(self, *, browser_binding):
        from .user_auth import _encode, _session_secret
        if type(browser_binding) is not str or not 16 <= len(browser_binding) <= 1024:
            raise HostAPIError('INVALID_SCHEMA')
        with self._locked():
            config = _read(self.root, '.sso-config.json')
            if config.get('enabled') is not True:
                raise HostAPIError('SSO_DISABLED', 403)
            now = int(self.clock())
            states = {key: value for key, value in _read(self.root, '.sso-states.json').items() if value['expires_at'] > now}
            if len(states) >= 256:
                raise HostAPIError('SSO_STATE_CAPACITY', 429)
            nonce, sid = secrets.token_urlsafe(32), secrets.token_hex(16)
            ticket = {'sid': sid, 'nonce': nonce, 'issued_at': now, 'expires_at': now + 300,
                'binding': hashlib.sha256(browser_binding.encode()).hexdigest(), 'revision': config['revision']}
            body = _encode(json.dumps(ticket, sort_keys=True, separators=(',', ':')).encode())
            signature = _encode(hmac.digest(_session_secret(self.root, create=True), b'sso-state:' + body.encode(), 'sha256'))
            states[sid] = {**ticket, 'consumed': False}
            _write(self.root, '.sso-states.json', states)
        return {'ok': True, 'state': body + '.' + signature, 'nonce': nonce,
            'expires_at': now + 300, 'protocol': config['protocol'], 'issuer_url': config['issuer_url']}

    # LLM: state先验MAC再解析，单次消费另在原锁内；不能把客户端nonce/角色/user_id当作已验证身份。
    # 函数用途: 验证握手签名、时限、浏览器关联和当前配置版本。
    def _ticket(self, state, nonce, browser_binding, config):
        from .user_auth import _decode, _session_secret
        try:
            if type(state) is not str or len(state) > 4096 or type(nonce) is not str or type(browser_binding) is not str:
                raise ValueError('state')
            body, mac = state.split('.')
            expected = hmac.digest(_session_secret(self.root), b'sso-state:' + body.encode('ascii'), 'sha256')
            if not hmac.compare_digest(expected, _decode(mac)):
                raise ValueError('signature')
            row = load_strict_json(_decode(body))
            now = int(self.clock())
            if (not config.get('enabled') or row['revision'] != config['revision']
                    or row['expires_at'] - row['issued_at'] != 300 or not row['issued_at'] <= now < row['expires_at']
                    or not hmac.compare_digest(row['nonce'], nonce)
                    or not hmac.compare_digest(row['binding'], hashlib.sha256(browser_binding.encode()).hexdigest())):
                raise ValueError('binding')
            return row
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise HostAPIError('SSO_STATE_INVALID', 401) from None

    # LLM: 锁外验签器仅得独立深拷贝，不得改变授权快照；state单次消费，MFA失败须重开握手，不恢复旧state。
    # 函数用途: 校验IdP断言和单次握手，将可信subject映射到既有普通账号并沿唯一认证服务签发会话。
    def complete_sso_callback(self, *, state, nonce, response, browser_binding, totp_code=None):
        from .user_auth import _current_credential, _issue_session
        if not callable(self.assertion_verifier):
            raise HostAPIError('HOST_NOT_READY', 503)
        config = _read(self.root, '.sso-config.json')
        ticket = self._ticket(state, nonce, browser_binding, config)
        identity = self.assertion_verifier(deepcopy(config), response, ticket['nonce'])
        if (type(identity) is not VerifiedSSOIdentity or identity.issuer_url != config['issuer_url']
                or type(identity.subject) is not str or not identity.subject
                or type(identity.nonce) is not str or not hmac.compare_digest(identity.nonce, ticket['nonce'])
                or type(identity.roles) is not tuple or any(type(role) is not str for role in identity.roles)):
            raise HostAPIError('SSO_ASSERTION_INVALID', 401)
        if config.get('role_mapping') and not any(role in config['role_mapping'] for role in identity.roles):
            raise HostAPIError('SSO_IDENTITY_UNMAPPED', 403)
        uid = config.get('subject_mapping', {}).get(identity.subject)
        if uid is None:
            raise HostAPIError('SSO_IDENTITY_UNMAPPED', 403)
        self._user(uid)
        with self._locked():
            current_config = _read(self.root, '.sso-config.json')
            self._ticket(state, nonce, browser_binding, current_config)
            states = _read(self.root, '.sso-states.json')
            saved = states.get(ticket['sid'])
            if saved is None or saved.get('consumed'):
                raise HostAPIError('SSO_STATE_REPLAYED', 409)
            if {k: v for k, v in saved.items() if k != 'consumed'} != ticket:
                raise HostAPIError('SSO_STATE_INVALID', 401)
            saved['consumed'] = True
            _write(self.root, '.sso-states.json', states)
            expected_stamp = authentication_stamp(self.root, uid)
        credential = _current_credential(self.root, 'local', uid, '', allow_passwordless=True)
        session = _issue_session(self.root, credential, 'local', '', 86400,
            totp_code=totp_code, expected_auth_stamp=expected_stamp)
        if _read(self.root, '.sso-config.json') != config:
            raise HostAPIError('SESSION_REVOKED', 401)
        return session.public_view()
