"""企业微信回调加解密(Phase 4,移植 参考实现 im/wecom_crypto.py 的官方加解密方案)。

- 签名:``sha1("".join(sorted([token, timestamp, nonce, encrypt])))``,常数时间比较(stdlib 自建)。
- AES-256-CBC:key = Base64Decode(EncodingAESKey + "="),iv = key[:16];明文结构 =
  random(16) + 网络序 msg_len(4) + msg + receiveid;自定义 PKCS7(块长 32)。
  解密后校验 receiveid == corpid(防跨企业重放)。

自建取舍:签名/结构解析全 stdlib 自建;**AES 是唯一借库**——stdlib 无 AES、密码学不能手搓,
用 cryptography(Phase 1 已引的可选依赖)。WeCom 没 AES 根本无法解密,故 cryptography 对 WeCom
是硬需求:缺失则明确抛 WecomCryptoError(不静默,诚实告知"装 cryptography 才能用企业微信")。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct
from typing import NamedTuple

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False

BLOCK_SIZE = 32


class WecomCryptoError(Exception):
    pass


class WecomSignParts(NamedTuple):
    """企业微信签名的四个分量(打包传参,内聚 + 满足参数数纪律)。"""

    token: str
    timestamp: str
    nonce: str
    encrypt: str


def compute_signature(parts: WecomSignParts) -> str:
    items = sorted([parts.token, parts.timestamp, parts.nonce, parts.encrypt])
    return hashlib.sha1("".join(items).encode("utf-8")).hexdigest()


def verify_signature(parts: WecomSignParts, signature: str) -> bool:
    if not (parts.token and parts.timestamp and parts.nonce and parts.encrypt and signature):
        return False
    return hmac.compare_digest(compute_signature(parts), signature)


def _require_crypto() -> None:
    if not _HAS_CRYPTO:
        raise WecomCryptoError("企业微信回调需 AES-256-CBC,请安装 cryptography(stdlib 无 AES,不能自建)")


def _aes_key(encoding_aes_key: str) -> bytes:
    if len(encoding_aes_key) != 43:
        raise WecomCryptoError("EncodingAESKey 长度必须为 43")
    try:
        key = base64.b64decode(encoding_aes_key + "=")
    except (ValueError, TypeError) as exc:
        raise WecomCryptoError("EncodingAESKey 解码失败") from exc
    if len(key) != 32:
        raise WecomCryptoError("EncodingAESKey 解码后须为 32 字节")
    return key


def _pkcs7_pad(data: bytes) -> bytes:
    pad = BLOCK_SIZE - (len(data) % BLOCK_SIZE) or BLOCK_SIZE
    return data + bytes([pad]) * pad


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        raise WecomCryptoError("密文为空")
    pad = data[-1]
    if pad < 1 or pad > BLOCK_SIZE or pad > len(data):
        raise WecomCryptoError("填充非法")
    return data[:-pad]


def decrypt_message(encoding_aes_key: str, encrypted_b64: str, *, corp_id: str) -> str:
    """解密回调密文并校验 receiveid;返回明文消息(XML 或 echostr)。"""
    _require_crypto()
    key = _aes_key(encoding_aes_key)
    try:
        ciphertext = base64.b64decode(encrypted_b64)
    except (ValueError, TypeError) as exc:
        raise WecomCryptoError("密文 base64 解码失败") from exc
    if not ciphertext or len(ciphertext) % 16 != 0:
        raise WecomCryptoError("密文长度非法")
    decryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
    plain = _pkcs7_unpad(decryptor.update(ciphertext) + decryptor.finalize())
    if len(plain) < 20:
        raise WecomCryptoError("明文过短")
    msg_len = struct.unpack(">I", plain[16:20])[0]
    if msg_len < 0 or 20 + msg_len > len(plain):
        raise WecomCryptoError("明文长度字段非法")
    message = plain[20 : 20 + msg_len]
    receive_id = plain[20 + msg_len :].decode("utf-8", errors="replace")
    if corp_id and not hmac.compare_digest(receive_id, corp_id):
        raise WecomCryptoError("receiveid 与 corpid 不符")
    try:
        return message.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WecomCryptoError("明文非 UTF-8") from exc


def encrypt_message(encoding_aes_key: str, message: str, *, corp_id: str) -> str:
    """加密出站回包(被动回复用)。"""
    _require_crypto()
    key = _aes_key(encoding_aes_key)
    msg_bytes = message.encode("utf-8")
    payload = os.urandom(16) + struct.pack(">I", len(msg_bytes)) + msg_bytes + corp_id.encode("utf-8")
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    ciphertext = encryptor.update(_pkcs7_pad(payload)) + encryptor.finalize()
    return base64.b64encode(ciphertext).decode("ascii")
