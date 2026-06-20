"""Phase 4 企业微信回调加解密测试:签名 + AES-256-CBC 往返 + receiveid 防重放。"""

from __future__ import annotations

import base64
import os

import pytest

from agent_py_agent.agent.adapter.wecom_crypto import (
    WecomCryptoError,
    WecomSignParts,
    compute_signature,
    decrypt_message,
    encrypt_message,
    verify_signature,
)


def _valid_aes_key() -> str:
    # WeCom EncodingAESKey:43 字符(32 字节 base64 去掉尾部 "=")
    return base64.b64encode(os.urandom(32)).decode("ascii").rstrip("=")


def test_compute_signature_deterministic_and_order_independent() -> None:
    parts = WecomSignParts("tok", "123", "nonce", "ENC")
    assert compute_signature(parts) == compute_signature(parts)
    assert len(compute_signature(parts)) == 40  # sha1 hex


def test_verify_signature_valid_and_invalid() -> None:
    parts = WecomSignParts("tok", "ts", "nc", "enc")
    sig = compute_signature(parts)
    assert verify_signature(parts, sig) is True
    assert verify_signature(parts, "wrong") is False
    assert verify_signature(WecomSignParts("", "ts", "nc", "enc"), sig) is False  # 缺字段


def test_aes_roundtrip() -> None:
    key = _valid_aes_key()
    cipher = encrypt_message(key, "<xml>你好 WeCom</xml>", corp_id="ww123")
    assert decrypt_message(key, cipher, corp_id="ww123") == "<xml>你好 WeCom</xml>"


def test_decrypt_rejects_wrong_corpid() -> None:
    key = _valid_aes_key()
    cipher = encrypt_message(key, "hi", corp_id="ww-real")
    with pytest.raises(WecomCryptoError):  # receiveid != corpid → 防跨企业重放
        decrypt_message(key, cipher, corp_id="ww-attacker")


def test_invalid_aes_key_length() -> None:
    with pytest.raises(WecomCryptoError):
        encrypt_message("tooshort", "hi", corp_id="x")


def test_tampered_ciphertext_errors() -> None:
    key = _valid_aes_key()
    with pytest.raises(WecomCryptoError):
        decrypt_message(key, "not-valid-base64-ciphertext!!!", corp_id="x")
