"""加密密钥库(Phase 1)。综合 claw / 长期助手 / 通道运行时 三家 + my-agent 自建优先原则。

密钥按引用(``sec_...``)暴露给 agent/客户端;明文只在 Gateway 内部边界解析(把 API key 交给
HTTP 客户端那一刻),**绝不进 API 返回 / 事件 / 审计 / 日志**,摘要一律脱敏。

设计取舍(用户原则:能自建就自建,不能的才借库):
- **核心全自建 stdlib**:引用机制(学 claw 的 secret-by-ref)、文件存储、脱敏、0o600 权限
  (通道运行时 海量用)、`env:` 源解析。
- **唯一借库=加密原语**:Python stdlib 无 AES,且"密码学绝不能自己手搓"(安全铁律)→ 用
  ``cryptography`` 的 Fernet。但设为**可选依赖**:装了→真 at-rest 加密(学 claw);没装→
  base64 + 0o600 兜底并**诚实标注 encrypted=false**(不静默假装加密)。my-agent 因此零必需新依赖。
- claw 的 per-owner HKDF 多租户隔离留 future(my-agent 主要面向个人/小团队)。
"""

from __future__ import annotations

import base64
import json
import os
import secrets as _stdlib_secrets
import time
from dataclasses import dataclass
from pathlib import Path

from .common.json_io import locked_json_path

try:  # 唯一的"借库"且可选:加密原语(stdlib 无 AES,不能自建)
    from cryptography.fernet import Fernet, InvalidToken

    _HAS_CRYPTO = True
except ImportError:  # 没装也能用,降级到 0o600+base64 并诚实标注未加密
    _HAS_CRYPTO = False

_REF_PREFIX = "sec_"


class SecretStoreError(RuntimeError):
    """密钥库操作错误(如文件损坏时拒绝写入,防覆盖销毁其余密钥)。"""


@dataclass(frozen=True)
class SecretSummary:
    """密钥的安全摘要:只含 ref/name/created_at/encrypted,**永远不含明文**。"""

    ref: str
    name: str
    created_at: float
    encrypted: bool

    def to_dict(self) -> dict[str, object]:
        return {"ref": self.ref, "name": self.name, "created_at": self.created_at, "encrypted": self.encrypted}


def _load_or_create_master_key(path: Path) -> bytes:
    # 已存在则读;否则生成 Fernet 主密钥,O_EXCL + 0o600 原子创建(只属主可读写,杜绝同机他人读)。
    if path.exists():
        return path.read_bytes().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    return key


class SecretStore:
    """密钥库:核心全自建,加密原语可选借 cryptography。

    用法::

        store = SecretStore(home_root / "secrets")
        ref = store.register("minimax_api_key", "sk-...")    # 返回 sec_xxx,值不再出来
        key = store.resolve_source(ref)                       # 或 resolve_source("env:AGENT_API_KEY")
    """

    def __init__(self, secrets_dir: str | Path) -> None:
        self._dir = Path(secrets_dir).expanduser()
        self._store_path = self._dir / "store.json"
        self._fernet = None
        if _HAS_CRYPTO:
            self._fernet = Fernet(_load_or_create_master_key(self._dir / "master.key"))

    @property
    def encryption_active(self) -> bool:
        """True=真 Fernet 加密;False=未装 cryptography,仅 0o600+base64(调用方应警示用户)。"""
        return self._fernet is not None

    # --- 加/解密原语(借库 or 兜底)---
    def _seal(self, value: str) -> tuple[str, bool]:
        if self._fernet is not None:
            return self._fernet.encrypt(value.encode("utf-8")).decode("ascii"), True
        # 兜底:base64 不是加密,只是编码;靠 0o600 文件权限保护。encrypted=False 如实标注。
        return base64.b64encode(value.encode("utf-8")).decode("ascii"), False

    def _open(self, payload: str, encrypted: bool) -> str:
        if encrypted:
            if self._fernet is None:
                raise PermissionError("entry is encrypted but cryptography is not installed")
            try:
                return self._fernet.decrypt(payload.encode("ascii")).decode("utf-8")
            except InvalidToken as exc:
                raise PermissionError("secret cannot be decrypted with this runtime master key") from exc
        return base64.b64decode(payload.encode("ascii")).decode("utf-8")

    # --- 持久化(JSON 文件,0o600)---
    def _read(self) -> dict[str, dict[str, object]]:
        try:
            data = json.loads(self._store_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _read_for_mutation(self) -> dict[str, dict[str, object]]:
        """写路径专用:文件不存在 → {};存在但损坏 → 抛错(绝不当空,防被一条 register 覆盖销毁其余密钥)。"""
        if not self._store_path.exists():
            return {}
        try:
            data = json.loads(self._store_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SecretStoreError(f"密钥库文件损坏,拒绝写入以免覆盖销毁其余密钥: {type(exc).__name__}") from exc
        if not isinstance(data, dict):
            raise SecretStoreError("密钥库文件根不是对象,拒绝写入以免覆盖")
        return data

    def _write(self, data: dict[str, dict[str, object]]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self._store_path.with_name(self._store_path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._store_path)  # 原子替换,避免半写

    # --- 公开 API ---
    def register(self, name: str, value: str) -> str:
        """存一条密钥,返回引用 ``sec_...``。值除经 :meth:`resolve_plaintext_for_gateway` 外不再出来。"""
        if not name:
            raise ValueError("secret name required")
        if not value:
            raise ValueError("secret value required")
        ref = _REF_PREFIX + _stdlib_secrets.token_hex(12)
        payload, encrypted = self._seal(value)
        with locked_json_path(self._store_path):  # 线程锁+flock:读改写原子,防并发丢密钥
            data = self._read_for_mutation()  # 损坏则抛,不覆盖销毁
            data[ref] = {"name": name, "ciphertext": payload, "encrypted": encrypted, "created_at": time.time()}
            self._write(data)
        return ref

    def exists(self, ref: str) -> bool:
        return ref in self._read()

    def summary(self, ref: str) -> SecretSummary:
        entry = self._read().get(ref)
        if entry is None:
            raise KeyError(f"secret not found: {ref}")
        return self._summary_of(ref, entry)

    def list_summaries(self) -> list[SecretSummary]:
        out = [self._summary_of(ref, e) for ref, e in self._read().items()]
        return sorted(out, key=lambda s: s.created_at)

    @staticmethod
    def _summary_of(ref: str, entry: dict[str, object]) -> SecretSummary:
        return SecretSummary(
            ref=ref,
            name=str(entry.get("name", "")),
            created_at=float(entry.get("created_at", 0.0)),
            encrypted=bool(entry.get("encrypted", False)),
        )

    def remove(self, ref: str) -> bool:
        with locked_json_path(self._store_path):  # 与 register 同一把锁,读改写原子
            data = self._read_for_mutation()
            if ref not in data:
                return False
            del data[ref]
            self._write(data)
        return True

    def resolve_plaintext_for_gateway(self, ref: str) -> str:
        """Gateway 内部在 provider 边界解析明文。**禁止**从 API handler / 事件 / 审计 / 日志调用。"""
        if not isinstance(ref, str) or not ref.startswith(_REF_PREFIX):
            raise PermissionError(f"invalid secret reference: {ref!r}")
        entry = self._read().get(ref)
        if entry is None:
            raise KeyError(f"secret not found: {ref}")
        return self._open(str(entry.get("ciphertext", "")), bool(entry.get("encrypted", False)))

    def resolve_source(self, source: str) -> str:
        """统一解析"密钥来源"(Gateway 内部边界用),合三家最优:
        - ``sec_xxx``:本库密钥(学 claw 的引用机制)
        - ``env:VARNAME``:从环境变量取
        其余一律拒绝——不接受内联明文,逼调用方走引用/env,杜绝明文散落在 config/代码里。"""
        if not isinstance(source, str) or not source:
            raise ValueError("empty secret source")
        if source.startswith(_REF_PREFIX):
            return self.resolve_plaintext_for_gateway(source)
        if source.startswith("env:"):
            var = source[len("env:") :].strip()
            val = os.environ.get(var, "")
            if not val:
                raise KeyError(f"env secret not set: {var}")
            return val
        raise PermissionError(f"unsupported secret source (use sec_ ref or env:VAR): {source!r}")

    @staticmethod
    def redact(value: str) -> str:
        """把疑似密钥的字符串脱敏成可安全展示(不可逆)。"""
        if not value:
            return ""
        if len(value) <= 4:
            return "****"
        return f"{value[:2]}***{value[-2:]}"
