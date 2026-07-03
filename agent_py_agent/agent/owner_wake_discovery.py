"""磁盘级 owner 唤醒发现:治「盯守转非阻塞后睡死叫不醒」的登记表易失性。

真机实锤(接手文档 §1):scoped owner 的到点唤醒(progress policy)/待处理唤醒信号
(wake signal)都持久化在该 owner 自己的会话存储里,但后台主代理循环只 tick「进程内
最近活跃 owner 登记表」里的 owner——登记表是易失的(网关重启清零、LRU 逐出),且只有
新入站请求才补记。长盯守的非阻塞挂起期恰恰没有新请求:网关一重启,policy 到点后
永远无人消费,盯守中途停摆(u-mix-6 现场:policy enabled、next_due_at 过期 2 万秒,
spool 攒 445 个候选没人读)。

本模块把「哪些 owner 有待消费的调度事实」改为从磁盘直接发现(纯结构化信号:
enabled 的 policy 文件存在 / wake_queue 待处理信号文件存在),供后台循环周期性
把这些 owner 种回活跃登记表——重启/逐出后自愈,登记表退化为热路径加速。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

# owner home 内会话存储根(conversations 目录)的已知形态。网关按 workspace root 跑时
# 是 workspace/runtime/workspaces/<slug>/conversations;直连/历史部署可能落在
# conversations 或 data/conversations。全部是固定深度的 glob,不做递归扫描。
_STORE_ROOT_PATTERNS = (
    "workspace/runtime/workspaces/*/conversations",
    "conversations",
    "data/conversations",
)
_PROVIDER_BUCKET_KINDS = {"users": "user", "groups": "group"}


def discover_wake_pending_owners(owners_dir: str | Path, *, limit: int = 64) -> list[Any]:
    """扫 owners/providers/<provider>/{users,groups}/<id> 找「有待消费调度事实」的 owner。

    事实=任一会话存储根下:enabled 的 progress policy,或 wake_queue/{urgent,normal}
    里的待处理信号文件。返回 OwnerIdentity 列表(最多 limit 个)。base(local/main)
    不在此列——它恒被后台循环 tick,无需发现。
    """
    providers_root = Path(owners_dir) / "providers"
    if not providers_root.is_dir():
        return []
    found: list[Any] = []
    for provider, owner_kind, owner_home in _candidate_owner_homes(providers_root):
        if len(found) >= max(1, limit):
            break
        if _owner_has_wake_pending_facts(owner_home):
            found.append(_identity(provider, owner_kind, owner_home.name))
    return found


def _candidate_owner_homes(providers_root: Path):
    for provider_dir, owner_kind, bucket_dir in _provider_buckets(providers_root):
        for owner_home in _dirs_of(bucket_dir):
            yield provider_dir.name, owner_kind, owner_home


def _provider_buckets(providers_root: Path):
    for provider_dir in _dirs_of(providers_root):
        for bucket, owner_kind in _PROVIDER_BUCKET_KINDS.items():
            yield provider_dir, owner_kind, provider_dir / bucket


def _dirs_of(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return [path for path in sorted(root.iterdir()) if path.is_dir()]


def seed_registry_from_disk(registry: Any, owners_dir: str | Path, *, limit: int = 64) -> int:
    """把磁盘发现的待唤醒 owner 种进活跃登记表;返回种入数。best-effort,绝不外抛。"""
    try:
        owners = discover_wake_pending_owners(owners_dir, limit=limit)
        for owner in owners:
            registry.record(owner)
        return len(owners)
    except Exception:
        _LOGGER.warning("owner wake discovery failed (owners_dir=%s)", owners_dir, exc_info=True)
        return 0


def _owner_has_wake_pending_facts(owner_home: Path) -> bool:
    return any(
        _has_pending_wake_signal(store_root) or _has_enabled_progress_policy(store_root)
        for store_root in _store_roots(owner_home)
    )


def _store_roots(owner_home: Path):
    for pattern in _STORE_ROOT_PATTERNS:
        yield from owner_home.glob(pattern)


def _has_pending_wake_signal(store_root: Path) -> bool:
    # wake_queue/{urgent,normal} 里的文件即待处理(处理过的会被挪进 handled/)。
    for kind in ("urgent", "normal"):
        if any((store_root / "wake_queue" / kind).glob("*.json")):
            return True
    return False


def _has_enabled_progress_policy(store_root: Path) -> bool:
    for path in (store_root / "progress_policies").glob("*.json"):
        if _policy_enabled(path):
            return True
    return False


def _policy_enabled(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return False
    return isinstance(payload, dict) and bool(payload.get("enabled", True))


def _identity(provider: str, owner_kind: str, owner_id: str) -> Any:
    from .user_space.owner_resolver import OwnerIdentity

    if owner_kind == "group":
        return OwnerIdentity.provider_group(provider, owner_id)
    return OwnerIdentity.provider_user(provider, owner_id)


__all__ = ["discover_wake_pending_owners", "seed_registry_from_disk"]
