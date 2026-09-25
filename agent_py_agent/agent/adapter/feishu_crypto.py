"""飞书 webhook 加解密(Tier 1 飞书接入,移植 参考实现 im/feishu_crypto.py)。

my-agent 现有 feishu.py 只验签、**缺加密事件解密**(配了 Encrypt Key 时飞书发的是 AES 加密体);
本模块补上。研究确认 参考实现 是三家里唯一真实现飞书 AES 解密 + fail-closed 的。

- 签名:``sha256(timestamp + nonce + encrypt_key + raw_body)``,常数时间比较;**encrypt_key 空则
  fail-closed**(签名不含任何秘密,攻击者可自算匹配 → 验签沦为空操作)。
- 加密事件:``AES-256-CBC``,key = ``SHA256(encrypt_key)``(32 字节),IV = 密文前 16 字节,PKCS7(块 16)。

自建取舍:签名/结构解析/PKCS7 全 stdlib 自建;**AES 唯一借库**(stdlib 无 AES、密码学不能手搓),
用 Phase 1 已引的可选 cryptography,缺失则明确报错(不静默)。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import NamedTuple

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False


class FeishuCryptoError(Exception):
    pass


class FeishuSignParts(NamedTuple):
    """飞书签名的四个分量(打包传参,内聚 + 满足参数数纪律)。"""

    timestamp: str
    nonce: str
    encrypt_key: str
    raw_body: bytes


@dataclass(frozen=True)
class FeishuWebhookDecodeRequest:
    raw_body: bytes
    encrypt_key: str = ""
    verification_token: str = ""
    timestamp: str = ""
    nonce: str = ""
    signature: str = ""


def compute_signature(parts: FeishuSignParts) -> str:
    content = parts.timestamp.encode() + parts.nonce.encode() + parts.encrypt_key.encode() + parts.raw_body
    return hashlib.sha256(content).hexdigest()


def verify_signature(parts: FeishuSignParts, signature: str) -> bool:
    if not (parts.timestamp and parts.nonce and signature and parts.encrypt_key):
        return False  # encrypt_key 空 → fail-closed
    return hmac.compare_digest(compute_signature(parts), signature)


def verify_and_decode_webhook(request: FeishuWebhookDecodeRequest) -> dict | None:
    """飞书 webhook 的唯一验真/解密入口；任一步失败都返回 None。"""
    if not request.encrypt_key and not request.verification_token:
        return None
    if request.encrypt_key and not verify_signature(
        FeishuSignParts(
            request.timestamp,
            request.nonce,
            request.encrypt_key,
            request.raw_body,
        ),
        request.signature,
    ):
        return None
    try:
        outer = json.loads(request.raw_body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(outer, dict):
        return None
    if not request.encrypt_key and not _verification_token_matches(
        outer,
        request.verification_token,
    ):
        return None
    if "encrypt" not in outer:
        return outer
    if not request.encrypt_key:
        return None
    try:
        inner = json.loads(decrypt_event(request.encrypt_key, str(outer["encrypt"])))
    except Exception:
        return None
    return inner if isinstance(inner, dict) else None


def _verification_token_matches(payload: dict, expected: str) -> bool:
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    actual = str(payload.get("token") or header.get("token") or "")
    return bool(expected) and hmac.compare_digest(actual, expected)


def _require_crypto() -> None:
    if not _HAS_CRYPTO:
        raise FeishuCryptoError("飞书加密事件需 AES-256-CBC,请装 cryptography(stdlib 无 AES,不能自建)")


def _pkcs7_unpad16(padded: bytes) -> bytes:
    pad_len = padded[-1] if padded else 0
    if pad_len < 1 or pad_len > 16 or pad_len > len(padded):
        raise FeishuCryptoError("PKCS7 填充非法")
    return padded[:-pad_len]


def decrypt_event(encrypt_key: str, encrypted_b64: str) -> bytes:
    """解密飞书 ``encrypt`` 载荷;任何一步出错都 fail-closed。"""
    _require_crypto()
    if not encrypted_b64:
        raise FeishuCryptoError("空的加密载荷")
    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    try:
        raw = base64.b64decode(encrypted_b64)
    except (ValueError, TypeError) as exc:
        raise FeishuCryptoError("base64 解码失败") from exc
    if len(raw) <= 16 or (len(raw) - 16) % 16 != 0:
        raise FeishuCryptoError("密文长度非法")
    iv, ciphertext = raw[:16], raw[16:]
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    return _pkcs7_unpad16(padded)


def encrypt_event_for_test(encrypt_key: str, plaintext: bytes, iv: bytes) -> str:
    """测试助手:产出飞书格式加密载荷(iv + 密文,base64);非生产用。"""
    _require_crypto()
    if len(iv) != 16:
        raise FeishuCryptoError("IV 必须为 16 字节")
    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    pad_len = 16 - (len(plaintext) % 16)
    padded = plaintext + bytes([pad_len]) * pad_len
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(iv + ciphertext).decode("ascii")
