"""密码策略 + 加盐慢散列(scrypt,纯 stdlib)。

策略:至少 8 位,必须同时含大写字母、小写字母和数字。密码永不明文存储,
散列格式 ``scrypt$<n>$<r>$<p>$<salt_hex>$<hash_hex>``,校验用恒时比较。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16
_KEY_LEN = 32


class PasswordPolicyError(ValueError):
    """密码不符合策略(长度/大小写/数字)。"""


def validate_password_policy(password: str) -> None:
    problems: list[str] = []
    if len(password) < 8:
        problems.append("长度至少 8 位")
    if not any(c.isupper() for c in password):
        problems.append("需要至少一个大写字母")
    if not any(c.islower() for c in password):
        problems.append("需要至少一个小写字母")
    if not any(c.isdigit() for c in password):
        problems.append("需要至少一个数字")
    if problems:
        raise PasswordPolicyError("密码不符合策略:" + ";".join(problems))


def hash_password(password: str) -> str:
    validate_password_policy(password)
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_KEY_LEN,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """恒时比较验证;格式坏/方案不符一律 False(绝不外抛)。"""
    try:
        scheme, n_s, r_s, p_s, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = bytes.fromhex(hash_hex)
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n_s),
            r=int(r_s),
            p=int(p_s),
            dklen=len(expected),
        )
        return hmac.compare_digest(digest, expected)
    except (ValueError, TypeError):
        return False


__all__ = ["PasswordPolicyError", "hash_password", "validate_password_policy", "verify_password"]
