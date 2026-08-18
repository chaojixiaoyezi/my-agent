#!/usr/bin/env python3
"""#233 第 5 步：525 遗留 active 任务隔离迁移 CLI。

用法:
  python3 -m agent_py_agent.scripts.migrate_legacy_wake_tasks --owners-dir <dir> [--dry-run]
  python3 -m agent_py_agent.scripts.migrate_legacy_wake_tasks --owners-dir <dir> --revert --version v1-<date>
  python3 -m agent_py_agent.scripts.migrate_legacy_wake_tasks --owners-dir <dir> --show

只写隔离台账 wake_legacy_migrations（不批量删不标完成）；--dry-run 零写；
--revert 按 version 回滚台账（tasks 不动）。输出 before/after JSON（§7 证据）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root

from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import runtime_db_path
from agent_py_agent.agent.wake_legacy_migration import (
    _owner_homes_iter,
    _owner_id,
    revert_legacy_migration,
    run_legacy_migration,
)


def _default_version() -> str:
    import datetime

    return f"v1-{datetime.date.today().isoformat()}"


def _show_counts(owners_dir: Path) -> dict[str, object]:
    """只读快照：每 owner active 任务数 + 已隔离台账数（迁移前证据）。"""
    owners = 0
    active = 0
    isolated = 0
    by_state: dict[str, int] = {}
    for identity, home in _owner_homes_iter(owners_dir):
        home = Path(home)
        db_path = runtime_db_path(home)
        if not db_path.is_file():
            continue
        try:
            repo = RuntimeRepository(db_path)
        except Exception:  # noqa: BLE001
            continue
        owners += 1
        owner_id = _owner_id(identity)
        try:
            active += int(repo.count_active_tasks(owner_id) or 0)
            states = repo.legacy_migration_counts_by_state()
            for state, count in states.items():
                by_state[str(state)] = int(by_state.get(str(state)) or 0) + int(count or 0)
            isolated += int(sum(states.values()) or 0)
        except Exception:  # noqa: BLE001
            continue
    return {"owners": owners, "active_tasks": active, "isolated": isolated, "by_state": by_state}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owners-dir", required=True, help="owners 根目录（含 providers/）")
    parser.add_argument("--dry-run", action="store_true", help="只报告不写")
    parser.add_argument("--revert", action="store_true", help="回滚指定 version 台账")
    parser.add_argument("--version", default="", help="migration_version（默认 v1-<today>）")
    parser.add_argument("--show", action="store_true", help="只读快照（迁移前证据）")
    args = parser.parse_args()

    owners_dir = Path(args.owners_dir).expanduser()
    if not owners_dir.is_dir():
        print(json.dumps({"error": f"owners-dir not found: {owners_dir}"}, ensure_ascii=False))
        return 2

    version = args.version or _default_version()

    if args.show:
        print(json.dumps(_show_counts(owners_dir), ensure_ascii=False, indent=2))
        return 0

    if args.revert:
        total = 0
        for _identity, home in _owner_homes_iter(owners_dir):
            home = Path(home)
            db_path = runtime_db_path(home)
            if not db_path.is_file():
                continue
            try:
                repo = RuntimeRepository(db_path)
            except Exception:  # noqa: BLE001
                continue
            total += revert_legacy_migration(repo, migration_version=version)
        print(json.dumps({"reverted": total, "version": version}, ensure_ascii=False))
        return 0

    summary = run_legacy_migration(
        owners_dir, migration_version=version, dry_run=args.dry_run,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
