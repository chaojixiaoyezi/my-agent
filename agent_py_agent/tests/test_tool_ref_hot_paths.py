"""07 steer2：高频artifact/晋升/控制面逐条等价与非匹配行零loads。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_py_agent.agent.common.json_io import jsonl_lines, read_jsonl_objects_report
from agent_py_agent.agent.common.tool_output_paths import tool_output_index_paths_for_lookup
from agent_py_agent.agent.memory_archive import control_plane
from agent_py_agent.agent.memory_archive.artifact import reader
from agent_py_agent.agent.memory_store import promotion
from agent_py_agent.tests.test_tool_ref_scope_stream import write_index


def full_rows(root: Path) -> list[dict]:
    rows = []
    for path in tool_output_index_paths_for_lookup(root):
        rows.extend(file_rows(path))
    return rows


def file_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    result = []
    for text in jsonl_lines(path.read_text(encoding="utf-8")):
        try:
            row = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            result.append(row)
    return result


def old_find(root: Path, ref: str, request) -> dict | None:
    rows = full_rows(root)
    path = Path(ref).expanduser()
    resolved = path.resolve(strict=False) if path.is_absolute() or reader._looks_like_path(ref) else None
    matches = [row for row in rows if reader._record_matches_ref(row, ref, resolved)]
    scoped = reader._scoped_matches(matches, ref, request)
    if scoped:
        return scoped[-1]
    if matches and reader._request_has_scope(request):
        return matches[-1] if resolved is not None and not reader._request_has_strong_scope(request) else None
    if matches:
        return matches[-1]
    return reader._unique_record_by_basename(rows, path.name) if resolved is not None else None


def artifact_tree(root: Path) -> None:
    records = [
        {"call_id": "call-hit", "scoped_call_id": "run-a:call-hit", "run_id": "run-a", "path": "a/item.json", "sha256": "sha-hit"},
        {"call_id": "call-hit", "scoped_call_id": "run-b:call-hit", "run_id": "run-b", "path": "b/item.json"},
        {"call_id": "call-hit", "path": "legacy.json"},
        {"call_id": "转义/a", "run_id": "run-a", "path": "unique.json"},
        {"call_id": True, "path": "boolean.json"}, {"call_id": [1], "path": "array.json"},
    ]
    content = b"\r\n{bad}\nnull\n{}\n" + b"\r".join(json.dumps(row).encode() for row in records)
    write_index(root, content)
    write_index(root / "runs/2026-10-08/a/work", json.dumps(records[0]).encode())


@pytest.mark.parametrize("ref", ["call-hit", "run-a:call-hit", "sha-hit", "转义/a", "True", "[1]", "a/item.json", "item.json", "unique.json", "missing"])
@pytest.mark.parametrize("scope", [{}, {"run_id": "run-b"}, {"run_id": "missing", "task_id": "strong"}])
def test_artifact_find_equals_frozen_full_read(tmp_path: Path, ref: str, scope: dict) -> None:
    artifact_tree(tmp_path)
    request = reader.ReadToolOutputArtifactRequest(tmp_path, ref, **scope)
    assert reader._find_index_record(tmp_path, ref, request) == old_find(tmp_path, ref, request)


def old_archive_matches(root: Path, query: dict) -> list[dict]:
    matches = []
    for path in tool_output_index_paths_for_lookup(root):
        report = read_jsonl_objects_report(path)
        if not report.load_errors:
            matches.extend(row for row in report.records if archive_matches(row, query))
    return matches


def archive_matches(row: dict, query: dict) -> bool:
    operation = row.get("tool_operation")
    operation_id = str(operation.get("operation_id") or "").strip() if isinstance(operation, dict) else ""
    return all(str(row.get(key) or "").strip() == query[key] for key in ("run_id", "call_id", "tool")) and (
        not query["operation_id"] or operation_id == query["operation_id"]
    )


def query_archive(root: Path, query: dict) -> list[dict]:
    return promotion._owner_tool_archive_matches(root, promotion._ToolArchiveMatch(
        query["run_id"], query["call_id"], query["tool"], query["operation_id"]))


@pytest.mark.parametrize("value", ["run-hit", "转义/a", True, 100.0, [1], {"x": "y"}])
@pytest.mark.parametrize("bad", [b"", b"\n{bad unrelated}\n", b"\n[]\n", b"\nnull\n", b"\n\xff\n", b'\n{"run_id":"run-hit",bad}\n'])
def test_promotion_equals_old_including_whole_file_rejection(tmp_path: Path, value, bad: bytes) -> None:
    row = {"run_id": value, "call_id": "call-hit", "tool": "read_file", "tool_operation": {"operation_id": "op-hit"}}
    write_index(tmp_path, json.dumps(row).encode() + bad)
    write_index(tmp_path / "tasks/2026-10-08/good/work", json.dumps(row).encode())
    query = {"run_id": str(value), "call_id": "call-hit", "tool": "read_file", "operation_id": "op-hit"}
    assert query_archive(tmp_path, query) == old_archive_matches(tmp_path, query)


@pytest.mark.parametrize("options", [control_plane.MemoryControlPlaneQueryOptions(run_id="run-hit", limit=1),
                                   control_plane.MemoryControlPlaneQueryOptions(task_id="task-hit"),
                                   control_plane.MemoryControlPlaneQueryOptions(event_type="control_plane_decode_error"),
                                   control_plane.MemoryControlPlaneQueryOptions(limit=0)])
def test_general_control_plane_equals_old(tmp_path: Path, options) -> None:
    content = b'\n{bad}\r{"run_id":"run-hit","scope":{"task_id":"task-hit"}}\r\n[]\n{}\n'
    write_index(tmp_path, content)
    path = tool_output_index_paths_for_lookup(tmp_path)[0]
    old = [control_plane._decode_line(path, number, text)
           for number, text in enumerate(jsonl_lines(path.read_text()), 1) if text.strip()]
    expected = control_plane._limited(control_plane._matching_records(old, options), options.limit)
    assert control_plane.query_memory_control_plane(tmp_path, options)["tool_outputs"] == expected


@pytest.mark.parametrize("mode", ["artifact", "promotion", "control"])
def test_discarded_rows_use_c_decode_only_when_error_contract_requires_it(tmp_path: Path, monkeypatch, mode: str) -> None:
    noise = {"run_id": "other", "call_id": "other", "tool": "read_file", "path": "other.json"}
    hit = {"run_id": "run-hit", "call_id": "call-hit", "tool": "read_file", "path": "hit.json"}
    write_index(tmp_path, (json.dumps(noise) + "\n") .encode() * 40 + json.dumps(hit).encode())
    calls = []
    real = json.loads

    def loads(text, *args, **kwargs):
        calls.append(text)
        return real(text, *args, **kwargs)

    monkeypatch.setattr(json, "loads", loads)
    if mode == "artifact":
        request = reader.ReadToolOutputArtifactRequest(tmp_path, "call-hit")
        assert reader._find_index_record(tmp_path, "call-hit", request)["call_id"] == "call-hit"
    elif mode == "promotion":
        assert query_archive(tmp_path, {"run_id": "run-hit", "call_id": "call-hit", "tool": "read_file", "operation_id": ""}) == [hit]
    else:
        assert control_plane.query_memory_control_plane(tmp_path, control_plane.MemoryControlPlaneQueryOptions(run_id="run-hit"))["tool_outputs"] == [hit]
    # toolrefs-2明确改口径：路径错误/整文件合法性需要校验的入口C解码后立即丢弃；控制面仍可预筛。
    assert len(calls) == (42 if mode == "control" else 41)


def test_canonical_posix_index_path_does_not_build_interned_path_objects(tmp_path: Path, monkeypatch) -> None:
    import os
    if os.name != "posix":
        pytest.skip("Windows保留原生Path语义")
    calls, original = [], Path.resolve

    def resolve(path, *args, **kwargs):
        calls.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    assert not reader._record_matches_ref({"path": str(tmp_path / "noise.json")}, "hit", None)
    assert calls == []


@pytest.mark.parametrize("suffix", ["normal.json", "./normal.json", "dir/../normal.json", "embedded\0null", "loop"])
def test_posix_record_resolver_equals_original_path_contract(tmp_path: Path, suffix: str) -> None:
    if suffix == "loop":
        (tmp_path / "loop").symlink_to("loop")
    path = str(tmp_path) + "/" + suffix
    row = {"path": path, "call_id": "hit"}
    expected = _path_outcome(lambda: Path(path).expanduser().resolve(strict=False))
    actual = _path_outcome(lambda: reader._record_matches_ref(row, "hit", None))
    if expected[0] == "error":
        assert actual == expected
    else:
        assert actual == ("value", True)


def _path_outcome(call):
    try:
        return ("value", call())
    except Exception as exc:
        return ("error", type(exc).__name__, str(exc))


def test_invalid_ref_path_does_not_collect_whole_history(tmp_path: Path, monkeypatch) -> None:
    import gc
    import tracemalloc
    line = json.dumps({"run_id": "other", "padding": "x" * 2048}).encode() + b"\n"
    write_index(tmp_path, line * 2000)
    original = Path.expanduser

    def expand(path):
        if str(path) == "invalid-index-ref":
            raise RuntimeError("invalid query")
        return original(path)

    monkeypatch.setattr(Path, "expanduser", expand)
    gc.collect()
    tracemalloc.start()
    try:
        with pytest.raises(RuntimeError, match="invalid query"):
            reader._find_index_record(tmp_path, "invalid-index-ref", reader.ReadToolOutputArtifactRequest(tmp_path, "invalid-index-ref"))
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 3_000_000


@pytest.mark.parametrize("late_utf8", [False, True])
def test_badline_before_resource_error_keeps_original_report_priority(tmp_path: Path, late_utf8: bool) -> None:
    content = b'bad JSON\n{"integer":' + b'1' * 5000 + b'}\n' + (b'\xff\n' if late_utf8 else b'')
    path = write_index(tmp_path, content)
    query = promotion._ToolArchiveMatch("run-hit", "call-hit", "read_file", "")
    if late_utf8:
        expected = read_jsonl_objects_report(path)
        assert expected.load_errors and expected.records == []
        assert promotion._archive_file_matches(path, query, None) == []
        return
    with pytest.raises(ValueError):
        read_jsonl_objects_report(path)
    with pytest.raises(ValueError):
        promotion._archive_file_matches(path, query, None)


def test_path_alias_keeps_resolved_path_and_basename_fallback(tmp_path: Path) -> None:
    actual = tmp_path / "actual.json"
    alias = tmp_path / "alias.json"
    actual.write_text("{}")
    alias.symlink_to(actual)
    write_index(tmp_path, json.dumps({"path": str(actual), "call_id": "hit"}).encode())
    request = reader.ReadToolOutputArtifactRequest(tmp_path, str(alias))
    assert reader._find_index_record(tmp_path, str(alias), request) == old_find(tmp_path, str(alias), request)


def test_late_invalid_utf8_is_not_hidden_by_artifact_candidate(tmp_path: Path) -> None:
    write_index(tmp_path, b'{"call_id":"call-hit"}\n\xff\n')
    request = reader.ReadToolOutputArtifactRequest(tmp_path, "call-hit")
    with pytest.raises(UnicodeDecodeError):
        reader._find_index_record(tmp_path, "call-hit", request)


def test_dictionary_query_is_already_unsafe(tmp_path: Path) -> None:
    from agent_py_agent.agent.common.tool_index_stream import tool_index_prescreen_pattern
    value = {"a": "b"}
    assert tool_index_prescreen_pattern((str(value),)) is None
    write_index(tmp_path, json.dumps({"run_id": value, "call_id": "c", "tool": "t"}).encode())
    query = {"run_id": str(value), "call_id": "c", "tool": "t", "operation_id": ""}
    assert query_archive(tmp_path, query) == old_archive_matches(tmp_path, query)


def test_deep_bad_json_rejects_promotion_file(tmp_path: Path) -> None:
    write_index(tmp_path, ('{"x":' + '[' * 128 + '0' + ']' * 128 + ',}').encode())
    query = {"run_id": "run-hit", "call_id": "c", "tool": "t", "operation_id": ""}
    assert query_archive(tmp_path, query) == old_archive_matches(tmp_path, query) == []


@pytest.mark.parametrize("mode", ["promotion", "artifact"])
def test_unmatched_oversized_integer_keeps_decode_error(tmp_path: Path, mode: str) -> None:
    import sys
    limit = sys.get_int_max_str_digits()
    assert limit > 0
    text = '1' * (limit + 1)
    write_index(tmp_path, text.encode())
    query = {"run_id": "run-hit", "call_id": "c", "tool": "t", "operation_id": ""}
    request = reader.ReadToolOutputArtifactRequest(tmp_path, "needle")
    def old():
        return old_archive_matches(tmp_path, query) if mode == "promotion" else old_find(tmp_path, "needle", request)

    def new():
        return query_archive(tmp_path, query) if mode == "promotion" else reader._find_index_record(tmp_path, "needle", request)
    with pytest.raises(ValueError, match="integer string conversion"):
        old()
    with pytest.raises(ValueError, match="integer string conversion"):
        new()


def test_unmatched_symlink_loop_keeps_artifact_path_error(tmp_path: Path) -> None:
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    write_index(tmp_path, json.dumps({"call_id": "other", "path": str(loop)}).encode())
    request = reader.ReadToolOutputArtifactRequest(tmp_path, "needle")
    with pytest.raises(RuntimeError):
        old_find(tmp_path, "needle", request)
    with pytest.raises(RuntimeError):
        reader._find_index_record(tmp_path, "needle", request)


@pytest.mark.parametrize("path_value", ["~missing_toolrefs_user/a.json", "a\x00b", None, 10, {"a": "b"}])
def test_artifact_abnormal_path_matches_old_error_or_result(tmp_path: Path, path_value) -> None:
    write_index(tmp_path, json.dumps({"call_id": "other", "path": path_value}).encode())
    request = reader.ReadToolOutputArtifactRequest(tmp_path, "needle")
    try:
        expected = old_find(tmp_path, "needle", request)
    except (RuntimeError, ValueError) as exc:
        with pytest.raises(type(exc)):
            reader._find_index_record(tmp_path, "needle", request)
    else:
        assert reader._find_index_record(tmp_path, "needle", request) == expected


def test_integer_error_does_not_hide_late_utf8(tmp_path: Path) -> None:
    import sys
    text = '1' * (sys.get_int_max_str_digits() + 1) + '\n' + ' ' * 20000
    write_index(tmp_path, text.encode() + b'\xff\n')
    request = reader.ReadToolOutputArtifactRequest(tmp_path, "needle")
    with pytest.raises(UnicodeDecodeError):
        old_find(tmp_path, "needle", request)
    with pytest.raises(UnicodeDecodeError):
        reader._find_index_record(tmp_path, "needle", request)


def test_requested_path_error_does_not_hide_index_utf8(tmp_path: Path) -> None:
    write_index(tmp_path, b'\xff\n')
    ref = "~missing_toolrefs_user/a.json"
    request = reader.ReadToolOutputArtifactRequest(tmp_path, ref)
    with pytest.raises(UnicodeDecodeError):
        old_find(tmp_path, ref, request)
    with pytest.raises(UnicodeDecodeError):
        reader._find_index_record(tmp_path, ref, request)
