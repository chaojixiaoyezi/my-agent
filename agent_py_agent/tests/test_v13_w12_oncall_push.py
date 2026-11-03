"""通过真实 dispatcher、owner Store 与 HTTP 验证值班、标准 WebPush 和实时票据。"""
import base64
import hashlib
import hmac
import json
import stat
import struct
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from agent_py_agent.agent.business_spaces import v13_w12_oncall_push as module
from agent_py_agent.agent.business_spaces.v13_store import V13Store
from agent_py_agent.agent.gateway_parts import business_api as api
from agent_py_agent.agent.gateway_parts.business_api_v13 import V13Call
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import home_paths_with_owner, resolve_owner_home
from agent_py_agent.tests.test_v13_w0_harness import v13_call, v13_native  # noqa: F401


# LLM: 基础 home_paths 尚未绑定 owner，测试必须使用真实规范 owner 解析。
# 函数用途: 构建明确 local/main 的路径投影。
def owner_paths(root):
    return home_paths_with_owner(home_paths(root), resolve_owner_home(root))


# LLM: 每个测试使用临时 home 与固定可推进时钟。
# 函数用途: 创建真实领域服务使用的隔离宿主。
@pytest.fixture
def host(tmp_path, monkeypatch):
    root = tmp_path / 'host'
    monkeypatch.setenv('MY_AGENT_HOME', str(root))
    monkeypatch.setenv('HOME', str(tmp_path / 'os-home'))
    clock = [1793664000.0]
    return SimpleNamespace(home_paths=owner_paths(root), config=AgentConfig(business_spaces_enabled=True),
        business_v13_clock=lambda: clock[0], clock=clock)


# LLM: 请求只从真实 bind_context 取得身份，不往 JSON 塞 owner。
# 函数用途: 调用真实统一业务 dispatcher。
def invoke(host, action, params=None, *, subject='alice', key=None, admin=None, authorize=None, expected=None):
    if admin is not None:
        host.business_native_admin = lambda: admin
    context = api.bind_business_context(host, subject_id=subject,
        authorize=authorize if authorize is not None else lambda action, facts: True)
    request = {'action': action, 'request_id': uuid4().hex, 'params': params or {}}
    if key is not None:
        request['idempotency_key'] = key
    if expected is not None:
        request['expected_revision'] = expected
    return api.execute_business(request, context, api.BusinessServices(host))


# LLM: 断言标准成功信封而非手写假结果。
# 函数用途: 调用并取出校验过的业务数据。
def ok(host, action, params=None, *, subject='alice', key=None, expected=None):
    status, body = invoke(host, action, params, subject=subject, key=key, expected=expected)
    assert status == 200, body
    assert body['state'] == 'completed' and body['error'] is None, body
    return body['data']


# LLM: 所有写使用足够长的合成幂等键。
# 函数用途: 创建测试幂等键。
def idem():
    return 'w12-' + uuid4().hex


# LLM: 独立实现编码，不复用产品加解密函数。
# 函数用途: 编码合成订阅参数。
def b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


# LLM: 浏览器接收端私钥只存在于测试进程。
# 函数用途: 创建真实 P256 接收密钥。
def subscription(hostname='fcm.googleapis.com', suffix=None):
    private = ec.generate_private_key(ec.SECP256R1())
    auth = bytes(range(16))
    public = private.public_key().public_bytes(serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint)
    params = {'endpoint': 'https://' + hostname + '/send/' + (suffix or uuid4().hex),
        'keys': {'p256dh': b64(public), 'auth': b64(auth)}}
    return params, private, auth


# LLM: 配置经真实管理员动作执行，不直接伪造 PEM 或回执。
# 函数用途: 配置合成主机 VAPID。
def configured(host, subject='mailto:test@example.invalid'):
    params = {} if subject is None else {'subject': subject}
    return ok(host, 'push.vapid.configure', params, key=idem())


# LLM: 通过班表真实写动作建立 oncall 来源。
# 函数用途: 配置包含两位响应人的当前班表。
def set_shift(host):
    return ok(host, 'shifts.schedule.set', {'timezone': 'UTC', 'shifts': [
        {'shift_id': 'day', 'name': '日班', 'start': '00:00', 'end': '23:59',
         'days': ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'],
         'members': ['alice', 'bob'], 'lead': 'alice'}]}, key=idem(), expected=0)


# LLM: 显式关闭必须覆盖班表激活，不得泄露另租户。
# 函数用途: 验证列表合并优先级与租户范围。
def test_oncall_list_merges_self_status_and_current_shift(host):
    set_shift(host)
    ok(host, 'oncall.me.set', {'on_call': False, 'display_name': '本人'}, key=idem())
    rows = ok(host, 'oncall.list')
    assert rows['current_shift_id'] == 'day' and rows['coverage'] == 'complete'
    entries = {row['subject_id']: row for row in rows['items']}
    assert entries['alice']['source'] == 'self' and entries['alice']['on_call'] is False
    assert entries['bob']['source'] == 'shift' and entries['bob']['on_call'] is True
    other = SimpleNamespace(home_paths=owner_paths(host.home_paths.root / 'other-host'), config=host.config)
    assert ok(other, 'oncall.list')['items'] == []


# LLM: 原 issue 空参数例子必须真实成功，无记录明确 false。
# 函数用途: 验证本人值班查询示例。
def test_oncall_me_example_empty_params(host):
    entry = ok(host, 'oncall.me.get', {})['entry']
    assert entry['subject_id'] == 'alice' and entry['on_call'] is False
    set_shift(host)
    assert ok(host, 'oncall.me.get')['entry']['on_call'] is True


# LLM: 时间推进来自宿主时钟，校验过期关闭和同键参数冲突。
# 函数用途: 验证值班切换、期限和幂等。
def test_oncall_me_set_toggles_and_until_expires(host):
    until = module._at(host.clock[0] + 60)
    key = idem()
    first = ok(host, 'oncall.me.set', {'on_call': True, 'until': until}, key=key)
    assert first['entry']['on_call'] is True and first['revision'] == 1
    assert ok(host, 'oncall.me.set', {'on_call': True, 'until': until}, key=key) == first
    status, body = invoke(host, 'oncall.me.set', {'on_call': False}, key=key)
    assert status == 409 and body['error']['code'] == 'IDEMPOTENCY_CONFLICT'
    host.clock[0] += 61
    assert ok(host, 'oncall.me.get')['entry']['on_call'] is False
    assert ok(host, 'oncall.me.get', subject='bob')['entry']['on_call'] is False
    status, body = invoke(host, 'oncall.me.set', {'on_call': True, 'until': until}, key=idem())
    assert status == 400 and body['error']['code'] == 'INVALID_SCHEMA'


# LLM: 无配置读取不建密钥，配置只返回公开字段。
# 函数用途: 验证 VAPID 管理员门、稳定性及私钥权限。
def test_vapid_configure_once_and_public_key_stable(host):
    before = list(host.home_paths.owner_home_dir.rglob('*'))
    status, body = invoke(host, 'push.public_key')
    assert status == 503 and body['error']['code'] == 'HOST_NOT_READY'
    assert list(host.home_paths.owner_home_dir.rglob('*')) == before
    status, body = invoke(host, 'push.vapid.configure', {}, key=idem(), admin=False)
    assert status == 403 and body['error']['code'] == 'BUSINESS_ACCESS_DENIED'
    host.business_native_admin = lambda: True
    status, body = invoke(host, 'push.vapid.configure', {'subject': 'ftp://wrong.invalid'}, key=idem())
    assert status == 400 and body['error']['code'] == 'INVALID_SCHEMA'
    first = configured(host)
    second = configured(host, 'https://example.invalid/contact')
    assert first['created'] is True and second['created'] is False
    assert first['key_id'] == second['key_id'] and first['public_key'] == second['public_key']
    assert set(first) == {'key_id', 'public_key', 'subject_configured', 'created'}
    assert ok(host, 'push.public_key')['public_key'] == first['public_key']
    pem = next((host.home_paths.owner_home_dir / 'data' / 'business_v13').glob('*.pem'))
    assert stat.S_IMODE(pem.stat().st_mode) == 0o600
    assert 'PRIVATE KEY' not in json.dumps(first)
    services = api.BusinessServices(host)
    probe = SimpleNamespace(services=services, context=None)
    assert module.vapid_ready(probe) is True
    pem.chmod(0o644)
    assert module.vapid_ready(probe) is False
    pem.chmod(0o600)
    original = pem.read_bytes()
    pem.unlink()
    target = pem.parent / 'synthetic-key-copy'
    target.write_bytes(original)
    pem.symlink_to(target.name)
    assert module.vapid_ready(probe) is False


# LLM: 覆盖主机欺骗、非 HTTPS、非法点与 auth 长度，不能只测合法输入。
# 函数用途: 验证订阅地址与密钥范围。
def test_subscribe_validates_keys_and_endpoint_scope_check(host):
    configured(host)
    params, private, auth = subscription()
    key = idem()
    first = ok(host, 'push.subscribe', params, key=key)
    assert first['created'] is True
    assert ok(host, 'push.subscribe', params, key=key) == first
    assert ok(host, 'push.subscribe', params, key=idem())['created'] is False
    changed, unused, unused_auth = subscription(suffix='changed')
    status, body = invoke(host, 'push.subscribe', changed, key=key)
    assert status == 409 and body['error']['code'] == 'IDEMPOTENCY_CONFLICT'
    for endpoint in ('http://fcm.googleapis.com/x', 'https://fcm.googleapis.com.evil.invalid/x',
            'https://evilfcm.googleapis.com/x', 'https://fcm.googleapis.com@evil.invalid/x',
            'https://127.0.0.1/x', 'https://fcm.googleapis.com:8443/x',
            'https://fcm.googleapis.com/x#fragment', 'https://evil.invalid/a.push.services.mozilla.com'):
        status, body = invoke(host, 'push.subscribe', {**params, 'endpoint': endpoint}, key=idem())
        assert status == 400 and body['error']['code'] == 'INVALID_SCHEMA', endpoint
    for keys in ({'p256dh': b64(b'\x04' + b'\x00' * 64), 'auth': b64(auth)},
            {'p256dh': params['keys']['p256dh'], 'auth': b64(b'x' * 15)},
            {'p256dh': '!' + params['keys']['p256dh'], 'auth': b64(auth)}):
        status, body = invoke(host, 'push.subscribe', {**params, 'keys': keys}, key=idem())
        assert status == 400 and body['error']['code'] == 'INVALID_SCHEMA'
    for hostname in ('updates.push.services.mozilla.com', 'one.push.services.mozilla.com',
            'one.notify.windows.com', 'web.push.apple.com'):
        valid, unused, unused_auth = subscription(hostname)
        assert ok(host, 'push.subscribe', valid, key=idem())['endpoint_host'] == hostname


# LLM: 同 URL 的他人订阅必须保留且不能被存在性探测。
# 函数用途: 验证注销两种选择器和主体隔离。
def test_unsubscribe_by_endpoint_or_id(host):
    configured(host)
    params, unused, unused_auth = subscription()
    first = ok(host, 'push.subscribe', params, key=idem())
    other = ok(host, 'push.subscribe', params, subject='bob', key=idem())
    assert first['subscription_id'] != other['subscription_id']
    assert ok(host, 'push.unsubscribe', {'subscription_id': other['subscription_id']}, key=idem()) == {'removed': False}
    assert ok(host, 'push.unsubscribe', {'endpoint': params['endpoint']}, key=idem()) == {'removed': True}
    assert ok(host, 'push.unsubscribe', {'subscription_id': other['subscription_id']}, subject='bob', key=idem()) == {'removed': True}
    for params in ({}, {'endpoint': 'https://fcm.googleapis.com/x', 'subscription_id': 'push_unknown'}):
        status, body = invoke(host, 'push.unsubscribe', params, key=idem())
        assert status == 400 and body['error']['code'] == 'INVALID_SCHEMA'


# LLM: 独立 HMAC 实现 HKDF，不调用产品派生函数。
# 函数用途: 从接收端复算 RFC8291 密钥。
def expand(secret, salt, info, length):
    prk = hmac.digest(salt, secret, 'sha256')
    output, previous, counter = b'', b'', 1
    while len(output) < length:
        previous = hmac.digest(prk, previous + info + bytes([counter]), 'sha256')
        output += previous
        counter += 1
    return output[:length]


# LLM: 从实际请求的二进制头独立解密，而非相信产品返回字段。
# 函数用途: 验证 aes128gcm 与 VAPID JWT 标准兼容。
def test_push_test_body_decrypts_per_rfc8291_and_jwt_verifies(host, monkeypatch):
    config = configured(host)
    params, receiver, auth = subscription()
    ok(host, 'push.subscribe', params, key=idem())
    requests = []

    # LLM: 只截获最后网络边界，产品实际执行标准加密。
    # 函数用途: 保存请求并模拟 201 响应。
    def capture(request):
        requests.append(request)
        return 201

    monkeypatch.setattr(module, '_post', capture)
    key = idem()
    first = ok(host, 'push.test', {'title': '合成测试', 'body': '独立解密'}, key=key)
    assert first['delivered'] == 1 and first['failed'] == 0
    assert ok(host, 'push.test', {'title': '合成测试', 'body': '独立解密'}, key=key) == first
    assert len(requests) == 1
    request = requests[0]
    assert request.get_method() == 'POST' and request.get_header('Content-encoding') == 'aes128gcm'
    raw = request.data
    salt, record_size, public_length = raw[:16], struct.unpack('!I', raw[16:20])[0], raw[20]
    assert record_size == 4096 and public_length == 65
    sender = raw[21:21 + public_length]
    receiver_public = receiver.public_key().public_bytes(serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint)
    shared = receiver.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), sender))
    ikm = expand(shared, auth, b'WebPush: info\x00' + receiver_public + sender, 32)
    cek = expand(ikm, salt, b'Content-Encoding: aes128gcm\x00', 16)
    nonce = expand(ikm, salt, b'Content-Encoding: nonce\x00', 12)
    clear = AESGCM(cek).decrypt(nonce, raw[21 + public_length:], None)
    assert clear[-1:] == b'\x02'
    assert json.loads(clear[:-1]) == {'title': '合成测试', 'body': '独立解密', 'test': True}
    authorization = request.get_header('Authorization')
    assert authorization.startswith('vapid t=')
    jwt, public = authorization[8:].split(', k=')
    header, claims, signature = jwt.split('.')
    # LLM: 测试端独立解码标准 JWT 段。
    # 函数用途: 解码无填充 base64url。
    def decode(text):
        return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))
    assert json.loads(decode(header)) == {'typ': 'JWT', 'alg': 'ES256'}
    assert json.loads(decode(claims)) == {'aud': 'https://fcm.googleapis.com',
        'exp': int(host.clock[0]) + 43200, 'sub': 'mailto:test@example.invalid'}
    assert public == config['public_key']
    signature = decode(signature)
    assert len(signature) == 64
    ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), decode(public)).verify(
        encode_dss_signature(int.from_bytes(signature[:32], 'big'), int.from_bytes(signature[32:], 'big')),
        (header + '.' + claims).encode(), ec.ECDSA(hashes.SHA256()))


# LLM: 覆盖全部投递分类，过期删除必须真实持久化。
# 函数用途: 验证状态码、网络错误、Apple subject 门与主体范围。
def test_push_status_codes_network_failure_expiry_and_subject_scope(host, monkeypatch):
    configured(host, subject=None)
    for index in range(6):
        params, unused, unused_auth = subscription(suffix=str(index))
        ok(host, 'push.subscribe', params, key=idem())
    apple, unused, unused_auth = subscription('web.push.apple.com')
    ok(host, 'push.subscribe', apple, key=idem())
    other, unused, unused_auth = subscription(suffix='bob-only')
    ok(host, 'push.subscribe', other, subject='bob', key=idem())
    statuses = {'0': 201, '1': 202, '2': 404, '3': 410, '4': 500}
    requests = []

    # LLM: 地址尾段只是合成测试输入，不驱动产品分支。
    # 函数用途: 模拟不同服务响应及网络中断。
    def capture(request):
        requests.append(request.full_url)
        index = request.full_url.rsplit('/', 1)[1]
        if index == '5':
            raise OSError('synthetic-network-failure')
        return statuses[index]

    monkeypatch.setattr(module, '_post', capture)
    result = ok(host, 'push.test', key=idem())
    assert result['delivered'] == 2 and result['failed'] == 4
    assert len(requests) == 6 and not any('bob-only' in url for url in requests)
    assert {r['error_code'] for r in result['results']} == {None, 'PUSH_HTTP_404', 'PUSH_HTTP_410',
        'PUSH_HTTP_500', 'PUSH_NETWORK_ERROR', 'VAPID_SUBJECT_REQUIRED'}
    rows = V13Store(host.home_paths.owner_home_dir, owner_id=host.home_paths.owner_id).list('push_subscriptions')
    assert not any(row['endpoint'].endswith(('/2', '/3')) for row in rows)
    assert any(row['endpoint'].endswith('bob-only') for row in rows)


# LLM: 网络发生后提交失败，同键重试不得再次触网。
# 函数用途: 验证耐久占位对不确定结果的 at-most-once 保护。
def test_push_commit_failure_never_resends(host, monkeypatch):
    from agent_py_agent.agent.business_spaces import v13_store
    configured(host)
    params, unused, unused_auth = subscription()
    ok(host, 'push.subscribe', params, key=idem())
    sent = []
    original_write = v13_store.write_bytes_atomic_beneath

    # LLM: 只在实际发送完成后注入日志提交失败。
    # 函数用途: 模拟网络与本地提交之间的中断。
    def fail_write(root, parts, payload):
        if sent and parts[-1] == '.transaction.json':
            raise OSError('synthetic-commit-failure')
        return original_write(root, parts, payload)

    # LLM: 首次外部投递仍由产品正常加密路径构建。
    # 函数用途: 记录一次外部发送。
    def capture(request):
        sent.append(request)
        return 201

    monkeypatch.setattr(module, '_post', capture)
    monkeypatch.setattr(v13_store, 'write_bytes_atomic_beneath', fail_write)
    key = idem()
    status, body = invoke(host, 'push.test', key=key)
    assert status == 503 and body['error']['code'] == 'OUTCOME_UNKNOWN'
    monkeypatch.setattr(v13_store, 'write_bytes_atomic_beneath', original_write)
    result = ok(host, 'push.test', key=key)
    assert len(sent) == 1 and result['results'][0]['error_code'] == 'OUTCOME_UNKNOWN'


# LLM: 原 urllib opener 对真实 3xx 必须停止，不能将认证头跟随到另一地址。
# 函数用途: 通过本地测试 HTTP 验证状态码与重定向拒绝。
def test_http_transport_status_and_no_redirect():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from urllib.request import Request
    received = []

    class Handler(BaseHTTPRequestHandler):
        # LLM: 响应只由合成测试路径控制，不访问外部服务。
        # 函数用途: 返回测试状态或重定向。
        def do_POST(self):
            received.append(self.path)
            code = int(self.path[1:])
            self.send_response(code)
            if code == 302:
                self.send_header('Location', '/201')
            self.end_headers()

        # LLM: 不输出合成请求正文或凭据。
        # 函数用途: 抑制测试 HTTP 日志。
        def log_request(self, code='-', size='-'):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for code in (201, 202, 404, 410, 302):
            request = Request('http://127.0.0.1:' + str(server.server_port) + '/' + str(code),
                data=b'synthetic-ciphertext', method='POST')
            assert module._post(request) == code
        assert received == ['/201', '/202', '/404', '/410', '/302']
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# LLM: 原始 ticket 及其 URL 副本都不能出现在回执、记录或审计文件。
# 函数用途: 验证 secret-once、单次消费、期限和所有身份维度。
def test_ticket_mint_once_verify_single_use_and_expiry(host):
    key = idem()
    minted = ok(host, 'realtime.ticket.mint', {'channel': 'events'}, key=key)
    replay = ok(host, 'realtime.ticket.mint', {'channel': 'events'}, key=key)
    assert replay['ticket'] == '<redacted>' and minted['ticket'] not in replay['ws_path']
    assert minted['ticket'].startswith('rt_')
    assert minted['ws_path'] == '/ws/events?token=<redacted>'
    assert minted['ticket'] not in minted['ws_path']
    for path in (host.home_paths.owner_home_dir / 'data' / 'business_v13').glob('*.json'):
        assert minted['ticket'] not in path.read_text()
    digest = hashlib.sha256(minted['ticket'].encode()).hexdigest()
    assert V13Store(host.home_paths.owner_home_dir, owner_id=host.home_paths.owner_id).get(
        'realtime_tickets', digest)['ticket_sha256'] == digest
    for subject, channel, ticket in (('bob', 'events', minted['ticket']),
            ('alice', 'alerts', minted['ticket']), ('alice', 'events', 'rt_unknown')):
        status, body = invoke(host, 'realtime.ticket.verify', {'ticket': ticket, 'channel': channel},
            subject=subject, key=idem())
        assert status == 403 and body['error']['code'] == 'VERIFICATION_FAILED'
    other = SimpleNamespace(home_paths=owner_paths(host.home_paths.root / 'other'), config=host.config)
    status, body = invoke(other, 'realtime.ticket.verify', {'ticket': minted['ticket'], 'channel': 'events'}, key=idem())
    assert status == 403 and body['error']['code'] == 'VERIFICATION_FAILED'
    verified = ok(host, 'realtime.ticket.verify', {'ticket': minted['ticket'], 'channel': 'events'}, key=idem())
    assert verified['valid'] is True and verified['owner_id'] == host.home_paths.owner_id
    status, body = invoke(host, 'realtime.ticket.verify', {'ticket': minted['ticket'], 'channel': 'events'}, key=idem())
    assert status == 403 and body['error']['code'] == 'VERIFICATION_FAILED'
    expiring = ok(host, 'realtime.ticket.mint', {'channel': 'events', 'ttl_seconds': 10}, key=idem())
    host.clock[0] += 11
    status, body = invoke(host, 'realtime.ticket.verify', {'ticket': expiring['ticket'], 'channel': 'events'}, key=idem())
    assert status == 403 and body['error']['code'] == 'VERIFICATION_FAILED'
    host.clock[0] += 86401
    ok(host, 'realtime.ticket.mint', {'channel': 'events'}, key=idem())
    assert V13Store(host.home_paths.owner_home_dir, owner_id=host.home_paths.owner_id).get('realtime_tickets', digest) is None


# LLM: 并行验证必须只有一个不同幂等键能消费票据。
# 函数用途: 验证单次票据并发互斥。
def test_ticket_concurrent_consumption(host):
    from concurrent.futures import ThreadPoolExecutor
    ticket = ok(host, 'realtime.ticket.mint', {'channel': 'events'}, key=idem())['ticket']

    # LLM: 每个线程持真实独立上下文并共享同一 owner Store。
    # 函数用途: 尝试消费同一票据。
    def consume(index):
        return invoke(host, 'realtime.ticket.verify', {'ticket': ticket, 'channel': 'events'}, key=idem())[0]

    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(consume, range(4)))
    assert sorted(statuses) == [200, 403, 403, 403]


# LLM: 健康检查不将路线存在冒充实际观察源就绪。
# 函数用途: 验证 health 三态、原路线及本人票据计数。
def test_realtime_health_reflects_stream_readiness(host, monkeypatch):
    from agent_py_agent.agent.gateway_parts import http_routes
    ok(host, 'realtime.ticket.mint', {'channel': 'events'}, key=idem())
    assert ok(host, 'realtime.health')['status'] == 'degraded'
    assert ok(host, 'realtime.health')['active_tickets'] == 1
    assert ok(host, 'realtime.health', subject='bob')['active_tickets'] == 0
    services = api.BusinessServices(host)
    original_ready = services.ready
    services.ready = lambda action: True if action == 'events.list' else original_ready(action)
    context = api.bind_business_context(host, subject_id='alice', authorize=lambda action, facts: True)
    call = V13Call('realtime.health', {}, 'health', host.home_paths.owner_id, 'alice', context,
        services, host.home_paths.owner_home_dir, clock=host.business_v13_clock)
    assert module.realtime_health(call)['status'] == 'healthy'
    monkeypatch.setattr(http_routes, 'GATEWAY_HTTP_ROUTES', {'POST': ()})
    assert module.realtime_health(call)['status'] == 'unavailable'


# LLM: 原生客户端真正穿过认证、dispatcher 与 Store，而非分别 mock 两端 JSON。
# 函数用途: 验证 HTTP 关键值班、推送、票据链路。
def test_native_http_oncall_push_and_ticket_roundtrip(v13_native, monkeypatch):  # noqa: F811
    server, agent = v13_native
    configured(agent)
    status, body = v13_call(server, 'oncall.me.get', {}, user='business-alice')
    assert status == 200 and body['data']['entry']['on_call'] is False, body
    status, body = v13_call(server, 'oncall.me.set', {'on_call': True}, user='business-alice', idempotency_key=idem())
    assert status == 200 and body['data']['entry']['on_call'] is True, body
    params, unused, unused_auth = subscription()
    status, body = v13_call(server, 'push.subscribe', params, user='business-alice', idempotency_key=idem())
    assert status == 200 and body['data']['created'] is True, body
    monkeypatch.setattr(module, '_post', lambda request: 202)
    status, body = v13_call(server, 'push.test', {}, user='business-alice', idempotency_key=idem())
    assert status == 200 and body['data']['delivered'] == 1, body
    status, body = v13_call(server, 'realtime.ticket.mint', {'channel': 'events'}, user='business-alice', idempotency_key=idem())
    assert status == 200, body
    ticket = body['data']['ticket']
    status, body = v13_call(server, 'realtime.ticket.verify', {'ticket': ticket, 'channel': 'events'},
        user='business-bob', idempotency_key=idem())
    assert status == 403 and body['error']['code'] == 'VERIFICATION_FAILED', body
    status, body = v13_call(server, 'realtime.ticket.verify', {'ticket': ticket, 'channel': 'events'},
        user='business-alice', idempotency_key=idem())
    assert status == 200 and body['data']['valid'] is True, body
    status, body = v13_call(server, 'realtime.health', {}, user='business-alice')
    assert status == 200 and body['data']['websocket_bridge'] == 'bff', body


# LLM: 同键并发不能争抢占位而丢失最终结果，也不能重复发送。
# 函数用途: 验证多线程推送幂等。
def test_push_concurrent_same_key_sends_once(host, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    configured(host)
    params, unused, unused_auth = subscription()
    ok(host, 'push.subscribe', params, key=idem())
    sent = []

    # LLM: 仅截获外部发网，不替代加密与持久事务。
    # 函数用途: 记录投递次数。
    def capture(request):
        sent.append(request)
        return 202

    monkeypatch.setattr(module, '_post', capture)
    key = idem()

    # LLM: 每位调用者都使用独立 live context。
    # 函数用途: 并行调用同一推送请求。
    def send(index):
        return ok(host, 'push.test', key=key)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(send, range(4)))
    assert len(sent) == 1
    assert all(result == results[0] and result['delivered'] == 1 for result in results)


# LLM: 所有本片动作必须使用闭合 params，额外字段不能流入领域。
# 函数用途: 验证全部十一动作的 schema 和即时撤权。
def test_all_oncall_push_ticket_actions_closed_and_live_authorized(host):
    names = ('oncall.list', 'oncall.me.get', 'oncall.me.set', 'push.vapid.configure',
        'push.public_key', 'push.subscribe', 'push.unsubscribe', 'push.test',
        'realtime.ticket.mint', 'realtime.ticket.verify', 'realtime.health')
    for action in names:
        status, body = invoke(host, action, {'unexpected': True}, key=idem())
        assert status == 400 and body['error']['code'] == 'INVALID_SCHEMA', action
    status, body = invoke(host, 'oncall.list', authorize=lambda action, facts: False)
    assert status == 403 and body['error']['code'] == 'CAPABILITY_DENIED'
    host.config.business_spaces_enabled = False
    status, body = invoke(host, 'realtime.health')
    assert status == 503 and body['error']['code'] == 'HOST_NOT_READY'


# LLM: 每个配置/执行动作均覆盖同键改参冲突，不能只验证其中一个。
# 函数用途: 验证写动作参数摘要与故障映射。
def test_write_conflicts_and_transaction_failure(host, monkeypatch):
    configured(host)
    params, unused, unused_auth = subscription()
    first = ok(host, 'push.subscribe', params, key=idem())
    cases = [('push.vapid.configure', {'subject': 'mailto:first@example.invalid'},
              {'subject': 'mailto:second@example.invalid'}),
        ('push.unsubscribe', {'subscription_id': 'unknown-a'}, {'subscription_id': 'unknown-b'}),
        ('push.test', {'title': 'first'}, {'title': 'second'}),
        ('realtime.ticket.mint', {'channel': 'events'}, {'channel': 'alerts'})]
    monkeypatch.setattr(module, '_post', lambda request: 201)
    for action, params_a, params_b in cases:
        key = idem()
        ok(host, action, params_a, key=key)
        status, body = invoke(host, action, params_b, key=key)
        assert status == 409 and body['error']['code'] == 'IDEMPOTENCY_CONFLICT', action
    minted = ok(host, 'realtime.ticket.mint', {'channel': 'events'}, key=idem())
    key = idem()
    ok(host, 'realtime.ticket.verify', {'ticket': minted['ticket'], 'channel': 'events'}, key=key)
    status, body = invoke(host, 'realtime.ticket.verify', {'ticket': minted['ticket'], 'channel': 'alerts'}, key=key)
    assert status == 409 and body['error']['code'] == 'IDEMPOTENCY_CONFLICT'

    # LLM: 标准写入入口真实抛错，统一边界必须报告未知结果而非假成功。
    # 函数用途: 注入磁盘写入故障。
    def broken_transaction(self, collection, mutate, *, idempotency_key, params_digest,
            expected_revision=None, receipt_view=None):
        raise OSError('synthetic-store-error')

    monkeypatch.setattr(V13Store, 'transaction', broken_transaction)
    for action, params in (('oncall.me.set', {'on_call': True}), ('push.vapid.configure', {}),
            ('push.subscribe', subscription()[0]), ('push.unsubscribe', {'subscription_id': first['subscription_id']}),
            ('push.test', {}), ('realtime.ticket.mint', {'channel': 'events'}),
            ('realtime.ticket.verify', {'ticket': minted['ticket'], 'channel': 'events'})):
        status, body = invoke(host, action, params, key=idem())
        assert status == 503 and body['error']['code'] == 'OUTCOME_UNKNOWN', action


# LLM: 空间/线程过滤器只能引用本人可见资源，不把未知 ID 签成授权票据。
# 函数用途: 验证票据过滤器的真实领域授权。
def test_ticket_filters_resolve_real_space_and_reject_unknown_resources(host):
    space = ok(host, 'spaces.create', {'name': '实时票据测试空间'}, key=idem())
    result = ok(host, 'realtime.ticket.mint', {'channel': 'events', 'space_id': space['space_id']}, key=idem())
    assert result['filter'] == {'space_id': space['space_id'], 'thread_id': None}
    status, body = invoke(host, 'realtime.ticket.mint', {'channel': 'events', 'space_id': 'unknown-space'}, key=idem())
    # 原 spaces.get 的冻结领域码是 SPACE_NOT_FOUND，票据签发不重写该码。
    assert status == 404 and body['error']['code'] == 'SPACE_NOT_FOUND'
    status, body = invoke(host, 'realtime.ticket.mint', {'channel': 'events', 'thread_id': 'unknown-thread'}, key=idem())
    assert status == 503 and body['error']['code'] == 'HOST_NOT_READY'
    for ttl in (9, 301):
        status, body = invoke(host, 'realtime.ticket.mint', {'channel': 'events', 'ttl_seconds': ttl}, key=idem())
        assert status == 400 and body['error']['code'] == 'INVALID_SCHEMA'


# LLM: 公开字段形状由合同规定，秘密记录额外字段绝不能混入返回。
# 函数用途: 守住产品函数签名和秘密回执的静态边界。
def test_static_signatures_and_secret_receipt_boundaries():
    import ast
    from pathlib import Path
    tree = ast.parse(Path(module.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert node.args.vararg is None and node.args.kwarg is None, node.name
    receipt = module._ticket_receipt({'ticket': 'rt_synthetic', 'channel': 'events',
        'filter': {'space_id': None, 'thread_id': None}, 'expires_at': None,
        'ws_path': '/ws/events?token=rt_synthetic'})
    assert 'rt_synthetic' not in json.dumps(receipt)


# LLM: 任意合法公开幂等键都不能命中产品内部发送占位的回执。
# 函数用途: 回归派生占位键与用户键碰撞。
def test_push_internal_reservation_namespace_cannot_collide(host, monkeypatch):
    configured(host)
    params, unused, unused_auth = subscription()
    ok(host, 'push.subscribe', params, key=idem())
    sent = []

    # LLM: 只截获外部网络边界，保留产品加密和真实幂等事务。
    # 函数用途: 记录两次不同公开键的发送。
    def capture(request):
        sent.append(request)
        return 201

    monkeypatch.setattr(module, '_post', capture)
    key = idem()
    first = ok(host, 'push.test', key=key)
    derived = 'reserve_' + hashlib.sha256(key.encode()).hexdigest()
    second = ok(host, 'push.test', key=derived)
    assert first['delivered'] == second['delivered'] == 1
    assert len(sent) == 2
