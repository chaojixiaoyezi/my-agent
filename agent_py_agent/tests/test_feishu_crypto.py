"""Tier 1 飞书加解密测试:签名 fail-closed + AES-256-CBC 往返 + 篡改拒绝。"""

from __future__ import annotations

import pytest

from agent_py_agent.agent.adapter.feishu_crypto import (
    FeishuCryptoError,
    FeishuSignParts,
    compute_signature,
    decrypt_event,
    encrypt_event_for_test,
    verify_signature,
)


def test_signature_deterministic() -> None:
    parts = FeishuSignParts("1700000000", "nonce1", "ek-secret", b'{"a":1}')
    assert compute_signature(parts) == compute_signature(parts)
    assert len(compute_signature(parts)) == 64  # sha256 hex


def test_verify_signature_valid_invalid_and_failclosed() -> None:
    parts = FeishuSignParts("ts", "nc", "ek", b"body")
    sig = compute_signature(parts)
    assert verify_signature(parts, sig) is True
    assert verify_signature(parts, "wrong") is False
    # encrypt_key 空 → fail-closed(否则验签沦为空操作)
    assert verify_signature(FeishuSignParts("ts", "nc", "", b"body"), compute_signature(FeishuSignParts("ts", "nc", "", b"body"))) is False


def test_aes_event_roundtrip() -> None:
    key = "feishu-encrypt-key-123"
    plain = '{"type":"event","msg":"你好飞书"}'.encode()
    payload = encrypt_event_for_test(key, plain, iv=b"0123456789abcdef")
    assert decrypt_event(key, payload) == plain


def test_decrypt_wrong_key_fails_closed() -> None:
    payload = encrypt_event_for_test("right-key", b"secret", iv=b"0123456789abcdef")
    with pytest.raises(FeishuCryptoError):  # 错 key → 解出乱码 PKCS7 非法 → fail-closed
        decrypt_event("wrong-key", payload)


def test_decrypt_empty_and_malformed() -> None:
    with pytest.raises(FeishuCryptoError):
        decrypt_event("k", "")
    with pytest.raises(FeishuCryptoError):
        decrypt_event("k", "not-valid-base64!!!")


def test_encrypt_requires_16_byte_iv() -> None:
    with pytest.raises(FeishuCryptoError):
        encrypt_event_for_test("k", b"x", iv=b"short")
