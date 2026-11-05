"""#83 定向测试：真实原生 TOTP、真实生成公私钥和 WebAuthn 签名。

独立测试与网关测试均走真实 W0 存储及当前已签发上下文，现场生成全部密钥。
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import struct
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from agent_py_agent.agent.business_spaces import v13_w10_identity as identity
from agent_py_agent.agent.business_spaces import v13_w10_mfa as mfa
from agent_py_agent.agent.conversation.business_access import BusinessError
from agent_py_agent.agent.gateway_parts import business_api as api
from agent_py_agent.agent.gateway_parts.business_api_v13 import V13Call
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.user_space.local_user_lifecycle import LocalUserLifecycle
from agent_py_agent.agent.user_space.local_user_store import LocalUserStore
from agent_py_agent.agent.user_space.totp_sso_auth import _read, totp_code
from agent_py_agent.tests.test_v13_w0_harness import v13_native as v13_native


# LLM: 所有原生操作只使用临时 HOME，不触碰真实账号或服务。
# 函数用途: 创建普通用户和 live context，使用完整真实存储事务。
@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path))
    root = tmp_path / "native"
    lifecycle = LocalUserLifecycle(LocalUserStore(root))
    for user in ("alice", "bob"):
        lifecycle.create(user, idempotency_key="test-mfa-create-" + user)
    clock, checks, allowed = [1800000000.0], [], [True]

    # LLM: 此检查点记录真实 identity.write 是否在副作用前调用 live context。
    # 函数用途: 记录测试调用的实时校验。
    def authorize(action, facts):
        checks.append((action, facts))
        return allowed[0]

    # LLM: 构造 F10 同形调用；不伪造业务响应或签名结果。
    # 函数用途: 创建有独立幂等键的 handler 调用。
    def call(action, params=None, *, owner="local/alice", subject=None, key=None, revision=None):
        from agent_py_agent.agent.user_space.home_layout import home_paths
        from agent_py_agent.agent.user_space.owner_resolver import (
            home_paths_with_owner,
            resolve_owner_home,
        )
        resolved = resolve_owner_home(root) if owner == "local/main" else LocalUserStore(root).owner_home(owner.removeprefix("local/"))
        paths = home_paths_with_owner(home_paths(root), resolved)
        host = SimpleNamespace(home_paths=paths, config=SimpleNamespace(business_spaces_enabled=True))
        context = api.bind_business_context(host, authorize=authorize, subject_id=subject or owner)
        return V13Call(action=action, params=params or {}, request_id="test-request", owner_id=owner,
            subject_id=subject or owner, owner_home=paths.owner_home_dir, context=context,
            idempotency_key=key or secrets.token_hex(16), expected_revision=revision,
            clock=lambda: clock[0], services=SimpleNamespace(owner_id=owner))

    return SimpleNamespace(root=root, clock=clock, call=call, checks=checks, allowed=allowed)


# LLM: 编码器只生成标准认证器测试字节，不替代产品解析器。
# 函数用途: 编码测试使用的 CBOR 主类型。
def cbor(value):
    # LLM: 长度选择按二进制范围，不走产品 CBOR 实现。
    # 函数用途: 构建 CBOR 头。
    def head(major, size):
        if size < 24:
            return bytes([major * 32 + size])
        if size <= 255:
            return bytes([major * 32 + 24, size])
        if size <= 65535:
            return bytes([major * 32 + 25]) + struct.pack(">H", size)
        return bytes([major * 32 + 26]) + struct.pack(">I", size)

    if type(value) is int:
        return head(0, value) if value >= 0 else head(1, -1 - value)
    if type(value) is bytes:
        return head(2, len(value)) + value
    if type(value) is str:
        raw = value.encode()
        return head(3, len(raw)) + raw
    if isinstance(value, list):
        return head(4, len(value)) + b"".join(cbor(item) for item in value)
    if isinstance(value, dict):
        return head(5, len(value)) + b"".join(cbor(key) + cbor(item) for key, item in value.items())
    raise TypeError("测试编码类型无效")


# LLM: 测试编码独立于产品 base64 实现。
# 函数用途: 编码浏览器 JSON 的二进制字段。
def b64(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


# LLM: 每次测试现场生成密钥，仓库不含真实或固定私钥。
# 函数用途: 创建 ES256/RS256 私钥与 COSE 公钥字节。
def keypair(algorithm):
    if algorithm == "ES256":
        key = ec.generate_private_key(ec.SECP256R1())
        numbers = key.public_key().public_numbers()
        cose = {1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big")}
    else:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = key.public_key().public_numbers()
        cose = {1: 3, 3: -257, -1: numbers.n.to_bytes(256, "big"), -2: numbers.e.to_bytes(3, "big")}
    return key, cbor(cose)


# LLM: 使用密码库签出真实签名，不将 verified 布尔当作凭据。
# 函数用途: 为测试认证器签署 ES256/RS256 数据。
def sign(key, raw):
    if isinstance(key, ec.EllipticCurvePrivateKey):
        return key.sign(raw, ec.ECDSA(hashes.SHA256()))
    return key.sign(raw, padding.PKCS1v15(), hashes.SHA256())


# LLM: 模拟浏览器原始 clientDataJSON 字节，签名必须绑定这些准确字节。
# 函数用途: 构造创建或认证的客户端数据。
def client_data(ticket, purpose, *, origin="https://login.example.test", challenge=None):
    return json.dumps({"type": "webauthn.create" if purpose == "register" else "webauthn.get",
        "challenge": challenge or ticket["options"]["challenge"], "origin": origin}, separators=(",", ":")).encode()


# LLM: 标准 none 与 packed self-attestation 现场构造，产品负责真实解析校验。
# 函数用途: 生成注册凭证及其私钥上下文。
def registration(ticket, algorithm="ES256", fmt="none", *, count=0):
    key, cose = keypair(algorithm)
    identifier = secrets.token_bytes(24)
    client = client_data(ticket, "register")
    auth = hashlib.sha256(b"example.test").digest() + b"\x41" + struct.pack(">I", count)
    auth += bytes(16) + struct.pack(">H", len(identifier)) + identifier + cose
    statement = {} if fmt == "none" else {"alg": -7 if algorithm == "ES256" else -257,
        "sig": sign(key, auth + hashlib.sha256(client).digest())}
    credential = {"id": b64(identifier), "rawId": b64(identifier), "type": "public-key",
        "response": {"clientDataJSON": b64(client),
            "attestationObject": b64(cbor({"fmt": fmt, "authData": auth, "attStmt": statement}))}}
    return credential, key


# LLM: 签名覆盖实际 flags、RP hash、计数和 clientDataJSON，不固定通过值。
# 函数用途: 生成真实 WebAuthn 认证断言。
def assertion(ticket, credential_id, key, *, count=1, origin="https://login.example.test", challenge=None,
              rp="example.test", flags=1, subject="local/alice"):
    client = client_data(ticket, "authenticate", origin=origin, challenge=challenge)
    auth = hashlib.sha256(rp.encode()).digest() + bytes([flags]) + struct.pack(">I", count)
    return {"id": credential_id, "rawId": credential_id, "type": "public-key", "response": {
        "clientDataJSON": b64(client), "authenticatorData": b64(auth),
        "signature": b64(sign(key, auth + hashlib.sha256(client).digest())), "userHandle": b64(subject.encode())}}


# LLM: 测试助手组织真实handler调用；标签由客户端按正式字段传入finish，不依赖挑战私有字段。
# 函数用途: 创建注册挑战并完成一把凭证注册。
def register(env, algorithm="ES256", fmt="none", *, count=0):
    ticket = mfa.passkey_register(env.call("auth.passkeys.register", {"phase": "begin",
        "rp_id": "example.test", "origin": "https://login.example.test", "label": "测试设备"}))
    credential, key = registration(ticket, algorithm, fmt, count=count)
    finish = env.call("auth.passkeys.register", {"phase": "finish", "challenge_id": ticket["challenge_id"],
        "credential": credential, "label": "测试设备"})
    result = mfa.passkey_register(finish)
    return result["passkey"], key, ticket, finish


# LLM: 每次挑战独立请求；相同凭证只能使用所属主体上下文。
# 函数用途: 生成认证 begin 结果。
def auth_begin(env, *, subject=None, owner="local/alice"):
    return mfa.passkey_authenticate(env.call("auth.passkeys.authenticate", {"phase": "begin",
        "rp_id": "example.test", "origin": "https://login.example.test"}, owner=owner, subject=subject))


# LLM: 只按稳定错误码和状态断言，不依赖自然语言消息。
# 函数用途: 检查准确业务拒绝。
def rejected(code, status, function, call):
    with pytest.raises((HostAPIError, BusinessError)) as failure:
        function(call)
    assert failure.value.code == code
    if isinstance(failure.value, HostAPIError):
        assert failure.value.status == status


# LLM: 空参数例子与 main 默认状态都不创建种子。
# 函数用途: 验证状态及内嵌策略。
def test_status_example_empty_params_and_policy_included(env):
    result = mfa.status(env.call("auth.mfa.status"))
    assert result == {"enabled": False, "pending": False, "recovery_codes_remaining": 0, "passkeys": 0,
        "policy": {"required": False, "enforce_for": "all", "grace_days": 0, "revision": 0, "updated_at": None}}
    assert mfa.status(env.call("auth.mfa.status", owner="local/main")) == result
    assert not (env.root / "local_users_auth" / "alice.json").exists()


# LLM: 真实原生存储保持密文，持久化回执只能是脱敏结果。
# 函数用途: 验证秘密仅首次显示及重放脱敏。
def test_enroll_begin_secret_once_and_replay_redacted(env):
    call = env.call("auth.mfa.enroll_begin")
    first = mfa.enroll_begin(call)
    assert len(base64.b32decode(first["secret"])) == 20
    assert first["secret"] in first["otpauth_uri"] and len(first["recovery_codes"]) == 8
    assert mfa.status(env.call("auth.mfa.status"))["pending"] is True
    replay = mfa.enroll_begin(call)
    assert replay["secret"] == replay["otpauth_uri"] == "<redacted>"
    assert replay["recovery_codes"] == ["<redacted>"] * 8
    stored = (env.root / "local_users_auth" / "alice.json").read_text()
    receipt = (call.owner_home / "data" / "business_v13" / "webauthn_challenges.json").read_text()
    for secret in [first["secret"], *first["recovery_codes"]]:
        assert secret not in stored and secret not in receipt
    assert "secret_ciphertext" in stored and env.checks


# LLM: 以实际生成种子和可信时钟计算 RFC6238 六位码。
# 函数用途: 验证首码激活、相同请求幂等和不同请求同码拒绝。
def test_confirm_with_rfc6238_code_enables_and_replay_is_conflict(env):
    setup = mfa.enroll_begin(env.call("auth.mfa.enroll_begin"))
    code = totp_code(setup["secret"], int(env.clock[0]) // 30)
    call = env.call("auth.mfa.enroll_confirm", {"code": code})
    assert mfa.enroll_confirm(call) == {"enabled": True}
    assert mfa.enroll_confirm(call) == {"enabled": True}
    rejected("STATE_CONFLICT", 409, mfa.enroll_confirm, env.call("auth.mfa.enroll_confirm", {"code": code}))
    rejected("STATE_CONFLICT", 409, mfa.enroll_begin, env.call("auth.mfa.enroll_begin"))
    assert mfa.status(env.call("auth.mfa.status"))["enabled"] is True
    assert mfa.status(env.call("auth.mfa.status"))["pending"] is False


# LLM: 限速、恢复码及码窗口均由原生认证实际落盘核验。
# 函数用途: 验证动态码、恢复码单次消费和原生锁定映射。
def test_verify_code_recovery_code_lockout(env):
    setup = mfa.enroll_begin(env.call("auth.mfa.enroll_begin"))
    code = totp_code(setup["secret"], int(env.clock[0]) // 30)
    mfa.enroll_confirm(env.call("auth.mfa.enroll_confirm", {"code": code}))
    env.clock[0] += 30
    code = totp_code(setup["secret"], int(env.clock[0]) // 30)
    assert mfa.verify(env.call("auth.mfa.verify", {"code": code})) == {"verified": True, "recovery_code_used": False}
    rejected("STATE_CONFLICT", 409, mfa.verify, env.call("auth.mfa.verify", {"code": code}))
    recovery = setup["recovery_codes"][0]
    assert mfa.verify(env.call("auth.mfa.verify", {"code": recovery}))["recovery_code_used"] is True
    assert mfa.status(env.call("auth.mfa.status"))["recovery_codes_remaining"] == 7
    rejected("VERIFICATION_FAILED", 403, mfa.verify, env.call("auth.mfa.verify", {"code": recovery}))
    for _ in range(9):
        rejected("VERIFICATION_FAILED", 403, mfa.verify, env.call("auth.mfa.verify", {"code": "invalid"}))
    rejected("RATE_LIMITED", 429, mfa.verify, env.call("auth.mfa.verify", {"code": setup["recovery_codes"][1]}))
    env.clock[0] += 301
    assert mfa.verify(env.call("auth.mfa.verify", {"code": setup["recovery_codes"][1]}))["verified"] is True


# LLM: 使用真实管理员决策替身只用于本层测试，CAS 仍走父级真实 helper。
# 函数用途: 验证策略主租户共享、原生 admin 限制和行 CAS。
def test_policy_set_admin_only_cas(env):
    rejected("BUSINESS_ACCESS_DENIED", 403, mfa.set_policy,
        env.call("auth.mfa.policy.set", {"required": True}, revision=0))
    call = env.call("auth.mfa.policy.set", {"required": True, "enforce_for": "admins", "grace_days": 3}, owner="local/main", revision=0)
    result = mfa.set_policy(call)
    assert result["policy"]["revision"] == 1 and result["policy"]["required"] is True
    assert mfa.set_policy(call) == result
    assert mfa.status(env.call("auth.mfa.status"))["policy"] == result["policy"]
    rejected("REVISION_CONFLICT", 409, mfa.set_policy,
        env.call("auth.mfa.policy.set", {"required": False}, owner="local/main", revision=0))
    assert mfa.set_policy(env.call("auth.mfa.policy.set", {"required": False}, owner="local/main", revision=1))["policy"]["revision"] == 2


# LLM: 停用不能由客户端布尔绕过第二因素，原生 epoch 变更仍由原服务处理。
# 函数用途: 验证停用必须消费有效恢复码或动态码。
def test_disable_requires_valid_second_factor(env):
    setup = mfa.enroll_begin(env.call("auth.mfa.enroll_begin"))
    mfa.enroll_confirm(env.call("auth.mfa.enroll_confirm", {"code": totp_code(setup["secret"], int(env.clock[0]) // 30)}))
    rejected("VERIFICATION_FAILED", 403, mfa.disable, env.call("auth.mfa.disable", {"code": "invalid"}))
    assert mfa.disable(env.call("auth.mfa.disable", {"code": setup["recovery_codes"][0]})) == {"enabled": False}
    assert mfa.status(env.call("auth.mfa.status"))["enabled"] is False


# LLM: 重置目标为真实原生账号，普通主体不能越权。
# 函数用途: 验证管理员重置另一用户及不存在用户拒绝。
def test_admin_reset_other_user(env):
    mfa.enroll_begin(env.call("auth.mfa.enroll_begin"))
    rejected("BUSINESS_ACCESS_DENIED", 403, mfa.reset, env.call("auth.mfa.reset", {"user_id": "alice"}))
    result = mfa.reset(env.call("auth.mfa.reset", {"user_id": "alice"}, owner="local/main"))
    assert result == {"user_id": "alice", "enabled": False}
    assert "secret_ciphertext" not in _read(env.root, "alice.json")
    rejected("RESOURCE_NOT_FOUND", 404, mfa.reset,
        env.call("auth.mfa.reset", {"user_id": "absent"}, owner="local/main"))


# LLM: 两算法与两种 attestation 组合均走现场密钥和真实验签。
# 函数用途: 验证完整注册认证、label、幂等回放、列表和删除。
@pytest.mark.parametrize("algorithm", ["ES256", "RS256"])
@pytest.mark.parametrize("fmt", ["none", "packed"])
def test_passkey_register_and_authenticate_es256_round_trip(env, algorithm, fmt):
    view, key, ticket, finish = register(env, algorithm, fmt)
    assert view["algorithm"] == algorithm and view["label"] == "测试设备" and view["sign_count"] == 0
    assert mfa.passkey_register(finish)["passkey"] == view
    rejected("STATE_CONFLICT", 409, mfa.passkey_register,
        env.call("auth.passkeys.register", finish.params))
    assert len(base64.urlsafe_b64decode(ticket["options"]["challenge"] + "=")) == 32
    begin = auth_begin(env)
    assert begin["options"]["allowCredentials"] == [{"id": view["credential_id"], "type": "public-key"}]
    credential = assertion(begin, view["credential_id"], key)
    call = env.call("auth.passkeys.authenticate", {"phase": "finish", "challenge_id": begin["challenge_id"], "credential": credential})
    result = mfa.passkey_authenticate(call)
    assert result == {"phase": "finish", "challenge_id": None, "options": None, "verified": True, "credential_id": view["credential_id"]}
    assert mfa.passkey_authenticate(call) == result
    assert mfa.passkey_list(env.call("auth.passkeys.list"))["items"][0]["sign_count"] == 1
    assert mfa.status(env.call("auth.mfa.status"))["passkeys"] == 1


# LLM: 同 owner 不同 subject 也不能读取或吊销凭证。
# 函数用途: 验证列表隔离、删除回放和删除后认证拒绝。
def test_passkeys_list_and_delete(env):
    view, key, _, _ = register(env)
    identifier = view["credential_id"]
    assert mfa.passkey_list(env.call("auth.passkeys.list", subject="other")) == {"items": []}
    rejected("RESOURCE_NOT_FOUND", 404, mfa.passkey_delete,
        env.call("auth.passkeys.delete", {"credential_id": identifier}, subject="other"))
    begin = auth_begin(env)
    call = env.call("auth.passkeys.delete", {"credential_id": identifier})
    assert mfa.passkey_delete(call) == {"credential_id": identifier, "deleted": True}
    assert mfa.passkey_delete(call)["deleted"] is True
    assert mfa.passkey_list(env.call("auth.passkeys.list")) == {"items": []}
    rejected("RESOURCE_NOT_FOUND", 404, mfa.passkey_delete, env.call("auth.passkeys.delete", call.params))
    rejected("VERIFICATION_FAILED", 403, mfa.passkey_authenticate, env.call("auth.passkeys.authenticate", {
        "phase": "finish", "challenge_id": begin["challenge_id"], "credential": assertion(begin, identifier, key)}))


# LLM: 逐个更改被签名绑定的字段，签名仍由真实私钥生成。
# 函数用途: 验证错误 origin/challenge/RP/UP/type/签名/userHandle 都拒绝。
@pytest.mark.parametrize("field", ["origin", "challenge", "rp", "flags", "signature", "type", "subject", "rawId", "crossOrigin"])
def test_authentication_rejects_invalid_browser_binding(env, field):
    view, key, _, _ = register(env)
    begin = auth_begin(env)
    options = {}
    if field in {"origin", "challenge", "rp", "flags", "subject"}:
        options[field] = {"origin": "https://other.example.test", "challenge": "wrong",
            "rp": "other.test", "flags": 0, "subject": "other"}[field]
    credential = assertion(begin, view["credential_id"], key, **options)
    if field == "signature":
        credential["response"]["signature"] = b64(bytes(64))
    if field == "type":
        credential["type"] = "other"
    if field == "rawId":
        credential["rawId"] = b64(b"other")
    if field == "crossOrigin":
        raw = json.loads(base64.urlsafe_b64decode(credential["response"]["clientDataJSON"] + "=="))
        raw["crossOrigin"] = True
        credential["response"]["clientDataJSON"] = b64(json.dumps(raw).encode())
    call = env.call("auth.passkeys.authenticate", {"phase": "finish", "challenge_id": begin["challenge_id"], "credential": credential})
    rejected("VERIFICATION_FAILED", 403, mfa.passkey_authenticate, call)
    assert mfa.passkey_list(env.call("auth.passkeys.list"))["items"][0]["sign_count"] == 0
    rejected("STATE_CONFLICT", 409, mfa.passkey_authenticate, env.call("auth.passkeys.authenticate", call.params))


# LLM: 主体绑定检查不能只依赖 owner 目录，注册和认证都检查。
# 函数用途: 验证挑战跨主体、跨 owner、用途与五分钟边界。
@pytest.mark.parametrize("case", ["subject", "owner", "expired", "purpose", "unknown"])
def test_challenge_scope_single_use_and_five_minute_expiry(env, case):
    view, key, _, _ = register(env)
    begin = auth_begin(env)
    credential = assertion(begin, view["credential_id"], key)
    params = {"phase": "finish", "challenge_id": begin["challenge_id"], "credential": credential}
    options, function, action = {}, mfa.passkey_authenticate, "auth.passkeys.authenticate"
    if case == "subject":
        options["subject"] = "other"
    elif case == "owner":
        options["owner"] = "local/bob"
    elif case == "expired":
        env.clock[0] += 300
    elif case == "purpose":
        function, action = mfa.passkey_register, "auth.passkeys.register"
    else:
        params["challenge_id"] = "absent-challenge"
    rejected("STATE_CONFLICT", 409, function, env.call(action, params, **options))


# LLM: 认证器始终为零可以重复认证，但一旦非零不得回退或不增长。
# 函数用途: 验证 sign_count 的严格增长及全零例外。
def test_sign_count_monotonic_and_zero_authenticator(env):
    view, key, _, _ = register(env)
    for count in (0, 0, 1, 2):
        ticket = auth_begin(env)
        assert mfa.passkey_authenticate(env.call("auth.passkeys.authenticate", {"phase": "finish",
            "challenge_id": ticket["challenge_id"], "credential": assertion(ticket, view["credential_id"], key, count=count)}))["verified"]
    for count in (0, 1, 2):
        ticket = auth_begin(env)
        rejected("VERIFICATION_FAILED", 403, mfa.passkey_authenticate, env.call("auth.passkeys.authenticate", {
            "phase": "finish", "challenge_id": ticket["challenge_id"],
            "credential": assertion(ticket, view["credential_id"], key, count=count)}))
    assert mfa.passkey_list(env.call("auth.passkeys.list"))["items"][0]["sign_count"] == 2


# LLM: 同一计数的不同挑战并发完成必须最多一个提交。
# 函数用途: 用确定性屏障验证锁内计数检查而不是旧快照覆盖。
def test_concurrent_assertions_only_one_counter_commit(env):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    view, key, _, _ = register(env)
    calls = []
    for _ in range(2):
        ticket = auth_begin(env)
        calls.append(env.call("auth.passkeys.authenticate", {"phase": "finish", "challenge_id": ticket["challenge_id"],
            "credential": assertion(ticket, view["credential_id"], key)}))
    barrier = Barrier(2)

    # LLM: 并发只控制交错时点，不替换验签或最终事务。
    # 函数用途: 同时提交两条相同计数的真实断言。
    def run(call):
        barrier.wait(timeout=5)
        try:
            return mfa.passkey_authenticate(call)["verified"]
        except HostAPIError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, calls))
    assert results.count(True) == 1 and results.count("VERIFICATION_FAILED") == 1


# LLM: 原生动态码并发也必须最多消费一次，不从替身里模拟码结果。
# 函数用途: 验证不同幂等请求同码并发使用只允许一次。
def test_concurrent_totp_code_only_one_commit(env):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    setup = mfa.enroll_begin(env.call("auth.mfa.enroll_begin"))
    mfa.enroll_confirm(env.call("auth.mfa.enroll_confirm", {"code": totp_code(setup["secret"], int(env.clock[0]) // 30)}))
    env.clock[0] += 30
    code, barrier = totp_code(setup["secret"], int(env.clock[0]) // 30), Barrier(2)
    calls = [env.call("auth.mfa.verify", {"code": code}) for _ in range(2)]

    # LLM: 原生账保持实际落盘，测试只协调线程开始时间。
    # 函数用途: 并发提交同一个真实 TOTP 码。
    def run(call):
        barrier.wait(timeout=5)
        try:
            return mfa.verify(call)["verified"]
        except HostAPIError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, calls))
    assert results.count(True) == 1 and results.count("STATE_CONFLICT") == 1


# LLM: 以合法 CBOR 包装错误证明，不能因结构可解析而放过签名验证。
# 函数用途: 验证注册格式、self-attestation、ID 以及解析边界。
@pytest.mark.parametrize("case", ["bad_packed", "x5c", "fmt", "id", "up", "truncated", "duplicate_cbor", "client_type"])
def test_registration_rejects_invalid_proof(env, case):
    ticket = mfa.passkey_register(env.call("auth.passkeys.register", {"phase": "begin", "rp_id": "example.test", "origin": "https://login.example.test"}))
    credential, _ = registration(ticket, fmt="packed")
    raw = base64.urlsafe_b64decode(credential["response"]["attestationObject"] + "==")
    att, _ = mfa._cbor(raw)
    if case == "bad_packed":
        att["attStmt"]["sig"] = bytes(64)
    elif case == "x5c":
        att["attStmt"]["x5c"] = [b"certificate"]
    elif case == "fmt":
        att["fmt"] = "unsupported"
    elif case == "id":
        credential["id"] = credential["rawId"] = b64(b"different-id")
    elif case == "up":
        att["authData"] = att["authData"][:32] + b"\x40" + att["authData"][33:]
    elif case == "client_type":
        client = json.loads(base64.urlsafe_b64decode(credential["response"]["clientDataJSON"] + "=="))
        client["type"] = "webauthn.get"
        credential["response"]["clientDataJSON"] = b64(json.dumps(client).encode())
    if case == "truncated":
        encoded = raw[:-1]
    elif case == "duplicate_cbor":
        encoded = b"\xa2\x61a\x01\x61a\x02"
    else:
        encoded = cbor(att)
    credential["response"]["attestationObject"] = b64(encoded)
    rejected("VERIFICATION_FAILED", 403, mfa.passkey_register, env.call("auth.passkeys.register", {
        "phase": "finish", "challenge_id": ticket["challenge_id"], "credential": credential}))
    assert mfa.passkey_list(env.call("auth.passkeys.list")) == {"items": []}


# LLM: 同键不同参数必须由真实共享 write 的摘要拒绝，不能重发秘密或挑战。
# 函数用途: 验证幂等参数冲突及实时校验拒绝。
def test_idempotency_conflict_and_live_context(env):
    begin = env.call("auth.passkeys.register", {"phase": "begin", "rp_id": "example.test", "origin": "https://login.example.test"})
    mfa.passkey_register(begin)
    changed = env.call("auth.passkeys.register", {**begin.params, "label": "other"}, key=begin.idempotency_key)
    rejected("IDEMPOTENCY_CONFLICT", 409, mfa.passkey_register, changed)
    call = env.call("auth.mfa.enroll_begin")

    env.allowed[0] = False
    rejected("CAPABILITY_DENIED", 403, mfa.enroll_begin, call)
    assert not (env.root / "local_users_auth" / "alice.json").exists()


# LLM: 业务响应只能来自真实 dispatcher，包含当前注册表 schema 校验与持久化事务。
# 函数用途: 验证 #83 全部动作的 completed/error=null 信封和关键错误状态。
def test_envelopes(env):
    import time

    # LLM: 无替代响应；直接进入 HTTP 使用的相同业务执行入口。
    # 函数用途: 发送真实封闭请求并检查标准响应。
    def invoke(action, params=None, *, owner="local/alice", key=None, revision=None, expected=200, code=None):
        call = env.call(action, params, owner=owner)
        request = {"action": action, "request_id": secrets.token_hex(8), "params": params or {}}
        if action not in {"auth.mfa.status", "auth.passkeys.list"}:
            request["idempotency_key"] = key or secrets.token_hex(16)
        if revision is not None:
            request["expected_revision"] = revision
        status, body = api.execute_business(request, call.context, api.BusinessServices(call.context.host))
        assert status == expected, body
        assert set(body) == {"request_id", "operation_id", "state", "data", "error", "allowed_actions", "warnings", "effect_outcome"}
        if expected == 200:
            assert body["error"] is None and body["state"] == "completed"
        else:
            assert body["error"]["code"] == code and body["state"] == "failed" and body["data"] == {}
        return body["data"]

    invoke("auth.mfa.status")
    setup = invoke("auth.mfa.enroll_begin", key="wire-secret-once-request")
    replay = invoke("auth.mfa.enroll_begin", key="wire-secret-once-request")
    assert setup["secret"] != "<redacted>" and replay["secret"] == "<redacted>"
    code = totp_code(setup["secret"], int(time.time()) // 30)
    assert invoke("auth.mfa.enroll_confirm", {"code": code}) == {"enabled": True}
    invoke("auth.mfa.verify", {"code": code}, expected=409, code="STATE_CONFLICT")
    invoke("auth.mfa.verify", {"code": "invalid"}, expected=403, code="VERIFICATION_FAILED")
    assert invoke("auth.mfa.verify", {"code": setup["recovery_codes"][0]})["recovery_code_used"]
    assert invoke("auth.mfa.disable", {"code": setup["recovery_codes"][1]}) == {"enabled": False}
    invoke("auth.mfa.policy.set", {"required": True}, revision=0, expected=403, code="BUSINESS_ACCESS_DENIED")
    policy = invoke("auth.mfa.policy.set", {"required": True}, owner="local/main", revision=0)
    assert policy["policy"]["revision"] == 1
    invoke("auth.mfa.policy.set", {"required": False}, owner="local/main", revision=0, expected=409, code="REVISION_CONFLICT")
    assert invoke("auth.mfa.reset", {"user_id": "alice"}, owner="local/main")["enabled"] is False
    begin = invoke("auth.passkeys.register", {"phase": "begin", "rp_id": "example.test", "origin": "https://login.example.test"})
    credential, private_key = registration(begin, "RS256", "packed")
    view = invoke("auth.passkeys.register", {"phase": "finish", "challenge_id": begin["challenge_id"], "credential": credential})["passkey"]
    assert invoke("auth.passkeys.list")["items"] == [view]
    begin = invoke("auth.passkeys.authenticate", {"phase": "begin", "rp_id": "example.test", "origin": "https://login.example.test"})
    assert invoke("auth.passkeys.authenticate", {"phase": "finish", "challenge_id": begin["challenge_id"],
        "credential": assertion(begin, view["credential_id"], private_key)})["verified"]
    assert invoke("auth.passkeys.delete", {"credential_id": view["credential_id"]})["deleted"]
    invoke("auth.passkeys.delete", {"credential_id": view["credential_id"]}, expected=404, code="RESOURCE_NOT_FOUND")
    invoke("auth.mfa.status", {"extra": True}, expected=400, code="INVALID_SCHEMA")


# LLM: 只在临时 echo Gateway 测试，web 头沿真实中间件解析，不修改共享 harness。
# 函数用途: 经真实 /client/business HTTP 完成 WebAuthn 注册认证及吊销。
def test_http_client_business_passkey_round_trip(v13_native):
    import http.client
    server, _ = v13_native

    # LLM: 每条请求新建连接，身份只通过已认证的标准头传递。
    # 函数用途: 发送真实 Web HTTP 请求并解码信封。
    def invoke(action, params=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server.server_address[1], timeout=20)
        request = {"action": action, "request_id": secrets.token_hex(8), "params": params or {}}
        if action not in {"auth.mfa.status", "auth.passkeys.list"}:
            request["idempotency_key"] = secrets.token_hex(16)
        try:
            connection.request("POST", "/client/business", json.dumps(request), {
                "Content-Type": "application/json", "X-Gateway-Token": server._local_client_credential,
                "X-User-Id": "local", "X-Channel": "web"})
            response = connection.getresponse()
            body = json.loads(response.read())
            assert response.status == 200, body
            assert body["state"] == "completed" and body["error"] is None
            return body["data"]
        finally:
            connection.close()

    assert invoke("auth.mfa.status")["enabled"] is False
    begin = invoke("auth.passkeys.register", {"phase": "begin", "rp_id": "example.test", "origin": "https://login.example.test"})
    subject = begin["options"]["user"]["name"]
    credential, key = registration(begin)
    identifier = invoke("auth.passkeys.register", {"phase": "finish", "challenge_id": begin["challenge_id"], "credential": credential})["passkey"]["credential_id"]
    begin = invoke("auth.passkeys.authenticate", {"phase": "begin", "rp_id": "example.test", "origin": "https://login.example.test"})
    assert invoke("auth.passkeys.authenticate", {"phase": "finish", "challenge_id": begin["challenge_id"],
        "credential": assertion(begin, identifier, key, subject=subject)})["verified"]
    assert invoke("auth.passkeys.list")["items"][0]["sign_count"] == 1
    assert invoke("auth.passkeys.delete", {"credential_id": identifier})["deleted"]


# LLM: 日志提交后物化中断必须同时保留挑战消费和公钥，不能重做注册。
# 函数用途: 对真实多集合事务注入一次物化故障，验证同键恢复及逻辑原子视图。
def test_atomic_challenge_key_journal_recovery(env, monkeypatch):
    from agent_py_agent.agent.business_spaces import v13_store
    ticket = mfa.passkey_register(env.call("auth.passkeys.register", {"phase": "begin", "rp_id": "example.test", "origin": "https://login.example.test"}))
    credential, _ = registration(ticket)
    call = env.call("auth.passkeys.register", {"phase": "finish", "challenge_id": ticket["challenge_id"], "credential": credential})
    original, failed = v13_store.write_bytes_atomic_beneath, [False]

    # LLM: 只在提交点之后的一次公钥文件物化中断，不改变业务判断。
    # 函数用途: 模拟 journal 已提交后的单次磁盘写失败。
    def interrupted(root, parts, raw):
        if parts[-1] == "passkeys.json" and not failed[0]:
            failed[0] = True
            raise OSError("测试提交后中断")
        return original(root, parts, raw)

    monkeypatch.setattr(v13_store, "write_bytes_atomic_beneath", interrupted)
    with pytest.raises(OSError):
        mfa.passkey_register(call)
    assert identity.one(call, "webauthn_challenges", ticket["challenge_id"])["used"] is True
    assert identity.one(call, "passkeys", credential["id"])["credential_id"] == credential["id"]
    result = mfa.passkey_register(call)
    assert result["passkey"]["credential_id"] == credential["id"]
    directory = call.owner_home / "data" / "business_v13"
    assert json.loads((directory / ".transaction.json").read_text()) == {}
    challenges, keys = [json.loads((directory / name).read_text()) for name in ("webauthn_challenges.json", "passkeys.json")]
    assert challenges['receipts'] == keys['receipts'] == {}
    receipts = []
    for collection in ('webauthn_challenges', 'passkeys'):
        rows = identity.store(call)._rows(collection)
        committed = {}
        for path in (directory / 'history' / collection / 'receipts').glob('*.json'):
            row = rows.decode(path.read_bytes(), 'receipts')
            assert rows.parts('receipts', row['key'])[-1] == path.name
            committed[row['key']] = rows.get('receipts', row['key'])
        receipts.append(committed)
    common = set(receipts[0]) & set(receipts[1])
    assert len(common) == 1
    receipt_key = common.pop()
    assert receipts[0][receipt_key] == receipts[1][receipt_key]
    assert receipts[0][receipt_key]['result'] == result
    assert len(keys["records"]) == 1


# LLM: 外部身份 subject 即使与普通账号同名，也不能读写该账号 TOTP。
# 函数用途: 验证 TOTP 仅适用于精确 local owner，不受展示名称碰撞影响。
def test_nonlocal_subject_never_selects_native_totp_account(env):
    from dataclasses import replace
    setup = mfa.enroll_begin(env.call("auth.mfa.enroll_begin"))
    mfa.enroll_confirm(env.call("auth.mfa.enroll_confirm", {"code": totp_code(setup["secret"], int(env.clock[0]) // 30)}))
    original = env.call("auth.mfa.status")
    owner = "providers/feishu/users/alice"
    host = SimpleNamespace(home_paths=SimpleNamespace(root=env.root, owner_id=owner,
        owner_home_dir=env.root / "external-owner", owner_provider="feishu", owner_kind="users"),
        config=SimpleNamespace(business_spaces_enabled=True))
    context = api.bind_business_context(host, authorize=lambda action, facts: True, subject_id="alice")
    call = replace(original, owner_id=owner, subject_id="alice", owner_home=host.home_paths.owner_home_dir,
        context=context, services=SimpleNamespace(owner_id=owner))
    result = mfa.status(call)
    assert result["enabled"] is False and result["recovery_codes_remaining"] == 0
    rejected("BUSINESS_ACCESS_DENIED", 403, mfa.enroll_begin, replace(call, action="auth.mfa.enroll_begin"))
    assert mfa.status(env.call("auth.mfa.status"))["enabled"] is True


# LLM: Base64内的JSON不受请求外层深度约束；过深数据也必须原子消费一次性挑战。
# 函数用途: 验证递归解析拒绝不会回滚挑战消费。
@pytest.mark.parametrize("purpose", ["register", "authenticate"])
def test_deep_client_data_is_rejected_and_consumes_challenge(env, purpose):
    if purpose == "authenticate":
        view, key, _, _ = register(env)
        ticket = auth_begin(env)
        credential = assertion(ticket, view["credential_id"], key)
        function = mfa.passkey_authenticate
    else:
        ticket = mfa.passkey_register(env.call("auth.passkeys.register", {"phase": "begin",
            "rp_id": "example.test", "origin": "https://login.example.test"}))
        credential, _ = registration(ticket)
        function = mfa.passkey_register
    credential["response"]["clientDataJSON"] = b64(b'{"extra":' + b'[' * 1500 + b'0' + b']' * 1500 + b'}')
    action = "auth.passkeys." + ("register" if purpose == "register" else "authenticate")
    params = {"phase": "finish", "challenge_id": ticket["challenge_id"], "credential": credential}
    call = env.call(action, params)
    with pytest.raises(HostAPIError) as error:
        function(call)
    assert error.value.code == "VERIFICATION_FAILED" and error.value.outcome == "committed"
    assert identity.one(call, "webauthn_challenges", ticket["challenge_id"])["used"] is True
    rejected("STATE_CONFLICT", 409, function, env.call(action, params))


# LLM: JSON字符串中括号不是结构深度，转义引号也不能改变解析状态。
# 函数用途: 验证有界解析不误拒绝合法客户端扩展字符串。
def test_client_json_brackets_inside_string_are_not_depth(env):
    ticket = mfa.passkey_register(env.call("auth.passkeys.register", {"phase": "begin",
        "rp_id": "example.test", "origin": "https://login.example.test"}))
    credential, _ = registration(ticket)
    data = json.loads(base64.urlsafe_b64decode(credential["response"]["clientDataJSON"] + "=="))
    data["extra"] = '[{\\"' * 80
    credential["response"]["clientDataJSON"] = b64(json.dumps(data).encode())
    result = mfa.passkey_register(env.call("auth.passkeys.register", {"phase": "finish",
        "challenge_id": ticket["challenge_id"], "credential": credential}))
    assert result["passkey"]["credential_id"] == credential["id"]


# LLM: 挑战记录遵守闭合字段合同，设备标签只在finish请求写入正式passkeys记录。
# 函数用途: 验证持久挑战无私有字段并保留完成请求指定的标签。
def test_passkey_finish_label_and_closed_challenge(env):
    begin = env.call("auth.passkeys.register", {"phase": "begin", "rp_id": "example.test",
        "origin": "https://login.example.test", "label": "开始阶段标签"})
    ticket = mfa.passkey_register(begin)
    fields = {"challenge_id", "owner_id", "subject_id", "purpose", "challenge_b64", "rp_id", "origin",
        "used", "expires_at", "revision", "created_at", "updated_at"}
    assert set(identity.one(begin, "webauthn_challenges", ticket["challenge_id"])) == fields
    credential, _ = registration(ticket)
    finish = env.call(begin.action, {"phase": "finish", "challenge_id": ticket["challenge_id"],
        "credential": credential, "label": "完成阶段标签"})
    result = mfa.passkey_register(finish)
    assert result["passkey"]["label"] == "完成阶段标签"
    assert identity.one(finish, "passkeys", credential["id"])["label"] == "完成阶段标签"
    assert set(identity.one(begin, "webauthn_challenges", ticket["challenge_id"])) == fields
