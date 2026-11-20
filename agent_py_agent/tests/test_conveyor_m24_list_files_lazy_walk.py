from __future__ import annotations

import fnmatch
import os
import random
import tracemalloc
from itertools import islice
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.cancellation import (
    CancellationToken,
    ToolCancelled,
    bind_cancellation_token,
)
from agent_py_agent.agent.path_access_policy import PathAccessPolicy
from agent_py_agent.agent.tooling import _filesystem_list as list_module
from agent_py_agent.agent.tooling._filesystem_helpers import _internal_agent_status_ref
from agent_py_agent.agent.tooling._filesystem_list import ListFilesTool
from agent_py_agent.agent.tooling._filesystem_read import (
    _COMMON_FILE_DISCOVERY_IGNORES,
    FileSystemAccessOptions,
)


def _write(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def _link(path: Path, destination: Path, *, directory: bool = False) -> None:
    try:
        path.symlink_to(destination, target_is_directory=directory)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"符号链接不可用：{exc}")


def _rows(result) -> list[str]:
    assert result.ok, result.output
    lines = result.output.splitlines()
    return [line for line in lines if not line.startswith(("... ", "说明：")) and line != "目录为空"]


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    state = SimpleNamespace(now=0.0)
    monkeypatch.setattr(list_module, "time", SimpleNamespace(monotonic=lambda: state.now), raising=False)
    return state


def _legacy_order(tool: ListFilesTool, root: Path, params: dict) -> list[str]:
    recursive = params.get("recursive", False)
    include_ignored = params.get("include_ignored", False)
    include_dirs = params.get("include_dirs", True)
    include_files = params.get("include_files", True)
    max_depth = params.get("max_depth", 0)
    pattern = params.get("file_glob", "")
    candidates = []
    if recursive:
        for parent, dirs, files in os.walk(root):
            if not include_ignored:
                dirs[:] = [name for name in dirs if name not in _COMMON_FILE_DISCOVERY_IGNORES]
            candidates.extend(Path(parent) / name for name in dirs + files)
    else:
        candidates = [entry for entry in root.iterdir() if include_ignored or entry.name not in _COMMON_FILE_DISCOVERY_IGNORES]
    rows = []
    for item in sorted(candidates, key=lambda entry: (entry.as_posix().lower(), entry.as_posix())):
        is_dir = item.is_dir()
        if (is_dir and not include_dirs) or (item.is_file() and not include_files):
            continue
        if recursive and max_depth and len(item.relative_to(root).parts) > max_depth:
            continue
        if _internal_agent_status_ref(item, include_agent_directory=True):
            continue
        if tool.path_access_policy.host_managed_store(item) is not None:
            continue
        display = tool.display_path(item)
        if pattern and not (fnmatch.fnmatch(item.name, pattern) or fnmatch.fnmatch(display, pattern)):
            continue
        rows.append(display + ("/" if is_dir else ""))
    return rows


@pytest.fixture(params=["main", "second"])
def mixed_tree(tmp_path, request):
    root = tmp_path / "home" / "owners" / "local" / request.param
    service_log = root / "workspace/runtime/services/gateway/gateway.log"
    managed_file = root / "data/plugins/store.txt"
    for item in (
        service_log,
        service_log.with_name("supervisor.log"),
        service_log.with_name("queue.txt"),
        service_log.parent.parent / "other" / "entry.txt",
        managed_file,
        root / "work/agents/run-test/state.json",
        root / "work/agents/subagent-test/notes.txt",
        root / "work/agents/user-notes/kept.txt",
        root / ".git/internal",
        root / "files/node_modules",
        root / "a/leaf.txt",
        root / "a-b/leaf.txt",
        root / "a.txt",
        root / "a0/leaf.txt",
    ):
        _write(item)
    randomizer = random.Random(f"m24-{request.param}")
    for index in range(55):
        parts = [randomizer.choice(("Z", "apple", "AΣ", "İ", "Σx", "a.dot")) for _ in range(randomizer.randrange(4))]
        _write(root.joinpath(*parts, f"note-{index:03}.txt"))
    outside_file = tmp_path / "outside.txt"
    _write(outside_file)
    _link(root / "link-file", root / "a.txt")
    _link(root / "link-dir", root / "a", directory=True)
    _link(root / "link-outside", outside_file)
    _link(root / "link-missing", root / "nonexistent")
    _link(root / ".venv", root / "a", directory=True)
    _link(root / "link-managed", managed_file)
    tool = ListFilesTool(root, max_entries=50, access_options=FileSystemAccessOptions(owner_scope_root=str(root)))
    return tool, root, request.param


@pytest.mark.parametrize("params", [
    {"recursive": True},
    {"recursive": True, "max_depth": 2, "include_dirs": False, "include_ignored": True},
    {"recursive": True, "include_files": False},
    {"recursive": True, "file_glob": "*.txt", "include_ignored": False},
    {"recursive": False},
])
def test_offset_pages_match_legacy_order(mixed_tree, params):
    tool, root, owner = mixed_tree
    expected = _legacy_order(tool, root, params)
    actual = []
    for offset in range(0, len(expected) + 1, 7):
        actual.extend(_rows(tool.execute({**params, "offset": offset, "limit": 7})))
    assert actual == expected
    if params == {"recursive": True}:
        assert "work/agents/user-notes/kept.txt" in actual
        assert "link-dir/" in actual and "link-dir/leaf.txt" not in actual
        assert "files/node_modules" in actual
        assert ("workspace/runtime/services/gateway/gateway.log" in actual) == (owner == "main")
        assert "workspace/runtime/services/gateway/queue.txt" not in actual


@pytest.mark.parametrize("limit", [1, 7, 50])
@pytest.mark.parametrize("recursive", [False, True])
def test_keyset_pages_match_legacy_order(mixed_tree, limit, recursive):
    tool, root, _ = mixed_tree
    params = {"recursive": recursive, "limit": limit}
    expected = _legacy_order(tool, root, params)
    actual = []
    cursor = ""
    for _ in range(len(expected) + 1):
        result = tool.execute({**params, "after_entry": cursor})
        actual.extend(_rows(result))
        cursor = result.result_envelope.get("next_after_entry", "")
        if result.result_envelope["page_window"]["complete"]:
            break
        assert cursor
    assert actual == expected


def test_prunes_managed_and_internal_subtrees_without_writes(mixed_tree, monkeypatch):
    tool, root, owner = mixed_tree
    data_root = root.parents[2]
    before = set(data_root.rglob("*"))
    scanned = set()
    original = os.scandir

    def observe(path):
        scanned.add(Path(path))
        return original(path)

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "scandir", observe)
        result = tool.execute({"recursive": True, "file_glob": "*.log", "include_ignored": False})
    assert root / "work/agents/user-notes" in scanned
    for path in ("work/agents/run-test", "work/agents/subagent-test", "data/plugins", "workspace/runtime/services/other"):
        assert root / path not in scanned
    gateway = root / "workspace/runtime/services/gateway"
    assert (gateway in scanned) == (owner == "main")
    assert _rows(result) == (
        ["workspace/runtime/services/gateway/gateway.log", "workspace/runtime/services/gateway/supervisor.log"]
        if owner == "main" else []
    )
    assert set(data_root.rglob("*")) == before


def test_after_entry_counts_offset_from_lexical_path(tmp_path):
    root = tmp_path / "nested"
    _write(root / "a.txt")
    _write(root / "z.txt")
    _write(root / "middle.txt")
    _link(root / "b-link", root / "z.txt")
    tool = ListFilesTool(tmp_path, max_entries=10)
    for cursor in ("b-link", str(root / "b-link")):
        result = tool.execute({"path": "nested", "after_entry": cursor, "offset": 1})
        assert _rows(result) == ["nested/z.txt"]


def test_cursor_preserves_implicit_ignore_fallback(tmp_path):
    root = tmp_path.resolve()
    _write(root / "node_modules" / "a.txt")
    _write(root / "node_modules" / "b.txt")
    tool = ListFilesTool(root, max_entries=1)
    params = {"recursive": True, "file_glob": "node_modules*"}
    first = tool.execute(params)
    assert _rows(first) == ["node_modules/"]
    assert first.result_envelope["next_after_entry"] == "node_modules/"
    assert _rows(tool.execute({**params, "after_entry": first.result_envelope["next_after_entry"]})) == [
        "node_modules/a.txt",
    ]

    _write(root / "0.txt")
    visible = tool.execute({"recursive": True, "file_glob": "*.txt", "after_entry": "0.txt"})
    assert _rows(visible) == []
    assert visible.result_envelope["page_window"]["complete"]


@pytest.fixture
def wide_tree(tmp_path, monkeypatch, wide_entries):
    root = tmp_path.resolve()
    deep = root / "one" / "two"
    deep.mkdir(parents=True)
    virtual_entries, _ = wide_entries
    original = os.scandir

    def scandir(path):
        if Path(path) == deep:
            return _Scandir(entry for entry in virtual_entries)
        return original(path)

    monkeypatch.setattr(os, "scandir", scandir)
    return root, deep


def test_max_depth_never_scans_below_the_limit(wide_tree, monkeypatch):
    root, _ = wide_tree
    original = os.scandir
    scans = []

    def counted(path):
        scans.append(Path(path))
        return original(path)

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "scandir", counted)
        result = ListFilesTool(root, max_entries=50).execute({"recursive": True, "max_depth": 1})
    assert _rows(result) == ["one/"]
    assert scans == [root]


def test_flat_100k_page_stays_below_32_mib(wide_tree):
    _, root = wide_tree
    tool = ListFilesTool(root, max_entries=50)
    tracemalloc.start()
    try:
        result = tool.execute({"recursive": True, "limit": 50})
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(_rows(result)) == 50
    assert peak < 32 * 1024 * 1024, f"峰值 {peak / (1024 * 1024):.1f} MiB"


def test_legacy_offset_crosses_directory_batch(wide_tree, wide_entries):
    _, root = wide_tree
    _, ordered = wide_entries
    tool = ListFilesTool(root, max_entries=3)
    result = tool.execute({"offset": 4095})
    assert _rows(result) == ordered[4095:4098]
    assert result.result_envelope["page_window"]["next_offset"] == 4098
    assert result.result_envelope["next_after_entry"] == ordered[4097]


def test_budget_cursor_resumes_without_descending_emitted_prefix(tmp_path, monkeypatch, clock):
    root = tmp_path.resolve()
    for path in ("a/only.txt", "b/early.txt", "b/late.txt", "c/final.txt"):
        _write(root / path)
    tool = ListFilesTool(root, max_entries=50)
    original = os.scandir
    scans = []
    expires = True

    def counted(path):
        directory = Path(path)
        scans.append(directory)

        def finish():
            if expires and directory == root / "c":
                clock.now = 9.0

        return _Scandir(original(path), on_close=finish)

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "scandir", counted)
        first = tool.execute({"recursive": True, "include_dirs": False})
        assert first.result_envelope.get("scan_incomplete") is True
        prefix = _rows(first)
        assert prefix and first.result_envelope["next_after_entry"] == prefix[-1]
        expires = False
        scans.clear()
        clock.now = 0.0
        rest = tool.execute({"recursive": True, "include_dirs": False, "after_entry": prefix[-1]})
        assert root / "b" in scans
        assert root / "a" not in scans
        assert rest.result_envelope["scan_incomplete"] is False
    assert prefix + _rows(rest) == ["a/only.txt", "b/early.txt", "b/late.txt", "c/final.txt"]


class _Scandir:
    def __init__(self, entries, *, on_close=None):
        self._entries = entries
        self._on_close = on_close

    def __enter__(self):
        return self

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._entries)

    def __exit__(self, *_):
        self._entries.close()
        if self._on_close is not None:
            self._on_close()


class _VirtualEntry:
    __slots__ = ("name", "directory")

    def __init__(self, name, *, directory=False):
        self.name = name
        self.directory = directory

    def is_dir(self, *, follow_symlinks=True):
        return self.directory

    def is_file(self, *, follow_symlinks=True):
        return not self.directory

    def is_symlink(self):
        return False

    def stat(self, *, follow_symlinks=True):
        return SimpleNamespace(st_file_attributes=0)


@pytest.mark.parametrize("limit", [1, 7, 50])
def test_cursor_merges_case_colliding_directory_names(tmp_path, monkeypatch, limit):
    # 默认 macOS 卷可能无法实建同名变体，使用目录枚举替身验证完整排序键。
    root = tmp_path.resolve()
    tool = ListFilesTool(root, max_entries=limit)
    files = {
        "a": ("next.txt", "Ω.txt"),
        "A": ("NEXT.txt", "end.txt"),
        "a-": ("end.txt",),
        "a0": ("end.txt",),
        "Σ": ("item.txt",),
        "σ": ("item.txt",),
        "İ": ("item.txt",),
        "i\u0307": ("item.txt",),
    }
    # 路径对象在 Windows 上按大小写无关比较，替身用 POSIX 显示字符串作为原样 key。
    choices = {
        (root / parent).as_posix(): tuple(_VirtualEntry(name) for name in names)
        for parent, names in files.items()
    }
    choices[root.as_posix()] = (_VirtualEntry("a.txt"), *(_VirtualEntry(name, directory=True) for name in files))
    expected = [
        "a.txt",
        *(parent + "/" for parent in files),
        *(f"{parent}/{name}" for parent, names in files.items() for name in names),
    ]
    expected.sort(key=lambda row: (row.rstrip("/").lower(), row.rstrip("/")))
    original = os.scandir

    def enumerate_tree(path):
        names = choices.get(Path(path).as_posix())
        return _Scandir(entry for entry in names) if names is not None else original(path)

    actual = []
    cursor = ""
    with monkeypatch.context() as scoped:
        scoped.setattr(os, "scandir", enumerate_tree)
        for _ in range(len(expected) + 1):
            result = tool.execute({"recursive": True, "after_entry": cursor})
            actual.extend(_rows(result))
            cursor = result.result_envelope["next_after_entry"]
            if not cursor:
                break
    assert actual == expected


def _wide_names():
    return (f"{prefix}{index:05}.txt" for prefix in ("z", "a") for index in range(50_000))


@pytest.fixture(scope="module")
def wide_entries():
    entries = tuple(_VirtualEntry(name) for name in _wide_names())
    return entries, sorted(entry.name for entry in entries)


@pytest.mark.parametrize("start", range(0, 100_000, 20_000))
def test_wide_unordered_directory_advances_during_expired_selections(tmp_path, monkeypatch, clock, wide_entries, start):
    root = tmp_path.resolve()
    tool = ListFilesTool(root, max_entries=4000)
    tool.path_access_policy = PathAccessPolicy()
    virtual_entries, ordered = wide_entries
    end = start + 20_000
    original = os.scandir
    scan_count = 0
    expired_pages = 0
    expire_next = False
    checked = []
    original_visible = tool._list_item_visible

    def observe(item, **kwargs):
        checked.append(item.name)
        return original_visible(item, **kwargs)

    monkeypatch.setattr(tool, "_list_item_visible", observe)

    def expiring_entries():
        entries = iter(virtual_entries)
        yield from islice(entries, 60_000)
        clock.now = 9.0
        yield from entries

    def unordered(path):
        nonlocal scan_count
        if Path(path) == root:
            scan_count += 1
            return _Scandir(expiring_entries() if expire_next else (entry for entry in virtual_entries))
        return original(path)

    # 各段用前一段的末键衔接，覆盖同一稳定目录的全部 100,000 项。
    cursor = ordered[start - 1] if start else ""
    actual = []
    with monkeypatch.context() as scoped:
        scoped.setattr(os, "scandir", unordered)
        for page_index in range(9):
            clock.now = 0.0
            expire_next = start == 0 and page_index in {0, 2, 4}
            previous_scans = scan_count
            checked.clear()
            result = tool.execute({"recursive": True, "after_entry": cursor, "limit": min(4000, end - start - len(actual))})
            page = _rows(result)
            if expire_next:
                assert result.result_envelope.get("scan_incomplete") is True
                assert page
                expired_pages += 1
            actual.extend(page)
            assert scan_count == previous_scans + 1
            assert len(checked) <= len(page) + 1 and all(name > cursor for name in checked)
            complete = result.result_envelope["page_window"]["complete"]
            if start + len(actual) == end:
                assert complete == (end == len(ordered))
                assert result.result_envelope["next_after_entry"] == ("" if complete else ordered[end - 1])
                break
            assert not complete
            next_cursor = result.result_envelope["next_after_entry"]
            assert next_cursor == page[-1]
            assert (next_cursor.lower(), next_cursor) > (cursor.lower(), cursor)
            cursor = next_cursor
    assert expired_pages == (3 if start == 0 else 0)
    assert actual == ordered[start:end]
    assert not list(root.iterdir())


def test_cancelled_selection_closes_iterator(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    tool = ListFilesTool(root, max_entries=50)
    token = CancellationToken()
    read_count = 0
    closed = False
    original = os.scandir

    def entries():
        nonlocal read_count
        for name in _wide_names():
            read_count += 1
            if read_count == 300:
                token.cancel("selection stopped")
            yield _VirtualEntry(name)

    def counted(path):
        if Path(path) != root:
            return original(path)

        def finish():
            nonlocal closed
            closed = True

        return _Scandir(entries(), on_close=finish)

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "scandir", counted)
        with bind_cancellation_token(token), pytest.raises(ToolCancelled, match="selection stopped"):
            tool.execute({"recursive": True})
    assert closed
    assert 300 <= read_count <= 300 + 256
