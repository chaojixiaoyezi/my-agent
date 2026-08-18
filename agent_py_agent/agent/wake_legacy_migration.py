"""525 遗留 active 任务隔离迁移（#233 第 5 步）。

spec §6 第 5 步 / §8「不批量删不标完成，只迁移隔离 + 保留证据」：
- 对 tasks.status='active' 但**无 role='main' 权威 run** 且**无当前 wake policy**
  的遗留任务，写独立隔离台账 `wake_legacy_migrations`（ORPHANED + reason/
  last_seen/source/provenance/migration_version）。
- **绝不改 tasks.status**（保留 resume 读取语义）、**绝不批量删、绝不标完成**。
- 可逆：按 migration_version 删台账行即回滚；tasks 完全不动。
- 迁移前后计数落 metadata（key=`wake_legacy_migration:{version}:counts`）。

幂等：已隔离任务跳过（get_legacy_migration 命中）；重跑隔离 ~0。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .runtime_db.schema import runtime_db_path
from .user_space.owner_resolver import OwnerIdentity

_MIGRATION_VERSION = "v1"
_METADATA_KEY_PREFIX = "wake_legacy_migration"


def isolate_legacy_active_tasks(
    repo: object,
    *,
    owner_id: str,
    owner_home: Path,
    now: float | None = None,
    limit: int = 1000,
    migration_version: str = _MIGRATION_VERSION,
    dry_run: bool = False,
) -> dict[str, int]:
    """单 owner 隔离一趟。返回 {scanned, isolated, skipped}。dry_run 零写。

    判据（结构化）：
    - tasks.status='active'（list_active_tasks）
    - 无 role='main' 权威 run（main_agent_run_for_task None → 遗留无 run）
    - 无当前 wake policy（async + interactive 均无 → legacy_unbound）
    - 未在台账（get_legacy_migration None → 幂等）
    满足 → ORPHANED 台账行 + provenance（link path 或 runtime_task ref）。
    """
    now = time.time() if now is None else now
    counts = {"scanned": 0, "isolated": 0, "skipped": 0}
    try:
        active_tasks = repo.list_active_tasks(owner_id, limit=limit)
    except Exception:  # noqa: BLE001 单 owner 扫描异常不阻断编排
        counts["scanned"] = 0
        return counts
    for task_row in active_tasks:
        counts["scanned"] += 1
        task_id = str(task_row.get("task_id") or "")
        if not task_id:
            counts["skipped"] += 1
            continue
        try:
            if repo.get_legacy_migration(task_id) is not None:
                counts["skipped"] += 1  # 已隔离（幂等）
                continue
            if repo.main_agent_run_for_task(task_id) is not None:
                counts["skipped"] += 1  # 有权威 run → 非遗留
                continue
            if _has_any_current_policy(repo, owner_id):
                counts["skipped"] += 1  # 有 policy → 非 legacy_unbound
                continue
        except Exception:  # noqa: BLE001 单任务判定异常不阻断
            counts["skipped"] += 1
            continue
        if dry_run:
            counts["isolated"] += 1  # dry-run 只计数不写
            continue
        provenance_ref = _provenance_for_task(owner_home, task_id)
        try:
            repo.upsert_legacy_migration(
                task_id=task_id, owner_id=owner_id,
                isolation_state="ORPHANED",
                reason="legacy_unbound_no_run_no_policy",
                last_seen=now, source="gateway_conversation",
                provenance_ref=provenance_ref,
                migration_version=migration_version, now=now,
            )
            counts["isolated"] += 1
        except Exception:  # noqa: BLE001 单任务台账写失败不阻断
            counts["skipped"] += 1
    return counts


def run_legacy_migration(
    owners_dir: str | Path,
    *,
    migration_version: str = _MIGRATION_VERSION,
    now: float | None = None,
    dry_run: bool = False,
) -> dict[str, object]:
    """全 owner 迁移编排：逐 owner before/after + 计数落 metadata（dry_run 零写）。

    返回 {owners, before_active, after_isolated, by_state, dry_run} 结构化汇总
    （§7 迁移前后快照证据）。
    """
    now = time.time() if now is None else now
    owners_dir = Path(owners_dir)
    summary: dict[str, object] = {
        "owners": 0, "before_active": 0, "after_isolated": 0,
        "by_state": {}, "dry_run": bool(dry_run),
    }
    from .runtime_db.repository import RuntimeRepository

    for identity, home in _owner_homes_iter(owners_dir):
        home = Path(home)
        db_path = runtime_db_path(home)
        if not db_path.is_file():
            continue
        try:
            repo = RuntimeRepository(db_path)
        except Exception:  # noqa: BLE001 单 owner 库异常不阻断
            continue
        owner_id = _owner_id(identity)
        if not owner_id:
            continue
        summary["owners"] = int(summary["owners"]) + 1
        try:
            before = repo.count_active_tasks(owner_id)
        except Exception:  # noqa: BLE001
            before = 0
        summary["before_active"] = int(summary["before_active"]) + before
        counts = isolate_legacy_active_tasks(
            repo, owner_id=owner_id, owner_home=home, now=now,
            migration_version=migration_version, dry_run=dry_run,
        )
        if not dry_run:
            try:
                after = repo.legacy_migration_counts_by_state()
            except Exception:  # noqa: BLE001
                after = {}
            summary["after_isolated"] = int(summary["after_isolated"]) + int(
                counts.get("isolated") or 0
            )
            _merge_by_state(summary["by_state"], after)
            _ledger_counts(repo, migration_version, summary, before)
    return summary


def revert_legacy_migration(
    repo: object,
    *,
    migration_version: str = _MIGRATION_VERSION,
) -> int:
    """回滚一批隔离（可逆）：删该 version 台账行。tasks 完全不动。"""
    removed = 0
    try:
        with repo.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM wake_legacy_migrations WHERE migration_version = ?",
                (migration_version,),
            )
            removed = int(cur.rowcount or 0)
    except Exception:  # noqa: BLE001
        removed = 0
    return removed


# ------------------------------------------------------------- helpers
def _has_any_current_policy(repo, owner_id: str) -> bool:
    try:
        if repo.current_wake_policy(owner_id, "async") is not None:
            return True
        if repo.current_wake_policy(owner_id, "interactive") is not None:
            return True
    except Exception:  # noqa: BLE001 查 policy 失败保守视为有（不误隔离）
        return True
    return False


def _provenance_for_task(owner_home: Path, task_id: str) -> str:
    """provenance：优先 conversation link path（生命周期权威），否则 runtime_task ref。"""
    links_root = Path(owner_home) / "workspace" / "runtime" / "workspaces"
    if links_root.is_dir():
        try:
            for link in links_root.glob(f"*/conversations/tasks/{task_id}.json"):
                if link.is_file():
                    return str(link)
        except OSError:
            pass
    return f"runtime_task:{task_id}"


def _owner_id(identity: OwnerIdentity) -> str:
    """规范 owner_id（与 owner_resolver._owner_id 同源，含 local/main 特例）。"""
    if identity.provider == "local" and identity.owner_kind == "main":
        return "local/main"
    bucket = "users" if identity.owner_kind == "user" else "groups"
    return f"providers/{identity.provider}/{bucket}/{identity.owner_id}"


def _owner_homes_iter(owners_dir: Path):
    """遍历 owner 库（新式 providers/ + legacy owners/<provider>/<id>），产出 (OwnerIdentity, home)。

    与 gateway_loops._owner_homes_iter 同构——base/local（legacy local/main）也必须
    进迁移扫描（525 就在 base owner 库）。"""
    providers_root = owners_dir / "providers"
    if providers_root.is_dir():
        for provider_dir in sorted(providers_root.iterdir()):
            if not provider_dir.is_dir():
                continue
            for bucket, owner_kind in (("users", "user"), ("groups", "group")):
                bucket_dir = provider_dir / bucket
                if not bucket_dir.is_dir():
                    continue
                for owner_home in sorted(bucket_dir.iterdir()):
                    if not owner_home.is_dir():
                        continue
                    identity = (
                        OwnerIdentity.provider_user(provider_dir.name, owner_home.name)
                        if owner_kind == "user"
                        else OwnerIdentity.provider_group(provider_dir.name, owner_home.name)
                    )
                    yield identity, owner_home
    # Legacy/local 布局：owners/<provider>/<id>（local/main 主 owner 等）
    for provider_dir in sorted(owners_dir.iterdir()):
        if not provider_dir.is_dir() or provider_dir.name == "providers":
            continue
        for owner_home in sorted(provider_dir.iterdir()):
            if not owner_home.is_dir():
                continue
            identity = OwnerIdentity(provider=provider_dir.name, owner_kind="main", owner_id=owner_home.name)
            yield identity, owner_home


def _merge_by_state(acc: dict, by_state: dict) -> None:
    for state, count in by_state.items():
        acc[str(state)] = int(acc.get(str(state)) or 0) + int(count or 0)


def _ledger_counts(repo, migration_version: str, summary: dict, before: int) -> None:
    """迁移前后计数落 metadata（§7 快照证据）。"""
    try:
        payload = json.dumps(
            {
                "before_active": int(summary["before_active"]),
                "after_isolated": int(summary["after_isolated"]),
                "by_state": summary["by_state"],
                "version": migration_version,
            },
            ensure_ascii=False,
        )
        repo.set_metadata(f"{_METADATA_KEY_PREFIX}:{migration_version}:counts", payload)
    except Exception:  # noqa: BLE001 计数落账失败不阻断
        pass


__all__ = [
    "isolate_legacy_active_tasks",
    "run_legacy_migration",
    "revert_legacy_migration",
]
