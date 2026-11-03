"""租户值班、RFC8291/8292 WebPush 与单次实时票据；秘密不进入公开投影。"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import secrets
import stat
import struct
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..common.directory_lock import locked_private_directory
from ..common.nofollow_fs import read_bytes_beneath, write_bytes_atomic_beneath
from ..conversation.business_access import BusinessError
from ..plugin_host.errors import HostAPIError
from .v13_store import v13_store_for


# LLM: 使用调用时钟而非系统本地时区。
# 函数用途: 输出 UTC 时间。
def _at(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


# LLM: 无时区时间不能用于授权有效期。
# 函数用途: 解析合同时间戳。
def _epoch(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('timezone')
        return parsed.timestamp()
    except (ValueError, TypeError, AttributeError):
        raise BusinessError('INVALID_SCHEMA') from None


# LLM: 回执只保存摘要，避免秘密参数进入账本。
# 函数用途: 生成动作参数指纹。
def _digest(call):
    return hashlib.sha256(json.dumps([call.action, call.params], sort_keys=True,
        separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


# LLM: 写入复用真实 Store 的幂等、锁及末写授权。
# 函数用途: 提交单集合事务。
def _transaction(call, collection, mutate, scope='owner', receipt_view=None):
    return v13_store_for(call, scope).transaction(collection, mutate,
        idempotency_key=call.idempotency_key, params_digest=_digest(call), receipt_view=receipt_view)


# LLM: 基础层只维护集合修订，记录修订属于业务。
# 函数用途: 生成记录时间及修订字段。
def _stamp(call, previous):
    return {'revision': previous.get('revision', 0) + 1,
        'created_at': previous.get('created_at', _at(call.clock())), 'updated_at': _at(call.clock())}


# LLM: 函数内导入避免分片注册器循环。
# 函数用途: 读取当前租户班表。
def _shift(call):
    from .v13_w12_shifts_analytics import current_shift
    return current_shift(call)


# LLM: 显式状态优先；过期不回退成班表激活。
# 函数用途: 投影公开值班状态。
def _entry(call, subject, record, shift):
    if record is not None:
        if record.get('owner_id') != call.owner_id:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        return {'subject_id': subject, 'display_name': record.get('display_name'),
            'on_call': bool(record['on_call'] and (record.get('until') is None
                or _epoch(record['until']) > call.clock())), 'until': record.get('until'),
            'note': record.get('note'), 'source': 'self', 'shift_id': None,
            'updated_at': record.get('updated_at')}
    return {'subject_id': subject, 'display_name': None, 'on_call': subject in shift['on_duty'],
        'until': None, 'note': None, 'source': 'shift', 'shift_id': shift['shift_id'], 'updated_at': None}


# LLM: 不建立跨 owner 花名册，超量明确 partial。
# 函数用途: 合并本人状态集合与当前班表。
def oncall_list(call):
    rows = v13_store_for(call).list('oncall_status')
    shift = _shift(call)
    explicit = {row['subject_id']: row for row in rows if row.get('owner_id') == call.owner_id}
    subjects = sorted(set(explicit) | set(shift['on_duty']))
    return {'items': [_entry(call, subject, explicit.get(subject), shift) for subject in subjects[:500]],
        'current_shift_id': shift['shift_id'], 'coverage': 'partial' if len(subjects) > 500 else 'complete'}


# LLM: subject 只能来自活跃身份。
# 函数用途: 返回本人值班状态。
def oncall_me_get(call):
    row = v13_store_for(call).get('oncall_status', call.subject_id)
    return {'entry': _entry(call, call.subject_id, row, _shift(call))}


# LLM: 不允许写入已经过期的截止时间。
# 函数用途: 切换本人值班状态。
def oncall_me_set(call):
    # LLM: 锁内只改变本人的键。
    # 函数用途: 写入规范值班记录。
    def mutate(rows):
        if call.params.get('until') is not None and _epoch(call.params['until']) <= call.clock():
            raise BusinessError('INVALID_SCHEMA')
        old = rows.get(call.subject_id, {})
        row = {'subject_id': call.subject_id, 'owner_id': call.owner_id,
            'on_call': call.params['on_call'], 'until': call.params.get('until'),
            'note': call.params.get('note'), 'display_name': call.params.get('display_name'), **_stamp(call, old)}
        rows[call.subject_id] = row
        return {'entry': _entry(call, call.subject_id, row, {'shift_id': None, 'on_duty': []}),
            'revision': row['revision']}

    return _transaction(call, 'oncall_status', mutate)


# LLM: RFC 使用无填充 URL-safe 编码。
# 函数用途: 编码密钥和 JWT 字段。
def _b64(raw):
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


# LLM: 严格验证编码字符和长度，不宽松吞掉非法字符。
# 函数用途: 解码浏览器订阅密钥。
def _decode(value, length):
    try:
        if not isinstance(value, str) or re.fullmatch(r'[A-Za-z0-9_-]+={0,2}', value) is None:
            raise ValueError('base64')
        raw = base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)
        if len(raw) != length:
            raise ValueError('length')
        return raw
    except (ValueError, binascii.Error):
        raise BusinessError('INVALID_SCHEMA') from None


# LLM: 允许域只按完整主机或点边界子域比较，不允许用户信息和旁路端口。
# 函数用途: 检查推送地址的 HTTPS 与主机范围。
def _endpoint(endpoint):
    try:
        url = urlsplit(endpoint)
        host = url.hostname or ''
        allowed = host in ('fcm.googleapis.com', 'updates.push.services.mozilla.com', 'web.push.apple.com')
        allowed = allowed or any(host.endswith('.' + suffix) and host != suffix
            for suffix in ('push.services.mozilla.com', 'notify.windows.com'))
        if (url.scheme != 'https' or not allowed or url.username is not None or url.password is not None
                or url.port not in (None, 443) or url.fragment or '\\' in endpoint
                or any(ord(ch) <= 32 for ch in endpoint)):
            raise ValueError('host')
        return host
    except ValueError:
        raise BusinessError('INVALID_SCHEMA', details={'reason': 'endpoint_host_not_allowed'}) from None


# LLM: 限定单个文件名，no-follow 读取后核权限和公开点匹配。
# 函数用途: 安全读取 main scope VAPID 私钥。
def _private(store, row):
    name = row['private_key_file']
    if re.fullmatch(r'vapid_[a-f0-9]+\.pem', name) is None:
        raise HostAPIError('HOST_NOT_READY', 503)
    parts = ('data', 'business_v13', name)
    raw = read_bytes_beneath(store.owner_home, parts, max_bytes=4096)
    path = store.owner_home.joinpath(*parts)
    if raw is None or stat.S_IMODE(path.lstat().st_mode) != 0o600:
        raise HostAPIError('HOST_NOT_READY', 503)
    key = serialization.load_pem_private_key(raw, password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise HostAPIError('HOST_NOT_READY', 503)
    public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    if _b64(public) != row['public_key']:
        raise HostAPIError('HOST_NOT_READY', 503)
    return key


# LLM: 主密钥不从调用方 owner 目录猜测。
# 函数用途: 取得唯一配置和私钥。
def _vapid(call):
    store = v13_store_for(call, 'main')
    rows = store.list('push_vapid_keys')
    if len(rows) != 1 or rows[0].get('owner_id') != store.owner_id:
        raise HostAPIError('HOST_NOT_READY', 503)
    return rows[0], _private(store, rows[0])


# LLM: readiness 不创建目录或秘密。
# 函数用途: 向注册器报告真实 VAPID 可用性。
def vapid_ready(call):
    from ..user_space.owner_resolver import resolve_owner_home
    from .v13_store import V13Store
    try:
        home = resolve_owner_home(call.services.host.home_paths.root).home_dir
        store = V13Store(home, owner_id='local/main')
        store._readonly = True
        store._scope = 'main'
        rows = store.list('push_vapid_keys')
        if len(rows) != 1 or rows[0].get('owner_id') != store.owner_id:
            return False
        _private(store, rows[0])
        return True
    except (OSError, ValueError, TypeError, KeyError, BusinessError, HostAPIError):
        return False


# LLM: 只有原生管理员可配置，已有密钥保持稳定。
# 函数用途: 创建或更新 VAPID 公开配置。
def vapid_configure(call):
    from ..gateway_parts.business_api_v13 import v13_require_admin
    v13_require_admin(call)
    subject = call.params.get('subject')
    if subject is not None and not subject.startswith(('mailto:', 'https://')):
        raise BusinessError('INVALID_SCHEMA')
    store = v13_store_for(call, 'main')

    # LLM: 集合锁内生成密钥，私钥以 0600 原子落盘，从不返回路径。
    # 函数用途: 保留原密钥并更新 subject。
    def mutate(rows):
        v13_require_admin(call)
        created = not rows
        if rows:
            if len(rows) != 1:
                raise HostAPIError('HOST_NOT_READY', 503)
            row = next(iter(rows.values()))
            _private(store, row)
        else:
            key = ec.generate_private_key(ec.SECP256R1())
            key_id = 'vapid_' + secrets.token_hex(16)
            name = key_id + '.pem'
            write_bytes_atomic_beneath(store.owner_home, ('data', 'business_v13', name),
                key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption()), file_mode=0o600)
            row = {'key_id': key_id, 'owner_id': store.owner_id, 'private_key_file': name,
                'public_key': _b64(key.public_key().public_bytes(serialization.Encoding.X962,
                    serialization.PublicFormat.UncompressedPoint))}
        row.update(_stamp(call, row))
        if subject is not None or created:
            row['subject'] = subject
        rows[row['key_id']] = row
        v13_require_admin(call)
        return {'key_id': row['key_id'], 'public_key': row['public_key'],
            'subject_configured': bool(row['subject']), 'created': created}

    return _transaction(call, 'push_vapid_keys', mutate, 'main')


# LLM: 查询不能隐式生成密钥或披露 subject 正文。
# 函数用途: 返回 applicationServerKey。
def public_key(call):
    row, unused = _vapid(call)
    return {'key_id': row['key_id'], 'public_key': row['public_key'], 'subject_configured': bool(row['subject'])}


# LLM: 订阅属于 owner+subject；同 endpoint 只刷新自己的记录。
# 函数用途: 校验并保存浏览器推送订阅。
def subscribe(call):
    _vapid(call)
    host = _endpoint(call.params['endpoint'])
    point = _decode(call.params['keys']['p256dh'], 65)
    _decode(call.params['keys']['auth'], 16)
    try:
        if point[0] != 4:
            raise ValueError('point')
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)
    except ValueError:
        raise BusinessError('INVALID_SCHEMA') from None

    # LLM: 不覆盖其它 subject 的相同 URL 订阅。
    # 函数用途: 在集合锁内创建或刷新订阅。
    def mutate(rows):
        matches = [r for r in rows.values() if r.get('owner_id') == call.owner_id
            and r.get('subject_id') == call.subject_id and r['endpoint'] == call.params['endpoint']]
        old = matches[0] if matches else {}
        own = [r for r in rows.values() if r.get('owner_id') == call.owner_id
            and r.get('subject_id') == call.subject_id]
        if not matches and len(own) >= 20:
            raise BusinessError('LIMIT_EXCEEDED')
        identifier = old.get('subscription_id', 'push_' + secrets.token_hex(16))
        rows[identifier] = {'subscription_id': identifier, 'owner_id': call.owner_id,
            'subject_id': call.subject_id, 'endpoint': call.params['endpoint'], 'endpoint_host': host,
            'keys': dict(call.params['keys']), 'user_agent': call.params.get('user_agent'), **_stamp(call, old)}
        return {'subscription_id': identifier, 'endpoint_host': host, 'created': not matches}

    return _transaction(call, 'push_subscriptions', mutate)


# LLM: 删除只作用于当前主体，不通过存在性暴露其它主体记录。
# 函数用途: 按 URL 或 ID 注销本人订阅。
def unsubscribe(call):
    if ('endpoint' in call.params) == ('subscription_id' in call.params):
        raise BusinessError('INVALID_SCHEMA')

    # LLM: 锁内筛选身份再删除，未知记录返回 false。
    # 函数用途: 删除匹配的本人订阅。
    def mutate(rows):
        field = 'endpoint' if 'endpoint' in call.params else 'subscription_id'
        selected = [key for key, row in rows.items() if row.get('owner_id') == call.owner_id
            and row.get('subject_id') == call.subject_id and row.get(field) == call.params[field]]
        for key in selected:
            del rows[key]
        return {'removed': bool(selected)}

    return _transaction(call, 'push_subscriptions', mutate)


# LLM: 标准 HKDF 保持 key_info/CEK/nonce 的不同上下文。
# 函数用途: 派生 RFC8291 密钥材料。
def _derive(secret, salt, info, size):
    return HKDF(algorithm=hashes.SHA256(), length=size, salt=salt, info=info).derive(secret)


# LLM: 不重用临时 ECDH 私钥和 salt，密文带 RFC8188 头。
# 函数用途: 生成单记录 aes128gcm 载荷。
def _encrypt(row, payload):
    receiver = _decode(row['keys']['p256dh'], 65)
    auth = _decode(row['keys']['auth'], 16)
    remote = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), receiver)
    ephemeral = ec.generate_private_key(ec.SECP256R1())
    sender = ephemeral.public_key().public_bytes(serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint)
    shared = ephemeral.exchange(ec.ECDH(), remote)
    ikm = _derive(shared, auth, b'WebPush: info\x00' + receiver + sender, 32)
    salt = secrets.token_bytes(16)
    cek = _derive(ikm, salt, b'Content-Encoding: aes128gcm\x00', 16)
    nonce = _derive(ikm, salt, b'Content-Encoding: nonce\x00', 12)
    ciphertext = AESGCM(cek).encrypt(nonce, payload + b'\x02', None)
    if len(ciphertext) > 4096:
        raise BusinessError('LIMIT_EXCEEDED')
    return salt + struct.pack('!I', 4096) + bytes([len(sender)]) + sender + ciphertext


# LLM: aud 只绑定服务 origin，ES256 签名用 raw r||s 而非 DER。
# 函数用途: 生成 RFC8292 VAPID 授权头。
def _authorization(call, endpoint, config, key):
    origin = urlsplit(endpoint)
    claims = {'aud': origin.scheme + '://' + origin.netloc, 'exp': int(call.clock()) + 43200}
    if config.get('subject'):
        claims['sub'] = config['subject']
    signing = _b64(json.dumps({'typ': 'JWT', 'alg': 'ES256'}, separators=(',', ':')).encode())
    signing += '.' + _b64(json.dumps(claims, separators=(',', ':')).encode())
    r, s = decode_dss_signature(key.sign(signing.encode(), ec.ECDSA(hashes.SHA256())))
    token = signing + '.' + _b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))
    return 'vapid t=' + token + ', k=' + config['public_key']


class _NoRedirect(HTTPRedirectHandler):
    """推送 POST 不能跟随任意 Location，避免把订阅秘密送出允许域。"""

    # LLM: 即使同域也不自动重发，3xx 由调用方明确记失败。
    # 函数用途: 阻止所有推送重定向。
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# LLM: HTTP 客户端只发送标准加密请求；不解析外部响应正文。
# 函数用途: 发送一次 HTTPS 推送并返回状态码。
def _post(request):
    try:
        with build_opener(_NoRedirect()).open(request, timeout=15) as response:
            return response.status
    except HTTPError as error:
        status = error.code
        error.close()
        return status


# LLM: 每次发送前复核 URL，不信任磁盘记录里的 endpoint_host。
# 函数用途: 加密并发送一个有标签的测试通知。
def _send(call, row, config, key):
    result = {'subscription_id': row['subscription_id'], 'endpoint_host': _endpoint(row['endpoint']),
        'status': 'failed', 'http_status': None, 'error_code': None}
    if result['endpoint_host'] == 'web.push.apple.com' and not config.get('subject'):
        result.update(status='skipped', error_code='VAPID_SUBJECT_REQUIRED')
        return result
    payload = json.dumps({'title': call.params.get('title', 'WebPush 测试通知'),
        'body': call.params.get('body', '这是一条测试通知'), 'test': True},
        ensure_ascii=False, separators=(',', ':')).encode()
    request = Request(row['endpoint'], data=_encrypt(row, payload), method='POST', headers={
        'TTL': '60', 'Content-Encoding': 'aes128gcm', 'Content-Type': 'application/octet-stream',
        'Authorization': _authorization(call, row['endpoint'], config, key)})
    try:
        status = _post(request)
    except OSError:
        result['error_code'] = 'PUSH_NETWORK_ERROR'
        return result
    result['http_status'] = status
    if status in (201, 202):
        result['status'] = 'delivered'
    else:
        result['status'] = 'expired' if status in (404, 410) else 'failed'
        result['error_code'] = 'PUSH_HTTP_' + str(status)
    return result


# LLM: 发网前先耐久占位；提交失败或崩溃后的同键调用不重发，不能承诺网络 exactly-once。
# 函数用途: 向本人订阅发送一次幂等测试通知。
def push_test(call):
    with locked_private_directory(call.owner_home, relative_parts=('data', 'business_v13'),
            lock_name='.push-send.lock'):
        return _push_test_once(call)


# LLM: 单独的发送序列锁使并发同键请求等待最终回执，不提前消费发送占位。
# 函数用途: 在发送锁内完成占位与结果事务。
def _push_test_once(call):
    config, key = _vapid(call)
    store = v13_store_for(call)
    nonce = secrets.token_urlsafe(24)

    # LLM: 只有首次提交者获得 nonce，回执重放无法取得发送资格。
    # 函数用途: 生成一次发送许可。
    def reserve(rows):
        return {'dispatch': nonce}

    # LLM: 占位回执不保留可重发凭据。
    # 函数用途: 脱敏一次发送许可。
    def hide(result):
        return {'dispatch': '<redacted>'}

    # 冒号不属于公开幂等键语法，内部占位不可能与任何合法用户键碰撞。
    reservation = store.transaction('push_subscriptions', reserve,
        idempotency_key=':reserve:' + hashlib.sha256(call.idempotency_key.encode()).hexdigest(),
        params_digest=_digest(call), receipt_view=hide)

    # LLM: 成功回执重放由 Store 短路；未确认占位只能返回未知，不再触网。
    # 函数用途: 发送并保存结果，同时清除过期订阅。
    def mutate(rows):
        own = [row for row in rows.values() if row.get('owner_id') == call.owner_id
            and row.get('subject_id') == call.subject_id]
        if len(own) > 20:
            raise BusinessError('LIMIT_EXCEEDED')
        results = []
        for row in sorted(own, key=lambda item: item['subscription_id']):
            if reservation['dispatch'] != nonce:
                result = {'subscription_id': row['subscription_id'], 'endpoint_host': row['endpoint_host'],
                    'status': 'failed', 'http_status': None, 'error_code': 'OUTCOME_UNKNOWN'}
            else:
                call.context.check(call.action, {'owner_id': call.owner_id, 'subject_id': call.subject_id})
                result = _send(call, row, config, key)
            results.append(result)
            if result['status'] == 'expired':
                del rows[row['subscription_id']]
        return {'results': results, 'delivered': sum(r['status'] == 'delivered' for r in results),
            'failed': sum(r['status'] in ('failed', 'expired') for r in results)}

    return _transaction(call, 'push_subscriptions', mutate)


# LLM: secret-once 回执必须同时隐藏 ticket 和 URL 中的副本。
# 函数用途: 生成安全票据回执。
def _ticket_receipt(result):
    return {**result, 'ticket': '<redacted>', 'ws_path': '/ws/' + result['channel'] + '?token=<redacted>'}


# LLM: 原空间与线程授权先复核，不把任意过滤器转成跨范围读凭据。
# 函数用途: 签发只返回一次的短期实时票据。
def ticket_mint(call):
    from ..gateway_parts.business_api_v13 import v13_call_frozen, v13_resolve_space
    if call.params.get('space_id'):
        v13_resolve_space(call, call.params['space_id'])
    if call.params.get('thread_id'):
        v13_call_frozen(call, 'messages.list', {'target': {'kind': 'background_main',
            'id': call.params['thread_id']}, 'limit': 1})
    ttl = call.params.get('ttl_seconds', 60)
    if type(ttl) is not int or not 10 <= ttl <= 300:
        raise BusinessError('INVALID_SCHEMA')
    ticket = 'rt_' + secrets.token_urlsafe(32)
    digest = hashlib.sha256(ticket.encode()).hexdigest()
    expiry = _at(call.clock() + ttl)
    filters = {'space_id': call.params.get('space_id'), 'thread_id': call.params.get('thread_id')}

    # LLM: 存储只保存哈希；每笔最多清理 99 条，遵守 Store 改动上限。
    # 函数用途: 清理旧票据并写入新票据。
    def mutate(rows):
        stale = [key for key, row in rows.items() if
            _epoch(row.get('used_at') or row['expires_at']) < call.clock() - 86400]
        for key in sorted(stale)[:99]:
            del rows[key]
        rows[digest] = {'ticket_sha256': digest, 'owner_id': call.owner_id,
            'subject_id': call.subject_id, 'channel': call.params['channel'], 'filter': filters,
            'expires_at': expiry, 'used_at': None, **_stamp(call, {})}
        return {'ticket': ticket, 'channel': call.params['channel'], 'filter': filters,
            'expires_at': expiry, 'ws_path': '/ws/' + call.params['channel'] + '?token=<redacted>'}

    return _transaction(call, 'realtime_tickets', mutate, receipt_view=_ticket_receipt)


# LLM: 不同 owner/subject/channel 与过期、已用都返回同一错误，避免存在性泄露。
# 函数用途: 原子消费一次实时票据。
def ticket_verify(call):
    digest = hashlib.sha256(call.params['ticket'].encode()).hexdigest()

    # LLM: 校验与置 used 必须在同一集合锁内。
    # 函数用途: 核验并消费票据。
    def mutate(rows):
        row = rows.get(digest)
        if (row is None or row.get('owner_id') != call.owner_id or row.get('subject_id') != call.subject_id
                or row.get('channel') != call.params['channel'] or row.get('used_at') is not None
                or _epoch(row['expires_at']) <= call.clock()):
            raise HostAPIError('VERIFICATION_FAILED', 403)
        row.update(_stamp(call, row))
        row['used_at'] = _at(call.clock())
        return {'valid': True, 'owner_id': call.owner_id, 'subject_id': call.subject_id,
            'channel': row['channel'], 'filter': row['filter'], 'expires_at': row['expires_at']}

    return _transaction(call, 'realtime_tickets', mutate)


# LLM: health 观察原路由表和原 events.list readiness，不声称已存在 WebSocket 服务。
# 函数用途: 报告 NDJSON 通道能力及本人活跃票据。
def realtime_health(call):
    from ..gateway_parts.http_routes import GATEWAY_HTTP_ROUTES
    enabled = bool(getattr(call.context.host.config, 'business_spaces_enabled', False))
    route = any(row.template == '/client/business/stream' for row in GATEWAY_HTTP_ROUTES.get('POST', ()))
    ready = enabled and call.services.ready('events.list')
    active = [row for row in v13_store_for(call).list('realtime_tickets')
        if row.get('owner_id') == call.owner_id and row.get('subject_id') == call.subject_id
        and row.get('used_at') is None and _epoch(row['expires_at']) > call.clock()]
    return {'status': 'healthy' if route and ready else 'degraded' if route else 'unavailable',
        'transport': 'ndjson', 'stream_route': '/client/business/stream', 'websocket_bridge': 'bff',
        'business_spaces_enabled': enabled, 'active_tickets': len(active), 'server_time': _at(call.clock())}
