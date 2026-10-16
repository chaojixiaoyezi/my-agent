from __future__ import annotations

"""E1c：上下文包快照与工具清单接进 retention 的清理行为。

- 范围：memory_archive/snapshots/context_bundles/<日期>/ 下的每请求快照（.json/.md），latest 副本永远保留；
- 保留期：30 天（代码常量），按 mtime 判断，过期快照走可恢复回收站（trash_file + tombstone）；
- 工具清单：超过 30 天且没有被任何剩余快照引用（读摘要里的 manifest_sha256 + manifest_ref）才回收；
  复用（touch）过的清单 mtime 刷新，不会被当成"很久以前写的"删掉；
- 复用现有机制：legal hold 直接生效；apply 前按指纹/mtime 复核；plan 只读、与 apply 结果一致；
- 失败方向：任一份剩余快照读不懂时不动任何清单（关闭式），并报结构化错误。
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.retention import MemoryRetentionService
from agent_py_agent.agent.user_space.context_bundle_tool_manifest import (
    build_tool_manifest_summary,
)
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

NOW = datetime(2026, 8, 4, tzinfo=timezone.utc)
DAY = 86_400
OLD = NOW.timestamp() - 31 * DAY
FRESH = NOW.timestamp() - 29 * DAY


def _runtime(tmp_path: Path, name: str = "home"):
    home = ensure_my_agent_home(tmp_path / name)
    service = MemoryRetentionService(
        home_paths=home,
        candidates=CandidateService(home.owner_memory_candidates_jsonl),
    )
    return home, service


def _snapshots_root(home) -> Path:
    return Path(home.owner_home_dir) / "memory_archive" / "snapshots"


def _write_snapshot(home, where, mtime: float, manifest=None):
    """写一份像真快照的 .json/.md；where=(日期目录, 文件名前缀)，manifest=(sha, ref) 或 None。"""

    date_dir, stem = where
    sha, ref = manifest or ("", "")
    base = _snapshots_root(home) / "context_bundles" / date_dir
    base.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "main_context_bundle.v1",
        "tool_manifest": {"manifest_sha256": sha, "manifest_ref": ref},
    }
    json_path = base / f"{stem}.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    markdown_path = base / f"{stem}.md"
    markdown_path.write_text(f"# {stem}\n", encoding="utf-8")
    os.utime(json_path, (mtime, mtime))
    os.utime(markdown_path, (mtime, mtime))
    return json_path, markdown_path


def _write_manifest(home, sha: str, mtime: float) -> Path:
    directory = _snapshots_root(home) / "tool_manifests"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{sha}.json"
    path.write_text("{}", encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def _set_policy(home, **updates: object) -> None:
    payload = json.loads(home.owner_retention_json.read_text(encoding="utf-8"))
    payload.update(updates)
    home.owner_retention_json.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )


def _trash_containers(home, category: str) -> list[Path]:
    root = Path(home.owner_trash_dir) / "retention" / category
    if not root.exists():
        return []
    return sorted(path for path in root.glob("*") if path.is_dir())


def _trash_payload_for(home, category: str, source_path: Path) -> Path | None:
    for container in _trash_containers(home, category):
        tombstone = json.loads((container / "tombstone.json").read_text(encoding="utf-8"))
        if tombstone["source_path"] == str(source_path):
            return Path(tombstone["payload_path"])
    return None


def test_old_snapshots_move_to_trash_recent_and_latest_stay(tmp_path: Path):
    home, service = _runtime(tmp_path)
    old_json, old_md = _write_snapshot(home, ("2026-01-01", "req-old"), OLD)
    recent_json, recent_md = _write_snapshot(home, ("2026-01-02", "req-fresh"), FRESH)
    latest_json, latest_md = _write_snapshot(home, ("2026-01-03", "latest_context_bundle"), OLD)
    old_json_text = old_json.read_text(encoding="utf-8")

    plan = service.plan(now=NOW)

    snapshot_actions = [a for a in plan.actions if a.category == "context_bundle_snapshot"]
    assert {a.path for a in snapshot_actions} == {old_json, old_md}
    assert all(a.operation == "trash_file" for a in snapshot_actions)
    assert old_json.exists() and old_md.exists(), "预演（plan）必须只读"

    applied = service.apply(now=NOW)

    assert applied.applied is True and applied.ok is True
    assert {a.action_id for a in plan.actions} == {a.action_id for a in applied.actions}
    assert {a.status for a in applied.actions} == {"trashed"}
    assert not old_json.exists() and not old_md.exists()
    assert recent_json.exists() and recent_md.exists()
    assert latest_json.exists() and latest_md.exists(), "latest 副本永远保留"
    payload = _trash_payload_for(home, "context_bundle_snapshot", old_json)
    assert payload is not None and payload.read_text(encoding="utf-8") == old_json_text
    for container in _trash_containers(home, "context_bundle_snapshot"):
        tombstone = json.loads((container / "tombstone.json").read_text(encoding="utf-8"))
        assert tombstone["status"] == "trashed"


def test_manifest_referenced_by_remaining_snapshot_survives_unreferenced_goes(tmp_path: Path):
    home, service = _runtime(tmp_path)
    kept_sha, orphan_sha, fresh_sha = "a" * 64, "b" * 64, "c" * 64
    _write_snapshot(home, ("2026-07-30", "req-kept"), FRESH, (kept_sha, "/kept"))
    _write_snapshot(home, ("2026-01-01", "req-old"), OLD, (orphan_sha, "/orphan"))
    kept_manifest = _write_manifest(home, kept_sha, OLD)
    orphan_manifest = _write_manifest(home, orphan_sha, OLD)
    fresh_manifest = _write_manifest(home, fresh_sha, FRESH)

    plan = service.plan(now=NOW)
    planned = {a.path for a in plan.actions if a.category == "tool_manifest"}
    assert planned == {orphan_manifest}

    applied = service.apply(now=NOW)

    assert applied.ok is True
    assert kept_manifest.exists(), "被保留快照引用的旧清单不能删"
    assert not orphan_manifest.exists(), "没有任何剩余快照引用的旧清单要回收"
    assert fresh_manifest.exists(), "没超过 30 天的清单不动"
    statuses = {a.path: a.status for a in applied.actions}
    assert statuses[orphan_manifest] == "trashed"


def test_reused_manifest_is_touched_and_survives_cleanup(tmp_path: Path):
    home, service = _runtime(tmp_path)
    payload = {"schema_name": "test", "visible_tools": ["read_file"], "tool_load_errors": []}
    first = build_tool_manifest_summary(payload, home, save=True)
    manifest = Path(first["manifest_ref"])
    assert manifest.is_file()
    original = manifest.read_bytes()
    os.utime(manifest, (OLD, OLD))

    rebuilt = build_tool_manifest_summary(payload, home, save=True)

    assert rebuilt["manifest_ref"] == str(manifest)
    assert manifest.read_bytes() == original, "touch 只动时间，不动内容"
    assert manifest.stat().st_mtime > OLD, "复用清单要把 mtime 刷新到现在"

    orphan = _write_manifest(home, "d" * 64, OLD)
    applied = service.apply(now=NOW)

    assert applied.ok is True
    assert manifest.exists(), "刚被复用的清单不能被当成很久以前写的删掉"
    assert not orphan.exists()


def test_legal_hold_keeps_snapshots_and_manifests(tmp_path: Path):
    home, service = _runtime(tmp_path)
    _set_policy(home, legal_hold=True)
    old_json, old_md = _write_snapshot(home, ("2026-01-01", "req-old"), OLD)
    orphan = _write_manifest(home, "e" * 64, OLD)

    plan = service.plan(now=NOW)
    applied = service.apply(now=NOW)

    assert plan.legal_hold is True and plan.actions == ()
    assert applied.applied is False and applied.legal_hold is True
    assert old_json.exists() and old_md.exists() and orphan.exists()
    assert _trash_containers(home, "context_bundle_snapshot") == []
    assert _trash_containers(home, "tool_manifest") == []


def test_two_owners_clean_their_own_archives_only(tmp_path: Path):
    home_a, service_a = _runtime(tmp_path, "home-a")
    home_b, service_b = _runtime(tmp_path, "home-b")
    a_json, _a_md = _write_snapshot(home_a, ("2026-01-01", "req-a"), OLD)
    b_json, _b_md = _write_snapshot(home_b, ("2026-01-01", "req-b"), OLD)

    applied_a = service_a.apply(now=NOW)

    assert applied_a.ok is True
    assert not a_json.exists()
    assert b_json.exists(), "A 的清理不能碰 B 的归档"
    assert _trash_containers(home_a, "context_bundle_snapshot") != []
    assert _trash_containers(home_b, "context_bundle_snapshot") == []

    service_b.apply(now=NOW)

    assert not b_json.exists()
    assert len(_trash_containers(home_a, "context_bundle_snapshot")) == 2
    assert len(_trash_containers(home_b, "context_bundle_snapshot")) == 2


def test_apply_skips_snapshot_changed_after_plan(tmp_path: Path, monkeypatch):
    from agent_py_agent.agent.memory_store import retention as retention_module

    home, service = _runtime(tmp_path)
    changed_json, _changed_md = _write_snapshot(home, ("2026-01-01", "req-changed"), OLD)
    bumped_json, _bumped_md = _write_snapshot(home, ("2026-01-02", "req-bumped"), OLD)

    plan = service.plan(now=NOW)
    assert {changed_json, bumped_json} <= {a.path for a in plan.actions}

    original = retention_module.execute_retention_plan

    def _change_then_execute(**kwargs):
        # 在 apply 内部重规划之后、真正执行之前动手：一个改内容（mtime 仍是旧的），一个刷新 mtime。
        changed_json.write_text(changed_json.read_text(encoding="utf-8") + " ", encoding="utf-8")
        os.utime(changed_json, (OLD, OLD))
        os.utime(bumped_json, (NOW.timestamp(), NOW.timestamp()))
        return original(**kwargs)

    monkeypatch.setattr(retention_module, "execute_retention_plan", _change_then_execute)
    applied = service.apply(now=NOW)

    assert applied.applied is True
    statuses = {a.path: a.status for a in applied.actions}
    assert statuses[changed_json] == "state_changed"
    assert statuses[bumped_json] == "state_changed"
    assert "MEMORY_RETENTION_STATE_CHANGED" in {e.error_code for e in applied.errors}
    assert changed_json.exists() and bumped_json.exists()
    assert _trash_payload_for(home, "context_bundle_snapshot", changed_json) is None
    assert _trash_payload_for(home, "context_bundle_snapshot", bumped_json) is None


def test_empty_date_directory_is_pruned_only_when_empty(tmp_path: Path):
    home, service = _runtime(tmp_path)
    gone_json, _gone_md = _write_snapshot(home, ("2026-01-01", "req-gone"), OLD)
    old_json, _old_md = _write_snapshot(home, ("2026-01-02", "req-old"), OLD)
    keep_json, keep_md = _write_snapshot(home, ("2026-01-02", "req-keep"), FRESH)

    applied = service.apply(now=NOW)

    assert applied.ok is True
    assert not gone_json.parent.exists(), "移走最后一个文件后，空的日期目录要删掉"
    assert not old_json.exists()
    assert keep_json.exists() and keep_md.exists()
    assert old_json.parent.exists(), "目录里还有保留文件时不能删"


def test_unreadable_kept_snapshot_blocks_manifest_cleanup(tmp_path: Path):
    home, service = _runtime(tmp_path)
    broken = _snapshots_root(home) / "context_bundles" / "2026-07-30" / "req-broken.json"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("{broken", encoding="utf-8")
    os.utime(broken, (FRESH, FRESH))
    orphan = _write_manifest(home, "f" * 64, OLD)

    plan = service.plan(now=NOW)

    assert "MEMORY_RETENTION_CONTEXT_BUNDLE_INVALID" in {e.error_code for e in plan.errors}
    assert plan.ok is False
    assert not any(a.category == "tool_manifest" for a in plan.actions)

    applied = service.apply(now=NOW)

    assert applied.applied is True
    assert orphan.exists(), "引用集合不完整时不动任何清单"


def test_changed_old_snapshot_keeps_its_manifest_during_apply(tmp_path: Path, monkeypatch):
    from agent_py_agent.agent.memory_store import retention as retention_module

    home, service = _runtime(tmp_path)
    sha = '1' * 64
    snapshot, _ = _write_snapshot(home, ('2026-01-01', 'changed'), OLD, (sha, '/old-ref'))
    manifest = _write_manifest(home, sha, OLD)
    original = retention_module.execute_retention_plan

    def change_then_execute(**kwargs):
        snapshot.write_text(snapshot.read_text() + ' ', encoding='utf-8')
        os.utime(snapshot, (OLD, OLD))
        return original(**kwargs)

    monkeypatch.setattr(retention_module, 'execute_retention_plan', change_then_execute)
    applied = service.apply(now=NOW)

    statuses = {action.path: action.status for action in applied.actions}
    assert statuses[snapshot] == 'state_changed'
    assert statuses[manifest] == 'state_changed'
    assert snapshot.exists() and manifest.exists()


def test_failed_snapshot_move_keeps_its_manifest(tmp_path: Path, monkeypatch):
    from agent_py_agent.agent.memory_store import retention_apply

    home, service = _runtime(tmp_path)
    sha = '2' * 64
    snapshot, _ = _write_snapshot(home, ('2026-01-01', 'move-fails'), OLD, (sha, '/old-ref'))
    manifest = _write_manifest(home, sha, OLD)
    original = retention_apply.shutil.move

    def move(source, destination, *args, **kwargs):
        if Path(source) == snapshot:
            raise OSError('synthetic snapshot move failure')
        return original(source, destination, *args, **kwargs)

    monkeypatch.setattr(retention_apply.shutil, 'move', move)
    applied = service.apply(now=NOW)

    statuses = {action.path: action.status for action in applied.actions}
    assert statuses[snapshot] == 'failed'
    assert statuses[manifest] == 'state_changed'
    assert snapshot.exists() and manifest.exists()


def test_snapshot_created_after_plan_protects_manifest(tmp_path: Path, monkeypatch):
    from agent_py_agent.agent.memory_store import retention as retention_module

    home, service = _runtime(tmp_path)
    sha = '3' * 64
    manifest = _write_manifest(home, sha, OLD)
    original = retention_module.execute_retention_plan

    def add_then_execute(**kwargs):
        _write_snapshot(home, ('2026-08-04', 'new'), NOW.timestamp(), (sha, '/new-ref'))
        return original(**kwargs)

    monkeypatch.setattr(retention_module, 'execute_retention_plan', add_then_execute)
    applied = service.apply(now=NOW)

    statuses = {action.path: action.status for action in applied.actions}
    assert statuses[manifest] == 'state_changed'
    assert manifest.exists()


def test_live_manifest_reference_scan_runs_once_per_apply(tmp_path: Path, monkeypatch):
    from agent_py_agent.agent.memory_store import retention_apply

    home, service = _runtime(tmp_path)
    _write_manifest(home, '4' * 64, OLD)
    _write_manifest(home, '5' * 64, OLD)
    calls = []
    original = retention_apply.current_tool_manifest_references

    def scan(home_paths):
        calls.append(home_paths)
        return original(home_paths)

    monkeypatch.setattr(retention_apply, 'current_tool_manifest_references', scan)
    applied = service.apply(now=NOW)

    assert applied.ok
    assert len(calls) == 1


def test_task_legal_hold_protects_snapshot_pair_and_manifest(tmp_path: Path):
    home, service = _runtime(tmp_path)
    _set_policy(home, legal_hold_task_ids=['held-task'])
    sha = '6' * 64
    snapshot, markdown = _write_snapshot(home, ('2026-01-01', 'held-request'), OLD, (sha, '/held-ref'))
    payload = json.loads(snapshot.read_text())
    payload['scope'] = {'task_id': 'held-task'}
    snapshot.write_text(json.dumps(payload), encoding='utf-8')
    os.utime(snapshot, (OLD, OLD))
    manifest = _write_manifest(home, sha, OLD)
    orphan = _write_manifest(home, '7' * 64, OLD)

    plan = service.plan(now=NOW)
    assert {snapshot, markdown, manifest}.isdisjoint({action.path for action in plan.actions})
    applied = service.apply(now=NOW)

    assert applied.ok
    assert snapshot.exists() and markdown.exists() and manifest.exists()
    assert not orphan.exists()
