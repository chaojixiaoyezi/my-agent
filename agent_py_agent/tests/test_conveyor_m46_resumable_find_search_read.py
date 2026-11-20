from __future__ import annotations

import base64
import json
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.tooling import _filesystem_find as find
from agent_py_agent.agent.tooling import _filesystem_helpers, _filesystem_list
from agent_py_agent.agent.tooling import _filesystem_search as search
from agent_py_agent.agent.tooling._filesystem_find import FindFilesTool
from agent_py_agent.agent.tooling._filesystem_read import ReadFileTool
from agent_py_agent.agent.tooling._filesystem_search import SearchTextTool


def _meter_source_reads(monkeypatch):
    reads = []
    original = search._checked_search_file

    class Meter:
        def __init__(self, handle):
            self.handle = handle

        def __getattr__(self, attr):
            return getattr(self.handle, attr)

        def read(self, size):
            data = self.handle.read(size)
            reads.append(len(data))
            return data

    @contextmanager
    def metered(tool, filename):
        with original(tool, filename) as handle:
            yield Meter(handle)

    monkeypatch.setattr(search, "_checked_search_file", metered)
    return reads


@pytest.fixture
def large_tree_path():
    with tempfile.TemporaryDirectory(prefix="m46-tree-") as directory:
        yield Path(directory).resolve()


@pytest.fixture
def large_search_tree(large_tree_path):
    source_bytes = 0
    for group in range(10):
        directory = large_tree_path / f"{group:02}"
        directory.mkdir()
        for index in range(2000):
            content = b"needle\n" if index == 1999 else b"sample\n"
            (directory / f"{index:04}.txt").write_bytes(content)
            source_bytes += len(content)
    return large_tree_path, source_bytes


@pytest.mark.timeout(60, func_only=True)
def test_search_ten_pages_reads_each_candidate_once(large_search_tree, monkeypatch):
    tmp_path, source_bytes = large_search_tree
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path))
    # 此处单独核算读放大；时限有独立的续页用例。
    fake_clock = SimpleNamespace(monotonic=lambda: 0.0)
    for module in (search, _filesystem_list, _filesystem_helpers):
        monkeypatch.setattr(module, "time", fake_clock)
    reads = _meter_source_reads(monkeypatch)
    rows = []
    cursor = ""
    for _ in range(10):
        # A new instance demonstrates that continuation carries its own position.
        tool = SearchTextTool(tmp_path, max_matches=1)
        result = tool.execute({"query": "needle", "literal": True, "limit": 1, "cursor": cursor})
        assert result.ok, result.output
        rows.extend(row for row in result.output.splitlines() if row.endswith(": needle"))
        cursor = result.result_envelope["cursor"]
        if not cursor:
            break
    assert rows == [f"{group:02}/1999.txt:1: needle" for group in range(10)]
    assert sum(reads) <= source_bytes + 10 * len(b"needle\n")


def test_read_checkpoint_resumes_and_detects_file_change(tmp_path, monkeypatch):
    path = tmp_path / "sample.txt"
    path.write_bytes("甲\r\n乙\r\n丙".encode())
    request = {"path": "sample.txt"}
    first = ReadFileTool(tmp_path, max_chars=80).execute({**request, "end_line": 1})
    assert first.ok
    checkpoint = first.result_envelope["decode_checkpoint"]
    assert checkpoint["v"] == 1
    assert checkpoint["line"] == 2
    assert checkpoint["byte_offset"] == len("甲\r\n".encode())

    from agent_py_agent.agent.tooling import filesystem_read_file

    def unexpected_index(*_args):
        pytest.fail("same-process continuation rebuilt the full text index")

    monkeypatch.setattr(filesystem_read_file, "text_file_index", unexpected_index)
    tool = ReadFileTool(tmp_path, max_chars=80)
    second = tool.execute({**request, "checkpoint": checkpoint, "end_line": 2})
    assert second.ok
    assert second.output.splitlines()[0] == "2: 乙"

    path.write_bytes(b"changed\n")
    stale = tool.execute({**request, "checkpoint": checkpoint})
    assert not stale.ok
    assert stale.error_code == "CURSOR_SOURCE_CHANGED"


@pytest.fixture
def wide_find_tree(large_tree_path):
    matches = [f"{index:06}.match" for index in range(0, 100000, 10000)]
    for index in range(100000):
        suffix = ".match" if index % 10000 == 0 else ".txt"
        with (large_tree_path / f"{index:06}{suffix}").open("xb"):
            pass
    return large_tree_path, matches


@pytest.mark.timeout(60, func_only=True)
def test_find_order_and_progress_across_expired_budgets(wide_find_tree, monkeypatch):
    tmp_path, matches = wide_find_tree
    original_scandir = _filesystem_list.os.scandir
    with original_scandir(tmp_path) as entries:
        reversed_entries = sorted(entries, key=lambda entry: entry.name, reverse=True)
    elapsed = 0.0

    class ReversedDirectory:
        def __enter__(self):
            return iter(reversed_entries)

        def __exit__(self, *_args):
            return False

    def scandir(path):
        nonlocal elapsed
        if isinstance(path, (str, Path)) and Path(path) == tmp_path:
            elapsed += 61
            return ReversedDirectory()
        return original_scandir(path)

    monkeypatch.setattr(_filesystem_list.os, "scandir", scandir)
    fake_clock = SimpleNamespace(monotonic=lambda: elapsed)
    monkeypatch.setattr(_filesystem_list, "time", fake_clock)
    monkeypatch.setattr(find, "time", fake_clock)
    observed = []
    cursor = ""
    for _ in range(len(matches) + 1):
        tool = FindFilesTool(tmp_path, max_matches=2)
        page = tool.execute({"pattern": "*.match", "cursor": cursor})
        assert page.ok, page.output
        rows = [row for row in page.output.splitlines() if row.endswith(".match")]
        observed.extend(rows)
        cursor = page.result_envelope["cursor"]
        if not cursor:
            break
        assert rows
        assert page.result_envelope["scan_incomplete"]
        state = json.loads(base64.urlsafe_b64decode(cursor))
        assert state["after_entry"] == rows[-1]
    assert observed == matches


def test_search_rejects_changed_anchor_and_query(tmp_path):
    path = tmp_path / "source.txt"
    path.write_text("sample\nsample\n", encoding="utf-8")
    tool = SearchTextTool(tmp_path, max_matches=1)
    first = tool.execute({"query": "sample", "cursor": ""})
    cursor = first.result_envelope["cursor"]
    changed_query = tool.execute({"query": "another", "cursor": cursor})
    assert changed_query.error_code == "CURSOR_SOURCE_CHANGED"
    path.write_text("sample\nsample\nchanged\n", encoding="utf-8")
    changed_file = tool.execute({"query": "sample", "cursor": cursor})
    assert changed_file.error_code == "CURSOR_SOURCE_CHANGED"
    assert changed_file.result_envelope["after_entry"] == "source.txt"


def test_search_large_file_resumes_byte_reads_within_one_source(tmp_path, monkeypatch):
    monkeypatch.setattr(search.shutil, "which", lambda _name: None)
    path = tmp_path / "source.txt"
    path.write_bytes((b"sample text\n" * 16000 + b"needle\n") * 10)
    source_size = path.stat().st_size
    reads = _meter_source_reads(monkeypatch)
    cursor = ""
    returned = []
    for number in range(10):
        query = {"query": "needle", "path": path.name, "literal": True, "output_mode": "line_numbers"}
        if cursor is None:
            query["offset"] = number
        else:
            query["cursor"] = cursor
        page = SearchTextTool(tmp_path, max_matches=1).execute(query)
        assert page.ok, page.output
        returned.append(page.output.splitlines()[0])
        cursor = page.result_envelope.get("cursor")
    assert returned == [f"source.txt:{16001 * number}" for number in range(1, 11)]
    assert sum(reads) <= 2 * source_size


def test_search_resume_preserves_before_and_after_context(tmp_path):
    (tmp_path / "source.txt").write_text("before\nsample one\nsample two\nafter\n")
    query = {"query": "sample", "context": 1, "cursor": ""}
    tool = SearchTextTool(tmp_path, max_matches=1)
    first = tool.execute(query)
    second = tool.execute({**query, "cursor": first.result_envelope["cursor"]})
    assert second.ok, second.output
    assert "sample one" in second.output and "sample two" in second.output and "after" in second.output


def test_read_checkpoint_uses_raw_boundary_with_standalone_cr(tmp_path):
    file = tmp_path / "source.txt"
    data = b"\xfe\xff" + "甲\r\r丙\r丁".encode("utf-16-be")
    file.write_bytes(data)
    first = ReadFileTool(tmp_path, max_chars=80).execute({"path": file.name, "end_line": 2})
    checkpoint = first.result_envelope["decode_checkpoint"]
    assert checkpoint["byte_offset"] == len(b"\xfe\xff" + "甲\r\r".encode("utf-16-be"))
    resumed = ReadFileTool(tmp_path, max_chars=80).execute({"path": file.name, "checkpoint": checkpoint})
    assert resumed.ok, resumed.output
    assert resumed.output.splitlines()[:2] == ["3: 丙", "4: 丁"]
    portable = {key: checkpoint[key] for key in ("v", "file_id", "byte_offset", "line", "encoding")}
    reopened = ReadFileTool(tmp_path, max_chars=80).execute({"path": file.name, "checkpoint": portable})
    assert reopened.ok and reopened.output.splitlines()[:2] == ["3: 丙", "4: 丁"]

    modified = {**checkpoint, "byte_offset": checkpoint["byte_offset"] + 1}
    invalid = ReadFileTool(tmp_path, max_chars=80).execute({"path": file.name, "checkpoint": modified})
    assert not invalid.ok and invalid.error_code == "TOOL_INVALID_ARGUMENTS"


def test_search_discovery_resume_advances_without_a_match(tmp_path, monkeypatch):
    (tmp_path / "source.txt").write_text("skip\n" * 10 + "needle\n")
    monkeypatch.setattr(search, "_SEARCH_READ_MAX_BYTES", 12)
    request = {"query": "needle", "literal": True, "cursor": ""}
    tool = SearchTextTool(tmp_path, max_matches=1)
    page = tool.execute(request)
    assert page.ok, page.output
    assert page.result_envelope["scan_incomplete"]
    cursor = page.result_envelope["cursor"]
    first = json.loads(base64.urlsafe_b64decode(cursor))
    assert first["after_entry"] == ""
    assert first["candidate"] == "source.txt"
    assert first["byte_offset"] > 0
    assert first["phase"] == "discovery"

    positions = [first["byte_offset"]]
    rows = []
    for _ in range(10):
        page = tool.execute({**request, "cursor": cursor})
        assert page.ok, page.output
        rows.extend(row for row in page.output.splitlines() if row.endswith(": needle"))
        cursor = page.result_envelope["cursor"]
        if not cursor:
            break
        positions.append(json.loads(base64.urlsafe_b64decode(cursor))["byte_offset"])
    assert not cursor
    assert positions == sorted(set(positions))
    assert rows == ["source.txt:11: needle"]


def test_find_rejects_changed_last_emitted_entry(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("sample")
    (tmp_path / "b.txt").write_text("sample")
    request = {"pattern": "*.txt", "cursor": ""}
    tool = FindFilesTool(tmp_path, max_matches=1)
    page = tool.execute(request)
    token = page.result_envelope["cursor"]
    assert json.loads(base64.urlsafe_b64decode(token))["phase"] == "known_matches"
    path.write_text("sample changed")
    stale = tool.execute({**request, "cursor": token})
    assert stale.error_code == "CURSOR_SOURCE_CHANGED"
    assert stale.result_envelope["after_entry"] == "a.txt"


def test_legacy_numbered_reads_seek_from_the_line_index(tmp_path, monkeypatch):
    from agent_py_agent.agent.common.text_file_window import TextFileIndex

    path = tmp_path / "long.txt"
    path.write_bytes(b"sample line\n" * 20000)
    original = TextFileIndex.open
    calls = 0

    class Meter:
        def __init__(self, handle):
            self.handle = handle

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def readline(self, size):
            nonlocal calls
            calls += 1
            return self.handle.readline(size)

    @contextmanager
    def metered(index):
        with original(index) as handle:
            yield Meter(handle)

    monkeypatch.setattr(TextFileIndex, "open", metered)
    tool = ReadFileTool(tmp_path, max_chars=80)
    page = tool.execute({"path": path.name, "start_line": 19000, "end_line": 19001})
    assert page.ok, page.output
    assert page.output.splitlines()[0] == "19000: sample line"
    assert calls < 6000


def test_search_checks_last_delivered_file_even_after_unmatched_candidates(tmp_path):
    (tmp_path / "first.txt").write_text("needle\n")
    (tmp_path / "gap.txt").write_text("sample\n")
    (tmp_path / "last.txt").write_text("needle\n")
    tool = SearchTextTool(tmp_path, max_matches=1)
    query = {"query": "needle", "cursor": "", "literal": True}
    first = tool.execute(query)
    token = first.result_envelope["cursor"]
    state = json.loads(base64.urlsafe_b64decode(token))
    assert state["candidate"] == "gap.txt"
    assert state["after_entry"] == "first.txt"
    (tmp_path / "first.txt").write_text("needle changed\n")
    stale = tool.execute({**query, "cursor": token})
    assert stale.error_code == "CURSOR_SOURCE_CHANGED"
    assert stale.result_envelope["after_entry"] == "first.txt"


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("count", ["a.txt: 5", "b.txt: 3"]), ("files_with_matches", ["a.txt", "b.txt"])],
)
def test_search_resume_preserves_file_level_outputs(tmp_path, monkeypatch, mode, expected):
    (tmp_path / "a.txt").write_text("needle\n" * 5)
    (tmp_path / "b.txt").write_text("needle\n" * 3)
    monkeypatch.setattr(search, "_SEARCH_READ_MAX_BYTES", 16)
    tool = SearchTextTool(tmp_path, max_matches=1)
    cursor = ""
    observed = []
    for _ in range(12):
        page = tool.execute({"query": "needle", "output_mode": mode, "cursor": cursor})
        assert page.ok, page.output
        observed.extend(row for row in page.output.splitlines() if row.startswith(("a.txt", "b.txt")))
        cursor = page.result_envelope["cursor"]
        if not cursor:
            break
    assert not cursor
    assert observed == expected


@pytest.mark.parametrize(
    ("name", "query", "rows"),
    [
        ("find_files", {"pattern": "*.txt"}, ["a.txt", "b.txt"]),
        ("search_text", {"query": "needle"}, ["a.txt:1: needle", "b.txt:1: needle"]),
    ],
)
def test_registry_exposes_and_accepts_the_cursor(tmp_path, name, query, rows):
    from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call
    from agent_py_agent.tests.test_tools.backends import make_tool_registry

    for filename in ("a.txt", "b.txt"):
        (tmp_path / filename).write_text("needle\n")
    registry = make_tool_registry(tmp_path)
    params = {**query, "limit": 1, "cursor": ""}
    first = execute_registry_test_call(registry, name, params, allowed_tools=[name], call_id="m46-page-1")
    assert first.ok, first.output
    details = first.metadata["handler_details"]
    token = details["cursor"]
    assert token and details["page_window"]["cursor"] == token
    assert f"cursor={token}" in first.output
    params["cursor"] = token
    second = execute_registry_test_call(registry, name, params, allowed_tools=[name], call_id="m46-page-2")
    assert second.ok, second.output
    assert [first.output.splitlines()[0], second.output.splitlines()[0]] == rows


@pytest.fixture(scope="module")
def sparse_discovery_tree():
    # 120 directories x 200 files = 24,000 files; a single notebook sits in the last directory.
    with tempfile.TemporaryDirectory(prefix="m46-sparse-") as directory:
        root = Path(directory).resolve()
        for group in range(120):
            folder = root / f"d{group:03}"
            folder.mkdir()
            for index in range(200):
                with (folder / f"{index:03}.py").open("xb"):
                    pass
        (root / "d119" / "zz.ipynb").write_bytes(b"needle\n")
        yield root


def _discovery_page(name, root, cursor, *, query):
    tool = FindFilesTool(root, max_matches=10) if name == "find_files" else SearchTextTool(root, max_matches=10)
    page = tool.execute({**query, "cursor": cursor})
    assert page.ok, page.output
    return page


_SPARSE_QUERIES = {
    "find_files": ({"pattern": "*.ipynb"}, ["d119/zz.ipynb"]),
    "search_text": ({"query": "needle", "literal": True, "file_glob": "*.ipynb"}, ["d119/zz.ipynb:1: needle"]),
}


@pytest.mark.timeout(120, func_only=True)
@pytest.mark.parametrize("name", sorted(_SPARSE_QUERIES))
def test_sparse_discovery_yields_strictly_advancing_pages_after_deadline(sparse_discovery_tree, monkeypatch, name):
    root = sparse_discovery_tree
    query, expected = _SPARSE_QUERIES[name]
    original_scandir = _filesystem_list.os.scandir
    elapsed = 0.0
    passes = 0

    def scandir(path):
        # Every completed directory selection pass exhausts the eight-second budget.
        nonlocal elapsed, passes
        passes += 1
        elapsed += 61
        return original_scandir(path)

    monkeypatch.setattr(_filesystem_list.os, "scandir", scandir)
    fake_clock = SimpleNamespace(monotonic=lambda: elapsed)
    for module in (find, search, _filesystem_list, _filesystem_helpers):
        monkeypatch.setattr(module, "time", fake_clock)
    monkeypatch.setattr(search.shutil, "which", lambda _name: None)
    cursor, rows, positions, pages = "", [], [], 0
    while True:
        passes = 0
        page = _discovery_page(name, root, cursor, query=query)
        pages += 1
        # Re-entering the root and the current directory is allowed; the rest of the tree is not walked.
        assert passes <= 3
        rows.extend(row for row in page.output.splitlines() if "zz.ipynb" in row)
        cursor = page.result_envelope["cursor"]
        if not cursor:
            assert not page.result_envelope["scan_incomplete"]
            break
        assert page.result_envelope["scan_incomplete"]
        state = json.loads(base64.urlsafe_b64decode(cursor))
        # Progress is the later of the processed position and the open candidate, then the byte offset.
        key = max(_filesystem_list._path_sort_key(state.get("scan_after", "")),
                  _filesystem_list._path_sort_key(state["candidate"]))
        positions.append((key, state.get("byte_offset", 0)))
        if not rows:
            assert state["after_entry"] == "" and state["phase"] == "discovery"
            assert "cursor=" in page.output
        assert pages <= 400
    assert rows == expected
    assert pages > 120
    assert positions == sorted(set(positions))


@pytest.mark.timeout(120, func_only=True)
@pytest.mark.parametrize("name", sorted(_SPARSE_QUERIES))
def test_negative_discovery_charges_filtered_entries_to_the_file_budget(sparse_discovery_tree, monkeypatch, name):
    root = sparse_discovery_tree
    query, _ = _SPARSE_QUERIES[name]
    query = {**query, "pattern": "*.none"} if name == "find_files" else {**query, "file_glob": "*.none"}
    fake_clock = SimpleNamespace(monotonic=lambda: 0.0)
    for module in (find, search, _filesystem_list, _filesystem_helpers):
        monkeypatch.setattr(module, "time", fake_clock)
    monkeypatch.setattr(search.shutil, "which", lambda _name: None)
    first = _discovery_page(name, root, "", query=query)
    assert first.result_envelope["scan_incomplete"]
    state = json.loads(base64.urlsafe_b64decode(first.result_envelope["cursor"]))
    # 20,000 examined entries stop the walk after about a hundred of the 120 directories.
    assert "d090/" <= state["scan_after"] < "d119/"
    second = _discovery_page(name, root, first.result_envelope["cursor"], query=query)
    assert not second.result_envelope["cursor"]
    assert not second.result_envelope["scan_incomplete"]


def test_zero_match_cursor_rejects_file_positions_without_a_candidate(tmp_path):
    (tmp_path / "a.txt").write_text("sample\n")
    (tmp_path / "b.txt").write_text("sample\n")
    tool = FindFilesTool(tmp_path, max_matches=1)
    first = tool.execute({"pattern": "*.txt", "cursor": ""})
    root_id = json.loads(base64.urlsafe_b64decode(first.result_envelope["cursor"]))["root_id"]
    token_state = {"v": 1, "kind": "find", "root_id": root_id, "query_sig": "x", "after_entry": "",
                   "candidate": "", "file_id": [], "phase": "discovery", "scan_after": "a/"}
    for broken in ({"byte_offset": 3}, {"scan_after": ""}, {"scan_after": "../a"}, {"file_id": [1, 2, 3, 4]}):
        token = base64.urlsafe_b64encode(json.dumps({**token_state, **broken}).encode()).decode()
        page = tool.execute({"pattern": "*.txt", "cursor": token})
        assert not page.ok and page.error_code == "TOOL_INVALID_ARGUMENTS", (broken, page.output)
