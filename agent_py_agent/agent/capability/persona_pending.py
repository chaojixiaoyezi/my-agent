# LLM: 待确认的 SOUL/AGENTS 长期人设写入——文件型存储,放 <my_agent_home>/pending_persona/<token>.json,
#   网关侧(update_persona 工具存)与适配器侧(飞书卡片回调取)同一 my_agent_home 根、都能读到。一条记录=
#   {token, owner_*, target(soul/agents), content, created_at}。pop 用原子领取(os.replace)保证并发/重复
#   回调只有一个调用能拿到记录 → 只写一次(幂等基石)。TTL 过期(默认 24h)自动作废。改动时同步测试。
from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

_DIR_NAME = "pending_persona"
_DEFAULT_TTL_SECONDS = 24 * 3600
# token 由 uuid4().hex 生成;取用时严格校验(卡片 value.token 属外部输入,防路径穿越)。
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


@dataclass(frozen=True)
class PendingPersona:
    token: str
    owner_provider: str
    owner_kind: str
    owner_id: str
    target: str  # soul / agents
    content: str
    created_at: float

    def is_expired(self, ttl_seconds: float = _DEFAULT_TTL_SECONDS, *, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) - self.created_at > ttl_seconds


def add(root: str | Path, owner: tuple[str, str, str], target: str, content: str) -> str:
    """登记一条待确认写入,返回 token(uuid4)。owner=(provider, owner_kind, owner_id)。
    顺手清过期(自愈,防堆积)。"""
    provider, owner_kind, owner_id = owner
    token = uuid.uuid4().hex
    record = PendingPersona(
        token=token,
        owner_provider=str(provider),
        owner_kind=str(owner_kind),
        owner_id=str(owner_id),
        target=str(target),
        content=str(content),
        created_at=time.time(),
    )
    directory = _pending_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    purge_expired(root)
    _write_record(directory / f"{token}.json", record)
    return token


def load(root: str | Path, token: str, ttl_seconds: float = _DEFAULT_TTL_SECONDS) -> PendingPersona | None:
    """读一条待确认记录(不删除);缺失/损坏/过期返回 None。"""
    path = _record_path(root, token)
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    record = _record_from_dict(data)
    if record is None or record.is_expired(ttl_seconds):
        return None
    return record


def pop(root: str | Path, token: str, ttl_seconds: float = _DEFAULT_TTL_SECONDS) -> PendingPersona | None:
    """原子领取一条待确认记录并删除。并发/重复调用只有一个能拿到(os.replace 原子),其余得 None
    ——这是"卡片重复回调不重复写"的幂等基石。缺失/损坏/过期返回 None。"""
    path = _record_path(root, token)
    if path is None:
        return None
    claim = path.parent / f"{path.stem}.claim.{uuid.uuid4().hex}"
    try:
        os.replace(path, claim)  # 原子领取:并发下只有一个 replace 成功,其余 FileNotFoundError
    except OSError:
        return None
    try:
        data = json.loads(claim.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _unlink_quiet(claim)
        return None
    _unlink_quiet(claim)
    record = _record_from_dict(data)
    if record is None or record.is_expired(ttl_seconds):
        return None
    return record


def purge_expired(root: str | Path, ttl_seconds: float = _DEFAULT_TTL_SECONDS, *, now: float | None = None) -> int:
    """清掉过期/损坏的待确认记录,返回清理条数。目录不存在返回 0。"""
    directory = _pending_dir(root)
    if not directory.is_dir():
        return 0
    cutoff = now if now is not None else time.time()
    removed = 0
    for path in directory.glob("*.json"):
        try:
            created = float(json.loads(path.read_text(encoding="utf-8")).get("created_at", 0))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            _unlink_quiet(path)  # 损坏文件一并清掉
            removed += 1
            continue
        if cutoff - created > ttl_seconds:
            _unlink_quiet(path)
            removed += 1
    return removed


def _pending_dir(root: str | Path) -> Path:
    return Path(root) / _DIR_NAME


def _record_path(root: str | Path, token: str) -> Path | None:
    if not token or not _TOKEN_RE.match(str(token)):
        return None  # 非法 token(含路径穿越)直接拒绝
    return _pending_dir(root) / f"{token}.json"


def _write_record(path: Path, record: PendingPersona) -> None:
    payload = {
        "token": record.token,
        "owner_provider": record.owner_provider,
        "owner_kind": record.owner_kind,
        "owner_id": record.owner_id,
        "target": record.target,
        "content": record.content,
        "created_at": record.created_at,
    }
    tmp = path.parent / f"{path.stem}.tmp.{uuid.uuid4().hex}"
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)  # 原子落盘,避免读到写一半的半截文件


def _record_from_dict(data: object) -> PendingPersona | None:
    if not isinstance(data, dict):
        return None
    try:
        return PendingPersona(
            token=str(data["token"]),
            owner_provider=str(data["owner_provider"]),
            owner_kind=str(data["owner_kind"]),
            owner_id=str(data["owner_id"]),
            target=str(data["target"]),
            content=str(data["content"]),
            created_at=float(data["created_at"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


__all__ = ["PendingPersona", "add", "load", "pop", "purge_expired"]
