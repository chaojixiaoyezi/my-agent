"""W13：真实展示归档写入、维护通道与读回的生命周期及隔离反例。"""
import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import display_archive as archive
from agent_py_agent.agent.conversation import display_archive_maintenance as maintenance
from agent_py_agent.agent.conversation.models import ConversationThread
from agent_py_agent.agent.conversation.store_layout import ConversationStorage
from agent_py_agent.agent.conversation.store_threads import ThreadStore
from agent_py_agent.agent.memory_store import retention_scan
from agent_py_agent.agent.memory_store.retention_models import MemoryRetentionPolicy
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_maintenance import run_storage_maintenance_slice
from agent_py_agent.agent.user_space.owner_quota import owner_logical_usage_bytes
from agent_py_agent.agent.user_space.storage_lanes import (
    StorageLaneBudget,
    StorageLaneRequest,
    owner_conversation_roots,
    resolve_storage_lane,
    validate_lane_receipt,
)

NOW = 2_000_000_000.0
DAY = 86400


@pytest.fixture(autouse=True)
def clean_domain_scans():
    """不让真实目录迭代器或 inode 游标跨用例泄漏。"""
    def reset():
        with maintenance._scan_cursor_lock:
            for scans, _, _ in maintenance._live_scans.values():
                scans.close()
            maintenance._live_scans.clear()
            maintenance._scan_cursors.clear()
            maintenance._scan_recent.clear()
            maintenance._scan_waiting.clear()
    reset()
    yield
    reset()


@pytest.fixture
def owners(tmp_path):
    """每个 owner 同时具有直接会话根和运行时工作区会话根。"""
    result = []
    for name in ("owner-a", "owner-b"):
        home = home_paths(tmp_path / name)
        roots = (
            home.owner_home_dir / "conversations",
            home.owner_workspace_dir / "runtime" / "workspaces" / "scope-test" / "conversations",
        )
        for root in roots:
            (root / "threads").mkdir(parents=True)
        home.owner_retention_json.write_text(json.dumps(MemoryRetentionPolicy().to_dict()))
        assert owner_conversation_roots(home) == roots
        result.append((home, roots))
    return result


def store(root):
    return SimpleNamespace(storage=SimpleNamespace(root=root), threads=SimpleNamespace(load=lambda _: True))


def write_archive(root, *, age=0, text="展示原文" * 4000):
    reference = archive.archive_display_rows(
        SimpleNamespace(conversation_store=store(root)), thread_id="thread-w13", rows=[{"text": text}],
    )
    manifest = root / "display_archives" / reference["archive_id"] / "manifest.json"
    os.utime(manifest, (NOW - age * DAY,) * 2)
    return reference


def request(home, cursor=None, *, now=NOW, maximum=128, commit=None, legal_hold=False):
    return StorageLaneRequest(
        kind="display_archives", home=home, now=now, cursor={} if cursor is None else cursor,
        budget=StorageLaneBudget(maximum, None, None), legal_hold=legal_hold,
        coordinator=commit is not None, pressure=None, commit=commit,
    )


def drain(home, *, now=NOW, maximum=128, commit=None):
    cursor, receipts = {}, []
    for _ in range(500):
        receipt = maintenance.maintain_display_archives_lane(
            request(home, cursor, now=now, maximum=maximum, commit=commit),
        )
        assert validate_lane_receipt(receipt) == receipt
        receipts.append(receipt)
        if receipt["complete"]:
            assert receipt["cursor"] == {}
            return receipts
        cursor = receipt["cursor"]
    pytest.fail("归档通道未在有界轮次内完成")


def contents(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_lane_roundtrip_lifecycle_in_two_owner_writer_roots(owners):
    cases = []
    for home, roots in owners:
        rows = []
        for root in roots:
            old = write_archive(root, age=15)
            middle = write_archive(root, age=2)
            fresh = write_archive(root)
            before = [archive.read_display_archive_page(store(root), middle, page)["rows"]
                      for page in range(middle["page_count"])]
            assert middle["page_count"] > 1
            fresh_dir = root / "display_archives" / fresh["archive_id"]
            rows.append((root, old, middle, fresh, before, contents(fresh_dir)))
        cases.append((home, rows))
    other_before = contents(owners[1][0].owner_home_dir)
    assert resolve_storage_lane("display_archives") is maintenance.maintain_display_archives_lane
    for index, (home, rows) in enumerate(cases):
        receipts = drain(home)
        assert not any(row.get("errors") for row in receipts)
        assert sum(row.get("deleted_items", 0) for row in receipts) == 2
        if index == 0:
            assert contents(owners[1][0].owner_home_dir) == other_before
        for root, old, middle, fresh, before, fresh_before in rows:
            archive_root = root / "display_archives"
            old_dir = archive_root / old["archive_id"]
            assert {path.name for path in old_dir.iterdir()} == {"manifest.json"}
            tombstone = json.loads((old_dir / "manifest.json").read_text())
            assert tombstone["expired"] is True and tombstone["expired_at"] == NOW
            with pytest.raises(archive.DisplayArchiveError) as caught:
                archive.read_display_archive_page(store(root), old, 0)
            assert caught.value.code == "DISPLAY_ARCHIVE_EXPIRED"
            middle_dir = archive_root / middle["archive_id"]
            assert (middle_dir / "pages_bundle.json").is_file()
            assert not (middle_dir / "0.json").exists()
            assert [archive.read_display_archive_page(store(root), middle, page)["rows"]
                    for page in range(middle["page_count"])] == before
            assert contents(archive_root / fresh["archive_id"]) == fresh_before
        drain(home, now=NOW + 31 * DAY)
        assert all(not (root / "display_archives" / old["archive_id"]).exists()
                   for root, old, *_ in rows)


def test_conversation_message_reference_projection_pins_archive(owners):
    home, roots = owners[0]
    pinned = write_archive(roots[0], age=15)
    unpinned = write_archive(roots[0], age=15)
    # 引用来源是规范线程中保留的消息投影；正文中仅提及 ID 不产生 pin。
    thread = ConversationThread(
        thread_id="thread-w13", canonical_user_id="owner-a",
        metadata={"message": {"role": "assistant", "display_archive_ref": pinned["archive_id"],
                              "content": unpinned["archive_id"]}},
    )
    (roots[0] / "threads" / "thread-w13.json").write_text(json.dumps(thread.to_dict()))
    drain(home)
    archive_root = roots[0] / "display_archives"
    assert not json.loads((archive_root / pinned["archive_id"] / "manifest.json").read_text()).get("expired")
    assert archive.read_display_archive_page(store(roots[0]), pinned, 0)["rows"]
    assert json.loads((archive_root / unpinned["archive_id"] / "manifest.json").read_text())["expired"]


def test_incomplete_pins_skip_only_expiry_and_are_fetched_once(owners, monkeypatch):
    home, roots = owners[0]
    reference = write_archive(roots[0], age=15)
    calls, commits = [], []

    def incomplete(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(ids=set(), complete=False), False

    def commit(operation):
        commits.append(1)
        return operation()

    monkeypatch.setattr(retention_scan, "current_retention_pins", incomplete)
    compact = maintenance.maintain_display_archives_lane(request(home, commit=commit))
    assert compact["skipped"]["compacted"] == 1 and calls == []
    expired = maintenance.maintain_display_archives_lane(request(home, compact["cursor"], commit=commit))
    assert expired["deleted_items"] == 0 and expired["skipped"]["pins_incomplete"] == 1
    assert len(calls) == len(commits) == 1
    assert calls[0][0][1] == roots
    assert calls[0][1]["now"].timestamp() == NOW
    directory = roots[0] / "display_archives" / reference["archive_id"]
    assert (directory / "pages_bundle.json").is_file()
    assert not json.loads((directory / "manifest.json").read_text()).get("expired")
    assert archive.read_display_archive_page(store(roots[0]), reference, 0)["rows"]


def test_small_budget_progresses_300_archives_while_other_owner_gets_slices(owners):
    home_a, roots_a = owners[0]
    home_b, roots_b = owners[1]
    large = [write_archive(roots_a[0], age=2, text=f"归档-{index}") for index in range(300)]
    later_root = write_archive(roots_a[1], age=2, text="同 owner 的下一根")
    other = write_archive(roots_b[1], age=15, text="另一 owner 的到期归档")
    # 基线尚无消费 StorageLaneRequest 的 W1 dispatcher；按合同交替驱动真实通道续点。
    cursor_a, cursor_b, compacted, other_done, other_steps = {}, {}, 0, False, 0
    for turn in range(400):
        row_a = maintenance.maintain_display_archives_lane(request(home_a, cursor_a, maximum=3))
        compacted += row_a["skipped"]["compacted"]
        assert row_a["skipped"]["compacted"] <= 3 and row_a["items"] <= 64
        assert not row_a["errors"]
        if turn == 0:
            assert row_a["backlog"] == 1 and row_a["cursor"] == {"root": str(roots_a[0]), "phase": "compact"}
        cursor_a = row_a["cursor"]
        if not other_done:
            row_b = maintenance.maintain_display_archives_lane(request(home_b, cursor_b, maximum=3))
            cursor_b, other_done = row_b["cursor"], row_b["complete"]
            other_steps += 1
        if row_a["complete"]:
            break
    else:
        pytest.fail("300 个归档或后续根被饿死")
    assert compacted == 301 and other_done and other_steps < turn
    assert all((roots_a[0] / "display_archives" / ref["archive_id"] / "pages_bundle.json").is_file()
               for ref in large)
    assert archive.read_display_archive_page(store(roots_a[1]), later_root, 0)["rows"][0]["text"] == "同 owner 的下一根"
    with pytest.raises(archive.DisplayArchiveError) as caught:
        archive.read_display_archive_page(store(roots_b[1]), other, 0)
    assert caught.value.code == "DISPLAY_ARCHIVE_EXPIRED"


@pytest.mark.parametrize("shape", ["missing", "empty", "file", "symlink"])
def test_empty_or_unsafe_archive_roots_make_zero_commits(owners, shape):
    home, roots = owners[0]
    other_home, other_roots = owners[1]
    write_archive(other_roots[0], age=15)
    other_before = contents(other_home.owner_home_dir)
    root = roots[0] / "display_archives"
    if shape == "empty":
        root.mkdir()
    elif shape == "file":
        root.write_text("不是目录")
    elif shape == "symlink":
        root.symlink_to(other_roots[0] / "display_archives", target_is_directory=True)
    commits = []

    def commit(operation):
        commits.append(1)
        return operation()

    rows = drain(home, commit=commit)
    assert commits == [] and len(rows) == 4
    assert all(row["items"] == row["deleted_items"] == 0 for row in rows)
    assert contents(other_home.owner_home_dir) == other_before
    if shape == "missing":
        assert not root.exists()


def test_coordinator_wraps_each_nonempty_root_phase_in_one_commit(owners, monkeypatch):
    home, roots = owners[0]
    for root in roots:
        write_archive(root, age=15)
    active, commits, phases = [], [], []
    compact, prune = maintenance.compact_display_archives, maintenance.prune_expired_display_archives

    def observe(operation, name):
        def called(root, **kwargs):
            assert active == [True]
            phases.append((Path(root), name, kwargs))
            return operation(root, **kwargs)
        return called

    def commit(operation):
        commits.append(1)
        active.append(True)
        try:
            return operation()
        finally:
            active.pop()

    monkeypatch.setattr(maintenance, "compact_display_archives", observe(compact, "compact"))
    monkeypatch.setattr(maintenance, "prune_expired_display_archives", observe(prune, "expire"))
    rows = drain(home, commit=commit)
    assert len(commits) == len(rows) == len(phases) == 4
    assert [(root, phase) for root, phase, _ in phases] == [(root, phase) for root in roots for phase in ("compact", "expire")]
    assert phases[0][2]["min_age_days"] == 1.0
    assert phases[1][2]["ttl_days"] == 14.0
    assert phases[1][2]["remove_expired_dirs_after_days"] == 30.0


def test_legal_hold_keeps_cursor_and_every_byte_without_discovery_or_commit(owners, monkeypatch):
    from agent_py_agent.agent.user_space import storage_lanes

    home, roots = owners[0]
    write_archive(roots[0], age=60)
    before = contents(home.owner_home_dir)
    cursor = {"root": str(roots[0]), "phase": "expire"}

    def forbidden(*args):
        pytest.fail("法律保留不应发现或改写目录")

    monkeypatch.setattr(storage_lanes, "owner_conversation_roots", forbidden)
    row = maintenance.maintain_display_archives_lane(request(home, cursor, legal_hold=True, commit=forbidden))
    assert row == {"cursor": cursor, "complete": True, "items": 0, "bytes": 0, "deferred_reason": "legal_hold"}
    assert contents(home.owner_home_dir) == before


def test_expiry_reduces_exact_logical_usage_but_counts_tombstones(owners):
    home, roots = owners[0]
    reference = write_archive(roots[0], age=15, text="配额中的真实正文" * 6000)
    before = owner_logical_usage_bytes(home.owner_home_dir, exact=True)
    row = maintenance.maintain_display_archives_lane(
        request(home, {"root": str(roots[0]), "phase": "expire"}),
    )
    after = owner_logical_usage_bytes(home.owner_home_dir, exact=True)
    assert row["deleted_items"] == 1 and row["bytes"] == 0
    assert "freed_bytes" not in row and 0 < after < before
    tombstone = roots[0] / "display_archives" / reference["archive_id"] / "manifest.json"
    assert owner_logical_usage_bytes(tombstone.parent, exact=True) == tombstone.stat().st_size > 0
    assert after == sum(path.stat().st_size for path in home.owner_home_dir.rglob("*") if path.is_file())


def test_interrupted_expiry_retries_same_phase_and_finishes_next_slice(owners, monkeypatch):
    home, roots = owners[0]
    reference = write_archive(roots[0], age=15)
    directory = roots[0] / "display_archives" / reference["archive_id"]
    before = {path.name: path.read_bytes() for path in directory.glob("[0-9]*.json")}
    unlink, injected = maintenance._unlink, []

    def interrupted(fd, name, expected):
        if name != "manifest.json" and not injected:
            injected.append(name)
            raise OSError("一次载荷删除中断")
        return unlink(fd, name, expected)

    monkeypatch.setattr(maintenance, "_unlink", interrupted)
    cursor = {"root": str(roots[0]), "phase": "expire"}
    first = maintenance.maintain_display_archives_lane(request(home, cursor))
    assert first["errors"] == [{"code": "display_archive_errors", "path": "conversations"}]
    assert first["cursor"] == {**cursor, "retry": True} and first["backlog"] == 1
    assert {path.name: path.read_bytes() for path in directory.glob("[0-9]*.json")} == before
    assert json.loads((directory / "manifest.json").read_text())["expired"] is True
    with pytest.raises(archive.DisplayArchiveError) as caught:
        archive.read_display_archive_page(store(roots[0]), reference, 0)
    assert caught.value.code == "DISPLAY_ARCHIVE_EXPIRED"
    second = maintenance.maintain_display_archives_lane(request(home, first["cursor"]))
    assert not second["errors"] and second["cursor"] != cursor
    assert {path.name for path in directory.iterdir()} == {"manifest.json"}


def test_busy_domain_flock_is_nonblocking_and_retried(owners):
    import fcntl

    home, roots = owners[0]
    write_archive(roots[0], age=2)
    descriptor = os.open(roots[0] / "display_archives", os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        first = maintenance.maintain_display_archives_lane(request(home))
        assert first["errors"] and first["backlog"] == 1 and first["items"] == 0
        assert first["cursor"] == {"root": str(roots[0]), "phase": "compact", "retry": True}
    finally:
        os.close(descriptor)
    second = maintenance.maintain_display_archives_lane(request(home, first["cursor"]))
    assert second["skipped"]["compacted"] == 1 and not second["errors"]


def test_symlinked_workspace_levels_never_reach_other_owner(owners):
    home, roots = owners[0]
    other_home, other_roots = owners[1]
    write_archive(other_roots[0], age=60)
    before = contents(other_home.owner_home_dir)
    runtime = roots[1].parent.parent
    (runtime / "linked-scope").symlink_to(other_roots[0].parent, target_is_directory=True)
    unsafe = runtime / "linked-conversations"
    unsafe.mkdir()
    (unsafe / "conversations").symlink_to(other_roots[0], target_is_directory=True)
    assert owner_conversation_roots(home) == roots
    drain(home)
    assert contents(other_home.owner_home_dir) == before


@pytest.mark.parametrize("maximum, expected", [(None, 128), (0, 128), (1, 1), (999, 128)])
def test_domain_budget_cap_is_explicit(owners, monkeypatch, maximum, expected):
    home, roots = owners[0]
    write_archive(roots[0], age=2)
    compact, calls = maintenance.compact_display_archives, []

    def observed(root, **kwargs):
        calls.append(kwargs)
        return compact(root, **kwargs)

    monkeypatch.setattr(maintenance, "compact_display_archives", observed)
    maintenance.maintain_display_archives_lane(request(home, maximum=maximum))
    assert calls[0]["max_archives"] == expected


def test_windows_branch_preserves_without_discovery(owners, monkeypatch):
    home, _ = owners[0]
    monkeypatch.setattr(maintenance, "os", SimpleNamespace(name="nt"))
    row = maintenance.maintain_display_archives_lane(request(home))
    assert row == {"cursor": {}, "complete": True, "items": 0, "bytes": 0, "deferred_reason": "posix_only"}


def test_missing_roots_and_stale_cursors_complete_without_writes(owners):
    home, roots = owners[0]
    rootless = replace(home, owner_home_dir=home.owner_home_dir / "absent")
    assert maintenance.maintain_display_archives_lane(request(rootless))["complete"]
    stale = {"root": str(home.owner_home_dir / "zz-no-longer-present"), "phase": "expire"}
    assert maintenance.maintain_display_archives_lane(request(home, stale))["cursor"] == {}
    assert not any((root / "display_archives").exists() for root in roots)


def test_missing_retention_policy_cannot_authorize_expiry(owners):
    home, roots = owners[0]
    reference = write_archive(roots[0], age=60)
    home.owner_retention_json.unlink()
    row = maintenance.maintain_display_archives_lane(
        request(home, {"root": str(roots[0]), "phase": "expire"}),
    )
    assert row["deleted_items"] == 0 and row["skipped"]["pins_incomplete"] == 1
    assert row["errors"] == [{"code": "display_archive_errors", "path": "conversations"}]
    assert archive.read_display_archive_page(store(roots[0]), reference, 0)["rows"]


@pytest.mark.parametrize("failure", ["preflight", "domain"])
def test_persistent_first_root_errors_do_not_starve_later_roots(owners, monkeypatch, failure):
    home, roots = owners[0]
    broken = write_archive(roots[0], age=15)
    collectible = write_archive(roots[1], age=15)
    broken_dir = roots[0] / "display_archives" / broken["archive_id"]
    if failure == "domain":
        (broken_dir / "manifest.json").write_text("{broken")
    else:
        original = maintenance._display_archive_lane_nonempty

        def denied(root):
            if Path(root) == roots[0]:
                raise PermissionError("首根持续不可读")
            return original(root)

        monkeypatch.setattr(maintenance, "_display_archive_lane_nonempty", denied)
    before = contents(broken_dir)
    rows = drain(home)
    assert len(rows) == 6
    assert rows[0]["cursor"] == {"root": str(roots[0]), "phase": "compact", "retry": True}
    assert rows[1]["cursor"] == {"root": str(roots[0]), "phase": "expire"}
    assert rows[2]["cursor"] == {"root": str(roots[0]), "phase": "expire", "retry": True}
    assert rows[3]["cursor"] == {"root": str(roots[1]), "phase": "compact"}
    assert contents(broken_dir) == before
    with pytest.raises(archive.DisplayArchiveError) as caught:
        archive.read_display_archive_page(store(roots[1]), collectible, 0)
    assert caught.value.code == "DISPLAY_ARCHIVE_EXPIRED"
    # 一轮结束不丢弃错误对象；下一轮仍会访问首根并报告错误。
    assert maintenance.maintain_display_archives_lane(request(home, rows[-1]["cursor"]))["errors"]


def test_archive_root_swapped_to_symlink_before_preflight_open_is_not_followed(owners, monkeypatch):
    home, roots = owners[0]
    other_home, other_roots = owners[1]
    write_archive(roots[0], age=15)
    write_archive(other_roots[0], age=15)
    directory = roots[0] / "display_archives"
    outside = other_roots[0] / "display_archives"
    outside_before = contents(other_home.owner_home_dir)
    lstat, scandir, swapped, commits = os.lstat, os.scandir, [], []
    outside_identity = (outside.stat().st_dev, outside.stat().st_ino)

    def swap_after_lstat(path, *args, **kwargs):
        metadata = lstat(path, *args, **kwargs)
        if Path(path) == directory and not swapped:
            swapped.append(True)
            directory.rename(directory.with_name("detached-display-archives"))
            directory.symlink_to(outside, target_is_directory=True)
        return metadata

    def observed_scandir(target):
        if isinstance(target, int):
            info = os.fstat(target)
            assert (info.st_dev, info.st_ino) != outside_identity, "预检越界读取了另一个owner"
        else:
            assert Path(target) != directory, "预检不得按竞态路径重新打开目录"
        return scandir(target)

    def commit(operation):
        commits.append(1)
        return operation()

    monkeypatch.setattr(maintenance.os, "lstat", swap_after_lstat)
    monkeypatch.setattr(maintenance.os, "scandir", observed_scandir)
    row = maintenance.maintain_display_archives_lane(request(home, commit=commit))
    assert swapped and row["errors"] and row["items"] == 0 and commits == []
    assert contents(other_home.owner_home_dir) == outside_before


def test_real_owner_rotation_retains_bounded_lane_progress_to_expired_tail(tmp_path, monkeypatch):
    """257 个非空 owner 的正常维护不能清掉尚未走完的 2049 档续点。"""
    policy = MemoryRetentionPolicy(maintenance_interval_seconds=17)

    def owner(name):
        home = home_paths(tmp_path / name)
        root = home.owner_home_dir / "conversations"
        (root / "threads").mkdir(parents=True)
        home.owner_retention_json.write_text(json.dumps(policy.to_dict()))
        return home, root

    home, root = owner("large")
    references = [write_archive(root, text=f"真实归档-{index}") for index in range(2049)]
    by_id = {ref["archive_id"]: ref for ref in references}
    with os.scandir(root / "display_archives") as entries:
        ordered = [by_id[entry.name] for entry in entries]
    target = ordered[-1]
    target_dir = root / "display_archives" / target["archive_id"]
    os.utime(target_dir / "manifest.json", (NOW - 15 * DAY,) * 2)
    original_tail = archive.read_display_archive_page(store(root), target, 0)["rows"]
    ThreadStore(ConversationStorage(root, initialize=False)).write(
        ConversationThread(thread_id="thread-w13", canonical_user_id="large"),
    )
    others = []
    for index in range(257):
        other_home, other_root = owner(f"other-{index}")
        ref = write_archive(other_root, age=15, text=f"被引用的归档-{index}")
        ThreadStore(ConversationStorage(other_root, initialize=False)).write(ConversationThread(
            thread_id="thread-w13", canonical_user_id=f"other-{index}",
            metadata={"message": {"role": "assistant", "display_archive_ref": ref["archive_id"]}},
        ))
        others.append((other_home, other_root, ref, {}))

    foreground_writes = []

    def forbidden_write(*args, **kwargs):
        foreground_writes.append(1)
        pytest.fail("夹具完成后只能维护，不能重新写归档或线程")

    monkeypatch.setattr(archive, "archive_display_rows", forbidden_write)
    monkeypatch.setattr(ThreadStore, "write", forbidden_write)
    monkeypatch.setattr(ThreadStore, "update_atomic", forbidden_write)
    clock = [100.0]
    monkeypatch.setattr(maintenance, "time", SimpleNamespace(
        time=maintenance.time.time, monotonic=lambda: clock[0],
    ))
    counts, covered, commits = [], {"compact": set(), "expire": set()}, []
    active = [None, None]
    open_scan = maintenance._open_scan

    def observed_scan(descriptor):
        scans, entries = open_scan(descriptor)

        def counted():
            for entry in entries:
                counts[-1] += 1
                if active[0] == home:
                    covered[active[1]].add(entry.name)
                yield entry

        return scans, counted()

    monkeypatch.setattr(maintenance, "_open_scan", observed_scan)
    lane = resolve_storage_lane("display_archives")
    assert lane is maintenance.maintain_display_archives_lane

    def commit(operation):
        commits.append(1)
        return operation()

    def step(current_home, cursor, maximum=128):
        active[:] = [current_home, cursor.get("phase", "compact")]
        counts.append(0)
        receipt = validate_lane_receipt(lane(request(current_home, cursor, maximum=maximum, commit=commit)))
        assert not receipt["errors"] and counts[-1] <= 2048
        assert len(maintenance._live_scans) <= 128 and len(maintenance._scan_recent) <= 257
        return receipt

    rows = []

    def adapter(cursor, budget):
        receipt = step(home, cursor, budget["items"])
        rows.append(receipt)
        # 与生产 adapter 相同，只透传调度器实际消费的四个字段。
        return {key: receipt[key] for key in ("cursor", "complete", "items", "bytes")}

    fd_dir = "/proc/self/fd" if Path("/proc/self/fd").is_dir() else "/dev/fd"
    fd_before = len(os.listdir(fd_dir))
    state = {}
    try:
        for turn in range(8):
            state = run_storage_maintenance_slice(
                state, {"display_archives": adapter}, max_items=128, max_bytes=0, max_seconds=0,
            )
            assert not state["errors"]
            receipt = rows[-1]
            assert ("display_archives" in state["pending_kinds"]) is not receipt["complete"]
            assert state["cursors"]["display_archives"] == receipt["cursor"]
            if receipt["skipped"]["compacted"]:
                assert archive.read_display_archive_page(store(root), target, 0)["rows"] == original_tail
            for index, (other_home, other_root, ref, cursor) in enumerate(others):
                other = step(other_home, cursor)
                others[index] = (other_home, other_root, ref, other["cursor"])
            if turn == 0:
                info = (root / "display_archives").stat()
                key = (info.st_dev, info.st_ino, "compact_display_archives.<locals>.action")
                assert key in maintenance._live_scans, "未到清退期限的活扫描被其它 owner 淘汰"
                assert maintenance._scan_recent[key] == clock[0] + 2 * policy.maintenance_interval_seconds
            if receipt["complete"]:
                assert covered == {phase: set(by_id) for phase in covered}
                break
            clock[0] += 1
        else:
            pytest.fail("有限归档目录未能沿原调度器续点完成")
        assert [row["cursor"].get("phase") for row in rows] == ["compact", "expire", "expire", None]
        assert [row["backlog"] for row in rows] == [1, 0, 1, 0]
        assert rows[1]["skipped"]["compacted"] == rows[-1]["deleted_items"] == 1
        assert json.loads((target_dir / "manifest.json").read_text())["expired"] is True
        assert {path.name for path in target_dir.iterdir()} == {"manifest.json"}
        with pytest.raises(archive.DisplayArchiveError) as caught:
            archive.read_display_archive_page(store(root), target, 0)
        assert caught.value.code == "DISPLAY_ARCHIVE_EXPIRED" and original_tail
        assert archive.read_display_archive_page(store(root), ordered[0], 0)["rows"]
        for _, other_root, ref, _ in others:
            assert archive.read_display_archive_page(store(other_root), ref, 0)["rows"]
        assert len(commits) == len(counts) and foreground_writes == []
    finally:
        with maintenance._scan_cursor_lock:
            for scans, _, _ in maintenance._live_scans.values():
                scans.close()
            maintenance._live_scans.clear()
            maintenance._scan_cursors.clear()
            maintenance._scan_recent.clear()
            maintenance._scan_waiting.clear()
        assert len(os.listdir(fd_dir)) - fd_before == 0
