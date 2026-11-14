"""Wave 3 lossless consolidated tests (test_consolidated_c5_g1).

Sources:
- test_v13_w10_mfa.py: #83 定向测试：真实原生 TOTP、真实生成公私钥和 WebAuthn 签名。
- test_v13_w5_standalone_inventory.py: #52：临时假树核真实目录、文件锁及正常 stop→迁移，不启动服务。
- test_v13_w5_web_integrity_scope.py: W5：写普通脚本不能把整个 owner home 当作网站。
- test_v13_w6_curator_task_file_and_dedupe.py: SYNTHETIC_ONLY：任务指针关联、广播去重和增量规则的隔离回归。
- test_v13_w7_detection_lifecycle.py: 检测生命周期的真实 HTTP 往返、耐久事务、回放与异步回写回归。
- test_v13_w7_scripts.py: #73 固定脚本合同回归：原生资产、持久回执、真实 HTTP 与原审批门。
- test_v13_w7_simulation_sessions.py: W7 桌面推演的真实原生 HTTP 往返与持久状态回归。
"""

from __future__ import annotations

import base64
import builtins
import errno
import hashlib
import json
import os
import secrets
import stat
import struct
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event, Lock, get_ident
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from agent_py_agent.agent.assets.descriptor import AssetDescriptor, IOContract
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.business_spaces import (
    v13_w7_detection_lifecycle as lifecycle,
)
from agent_py_agent.agent.business_spaces import (
    v13_w7_scripts as scripts,
)
from agent_py_agent.agent.business_spaces import (
    v13_w10_identity as identity,
)
from agent_py_agent.agent.business_spaces import (
    v13_w10_mfa as mfa,
)
from agent_py_agent.agent.business_spaces.v13_store import V13Store
from agent_py_agent.agent.common.json_io import locked_json_path
from agent_py_agent.agent.contracts.tool_approval import (
    ToolApprovalDecision,
    build_tool_approval_request,
)
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.business_access import BusinessError
from agent_py_agent.agent.gateway_parts import (
    business_api as api,
)
from agent_py_agent.agent.gateway_parts import (
    standalone_inventory as inventory,
)
from agent_py_agent.agent.gateway_parts import (
    standalone_stop as stop,
)
from agent_py_agent.agent.gateway_parts.business_api_v13 import V13Call
from agent_py_agent.agent.gateway_parts.command_stream_protocol import command_approval_path
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_guard
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.permission_bridge import (
    write_gateway_permission_decision,
)
from agent_py_agent.agent.gateway_parts.standalone_lifecycle import standalone_lifecycle_guard
from agent_py_agent.agent.gateway_parts.standalone_migration_errors import (
    StandaloneMigrationRejected,
)
from agent_py_agent.agent.gateway_parts.standalone_stop import record_standalone_stop
from agent_py_agent.agent.local_storage.tool_operations import (
    ToolOperationClaimRequest,
    ToolOperationCompletionRequest,
    new_tool_operation_holder,
)
from agent_py_agent.agent.memory_store.candidate_models import (
    CandidateObservation,
    MemoryScope,
)
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.curator_commit import CuratorBatchCommitter
from agent_py_agent.agent.memory_store.curator_inputs import (
    CuratorMessageSource,
    CuratorToolReferenceSource,
    collect_curator_inputs,
)
from agent_py_agent.agent.memory_store.curator_models import (
    MemoryCuratorConfig,
    MemoryCuratorState,
)
from agent_py_agent.agent.memory_store.daily import DailyMemoryEvent, DailyMemoryStore
from agent_py_agent.agent.plugin_host.contracts import DomainResult
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.plugin_host.schemas import canonical_json
from agent_py_agent.agent.runtime_db.managed_operation_store import ManagedOperationStore
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import runtime_db_path
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents.static_site import validator
from agent_py_agent.agent.tooling import web_project_integrity as web
from agent_py_agent.agent.tooling.process_session_store import process_session_store_root
from agent_py_agent.agent.user_space.local_user_lifecycle import LocalUserLifecycle
from agent_py_agent.agent.user_space.local_user_store import LocalUserStore
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home
from agent_py_agent.agent.user_space.totp_sso_auth import _read, totp_code
from agent_py_agent.tests import (
    test_business_v12_f9_e2e as native_fixtures,
)
from agent_py_agent.tests import (
    test_v13_w0_harness as harness,
)
from agent_py_agent.tests import (
    test_v13_w0_observation_readers as observation_roundtrip,
)
from agent_py_agent.tests.test_gateway_standalone_migration import _prepare, stopped_home
from agent_py_agent.tests.test_mega_business_api_actions import native_request
from agent_py_agent.tests.test_v13_w0_harness import v13_call, v13_seed, v13_space

v13_host = harness.v13_host


# === Source: agent_py_agent/tests/test_v13_w10_mfa.py ===

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
def test_w10_mfa_envelopes(env):
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


# === Source: agent_py_agent/tests/test_v13_w5_standalone_inventory.py ===

@pytest.fixture
def paths(tmp_path):
    value = gateway_paths_from_root(tmp_path / "gateway")
    value.processing.mkdir(parents=True)
    return value


def _sources(paths):
    namespaces, files = {}, {}
    report = inventory._gateway_sources(paths, namespaces, files)
    return namespaces, files, report


def _local_tree(tmp_path):
    home = tmp_path / "home"
    resolve_owner_home(home).home_dir.mkdir(parents=True)
    for uid in ("qimiaokeju11..88", "real-test-a", "scale-proof"):
        owner = resolve_owner_home(home, OwnerIdentity.local_user(uid))
        owner.home_dir.mkdir()
        canonical = process_session_store_root(owner.home_dir, owner.home_dir)
        canonical.mkdir(parents=True)
        (canonical.parent / uid).mkdir()
    return home


def test_local_users_and_native_namespaces_are_bound(tmp_path):
    home = _local_tree(tmp_path)
    namespaces = {}
    owners = inventory._owners(home, namespaces)
    assert [owner.identity for owner in owners] == [OwnerIdentity.local_main()] + [
        OwnerIdentity.local_user(uid) for uid in ("qimiaokeju11..88", "real-test-a", "scale-proof")]
    inventory._native_namespaces(home, owners, namespaces)
    expected = {root.name for owner in owners[1:] for root in (
        process_session_store_root(owner.home_dir, owner.home_dir),
        process_session_store_root(owner.home_dir, owner.home_dir).parent / owner.identity.owner_id)}
    assert set(namespaces["owners/local/.my-agent-runtime/process_sessions"][1]) == expected


def test_exact_regular_ds_store_ignored_without_opening_nondirectories(tmp_path, monkeypatch):
    home = _local_tree(tmp_path)
    provider = home / "owners/providers/example/users/user-a"
    provider.mkdir(parents=True)
    for parts in ("owners", "owners/local", "owners/providers", "owners/providers/example",
                  "owners/providers/example/users"):
        (home / parts / ".DS_Store").write_bytes(b"finder metadata")
    original = inventory.open_directory_beneath

    def checked(root, parts, **kwargs):
        target = root.joinpath(*parts)
        if target.exists():
            assert stat.S_ISDIR(target.lstat().st_mode), parts
        return original(root, parts, **kwargs)

    monkeypatch.setattr(inventory, "open_directory_beneath", checked)
    namespaces = {}
    assert len(inventory._owners(home, namespaces)) == 5
    assert all(".DS_Store" not in value[1] for value in namespaces.values())


@pytest.mark.parametrize("place", ["owners", "owners/local", "owners/providers"])
@pytest.mark.parametrize("unsafe", ["symlink", "other-file", "invalid-uid", "regular-uid"])
def test_unknown_or_link_owner_entries_never_open_as_directory(tmp_path, monkeypatch, place, unsafe):
    home = _local_tree(tmp_path)
    parent = home / place
    parent.mkdir(exist_ok=True)
    if unsafe == "symlink":
        (parent / ".DS_Store").symlink_to(home / "owners/local/main", target_is_directory=True)
    elif unsafe == "invalid-uid":
        (parent / "-bad-").mkdir()
    else:
        (parent / (".DS_Store.extra" if unsafe == "other-file" else "looks-valid")).write_bytes(b"")
    original = inventory.open_directory_beneath

    def checked(root, parts, **kwargs):
        target = root.joinpath(*parts)
        if target.exists():
            assert stat.S_ISDIR(target.lstat().st_mode), parts
        return original(root, parts, **kwargs)

    monkeypatch.setattr(inventory, "open_directory_beneath", checked)
    with pytest.raises((StandaloneMigrationRejected, OSError, ValueError)):
        inventory._owners(home, {})


def _decision(paths, index=0, *, state="completed", decision="approved"):
    request_id, permission_id = f"gwreq-history-{index}", f"permission-{index}"
    request_hash = hashlib.sha256(request_id.encode()).hexdigest()[:24]
    permission_hash = hashlib.sha256(permission_id.encode()).hexdigest()[:24]
    path = paths.processing / ".approvals" / request_hash / (permission_hash + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {**ToolApprovalDecision(permission_id, decision).to_dict(), "request_id": request_id,
           "binding": {"tool_name": "synthetic", "run_id": "run-history", "operation_id": "op-history",
                       "idempotency_key": "key-history", "args_hash": "a" * 64}}
    if state is not None:
        row["state"] = state
    path.write_text(json.dumps(row))
    return path


def test_31_structurally_settled_approvals_remain_in_place(paths):
    decisions = [_decision(paths, i, decision=("approved", "denied", "cancelled")[i % 3]) for i in range(31)]
    before = {path: path.read_bytes() for path in decisions}
    namespaces, files, report = _sources(paths)
    assert len(files) == 31 and namespaces
    assert all(path.read_bytes() == raw for path, raw in before.items())
    assert _sources(paths) == (namespaces, files, report)


@pytest.mark.parametrize("damage", ["pending", "unknown", "no-decision", "text-only", "bad-binding", "wrong-id"])
def test_unconsumed_or_invalid_approvals_keep_blocking(paths, damage):
    target = _decision(paths, state=None if damage == "pending" else "completed")
    row = json.loads(target.read_text())
    if damage == "unknown":
        row["decision"] = "mystery"
    elif damage == "no-decision":
        del row["decision"]
    elif damage == "text-only":
        row.pop("state")
        row["feedback"] = "completed consumed 已完成"
    elif damage == "bad-binding":
        row["binding"] = {}
    elif damage == "wrong-id":
        row["permission_id"] = "other"
    target.write_text(json.dumps(row))
    with pytest.raises(StandaloneMigrationRejected, match="processing_auxiliary_unsettled"):
        _sources(paths)


def _orphan(paths, *, old=True):
    path = paths.processing / "gwreq-orphan.json.lock"
    path.write_bytes(b"")
    if old:
        os.utime(path, (time.time() - 90000,) * 2)
    return path


def test_orphan_lock_report_is_readonly_and_age_bounded(paths):
    lock = _orphan(paths)
    report = _sources(paths)[2]
    assert report["cleanable_locks"] == ["requests/processing/" + lock.name]
    assert lock.exists()
    os.utime(lock, None)
    assert _sources(paths)[2]["cleanable_locks"] == []
    os.utime(lock, (time.time() - 90000,) * 2)
    lock.with_suffix("").write_text("{}")
    with pytest.raises(StandaloneMigrationRejected, match="processing_request_present"):
        _sources(paths)


def test_normal_stop_cleans_only_unlocked_stale_orphan_then_migrates(tmp_path, monkeypatch):
    case = stopped_home(tmp_path, monkeypatch)
    case.configure(0)
    case.paths.processing.mkdir(parents=True, exist_ok=True)
    stale = _orphan(case.paths)
    fresh = case.paths.processing / "gwreq-fresh.json.lock"
    fresh.write_bytes(b"")
    # 普通预检不核 launcher 也不能删除；只有实际正常 stop 持锁上下文可清理。
    inventory.standalone_inventory(case.home, case.context.config, case.paths, check_launcher=False)
    assert stale.exists()
    cleanup = json.loads(case.paths.state.read_text())["standalone_stop"]["cleanup"]
    with standalone_lifecycle_guard(case.paths.root) as held:
        fact = record_standalone_stop(case.context, held, cleanup)
    assert fact["standalone_stop"]["blockers"] == []
    assert not stale.exists() and fresh.exists()
    case.args, case.config = case.configure(2)
    with coordinator_guard(case.paths.root, 2):
        assert not _prepare(case)._all_quarantined


def test_occupied_stale_lock_not_deleted_on_stop(tmp_path, monkeypatch):
    case = stopped_home(tmp_path, monkeypatch)
    case.configure(0)
    case.paths.processing.mkdir(parents=True, exist_ok=True)
    lock = _orphan(case.paths)
    cleanup = json.loads(case.paths.state.read_text())["standalone_stop"]["cleanup"]
    with locked_json_path(lock.with_suffix("")):
        with standalone_lifecycle_guard(case.paths.root) as held:
            fact = record_standalone_stop(case.context, held, cleanup)
        assert lock.exists()
        assert fact["standalone_stop"]["blockers"]


def test_missing_fcntl_still_fails_closed(paths, monkeypatch):
    target = _decision(paths)
    target.unlink()
    lock = target.with_name(target.name + ".lock")
    lock.write_bytes(b"")
    original = builtins.__import__

    def without_fcntl(name, *args, **kwargs):
        if name == "fcntl":
            raise ImportError("synthetic platform")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_fcntl)
    with pytest.raises(StandaloneMigrationRejected, match="processing_approval_lock_unsettled"):
        _sources(paths)
    assert lock.exists()


def _legacy(paths, index=0, **overrides):
    operation_id = "gwctl-msg-" + f"{index:032x}"
    path = paths.root / "control_operations" / (operation_id + ".json")
    path.parent.mkdir(exist_ok=True)
    row = {"schema_version": "gateway_control_operation.v1", "operation_id": operation_id,
           "state": "completed", "result": {"ok": True}, **overrides}
    path.write_text(json.dumps(row))
    return path


def test_legacy_completed_receipts_are_versioned_in_place_with_archive_tolerated(paths):
    first, second = _legacy(paths), _legacy(paths, 1)
    before = {path: path.read_bytes() for path in (first, second)}
    archive = paths.root / "archive" / "old"
    archive.mkdir(parents=True)
    (archive / "anything.json").write_text("not active")
    report = _sources(paths)[2]
    assert len(report["legacy_completed"]) == 2
    assert all(row["schema_version"] == "gateway_control_operation.v1" for row in report["legacy_completed"])
    assert all(path.read_bytes() == raw for path, raw in before.items())
    assert sorted(p.name for p in archive.iterdir()) == ["anything.json"]


@pytest.mark.parametrize("row", [{}, [], {"state": "completed"}, {"state": "executing"},
                                {"state": "terminal_unknown"}, {"schema_version": "future.v99"},
                                {"dispatch": {}}, {"result": "completed"}, {"operation_id": "different"}])
def test_unknown_or_inflight_legacy_control_still_blocks(paths, row):
    target = _legacy(paths, **row) if isinstance(row, dict) and row else _legacy(paths)
    if not row or isinstance(row, list) or row == {"state": "completed"}:
        target.write_text(json.dumps(row))
    with pytest.raises((StandaloneMigrationRejected, ValueError), match="control_operation_unsettled"):
        _sources(paths)


def test_local_owner_full_inventory_uses_existing_runtime_only(tmp_path, monkeypatch):
    case = stopped_home(tmp_path, monkeypatch)
    user = resolve_owner_home(case.home, OwnerIdentity.local_user("real-test-a"))
    user.home_dir.mkdir()
    RuntimeRepository(runtime_db_path(user.home_dir))
    process_session_store_root(user.home_dir, user.home_dir).mkdir(parents=True, exist_ok=True)
    inventory.prepare_standalone_read_facilities(case.home)
    snapshot = inventory.standalone_inventory(case.home, case.config, case.paths)
    assert {row["owner_id"] for row in snapshot["owners"]} == {"main", "real-test-a"}
    assert user.owner_id in snapshot["databases"]


def test_literal_uid_native_namespace_is_inspected_not_just_allowed(tmp_path, monkeypatch):
    case = stopped_home(tmp_path, monkeypatch)
    user = resolve_owner_home(case.home, OwnerIdentity.local_user("real-test-a"))
    user.home_dir.mkdir()
    RuntimeRepository(runtime_db_path(user.home_dir))
    literal = process_session_store_root(user.home_dir, user.home_dir).parent / "real-test-a"
    literal.mkdir()
    inventory.prepare_standalone_read_facilities(case.home)
    inventory.standalone_inventory(case.home, case.config, case.paths)
    (literal / ".process-sessions.redo.json").write_text("{}")
    with pytest.raises(StandaloneMigrationRejected, match="native_redo_requires_recovery"):
        inventory.standalone_inventory(case.home, case.config, case.paths)


@pytest.mark.parametrize("damage", ["nonempty", "symlink", "json-present", "young"])
def test_stop_cleanup_does_not_delete_unproven_orphans(tmp_path, monkeypatch, damage):
    case = stopped_home(tmp_path, monkeypatch)
    case.configure(0)
    case.paths.processing.mkdir(parents=True, exist_ok=True)
    lock = _orphan(case.paths)
    if damage == "nonempty":
        lock.write_bytes(b"unsettled")
        os.utime(lock, (time.time() - 90000,) * 2)
    elif damage == "symlink":
        outside = case.home / "not-a-lock"
        outside.write_bytes(b"")
        lock.unlink()
        lock.symlink_to(outside)
    elif damage == "json-present":
        lock.with_suffix("").write_text("{}")
    else:
        os.utime(lock, None)
    cleanup = json.loads(case.paths.state.read_text())["standalone_stop"]["cleanup"]
    with standalone_lifecycle_guard(case.paths.root) as held:
        fact = record_standalone_stop(case.context, held, cleanup)
    assert lock.exists()
    if damage != "young":
        assert fact["standalone_stop"]["blockers"]


def test_data_file_appearing_before_unlink_prevents_cleanup(tmp_path, monkeypatch):
    case = stopped_home(tmp_path, monkeypatch)
    case.configure(0)
    case.paths.processing.mkdir(parents=True, exist_ok=True)
    lock = _orphan(case.paths)
    original = inventory._missing_leaf
    calls = []

    def published(parent, name):
        calls.append(name)
        if len(calls) == 2:
            lock.with_suffix("").write_text("{}")
        return original(parent, name)

    monkeypatch.setattr(inventory, "_missing_leaf", published)
    cleanup = json.loads(case.paths.state.read_text())["standalone_stop"]["cleanup"]
    with standalone_lifecycle_guard(case.paths.root) as held:
        fact = record_standalone_stop(case.context, held, cleanup)
    assert lock.exists() and lock.with_suffix("").exists()
    assert fact["standalone_stop"]["blockers"]


def test_corrupt_current_receipt_cannot_use_legacy_completed_bypass(paths):
    target = _legacy(paths, schema_version="gateway_control_operation.v2")
    with pytest.raises(StandaloneMigrationRejected, match="control_operation_unsettled"):
        _sources(paths)
    assert target.exists()


def test_later_bad_receipt_is_not_swallowed_after_legacy_completion(paths):
    _legacy(paths)
    bad = _legacy(paths, 1, state="executing")
    with pytest.raises(StandaloneMigrationRejected) as rejected:
        _sources(paths)
    assert rejected.value.reason == "control_operation_unsettled"
    assert rejected.value.ref == "control_operations/" + bad.name


def test_missing_fcntl_on_orphan_never_deletes(paths, monkeypatch):
    lock = _orphan(paths)
    original = builtins.__import__

    def without_fcntl(name, *args, **kwargs):
        if name == "fcntl":
            raise ImportError("synthetic platform")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_fcntl)
    with pytest.raises(StandaloneMigrationRejected, match="processing_approval_lock_unsettled"):
        _sources(paths)
    assert lock.exists()


# LLM: 只在tmp使用真实审批writer和RuntimeDB领取/结算，不给决定补写state/status。
# 函数用途: 建立可精确核run/op/identity的历史决定，或保留相同协议的在途决定。
def _writer_history(case, index=0, *, complete=True, kind="foreground"):
    run_id, operation_id = f"run-writer-{index}", f"operation-writer-{index}"
    ids = case.repo.record_run_creation(owner_id=case.owner.owner_id, run_id=run_id)
    call = SimpleNamespace(call_id=f"call-{index}", tool_name="synthetic", run_id=run_id,
                           operation_id=operation_id, idempotency_key=f"key-{index}",
                           args_hash="a" * 64, turn_id=f"turn-{index}")
    store = ManagedOperationStore(case.repo)
    claimed = store.claim_tool_operation(ToolOperationClaimRequest(
        owner_id=case.owner.owner_id, run_id=run_id, task_id=ids["task_id"],
        operation_id=operation_id, tool=call.tool_name, args_hash=call.args_hash,
        idempotency_key=call.idempotency_key, idempotency_scope="operation", idempotency_namespace="test",
        holder=new_tool_operation_holder(), lease_expires_at=time.time() + 300, attempt_id=ids["attempt_id"]))
    assert claimed.action == "execute"
    request = build_tool_approval_request(call, request_id=f"gwreq-writer-{index}", round_number=1,
                                         call_index=0, description="临时历史审批")
    chunk = (command_approval_path(case.paths, case.owner.identity, request.request_id)
             if kind == "host-command" else case.paths.processing / "unused.chunks.jsonl")
    target = write_gateway_permission_decision(chunk, request, ToolApprovalDecision(request.permission_id, "approved"))
    assert not {"state", "status"}.intersection(json.loads(target.read_text()))
    if complete:
        store.finish_tool_operation(ToolOperationCompletionRequest(
            owner_id=case.owner.owner_id, run_id=run_id, operation_id=operation_id,
            holder_id=claimed.record.holder_id, generation=claimed.record.generation,
            status="succeeded", result={"ok": True}))
        assert case.repo.settle_agent_run(agent_run_id=ids["agent_run_id"], attempt_id=ids["attempt_id"],
                                          status="done")["settled"]
        assert case.repo.settle_task_run_if_agent_tree_terminal(
            task_run_id=ids["task_run_id"], task_id=ids["task_id"])["settled"]
    return target, ids, operation_id


# LLM: 31条只是合成历史批量，不代表生产31条已取样；停止和迁移沿原入口。
# 函数用途: 验证真实writer无终态字段的决定被规范结算证据放行且原地保留。
def test_31_real_writer_settled_approvals_stop_then_migrate(tmp_path, monkeypatch):
    case = stopped_home(tmp_path, monkeypatch)
    targets = [_writer_history(case, i, kind="host-command" if i % 2 else "foreground")[0] for i in range(31)]
    before = {target: target.read_bytes() for target in targets}
    case.configure(0)
    cleanup = json.loads(case.paths.state.read_text())["standalone_stop"]["cleanup"]
    with standalone_lifecycle_guard(case.paths.root) as held:
        fact = record_standalone_stop(case.context, held, cleanup)
    assert fact["standalone_stop"]["blockers"] == []
    assert all(target.read_bytes() == raw for target, raw in before.items())
    case.args, case.config = case.configure(2)
    with coordinator_guard(case.paths.root, 2):
        assert not _prepare(case)._all_quarantined


# LLM: 同一真实writer，只有权威结算/身份受损；不按文案、年龄或空目录放行。
# 函数用途: 覆盖未消费、缺证据、错误binding和未知/在途/坏库的失败关闭。
@pytest.mark.parametrize("damage", ["pending", "missing-op", "missing-db", "corrupt-db",
    "binding-run_id", "binding-operation_id", "binding-tool_name", "binding-idempotency_key", "binding-args_hash",
    "missing-binding", "unknown", "no-settled", "no-ended", "run-created", "attempt-pending", "no-closed",
    "owner", "generation", "broken-payload", "missing-payload-key", "wrong-command-owner", "explicit-pending"])
def test_real_writer_unproven_history_still_blocks(tmp_path, monkeypatch, damage):
    case = stopped_home(tmp_path, monkeypatch)
    target, ids, op = _writer_history(case, complete=damage != "pending", kind="host-command"
                                     if damage == "wrong-command-owner" else "foreground")
    inventory.prepare_standalone_read_facilities(case.home)
    if damage != "pending":
        assert inventory.standalone_inventory(case.home, case.config, case.paths)
    if damage.startswith("binding-") or damage in {"missing-binding", "explicit-pending"}:
        row = json.loads(target.read_text())
        if damage == "missing-binding":
            del row["binding"]
        elif damage == "explicit-pending":
            row["state"] = "pending"
        else:
            row["binding"][damage.removeprefix("binding-")] = "wrong"
        target.write_text(json.dumps(row))
    elif damage == "missing-db":
        runtime_db_path(case.owner.home_dir).unlink()
    elif damage == "corrupt-db":
        runtime_db_path(case.owner.home_dir).write_bytes(b"not sqlite")
    elif damage == "wrong-command-owner":
        bucket = target.parents[3]
        wrong = bucket.with_name("0" * 64) / target.relative_to(bucket)
        wrong.parent.mkdir(parents=True)
        target.rename(wrong)
    elif damage != "pending":
        with case.repo._runtime_connection() as conn:
            if damage == "missing-op":
                conn.execute("DELETE FROM tool_operations WHERE operation_id=?", (op,))
            elif damage in {"unknown", "no-settled", "generation", "broken-payload", "missing-payload-key"}:
                if damage == "missing-payload-key":
                    payload = json.loads(conn.execute("SELECT outcome_json FROM tool_operations WHERE operation_id=?", (op,)).fetchone()[0])
                    del payload["idempotency_key"]
                    conn.execute("UPDATE tool_operations SET outcome_json=? WHERE operation_id=?", (json.dumps(payload), op))
                else:
                    column, value = {"unknown": ("status", "UNKNOWN"), "no-settled": ("settled_at", 0),
                        "generation": ("attempt_generation", 99), "broken-payload": ("outcome_json", "{")}[damage]
                    conn.execute(f"UPDATE tool_operations SET {column}=? WHERE operation_id=?", (value, op))
            elif damage in {"no-ended", "attempt-pending"}:
                column, value = ("ended_at", 0) if damage == "no-ended" else ("status", "pending")
                conn.execute(f"UPDATE agent_attempts SET {column}=? WHERE attempt_id=?", (value, ids["attempt_id"]))
            elif damage == "run-created":
                conn.execute("UPDATE agent_runs SET status='created' WHERE agent_run_id=?", (ids["agent_run_id"],))
            elif damage == "no-closed":
                conn.execute("UPDATE task_runs SET closed_at=0 WHERE task_run_id=?", (ids["task_run_id"],))
            elif damage == "owner":
                conn.execute("UPDATE tasks SET owner_id='other' WHERE task_id=?", (ids["task_id"],))
            conn.commit()
        # 写连接关闭可能回收WAL设施；由原准备口恢复，避免反例只测到sidecar缺失。
        inventory.prepare_standalone_read_facilities(case.home)
    with pytest.raises((StandaloneMigrationRejected, OSError, ValueError)) as rejected:
        inventory.standalone_inventory(case.home, case.config, case.paths)
    if damage not in {"corrupt-db", "broken-payload"}:
        assert rejected.value.reason == "processing_auxiliary_unsettled"


# LLM: 原runtime digest未含outcome_json/closed_at；新增读取事实必须进入原stop摘要比较。
# 函数用途: 保持合法终态，仅改变新读取责任，验证后继拒绝旧停止回执。
@pytest.mark.parametrize("change", ["payload", "closed-at"])
def test_real_writer_new_evidence_is_in_stable_stop_digest(tmp_path, monkeypatch, change):
    case = stopped_home(tmp_path, monkeypatch)
    _target, ids, op = _writer_history(case)
    case.configure(0)
    cleanup = json.loads(case.paths.state.read_text())["standalone_stop"]["cleanup"]
    with standalone_lifecycle_guard(case.paths.root) as held:
        fact = record_standalone_stop(case.context, held, cleanup)
    assert fact["standalone_stop"]["blockers"] == []
    before = inventory.standalone_inventory(case.home, case.config, case.paths)
    with case.repo._runtime_connection() as conn:
        if change == "payload":
            row = json.loads(conn.execute("SELECT outcome_json FROM tool_operations WHERE operation_id=?", (op,)).fetchone()[0])
            row["history_revision"] = 2
            conn.execute("UPDATE tool_operations SET outcome_json=? WHERE operation_id=?", (json.dumps(row), op))
        else:
            conn.execute("UPDATE task_runs SET closed_at=closed_at+1 WHERE task_run_id=?", (ids["task_run_id"],))
        conn.commit()
    inventory.prepare_standalone_read_facilities(case.home)
    assert inventory.standalone_inventory(case.home, case.config, case.paths) != before
    case.args, case.config = case.configure(2)
    with coordinator_guard(case.paths.root, 2), pytest.raises(StandaloneMigrationRejected):
        _prepare(case)


# LLM: 前后完整文件字节核对，仓储仍是真实实现，只观察连接参数，不替换读结果。
# 函数用途: 验证新证据读取仅使用existing/readonly，不改审批、DB或已有文件。
def test_real_writer_evidence_inventory_is_readonly(tmp_path, monkeypatch):
    case = stopped_home(tmp_path, monkeypatch)
    _writer_history(case)
    inventory.prepare_standalone_read_facilities(case.home)
    original, calls = inventory.RuntimeRepository, []

    # LLM: 仅记录构造参数并调用原类，任何读写模式漂移都直接失败。
    # 函数用途: 确认审批库存连接不初始化或迁移现存库。
    def observed(path, **kwargs):
        assert kwargs == {"read_only": True, "existing_only": True}
        calls.append(path)
        return original(path, **kwargs)

    monkeypatch.setattr(inventory, "RuntimeRepository", observed)
    before = {str(path.relative_to(case.home)): path.read_bytes()
              for path in case.home.rglob("*") if path.is_file()}
    assert inventory.standalone_inventory(case.home, case.config, case.paths)
    assert calls == [runtime_db_path(case.owner.home_dir)]
    after = {str(path.relative_to(case.home)): path.read_bytes()
             for path in case.home.rglob("*") if path.is_file()}
    assert after == before


# LLM: 在原stop双读的准确中间观察点修改新证据；不是按mtime猜测变化。
# 函数用途: 新读取责任在首末采样间漂移时必须拒绝签发无blocker停止回执。
def test_real_writer_evidence_change_between_stop_samples_is_rejected(tmp_path, monkeypatch):
    case = stopped_home(tmp_path, monkeypatch)
    _target, _ids, op = _writer_history(case)
    case.configure(0)
    cleanup = json.loads(case.paths.state.read_text())["standalone_stop"]["cleanup"]

    # LLM: 真实SQL提交后由原准备口恢复读设施，只改变责任不伪造停止结果。
    # 函数用途: 在两次库存之间确定性发表另一合法终态payload。
    def between_samples():
        with case.repo._runtime_connection() as conn:
            row = json.loads(conn.execute("SELECT outcome_json FROM tool_operations WHERE operation_id=?", (op,)).fetchone()[0])
            row["history_revision"] = 2
            conn.execute("UPDATE tool_operations SET outcome_json=? WHERE operation_id=?", (json.dumps(row), op))
            conn.commit()
        inventory.prepare_standalone_read_facilities(case.home)
        return ()

    monkeypatch.setattr(stop, "model_call_drain_snapshot", between_samples)
    with standalone_lifecycle_guard(case.paths.root) as held:
        fact = record_standalone_stop(case.context, held, cleanup)
    assert fact["standalone_stop"]["sources"] is None
    assert fact["standalone_stop"]["blockers"] == [{"reason": "stop_sources_changed", "ref": ""}]


@pytest.mark.parametrize("current_state", ["settled", "running", "missing-ended", "bad-generation"])
# LLM: 真API换代保留旧终态操作；历史身份与当前执行权不能混为一谈。
# 函数用途: 合法后继已结算可迁移，在途或当前身份缺证据仍拒绝。
def test_settled_writer_history_survives_later_attempt(tmp_path, monkeypatch, current_state):
    case = stopped_home(tmp_path, monkeypatch)
    target, ids, _operation_id = _writer_history(case)
    before = target.read_bytes()
    current = case.repo.create_attempt(ids["agent_run_id"])
    assert current["attempt_id"] != ids["attempt_id"]
    if current_state != "running":
        assert case.repo.settle_agent_run(agent_run_id=ids["agent_run_id"], attempt_id=current["attempt_id"],
                                          status="done")["settled"]
        assert case.repo.settle_task_run_if_agent_tree_terminal(
            task_run_id=ids["task_run_id"], task_id=ids["task_id"])["settled"]
        if current_state != "settled":
            with case.repo._runtime_connection() as conn:
                if current_state == "missing-ended":
                    conn.execute("UPDATE agent_attempts SET ended_at=0 WHERE attempt_id=?", (current["attempt_id"],))
                else:
                    conn.execute("UPDATE agent_attempts SET attempt_generation=99 WHERE attempt_id=?",
                                 (current["attempt_id"],))
                conn.commit()
    inventory.prepare_standalone_read_facilities(case.home)
    if current_state != "settled":
        with pytest.raises(StandaloneMigrationRejected, match="processing_auxiliary_unsettled"):
            inventory.standalone_inventory(case.home, case.config, case.paths)
        return
    assert inventory.standalone_inventory(case.home, case.config, case.paths)
    case.configure(0)
    cleanup = json.loads(case.paths.state.read_text())["standalone_stop"]["cleanup"]
    with standalone_lifecycle_guard(case.paths.root) as held:
        fact = record_standalone_stop(case.context, held, cleanup)
    assert fact["standalone_stop"]["blockers"] == []
    assert target.read_bytes() == before
    case.args, case.config = case.configure(2)
    with coordinator_guard(case.paths.root, 2):
        assert not _prepare(case)._all_quarantined


# === Source: agent_py_agent/tests/test_v13_w5_web_integrity_scope.py ===

@pytest.mark.parametrize("marker", ["runtime.db", "sessions", "workspace/runtime"])
def test_host_root_never_becomes_site_root(tmp_path, marker):
    marker_path = tmp_path / marker
    if marker == "runtime.db":
        marker_path.write_text("")
    else:
        marker_path.mkdir(parents=True)
    (tmp_path / "poster.html").write_text("<html>old poster</html>")
    script = tmp_path / "tasks/x/tmp/test.js"
    script.parent.mkdir(parents=True)
    script.write_text("console.log(1)")
    assert web._nearest_site_root(script, tmp_path) is None


def test_cjs_without_index_does_not_scan(tmp_path, monkeypatch):
    (tmp_path / "poster.html").write_text("<html>old poster</html>")
    script = tmp_path / "tasks/x/tmp/test.cjs"
    script.parent.mkdir(parents=True)
    script.write_text("console.log(1)")
    calls = []
    import agent_py_agent.agent.subagents.static_site as static_site
    monkeypatch.setattr(static_site, "run_static_site_check", lambda *args: calls.append(args))
    result = web.check_web_project_post_write(script, tmp_path)
    assert result.ok is True
    assert calls == []


def test_cjs_next_to_or_below_index_is_checked(tmp_path, monkeypatch):
    site = tmp_path / "site"
    script = site / "scripts/test.cjs"
    script.parent.mkdir(parents=True)
    script.write_text("console.log(1)")
    (site / "index.html").write_text("<html>website</html>")
    calls = []
    import agent_py_agent.agent.subagents.static_site as static_site

    def check(*args):
        calls.append(args)
        return SimpleNamespace(validation_result={})

    monkeypatch.setattr(static_site, "run_static_site_check", check)
    result = web.check_web_project_post_write(script, tmp_path)
    assert result.kind == "web_project"
    assert calls[0][0]["site_root"] == "site"


def test_workspace_boundary_cannot_be_crossed_for_index(tmp_path):
    (tmp_path / "index.html").write_text("<html>outside</html>")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = workspace / "test.cjs"
    script.write_text("")
    assert web._nearest_site_root(script, workspace) is None


def test_static_site_prunes_runtime_before_scandir(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<html><body>hello</body></html>")
    for name in ("local_store", "compact", "runs", "sessions", ".git", "__pycache__", "node_modules", "workspace/runtime"):
        root = tmp_path / name
        root.mkdir(parents=True)
        (root / "hidden.html").write_text("<a href='#'>ignored</a>")
    noise = tmp_path / "local_store"
    for number in range(10000):
        (noise / f"{number}.html").write_text("")
    original = os.scandir
    visited = []

    def observe(path):
        visited.append(Path(path).relative_to(tmp_path).as_posix())
        return original(path)

    monkeypatch.setattr(os, "scandir", observe)
    record = validator.run_static_site_check({"site_root": "."}, tmp_path)
    assert record.passed is True
    assert record.validation_result["checked_files"] == ["index.html"]
    assert set(visited) <= {".", "workspace"}


def test_static_site_excludes_runtime_even_in_explicit_scope(tmp_path):
    hidden = tmp_path / "sessions/hidden.html"
    hidden.parent.mkdir()
    hidden.write_text("<a href='#'>not part of site</a>")
    (tmp_path / "index.html").write_text("<html>site</html>")
    record = validator.run_static_site_check({"site_root": ".", "html_files": ["sessions/hidden.html"]}, tmp_path)
    assert "sessions/hidden.html" not in record.validation_result["checked_files"]


def test_static_site_reports_truncation_and_never_passes_partial_scan(tmp_path):
    for number in range(3):
        (tmp_path / f"{number}.html").write_text("<html><body>ok</body></html>")
    record = validator.run_static_site_check({"site_root": ".", "max_files": 1}, tmp_path)
    assert record.validation_result["scan_incomplete"] is True
    assert record.passed is False
    assert len(record.validation_result["checked_files"]) == 1


def test_canonical_workspace_alias_still_locates_site(tmp_path):
    root = tmp_path / "real"
    site = root / "site"
    site.mkdir(parents=True)
    (site / "index.html").write_text("<html>ok</html>")
    script = site / "test.cjs"
    script.write_text("")
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    assert web._nearest_site_root(alias / "site/test.cjs", alias) == site


@pytest.mark.parametrize("owner_kind", ["users", "groups"])
def test_canonical_provider_owner_without_markers_is_not_site(tmp_path, owner_kind):
    owner = tmp_path / "owners/providers/demo" / owner_kind / "one"
    script = owner / "tasks/x/tmp/test.js"
    script.parent.mkdir(parents=True)
    script.write_text("")
    (owner / "poster.html").write_text("<html>poster</html>")
    assert web._nearest_site_root(script, owner) is None


@pytest.mark.parametrize("root", ["sessions", "runs/nested", "workspace/runtime", "workspace/runtime/site"])
@pytest.mark.parametrize("explicit", [False, True])
def test_runtime_directory_cannot_become_site_root(tmp_path, monkeypatch, root, explicit):
    site = tmp_path / root
    site.mkdir(parents=True)
    (site / "hidden.html").write_text("<html><body>hidden</body></html>")
    visited = []
    original = os.scandir

    def observe(path):
        visited.append(Path(path))
        return original(path)

    monkeypatch.setattr(os, "scandir", observe)
    request = {"site_root": root}
    if explicit:
        request["html_files"] = ["hidden.html"]
    record = validator.run_static_site_check(request, tmp_path)
    assert record.passed is False
    assert record.validation_result["checked_files"] == []
    assert not visited


@pytest.mark.parametrize("scope", ["missing.html", "sessions/hidden.html", "../outside.html", "wrong.txt"])
def test_invalid_explicit_html_never_passes_as_empty_scope(tmp_path, scope):
    hidden = tmp_path / "sessions/hidden.html"
    hidden.parent.mkdir()
    hidden.write_text("<html>not site</html>")
    (tmp_path / "wrong.txt").write_text("not html")
    record = validator.run_static_site_check({"site_root": ".", "html_files": [scope]}, tmp_path)
    assert record.validation_result["checked_files"] == []
    assert record.validation_result["scan_incomplete"] is True
    assert record.passed is False


# === Source: agent_py_agent/tests/test_v13_w6_curator_task_file_and_dedupe.py ===

def _config(**changes):
    return replace(MemoryCuratorConfig(), **changes)


def _store_messages(root, count=1, *, text='SYNTHETIC_ONLY_RULE', channel='tui', created_at=6000.0):
    store = ConversationStore(root / 'conversations')
    messages = []
    for i in range(count):
        thread = store.threads.get_or_create({'canonical_user_id': 'SYNTHETIC_ONLY_USER', 'channel': channel,
            'channel_conversation_id': f'SYNTHETIC_ONLY_CHAT_{i}', 'channel_user_id': 'SYNTHETIC_ONLY_USER', 'now': created_at})
        messages.append(store.messages.append({'thread_id': thread.thread_id, 'role': 'user', 'channel': channel,
            'content': text, 'now': created_at + i, 'metadata': {'request_id': f'SYNTHETIC_ONLY_REQ_{i}',
            'dedupe_key': f'SYNTHETIC_ONLY_BROADCAST:{i}'}}))
    return store, messages


def _collect(root, store, *, tool_source=None, config=None):
    return collect_curator_inputs(message_source=CuratorMessageSource(store), audit_dir=root / 'audit',
        state=MemoryCuratorState(), config=config or _config(), tool_reference_source=tool_source)


def _artifact(root, message, path, *, ok=True, request_id=None, tool='read_file', body=None):
    output = body or ('SYNTHETIC_ONLY任务规则：重要改动独立复核。\n' * 500)
    digest = hashlib.sha256(output.encode()).hexdigest()
    call = 'SYNTHETIC_ONLY_CALL'
    request_id = request_id or message.metadata['request_id']
    artifact = root / 'frozen' / 'SYNTHETIC_ONLY_READ.json'
    artifact.parent.mkdir(parents=True, exist_ok=True)
    payload = {'kind': 'tool_output', 'tool': tool, 'call_id': call, 'run_id': 'SYNTHETIC_ONLY_RUN',
        'request_id': request_id, 'ok': ok, 'parameters': {'path': str(path)}, 'content': output,
        'sha256': digest, 'size_bytes': len(output.encode())}
    artifact.write_text(json.dumps(payload))
    audit = {'event_id': 'SYNTHETIC_ONLY_AUDIT', 'event_type': 'tool_call', 'tool': tool, 'tool_call_id': call,
        'tool_success': ok, 'status': 'ok' if ok else 'failed', 'effect_outcome': 'confirmed' if ok else 'failed',
        'thread_id': message.thread_id, 'request_id': request_id, 'run_id': 'SYNTHETIC_ONLY_RUN',
        'created_at': '1970-01-01T01:40:30+00:00'}
    audit_dir = root / 'audit'
    audit_dir.mkdir(exist_ok=True)
    (audit_dir / '1970-01-01.jsonl').write_text(json.dumps(audit) + '\n')
    row = {**payload, 'path': str(artifact)}
    source = CuratorToolReferenceSource(root, lambda owner, runs, limit: {'ok': True,
        'tool_outputs_by_run': {'SYNTHETIC_ONLY_RUN': [row]}})
    return source, artifact, digest


def test_task_file_evidence_attached_bounded(tmp_path):
    path = tmp_path / 'SYNTHETIC_ONLY_policy.md'
    path.write_text('已经变化的文件不该被策展器重新读取')
    store, messages = _store_messages(tmp_path, text=f'请完整阅读 `{path}` 并照做。')
    source, _, digest = _artifact(tmp_path, messages[0], path)
    batch = _collect(tmp_path, store, tool_source=source)
    evidence = getattr(batch.messages[0], 'referenced_files', ())
    assert len(evidence) == 1
    item = evidence[0]
    assert item['path'] == str(path.resolve()) and item['sha256'] == digest
    assert len(item['excerpt']) == 4000 and item['tool_call_id'] == 'SYNTHETIC_ONLY_CALL'
    assert '已经变化' not in item['excerpt']
    assert item['origin'] == 'user_referenced_file'
    assert batch.to_model_payload()['messages'][0]['referenced_files'] == list(evidence)


@pytest.mark.parametrize('mode', ['wrong_path', 'failed', 'other_request', 'other_tool', 'tampered', 'outside_owner'])
def test_unmatched_failed_or_untrusted_read_has_no_evidence(tmp_path, mode):
    path = tmp_path / 'SYNTHETIC_ONLY_policy.md'
    store, messages = _store_messages(tmp_path, text=f'完整阅读 `{path}`。')
    read_path = path.with_name('SYNTHETIC_ONLY_other.md') if mode == 'wrong_path' else path
    source, artifact, _ = _artifact(tmp_path, messages[0], read_path, ok=mode != 'failed',
        request_id='SYNTHETIC_ONLY_OTHER_REQ' if mode == 'other_request' else None,
        tool='run_command' if mode == 'other_tool' else 'read_file')
    if mode == 'tampered':
        payload = json.loads(artifact.read_text())
        payload['content'] += 'SYNTHETIC_ONLY_TAMPER'
        artifact.write_text(json.dumps(payload))
    if mode == 'outside_owner':
        source = replace(source, owner_root=tmp_path / 'SYNTHETIC_ONLY_OTHER_OWNER')
    batch = _collect(tmp_path, store, tool_source=source)
    assert not getattr(batch.messages[0], 'referenced_files', ())


def test_task_file_custom_budget_and_overall_budget(tmp_path):
    path = tmp_path / 'SYNTHETIC_ONLY_policy.md'
    store, messages = _store_messages(tmp_path, text=f'完整阅读 {path} 并照做。')
    source, _, _ = _artifact(tmp_path, messages[0], path)
    batch = _collect(tmp_path, store, tool_source=source, config=_config(task_file_evidence_max_chars=71))
    assert len(getattr(batch.messages[0], 'referenced_files', ())) == 1
    assert len(batch.messages[0].referenced_files[0]['excerpt']) == 71
    assert len(json.dumps(batch.to_model_payload(), ensure_ascii=False)) <= 40000


# 走真实配置快照和收集入口，不能靠给测试替身临时加属性绕过配置接线。
@pytest.mark.parametrize('budget', [0, 71, 40000])
def test_agent_config_file_evidence_budget_reaches_collect(tmp_path, budget):
    path = tmp_path / 'SYNTHETIC_ONLY_policy.md'
    store, messages = _store_messages(tmp_path, text=f'完整阅读 `{path}` 并照做。')
    body = 'SYNTHETIC_ONLY_FROZEN_RULE\n' * 500
    source, _, digest = _artifact(tmp_path, messages[0], path, body=body)
    config = MemoryCuratorConfig.from_agent_config(AgentConfig(
        memory_curator_task_file_evidence_max_chars=budget))
    batch = _collect(tmp_path, store, tool_source=source, config=config)
    assert len(batch.messages) == 1
    evidence = batch.messages[0].referenced_files
    if budget == 0:
        assert evidence == ()
    else:
        assert len(evidence) == 1
        assert evidence[0]['excerpt'] == body[:budget]
        assert evidence[0]['sha256'] == digest
    assert config.task_file_evidence_max_chars == budget


@pytest.mark.parametrize(('window', 'created_at', 'model_messages'), [
    (0, 6059.0, 2), (60, 6059.0, 2), (600, 6059.0, 1), (86400, 6599.0, 1),
])
def test_agent_config_dedupe_window_reaches_collect(tmp_path, window, created_at, model_messages):
    # 6059/6060 跨60秒桶，但仍处于同一个600秒桶；原始消息与游标必须始终保留两条。
    store, messages = _store_messages(tmp_path, 2, created_at=created_at)
    config = MemoryCuratorConfig.from_agent_config(AgentConfig(
        memory_curator_cross_thread_dedupe_window_seconds=window))
    batch = _collect(tmp_path, store, config=config)
    assert {message.message_id for message in batch.messages} == {message.message_id for message in messages}
    assert len(batch.to_model_payload()['messages']) == model_messages
    if window == 0:
        assert all(not message.broadcast for message in batch.messages)
    assert getattr(config, 'cross_thread_dedupe_window_seconds', 600) == window


def test_agent_config_default_revision_preserves_legacy_state():
    # 修复前真实默认快照的摘要；新默认字段不能使既有state误判为配置切换。
    assert MemoryCuratorConfig().revision() == 'curator-config-2a1d051db3fe348b'
    assert MemoryCuratorConfig.from_agent_config(AgentConfig()).revision() == MemoryCuratorConfig().revision()


@pytest.mark.parametrize(('field', 'value', 'default'), [
    ('task_file_evidence_max_chars', 0, 4000), ('task_file_evidence_max_chars', 71, 4000),
    ('cross_thread_dedupe_window_seconds', 0, 600), ('cross_thread_dedupe_window_seconds', 60, 600),
])
def test_agent_config_nondefault_revision_changes_and_restores(field, value, default):
    config = MemoryCuratorConfig.from_agent_config(AgentConfig(**{f'memory_curator_{field}': value}))
    assert config.revision() != MemoryCuratorConfig().revision()
    assert replace(config, **{field: default}).revision() == MemoryCuratorConfig().revision()


def test_real_tool_archive_and_control_plane_feed_task_evidence(tmp_path):
    from agent_py_agent.agent.memory_archive.control_plane import query_tool_output_refs_for_runs
    from agent_py_agent.agent.memory_archive.tool_output_externalizer import (
        ExternalizeToolOutputRequest,
        externalize_tool_output_record,
    )

    path = tmp_path / 'SYNTHETIC_ONLY_rule.md'
    body = 'SYNTHETIC_ONLY正式规则：独立复核。'
    store, messages = _store_messages(tmp_path, text=f'请读 `{path}` 并照做。')
    message = messages[0]
    call, run = 'SYNTHETIC_ONLY_REAL_CALL', 'SYNTHETIC_ONLY_REAL_RUN'
    record = externalize_tool_output_record(ExternalizeToolOutputRequest(root=tmp_path, tool='read_file',
        call_id=call, output=body, ok=True, request_id=message.metadata['request_id'], run_id=run,
        parameters={'path': str(path), 'start_line': 1, 'end_line': 1}, force_externalize=True))
    audit = {'event_id': 'SYNTHETIC_ONLY_REAL_AUDIT', 'event_type': 'tool_call', 'tool': 'read_file',
        'tool_call_id': call, 'tool_success': True, 'status': 'ok', 'effect_outcome': 'confirmed',
        'thread_id': message.thread_id, 'request_id': message.metadata['request_id'], 'run_id': run,
        'created_at': '1970-01-01T01:40:30+00:00'}
    (tmp_path / 'audit').mkdir()
    (tmp_path / 'audit' / '1970-01-01.jsonl').write_text(json.dumps(audit) + '\n')
    source = CuratorToolReferenceSource(tmp_path, lambda owner, runs, limit:
        query_tool_output_refs_for_runs(owner, run_ids=runs, limit_per_run=limit))
    batch = _collect(tmp_path, store, tool_source=source)
    [evidence] = batch.messages[0].referenced_files
    assert evidence['excerpt'] == body and evidence['tool_call_id'] == call
    assert evidence['artifact_ref'] == record['artifact_ref']
    assert evidence['sha256'] == hashlib.sha256(body.encode()).hexdigest()
    assert evidence['hash_scope'] == 'verified_read_output'


def test_broadcast_actual_curator_runs_commit_one_daily_candidate_and_all_cursors(tmp_path, monkeypatch):
    from agent_py_agent.agent.backends import ModelResponse
    from agent_py_agent.tests.curator_slice_test_support import expire_first_thread_read
    from agent_py_agent.tests.test_curator_observation_identity import _candidate, _output, _service

    store, messages = _store_messages(tmp_path, 12)
    by_id = {item.message_id: item for item in messages}

    class Backend:
        name = 'actual-batch-broadcast'
        calls = 0
        batches = []

        def generate_structured(self, prompt, *, response_schema):
            # 正式输入可能为任意合法短页；模型只回显当拍实际提供的结构化来源。
            payload = json.loads(prompt.rsplit('待提炼经历 JSON：\n', 1)[1])
            ids = [source['message_id'] for row in payload['messages']
                   for source in row.get('broadcast_sources', [row])]
            assert ids and len(ids) <= 6 and all(mid in by_id for mid in ids)
            self.calls += 1
            self.batches.append(ids)
            refs = [{'message_id': mid} for mid in ids]
            first = by_id[ids[0]]
            candidate = _candidate(first.message_id, 'SYNTHETIC_ONLY_RULE', 'SYNTHETIC_ONLY_SUBJECT')
            candidate.update(candidate_type='user_preference', source_message_refs=refs, promotion_target='user')
            output = _output(first, [candidate])
            output['processed_message_refs'] = refs
            output['daily_events'] = [{'event_type': 'decision', 'summary': 'SYNTHETIC_ONLY_RULE',
                'actor': 'user', 'origin': 'user_explicit', 'message_refs': refs, 'tool_refs': [],
                'artifact_refs': [], 'decisions': [], 'lessons': [], 'next_actions': []}]
            return ModelResponse(text=json.dumps(output), backend=self.name)

    backend = Backend()
    service = _service(tmp_path, backend, store)
    service.config = replace(service.config, batch_message_limit=6)
    fired = expire_first_thread_read(monkeypatch, store)
    expected = {item.thread_id: item.message_id for item in messages}
    results = []
    for _ in range(32):
        result = service.run(reason='admin')
        results.append(result)
        assert result.status in {'succeeded', 'busy'} and not result.failure_code, result
        assert 0 <= result.processed_messages <= 6, result
        if service.state_store.load().per_thread_cursors == expected:
            break
        assert result.processed_messages or 'message_scan_partial' in result.warnings, result
    else:
        pytest.fail(f'12条广播未在32片内全部提交: {results!r}')
    assert fired and 'message_scan_partial' in results[0].warnings
    [event] = service.daily_store.list(day='1970-01-01')
    [candidate] = service.candidate_service.list()
    assert event.sequence == 1 and not event.previous_event_id and len(event.message_refs) == 12
    assert candidate.occurrence_count == 1 and len(candidate.source_message_refs) == 12
    assert service.state_store.load().per_thread_cursors == expected
    assert sum(result.processed_messages for result in results) == 12
    assert {mid for batch in backend.batches for mid in batch} == set(by_id)
    assert backend.calls >= 2


def test_broadcast_interleaved_unprocessed_message_stops_contiguous_cursor(tmp_path):
    from agent_py_agent.agent.memory_store.curator_models import CuratorExtraction
    from agent_py_agent.agent.memory_store.curator_validation import (
        next_cursors,
        validate_extraction,
    )

    store, originals = _store_messages(tmp_path, 2)
    middle = store.messages.append({'thread_id': originals[0].thread_id, 'role': 'user', 'channel': 'tui',
        'content': 'SYNTHETIC_ONLY_UNRELATED', 'now': 6002})
    last = store.messages.append({'thread_id': originals[0].thread_id, 'role': 'user', 'channel': 'tui',
        'content': originals[0].content, 'now': 6003})
    batch = _collect(tmp_path, store)
    processed = tuple({'message_id': item.message_id} for item in (*originals, last))
    extraction = CuratorExtraction((), (), processed, (), (), (), {})
    validated = validate_extraction(extraction, batch, owner_id='SYNTHETIC_ONLY_OWNER')
    cursors = next_cursors({}, '', batch, validated)
    assert cursors.per_thread_cursors[originals[0].thread_id] == originals[0].message_id
    assert cursors.per_thread_cursors[originals[1].thread_id] == originals[1].message_id
    assert middle.message_id not in cursors.per_thread_cursors.values() and cursors.processed_messages == 2


def test_rule_notice_exact_budget_does_not_read_next_notice(tmp_path):
    from agent_py_agent.agent.memory_store.rule_notices import RuleNoticeStore

    store = RuleNoticeStore(tmp_path)
    first, second = 'SYNTHETIC_ONLY_A', 'SYNTHETIC_ONLY_B'
    for index, text in enumerate((first, second)):
        store.publish(memory_id=f'SYNTHETIC_ONLY_RULE_{index}', version=1, text=text)
    assert store.take('SYNTHETIC_ONLY_THREAD', max_chars=len(first)) == first
    assert store.take('SYNTHETIC_ONLY_THREAD', max_chars=len(second)) == second
    assert store.take('SYNTHETIC_ONLY_THREAD') == ''


def test_rule_notice_large_valid_publications_remain_self_readable(tmp_path):
    from agent_py_agent.agent.memory_store.rule_notices import RuleNoticeStore

    store = RuleNoticeStore(tmp_path)
    text = 'SYNTHETIC_ONLY_' + '界' * (40000 - len('SYNTHETIC_ONLY_'))
    for index in range(64):
        store.publish(memory_id=f'SYNTHETIC_ONLY_RULE_{index}', version=1, text=text, max_chars=40000)
    assert store.path.stat().st_size <= 2_097_152
    rows = store._read(store.path)['notices']
    assert rows and rows[-1]['memory_id'] == 'SYNTHETIC_ONLY_RULE_63'
    assert all(len(row['text']) == 40000 for row in rows)
    assert len(store.take('SYNTHETIC_ONLY_THREAD', max_chars=40000)) == 40000


def _persist_candidate_observations(committer, observations):
    changes, committed = committer._candidate_changes(SimpleNamespace(candidate_observations=tuple(observations),
        rejected_target_bindings=()))
    from agent_py_agent.agent.common.json_io import write_private_text_file_atomic_unlocked

    # 真实增量计划先建/写节点再发表唯一头，目录由正式私有writer建立。
    for path, text in changes.items():
        write_private_text_file_atomic_unlocked(path, text)
    return committed


def test_broadcast_12_threads_two_runs_one_daily(tmp_path):
    store, _ = _store_messages(tmp_path, 12)
    batch = _collect(tmp_path, store)
    # 游标仍保留全部原消息，提炼视图才聚合，不丢任何线程的处理身份。
    assert len(batch.messages) == 12
    payload = batch.to_model_payload()
    assert len(payload['messages']) == 1
    assert len(payload['messages'][0]['broadcast_sources']) == 12
    daily = DailyMemoryStore(tmp_path / 'memory' / 'daily')
    committer = object.__new__(CuratorBatchCommitter)
    committer.candidates = CandidateService(tmp_path / 'memory' / 'candidates.jsonl')
    for run, group in enumerate((batch.messages[:6], batch.messages[6:], batch.messages)):
        observations = []
        for message in group:
            ref = {**message.ref(), 'quote': message.full_content}
            daily.append(DailyMemoryEvent('decision', 'SYNTHETIC_ONLY_RULE', 'user', origin='user_explicit',
                message_refs=(ref,), created_at='1970-01-01T01:40:00+00:00', curator_run_id=f'SYNTHETIC_ONLY_RUN_{run}'))
            observations.append(CandidateObservation('user_preference', 'SYNTHETIC_ONLY_RULE', 'SYNTHETIC_ONLY_SUBJECT',
                MemoryScope('personal', 'personal'), 'user_explicit', source_message_refs=(ref,), evidence_refs=(ref,),
                observed_at='1970-01-01T01:40:00+00:00', promotion_target='user'))
        _persist_candidate_observations(committer, observations)
    events = daily.list(day='1970-01-01')
    assert len(events) == 1 and events[0].sequence == 1 and not events[0].previous_event_id
    rows = committer.candidates.readonly_rows()
    assert len(rows) == 1 and rows[0].occurrence_count == 1
    assert len(rows[0].source_message_refs) == 12
    assert len(events[0].message_refs) == 12


def test_different_windows_and_nonuser_sources_are_not_broadcast(tmp_path):
    store, _ = _store_messages(tmp_path, 2, channel='scheduler')
    batch = _collect(tmp_path, store)
    assert len(batch.to_model_payload()['messages']) == 2
    assert all('broadcast_hash' not in message.ref() for message in batch.messages)


def test_broadcast_does_not_collapse_semantic_whitespace(tmp_path):
    store, messages = _store_messages(tmp_path, 2, text="SYNTHETIC_ONLY printf 'a  b'")
    store.messages.append({'thread_id': messages[1].thread_id, 'role': 'user', 'channel': 'tui',
        'content': "SYNTHETIC_ONLY printf 'a b'", 'now': 6003})
    payload = _collect(tmp_path, store).to_model_payload()
    assert len(payload['messages']) == 2
    assert {row['content_preview'] for row in payload['messages']} == {
        "SYNTHETIC_ONLY printf 'a  b'", "SYNTHETIC_ONLY printf 'a b'"}


def test_broadcast_preserves_each_frozen_file_source(tmp_path):
    from agent_py_agent.agent.memory_store.curator_inputs import _with_broadcast_identity

    store, _ = _store_messages(tmp_path, 2)
    batch = _collect(tmp_path, store)
    messages = tuple(replace(message, referenced_files=({'path': '/SYNTHETIC_ONLY_rule.md',
        'sha256': 'a' * 64, 'excerpt': 'SYNTHETIC_ONLY_SAME_RULE',
        'tool_call_id': f'SYNTHETIC_ONLY_CALL_{index}'},)) for index, message in enumerate(batch.messages))
    batch = replace(batch, messages=_with_broadcast_identity(messages))
    view = batch.to_model_payload()['messages']
    assert len(view) == 1
    assert {source['referenced_files'][0]['tool_call_id'] for source in view[0]['broadcast_sources']} == {
        'SYNTHETIC_ONLY_CALL_0', 'SYNTHETIC_ONLY_CALL_1'}
    changed = replace(messages[1], referenced_files=({**messages[1].referenced_files[0], 'sha256': 'b' * 64,
        'excerpt': 'SYNTHETIC_ONLY_CHANGED_RULE'},))
    batch = replace(batch, messages=_with_broadcast_identity((messages[0], changed)))
    assert len(batch.to_model_payload()['messages']) == 2


def _record_instruction(committer, daily, message):
    ref = {**message.ref(), 'quote': message.full_content}
    daily.append(DailyMemoryEvent('decision', 'SYNTHETIC_ONLY_RULE', 'user', origin='user_explicit',
        message_refs=(ref,), created_at='1970-01-01T01:40:00+00:00'))
    observation = CandidateObservation('user_preference', 'SYNTHETIC_ONLY_RULE', 'SYNTHETIC_ONLY_SUBJECT',
        MemoryScope('personal', 'personal'), 'user_explicit', source_message_refs=(ref,), evidence_refs=(ref,),
        observed_at='1970-01-01T01:40:00+00:00', promotion_target='user')
    _persist_candidate_observations(committer, [observation])


def test_broadcast_single_message_batches_are_still_one_observation(tmp_path):
    store, _ = _store_messages(tmp_path, 2)
    config = _config(batch_message_limit=1)
    first = _collect(tmp_path, store, config=config)
    assert len(first.messages) == 1
    second = collect_curator_inputs(message_source=CuratorMessageSource(store), audit_dir=tmp_path / 'audit',
        state=MemoryCuratorState(per_thread_cursors={first.messages[0].thread_id: first.messages[0].message_id}), config=config)
    assert len(second.messages) == 1 and first.messages[0].thread_id != second.messages[0].thread_id
    committer = object.__new__(CuratorBatchCommitter)
    committer.candidates = CandidateService(tmp_path / 'memory' / 'candidates.jsonl')
    daily = DailyMemoryStore(tmp_path / 'memory' / 'daily')
    for message in (*first.messages, *second.messages, *first.messages, *second.messages):
        _record_instruction(committer, daily, message)
    events = daily.list(day='1970-01-01')
    rows = committer.candidates.readonly_rows()
    assert len(events) == 1 and len(events[0].message_refs) == 2
    assert len(rows) == 1 and rows[0].occurrence_count == 1 and len(rows[0].source_message_refs) == 2


def test_same_thread_repetition_is_not_a_cross_thread_broadcast(tmp_path):
    store, original = _store_messages(tmp_path)
    store.messages.append({'thread_id': original[0].thread_id, 'role': 'user', 'channel': 'tui',
        'content': original[0].content, 'now': 6001})
    batch = _collect(tmp_path, store)
    assert len(batch.to_model_payload()['messages']) == 2
    committer = object.__new__(CuratorBatchCommitter)
    committer.candidates = CandidateService(tmp_path / 'memory' / 'candidates.jsonl')
    daily = DailyMemoryStore(tmp_path / 'memory' / 'daily')
    for message in batch.messages:
        _record_instruction(committer, daily, message)
    assert len(daily.list(day='1970-01-01')) == 2
    assert committer.candidates.readonly_rows()[0].occurrence_count == 2


def test_current_symlink_cannot_prove_historical_file_binding(tmp_path):
    alias = tmp_path / 'SYNTHETIC_ONLY_alias.md'
    target = tmp_path / 'SYNTHETIC_ONLY_target.md'
    target.write_text('SYNTHETIC_ONLY_CURRENT_TARGET')
    alias.symlink_to(target)
    store, messages = _store_messages(tmp_path, text=f'请读 `{alias}`。')
    source, _, _ = _artifact(tmp_path, messages[0], target)
    batch = _collect(tmp_path, store, tool_source=source)
    assert not batch.messages[0].referenced_files


def test_broadcast_metadata_still_respects_whole_input_budget(tmp_path):
    store, original = _store_messages(tmp_path, text='SYNTHETIC_ONLY_RULE_' + 'x' * 300)
    for index in range(10):
        store.messages.append({'thread_id': original[0].thread_id, 'role': 'user', 'channel': 'tui',
            'content': f'SYNTHETIC_ONLY_RULE_{index}_' + 'x' * 300, 'now': 6001 + index})
    batch = _collect(tmp_path, store, config=_config(max_input_chars=2000))
    assert batch.messages
    assert len(json.dumps(batch.to_model_payload(), ensure_ascii=False)) <= 2000


@pytest.mark.parametrize('mode', ['missing', 'oversized', 'blob', 'input_budget'])
def test_missing_or_oversized_file_archive_degrades_without_fabricating_evidence(tmp_path, mode):
    path = tmp_path / 'SYNTHETIC_ONLY_policy.md'
    store, messages = _store_messages(tmp_path, text=f'请读 `{path}`。')
    source, artifact, _ = _artifact(tmp_path, messages[0], path)
    payload = json.loads(artifact.read_text())
    if mode == 'missing':
        artifact.rename(artifact.with_suffix('.SYNTHETIC_ONLY_missing'))
    elif mode == 'oversized':
        payload['SYNTHETIC_ONLY_padding'] = 'x' * 1_048_576
        artifact.write_text(json.dumps(payload))
    elif mode == 'blob':
        payload.pop('content')
        payload['blob_v1'] = {'path': '/SYNTHETIC_ONLY_unverified'}
        artifact.write_text(json.dumps(payload))
    config = _config(max_input_chars=2000) if mode == 'input_budget' else _config()
    batch = _collect(tmp_path, store, tool_source=source, config=config)
    assert len(batch.messages) == 1 and batch.messages[0].message_id == messages[0].message_id
    assert not batch.messages[0].referenced_files
    assert not batch.load_errors


def test_rule_notice_tail_once(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.runtime import loop_support
    from agent_py_agent.agent.prompting_parts.memory_context import memory_context_text
    from agent_py_agent.tests.memory_confirmation_fixtures import confirm_candidate
    from agent_py_agent.tests.test_ctxopt_loop_support import _record
    from agent_py_agent.tests.test_memory_promotion_v2 import _explicit, _runtime

    service, conversations, thread = _runtime(tmp_path)
    owner = service.persona.owner_home
    frozen = [_record('SYNTHETIC_ONLY_FROZEN', content='SYNTHETIC_ONLY_SYSTEM_PREFIX')]
    monkeypatch.setattr(loop_support, '_tool_snapshots_for_run', lambda *_: (None, None))
    monkeypatch.setattr(loop_support, '_routed_memory_context_for_request', lambda *a, **kw: SimpleNamespace(injected_sections=[]))
    monkeypatch.setattr(loop_support, '_formal_memories_for_request', lambda *a, **kw: frozen)
    monkeypatch.setattr(loop_support, '_resume_context_for_request', lambda *a, **kw: (SimpleNamespace(injected=False), ''))
    monkeypatch.setattr(loop_support, 'build_runtime_main_context_bundle', lambda *a, **kw: None)
    agent = SimpleNamespace(config=SimpleNamespace(memory_top_k=2, memory_rule_notices_enabled=True,
        memory_rule_notices_max_chars=1200), memory=service.long_term, home_paths=SimpleNamespace(owner_home_dir=owner))
    request = SimpleNamespace(context_scope='default', user_prompt='SYNTHETIC_ONLY_REQUEST', task_id='',
        task_attributes={'conversation_thread_id': 'SYNTHETIC_ONLY_THREAD_B'}, inject=['SYNTHETIC_ONLY_EXISTING_TAIL'])
    before = loop_support._prepare_runtime_context(agent, request)
    prefix = memory_context_text(before.memories).encode()
    candidate = _explicit(service, conversations, thread, content='SYNTHETIC_ONLY_RULE_NOTICE',
        candidate_type='working_agreement', subject_key='SYNTHETIC_ONLY_RULE')
    assert confirm_candidate(service, candidate.candidate_id).promoted
    notice_path = owner / 'memory' / 'rule_notices.json'
    assert notice_path.exists()
    stored = json.loads(notice_path.read_text())['notices']
    assert len(stored) == 1 and stored[0]['version'] == 1
    after = loop_support._prepare_runtime_context(agent, request)
    again = loop_support._prepare_runtime_context(agent, request)
    assert memory_context_text(after.memories).encode() == prefix == memory_context_text(again.memories).encode()
    assert after.runtime_injections[:-1] == before.runtime_injections
    assert after.runtime_injections[-1].count('SYNTHETIC_ONLY_RULE_NOTICE') == 1
    assert again.runtime_injections == before.runtime_injections
    # 同一正式条目换版本必须再通知一次，重复回执不能生成新通知。
    entry_id = service.long_term.all()[0].entry_id
    update = _explicit(service, conversations, thread, content='SYNTHETIC_ONLY_UPDATED_RULE',
        candidate_type='working_agreement', subject_key='SYNTHETIC_ONLY_RULE',
        proposed_action='replace', target_entry_id=entry_id)
    assert confirm_candidate(service, update.candidate_id).promoted
    newer = loop_support._prepare_runtime_context(agent, request)
    assert 'SYNTHETIC_ONLY_UPDATED_RULE' in newer.runtime_injections[-1]
    assert 'SYNTHETIC_ONLY_RULE_NOTICE' not in newer.runtime_injections[-1]
    assert loop_support._prepare_runtime_context(agent, request).runtime_injections == before.runtime_injections


def test_rule_notice_disabled_and_isolated_do_not_consume(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.runtime import loop_support
    from agent_py_agent.tests.memory_confirmation_fixtures import confirm_candidate
    from agent_py_agent.tests.test_memory_promotion_v2 import _explicit, _runtime

    service, conversations, thread = _runtime(tmp_path)
    candidate = _explicit(service, conversations, thread, content='SYNTHETIC_ONLY_PENDING_RULE',
        candidate_type='working_agreement', subject_key='SYNTHETIC_ONLY_SCOPE_RULE')
    assert confirm_candidate(service, candidate.candidate_id).promoted
    path = service.persona.owner_home / 'memory' / 'rule_notices.json'
    assert path.exists()
    # 只接管无关召回/恢复依赖，通知消费本身走生产实现。
    monkeypatch.setattr(loop_support, '_tool_snapshots_for_run', lambda *_: (None, None))
    monkeypatch.setattr(loop_support, '_routed_memory_context_for_request', lambda *a, **kw: SimpleNamespace(injected_sections=[]))
    monkeypatch.setattr(loop_support, '_formal_memories_for_request', lambda *a, **kw: [])
    monkeypatch.setattr(loop_support, '_resume_context_for_request', lambda *a, **kw: (SimpleNamespace(injected=False), ''))
    monkeypatch.setattr(loop_support, 'build_runtime_main_context_bundle', lambda *a, **kw: None)
    agent = SimpleNamespace(config=SimpleNamespace(memory_top_k=1, memory_rule_notices_enabled=False),
        memory=service.long_term, home_paths=SimpleNamespace(owner_home_dir=service.persona.owner_home))
    request = SimpleNamespace(context_scope='default', user_prompt='SYNTHETIC_ONLY_INPUT', task_id='',
        task_attributes={'conversation_thread_id': 'SYNTHETIC_ONLY_THREAD_B'}, inject=[])
    assert not loop_support._prepare_runtime_context(agent, request).runtime_injections
    agent.config.memory_rule_notices_enabled = True
    request.context_scope = 'task_local'
    assert not loop_support._prepare_runtime_context(agent, request).runtime_injections
    request.context_scope = 'default'
    assert 'SYNTHETIC_ONLY_PENDING_RULE' in loop_support._prepare_runtime_context(agent, request).runtime_injections[-1]


def test_user_notice_uses_formal_entry_and_late_callback_cannot_revive_old_rule(tmp_path):
    from agent_py_agent.agent.memory_store.rule_notices import (
        RuleNoticeStore,
        publish_promoted_rule,
    )
    from agent_py_agent.tests.test_memory_promotion_v2 import _runtime

    service, _, _ = _runtime(tmp_path)
    persona = service.persona
    path = persona.path_for('user')
    persona.versions_path.parent.mkdir(parents=True, exist_ok=True)
    old, new = 'SYNTHETIC_ONLY_OLD_RULE', 'SYNTHETIC_ONLY_NEW_RULE'
    ledger = []

    def formal_snapshot(text, version):
        # 隔离夹具构造已提交文档/版本；生产通知仍使用原仓库读取，不声称验证了正式写入事务。
        raw = '# USER\n\n- ' + text + '\n'
        path.write_text(raw)
        ledger.append({'target': 'user', 'sha256': hashlib.sha256(raw.encode()).hexdigest(), 'version': version})
        persona.versions_path.write_text(''.join(json.dumps(row) + '\n' for row in ledger))
        snapshot = persona.list_entries('user')
        assert snapshot['version'] == version
        return snapshot['entries'][0]['entry_id']

    first_id = formal_snapshot(old, 1)
    candidate = SimpleNamespace(candidate_type='user_preference', content=old, proposed_action='add',
        target_entry_id='', source_message_refs=(), scope={'scope_type': 'personal', 'scope_key': 'personal'})
    publish_promoted_rule(service, candidate, f'USER.md#{first_id}')
    new_id = formal_snapshot(new, 2)
    update = SimpleNamespace(**{**vars(candidate), 'content': new, 'proposed_action': 'replace', 'target_entry_id': first_id})
    publish_promoted_rule(service, update, f'USER.md#{new_id}')
    # 旧晋升的收尾晚到，也不能把旧条目/旧正文挂到新文档版本上。
    assert publish_promoted_rule(service, candidate, f'USER.md#{first_id}') is None
    store = RuleNoticeStore(persona.owner_home)
    [notice] = store._read(store.path)['notices']
    assert notice['text'] == new and notice['version'] == 2
    assert store.take('SYNTHETIC_ONLY_READER') == new


def test_hot_notice_reads_exact_formal_rule_not_delayed_candidate_body(tmp_path):
    from agent_py_agent.agent.memory_store.lessons import (
        HOT_SCHEMA_VERSION,
        HotRuleRecord,
        _render_hot,
    )
    from agent_py_agent.agent.memory_store.rule_notices import (
        RuleNoticeStore,
        publish_promoted_rule,
    )
    from agent_py_agent.tests.test_memory_promotion_v2 import _runtime

    service, _, _ = _runtime(tmp_path)
    rule = HotRuleRecord(HOT_SCHEMA_VERSION, 'SYNTHETIC_ONLY_HOT', 'SYNTHETIC_ONLY_CANDIDATE',
        'SYNTHETIC_ONLY_LESSON', 'SYNTHETIC_ONLY_LESSON_REF', 'SYNTHETIC_ONLY_FORMAL_RULE',
        3, ('SYNTHETIC_ONLY_EVIDENCE',), '2026-11-02T00:00:00+00:00')
    service.hot.path.write_text(_render_hot([rule]))
    candidate = SimpleNamespace(candidate_type='hot_rule', content='SYNTHETIC_ONLY_STALE_BODY',
        proposed_action='add', target_entry_id='', source_message_refs=(), scope={})
    reference = 'memory-hot.md#SYNTHETIC_ONLY_HOT'
    notice = publish_promoted_rule(service, candidate, reference)
    assert notice['text'] == rule.rule
    service.hot.path.write_text(_render_hot([]))
    assert publish_promoted_rule(service, candidate, reference) is None
    # 已发布提醒不是正式权威；这个反例只核验晚到收尾不会新增一条已离开HOT的规则。
    store = RuleNoticeStore(service.persona.owner_home)
    assert len(store._read(store.path)['notices']) == 1


# === Source: agent_py_agent/tests/test_v13_w7_detection_lifecycle.py ===

# LLM: 每次都由真实鉴权 HTTP 消费请求，再验证标准信封。
# 函数用途: 发送业务请求并核验成功或失败状态。
def _send(native, action, params, *, key=None, revision=None, user='business-alice', status=200):
    code, body = harness.v13_call(native, action, params, user=user,
        idempotency_key=key, expected_revision=revision)
    assert code == status, body
    assert body['state'] == ('accepted' if status == 202 else 'completed' if status == 200 else 'failed'), body
    if status in {200, 202}:
        assert body['error'] is None, body
    else:
        assert body['data'] == {}, body
    return body


# LLM: 空间由冻结真实入口创建，不预置虚构业务空间。
# 函数用途: 创建当前 HTTP 用户的测试空间。
def _space_w7_detection(native):
    token = uuid.uuid4().hex
    return _send(native, 'spaces.create', {'name': '检测空间-' + token},
                 key='lifecycle-space-' + token)['data']['space_id']


# LLM: 测试草案是输入，不是替代服务器返回的假响应。
# 函数用途: 组装合法的规则草案。
def _draft():
    return {'name': '固定规则', 'severity': 'medium', 'query': {'language': 'nl', 'text': '固定查询'}}


# LLM: 原生入口必须实际保存 pending 提案。
# 函数用途: 创建可继续评审的提案。
def _proposal(native, sid, *, key=None):
    data = _send(native, 'detection_proposals.create',
        {'space_id': sid, 'title': '检测提案', 'rule': _draft(), 'mitre_techniques': ['T1059']},
        key=key or 'proposal-create-' + uuid.uuid4().hex)['data']
    return data['proposal']


# LLM: 规则由 monitors 原生创建，测试不通过手写 JSON 绕过其领域检查。
# 函数用途: 创建可供调优与基线关联的规则。
def _rule(native, sid, *, scheduled=False):
    params = {**_draft(), 'space_id': sid}
    if scheduled:
        params.update(enabled=True, schedule={'kind': 'every', 'every_seconds': 60})
    return _send(native, 'monitors.rules.create', params,
                 key='monitor-create-' + uuid.uuid4().hex)['data']['rule']


# LLM: owner 路径从已真实解析的 HTTP 服务中取，不按用户名拼路径。
# 函数用途: 取得本测试唯一调用者的 owner Store。
def _store(native):
    services = list(native[0]._business_services.values())
    assert len(services) == 1
    return V13Store(services[0].owner_home, owner_id=services[0].owner_id)


# LLM: W8 种子完整使用真实字段名和规范实体结构。
# 函数用途: 生成一条可被聚合的规范告警。
def _alert(owner, sid, rule_id, alert_id, *, value='host-a', at='2026-10-01T01:00:00+00:00'):
    return {'alert_id': alert_id, 'owner_id': owner, 'space_id': sid, 'rule_id': rule_id,
        'title': '检测事件', 'description': '', 'severity': 'medium', 'status': 'new',
        'verdict': 'true_positive', 'ai_confidence': None, 'disposition': None, 'source': 'monitor',
        'external_ref': None, 'entities': [{'type': 'host', 'value': value}], 'techniques': ['T1059'],
        'assignee': None, 'case_id': None, 'tags': [], 'evidence_refs': [], 'acknowledged_at': None,
        'resolved_at': None, 'revision': 1, 'created_at': at, 'updated_at': at}


# LLM: 唯一可控信号是 override_verdict，不增加虚构 verdict 或 entity 字段。
# 函数用途: 生成一条真实 W8 形状反馈。
def _feedback(alert, override_id, *, verdict='false_positive', at='2026-10-01T02:00:00+00:00'):
    return {'override_id': override_id, 'alert_id': alert['alert_id'], 'owner_id': alert['owner_id'],
        'space_id': alert['space_id'], 'rule_id': alert['rule_id'], 'original_verdict': 'true_positive',
        'override_verdict': verdict, 'reason': '分析师复核', 'analyst': 'analyst-a', 'source': 'override',
        'created_at': at, 'updated_at': at, 'revision': 1}


# LLM: 使用真实 Store 事务种子，不手写底座文件或回执。
# 函数用途: 保存告警和反馈以驱动 HTTP 调优。
def _seed_feedback(native, sid, rule_id, *, count=2):
    store = _store(native)
    alerts = [_alert(store.owner_id, sid, rule_id, 'alert-' + str(index), value='host-' + str(index))
              for index in range(count)]
    harness.v13_seed(store.owner_home, 'soc_alerts', {row['alert_id']: row for row in alerts})
    facts = [_feedback(row, 'override-' + str(index)) for index, row in enumerate(alerts)]
    harness.v13_seed(store.owner_home, 'soc_feedback', {row['override_id']: row for row in facts})
    return alerts


# LLM: 最小直接调用仍使用真实签发上下文和真实空间，只替换注入时钟。
# 函数用途: 提供事务失败与异步回写的隔离领域夹具。
@pytest.fixture
def domain(v13_host):
    from agent_py_agent.agent.user_space.owner_resolver import (
        home_paths_with_owner,
        resolve_owner_home,
    )
    v13_host.home_paths = home_paths_with_owner(v13_host.home_paths, resolve_owner_home(v13_host.home_paths.root))
    context, services = api.local_business_context(v13_host)
    request = {'action': 'spaces.create', 'request_id': 'domain-space', 'params': {'name': '事务空间'},
               'idempotency_key': 'domain-space-create-key'}
    code, body = api.execute_business(request, context, services)
    assert code == 200, body
    sid = body['data']['space_id']

    # LLM: 身份和 owner_home 来自签发上下文，不能从调用 params 伪造。
    # 函数用途: 构造可信领域调用对象。
    def make(action, params, *, key='domain-request-key', revision=None):
        return V13Call(action, params, 'domain-request', context.owner_id, context.subject_id,
            context, services, services.owner_home, key, revision, lambda: 1793670000.0)

    return make, sid


# LLM: 原 issue 示例无需默认空间解析，也不许空成功掩盖未保存提案。
# 函数用途: 验证 pending 列表、过滤、分页游标及失效条件。
def test_list_example_status_pending_without_space_id(v13_native):
    sid = _space_w7_detection(v13_native)
    first = _proposal(v13_native, sid)
    _proposal(v13_native, sid)
    data = _send(v13_native, 'detection_proposals.list', {'status': 'pending', 'limit': 1})['data']
    assert len(data['items']) == 1 and data['next_cursor'] is not None
    second = _send(v13_native, 'detection_proposals.list',
        {'status': 'pending', 'limit': 1, 'cursor': data['next_cursor']})['data']
    assert len(second['items']) == 1 and data['items'] != second['items']
    assert first in data['items'] + second['items']
    failure = _send(v13_native, 'detection_proposals.list',
        {'status': 'approved', 'cursor': data['next_cursor']}, status=410)
    assert failure['error']['code'] == 'CURSOR_EXPIRED'


# LLM: 同键等参回放必须保持完整业务结果，换参必须明确冲突。
# 函数用途: 验证创建状态和幂等冲突。
def test_create_starts_pending(v13_native):
    sid = _space_w7_detection(v13_native)
    first = _proposal(v13_native, sid, key='proposal-fixed-create-key')
    assert first['status'] == 'pending' and first['promoted_rule_id'] is None
    assert _proposal(v13_native, sid, key='proposal-fixed-create-key') == first
    bad = _send(v13_native, 'detection_proposals.create',
        {'space_id': sid, 'title': '其它提案', 'rule': _draft()}, key='proposal-fixed-create-key', status=409)
    assert bad['error']['code'] == 'IDEMPOTENCY_CONFLICT'


# LLM: 缺省只能选择当前 owner 唯一活动空间，零个和多个候选都不能猜测。
# 函数用途: 经真实 HTTP 验证可选空间创建的明确解析策略。
@pytest.mark.parametrize('count,status,code', [(0, 404, 'RESOURCE_NOT_FOUND'), (1, 200, None), (2, 409, 'STATE_CONFLICT')])
def test_create_optional_space_uses_unique_active_space(v13_native, count, status, code):
    spaces = [_space_w7_detection(v13_native) for _index in range(count)]
    result = _send(v13_native, 'detection_proposals.create', {'title': '缺省空间提案', 'rule': _draft()},
        key='default-space-create-key', status=status)
    if status == 200:
        assert result['data']['proposal']['space_id'] == spaces[0]
    else:
        assert result['error']['code'] == code


# LLM: 缺记录与跨 owner 都是 404，不泄露提案存在性。
# 函数用途: 验证 get 的可见性边界。
def test_get_and_404(v13_native):
    row = _proposal(v13_native, _space_w7_detection(v13_native))
    assert _send(v13_native, 'detection_proposals.get', {'proposal_id': row['proposal_id']})['data']['proposal'] == row
    for proposal_id, user in [('absent', 'business-alice'), (row['proposal_id'], 'business-bob')]:
        body = _send(v13_native, 'detection_proposals.get', {'proposal_id': proposal_id}, user=user, status=404)
        assert body['error']['code'] == 'RESOURCE_NOT_FOUND'


# LLM: 同键重放不得增加第二条评论或改写 author。
# 函数用途: 验证评论原子追加与作者绑定。
def test_comment_appends_with_author(v13_native):
    row = _proposal(v13_native, _space_w7_detection(v13_native))
    params = {'proposal_id': row['proposal_id'], 'text': '已核对数据源'}
    data = _send(v13_native, 'detection_proposals.comment', params, key='proposal-comment-key')['data']
    assert data['proposal']['comments'][0]['author'] == row['author']
    assert len(data['proposal']['comments']) == 1
    assert _send(v13_native, 'detection_proposals.comment', params, key='proposal-comment-key')['data'] == data


# LLM: 记录 CAS 不得与集合版本混淆；其它提案写入不能使本记录 CAS 失效。
# 函数用途: 验证评审转换、旧版本拒绝与 rejected 终态。
def test_decide_transitions_and_terminal_conflict(v13_native):
    sid = _space_w7_detection(v13_native)
    row = _proposal(v13_native, sid)
    _proposal(v13_native, sid)
    params = {'proposal_id': row['proposal_id'], 'decision': 'request_changes'}
    first = _send(v13_native, 'detection_proposals.decide', params,
        key='proposal-decide-changes', revision=row['revision'])['data']['proposal']
    assert first['status'] == 'changes_requested'
    stale = _send(v13_native, 'detection_proposals.decide', params,
        key='proposal-decide-stale', revision=row['revision'], status=409)
    assert stale['error']['code'] == 'REVISION_CONFLICT'
    rejected = _send(v13_native, 'detection_proposals.decide', {**params, 'decision': 'reject'},
        key='proposal-decide-reject', revision=first['revision'])['data']['proposal']
    failed = _send(v13_native, 'detection_proposals.decide', {**params, 'decision': 'approve'},
        key='proposal-decide-terminal', revision=rejected['revision'], status=409)
    assert failed['error']['code'] == 'STATE_CONFLICT'


# LLM: 晋升必须创建原生可读规则，且同键回放不能重复创建。
# 函数用途: 验证批准前门禁、origin、CAS 和正式晋升。
def test_promote_requires_approved_and_creates_monitor_rule_once(v13_native):
    sid = _space_w7_detection(v13_native)
    row = _proposal(v13_native, sid)
    params = {'proposal_id': row['proposal_id']}
    failed = _send(v13_native, 'detection_proposals.promote', params,
        key='promotion-unapproved-key', revision=row['revision'], status=409)
    assert failed['error']['code'] == 'STATE_CONFLICT'
    approved = _send(v13_native, 'detection_proposals.decide', {**params, 'decision': 'approve'},
        key='promotion-approve-key', revision=row['revision'])['data']['proposal']
    result = _send(v13_native, 'detection_proposals.promote', params,
        key='promotion-success-key', revision=approved['revision'])['data']
    assert result['proposal']['status'] == 'promoted'
    assert result['rule']['origin'] == 'proposal'
    assert result['proposal']['promoted_rule_id'] == result['rule']['rule_id']
    assert _send(v13_native, 'detection_proposals.promote', params,
        key='promotion-success-key', revision=approved['revision'])['data'] == result
    listed = _send(v13_native, 'monitors.rules.list', {'space_id': sid})['data']['items']
    assert listed == [result['rule']]


# LLM: 只按 override_verdict 和规范 entities 推导建议，不采信展示 verdict。
# 函数用途: 验证真实误报证据、最新改判去重及稳定编号。
def test_tuning_suggestions_derive_from_false_positive_feedback(v13_native):
    sid = _space_w7_detection(v13_native)
    rule = _rule(v13_native, sid)
    alerts = _seed_feedback(v13_native, sid, rule['rule_id'])
    first = _send(v13_native, 'detection_tuning.list', {'rule_id': rule['rule_id']})['data']
    assert len(first['items']) == 2
    assert {row['field'] for row in first['items']} == {'host'}
    assert {row['value'] for row in first['items']} == {'host-0', 'host-1'}
    assert all(row['false_positive_count'] == 1 for row in first['items'])
    store = _store(v13_native)
    before = store.revision('detection_tuning')
    assert _send(v13_native, 'detection_tuning.list', {'rule_id': rule['rule_id']})['data'] == first
    assert store.revision('detection_tuning') == before
    later = _feedback(alerts[0], 'override-later', verdict='true_positive', at='2026-10-02T01:00:00+00:00')
    harness.v13_seed(store.owner_home, 'soc_feedback', {later['override_id']: later})
    second = _send(v13_native, 'detection_tuning.list', {'rule_id': rule['rule_id']})['data']['items']
    assert len(second) == 1 and second[0]['value'] == 'host-1'


# LLM: 摘要计数来自当前真实建议，预计降噪没有回测分母时必须为 null。
# 函数用途: 验证建议统计不编造降噪效果。
def test_tuning_summary_counts(v13_native):
    sid = _space_w7_detection(v13_native)
    rule = _rule(v13_native, sid)
    _seed_feedback(v13_native, sid, rule['rule_id'])
    summary = _send(v13_native, 'detection_tuning.summary', {'space_id': sid})['data']
    assert summary == {'rules_with_suggestions': 1, 'open': 2, 'applied': 0, 'dismissed': 0,
                       'estimated_alert_reduction': None, 'coverage': 'complete'}


# LLM: stale CAS 不能附加排除项；成功后规则和建议通过同一 HTTP 入口回读。
# 函数用途: 验证调优记录 CAS、source、原子状态和幂等重放。
def test_apply_adds_exclusion_with_rule_cas(v13_native):
    sid = _space_w7_detection(v13_native)
    rule = _rule(v13_native, sid)
    _seed_feedback(v13_native, sid, rule['rule_id'], count=1)
    suggestion = _send(v13_native, 'detection_tuning.list', {'rule_id': rule['rule_id']})['data']['items'][0]
    params = {'rule_id': rule['rule_id'], 'suggestion_id': suggestion['suggestion_id']}
    stale = _send(v13_native, 'detection_tuning.apply', params, key='tuning-apply-stale-key',
                  revision=rule['revision'] + 1, status=409)
    assert stale['error']['code'] == 'REVISION_CONFLICT'
    first = _send(v13_native, 'detection_tuning.apply', params, key='tuning-apply-success',
                  revision=rule['revision'])['data']
    assert first['suggestion']['status'] == 'applied'
    assert len(first['rule']['exclusions']) == 1
    assert first['rule']['exclusions'][0]['source'] == 'tuning'
    assert first['rule']['exclusions'][0]['value'] == suggestion['value']
    assert _send(v13_native, 'detection_tuning.apply', params, key='tuning-apply-success',
                 revision=rule['revision'])['data'] == first
    assert _send(v13_native, 'monitors.rules.get', {'rule_id': rule['rule_id']})['data']['rule'] == first['rule']


# LLM: 调优后的原生调度必须包含排除项，不能只改 monitor_rules JSON 让执行仍用旧提示词。
# 函数用途: 经真实 HTTP 和 SchedulerRepository 验证调优原生镜像重绘。
def test_apply_rerenders_native_scheduler_instruction(v13_native):
    from agent_py_agent.agent.assets.script_store_support import delivery_scheduler
    sid = _space_w7_detection(v13_native)
    rule = _rule(v13_native, sid, scheduled=True)
    assert rule['scheduler_job_id'] is not None
    _seed_feedback(v13_native, sid, rule['rule_id'], count=1)
    row = _send(v13_native, 'detection_tuning.list', {'rule_id': rule['rule_id']})['data']['items'][0]
    store = _store(v13_native)
    scheduler = delivery_scheduler(store.owner_home, store.owner_id)
    before = scheduler.get_job(rule['scheduler_job_id'])
    result = _send(v13_native, 'detection_tuning.apply',
        {'rule_id': rule['rule_id'], 'suggestion_id': row['suggestion_id']},
        key='tuning-scheduler-instruction', revision=rule['revision'])['data']
    after = scheduler.get_job(rule['scheduler_job_id'])
    assert after['job_id'] == result['rule']['scheduler_job_id']
    assert after['version'] > before['version']
    assert json.loads(after['prompt'].partition('\n')[2])['exclusions'] == result['rule']['exclusions']


# LLM: 后续新增同实体证据也不能使 dismissed 重开。
# 函数用途: 验证 sticky dismissed、无重复读写及禁止再次应用。
def test_dismiss_is_sticky(v13_native):
    sid = _space_w7_detection(v13_native)
    rule = _rule(v13_native, sid)
    alerts = _seed_feedback(v13_native, sid, rule['rule_id'], count=1)
    row = _send(v13_native, 'detection_tuning.list', {'rule_id': rule['rule_id']})['data']['items'][0]
    params = {'rule_id': rule['rule_id'], 'suggestion_id': row['suggestion_id'], 'reason': '已确认业务用途'}
    first = _send(v13_native, 'detection_tuning.dismiss', params, key='tuning-dismiss-key')['data']
    assert first['suggestion']['status'] == 'dismissed'
    store = _store(v13_native)
    new_alert = {**alerts[0], 'alert_id': 'alert-new'}
    new_feedback = _feedback(new_alert, 'override-new')
    harness.v13_seed(store.owner_home, 'soc_alerts', {'alert-new': new_alert})
    harness.v13_seed(store.owner_home, 'soc_feedback', {'override-new': new_feedback})
    revision = store.revision('detection_tuning')
    rows = _send(v13_native, 'detection_tuning.list', {'rule_id': rule['rule_id']})['data']['items']
    assert rows == [first['suggestion']]
    assert store.revision('detection_tuning') == revision
    failed = _send(v13_native, 'detection_tuning.apply',
        {'rule_id': rule['rule_id'], 'suggestion_id': row['suggestion_id']}, key='tuning-dismissed-apply',
        revision=rule['revision'], status=409)
    assert failed['error']['code'] == 'STATE_CONFLICT'


# LLM: 预览必须在建议尚未物化时也零写，不仅仅比较规则字段。
# 函数用途: 验证 auto_tune 全量应用与 dry_run 的全部业务文件零变化。
def test_auto_tune_applies_all_open_exclusions_or_previews(v13_native):
    sid = _space_w7_detection(v13_native)
    rule = _rule(v13_native, sid)
    _seed_feedback(v13_native, sid, rule['rule_id'])
    store = _store(v13_native)
    root = store.owner_home / 'data' / 'business_v13'
    before = {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}
    params = {'rule_id': rule['rule_id'], 'dry_run': True}
    preview = _send(v13_native, 'detection_tuning.auto_tune', params, key='auto-tune-preview-key',
        revision=rule['revision'])['data']
    assert preview['dry_run'] and preview['rule'] is None and len(preview['applied']) == 2
    after = {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}
    assert after == before
    first = _send(v13_native, 'detection_tuning.auto_tune', {**params, 'dry_run': False},
        key='auto-tune-apply-key', revision=rule['revision'])['data']
    assert len(first['applied']) == 2 and all(row['status'] == 'applied' for row in first['applied'])
    assert len(first['rule']['exclusions']) == 2
    assert {row['source'] for row in first['rule']['exclusions']} == {'auto_tune'}
    assert _send(v13_native, 'detection_tuning.auto_tune', {**params, 'dry_run': False},
        key='auto-tune-apply-key', revision=rule['revision'])['data'] == first


# LLM: 基线必须使用准确窗口与真实分母，缺失反馈不能被解释为零误报。
# 函数用途: 验证实算基线、坏数据排除及反馈最新改判去重。
def test_baselines_list_and_create(v13_native):
    sid = _space_w7_detection(v13_native)
    rule = _rule(v13_native, sid)
    alerts = _seed_feedback(v13_native, sid, rule['rule_id'])
    store = _store(v13_native)
    broken = {**alerts[0], 'alert_id': 'broken', 'created_at': None}
    harness.v13_seed(store.owner_home, 'soc_alerts', {'broken': broken, 'duplicate': alerts[0]})
    last = _feedback(alerts[1], 'override-final', verdict='true_positive', at='2026-10-01T03:00:00+00:00')
    harness.v13_seed(store.owner_home, 'soc_feedback', {'override-final': last})
    window = {'since': '2026-10-01T00:00:00+00:00', 'until': '2026-10-03T00:00:00+00:00'}
    params = {'space_id': sid, 'rule_id': rule['rule_id'], 'window': window, 'metric': 'alerts_per_day'}
    first = _send(v13_native, 'detection_proposals.baselines.create', params, key='baseline-alert-rate')['data']['baseline']
    assert first['value'] == 1.0
    ratio = _send(v13_native, 'detection_proposals.baselines.create', {**params, 'metric': 'fp_ratio'},
        key='baseline-fp-ratio')['data']['baseline']
    assert ratio['value'] == 0.5
    assert _send(v13_native, 'detection_proposals.baselines.create', params,
        key='baseline-alert-rate')['data']['baseline'] == first
    listed = _send(v13_native, 'detection_proposals.baselines.list', {'space_id': sid})['data']['items']
    assert len(listed) == 2 and first in listed and ratio in listed
    bad = _send(v13_native, 'detection_proposals.baselines.create',
        {**params, 'window': {'since': window['until'], 'until': window['since']}}, key='baseline-bad-window', status=400)
    assert bad['error']['code'] == 'INVALID_SCHEMA'


# LLM: 单元边界只替换异步派发端口；目标预留、状态转换与回写都使用真实 Store。
# 函数用途: 验证三种评估模式、批量 pending 选择与同键不扩批。
def test_evaluate_modes_and_run_eval_batch(domain, monkeypatch):
    make, sid = domain
    dispatched = []

    # LLM: 替身只模拟受理 ID，不返回任何评估业务结论。
    # 函数用途: 记录真实输入并提供稳定作业标识。
    def start(call, kind, instruction, *, space_id=None):
        dispatched.append((call, kind, instruction, space_id))
        return DomainResult(202, {'state': 'accepted', 'operation_id': 'operation-' + call.idempotency_key,
            'job_id': lifecycle._operation_id(call, 'job'), 'task_id': 'task-' + call.idempotency_key})

    monkeypatch.setattr(lifecycle._api(), 'v13_start_model_job', start)
    for index, mode in enumerate(('eval', 'rule', 'backtest')):
        row = lifecycle.create_proposal(make('detection_proposals.create',
            {'space_id': sid, 'title': mode, 'rule': _draft()}, key='mode-create-' + str(index)))['proposal']
        params = {'space_id': sid, 'proposal_id': row['proposal_id'], 'mode': mode}
        if mode == 'backtest':
            params['window'] = {'since': '2026-10-01T00:00:00+00:00', 'until': '2026-10-02T00:00:00+00:00'}
        result = lifecycle.evaluate(make('detection_proposals.evaluate', params, key='mode-eval-' + str(index)))
        assert result.http_status == 202 and len(result.data['job_ids']) == 1
        current = lifecycle.get_proposal(make('detection_proposals.get', {'proposal_id': row['proposal_id']}))['proposal']
        assert current['status'] == 'evaluating' and current['evaluations'][0]['mode'] == mode
    pending = lifecycle.create_proposal(make('detection_proposals.create',
        {'space_id': sid, 'title': '批量', 'rule': _draft()}, key='batch-proposal-first'))['proposal']
    batch = make('detection_proposals.evaluate', {'space_id': sid}, key='batch-evaluation-key')
    first = lifecycle.evaluate(batch)
    assert len(first.data['job_ids']) == 1
    assert pending['proposal_id'] in dispatched[-1][2]
    lifecycle.create_proposal(make('detection_proposals.create',
        {'space_id': sid, 'title': '后来新增', 'rule': _draft()}, key='batch-proposal-later'))
    assert lifecycle.evaluate(batch).data == first.data
    assert len(first.data['job_ids']) == 1
    implicit = lifecycle.evaluate(make('detection_proposals.evaluate', {}, key='batch-default-space-key'))
    assert len(implicit.data['job_ids']) == 1
    assert dispatched[-1][0].params['space_id'] == sid and dispatched[-1][3] == sid


# LLM: 测试 W0 真实派发链的受理结果，不用单元替身冒充端到端。
# 函数用途: 验证真实 HTTP 评估返回 accepted 和稳定作业编号。
def test_evaluate_accepts_through_real_http(v13_native, monkeypatch):
    import traceback

    from agent_py_agent.agent.conversation import session_task_dispatch
    frozen = lifecycle._api().v13_call_frozen
    original_report = session_task_dispatch.runtime_error_report
    failures = []

    # LLM: 仅观察原异常的真实 traceback，错误分类和回执仍由原报告函数生成。
    # 函数用途: 保存派发内部捕获的原始失败位置。
    def report(error, *, context):
        failures.append({'dispatch_traceback': ''.join(traceback.format_exception(error))})
        return original_report(error, context=context)

    # LLM: 观察器仍执行原调用、原参数和原结果，只给失败附上准确边界证据。
    # 函数用途: 捕获真实原生调用失败位置而不替换业务行为。
    def observe(call, action, params, *, idempotency_key=None, expected_revision=None):
        try:
            return frozen(call, action, params, idempotency_key=idempotency_key,
                          expected_revision=expected_revision)
        except (HostAPIError, BusinessError, OSError, ValueError, TypeError, RuntimeError) as error:
            failures.append({'action': action, 'traceback': traceback.format_exc(),
                             'outcome': repr(getattr(error, 'outcome', None))})
            raise

    monkeypatch.setattr(lifecycle._api(), 'v13_call_frozen', observe)
    monkeypatch.setattr(session_task_dispatch, 'runtime_error_report', report)
    sid = _space_w7_detection(v13_native)
    row = _proposal(v13_native, sid)
    params = {'space_id': sid, 'proposal_id': row['proposal_id'], 'mode': 'eval'}
    try:
        first = _send(v13_native, 'detection_proposals.evaluate', params, key='http-evaluate-native-key', status=202)['data']
    except AssertionError as error:
        error.add_note(json.dumps(failures, ensure_ascii=False))
        raise
    assert len(first['job_ids']) == 1
    assert _send(v13_native, 'detection_proposals.evaluate', params,
        key='http-evaluate-native-key', status=202)['data'] == first


# LLM: 冻结契约是非空短字符串，不额外把业务技术标签收紧为 MITRE 正则。
# 函数用途: 验证合法自定义技术标签能创建和回读。
@pytest.mark.parametrize('technique', ['custom_technique', 'local_tag'])
def test_custom_technique_matches_frozen_contract(domain, technique):
    make, sid = domain
    row = lifecycle.create_proposal(make('detection_proposals.create',
        {'space_id': sid, 'title': '自定义标签', 'rule': _draft(),
         'mitre_techniques': [technique]}))['proposal']
    assert row['mitre_techniques'] == [technique]
    saved = lifecycle.get_proposal(make('detection_proposals.get', {'proposal_id': row['proposal_id']}))['proposal']
    assert saved == row


# LLM: 没有任何目标便没有真实 operation，不得返回凭空构造的 accepted 身份。
# 函数用途: 验证空评估批次明确拒绝且不派发模型。
def test_empty_evaluation_batch_does_not_fake_acceptance(domain, monkeypatch):
    make, sid = domain
    dispatched = []

    # LLM: 捕获是否调用派发，不能提供任何假业务结论。
    # 函数用途: 阻止空批次偷偷启动模型。
    def start(call, kind, instruction, *, space_id=None):
        dispatched.append(kind)
        raise AssertionError('空目标不得派发')

    monkeypatch.setattr(lifecycle._api(), 'v13_start_model_job', start)
    with pytest.raises(BusinessError) as caught:
        lifecycle.evaluate(make('detection_proposals.evaluate', {'space_id': sid}))
    assert caught.value.code == 'STATE_CONFLICT'
    assert dispatched == []


# LLM: 原规则和最多 200 建议共享同一 CAS 事务；重放不能再改 revision 或追加排除项。
# 函数用途: 经真实 HTTP 验证自动调优超过旧 100 条限制及 200 条契约边界。
@pytest.mark.parametrize('count', [101, 200])
def test_auto_tune_full_contract_batch_is_atomic_and_replay_safe(v13_native, count):
    sid = _space_w7_detection(v13_native)
    rule = _rule(v13_native, sid)
    _seed_feedback(v13_native, sid, rule['rule_id'], count=count)
    params = {'rule_id': rule['rule_id'], 'dry_run': False}
    first = _send(v13_native, 'detection_tuning.auto_tune', params,
        key='full-contract-batch-key', revision=rule['revision'])['data']
    assert len(first['applied']) == count
    saved = _store(v13_native).get('monitor_rules', rule['rule_id'])
    assert len(saved['exclusions']) == count and saved['revision'] == rule['revision'] + 1
    suggestions = _store(v13_native).list('detection_tuning')
    assert len(suggestions) == count and all(row['status'] == 'applied' for row in suggestions)
    assert _send(v13_native, 'detection_tuning.auto_tune', params,
        key='full-contract-batch-key', revision=rule['revision'])['data'] == first
    assert _store(v13_native).get('monitor_rules', rule['rule_id']) == saved


# LLM: 种子只建立 W0 已受理作业的关联，评估结论仍交被测回写函数验证。
# 函数用途: 准备一条待回写提案和规范作业终态。
def _evaluation_fixture(domain):
    make, sid = domain
    base = make('detection_proposals.create', {'space_id': sid, 'title': '评估', 'rule': _draft()})
    row = lifecycle.create_proposal(base)['proposal']
    stored = V13Store(base.owner_home).get('detection_proposals', row['proposal_id'])
    stored['status'] = 'evaluating'
    stored['evaluations'] = [{'job_id': 'job-evaluation', 'mode': 'eval', 'state': 'accepted',
                              'summary': None, 'at': row['created_at']}]
    harness.v13_seed(base.owner_home, 'detection_proposals', {row['proposal_id']: stored})
    job = {'job_id': 'job-evaluation', 'owner_id': base.owner_id, 'space_id': sid,
           'kind': 'detection_proposal_evaluation', 'state': 'completed', 'result': {
               'proposal_id': row['proposal_id'], 'mode': 'eval', 'matches': None,
               'estimated_false_positive_ratio': None, 'findings': ['没有可访问的历史数据'], 'coverage': 'unknown'}}
    return make('async_jobs.get', {'job_id': job['job_id']}), row, job


# LLM: W0 终态提交之前必须校验领域绑定，不能先把错结果记为 completed。
# 函数用途: 验证完成前验证器拒绝错模式和错提案，且不变更业务状态。
@pytest.mark.parametrize('field,value', [('proposal_id', 'another-proposal'), ('mode', 'backtest')])
def test_evaluation_validator_rejects_before_completion(domain, field, value):
    call, row, job = _evaluation_fixture(domain)
    from agent_py_agent.agent.gateway_parts.business_api_v13_w7 import V13_JOB_KINDS
    validator = V13_JOB_KINDS['detection_proposal_evaluation']['validate_result']
    assert validator(call, job, job['result']) is True
    job['result'][field] = value
    with pytest.raises((BusinessError, ValueError, HostAPIError)):
        validator(call, job, job['result'])
    assert V13Store(call.owner_home).get('detection_proposals', row['proposal_id'])['status'] == 'evaluating'


# LLM: 完成回写只能更新准确 job_id，重读不得重复追加或提升覆盖。
# 函数用途: 验证评估结果回写、失败回收与终态单调性。
@pytest.mark.parametrize('state', ['completed', 'failed'])
def test_complete_evaluation_bound_and_replay_safe(domain, state):
    call, row, job = _evaluation_fixture(domain)
    job['state'] = state
    if state == 'failed':
        job['result'] = None
    first = lifecycle.complete_evaluation(call, job)
    assert first['updated'] is True
    proposal = first['proposal']
    assert proposal['status'] == 'pending' and len(proposal['evaluations']) == 1
    assert proposal['evaluations'][0]['state'] == state
    assert proposal['evaluations'][0]['summary'] == job['result']
    assert lifecycle.complete_evaluation(call, job) == {'updated': False}
    late = lifecycle.complete_evaluation(call, {**job, 'state': 'running'})
    assert late == {'updated': False}
    assert V13Store(call.owner_home).get('detection_proposals', row['proposal_id'])['revision'] == proposal['revision']


# LLM: 合法 schema 不足以授权跨提案或错模式回写。
# 函数用途: 验证作业结果的准确领域绑定。
@pytest.mark.parametrize('field,value', [('proposal_id', 'another-proposal'), ('mode', 'backtest')])
def test_complete_evaluation_rejects_wrong_binding(domain, field, value):
    call, row, job = _evaluation_fixture(domain)
    job['result'][field] = value
    with pytest.raises(BusinessError) as caught:
        lifecycle.complete_evaluation(call, job)
    assert caught.value.code == 'DOMAIN_RESULT_INVALID'
    assert V13Store(call.owner_home).get('detection_proposals', row['proposal_id'])['status'] == 'evaluating'


# LLM: 只种入规范已持久化终态，真实 HTTP get 必须触发 W0 注册观察器并更新提案。
# 函数用途: 验证终态重读恢复路径，而非只直接调用 complete_evaluation。
@pytest.mark.parametrize('state', ['completed', 'failed'])
def test_async_jobs_get_writes_bound_proposal_through_http(v13_native, state):
    sid = _space_w7_detection(v13_native)
    public = _proposal(v13_native, sid)
    store = _store(v13_native)
    proposal = store.get('detection_proposals', public['proposal_id'])
    job_id, at = 'job-http-terminal-observation', proposal['created_at']
    proposal.update(status='evaluating', revision=proposal['revision'] + 1, evaluations=[{
        'job_id': job_id, 'mode': 'eval', 'state': 'accepted', 'summary': None, 'at': at}])
    result = {'proposal_id': proposal['proposal_id'], 'mode': 'eval', 'matches': None,
              'estimated_false_positive_ratio': None, 'findings': ['缺少可回测证据'], 'coverage': 'unknown'}
    job = {'job_id': job_id, 'owner_id': store.owner_id, 'kind': 'detection_proposal_evaluation',
        'action': 'detection_proposals.evaluate', 'subject_id': public['author'], 'space_id': sid,
        'agent_id': 'agent-terminal-fixture', 'task_id': 'task-terminal-fixture',
        'operation_id': 'operation-terminal-fixture', 'state': state,
        'result': result if state == 'completed' else None,
        'error': None if state == 'completed' else {'code': 'JOB_TASK_FAILED', 'message': '任务失败'},
        'revision': 1, 'created_at': at, 'updated_at': at}
    harness.v13_seed(store.owner_home, 'detection_proposals', {proposal['proposal_id']: proposal})
    harness.v13_seed(store.owner_home, 'async_jobs', {job_id: job})
    observed = _send(v13_native, 'async_jobs.get', {'job_id': job_id})['data']
    current = _send(v13_native, 'detection_proposals.get', {'proposal_id': proposal['proposal_id']})['data']['proposal']
    assert observed['state'] == state and observed['result'] == job['result']
    assert current['status'] == 'pending' and current['evaluations'][0]['summary'] == job['result']
    assert current['evaluations'][0]['state'] == state and len(current['evaluations']) == 1
    assert _send(v13_native, 'async_jobs.get', {'job_id': job_id})['data'] == observed
    assert _send(v13_native, 'detection_proposals.get', {'proposal_id': proposal['proposal_id']})['data']['proposal'] == current


# LLM: 结构化输出校验不可被额外字段、负数或非有限数字绕过。
# 函数用途: 固定模型结果 schema 的拒绝边界。
@pytest.mark.parametrize('change', [{'extra': True}, {'matches': -1}, {'estimated_false_positive_ratio': float('nan')}])
def test_finalize_evaluation_rejects_invalid(change):
    result = {'proposal_id': 'proposal-example', 'mode': 'eval', 'matches': None,
              'estimated_false_positive_ratio': None, 'findings': [], 'coverage': 'unknown'}
    result.update(change)
    with pytest.raises((ValueError, HostAPIError)):
        lifecycle.finalize_evaluation(json.dumps(result))


# LLM: 同一原生规则创建路径必须在两个集合提交前统一收口。
# 函数用途: 准备事务故障测试用的已批准提案调用。
def _approved_call(domain):
    make, sid = domain
    row = lifecycle.create_proposal(make('detection_proposals.create',
        {'space_id': sid, 'title': '原子晋升', 'rule': _draft()}))['proposal']
    approved = lifecycle.decide(make('detection_proposals.decide',
        {'proposal_id': row['proposal_id'], 'decision': 'approve'}, revision=row['revision']))['proposal']
    return make('detection_proposals.promote', {'proposal_id': row['proposal_id']},
                key='atomic-promotion-key', revision=approved['revision'])


# LLM: 注入的是原子写叶的真实失败，不用忽略异常的外层成功冒充通过。
# 函数用途: 验证多集合提交前失败和提交后物化失败均可同键安全恢复。
@pytest.mark.parametrize('phase', ['before_commit', 'after_commit'])
def test_promote_atomic_failure_and_retry(domain, monkeypatch, phase):
    from agent_py_agent.agent.business_spaces import v13_store as storage
    call = _approved_call(domain)
    original = storage.write_bytes_atomic_beneath
    failures = []

    # LLM: 只在本次晋升的准确两集合事务观察点失败一次。
    # 函数用途: 注入提交点前后故障。
    def fail(root, parts, data):
        payload = json.loads(data) if parts[-1] == '.transaction.json' else None
        before = payload is not None and set(payload.get('states', {})) == {'detection_proposals', 'monitor_rules'}
        after = parts[-1] == 'monitor_rules.json'
        if not failures and ((phase == 'before_commit' and before) or (phase == 'after_commit' and after)):
            failures.append(phase)
            raise OSError('injected write failure')
        return original(root, parts, data)

    monkeypatch.setattr(storage, 'write_bytes_atomic_beneath', fail)
    with pytest.raises(OSError):
        lifecycle.promote(call)
    assert failures == [phase]
    store = V13Store(call.owner_home)
    proposal = store.get('detection_proposals', call.params['proposal_id'])
    rules = store.list('monitor_rules')
    if phase == 'before_commit':
        assert proposal['status'] == 'approved' and rules == []
    else:
        assert proposal['status'] == 'promoted' and len(rules) == 1
        assert proposal['promoted_rule_id'] == rules[0]['rule_id']
    first = lifecycle.promote(call)
    assert lifecycle.promote(call) == first
    assert len(store.list('monitor_rules')) == 1


# LLM: 参数闭合、缺 CAS 和未知动作须在领域写入前拒绝。
# 函数用途: 经真实 HTTP 验证错误信封。
@pytest.mark.parametrize('action,params,key,revision,code', [
    ('detection_proposals.create', {'title': 'test', 'rule': _draft(), 'unknown': True}, 'schema-error-key', None, 'INVALID_SCHEMA'),
    ('detection_proposals.decide', {'proposal_id': 'missing', 'decision': 'approve'}, 'missing-cas-key', None, 'INVALID_SCHEMA'),
    ('detection_proposals.unknown', {}, None, None, 'UNSUPPORTED_ACTION'),
])
def test_w7_detection_envelopes(v13_native, action, params, key, revision, code):
    body = _send(v13_native, action, params, key=key, revision=revision, status=400)
    assert body['error']['code'] == code


# LLM: 别名只映射规范动作，不建立另一套提案状态或授权路径。
# 函数用途: 经真实 HTTP 验证 monitors.proposals 别名和发现映射。
def test_monitors_proposals_aliases_resolve_to_detection_proposals(v13_native):
    sid = _space_w7_detection(v13_native)
    row = _proposal(v13_native, sid)
    assert _send(v13_native, 'monitors.proposals.get', {'proposal_id': row['proposal_id']})['data']['proposal'] == row
    assert _send(v13_native, 'monitors.proposals.list', {'status': 'pending'})['data']['items'] == [row]


# LLM: 原生手动创建路径不依赖分片 readiness 的 HTTP 预检，可独立验证来源计算。
# 函数用途: 为直接领域回归创建真实规则与 W8 来源。
def _direct_sources(domain):
    from agent_py_agent.agent.business_spaces.v13_w7_monitors import create_rule
    make, sid = domain
    call = make('monitors.rules.create', {**_draft(), 'space_id': sid}, key='direct-monitor-key')
    rule = create_rule(call)['rule']
    alerts = [_alert(call.owner_id, sid, rule['rule_id'], 'direct-alert-' + str(index),
                     value='direct-host-' + str(index)) for index in range(2)]
    harness.v13_seed(call.owner_home, 'soc_alerts', {row['alert_id']: row for row in alerts})
    feedback = [_feedback(row, 'direct-feedback-' + str(index)) for index, row in enumerate(alerts)]
    harness.v13_seed(call.owner_home, 'soc_feedback', {row['override_id']: row for row in feedback})
    return make, sid, call, rule, alerts


# LLM: 明显异常先排除，重复告警不翻倍，虚构反馈字段不能生成建议。
# 函数用途: 验证来源清洗、实际数字与覆盖降级。
def test_direct_source_anomalies_and_real_baselines(domain):
    make, sid, call, rule, alerts = _direct_sources(domain)
    broken = {**alerts[0], 'alert_id': 'broken', 'created_at': None}
    huge_id = {**alerts[0], 'alert_id': 'x' * 129}
    harness.v13_seed(call.owner_home, 'soc_alerts', {'duplicate': alerts[0], 'broken': broken, 'huge': huge_id})
    fabricated = {'override_id': 'fake', 'owner_id': call.owner_id, 'alert_id': alerts[0]['alert_id'],
                  'space_id': sid, 'rule_id': rule['rule_id'], 'verdict': 'false_positive', 'source': 'override',
                  'created_at': '2026-10-01T02:00:00+00:00'}
    harness.v13_seed(call.owner_home, 'soc_feedback', {'fake': fabricated})
    rows, coverage = lifecycle._derived_tuning(make('detection_tuning.list', {'rule_id': rule['rule_id']}))
    assert len(rows) == 2 and coverage == 'partial'
    assert all(row['false_positive_count'] == 1 for row in rows.values())
    window = {'since': '2026-10-01T00:00:00+00:00', 'until': '2026-10-03T00:00:00+00:00'}
    baseline_call = make('detection_proposals.baselines.create',
        {'space_id': sid, 'rule_id': rule['rule_id'], 'metric': 'alerts_per_day', 'window': window})
    assert lifecycle.create_baseline(baseline_call)['baseline']['value'] == 1.0
    ratio = lifecycle.create_baseline(replace(baseline_call,
        params={**baseline_call.params, 'metric': 'fp_ratio'}, idempotency_key='direct-fp-baseline'))
    assert ratio['baseline']['value'] == 1.0
    empty = replace(baseline_call, params={**baseline_call.params, 'metric': 'fp_ratio',
        'window': {'since': '2025-10-01T00:00:00+00:00', 'until': '2025-10-02T00:00:00+00:00'}},
        idempotency_key='empty-fp-baseline')
    with pytest.raises(BusinessError) as caught:
        lifecycle.create_baseline(empty)
    assert caught.value.code == 'STATE_CONFLICT'


# LLM: 零写测试在新派生建议尚未物化、专用编排锁尚不存在时执行。
# 函数用途: 验证 dry_run 不创建业务锁、回执、审计或建议文件。
def test_direct_dry_run_has_zero_writes(domain):
    make, _sid, call, rule, _alerts = _direct_sources(domain)
    root = call.owner_home / 'data' / 'business_v13'
    before = {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}
    preview_call = make('detection_tuning.auto_tune', {'rule_id': rule['rule_id'], 'dry_run': True},
                        revision=rule['revision'])
    result = lifecycle.auto_tune(preview_call)
    after = {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}
    assert len(result['applied']) == 2 and result['dry_run'] and result['rule'] is None
    assert after == before


# LLM: 来源更新不能撤销分析师的明确忽略决定。
# 函数用途: 验证未先 list 也能直接忽略派生建议且后续重建零重复写。
def test_direct_dismiss_is_sticky_before_materialization(domain):
    make, _sid, call, rule, alerts = _direct_sources(domain)
    listed = make('detection_tuning.list', {'rule_id': rule['rule_id']})
    derived, _coverage = lifecycle._derived_tuning(listed)
    row = sorted(derived.values(), key=lambda item: item['suggestion_id'])[0]
    dismiss = make('detection_tuning.dismiss', {'rule_id': rule['rule_id'],
        'suggestion_id': row['suggestion_id'], 'reason': '保留的业务实体'})
    first = lifecycle.dismiss_tuning(dismiss)['suggestion']
    assert first['status'] == 'dismissed'
    selected_alert = next(alert for alert in alerts if alert['entities'][0]['value'] == row['value'])
    new = {**selected_alert, 'alert_id': 'new-evidence'}
    harness.v13_seed(call.owner_home, 'soc_alerts', {new['alert_id']: new})
    feedback = _feedback(new, 'new-feedback')
    harness.v13_seed(call.owner_home, 'soc_feedback', {feedback['override_id']: feedback})
    materialized = lifecycle.list_tuning(listed)['items']
    assert first in materialized
    store = V13Store(call.owner_home)
    revision = store.revision('detection_tuning')
    assert lifecycle.list_tuning(listed)['items'] == materialized
    assert store.revision('detection_tuning') == revision


# LLM: 模拟实时 space 撤权时，基础请求事实仍允许，但准确 record 事实必须拒绝。
# 函数用途: 构造带可切换准确空间授权的已签发调用。
def _revocable_comment(domain):
    make, sid = domain
    row = lifecycle.create_proposal(make('detection_proposals.create',
        {'space_id': sid, 'title': '撤权回归', 'rule': _draft()}))['proposal']
    call = make('detection_proposals.comment', {'proposal_id': row['proposal_id'], 'text': '核对'},
                key='revocable-comment-key')
    policy = {'allowed': True}

    # LLM: 缺空间事实的基础检查不能代替精确 record 授权。
    # 函数用途: 在运行中对实际空间撤权。
    def authorize(action, facts):
        return facts.get('space_id') != sid or policy['allowed']

    context = api.bind_business_context(call.context.host, authorize=authorize, subject_id=call.subject_id)
    return replace(call, context=context), policy


# LLM: 同键回放必须重新核当前真实记录，不能直接漏出第一次成功回执。
# 函数用途: 验证空间撤权会拒绝已成功写入的同键重放。
def test_replay_rechecks_actual_record_space_permission(domain):
    call, policy = _revocable_comment(domain)
    first = lifecycle.comment(call)
    policy['allowed'] = False
    with pytest.raises(HostAPIError) as caught:
        lifecycle.comment(call)
    assert caught.value.code == 'CAPABILITY_DENIED'
    policy['allowed'] = True
    assert lifecycle.comment(call) == first


# LLM: 执行期间撤权必须在日志提交前再次拒绝，不能只在入口检查一次。
# 函数用途: 验证提交末核拒绝不会保存领域记录或成功回执。
def test_commit_rechecks_actual_record_space_permission(domain, monkeypatch):
    call, policy = _revocable_comment(domain)
    project = lifecycle._proposal_view

    # LLM: 领域 mutate 已完成但 Store 尚未提交时改变 live 授权。
    # 函数用途: 注入提交前撤权观察点。
    def revoke(row):
        result = project(row)
        policy['allowed'] = False
        return result

    monkeypatch.setattr(lifecycle, '_proposal_view', revoke)
    with pytest.raises(HostAPIError) as caught:
        lifecycle.comment(call)
    assert caught.value.code == 'CAPABILITY_DENIED'
    assert V13Store(call.owner_home).get('detection_proposals', call.params['proposal_id'])['comments'] == []


# === Source: agent_py_agent/tests/test_v13_w7_scripts.py ===

_native_gateway = harness.v13_native


# LLM: 原生审批读口要求真实队列目录已初始化；只补测试服务器启动条件，不创建票据或替换 reader。
# 函数用途: 在原 HTTP 服务器上初始化待处理目录，让审批读取具备真实来源。
@pytest.fixture
def v13_native(_native_gateway):
    server, agent = _native_gateway
    server.paths.processing.mkdir(parents=True, exist_ok=True)
    return server, agent


# LLM: 原上下文由真实 binder 签发，不能以 SimpleNamespace 冒充权限。
# 函数用途: 创建隔离 owner 的原业务调用环境。
@pytest.fixture
def world(v13_host):
    from agent_py_agent.agent.user_space.owner_resolver import (
        home_paths_with_owner,
        resolve_owner_home,
    )
    v13_host.home_paths = home_paths_with_owner(v13_host.home_paths, resolve_owner_home(v13_host.home_paths.root))
    context = api.bind_business_context(v13_host, authorize=lambda action, facts: True, subject_id='script-tester')
    return SimpleNamespace(host=v13_host, context=context, services=api.BusinessServices(v13_host))


# LLM: 每次请求经过正式 dispatcher，不跳过 schema 或返回值验证。
# 函数用途: 发起一次进程内真实业务调用。
def invoke(world, action, params, *, key=None, revision=None):
    request = {'action': action, 'request_id': 'script-test-' + uuid4().hex, 'params': params}
    if key is not None:
        request['idempotency_key'] = key
    if revision is not None:
        request['expected_revision'] = revision
    return api.execute_business(request, world.context, world.services)


# LLM: 合成代码只输出 JSON，不触碰外部服务或真实凭据。
# 函数用途: 通过公开创建入口保存可执行测试脚本。
def create(world, script_id='collect', *, patch=None, key=None):
    params = {'script_id': script_id, 'name': '固定采集', 'code': 'print(\'{"value": 7}\')'}
    params.update(patch or {})
    status, body = invoke(world, 'scripts.create', params, key=key or 'script-create-' + uuid4().hex)
    assert status == 200, body
    assert body['state'] == 'completed' and body['error'] is None
    return body['data']['script']


# LLM: 每个错误都核对真实运输状态、闭合失败信封和准确错误码。
# 函数用途: 断言正式业务失败响应。
def assert_error(result, status, code):
    actual, body = result
    assert actual == status, body
    assert body['state'] == 'failed' and body['data'] == {}, body
    assert body['error']['code'] == code, body


# LLM: 参数筛选不能误删 any 脚本，列表不携带代码或宿主文件路径。
# 函数用途: 验证 #73-1 的上下文、标签、字段和分页合同。
def test_list_filters_context_tag_and_projects_contract_fields(world):
    create(world, 'alert-collect', patch={'tags': ['osquery'], 'contexts': ['alert']})
    create(world, 'case-collect', patch={'tags': ['osquery'], 'contexts': ['case']})
    create(world, 'any-collect', patch={'tags': ['osquery']})
    status, body = invoke(world, 'scripts.list', {'context': 'alert', 'tag': 'osquery'})
    assert status == 200, body
    rows = body['data']['items']
    assert [row['script_id'] for row in rows] == ['alert-collect', 'any-collect']
    assert set(rows[0]) == {'script_id', 'name', 'description', 'input_schema', 'risk_level',
        'requires_approval', 'enabled', 'tags', 'contexts', 'scope_layer', 'revision', 'last_run'}
    assert rows[0]['risk_level'] == 'low' and rows[0]['requires_approval'] is False
    assert body['data']['next_cursor'] is None


# LLM: 合成旧资产直接来自原生 Store，发现入口必须补齐输入而不改写旧定义。
# 函数用途: 验证 #73-2 详情、schema 回退、风险与脱敏。
def test_get_returns_input_schema_risk_and_approval_flag(world):
    home = world.host.home_paths.owner_home_dir
    store = ScriptStore(home)
    store.save_script(AssetDescriptor(id='legacy', kind='script', name='旧资产',
        io_contract=IOContract(inputs=('number',))), 'print("{}")')
    status, body = invoke(world, 'scripts.get', {'script_id': 'legacy'})
    assert status == 200, body
    row = body['data']['script']
    assert row['input_schema'] == {'type': 'object', 'properties': {'number': {}},
        'required': ['number'], 'additionalProperties': False}
    assert row['code'] == 'print("{}")' and row['entrypoint'] == 'main.py'
    assert row['risk_level'] == 'low' and row['requires_approval'] is False
    assert 'entrypoint_path' not in row
    created = create(world, 'guarded', patch={'risk_level': 'high', 'requires_approval': True})
    assert created['risk_level'] == 'high' and created['requires_approval'] is True
    assert_error(invoke(world, 'scripts.get', {'script_id': 'absent'}), 404, 'RESOURCE_NOT_FOUND')


# LLM: 同键同参跨服务实例应重放，新键重复 ID 必须冲突，不允许静默覆盖。
# 函数用途: 验证 #73-3 创建与持久幂等行为。
def test_create_then_duplicate_is_state_conflict(world):
    key = 'stable-create-key-0001'
    first = create(world, key=key)
    world.services = api.BusinessServices(world.host)
    replay = create(world, key=key)
    assert replay == first
    assert_error(invoke(world, 'scripts.create', {'script_id': 'collect', 'name': '同名', 'code': 'pass'},
        key='other-create-key-0001'), 409, 'STATE_CONFLICT')
    assert_error(invoke(world, 'scripts.create', {'script_id': 'other', 'name': '另一份', 'code': 'pass'},
        key=key), 409, 'IDEMPOTENCY_CONFLICT')
    assert_error(invoke(world, 'scripts.create', {'script_id': 'bad', 'name': '错误入口', 'code': '',
        'entrypoint': '../main.py'}, key='bad-entrypoint-key-0001'), 400, 'INVALID_SCHEMA')
    assert not (world.host.home_paths.owner_home_dir / 'scripts/bad').exists()


# LLM: 集合修订与脚本修订故意不同，防止错误使用集合 CAS；未改的原始代码必须保留。
# 函数用途: 验证 #73-4 记录 CAS、启停和安全更新。
def test_update_cas_and_enable_toggle(world):
    first = create(world, patch={'code': 'token = 1\nprint("{}")'})
    create(world, 'another')
    params = {'script_id': 'collect', 'description': '更新描述', 'enabled': False}
    status, body = invoke(world, 'scripts.update', params, key='update-script-key-0001', revision=first['revision'])
    assert status == 200, body
    updated = body['data']['script']
    assert updated['enabled'] is False and updated['description'] == '更新描述'
    assert updated['revision'] > first['revision']
    raw = ScriptStore(world.host.home_paths.owner_home_dir).get_script('collect')
    assert raw['code'] == 'token = 1\nprint("{}")'
    assert_error(invoke(world, 'scripts.update', {'script_id': 'collect', 'enabled': True},
        key='stale-update-key-0001', revision=first['revision']), 409, 'REVISION_CONFLICT')
    world.services = api.BusinessServices(world.host)
    replay = invoke(world, 'scripts.update', params, key='update-script-key-0001', revision=first['revision'])
    assert replay[0] == 200 and replay[1]['data']['script'] == updated
    assert_error(invoke(world, 'scripts.run', {'script_id': 'collect'}, key='disabled-run-key-0001'), 409, 'STATE_CONFLICT')


# LLM: 删除必须核对匹配记录版本，启用监控引用阻止删除而非留下悬空配置。
# 函数用途: 验证 #73-5 删除 CAS、引用冲突与删除幂等。
def test_delete_requires_matching_revision(world):
    row = create(world)
    assert_error(invoke(world, 'scripts.delete', {'script_id': 'collect'}, key='stale-delete-key-0001',
        revision=row['revision'] + 1), 409, 'REVISION_CONFLICT')
    home = world.host.home_paths.owner_home_dir
    v13_seed(home, 'monitor_rules', {'rule-one': {'rule_id': 'rule-one',
        'owner_id': world.context.owner_id, 'script_id': 'collect', 'enabled': True}})
    assert_error(invoke(world, 'scripts.delete', {'script_id': 'collect'}, key='used-delete-key-0001',
        revision=row['revision']), 409, 'STATE_CONFLICT')
    store = V13Store(home)
    store.transaction('monitor_rules', lambda records: records.clear() or {},
        idempotency_key='clear-rule-key-0001', params_digest='clear')
    result = invoke(world, 'scripts.delete', {'script_id': 'collect'}, key='delete-script-key-0001', revision=row['revision'])
    assert result[0] == 200 and result[1]['data'] == {'script_id': 'collect', 'deleted': True}
    assert invoke(world, 'scripts.delete', {'script_id': 'collect'}, key='delete-script-key-0001',
        revision=row['revision'])[1]['data'] == result[1]['data']
    assert_error(invoke(world, 'scripts.get', {'script_id': 'collect'}), 404, 'RESOURCE_NOT_FOUND')


# LLM: 修改 base64 里的偏移而不改变签名也必须失败，另一主体不能复用游标。
# 函数用途: 验证分页游标的篡改、筛选与版本绑定。
def test_cursor_binds_owner_subject_filter_and_snapshot(world):
    create(world, 'first')
    create(world, 'second')
    status, body = invoke(world, 'scripts.list', {'limit': 1})
    assert status == 200, body
    cursor = body['data']['next_cursor']
    assert cursor is not None
    token = json.loads(base64.urlsafe_b64decode(cursor))
    token['offset'] = 2
    changed = base64.urlsafe_b64encode(json.dumps(token).encode()).decode()
    assert_error(invoke(world, 'scripts.list', {'limit': 1, 'cursor': changed}), 503, 'CURSOR_EXPIRED')
    assert_error(invoke(world, 'scripts.list', {'limit': 1, 'cursor': cursor, 'tag': 'other'}), 503, 'CURSOR_EXPIRED')
    status, next_body = invoke(world, 'scripts.list', {'limit': 1, 'cursor': cursor})
    assert status == 200 and next_body['data']['items'][0]['script_id'] == 'second'
    other = api.bind_business_context(world.host, authorize=lambda action, facts: True, subject_id='another-tester')
    assert_error(invoke(SimpleNamespace(context=other, services=world.services), 'scripts.list',
        {'cursor': cursor}), 503, 'CURSOR_EXPIRED')
    create(world, 'third')
    assert_error(invoke(world, 'scripts.list', {'cursor': cursor}), 503, 'CURSOR_EXPIRED')


# LLM: 真正调用原生 runner；持久 run 与返回数据必须一致，重放不会增加原生 run_count。
# 函数用途: 验证 #73-6 的同步执行、#73-7 结果读取和跨实例运行幂等。
def test_run_low_risk_native_runner_and_persisted_result(world):
    create(world)
    status, body = invoke(world, 'scripts.run', {'script_id': 'collect'}, key='low-run-key-00000001')
    assert status == 200, body
    run = body['data']['run']
    assert run['state'] == 'completed' and run['ok'] is True, run
    assert run['output'] == {'value': 7} and run['task_id'] is None
    assert run['duration_ms'] >= 0 and run['started_at'] is not None and run['finished_at'] is not None
    home = world.host.home_paths.owner_home_dir
    stored = V13Store(home).get('script_runs', run['execution_id'])
    assert stored['state'] == 'completed' and stored['output'] == run['output']
    assert stored['inputs_sha256'] == scripts.digest({}) and 'inputs' not in stored
    assert ScriptStore(home).get_script('collect')['runtime_summary']['run_count'] == 1
    world.services = api.BusinessServices(world.host)
    replay = invoke(world, 'scripts.run', {'script_id': 'collect'}, key='low-run-key-00000001')
    assert replay[0] == 200 and replay[1]['data']['run'] == run
    assert ScriptStore(home).get_script('collect')['runtime_summary']['run_count'] == 1
    status, result = invoke(world, 'scripts.runs.get', {'execution_id': run['execution_id']})
    assert status == 200 and result['data']['run'] == run
    assert_error(invoke(world, 'scripts.runs.get', {'execution_id': 'absent'}), 404, 'RESOURCE_NOT_FOUND')


# LLM: 输入失败不写运行记录，输出失败不能变成 HTTP 层异常或假成功。
# 函数用途: 验证输入 schema、默认值、非零退出与结果脱敏边界。
def test_run_validates_inputs_redacts_and_bounds_output(world):
    schema = {'type': 'object', 'properties': {'number': {'type': 'integer', 'minimum': 1}},
        'required': ['number'], 'additionalProperties': False}
    create(world, patch={'input_schema': schema,
        'code': 'import json,sys\nv=json.load(sys.stdin)\nprint(json.dumps({"value":v["number"],"api_key":"synthetic-secret"}))'})
    home = world.host.home_paths.owner_home_dir
    assert_error(invoke(world, 'scripts.run', {'script_id': 'collect', 'inputs': {'number': 'bad'}},
        key='invalid-input-key-0001'), 400, 'INVALID_SCHEMA')
    assert V13Store(home).list('script_runs') == []
    status, body = invoke(world, 'scripts.run', {'script_id': 'collect', 'inputs': {'number': 4}},
        key='valid-input-key-00001')
    assert status == 200 and body['data']['run']['output']['value'] == 4, body
    assert 'synthetic-secret' not in canonical_json(body)
    create(world, 'large', patch={'code': 'import json\nprint(json.dumps({"payload":"x"*70000,"api_key":"synthetic-secret"}))'})
    status, body = invoke(world, 'scripts.run', {'script_id': 'large'}, key='large-output-key-0001')
    assert status == 200 and body['data']['run']['ok'] is True, body
    output = body['data']['run']['output']
    assert output['truncated'] is True and len(canonical_json(output).encode()) <= 65536
    assert 'synthetic-secret' not in canonical_json(body)
    create(world, 'nonzero', patch={'code': 'import sys\nsys.exit(9)'})
    status, body = invoke(world, 'scripts.run', {'script_id': 'nonzero'}, key='nonzero-run-key-0001')
    assert status == 200, body
    failed = body['data']['run']
    assert failed['state'] == 'unknown' and failed['ok'] is None and failed['error'] == 'SCRIPT_EXIT_NONZERO'


# LLM: 两个服务实例并发使用同一回执；这是真实文件锁而非替换事务实现。
# 函数用途: 验证相同创建键只有一次原生写与记录 CAS 的单胜者。
def test_concurrent_idempotency_and_record_cas(world):
    contexts = [SimpleNamespace(host=world.host, context=world.context, services=api.BusinessServices(world.host))
                for index in range(2)]
    params = {'script_id': 'once', 'name': '只创建一次', 'code': 'print("{}")'}

    # LLM: 并行调用原 dispatcher，原生文件 revision 是副作用次数证据。
    # 函数用途: 对同一创建键发起并发请求。
    def concurrent_create(index):
        return invoke(contexts[index], 'scripts.create', params, key='concurrent-create-key-0001')

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(concurrent_create, range(2)))
    assert [status for status, body in results] == [200, 200], results
    assert results[0][1]['data'] == results[1][1]['data']
    revision = results[0][1]['data']['script']['revision']
    assert ScriptStore(world.host.home_paths.owner_home_dir).get_script('once')['revision'] == revision

    # LLM: 同一旧记录版本的不同请求，必须只有一个提交，另一个保留 REVISION_CONFLICT。
    # 函数用途: 发起并发 CAS 更新。
    def concurrent_update(index):
        return invoke(contexts[index], 'scripts.update', {'script_id': 'once', 'name': '修订' + str(index)},
            key='concurrent-update-key-' + str(index), revision=revision)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(concurrent_update, range(2)))
    assert sorted(status for status, body in results) == [200, 409], results
    assert next(body for status, body in results if status == 409)['error']['code'] == 'REVISION_CONFLICT'


# LLM: 中断后只读出 unknown，不通过重跑覆盖已发生但未确认的副作用。
# 函数用途: 验证持久 running 状态的恢复与幂等参数冲突。
def test_interrupted_run_is_unknown_and_never_reexecuted(world):
    create(world)
    key = 'interrupted-run-key-0001'
    execution_id = 'script-run-' + scripts.digest([world.context.owner_id, world.context.subject_id,
        'scripts.run', key])[:48]
    now = '2026-11-02T00:00:00+00:00'
    row = {'execution_id': execution_id, 'owner_id': world.context.owner_id, 'space_id': None,
        'script_id': 'collect', 'subject_id': world.context.subject_id, 'requires_approval': False,
        'task_id': None, 'context': None, 'inputs_sha256': scripts.digest({}), 'state': 'running',
        'ok': None, 'output': None, 'error': None, 'duration_ms': None, 'started_at': now,
        'finished_at': None, 'revision': 0, 'created_at': now, 'updated_at': now}
    store = V13Store(world.host.home_paths.owner_home_dir, owner_id=world.context.owner_id,
        subject_id=world.context.subject_id, action='scripts.run')

    # LLM: 模拟持久提交后的进程退出，只种入宿主原记录与回执，不伪造 runner 成功。
    # 函数用途: 写入一个真实中断点。
    def seed(records):
        records[execution_id] = row
        return {'execution_id': execution_id}

    store.transaction('script_runs', seed, idempotency_key=key,
        params_digest=scripts.digest(['scripts.run', {'script_id': 'collect'}, None]))
    status, body = invoke(world, 'scripts.run', {'script_id': 'collect'}, key=key)
    assert status == 200 and body['data']['run']['state'] == 'unknown', body
    assert body['data']['run']['ok'] is None and body['data']['run']['coverage'] == 'unknown'
    assert ScriptStore(world.host.home_paths.owner_home_dir).get_script('collect')['runtime_summary']['run_count'] == 0
    assert_error(invoke(world, 'scripts.run', {'script_id': 'collect', 'inputs': {'extra': True}}, key=key),
        409, 'IDEMPOTENCY_CONFLICT')


# LLM: Gateway/owner/dispatcher/runner 全链路真实运行，不用手写 JSON 代替服务端响应。
# 函数用途: 验证 #73-1/6/7/10 的真实 HTTP 往返与跨 owner 隔离。
def test_http_crud_run_and_owner_isolation(v13_native):
    space_id = v13_space(v13_native)
    params = {'space_id': space_id, 'script_id': 'http-collect', 'name': 'HTTP采集', 'code': 'print(\'{"wire":true}\')'}
    status, body = v13_call(v13_native, 'scripts.create', params, user='business-alice',
        idempotency_key='http-create-script-key-0001')
    assert status == 200 and body['error'] is None, body
    status, body = v13_call(v13_native, 'scripts.list', {'space_id': space_id}, user='business-alice')
    assert status == 200 and body['data']['items'][0]['script_id'] == 'http-collect', body
    assert_error(v13_call(v13_native, 'scripts.get', {'script_id': 'http-collect'}, user='business-bob'),
        404, 'RESOURCE_NOT_FOUND')
    status, body = v13_call(v13_native, 'scripts.run', {'script_id': 'http-collect', 'space_id': space_id},
        user='business-alice', idempotency_key='http-run-script-key-0001')
    assert status == 200 and body['state'] == 'completed' and body['error'] is None, body
    run = body['data']['run']
    assert run['ok'] is True and run['output'] == {'wire': True}, run
    status, result = v13_call(v13_native, 'scripts.runs.get', {'execution_id': run['execution_id']}, user='business-alice')
    assert status == 200 and result['data']['run'] == run, result
    assert_error(v13_call(v13_native, 'scripts.runs.get', {'execution_id': run['execution_id']}, user='business-bob'),
        404, 'RESOURCE_NOT_FOUND')


# LLM: 发现走正式扩展目录，冻结目录不被脚本动作污染，未知动作仍使用原错误码。
# 函数用途: 验证 #73-9 路由对应动作发现与 #73-10 信封。
def test_route_map_actions_are_discoverable_in_extensions(v13_native):
    status, body = v13_call(v13_native, 'system.capabilities.extensions', {}, user='business-alice')
    assert status == 200 and body['error'] is None, body
    expected = {'scripts.list', 'scripts.get', 'scripts.create', 'scripts.update',
        'scripts.delete', 'scripts.run', 'scripts.runs.get'}
    assert expected <= set(body['data']['supported_actions'])
    assert_error(v13_call(v13_native, 'scripts.unregistered', {}, user='business-alice'), 400, 'UNSUPPORTED_ACTION')
    assert_error(v13_call(v13_native, 'scripts.run', {'script_id': 'absent', 'query': 'free-form'},
        user='business-alice', idempotency_key='http-invalid-schema-key-01'), 400, 'INVALID_SCHEMA')


# LLM: 假模型只用现有 echo，不替换派工端口；受理信封绝不能代表脚本已经执行。
# 函数用途: 验证高风险真实 HTTP 派工、耐久任务关联与稳定重放。
@pytest.mark.parametrize('declared_approval', [True, False])
def test_high_risk_run_assigns_task_and_preserves_native_gate(v13_native, declared_approval, monkeypatch):
    from agent_py_agent.agent.conversation import session_task_dispatch
    from agent_py_agent.agent.gateway_parts import business_api_v13 as foundation
    original = foundation.v13_call_frozen
    report = session_task_dispatch.runtime_error_report

    # LLM: 仅将当前合成测试的原始异常栈加入测试输出，不改变原生派工失败分类。
    # 函数用途: 保留派工器屏蔽公开异常前的诊断。
    def traced_report(error, *, context=''):
        import traceback
        traceback.print_exception(error)
        return report(error, context=context)

    monkeypatch.setattr(session_task_dispatch, 'runtime_error_report', traced_report)

    # LLM: 诊断包装不改变执行或结果，仅保留高风险派工失败的准确原生入口。
    # 函数用途: 对真实冻结端口打印失败栈，不吞掉异常。
    def traced(call, action, params, *, idempotency_key=None, expected_revision=None):
        import traceback
        try:
            return original(call, action, params, idempotency_key=idempotency_key, expected_revision=expected_revision)
        except Exception:
            print('scripts frozen failure:', action)
            traceback.print_exc()
            raise

    monkeypatch.setattr(foundation, 'v13_call_frozen', traced)
    space_id = v13_space(v13_native)
    params = {'space_id': space_id, 'script_id': 'approval-collect', 'name': '待确认采集',
        'code': 'print(\'{"must_not_run_directly":true}\')', 'risk_level': 'high',
        'requires_approval': declared_approval}
    status, body = v13_call(v13_native, 'scripts.create', params, user='business-alice',
        idempotency_key='high-create-script-key-0001')
    assert status == 200, body
    run_params = {'space_id': space_id, 'script_id': 'approval-collect', 'inputs': {}}
    if declared_approval is False:
        run_params.pop('space_id')
    status, body = v13_call(v13_native, 'scripts.run', run_params, user='business-alice',
        idempotency_key='high-run-script-key-0001')
    assert status == 202 and body['state'] == 'accepted' and body['error'] is None, body
    accepted = body['data']
    assert accepted['requires_approval'] is True and accepted['task_id'] and accepted['operation_id']
    status, replay = v13_call(v13_native, 'scripts.run', run_params, user='business-alice',
        idempotency_key='high-run-script-key-0001')
    assert status == 202 and replay['data'] == accepted and replay['error'] is None, replay
    status, observed = v13_call(v13_native, 'scripts.runs.get', {'execution_id': accepted['execution_id']},
        user='business-alice')
    assert status == 200 and observed['data']['run']['task_id'] == accepted['task_id'], observed
    assert observed['data']['run']['ok'] is not True and observed['data']['run']['output'] is None
    assert_error(v13_call(v13_native, 'scripts.runs.get', {'execution_id': accepted['execution_id']},
        user='business-bob'), 404, 'RESOURCE_NOT_FOUND')
    status, approvals = v13_call(v13_native, 'approvals.list', {'filter': {'task_id': accepted['task_id']}},
        user='business-alice')
    assert status == 200 and 'items' in approvals['data'], approvals
    # 此处没有伪造 pending；原生后台默认非交互，echo 不产生工具调用。
    assert_error(v13_call(v13_native, 'approvals.decide', {'approval_id': 'not-a-ticket', 'decision': 'approved',
        'response_ticket': 'synthetic-invalid-ticket'},
        user='business-alice', idempotency_key='forbidden-approval-key-0001', expected_revision=0),
        403, 'CAPABILITY_DENIED')


# LLM: 合成无效 ticket 只是拒绝反例；跨 owner 读口为 partial 时不能把未知缺口判成不存在。
# 函数用途: 独立验证冻结审批读取与模型业务请求不能决定人工票据。
def test_http_approval_decision_is_human_only(v13_native):
    status, body = v13_call(v13_native, 'approvals.list', {}, user='business-alice')
    assert status == 200 and body['error'] is None and isinstance(body['data']['items'], list), body
    assert_error(v13_call(v13_native, 'approvals.get', {'approval_id': 'synthetic-absent-ticket'},
        user='business-alice'), 503, 'HOST_NOT_READY')
    assert_error(v13_call(v13_native, 'approvals.decide', {'approval_id': 'synthetic-absent-ticket',
        'decision': 'approved', 'response_ticket': 'synthetic-invalid-ticket'}, user='business-alice',
        idempotency_key='forbidden-approval-key-0002', expected_revision=0), 403, 'CAPABILITY_DENIED')


# LLM: 同一真实 Agent/Store 的审批读口完整，缺失详情才允许返回 404；不模拟 pending 或签票。
# 函数用途: 保留完整原生来源下的不存在断言，与跨 owner 未知分支分别验证。
def test_approval_detail_missing_requires_complete_native_source(v13_native):
    from agent_py_agent.agent.gateway_parts.business_api_v13_readers import native_approval_readers

    server, agent = v13_native
    context, services = api.local_business_context(agent)
    readers = native_approval_readers(agent, server.paths, agent.home_paths.owner_id,
        agent.conversation_store)
    page = readers['approvals']({})
    assert page.coverage == 'complete' and page.records == ()
    services.projection.readers.update(readers)
    request = {'action': 'approvals.get', 'request_id': 'complete-native-source-missing-approval',
        'params': {'approval_id': 'synthetic-absent-ticket'}}
    assert_error(api.execute_business(request, context, services), 404, 'OBSERVATION_NOT_FOUND')


# LLM: 真实 ScriptsTool 的 runtime_policy 交给原 ActionPolicy，不能以 metadata 绕过 ask 门。
# 函数用途: 验证高风险派工所依赖的原生工具审批策略不被 W7 改写。
def test_native_scripts_tool_requires_approval_in_ask_mode(tmp_path):
    from agent_py_agent.agent.tooling.action_policy import ActionPolicy, ActionPolicyRequest
    from agent_py_agent.agent.tooling.scripts_tool import ScriptsTool
    from agent_py_agent.tests._tool_runtime_harness import (
        canonical_test_call,
        runtime_snapshot_for_tools,
    )
    native = ScriptsTool(tmp_path)
    snapshot = runtime_snapshot_for_tools({'scripts': native})
    call = canonical_test_call(snapshot, 'scripts', {'action': 'run', 'script_id': 'registered', 'inputs': {}})
    decision = ActionPolicy().decide(ActionPolicyRequest(call, snapshot, tmp_path, approval_mode='ask'))
    assert decision.status == 'ask'
    assert not list(tmp_path.glob('**/main.py'))


# LLM: 这些是显式合成的观察端口反例，不声称覆盖原生票据发布或人工批准链。
# 函数用途: 验证 get_run 不把回音任务或缺失观察转换为脚本成功。
@pytest.mark.parametrize('observation', ['pending', 'echo_done', 'failed', 'unavailable'])
def test_get_run_handles_pending_failure_and_unavailable_evidence(world, monkeypatch, observation):
    from agent_py_agent.agent.gateway_parts import business_api_v13 as foundation
    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    now = '2026-11-02T00:00:00+00:00'
    row = {'execution_id': 'synthetic-observation', 'owner_id': world.context.owner_id, 'space_id': None,
        'script_id': 'collect', 'subject_id': world.context.subject_id, 'requires_approval': True,
        'task_id': 'synthetic-task', 'context': None, 'inputs_sha256': scripts.digest({}), 'state': 'accepted',
        'ok': None, 'output': None, 'error': None, 'duration_ms': None, 'started_at': None,
        'finished_at': None, 'revision': 0, 'created_at': now, 'updated_at': now}
    v13_seed(world.host.home_paths.owner_home_dir, 'script_runs', {'synthetic-observation': row})

    # LLM: 仅替换冻结观察响应，不替换写权限或脚本执行器；响应身份在测试名称中明确为合成。
    # 函数用途: 提供单一失败条件的结构化观察样本。
    def observe(call, action, params, *, idempotency_key=None, expected_revision=None):
        assert params.get('task_id', params.get('filter', {}).get('task_id')) == 'synthetic-task'
        if observation == 'unavailable':
            raise HostAPIError('HOST_NOT_READY', 503)
        if action == 'tasks.get':
            return {'task': {'status': 'failed' if observation == 'failed' else 'done' if observation == 'echo_done' else 'running'}}
        if action == 'approvals.list':
            items = [{'approval_id': 'synthetic-pending', 'state': 'pending'}] if observation == 'pending' else []
            return {'items': items, 'next_cursor': None, 'coverage': 'complete'}
        assert action == 'tool_calls.list'
        return {'items': [], 'next_cursor': None, 'coverage': 'complete'}

    monkeypatch.setattr(foundation, 'v13_call_frozen', observe)
    status, body = invoke(world, 'scripts.runs.get', {'execution_id': 'synthetic-observation'})
    assert status == 200, body
    run = body['data']['run']
    if observation == 'pending':
        assert run['state'] == 'awaiting_approval' and run['approval_ids'] == ['synthetic-pending']
    elif observation == 'failed':
        assert run['state'] == 'failed' and run['ok'] is False
    else:
        assert run['state'] == 'unknown' and run['ok'] is None and run['coverage'] == 'unknown'


# LLM: 明确的合成端口用于输入关联反例；不声称这是真实人工批准或真实原生操作落账。
# 函数用途: 验证只有同任务、同脚本、同输入的原生结果协议才能完成运行。
@pytest.mark.parametrize('case', ['list_only', 'wrong_script', 'completed', 'failed', 'missing_operation', 'unknown_operation'])
def test_get_run_requires_bound_script_run_result(world, monkeypatch, case):
    from agent_py_agent.agent.gateway_parts import business_api_v13 as foundation
    from agent_py_agent.agent.runtime_db.managed_operation_store import ManagedOperationStore
    from agent_py_agent.agent.tooling.runtime_contracts import tool_arguments_hash
    now = '2026-11-02T00:00:00+00:00'
    row = {'execution_id': 'synthetic-bound-run', 'owner_id': world.context.owner_id, 'space_id': None,
        'script_id': 'collect', 'subject_id': world.context.subject_id, 'requires_approval': True,
        'task_id': 'synthetic-task', 'context': None, 'inputs_sha256': scripts.digest({}), 'state': 'accepted',
        'ok': None, 'output': None, 'error': None, 'duration_ms': None, 'started_at': None,
        'finished_at': None, 'revision': 0, 'created_at': now, 'updated_at': now}
    row['tool_args_hashes'] = [tool_arguments_hash({'action': 'run', 'script_id': 'collect', 'inputs': {}})]
    v13_seed(world.host.home_paths.owner_home_dir, 'script_runs', {'synthetic-bound-run': row})
    tool = {'operation_id': 'synthetic-operation', 'run_id': 'synthetic-runtime-run',
        'attempt_id': 'synthetic-attempt', 'task_id': 'synthetic-task', 'tool_name': 'scripts',
        'status': 'SUCCEEDED', 'executed': True, 'effect_outcome': 'committed', 'duration_ms': 5,
        'error': None, 'result_ref': 'synthetic-reference'}

    # LLM: 假端口严格断言查询身份，不按正文匹配或将 scripts.list 当成 run。
    # 函数用途: 提供结构化冻结观察，保持明确的 action 与参数反例。
    def observe(call, action, params, *, idempotency_key=None, expected_revision=None):
        if action == 'tasks.get':
            assert params == {'task_id': 'synthetic-task'}
            return {'task': {'status': 'done'}}
        if action == 'approvals.list':
            return {'items': [], 'next_cursor': None, 'coverage': 'complete'}
        if action == 'tool_calls.list':
            return {'items': [tool], 'next_cursor': None, 'coverage': 'complete'}
        assert action == 'tool_calls.get' and params == {'operation_id': 'synthetic-operation'}
        return {'tool_call': tool, 'redacted_input': {}}

    # LLM: 此假读口只模拟原协议的内容，参数断言证明实现请求准确 owner/run/attempt/tool。
    # 函数用途: 返回合成 ToolOperationRecord 的真实协议形状或缺失反例。
    def operation(self, *, owner_id, run_id, operation_id, task_id='', attempt_id='', tool_name='',
                  args_hash='', resource_scopes=None):
        assert owner_id == world.context.owner_id and run_id == 'synthetic-runtime-run'
        assert operation_id == 'synthetic-operation' and attempt_id == 'synthetic-attempt' and tool_name == 'scripts'
        if case == 'missing_operation':
            return None
        raw = {'script_id': 'collect', 'ok': case != 'failed', 'output_data': {'result': 8},
            'duration_ms': 5, 'started_at': 1, 'ended_at': 2, 'error_code': 'SCRIPT_EXIT_NONZERO' if case == 'failed' else '',
            'effect_outcome': 'failed' if case == 'failed' else 'confirmed'}
        status = 'unknown' if case == 'unknown_operation' else 'failed' if case == 'failed' else 'succeeded'
        arguments = {'action': 'list' if case == 'list_only' else 'run',
            'script_id': 'another' if case == 'wrong_script' else 'collect', 'inputs': {}}
        return SimpleNamespace(status=status, args_hash=tool_arguments_hash(arguments), result={'schema_version': 'tool_execution_result.v1',
            'handler_executed': True, 'output': canonical_json(raw)})

    monkeypatch.setattr(foundation, 'v13_call_frozen', observe)
    monkeypatch.setattr(ManagedOperationStore, 'get_tool_operation', operation)
    status, body = invoke(world, 'scripts.runs.get', {'execution_id': 'synthetic-bound-run'})
    assert status == 200, body
    run = body['data']['run']
    if case in ('completed', 'failed'):
        assert run['state'] == case and run['ok'] is (case == 'completed')
        assert run['output'] == {'result': 8} and run['duration_ms'] == 5
    else:
        assert run['state'] == 'unknown' and run['ok'] is None and run['coverage'] == 'unknown'


# LLM: 撤权只拒绝真实原记录事实，普通动作准入仍允许，防止用更宽准入检查掩盖回执泄露。
# 函数用途: 证明创建和更新回执重放都必须实时复核当前脚本记录。
@pytest.mark.parametrize('action', ['scripts.create', 'scripts.update'])
def test_p1_config_replay_rechecks_current_record_authorization(world, action):
    blocked = [False]

    # LLM: 授权回调由真实 binder 绑定，只在已撤销且传入记录版本时拒绝。
    # 函数用途: 区分动作事实和当前原生脚本记录授权。
    def authorize(requested, facts):
        return not (blocked[0] and 'revision' in facts)

    world.context = api.bind_business_context(world.host, authorize=authorize, subject_id='script-tester')
    params = {'script_id': 'p1-auth', 'name': '原记录授权', 'code': 'print("{}")'}
    revision = None
    if action == 'scripts.update':
        revision = create(world, 'p1-auth')['revision']
        params = {'script_id': 'p1-auth', 'description': '必须重新鉴权'}
    first = invoke(world, action, params, key='p1-authorization-key-0001', revision=revision)
    assert first[0] == 200, first
    blocked[0] = True
    world.services = api.BusinessServices(world.host)
    assert_error(invoke(world, action, params, key='p1-authorization-key-0001', revision=revision),
        403, 'CAPABILITY_DENIED')


# LLM: 原详情回执不能在脚本已删除后泄露旧正文，删除动作自身的无详情回执仍可合法重放。
# 函数用途: 证明已删除脚本的创建/更新成功回执不能绕过当前存在性。
@pytest.mark.parametrize('action', ['scripts.create', 'scripts.update'])
def test_p1_detail_receipt_does_not_leak_deleted_script(world, action):
    params = {'script_id': 'p1-removed', 'name': '删除前详情', 'code': 'print("{}")'}
    revision = None
    if action == 'scripts.update':
        revision = create(world, 'p1-removed')['revision']
        params = {'script_id': 'p1-removed', 'description': '删除后不得回显'}
    first = invoke(world, action, params, key='p1-deleted-detail-key-0001', revision=revision)
    assert first[0] == 200, first
    current = first[1]['data']['script']
    deleted = invoke(world, 'scripts.delete', {'script_id': 'p1-removed'},
        key='p1-delete-after-write-key', revision=current['revision'])
    assert deleted[0] == 200, deleted
    replay = invoke(world, 'scripts.delete', {'script_id': 'p1-removed'},
        key='p1-delete-after-write-key', revision=current['revision'])
    assert replay[0] == 200 and replay[1]['data'] == {'script_id': 'p1-removed', 'deleted': True}
    assert_error(invoke(world, action, params, key='p1-deleted-detail-key-0001', revision=revision),
        404, 'RESOURCE_NOT_FOUND')


# LLM: 故障只发生在 V13 真实日志提交点之前，原生写入不回滚、不替换为假结果。
# 函数用途: 证明原生落盘后丢失最终回执的创建/更新/启停/删除均可按耐久身份恢复。
@pytest.mark.parametrize('operation', ['create', 'update', 'enable', 'mixed', 'delete'])
def test_p1_native_commit_before_receipt_is_recoverable(world, monkeypatch, operation):
    import hashlib

    from agent_py_agent.agent.business_spaces import v13_store
    home = world.host.home_paths.owner_home_dir
    action = 'scripts.create' if operation == 'create' else 'scripts.delete' if operation == 'delete' else 'scripts.update'
    params = {'script_id': 'p1-recover', 'name': '崩溃恢复', 'code': 'print("{}")'}
    revision = None
    if operation != 'create':
        initial = create(world, 'p1-recover')
        revision = initial['revision']
        params = {'script_id': 'p1-recover'}
        if operation in ('update', 'mixed'):
            params['description'] = '原生已提交的新描述'
        if operation in ('enable', 'mixed'):
            params['enabled'] = False
    key = 'p1-native-recovery-key-0001'
    receipt_key = hashlib.sha256((canonical_json([world.context.subject_id, action, key]) + '\n').encode()).hexdigest()
    original = v13_store.write_bytes_atomic_beneath
    fired = [False]

    # LLM: 仅拦截目标请求最终回执的首次 journal 发布，准备事务等其它真实写入保持原样。
    # 函数用途: 注入原生成功与 V13 最终提交之间的一次 I/O 失败。
    def fail_final_commit(root, parts, raw):
        if parts == ('data', 'business_v13', '.transaction.json') and not fired[0]:
            pending = json.loads(raw)
            if receipt_key in pending.get('states', {}).get('script_runs', {}).get('receipts', {}):
                fired[0] = True
                raise OSError('synthetic final receipt interruption')
        return original(root, parts, raw)

    monkeypatch.setattr(v13_store, 'write_bytes_atomic_beneath', fail_final_commit)
    assert_error(invoke(world, action, params, key=key, revision=revision), 503, 'OUTCOME_UNKNOWN')
    assert fired[0]
    native_after = ScriptStore(home).get_script('p1-recover')
    if operation == 'delete':
        assert native_after is None
    else:
        assert native_after is not None
        if operation in ('enable', 'mixed'):
            assert native_after['enabled'] is False
    world.services = api.BusinessServices(world.host)
    status, replay = invoke(world, action, params, key=key, revision=revision)
    assert status == 200, replay
    if operation == 'delete':
        assert replay['data'] == {'script_id': 'p1-recover', 'deleted': True}
        assert ScriptStore(home).get_script('p1-recover') is None
    else:
        assert replay['data']['script']['revision'] == native_after['revision']
        assert ScriptStore(home).get_script('p1-recover')['revision'] == native_after['revision']


# LLM: 原生两次写之间失败不是整体成功；恢复必须跳过已有身份标记的 save，仅执行尚未提交的启停。
# 函数用途: 证明元数据更新加启停能从准确中间阶段继续而不重复递增版本。
def test_p1_mixed_update_recovers_between_native_steps(world, monkeypatch):
    initial = create(world, 'p1-middle')
    home = world.host.home_paths.owner_home_dir
    original = ScriptStore.set_enabled
    fired = [False]

    # LLM: 只让首次启停在执行前抛错，前一原生 save 仍真实落盘。
    # 函数用途: 注入两次原生提交之间的真实中断。
    def interrupt_enable(self, script_id, enabled):
        if not fired[0]:
            fired[0] = True
            raise OSError('synthetic step boundary interruption')
        return original(self, script_id, enabled)

    monkeypatch.setattr(ScriptStore, 'set_enabled', interrupt_enable)
    params = {'script_id': 'p1-middle', 'description': '中间已保存', 'enabled': False}
    assert_error(invoke(world, 'scripts.update', params, key='p1-middle-native-key-0001',
        revision=initial['revision']), 503, 'OUTCOME_UNKNOWN')
    partial = ScriptStore(home).get_script('p1-middle')
    assert partial['revision'] == initial['revision'] + 1 and partial['enabled'] is True
    world.services = api.BusinessServices(world.host)
    status, body = invoke(world, 'scripts.update', params, key='p1-middle-native-key-0001', revision=initial['revision'])
    assert status == 200, body
    assert body['data']['script']['description'] == '中间已保存' and body['data']['script']['enabled'] is False
    assert body['data']['script']['revision'] == initial['revision'] + 2


# LLM: 准备回执不等于原生提交证明；另一操作者造成同内容/同缺失状态也不能被本请求认领。
# 函数用途: 核验缺少准确操作身份时拒绝把其它原生操作当作本请求成功。
@pytest.mark.parametrize('operation', ['create', 'update', 'delete'])
def test_p1_prepared_request_cannot_claim_another_native_operation(world, monkeypatch, operation):
    home = world.host.home_paths.owner_home_dir
    initial = None if operation == 'create' else create(world, 'p1-other')
    revision = None if initial is None else initial['revision']
    action = 'scripts.' + operation
    params = {'script_id': 'p1-other'}
    if operation == 'create':
        params.update(name='相同内容不是身份', code='print("{}")')
    elif operation == 'update':
        params['description'] = '相同内容不是身份'
    method_name = 'delete_script' if operation == 'delete' else 'save_script'
    original = getattr(ScriptStore, method_name)

    # LLM: 在原生方法执行前中断，准备已真实落盘但本请求未产生任何原生提交。
    # 函数用途: 注入原生开始边界故障，不能生成伪成功证明。
    def interrupt_save(self, descriptor, code, *, entrypoint='main.py', interpreter='python3',
                       timeout_seconds=120, target_scope='owner', expected_revision=None):
        raise OSError('synthetic interruption before native save')

    # LLM: 删除开始前中断，随后由其它原生操作删除，不属于本请求。
    # 函数用途: 注入删除开始边界故障。
    def interrupt_delete(self, script_id):
        raise OSError('synthetic interruption before native delete')

    monkeypatch.setattr(ScriptStore, method_name, interrupt_delete if operation == 'delete' else interrupt_save)
    assert_error(invoke(world, action, params, key='p1-other-native-key-0001', revision=revision),
        503, 'OUTCOME_UNKNOWN')
    monkeypatch.setattr(ScriptStore, method_name, original)
    native = ScriptStore(home)
    if operation == 'delete':
        assert native.delete_script('p1-other') is True
    elif operation == 'create':
        native.save_script(AssetDescriptor(id='p1-other', kind='script', name=params['name'], owner_id=world.context.owner_id),
            params['code'], expected_revision=0)
    else:
        old = native.get_script('p1-other')
        from dataclasses import fields, replace
        names = {field.name for field in fields(AssetDescriptor)}
        descriptor = AssetDescriptor.from_dict({name: value for name, value in old.items() if name in names})
        native.save_script(replace(descriptor, summary=params['description']), old['code'],
            expected_revision=revision)
    world.services = api.BusinessServices(world.host)
    expected = (404, 'RESOURCE_NOT_FOUND') if operation == 'delete' else (
        (409, 'STATE_CONFLICT') if operation == 'create' else (409, 'REVISION_CONFLICT'))
    assert_error(invoke(world, action, params, key='p1-other-native-key-0001', revision=revision), expected[0], expected[1])


# LLM: 旧删除操作的重试只能恢复原收据，不能误删同 ID 后来重建的新脚本。
# 函数用途: 核验原子移位证明将删除恢复绑定到原代资产而非当前同名资产。
def test_p1_delete_recovery_preserves_recreated_script(world, monkeypatch):
    from agent_py_agent.agent.business_spaces import v13_store
    initial = create(world, 'p1-generation')
    home = world.host.home_paths.owner_home_dir
    original = v13_store.write_bytes_atomic_beneath
    failed = [False]

    # LLM: 仅最终删除收据被拦截，准备和原生移位仍真实提交。
    # 函数用途: 在删除原代资产后、最终回执提交前中断。
    def fail_delete_receipt(root, parts, raw):
        if parts == ('data', 'business_v13', '.transaction.json') and not failed[0]:
            pending = json.loads(raw)
            receipts = pending.get('states', {}).get('script_runs', {}).get('receipts', {})
            if any(row['result'] == {'script_id': 'p1-generation', 'deleted': True} for row in receipts.values()):
                failed[0] = True
                raise OSError('synthetic delete receipt interruption')
        return original(root, parts, raw)

    monkeypatch.setattr(v13_store, 'write_bytes_atomic_beneath', fail_delete_receipt)
    assert_error(invoke(world, 'scripts.delete', {'script_id': 'p1-generation'},
        key='p1-generation-delete-0001', revision=initial['revision']), 503, 'OUTCOME_UNKNOWN')
    native = ScriptStore(home)
    replacement = native.save_script(AssetDescriptor(id='p1-generation', kind='script', name='后来重建',
        owner_id=world.context.owner_id), 'print("new generation")', expected_revision=0)
    status, replay = invoke(world, 'scripts.delete', {'script_id': 'p1-generation'},
        key='p1-generation-delete-0001', revision=initial['revision'])
    assert status == 200 and replay['data']['deleted'] is True
    current = native.get_script('p1-generation')
    assert current['revision'] == replacement['revision'] and current['code'] == 'print("new generation")'


# LLM: 这是冻结 receipt 形状适配测试，不冒充真实任务执行或人工批准；真实 HTTP 测试独立保留。
# 函数用途: 校验同步 assignment_ref 映射、真实202操作ID以及不受理/额外伪字段的拒绝。
@pytest.mark.parametrize('kind', ['native', 'accepted', 'pending', 'extra_field'])
def test_assignment_receipt_uses_native_identity(kind):
    from agent_py_agent.agent.gateway_parts.business_api_v13 import DomainResult
    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    raw = {'assignment_ref': 'original-assignment', 'task_id': 'original-task', 'receipt_state': 'accepted'}
    if kind == 'native':
        assert scripts._assignment_receipt(raw) == {'task_id': 'original-task', 'operation_id': 'original-assignment'}
    elif kind == 'accepted':
        result = DomainResult(202, {'assignment_ref': 'original-assignment',
            'task_id': 'original-task', 'operation_id': 'original-operation'})
        assert scripts._assignment_receipt(result) == {'task_id': 'original-task', 'operation_id': 'original-operation'}
    else:
        if kind == 'pending':
            raw['receipt_state'] = 'pending'
        else:
            raw['operation_id'] = 'non-native-operation'
        with pytest.raises(HostAPIError) as error:
            scripts._assignment_receipt(raw)
        assert error.value.code == 'HOST_NOT_READY'


# LLM: 原生目录操作使用 POSIX 描述符；枚举结束后再 fstat，排除采样器自身的临时目录 FD。
# 函数用途: 取得当前进程真实打开的描述符身份，不引入额外测试依赖。
def _script_fd_snapshot():
    directory = next(path for path in (Path('/proc/self/fd'), Path('/dev/fd')) if path.is_dir())
    with os.scandir(directory) as entries:
        numbers = [int(entry.name) for entry in entries if entry.name.isdecimal()]
    result = {}
    for number in numbers:
        try:
            status = os.fstat(number)
        except OSError as error:
            if error.errno != errno.EBADF:
                raise
        else:
            result[number] = (status.st_dev, status.st_ino, status.st_mode)
    return result


def _script_fd_is_open(number, identity):
    try:
        status = os.fstat(number)
    except OSError as error:
        if error.errno != errno.EBADF:
            raise
        return False
    return (status.st_dev, status.st_ino, status.st_mode) == identity


# LLM: 只观察原生获取/删除，不替换存储、服务或信封；单次错误只发生在目的目录获取处。
# 函数用途: 记录删除调用内的真实 FD 差额，失败断言后也收回测试观测到的残留资源。
@pytest.fixture
def delete_resources(monkeypatch):
    observed = SimpleNamespace(handles=[], measurements=[], failure=None, errors=0, home=None)
    original_open = scripts.open_directory_beneath
    original_delete = scripts._NativeCASStore._delete

    def open_directory(root, parts, **kwargs):
        if parts == ('data', 'business_v13', 'script_delete_proofs') and observed.failure is not None:
            error, observed.failure = observed.failure, None
            observed.errors += 1
            raise error
        number = original_open(root, parts, **kwargs)
        status = os.fstat(number)
        observed.home = Path(root)
        observed.handles.append((number, (status.st_dev, status.st_ino, status.st_mode), parts))
        return number

    def delete(store, script_id):
        before = _script_fd_snapshot()
        try:
            return original_delete(store, script_id)
        finally:
            after = _script_fd_snapshot()
            observed.measurements.append(len(after) - len(before))
            if len(after) != len(before):
                import fcntl
                added = {}
                for number, identity in after.items():
                    if before.get(number) != identity:
                        try:
                            path = fcntl.fcntl(number, fcntl.F_GETPATH, bytes(1024)).split(b'\0', 1)[0].decode()
                        except (AttributeError, OSError):
                            path = 'descriptor identity: ' + repr(identity)
                        added[number] = path
                print('Delete FD observation:', {'delta': observed.measurements[-1], 'added': added})

    monkeypatch.setattr(scripts, 'open_directory_beneath', open_directory)
    monkeypatch.setattr(scripts._NativeCASStore, '_delete', delete)
    try:
        yield observed
    finally:
        for number, identity, _parts in observed.handles:
            if _script_fd_is_open(number, identity):
                os.close(number)


def _script_delete_proof(directory, script_id):
    matches = [json.loads(path.read_text()) for path in directory.glob('*.json')]
    matches = [row for row in matches if row.get('schema_version') == 'script-config-proof.v1'
               and row.get('action') == 'scripts.delete' and row.get('script_id') == script_id]
    assert len(matches) == 1, matches
    return matches[0]


# LLM: 真实 HTTP 创建/删除及同键重试均经过原 dispatcher、CAS、原生资产与耐久回执，不手写响应。
# 函数用途: 验证成功和单次目的目录 EIO 都关闭 FD，失败保留准确证明，重试不删除后建同名资产。
@pytest.mark.parametrize('destination_error', [False, True])
def test_script_delete_cleanup_http(v13_native, delete_resources, destination_error):
    script_id, key = 'r4-http-cleanup', 'r4-http-delete-key-0001'
    status, created = v13_call(v13_native, 'scripts.create',
        {'script_id': script_id, 'name': '删除资源检查', 'code': 'print("original")'},
        user='business-alice', idempotency_key='r4-http-create-key-0001')
    assert status == 200 and created['state'] == 'completed' and created['error'] is None, created
    revision = created['data']['script']['revision']
    delete_resources.failure = OSError(errno.EIO, 'single destination acquisition error') if destination_error else None
    result = v13_call(v13_native, 'scripts.delete', {'script_id': script_id},
        user='business-alice', idempotency_key=key, expected_revision=revision)
    if destination_error:
        assert_error(result, 503, 'OUTCOME_UNKNOWN')
    print('HTTP delete fd_delta:', delete_resources.measurements)
    source_handles = [(fd, identity) for fd, identity, parts in delete_resources.handles if parts == ('scripts',)]
    assert source_handles
    assert not any(_script_fd_is_open(fd, identity) for fd, identity in source_handles), 'source descriptor retained'
    assert delete_resources.measurements == [0]
    home = delete_resources.home
    previous_proof = None
    if destination_error:
        assert delete_resources.errors == 1
        assert ScriptStore(home).get_script(script_id)['code'] == 'print("original")'
        previous_proof = _script_delete_proof(home / 'scripts' / script_id, script_id)
        assert previous_proof['phase'] == 'deleted'
        result = v13_call(v13_native, 'scripts.delete', {'script_id': script_id},
            user='business-alice', idempotency_key=key, expected_revision=revision)
        assert delete_resources.measurements == [0, 0]
    assert result[0] == 200 and result[1]['state'] == 'completed' and result[1]['error'] is None, result
    assert result[1]['data'] == {'script_id': script_id, 'deleted': True}
    assert ScriptStore(home).get_script(script_id) is None
    proof_root = home / 'data' / 'business_v13' / 'script_delete_proofs'
    directories = list(proof_root.iterdir())
    assert len(directories) == 1
    proof = _script_delete_proof(directories[0], script_id)
    assert proof['operation_id'] == directories[0].name and proof['phase'] == 'deleted'
    assert proof['fingerprint'] == scripts.digest(['scripts.delete', {'script_id': script_id}, revision])
    assert {path.name for path in directories[0].iterdir()} == {proof['operation_id'] + '.json'}
    if previous_proof is not None:
        assert proof == previous_proof
    status, replacement = v13_call(v13_native, 'scripts.create',
        {'script_id': script_id, 'name': '后建资产', 'code': 'print("replacement")'},
        user='business-alice', idempotency_key='r4-http-recreate-key-0001')
    assert status == 200 and replacement['error'] is None, replacement
    replay = v13_call(v13_native, 'scripts.delete', {'script_id': script_id},
        user='business-alice', idempotency_key=key, expected_revision=revision)
    assert replay[0] == 200 and replay[1]['data'] == result[1]['data'] and replay[1]['error'] is None, replay
    assert ScriptStore(home).get_script(script_id)['code'] == 'print("replacement")'
    assert _script_delete_proof(directories[0], script_id) == proof
    assert all(delta == 0 for delta in delete_resources.measurements)
    assert not any(_script_fd_is_open(fd, identity) for fd, identity, _parts in delete_resources.handles)


# LLM: CancelledError 只模拟同步删除栈的取消展开，不声称覆盖会话 stop；操作准备和证明仍由原 dispatcher 写入。
# 函数用途: 验证 BaseException 展开也关闭源 FD，保留证明并可用原键恢复。
def test_script_delete_cleanup_on_cancellation(world, delete_resources):
    from asyncio import CancelledError

    script_id, key = 'r4-cancel-cleanup', 'r4-cancel-delete-key-0001'
    initial = create(world, script_id)
    home = world.host.home_paths.owner_home_dir
    delete_resources.failure = CancelledError()
    with pytest.raises(CancelledError):
        invoke(world, 'scripts.delete', {'script_id': script_id}, key=key, revision=initial['revision'])
    print('Cancelled delete fd_delta:', delete_resources.measurements)
    assert delete_resources.errors == 1 and delete_resources.measurements == [0]
    assert not any(_script_fd_is_open(fd, identity) for fd, identity, _parts in delete_resources.handles)
    proof = _script_delete_proof(home / 'scripts' / script_id, script_id)
    assert ScriptStore(home).get_script(script_id) is not None
    status, body = invoke(world, 'scripts.delete', {'script_id': script_id}, key=key, revision=initial['revision'])
    assert status == 200 and body['state'] == 'completed' and body['error'] is None, body
    private = home / 'data' / 'business_v13' / 'script_delete_proofs' / proof['operation_id']
    assert _script_delete_proof(private, script_id) == proof
    assert delete_resources.measurements == [0, 0]
    assert ScriptStore(home).get_script(script_id) is None


# LLM: Event 等到第二请求竞争首请求实持的原生锁才释放，不以 HTTP 到达代替锁竞争，不替换真实锁行为。
# 函数用途: 证明同键请求只删除一次、活动删除数不超过一，后到重放不擦除准确删除证明。
def test_script_delete_same_key_interleaving_retains_proof(v13_native, delete_resources, monkeypatch):
    from agent_py_agent.agent.common import json_io

    entered, contending, release = Event(), Event(), Event()
    guard = Lock()
    active = {'current': 0, 'maximum': 0, 'calls': 0}
    held = {'thread': None, 'mutexes': (), 'contended_path': None}
    original_path_lock = json_io.path_thread_lock
    original_delete = scripts._NativeCASStore._delete
    key, script_id = 'r4-interleaved-delete-key-0001', 'r4-interleaved'
    status, created = v13_call(v13_native, 'scripts.create',
        {'script_id': script_id, 'name': '并发删除', 'code': 'print("{}")'},
        user='business-alice', idempotency_key='r4-interleaved-create-key-0001')
    assert status == 200, created
    revision = created['data']['script']['revision']

    def observe_path_lock(path):
        handle = original_path_lock(path)
        with guard:
            waiting = not release.is_set() and not contending.is_set()
            other_thread = held['thread'] is not None and get_ident() != held['thread']
            same_mutex = any(handle is mutex for mutex in held['mutexes'])
            if waiting and other_thread and same_mutex:
                # 先用同一实际锁证明竞争失败，再走原获取器；不靠调度快慢推测第二请求是否已竞争。
                if handle.lock.acquire(blocking=False):
                    handle.lock.release()
                    raise AssertionError('首请求仍在等待时原生锁已提前释放')
                held['contended_path'] = str(path)
                contending.set()
        return handle

    def delete(store, item):
        with json_io._JSON_FILE_LOCKS_GUARD:
            observed_mutexes = tuple(
                mutex for name, mutex in json_io._JSON_FILE_LOCKS.items()
                if name.startswith(str(store.owner_home) + os.sep) and mutex.lock.locked()
            )
        assert observed_mutexes, '原生删除没有实持的 owner 锁'
        with guard:
            active['current'] += 1
            active['calls'] += 1
            active['maximum'] = max(active['maximum'], active['current'])
            held['thread'] = get_ident()
            held['mutexes'] = observed_mutexes
        entered.set()
        try:
            assert release.wait(10), '未收到第二个请求的交错释放'
            with guard:
                held['mutexes'] = ()
            return original_delete(store, item)
        finally:
            with guard:
                held['mutexes'] = ()
                active['current'] -= 1

    monkeypatch.setattr(json_io, 'path_thread_lock', observe_path_lock)
    monkeypatch.setattr(scripts._NativeCASStore, '_delete', delete)

    def request():
        return v13_call(v13_native, 'scripts.delete', {'script_id': script_id},
            user='business-alice', idempotency_key=key, expected_revision=revision)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(request)
        try:
            assert entered.wait(10), '首个删除未进入原生边界'
            second = pool.submit(request)
            assert contending.wait(10), '第二个 HTTP 请求未竞争首请求实持的原生锁'
            assert active == {'current': 1, 'maximum': 1, 'calls': 1}
        finally:
            release.set()
        results = [first.result(timeout=20), second.result(timeout=20)]
    assert active == {'current': 0, 'maximum': 1, 'calls': 1}
    assert held['contended_path'] is not None
    assert [status for status, _body in results] == [200, 200], results
    assert all(body['state'] == 'completed' and body['error'] is None for _status, body in results)
    assert results[0][1]['data'] == results[1][1]['data'] == {'script_id': script_id, 'deleted': True}
    proof_root = delete_resources.home / 'data' / 'business_v13' / 'script_delete_proofs'
    directories = list(proof_root.iterdir())
    assert len(directories) == 1
    proof = _script_delete_proof(directories[0], script_id)
    assert proof['operation_id'] == directories[0].name
    assert delete_resources.measurements == [0]
    print('Interleaved delete maximum_active:', active['maximum'], 'fd_delta:', delete_resources.measurements,
          'contended_lock:', Path(held['contended_path']).name)


# LLM: 经真实 HTTP 准入高风险运行，任务和 operation 仍由原 writer 产生；不签审批票或模拟展示输入。
# 函数用途: 保存批准路径的关联并通过规范 managed 写口发布真实 runner 输出。
def _150_approved_roundtrip(native, *, inputs=None, arguments=None, ok=True, effect_path=None):
    from agent_py_agent.agent.conversation.session_tasks import SessionTaskUpdate
    from agent_py_agent.agent.workflows.script_runner import ScriptRunner
    from agent_py_agent.tests.test_v13_w0_observation_readers import managed_roundtrip_operation

    space = v13_space(native)
    schema = {'type': 'object', 'properties': {'number': {'type': 'integer'}}, 'additionalProperties': False}
    status, created = v13_call(native, 'scripts.create', {'script_id': 'roundtrip-script',
        'space_id': space, 'name': '原生结算往返', 'code': (
            f"import os\nwith open({str(effect_path)!r}, 'a') as f:\n f.write('issued\\n');f.flush();os.fsync(f.fileno())\nraise SystemExit(9)"
            if effect_path else 'print(\'{"value": 7}\')' if ok else 'raise SystemExit(9)'),
        'risk_level': 'high', 'requires_approval': True, 'input_schema': schema},
        user='business-alice', idempotency_key='150-create-roundtrip')
    assert status == 200, created
    params = {'script_id': 'roundtrip-script', 'space_id': space}
    if inputs is not None:
        params['inputs'] = inputs
    key = '150-admit-roundtrip'
    status, accepted = v13_call(native, 'scripts.run', params,
        user='business-alice', idempotency_key=key)
    assert status == 202, accepted
    [services] = [service for service in native[0]._business_services.values()
        if service.owner_id.endswith('/business-alice')]
    store = services.host.conversation_store
    task = store.session_tasks.require(accepted['data']['task_id'])
    if task.status == 'queued':
        task = store.session_tasks.advance(task.task_id, SessionTaskUpdate(status='accepted'))
    if task.status == 'accepted':
        task = store.session_tasks.advance(task.task_id, SessionTaskUpdate(status='done'))
    assert task.status == 'done'
    actual_inputs = inputs or {}
    # 这里只运行隔离测试资产，验证结果运输与恢复；不声称覆盖真实人工批准链。
    result = ScriptRunner(services.owner_home, owner_scope_root=services.owner_home,
        agent_home_root=services.host.home_paths.root,
        extra_write_roots=(effect_path.parent,) if effect_path else ()).run_script('roundtrip-script', actual_inputs)
    assert result.ok is ok
    raw = {**vars(result), 'script_id': 'roundtrip-script'}
    args = arguments if arguments is not None else {
        'action': 'run', 'script_id': 'roundtrip-script', 'inputs': actual_inputs}
    runtime, run, operation_id, payload = managed_roundtrip_operation(services, task,
        arguments=args, raw=raw, ok=ok)
    return services, task, runtime, run, operation_id, params, key, accepted['data']


# LLM: 参数 hash 在准入时固定，空对象只允许省略和显式两种原生形状；服务重建后仍可恢复。
# 函数用途: 真实 HTTP 核对准入摘要、成功/失败恢复、重放终态和来源失效后的单调性。
@pytest.mark.parametrize('inputs,omit_tool_inputs,ok', [
    (None, True, True), ({}, False, True), ({'number': 4}, False, True), ({}, True, False),
])
def test_150_approved_managed_http_terminal(v13_native, inputs, omit_tool_inputs, ok):
    from agent_py_agent.agent.runtime_db.schema import runtime_db_path
    from agent_py_agent.agent.tooling.runtime_contracts import tool_arguments_hash

    args = {'action': 'run', 'script_id': 'roundtrip-script'}
    if not omit_tool_inputs:
        args['inputs'] = inputs or {}
    services, task, runtime, run, operation_id, params, key, accepted = _150_approved_roundtrip(
        v13_native, inputs=inputs, arguments=args, ok=ok)
    stored = V13Store(services.owner_home).get('script_runs', accepted['execution_id'])
    expected = [tool_arguments_hash({'action': 'run', 'script_id': 'roundtrip-script', 'inputs': inputs or {}})]
    if not inputs:
        expected.append(tool_arguments_hash({'action': 'run', 'script_id': 'roundtrip-script'}))
    assert stored['tool_args_hashes'] == expected
    status, body = v13_call(v13_native, 'scripts.runs.get', {'execution_id': accepted['execution_id']}, user='business-alice')
    assert status == 200, body
    terminal = body['data']['run']
    assert terminal['state'] == ('completed' if ok else 'unknown') and terminal['ok'] is (True if ok else None), terminal
    assert terminal['output'] == ({'value': 7} if ok else {})
    assert terminal['task_id'] == task.task_id
    assert terminal['coverage'] == (services.observation.readers['approvals']({}).coverage if ok else 'unknown')
    status, replay = v13_call(v13_native, 'scripts.run', params, user='business-alice', idempotency_key=key)
    assert status == 200 and replay['data']['run'] == terminal, replay
    # 精确来源此时损坏；已保存终态不依赖再次观察才能继续返回。
    with runtime._runtime_connection() as conn:
        conn.execute('UPDATE tool_operations SET outcome_json=? WHERE operation_id=?', ('{broken', operation_id))
        conn.commit()
    assert runtime.db_path == runtime_db_path(services.owner_home)
    services.observation.readers.clear()
    status, again = v13_call(v13_native, 'scripts.runs.get', {'execution_id': accepted['execution_id']}, user='business-alice')
    assert status == 200 and again['data']['run'] == terminal, again
    status, again = v13_call(v13_native, 'scripts.run', params, user='business-alice', idempotency_key=key)
    assert status == 200 and again['data']['run'] == terminal, again


# LLM: 各反例都通过真实 claim/settle 写入，公开观察不会提供虚构 redacted_input。
# 函数用途: 验证摘要包含 action、script、完整输入和额外参数，坏 task provenance 不恢复结果。
@pytest.mark.parametrize('case', ['action', 'script', 'inputs', 'extra', 'task_provenance', 'owner', 'attempt'])
def test_150_native_binding_rejects_mismatches(v13_native, case):
    args = {'action': 'run', 'script_id': 'roundtrip-script', 'inputs': {}}
    if case == 'action':
        args['action'] = 'list'
    elif case == 'script':
        args['script_id'] = 'different-script'
    elif case == 'inputs':
        args['inputs'] = {'number': 99}
    elif case == 'extra':
        args['other'] = True
    services, task, runtime, run, operation_id, params, key, accepted = _150_approved_roundtrip(
        v13_native, arguments=args)
    with runtime._runtime_connection() as conn:
        if case == 'task_provenance':
            metadata = json.loads(conn.execute('SELECT metadata_json FROM task_runs WHERE task_run_id=?',
                (run['task_run_id'],)).fetchone()[0])
            metadata['session_task_ref'] = 'different-task'
            conn.execute('UPDATE task_runs SET metadata_json=? WHERE task_run_id=?', (json.dumps(metadata), run['task_run_id']))
        elif case == 'owner':
            conn.execute('UPDATE tasks SET owner_id=? WHERE task_id=?', ('local/other', run['task_id']))
        elif case == 'attempt':
            other = runtime.record_run_creation(owner_id=services.owner_id, run_id='different-run')
            conn.execute('UPDATE tool_operations SET attempt_id=? WHERE operation_id=?', (other['attempt_id'], operation_id))
        conn.commit()
    status, body = v13_call(v13_native, 'scripts.runs.get', {'execution_id': accepted['execution_id']}, user='business-alice')
    assert status == 200, body
    assert body['data']['run']['state'] == 'unknown' and body['data']['run']['ok'] is None, body


# LLM: 此组只验证已准入记录的消费，不代替上面的真实派工测试；规范任务创建不发布 wake。
# 函数用途: 在受限环境也能覆盖真实 managed 结算、观察与脚本终态恢复。

_observation_gateway = observation_roundtrip.background_http


@pytest.fixture
def background_http(_observation_gateway):
    _observation_gateway[0].paths.processing.mkdir(parents=True, exist_ok=True)
    return _observation_gateway


@pytest.mark.parametrize('case', ['explicit_empty', 'omitted_empty', 'populated', 'wrong_input', 'failed'])
def test_150_native_consumer_without_dispatch(background_http, case):
    from agent_py_agent.agent.conversation.session_tasks import SessionTaskUpdate
    from agent_py_agent.agent.tooling.runtime_contracts import tool_arguments_hash

    services, task = observation_roundtrip.observed_task(background_http)
    inputs = {'number': 4} if case == 'populated' else {}
    status, created = observation_roundtrip.background_call(background_http, 'scripts.create', {
        'script_id': 'consumer-script', 'name': '消费者往返',
        'code': 'raise SystemExit(9)' if case == 'failed' else 'print(\'{"value": 7}\')',
        'input_schema': {'type': 'object', 'properties': {'number': {'type': 'integer'}}, 'additionalProperties': False}},
        idempotency_key='150-consumer-create')
    assert status == 200, created
    status, executed = observation_roundtrip.background_call(background_http, 'scripts.run',
        {'script_id': 'consumer-script', 'inputs': inputs}, idempotency_key='150-consumer-native-run')
    assert status == 200, executed
    actual = executed['data']['run']
    assert actual['state'] == ('unknown' if case == 'failed' else 'completed'), actual
    admitted = V13Store(services.owner_home).get('script_runs', actual['execution_id'])
    args = {'action': 'run', 'script_id': 'consumer-script', 'inputs': inputs}
    hashes = [tool_arguments_hash(args)]
    if not inputs:
        hashes.append(tool_arguments_hash({'action': 'run', 'script_id': 'consumer-script'}))
    raw = {'script_id': 'consumer-script', 'ok': actual['ok'], 'output_data': actual['output'],
        'duration_ms': actual['duration_ms'], 'started_at': 1, 'ended_at': 2,
        'error_code': actual['error'], 'effect_outcome': 'confirmed' if actual['ok'] else 'unknown'}
    if case == 'omitted_empty':
        args.pop('inputs')
    elif case == 'wrong_input':
        args['inputs'] = {'number': 99}
    runtime, run, operation_id, payload = observation_roundtrip.managed_roundtrip_operation(
        services, task, arguments=args, raw=raw, ok=actual['ok'])
    task = services.host.conversation_store.session_tasks.advance(task.task_id, SessionTaskUpdate(status='accepted'))
    services.host.conversation_store.session_tasks.advance(task.task_id, SessionTaskUpdate(status='done'))
    # 这是消费端的明确初态夹具，由真实 V13Store 保存，不注入观察 reader 或结果字典。
    bound = {**admitted, 'execution_id': 'consumer-approved', 'task_id': task.task_id,
        'requires_approval': True, 'state': 'accepted', 'ok': None, 'output': None, 'error': None,
        'started_at': None, 'finished_at': None, 'tool_args_hashes': hashes}
    v13_seed(services.owner_home, 'script_runs', {'consumer-approved': bound})
    status, body = observation_roundtrip.background_call(background_http, 'scripts.runs.get', {'execution_id': 'consumer-approved'})
    assert status == 200, body
    result = body['data']['run']
    expected = 'unknown' if case in ('wrong_input', 'failed') else 'completed'
    assert result['state'] == expected, result
    if expected != 'unknown':
        assert result['output'] == actual['output'] and result['ok'] is actual['ok']
        status, again = observation_roundtrip.background_call(background_http, 'scripts.runs.get', {'execution_id': 'consumer-approved'})
        assert status == 200 and again['data']['run'] == result, again
        with runtime._runtime_connection() as conn:
            conn.execute('UPDATE tool_operations SET outcome_json=? WHERE operation_id=?', ('{broken', operation_id))
            conn.commit()
        status, again = observation_roundtrip.background_call(background_http, 'scripts.runs.get', {'execution_id': 'consumer-approved'})
        assert status == 200 and again['data']['run'] == result, again


def test_approved_unknown_script_same_key_never_dispatches_or_repeats_effect(v13_native, tmp_path):
    marker = tmp_path / 'business-issued.log'
    services, task, runtime, _, operation_id, params, key, accepted = _150_approved_roundtrip(
        v13_native, ok=False, effect_path=marker)
    assert marker.read_text().splitlines() == ['issued']
    original = None
    for _ in range(3):
        status, body = v13_call(v13_native, 'scripts.runs.get', {
            'execution_id': accepted['execution_id']}, user='business-alice')
        assert status == 200 and body['data']['run']['state'] == 'unknown'
        observed = body['data']['run']
        assert observed['task_id'] == task.task_id and observed['error'] == 'SCRIPT_EXIT_NONZERO'
        original = original or observed
        assert observed == original
        status, replay = v13_call(v13_native, 'scripts.run', params,
            user='business-alice', idempotency_key=key)
        assert status == 200 and replay['data']['run'] == original
        assert marker.read_text().splitlines() == ['issued']
    saved = V13Store(services.owner_home).get('script_runs', accepted['execution_id'])
    assert saved['execution_result_ref']['operation_id'] == operation_id
    # 原证明已冻结，观察源损坏不能抹去原错误或制造新受理责任。
    with runtime._runtime_connection() as conn:
        conn.execute('UPDATE tool_operations SET outcome_json=? WHERE operation_id=?', ('{broken', operation_id))
        conn.commit()
    status, replay = v13_call(v13_native, 'scripts.run', params,
        user='business-alice', idempotency_key=key)
    assert status == 200 and replay['data']['run'] == original
    assert marker.read_text().splitlines() == ['issued']


# === Source: agent_py_agent/tests/test_v13_w7_simulation_sessions.py ===

native_http = native_fixtures.native_http


# LLM: 本地用真实签发上下文与可撤回空间授权，时钟固定用于稳定重现并发边界。
# 函数用途: 准备空间授权与持久事务测试环境。
@pytest.fixture
def local_world(v13_host, tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import business_api as api

    monkeypatch.setenv('HOME', str(tmp_path / 'os-home'))
    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path / 'runtime-home'))
    host = v13_host
    host.home_paths = replace(host.home_paths, owner_id='local/main', owner_provider='local', owner_kind='main')
    host.home_paths.owner_home_dir.mkdir(parents=True, exist_ok=True)
    host.business_v13_clock = lambda: 1793664000.0
    original, services = api.local_business_context(host)
    denied = set()

    # LLM: 只撤具体空间的权限，动作本身继续开放，证明回执不绕过记录复核。
    # 函数用途: 模拟宿主实时空间授权变化。
    def authorize(action, facts):
        return facts.get('space_id') not in denied and original.authorize(action, facts)

    context = api.bind_business_context(host, authorize=authorize, subject_id=original.subject_id,
        actor=original.actor, business_context=original.business_context,
        actor_factory=original.actor_factory, host_provider=original.host_provider)
    space = services.spaces.create({'name': '事务测试空间'}, actor=services.actor(context),
        idempotency_key='simulation-local-space-key')
    return SimpleNamespace(host=host, context=context, services=services, denied=denied, sid=space['space_id'])


# LLM: 直接调用仍经 v13 参数及实时权限边界，未伪造回执和领域结果。
# 函数用途: 用确定性宿主调用待测动作。
def _local(world, action, params, *, key=None, revision=None):
    from agent_py_agent.agent.gateway_parts import business_api_v13 as v13

    return v13.v13_invoke(action, params, world.context, world.services,
        request_id='simulation-local-request', idempotency_key=key or 'simulation-' + uuid.uuid4().hex,
        expected_revision=revision)


# LLM: 测试通过真实鉴权运输层，不替产品绕过 schema 或 owner 门。
# 函数用途: 发起原生业务请求并读取信封。
def _call(host, action, params=None, *, key=None, revision=None, user='business-alice'):
    request = {'action': action, 'request_id': 'w7-' + uuid.uuid4().hex, 'params': params or {}}
    if key is not None:
        request['idempotency_key'] = key
    if revision is not None:
        request['expected_revision'] = revision
    status, _, raw = native_request(host, 'POST', '/client/business', request, user=user)
    return status, json.loads(raw)


# LLM: 空间由冻结接口真实创建，测试不能假定一个预置空间存在。
# 函数用途: 准备本次会话所属空间。
def _space_w7_simulation(host):
    status, body = _call(host, 'spaces.create', {'name': 'W7演练空间'}, key='w7-simulation-space-create')
    assert status == 200, body
    return body['data']['space_id']


# LLM: 辅助函数只组装有效请求，不替被测对象生成业务结果。
# 函数用途: 创建一个可继续验证的会话。
def _session(host):
    sid = _space_w7_simulation(host)
    params = {'space_id': sid, 'scenario': 'service_recovery', 'title': '服务恢复桌面推演'}
    status, body = _call(host, 'purple_team.tabletop.create', params, key='w7-simulation-session-create')
    assert status == 200, body
    return body['data']['session']


# LLM: 原需求明确允许省略 space_id，必须由 W0 真实默认空间解析完成。
# 函数用途: 验证原始示例参数及同键重放。
def test_create_with_issue_example_params(native_http):
    _space_w7_simulation(native_http)
    params = {'scenario': 'ransomware_lateral_movement', 'title': 'Q4 勒索横向移动桌面推演'}
    status, first = _call(native_http, 'purple_team.tabletop.create', params, key='w7-session-example-key')
    assert status == 200, first
    assert first['state'] == 'completed' and first['error'] is None
    row = first['data']['session']
    assert row['status'] == 'active' and row['findings'] == [] and row['revision'] == 1
    assert _call(native_http, 'purple_team.tabletop.create', params,
                 key='w7-session-example-key')[1]['data'] == first['data']
    status, body = _call(native_http, 'purple_team.tabletop.create',
                         {**params, 'title': 'changed'}, key='w7-session-example-key')
    assert status == 409 and body['error']['code'] == 'IDEMPOTENCY_CONFLICT', body


# LLM: 关联执行必须真实持久化，重复调用不得增加第二条发现或执行。
# 函数用途: 验证技术发现、幂等关联及读取。
def test_add_finding_creates_execution_for_technique(native_http):
    row = _session(native_http)
    params = {'session_id': row['session_id'], 'title': '检测缺口', 'severity': 'high',
              'technique_id': 'T1059', 'detected': False}
    status, first = _call(native_http, 'purple_team.tabletop.add_finding', params,
                          key='w7-simulation-finding-key', revision=row['revision'])
    assert status == 200, first
    assert first['data']['execution']['detected'] is False
    assert first['data']['session']['revision'] == row['revision'] + 1
    status, replay = _call(native_http, 'purple_team.tabletop.add_finding', params,
                           key='w7-simulation-finding-key', revision=row['revision'])
    assert status == 200 and replay['data'] == first['data'], replay
    status, fetched = _call(native_http, 'purple_team.tabletop.get', {'session_id': row['session_id']})
    assert status == 200, fetched
    assert len(fetched['data']['session']['findings']) == 1
    assert fetched['data']['executions'] == [first['data']['execution']]


# LLM: 没技术编号的发现不应创建虚假执行，缺失和他人记录都须拒绝。
# 函数用途: 验证读取形状、404 和无技术发现。
def test_get_includes_executions(native_http):
    row = _session(native_http)
    status, result = _call(native_http, 'purple_team.tabletop.add_finding', {
        'session_id': row['session_id'], 'title': '沟通流程', 'severity': 'low'},
        key='w7-simulation-no-technique', revision=row['revision'])
    assert status == 200 and result['data']['execution'] is None, result
    assert _call(native_http, 'purple_team.tabletop.get', {'session_id': row['session_id']})[1]['data']['executions'] == []
    for record_id, user in [(row['session_id'], 'business-bob'), ('absent', 'business-alice')]:
        status, body = _call(native_http, 'purple_team.tabletop.get', {'session_id': record_id}, user=user)
        assert status == 404 and body['error']['code'] == 'RESOURCE_NOT_FOUND', body


# LLM: 归档只能推进一次，旧版本追加和新键重复完成都不能复活终态。
# 函数用途: 验证归档状态机与记录 CAS。
def test_complete_once_then_state_conflict(native_http):
    row = _session(native_http)
    params = {'session_id': row['session_id'], 'summary': '已记录问题'}
    status, first = _call(native_http, 'purple_team.tabletop.complete', params,
                          key='w7-simulation-complete', revision=row['revision'])
    assert status == 200 and first['data']['session']['status'] == 'completed', first
    assert first['data']['session']['completed_at'] is not None
    assert _call(native_http, 'purple_team.tabletop.complete', params,
                 key='w7-simulation-complete', revision=row['revision'])[1]['data'] == first['data']
    for action, args in [('purple_team.tabletop.complete', params),
                         ('purple_team.tabletop.add_finding', {'session_id': row['session_id'],
                           'title': '晚到', 'severity': 'low'})]:
        status, body = _call(native_http, action, args, key='w7-new-terminal-' + uuid.uuid4().hex,
                             revision=first['data']['session']['revision'])
        assert status == 409 and body['error']['code'] == 'STATE_CONFLICT', body
    status, stale = _call(native_http, 'purple_team.tabletop.complete', params,
                          key='w7-complete-stale-key', revision=row['revision'])
    assert status == 409 and stale['error']['code'] == 'REVISION_CONFLICT', stale


# LLM: 引用错误时不得改变执行版本，合法检测状态在同一原生入口可回读。
# 函数用途: 验证检测证据的版本与引用检查。
def test_update_detection_validates_rule_and_alert_refs(native_http):
    row = _session(native_http)
    status, added = _call(native_http, 'purple_team.tabletop.add_finding', {
        'session_id': row['session_id'], 'title': '验证记录', 'severity': 'medium', 'technique_id': 'T1059.001'},
        key='w7-simulation-update-prepare', revision=row['revision'])
    assert status == 200, added
    execution = added['data']['execution']
    params = {'execution_id': execution['execution_id'], 'detected': True, 'notes': '人工验证'}
    for field in ('detection_rule_id', 'alert_id'):
        status, body = _call(native_http, 'purple_team.executions.update_detection', {**params, field: 'absent'},
                             key='w7-simulation-ref-' + field, revision=execution['revision'])
        assert status == 404 and body['error']['code'] == 'RESOURCE_NOT_FOUND', body
    status, updated = _call(native_http, 'purple_team.executions.update_detection', params,
                            key='w7-simulation-update-valid', revision=execution['revision'])
    assert status == 200 and updated['data']['execution']['detected'] is True, updated
    assert updated['data']['execution']['revision'] == execution['revision'] + 1
    assert _call(native_http, 'purple_team.tabletop.get', {'session_id': row['session_id']})[1]['data']['executions'] == [updated['data']['execution']]


# LLM: 零规则是真实计数，但没有目录时分母仍未知而不是零覆盖。
# 函数用途: 验证覆盖快照差异及幂等。
def test_drift_snapshot_records_delta_vs_previous(native_http):
    sid = _space_w7_simulation(native_http)
    status, first = _call(native_http, 'purple_team.drift_snapshot', {'space_id': sid}, key='w7-snapshot-first')
    assert status == 200, first
    initial = first['data']['snapshot']
    assert initial['rules_total'] == 0 and initial['coverage_ratio'] is None
    assert initial['delta_since_previous'] is None
    assert _call(native_http, 'purple_team.drift_snapshot', {'space_id': sid}, key='w7-snapshot-first')[1]['data'] == first['data']
    status, second = _call(native_http, 'purple_team.drift_snapshot', {'space_id': sid}, key='w7-snapshot-next')
    assert status == 200, second
    assert second['data']['snapshot']['delta_since_previous'] == {
        'rules_total': 0, 'rules_enabled': 0, 'techniques_added': [], 'techniques_removed': []}


# LLM: 非法 envelope 在领域调用前拒绝，不因实现新增动作而改变冻结错误语义。
# 函数用途: 验证闭合参数、缺失 CAS 及未知动作信封。
@pytest.mark.parametrize('action,params,key,revision,code', [
    ('purple_team.tabletop.create', {'scenario': 'invalid-value', 'title': 'test'}, 'w7-invalid-schema-key', None, 'INVALID_SCHEMA'),
    ('purple_team.tabletop.complete', {'session_id': 'absent'}, 'w7-missing-revision-key', None, 'INVALID_SCHEMA'),
    ('purple_team.unknown', {}, None, None, 'UNSUPPORTED_ACTION'),
])
def test_w7_simulation_envelopes(native_http, action, params, key, revision, code):
    status, body = _call(native_http, action, params, key=key, revision=revision)
    assert status == 400 and body['state'] == 'failed' and body['data'] == {}, body
    assert body['error']['code'] == code


# LLM: 冻结合同是非空最长十六字符，不在领域层偷偷加 MITRE 专用正则。
# 函数用途: 验证自定义技术标识仍创建真实执行。
def test_custom_technique_respects_frozen_string_contract(native_http):
    row = _session(native_http)
    status, body = _call(native_http, 'purple_team.tabletop.add_finding', {
        'session_id': row['session_id'], 'title': '自定义验证', 'severity': 'low',
        'technique_id': 'custom_technique'}, key='simulation-custom-technique', revision=row['revision'])
    assert status == 200, body
    assert body['data']['execution']['technique_id'] == 'custom_technique'


# LLM: 固定时钟和反序 UUID 排除碰巧按 UUID 找到最近快照的假绿。
# 函数用途: 验证非空覆盖变化后最近提交前序可靠。
def test_snapshot_same_time_uses_commit_order(local_world, monkeypatch):
    from agent_py_agent.agent.business_spaces import v13_w7_simulation_sessions as simulation

    world = local_world
    rule = _local(world, 'monitors.rules.create', {'space_id': world.sid, 'name': '前序测试',
        'query': {'language': 'sql', 'text': 'SELECT 1'}, 'severity': 'low', 'enabled': True,
        'mitre_techniques': ['T1059']})['rule']
    ids = iter(['ffffffff', '00000000', 'aaaaaaaa'])
    monkeypatch.setattr(simulation, 'uuid', SimpleNamespace(uuid4=lambda: SimpleNamespace(hex=next(ids))))
    _local(world, 'purple_team.drift_snapshot', {'space_id': world.sid}, key='simulation-sequence-first')
    _local(world, 'monitors.rules.update', {'rule_id': rule['rule_id'], 'enabled': False}, revision=rule['revision'])
    second = _local(world, 'purple_team.drift_snapshot', {'space_id': world.sid}, key='simulation-sequence-second')
    assert second['snapshot']['delta_since_previous']['techniques_removed'] == ['T1059']
    third = _local(world, 'purple_team.drift_snapshot', {'space_id': world.sid}, key='simulation-sequence-third')
    assert third['snapshot']['delta_since_previous'] == {
        'rules_total': 0, 'rules_enabled': 0, 'techniques_added': [], 'techniques_removed': []}


# LLM: 命中回执不等于再次取得空间授权，撤权不能泄露旧结果。
# 函数用途: 验证同键重放完整保留记录级权限门。
def test_replay_rechecks_space_authorization(local_world):
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    world = local_world
    row = _local(world, 'purple_team.tabletop.create', {'space_id': world.sid,
        'scenario': 'service_recovery', 'title': '撤权测试'})['session']
    params = {'session_id': row['session_id'], 'title': '一次发现', 'severity': 'low'}
    first = _local(world, 'purple_team.tabletop.add_finding', params,
        key='simulation-replay-scope', revision=row['revision'])
    assert len(first['session']['findings']) == 1
    world.denied.add(world.sid)
    with pytest.raises(HostAPIError) as error:
        _local(world, 'purple_team.tabletop.add_finding', params,
            key='simulation-replay-scope', revision=row['revision'])
    assert error.value.code == 'CAPABILITY_DENIED'
