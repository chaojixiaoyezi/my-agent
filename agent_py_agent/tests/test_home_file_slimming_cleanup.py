"""一次性清理必须先证明保护边界，所有数据均在pytest临时目录。"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/home_file_slimming_cleanup.py"
SPEC = importlib.util.spec_from_file_location("home_slimming", SCRIPT)
cleanup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cleanup)
DAY = 86400
NOW = time.time()
TODAY = time.strftime("%Y-%m-%d", time.localtime(NOW))
OLD_DATE = "2020-01-01"
OWNER = Path("owners/local/main")
CATEGORIES = (
    "dangling_orphan_agent_projections",
    "task_idle_tmp_and_empty_dirs",
    "stale_test_workspaces",
    "old_release_switch_backups",
    "archive_before_20261019_scratch_only",
)


def write(root, relative, text="scratch"):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def age_tree(path, days=10):
    paths = list(path.rglob("*")) if path.is_dir() else []
    for p in [*reversed(paths), path]:
        if not p.is_symlink():
            os.utime(p, (NOW - days * DAY, NOW - days * DAY))


def snapshot(root):
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and not p.is_symlink()
    }


@pytest.fixture
def home(tmp_path):
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture
def baggage(home):
    orphan = OWNER / "agents/orphan"
    write(
        home,
        orphan / "workspace_locator.json",
        json.dumps({"workspace_root": f"/var/folders/no-such-slimming-{os.getpid()}/gone"}),
    )
    write(home, orphan / "state.json", "{}")
    tmp = OWNER / "tasks" / OLD_DATE / "old/tmp"
    write(home, tmp / "pytest/a.pyc", "12345")
    (home / tmp / "empty/sub").mkdir(parents=True)
    workspace = OWNER / "workspace/runtime/workspaces/test-ws-old"
    write(home, workspace / "cache.bin", "1234567")
    archive = Path("archive-before-20261019/tasks") / OLD_DATE / "task/tmp"
    write(home, archive / "cache.bin", "123456789")
    for i in range(4):
        base = Path(f"releases/switch-{i}")
        write(home, base / "pre-switch-conversations.tar.gz", "tar")
        write(home, base / "runtime-before.db", "db")
        write(home, base / "runtime-before.db-wal", "wal")
        write(home, base / "RESULT.md", "保留报告")
        age_tree(home / base, 10 - i)
    for p in (orphan, tmp, workspace, archive):
        age_tree(home / p)
    return home


def test_five_categories_plan_exact_files_then_apply(baggage):
    before = snapshot(baggage)
    manifest = cleanup.run_cleanup(baggage, now=NOW)
    assert snapshot(baggage) == before
    expected = [2, 1, 1, 6, 1]
    assert [manifest["categories"][k]["planned_files"] for k in CATEGORIES] == expected
    result = cleanup.run_cleanup(baggage, apply=True, now=NOW, approved_manifest=manifest)
    assert [result["categories"][k]["deleted_files"] for k in CATEGORIES] == expected
    for category in CATEGORIES:
        row = result["categories"][category]
        assert row["deleted_bytes"] == row["planned_bytes"]
    assert not (baggage / OWNER / "agents/orphan").exists()
    assert (baggage / OWNER / "tasks" / OLD_DATE / "old/tmp").is_dir()
    assert not (baggage / OWNER / "tasks" / OLD_DATE / "old/tmp/empty").exists()
    assert (baggage / "releases/switch-3/runtime-before.db").is_file()
    assert (baggage / "releases/switch-2/pre-switch-conversations.tar.gz").is_file()
    assert (baggage / "releases/switch-0/RESULT.md").read_text() == "保留报告"
    assert all(
        row["deleted_files"] == 0
        for row in cleanup.run_cleanup(baggage, apply=True, now=NOW)["categories"].values()
    )


@pytest.mark.parametrize(
    "protected",
    [
        "output/video.mp4",
        "inputs/source.bin",
        "src/main.py",
        "RESULT.md",
        "FINAL_REPORT.md",
        "memories.jsonl",
        "AGENTS.md",
        "SOUL.md",
        "USER.md",
        "memory/long_term/a.jsonl",
        "skills/custom/SKILL.md",
        "conversations/messages/a.jsonl",
    ],
)
def test_protected_data_inside_scratch_is_untouched(home, protected):
    base = OWNER / "tasks" / OLD_DATE / "task/tmp"
    keep = write(home, base / protected, "唯一正式内容")
    junk = write(home, base / "junk.pyc")
    age_tree(home / base)
    before = keep.read_bytes()
    cleanup.run_cleanup(home, now=NOW)
    assert junk.exists() and keep.read_bytes() == before
    cleanup.run_cleanup(home, apply=True, now=NOW)
    assert not junk.exists() and keep.read_bytes() == before


@pytest.mark.parametrize("kind", ["today", "recent-file", "recent-empty-dir", "outside-tmp"])
def test_active_or_non_tmp_tasks_are_preserved(home, kind):
    date = TODAY if kind == "today" else OLD_DATE
    base = OWNER / "tasks" / date / "task"
    path = write(home, base / ("output/result.bin" if kind == "outside-tmp" else "tmp/a.pyc"))
    age_tree(home / base)
    if kind == "recent-file":
        os.utime(path, (NOW, NOW))
    if kind == "recent-empty-dir":
        (home / base / "tmp/recent").mkdir()
    before = snapshot(home)
    cleanup.run_cleanup(home, apply=True, now=NOW)
    assert snapshot(home) == before


@pytest.mark.parametrize(
    "reason",
    [
        "exists",
        "inside-home",
        "missing-locator",
        "malformed",
        "non-temp",
        "recent",
        "deliverable",
        "output-dir",
        "dangling-target-link",
    ],
)
def test_agent_requires_all_orphan_conditions(home, tmp_path, reason):
    base = OWNER / "agents/keep"
    target = tmp_path / "existing"
    target.mkdir()
    root = f"/tmp/no-such-slimming-{os.getpid()}-x"
    if reason == "exists":
        root = str(target)
    if reason == "inside-home":
        root = str(home / "missing")
    if reason == "non-temp":
        root = "/unknown/missing"
    if reason == "dangling-target-link":
        link = tmp_path / "target-link"
        link.symlink_to(tmp_path / "missing")
        root = str(link)
    write(home, base / "state.json", "{}")
    if reason != "missing-locator":
        write(
            home,
            base / "workspace_locator.json",
            "{" if reason == "malformed" else json.dumps({"workspace_root": root}),
        )
    if reason == "deliverable":
        write(home, base / "RESULT.md")
    if reason == "output-dir":
        (home / base / "output").mkdir()
    age_tree(home / base, 0 if reason == "recent" else 10)
    before = snapshot(home)
    cleanup.run_cleanup(home, apply=True, now=NOW)
    assert (home / base).is_dir() and snapshot(home) == before


@pytest.mark.parametrize("reason", ["canonical", "config", "symlink-ref", "recent", "unknown"])
def test_workspace_guards(home, reason):
    name = (
        "main-a4e68bb5ead1"
        if reason == "canonical"
        else "customer"
        if reason == "unknown"
        else "test-ws-keep"
    )
    base = OWNER / "workspace/runtime/workspaces" / name
    write(home, base / "cache.bin")
    age_tree(home / base, 0 if reason == "recent" else 10)
    if reason == "config":
        write(home, "config/desktop.yaml", f"workspace_id: {name}\n")
    if reason == "symlink-ref":
        (home / "current-workspace").symlink_to(home / base)
    before = snapshot(home)
    cleanup.run_cleanup(home, apply=True, now=NOW)
    assert snapshot(home) == before


def test_archive_only_scratch_not_reports_or_messages(home):
    root = Path("archive-before-20261019")
    junk = write(home, root / "tasks/d/task/work/x.cache")
    cache = write(home, root / "misc/__pycache__/x.pyc")
    for path in (
        "tasks/d/task/output/film.mp4",
        "tasks/d/task/work/RESULT.md",
        "conversations/messages/thread.jsonl",
        "memories.jsonl",
        "AGENTS.md",
    ):
        write(home, root / path, "正式")
    age_tree(home / root)
    keep = {p: b for p, b in snapshot(home).items() if not p.endswith(("x.cache", "x.pyc"))}
    cleanup.run_cleanup(home, apply=True, now=NOW)
    assert not junk.exists() and not cache.exists()
    assert snapshot(home) == keep


def test_symlink_parent_and_hardlink_metadata_are_never_followed(home, tmp_path, monkeypatch):
    external = tmp_path / "outside"
    write(external, "a.pyc", "外部")
    tmp = home / OWNER / "tasks" / OLD_DATE / "task/tmp"
    tmp.parent.mkdir(parents=True)
    tmp.symlink_to(external, target_is_directory=True)
    other = write(home, OWNER / "tasks" / OLD_DATE / "other/tmp/a.pyc")
    linked = write(home, other.with_name("linked").relative_to(home), "多链接内容")
    age_tree(other.parent)
    original = cleanup.os.stat

    def multiple_links(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if str(path) == "linked":
            values = list(info)
            values[3] = 2
            return os.stat_result(values)
        return info

    monkeypatch.setattr(cleanup.os, "stat", multiple_links)
    cleanup.run_cleanup(home, apply=True, now=NOW)
    assert (external / "a.pyc").read_text() == "外部"
    assert linked.exists() and not other.exists()


def test_apply_does_not_expand_approved_plan_or_accept_changed_files(baggage):
    approved = cleanup.run_cleanup(baggage, now=NOW)
    changed = baggage / OWNER / "tasks" / OLD_DATE / "old/tmp/pytest/a.pyc"
    changed.write_text("新内容")
    extra = write(baggage, OWNER / "tasks" / OLD_DATE / "extra/tmp/a.pyc")
    age_tree(extra.parent)
    cleanup.run_cleanup(baggage, apply=True, now=NOW, approved_manifest=approved)
    assert changed.read_text() == "新内容" and extra.exists()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"keep_release_backups": 1},
        {"min_task_tmp_age_days": -1},
        {"min_task_tmp_age_days": float("nan")},
    ],
)
def test_unsafe_policy_is_rejected(home, kwargs):
    with pytest.raises(ValueError):
        cleanup.run_cleanup(home, **kwargs)


def test_cli_manifest_and_dry_run_apply_are_auditable(baggage, tmp_path):
    dry = tmp_path / "dry.json"
    applied = tmp_path / "applied.json"
    before = snapshot(baggage)
    for flag, output in (("--dry-run", dry), ("--apply", applied)):
        args = [
            sys.executable,
            "-B",
            str(SCRIPT),
            "--home-dir",
            str(baggage),
            flag,
            "--manifest-out",
            str(output),
        ]
        if flag == "--apply":
            args += ["--approved-manifest", str(dry)]
        result = subprocess.run(args, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        manifest = json.loads(output.read_text())
        assert manifest["mode"] == flag[2:]
        if flag == "--dry-run":
            assert snapshot(baggage) == before
    assert any(row["deleted_files"] for row in manifest["categories"].values())


@pytest.mark.parametrize("change", ["workspace-config", "orphan-target"])
def test_eligibility_rechecked_after_plan_before_delete(home, change):
    if change == "workspace-config":
        base = OWNER / "workspace/runtime/workspaces/test-ws-racing"
        victim = write(home, base / "cache.bin")
    else:
        base = OWNER / "agents/racing"
        target = home.parent / "late-target"
        write(home, base / "workspace_locator.json", json.dumps({"workspace_root": str(target)}))
        victim = write(home, base / "state.json", "{}")
    age_tree(home / base)
    touched = []

    def before_delete(_manifest):
        if not touched:
            touched.append(True)
            if change == "workspace-config":
                write(home, "config/desktop.yaml", "workspace_id: test-ws-racing\n")
            else:
                target.mkdir()

    cleanup.run_cleanup(home, apply=True, now=NOW, checkpoint=before_delete)
    assert victim.exists(), "预演后的配置引用或目标复活必须撤销删除资格"


def test_unreadable_configuration_fails_closed_for_workspaces(home):
    victim = write(home, OWNER / "workspace/runtime/workspaces/test-ws-old/cache.bin")
    age_tree(victim.parent)
    path = home / "config/desktop.yaml"
    path.parent.mkdir()
    path.symlink_to(home / "missing")
    cleanup.run_cleanup(home, apply=True, now=NOW)
    assert victim.exists()


def test_configuration_parent_symlink_fails_closed(home, tmp_path):
    victim = write(home, OWNER / "workspace/runtime/workspaces/test-ws-old/cache.bin")
    age_tree(victim.parent)
    config = tmp_path / "external-config"
    config.mkdir()
    (home / "config").symlink_to(config)
    cleanup.run_cleanup(home, apply=True, now=NOW)
    assert victim.exists()


def test_backup_ranking_is_revalidated_after_plan(baggage, tmp_path):
    moved = []

    def after_plan(_manifest):
        if not moved:
            moved.append(True)
            for name in ("switch-3", "switch-2"):
                (baggage / "releases" / name).rename(tmp_path / name)

    report = cleanup.run_cleanup(baggage, apply=True, now=NOW, checkpoint=after_plan)
    assert report["categories"]["old_release_switch_backups"]["deleted_files"] == 0
    for name in ("switch-1", "switch-0"):
        assert (baggage / "releases" / name / "runtime-before.db").exists()
        assert (baggage / "releases" / name / "pre-switch-conversations.tar.gz").exists()


def test_cli_rejects_existing_manifest_and_in_home_manifest(home, tmp_path):
    for destination in (write(tmp_path, "keep.json", "原件"), home / "new.json"):
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(SCRIPT),
                "--home-dir",
                str(home),
                "--manifest-out",
                str(destination),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2
    assert (tmp_path / "keep.json").read_text() == "原件"
