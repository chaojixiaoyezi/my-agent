"""W10 MFA：原生 TOTP、主租户策略和真实 WebAuthn 公钥验证。

挑战消费与凭证更新使用同一多集合事务，不嵌套集合锁。验签失败以安全错误回执
原子消费挑战；同一幂等请求只回放结果，其他请求不能复用。本模块不签发会话。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import struct
from datetime import datetime, timezone
from urllib.parse import urlsplit

from ..plugin_host.errors import HostAPIError


# LLM: 使用稳定错误码，不回显凭证或底层异常。
# 函数用途: 抛出 MFA 业务错误。
def _fail(code="VERIFICATION_FAILED", status=403):
    raise HostAPIError(code, status)


# LLM: 时间来自可信调用时钟，不接受客户端时间。
# 函数用途: 将秒时间转换成标准 UTC 时间戳。
def _timestamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


# LLM: 复用原生认证服务的锁、密文存储和恢复码消费语义。
# 函数用途: 绑定本次调用的用户根与可信时钟。
def _native(call):
    from ..user_space.totp_sso_auth import TotpSSOAuthService
    from .v13_w10_identity import root
    return TotpSSOAuthService(root(call), clock=call.clock)


# LLM: 只按原生错误码映射，不从异常文本猜测原因。
# 函数用途: 将 TOTP 拒绝映射到 v13 错误协议。
def _totp_error(error):
    if error.code in {"TOTP_ALREADY_ENABLED", "TOTP_NOT_CONFIGURED", "TOTP_CODE_REPLAYED"}:
        _fail("STATE_CONFLICT", 409)
    if error.code == "TOTP_RATE_LIMITED":
        _fail("RATE_LIMITED", 429)
    if error.code in {"TOTP_INVALID", "TOTP_REQUIRED", "INVALID_CREDENTIALS"}:
        _fail()
    if error.code in {"USER_NOT_FOUND", "LOCAL_USER_NOT_FOUND"}:
        _fail("RESOURCE_NOT_FOUND", 404)
    if error.code == "STORE_UNAVAILABLE":
        _fail("HOST_NOT_READY", 503)
    raise error


# LLM: 默认策略不落盘，主租户策略只返回公开字段。
# 函数用途: 读取 MFA 策略，供身份投影复用。
def policy(call):
    from .v13_w10_identity import one
    row = one(call, "mfa_policy", "tenant", scope="main") or {}
    return {"required": row.get("required", False), "enforce_for": row.get("enforce_for", "all"),
            "grace_days": row.get("grace_days", 0), "revision": row.get("revision", 0),
            "updated_at": row.get("updated_at")}


# LLM: 只做状态投影，不解密种子；main 不进入普通用户校验路径。
# 函数用途: 返回 TOTP、通行密钥数量及主租户策略。
def status(call):
    from ..user_space.totp_sso_auth import _read
    from .v13_w10_identity import root, uid
    user = uid(call)
    try:
        row = _read(root(call), user + ".json") if call.owner_id.startswith("local/") and user != "main" else {}
    except HostAPIError as error:
        _totp_error(error)
    enabled = bool(row.get("enabled", False))
    return {"enabled": enabled, "pending": not enabled and row.get("pending_until", 0) > call.clock(),
            "recovery_codes_remaining": len(row.get("recovery_hashes", [])),
            "passkeys": len(_own_keys(call)), "policy": policy(call)}


# LLM: 原生操作无业务行，借挑战集合事务保存幂等回执，不另存种子或验证码。
# 函数用途: 在统一实时授权及幂等边界内执行 TOTP。
def _totp_write(call, operation):
    from .v13_w10_identity import uid, write
    if operation != "reset" and (not call.owner_id.startswith("local/") or uid(call) == "main"):
        _fail("BUSINESS_ACCESS_DENIED", 403)

    # LLM: 回调仅首次执行，重放由统一存储返回脱敏回执。
    # 函数用途: 调用原生认证并裁剪返回值。
    def mutate(records):
        service, user = _native(call), uid(call)
        try:
            if operation == "begin":
                result = service.setup_totp(user)
                return {"secret": result["secret"], "otpauth_uri": result["otpauth_uri"],
                        "recovery_codes": result["recovery_codes"],
                        "expires_at": _timestamp(int(call.clock()) + 600)}
            if operation == "confirm":
                return {"enabled": service.confirm_totp(user, call.params["code"])["enabled"]}
            if operation == "verify":
                code = call.params["code"]
                service.verify_totp(user, code)
                return {"verified": True, "recovery_code_used": re.fullmatch(r"[0-9]{6}", code) is None}
            if operation == "disable":
                return {"enabled": service.disable_totp(user, call.params["code"])["enabled"]}
            target = call.params["user_id"]
            return {"user_id": target, "enabled": service.disable_totp(target, admin=True)["enabled"]}
        except HostAPIError as error:
            _totp_error(error)

    # LLM: 首次返回秘密，其后回执不能包含秘密明文。
    # 函数用途: 构建 enrollment 的可持久化脱敏结果。
    def redacted(result):
        return {**result, "secret": "<redacted>", "otpauth_uri": "<redacted>",
                "recovery_codes": ["<redacted>" for _ in result["recovery_codes"]]}

    return write(call, "webauthn_challenges", mutate, receipt_view=redacted if operation == "begin" else None)


# LLM: URI 直接来自原生服务，不另建种子生成器。
# 函数用途: 签发一次性 TOTP 秘密。
def enroll_begin(call):
    return _totp_write(call, "begin")


# LLM: 首码激活与防重放由同一原生锁执行。
# 函数用途: 确认 TOTP enrollment。
def enroll_confirm(call):
    return _totp_write(call, "confirm")


# LLM: 动态码与恢复码共享原生限速及消费账。
# 函数用途: 校验并消费一个第二因素。
def verify(call):
    return _totp_write(call, "verify")


# LLM: 只信原生管理员门，RBAC 展示角色不授予写策略能力。
# 函数用途: CAS 更新主租户 MFA 策略。
def set_policy(call):
    from .v13_w10_identity import admin, cas, now, write
    if not admin(call):
        _fail("BUSINESS_ACCESS_DENIED", 403)

    # LLM: 锁内读取当前记录，避免旧快照覆盖。
    # 函数用途: 合并策略补丁并增加行版本。
    def mutate(records):
        old = records.get("tenant")
        cas(call, old)
        stamp = now(call)
        row = {"policy_id": "tenant", "owner_id": "local/main", "required": False,
               "enforce_for": "all", "grace_days": 0, "created_at": stamp, **(old or {}),
               **call.params, "revision": (old or {}).get("revision", 0) + 1, "updated_at": stamp}
        records["tenant"] = row
        return {"policy": {key: row[key] for key in ("required", "enforce_for", "grace_days", "revision", "updated_at")}}

    return write(call, "mfa_policy", mutate, scope="main")


# LLM: 用户停用必须消费有效第二因素。
# 函数用途: 停用自己的 TOTP。
def disable(call):
    return _totp_write(call, "disable")


# LLM: 目标用户参数只有原生管理员可使用。
# 函数用途: 管理员重置指定普通用户 TOTP。
def reset(call):
    from .v13_w10_identity import admin
    if not admin(call):
        _fail("BUSINESS_ACCESS_DENIED", 403)
    return _totp_write(call, "reset")


# LLM: 编码规范化，避免多种等价 credential ID。
# 函数用途: 编码标准无填充 base64url。
def _b64(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


# LLM: 输入有界且规范化，拒绝非法字符和非规范填充。
# 函数用途: 严格解码 WebAuthn 字段。
def _unb64(text):
    if type(text) is not str or not re.fullmatch(r"[A-Za-z0-9_-]+", text) or len(text) > 131072:
        _fail()
    try:
        result = base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=True)
    except ValueError:
        _fail()
    if _b64(result) != text:
        _fail()
    return result


# LLM: CBOR 只解析有限长度及深度，不执行标签或对象构造。
# 函数用途: 解码一个有界对象并返回消费位置。
def _cbor(raw, offset=0, depth=0):
    if depth > 16 or offset >= len(raw) or len(raw) > 98304:
        raise ValueError("CBOR边界无效")
    initial, offset = raw[offset], offset + 1
    major, info = initial >> 5, initial & 31
    if major == 7:
        if info in (20, 21, 22):
            return {20: False, 21: True, 22: None}[info], offset
        if info in (25, 26, 27):
            size, fmt = {25: (2, ">e"), 26: (4, ">f"), 27: (8, ">d")}[info]
            return struct.unpack(fmt, raw[offset:offset + size])[0], offset + size
        raise ValueError("CBOR简单值无效")
    if info < 24:
        value = info
    elif info in (24, 25, 26, 27):
        size = {24: 1, 25: 2, 26: 4, 27: 8}[info]
        if offset + size > len(raw):
            raise ValueError("CBOR长度无效")
        value, offset = int.from_bytes(raw[offset:offset + size], "big"), offset + size
    else:
        raise ValueError("CBOR不接受不定长")
    if major in (0, 1):
        return value if major == 0 else -1 - value, offset
    if major in (2, 3):
        if value > len(raw) - offset:
            raise ValueError("CBOR内容被截断")
        data = raw[offset:offset + value]
        return data if major == 2 else data.decode("utf-8"), offset + value
    if major in (4, 5) and value <= 2048:
        result = [] if major == 4 else {}
        for _ in range(value):
            item, offset = _cbor(raw, offset, depth + 1)
            if major == 4:
                result.append(item)
            else:
                if type(item) not in (int, str, bytes) or item in result:
                    raise ValueError("CBOR映射键无效")
                result[item], offset = _cbor(raw, offset, depth + 1)
        return result, offset
    raise ValueError("CBOR主类型无效")


# LLM: 只接受合同指定 COSE 算法，公钥参数由真实密码库校验。
# 函数用途: 从 COSE 字节恢复 ES256 或 RS256 公钥。
def _public_key(raw):
    from cryptography.hazmat.primitives.asymmetric import ec, rsa
    cose, end = _cbor(raw)
    if end != len(raw) or not isinstance(cose, dict):
        raise ValueError("COSE编码无效")
    if cose.get(1) == 2 and cose.get(3) == -7 and cose.get(-1) == 1:
        x, y = cose.get(-2), cose.get(-3)
        if type(x) is not bytes or type(y) is not bytes or len(x) != 32 or len(y) != 32:
            raise ValueError("EC坐标无效")
        return ec.EllipticCurvePublicNumbers(int.from_bytes(x, "big"), int.from_bytes(y, "big"), ec.SECP256R1()).public_key(), "ES256"
    if cose.get(1) == 3 and cose.get(3) == -257:
        n, e = cose.get(-1), cose.get(-2)
        if type(n) is not bytes or not 256 <= len(n) <= 1024 or type(e) is not bytes or not 1 <= len(e) <= 8:
            raise ValueError("RSA参数无效")
        return rsa.RSAPublicNumbers(int.from_bytes(e, "big"), int.from_bytes(n, "big")).public_key(), "RS256"
    raise ValueError("不支持的COSE算法")


# LLM: 验签算法取自已校验的公钥，不接受客户端替换。
# 函数用途: 执行真实 ES256 DER 或 RS256 PKCS1v15 验签。
def _signature(key, algorithm, signature, data):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, padding
    if algorithm == "ES256":
        key.verify(signature, data, ec.ECDSA(hashes.SHA256()))
    else:
        key.verify(signature, data, padding.PKCS1v15(), hashes.SHA256())


# LLM: owner 共享不代表跨主体授权。
# 函数用途: 取得当前 owner 与 subject 的私有公钥行。
def _own_keys(call):
    from .v13_w10_identity import rows
    return [row for row in rows(call, "passkeys")
            if row.get("owner_id") == call.owner_id and row.get("subject_id") == call.subject_id]


# LLM: 公钥与内部 scope 不进入公开展示。
# 函数用途: 裁剪通行密钥公开视图。
def _key_view(row):
    return {key: row[key] for key in ("credential_id", "label", "algorithm", "sign_count", "created_at", "last_used_at")}


# LLM: BFF 传入的 RP/origin 仍须满足浏览器基本绑定边界。
# 函数用途: 校验 begin 必填参数及 RP 和 HTTPS origin 的包含关系。
def _rp_params(call):
    rp, origin = call.params.get("rp_id"), call.params.get("origin")
    if type(rp) is not str or type(origin) is not str:
        _fail("INVALID_SCHEMA", 400)
    parsed = urlsplit(origin)
    host = parsed.hostname or ""
    if (not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", rp)
            or ".." in rp or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path or not (host == rp or host.endswith("." + rp))
            or parsed.scheme not in ("https", "http")
            or parsed.scheme == "http" and host != "localhost"):
        _fail("INVALID_SCHEMA", 400)
    return rp, origin


# LLM: 挑战用安全随机源，主体及用途持久化绑定，不信 finish 覆盖字段。
# 函数用途: 签发五分钟单次挑战和浏览器 publicKey options。
def _begin(call, purpose):
    from .v13_w10_identity import now, write
    rp, origin = _rp_params(call)
    keys = [{"id": key["credential_id"], "type": "public-key"} for key in _own_keys(call) if key["rp_id"] == rp]

    # LLM: 幂等重放返回原挑战；读公钥在事务外完成，不嵌套集合锁。
    # 函数用途: 保存挑战并生成标准创建或请求选项。
    def mutate(records):
        challenge_id, challenge = secrets.token_hex(16), _b64(secrets.token_bytes(32))
        stamp = now(call)
        row = {"challenge_id": challenge_id, "owner_id": call.owner_id, "subject_id": call.subject_id,
               "purpose": purpose, "challenge_b64": challenge, "rp_id": rp, "origin": origin,
               "used": False, "expires_at": _timestamp(call.clock() + 300), "revision": 1,
               "created_at": stamp, "updated_at": stamp}
        records[challenge_id] = row
        options = {"challenge": challenge, "timeout": 300000}
        if purpose == "register":
            options.update({"rp": {"id": rp, "name": rp},
                            "user": {"id": _b64(call.subject_id.encode()), "name": call.subject_id, "displayName": call.subject_id},
                            "pubKeyCredParams": [{"type": "public-key", "alg": -7}, {"type": "public-key", "alg": -257}],
                            "attestation": "none", "excludeCredentials": keys})
            return {"phase": "begin", "challenge_id": challenge_id, "options": options, "passkey": None}
        options.update({"rpId": rp, "allowCredentials": keys})
        return {"phase": "begin", "challenge_id": challenge_id, "options": options, "verified": False, "credential_id": None}

    return write(call, "webauthn_challenges", mutate)


# LLM: 此函数只能在挑战与凭证共同事务锁内调用。
# 函数用途: 检查并消费本主体本用途的未过期挑战。
def _claim(call, purpose, records):
    from .v13_w10_identity import now
    row = records.get(call.params["challenge_id"])
    if (not row or row.get("owner_id") != call.owner_id or row.get("subject_id") != call.subject_id
            or row.get("purpose") != purpose or row.get("used")
            or datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00")).timestamp() <= call.clock()):
        _fail("STATE_CONFLICT", 409)
    row.update({"used": True, "revision": row["revision"] + 1, "updated_at": now(call)})
    return row


# LLM: 验证浏览器绑定；跨 origin 与 userHandle 不能绕过主体边界。
# 函数用途: 校验 clientDataJSON 和标准凭证 ID。
def _client(call, challenge, purpose):
    from ..common.strict_json import load_strict_json
    credential = call.params["credential"]
    if credential.get("type") != "public-key" or not isinstance(credential.get("response"), dict):
        _fail()
    identifier = credential.get("rawId", credential.get("id"))
    _unb64(identifier)
    if credential.get("id", identifier) != identifier or len(identifier) > 1024:
        _fail()
    response = credential["response"]
    raw = _unb64(response.get("clientDataJSON"))
    # 编码字段不受外层JSON深度约束，先检查结构深度再进入递归解码器。
    depth, quoted, escaped = 0, False, False
    for char in raw.decode('utf-8'):
        if escaped:
            escaped = False
        elif quoted and char == '\\':
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif not quoted and char in '[{':
            depth += 1
            if depth > 16:
                _fail()
        elif not quoted and char in ']}':
            depth -= 1
    data = load_strict_json(raw)
    if (not isinstance(data, dict) or data.get("type") != ("webauthn.create" if purpose == "register" else "webauthn.get")
            or not hmac.compare_digest(str(data.get("challenge", "")), challenge["challenge_b64"])
            or data.get("origin") != challenge["origin"] or data.get("crossOrigin", False) is not False):
        _fail()
    if response.get("userHandle") is not None and _unb64(response["userHandle"]) != call.subject_id.encode():
        _fail()
    return identifier, response, hashlib.sha256(raw).digest()


# LLM: 验证 RP hash 与 UP；AT/ED 必须精确消费，不接受截断。
# 函数用途: 解析 authenticatorData 前缀、计数、凭证与扩展。
def _auth_data(raw, challenge, registration):
    if len(raw) < 37 or raw[:32] != hashlib.sha256(challenge["rp_id"].encode()).digest() or not raw[32] & 1:
        _fail()
    flags, count = raw[32], int.from_bytes(raw[33:37], "big")
    offset, identifier, cose = 37, None, None
    if registration:
        if not flags & 64 or len(raw) < 55:
            _fail()
        size = int.from_bytes(raw[53:55], "big")
        offset = 55 + size
        if not 1 <= size <= 768 or offset >= len(raw):
            _fail()
        identifier = _b64(raw[55:offset])
        _, end = _cbor(raw, offset)
        cose, offset = raw[offset:end], end
    elif flags & 64:
        _fail()
    if flags & 128:
        extensions, offset = _cbor(raw, offset)
        if not isinstance(extensions, dict):
            _fail()
    if offset != len(raw):
        _fail()
    return count, identifier, cose


# LLM: 证书链不当作self-attestation；标签取finish请求的正式字段，挑战保持闭合记录。
# 函数用途: 校验注册证明并准备公钥行。
def _registration(call, challenge):
    identifier, response, client_hash = _client(call, challenge, "register")
    raw = _unb64(response.get("attestationObject"))
    attestation, end = _cbor(raw)
    if end != len(raw) or not isinstance(attestation, dict) or type(attestation.get("authData")) is not bytes:
        _fail()
    auth = attestation["authData"]
    count, embedded_id, cose = _auth_data(auth, challenge, True)
    if identifier != embedded_id:
        _fail()
    key, algorithm = _public_key(cose)
    statement = attestation.get("attStmt")
    if attestation.get("fmt") == "none":
        if statement != {}:
            _fail()
    elif attestation.get("fmt") == "packed":
        if (not isinstance(statement, dict) or set(statement) != {"alg", "sig"}
                or statement["alg"] != (-7 if algorithm == "ES256" else -257) or type(statement["sig"]) is not bytes):
            _fail()
        _signature(key, algorithm, statement["sig"], auth + client_hash)
    else:
        _fail()
    from .v13_w10_identity import now
    stamp = now(call)
    return {"credential_id": identifier, "owner_id": call.owner_id, "subject_id": call.subject_id,
            "label": call.params.get("label", "通行密钥"), "algorithm": algorithm,
            "public_key_cose_b64": _b64(cose), "sign_count": count, "rp_id": challenge["rp_id"],
            "last_used_at": None, "revision": 1, "created_at": stamp, "updated_at": stamp}


# LLM: 最终凭证事务内验签及递增计数，避免两个挑战使用锁外旧计数覆盖。
# 函数用途: 使用当前公钥记录验证断言并提交使用状态。
def _authenticate_record(call, challenge, records):
    from .v13_w10_identity import now
    identifier, response, client_hash = _client(call, challenge, "authenticate")
    row = records.get(identifier)
    if (not row or row.get("owner_id") != call.owner_id or row.get("subject_id") != call.subject_id
            or row.get("rp_id") != challenge["rp_id"]):
        _fail()
    auth = _unb64(response.get("authenticatorData"))
    count, _, _ = _auth_data(auth, challenge, False)
    key, algorithm = _public_key(_unb64(row["public_key_cose_b64"]))
    _signature(key, algorithm, _unb64(response.get("signature")), auth + client_hash)
    previous = row["sign_count"]
    if (previous != 0 or count != 0) and count <= previous:
        _fail()
    row.update({"sign_count": count, "last_used_at": now(call), "updated_at": now(call),
                "revision": row["revision"] + 1})
    return {"phase": "finish", "challenge_id": None, "options": None, "verified": True, "credential_id": identifier}


# LLM: 挑战与凭证同笔提交；失败消费及安全错误回执也原子保存。
# 函数用途: 完成注册或认证，并统一处理格式及签名拒绝。
def _finish(call, purpose):
    from cryptography.exceptions import InvalidSignature

    from .v13_w10_identity import write_many
    if not call.params.get("challenge_id") or not isinstance(call.params.get("credential"), dict):
        _fail("INVALID_SCHEMA", 400)

    # LLM: 验证和当前凭证更新在同一锁内；不进行网络或模型调用。
    # 函数用途: 保存注册结果或通过当前公钥验证认证断言。
    def mutate(collections):
        challenge = _claim(call, purpose, collections["webauthn_challenges"])
        records = collections["passkeys"]
        try:
            if purpose == "authenticate":
                return _authenticate_record(call, challenge, records)
            row = _registration(call, challenge)
            identifier = row["credential_id"]
            if identifier in records:
                _fail("STATE_CONFLICT", 409)
            if sum(record.get("subject_id") == call.subject_id and record.get("owner_id") == call.owner_id
                   for record in records.values()) >= 50:
                _fail("LIMIT_EXCEEDED", 400)
            records[identifier] = row
            return {"phase": "finish", "challenge_id": None, "options": None, "passkey": _key_view(row)}
        except HostAPIError as error:
            return {"_failure": error.code, "_status": error.status}
        except (ValueError, TypeError, KeyError, UnicodeError, struct.error, InvalidSignature):
            return {"_failure": "VERIFICATION_FAILED", "_status": 403}

    result = write_many(call, ("webauthn_challenges", "passkeys"), mutate)
    if "_failure" in result:
        raise HostAPIError(result["_failure"], result["_status"], outcome="committed")
    return result


# LLM: phase 为公开结构化枚举，不使用文本路由。
# 函数用途: 执行通行密钥注册。
def passkey_register(call):
    return _begin(call, "register") if call.params["phase"] == "begin" else _finish(call, "register")


# LLM: MFA WebAuthn 与 passkey 使用同一签名及消费路径。
# 函数用途: 执行通行密钥第二因素认证。
def passkey_authenticate(call):
    return _begin(call, "authenticate") if call.params["phase"] == "begin" else _finish(call, "authenticate")


# LLM: 稳定排序返回自身公开字段，不泄露公钥或其他主体记录。
# 函数用途: 列出当前主体通行密钥。
def passkey_list(call):
    return {"items": [_key_view(row) for row in sorted(_own_keys(call), key=lambda row: row["credential_id"])]}


# LLM: 其他主体与不存在均返回 404，删除在当前行锁内进行。
# 函数用途: 吊销当前主体的一把通行密钥。
def passkey_delete(call):
    from .v13_w10_identity import write

    # LLM: scope 检查与删除原子执行，不依赖锁外快照。
    # 函数用途: 原子移除当前主体指定凭证。
    def mutate(records):
        identifier = call.params["credential_id"]
        row = records.get(identifier)
        if not row or row.get("owner_id") != call.owner_id or row.get("subject_id") != call.subject_id:
            _fail("RESOURCE_NOT_FOUND", 404)
        del records[identifier]
        return {"credential_id": identifier, "deleted": True}

    return write(call, "passkeys", mutate)
