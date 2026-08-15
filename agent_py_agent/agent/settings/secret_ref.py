"""密钥引用(SecretRef)解析 + 脱敏(#4 借鉴 通道运行时 secret-contract 的核心)。

让飞书等凭据字段的值不必内联明文进 YAML,而可写成间接引用:
  - ``env:FEISHU_APP_SECRET``  → 读环境变量(配 systemd EnvironmentFile,密钥不进源码树)
  - ``file:/etc/my-agent/feishu_secret`` → 读文件内容(去首尾空白)
  - 其余(普通字符串)         → 原样返回(兼容现状)

只做 env/file 两种来源(覆盖"密钥放源码树外"的核心诉求);通道运行时 还有 exec: 跑外部命令取密钥,
对本产品收益小、且引入子进程执行面,按简单优先暂不做。脱敏 redact() 供日志/回显用,杜绝明文泄漏。
"""

from __future__ import annotations

import os
from pathlib import Path

_ENV_PREFIX = "env:"
_FILE_PREFIX = "file:"


def resolve_secret_ref(value: str) -> str:
    """把密钥引用解析成实际值。``env:NAME`` / ``file:/path`` / 普通字符串(原样)。
    解析失败(env 未设 / 文件读不到)返回空串,由上层按"凭据缺失"处理。"""
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if stripped.startswith(_ENV_PREFIX):
        return os.environ.get(stripped[len(_ENV_PREFIX):].strip(), "")
    if stripped.startswith(_FILE_PREFIX):
        path = stripped[len(_FILE_PREFIX):].strip()
        try:
            return Path(path).expanduser().read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return value  # 普通值原样返回(不 strip,保持现状行为)


def is_secret_ref(value: str) -> bool:
    """是否为间接引用(env:/file:),用于判断该值是否需解析/不应直接当明文回显。"""
    if not isinstance(value, str):
        return False
    s = value.strip()
    return s.startswith(_ENV_PREFIX) or s.startswith(_FILE_PREFIX)


def redact(value: str, keep: int = 3) -> str:
    """脱敏:保留前 keep 位 + ``***``;空值返回空。用于日志/终端回显,杜绝明文密钥泄漏。"""
    if not value:
        return ""
    return f"{value[:keep]}***" if len(value) > keep else "***"


__all__ = ["resolve_secret_ref", "is_secret_ref", "redact"]
