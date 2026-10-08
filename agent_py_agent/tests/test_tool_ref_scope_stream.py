"""E11e：冻结旧整读口径，逐条核对 scope/投影及压缩应用单扫描。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_py_agent.agent.common import tool_output_paths
from agent_py_agent.agent.common.json_io import jsonl_lines
from agent_py_agent.agent.memory_archive import compact_apply, control_plane
from agent_py_agent.agent.memory_archive import compact_tool_output_refs as refs

KEYS = ("conversation_request_id", "request_id", "run_id", "task_id")
VALUES = ("needle/a", "运行", " run-x ", True, 100.0, 1, [1], ["a"], {"x": "y"}, False, None, "")
SCOPES = [{key: str(value).strip()} for key in KEYS for value in VALUES]
SCOPES += [
    {}, {"ignored": "anything"}, {"run_id": []}, {"run_id": [None, " "]},
    {"run_id": ["needle/a", "运行"]}, {"run_id": ("needle/a", "运行")},
    {"run_id": {"needle/a", "运行"}}, {"run_id": frozenset({"needle/a", "运行"})},
    {"run_id": "needle/a", "request_id": "运行"},
    {"conversation_request_id": "needle/a", "run_id": "needle/a"},
    {"run_id": True, "task_id": "True"}, {"run_id": 100.0},
    {"run_id": "run-nested"}, {"request_id": "legacy-only"},
]


def index_row(root: Path, kind: str, value: object, call_id: str) -> dict:
    return {
        "kind": kind, **dict.fromkeys(KEYS, value), "tool": "read_file", "call_id": call_id,
        "path": str(root / f"{call_id}.txt"), "parameters": {"path": "host.txt", "run_id": "host"},
        "model_parameters": {"path": "model.txt"}, "ok": True, "size_bytes": 7,
        "padding": "正文\u0085\u2028\u2029不是记录边界",
    }


def write_index(root: Path, content: bytes) -> Path:
    path = root / "blobs/tool_outputs/index.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _scope_tree(root: Path) -> None:
    rows = [index_row(root, kind, value, f"{kind}-{number}")
            for number, value in enumerate(VALUES) for kind in ("tool_call", "tool_output")]
    rows += [
        {**index_row(root, "tool_output", "needle/a", "legacy"), "conversation_request_id": ""},
        {**index_row(root, "tool_output", "needle/a", "no-fallback"), "conversation_request_id": "other"},
        {**index_row(root, "tool_output", "needle/a", "ledger"), "tool": "task_progress"},
        {**index_row(root, "tool_call", "needle/a", "ledger-call"), "tool": "task_progress"},
        {**index_row(root, "tool_output", "needle/a", "no-path"), "path": ""},
        {"kind": "tool_output", "scope": {"run_id": "run-nested"}, "path": "missing.txt"},
        {"kind": "unrelated", "request_id": "legacy-only"},
    ]
    serialized = [json.dumps(row, ensure_ascii=number % 2 == 0).encode() for number, row in enumerate(rows)]
    write_index(root, b"\n\r\n{bad json}\n[]\nnull\n{}\n" + b"\r\n".join(serialized) + b"\r\n")
    escaped = json.dumps(index_row(root, "tool_output", "needle/a", "escaped")).replace(
        '"needle/a"', r'"\u006eeedle\/a"'
    ).encode()
    write_index(root / "runs/2026-10-08/z/work", escaped + b"\n")
    write_index(root / "tasks/2026-10-08/a/work", serialized[0])  # 最后一行没有 LF


def _old_matches(row: dict, scope: dict) -> bool:
    # 真正基线 9e843077d 的匹配口径冻结在测试里，不借用修改后的 predicate。
    for key in KEYS:
        expected = scope.get(key)
        if not expected:
            continue
        values = expected if isinstance(expected, (list, tuple, set, frozenset)) else (expected,)
        allowed = {str(item or "").strip() for item in values if str(item or "").strip()}
        actual = str(row.get(key) or "").strip()
        legacy = key == "conversation_request_id" and not actual and str(row.get("request_id") or "").strip() in allowed
        if allowed and actual not in allowed and not legacy:
            return False
    return True


def _old_index_rows(path: Path) -> list[dict]:
    # 保留旧整读、解析及错误合同，只拆平测试oracle的循环层级。
    rows = []
    for line in jsonl_lines(path.read_text(encoding="utf-8")):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value:
            rows.append(value)
    return rows


def old_source_refs(root: Path, scope: dict) -> dict:
    rows = []
    for path in tool_output_paths.tool_output_index_paths_for_lookup(root):
        if path.exists():
            rows.extend(_old_index_rows(path))
    return {
        "tool_calls": [refs._tool_call_ref(row) for row in rows
                       if refs._is_tool_call_row(row) and _old_matches(row, scope)],
        "tool_outputs": [refs._source_ref(row) for row in rows
                         if refs._is_tool_output_row(row) and _old_matches(row, scope)],
    }


def compact_plan(root: Path, scope: dict) -> dict:
    return {"workspace_root": str(root), "scope": scope, "archive": {"files": []},
            "snapshots": {"latest": []}, "tokens": {"latest": []}}


@pytest.mark.parametrize("scope", SCOPES)
def test_scope_outputs_equal_frozen_full_read(tmp_path: Path, scope: dict) -> None:
    _scope_tree(tmp_path)
    expected = old_source_refs(tmp_path, scope)
    assert refs.tool_output_source_refs(tmp_path, scope) == expected["tool_outputs"]
    assert refs.tool_call_source_refs(tmp_path, scope) == expected["tool_calls"]
    combined = compact_apply._source_refs(compact_plan(tmp_path, scope))
    assert {key: combined[key] for key in expected} == expected


@pytest.mark.parametrize("reader", [refs.tool_call_source_refs, refs.tool_output_source_refs])
def test_nonmatching_invalid_utf8_still_raises(tmp_path: Path, reader) -> None:
    write_index(tmp_path, b'{"run_id":"other"}\n\xff\n')
    with pytest.raises(UnicodeDecodeError):
        reader(tmp_path, {"run_id": "needle/a"})


def test_output_only_does_not_project_malformed_small_call(tmp_path: Path) -> None:
    row = {**index_row(tmp_path, "tool_call", "needle/a", "bad"), "size_bytes": "bad-int"}
    write_index(tmp_path, json.dumps(row).encode())
    assert refs.tool_output_source_refs(tmp_path, {"run_id": "needle/a"}) == []
    with pytest.raises(ValueError):
        refs.tool_call_source_refs(tmp_path, {"run_id": "needle/a"})


@pytest.mark.parametrize("mode", ["output", "call", "compact"])
def test_scope_reads_only_candidates_and_compact_scans_once(tmp_path: Path, monkeypatch, mode: str) -> None:
    noise = json.dumps({**index_row(tmp_path, "tool_output", "other", "noise"), "padding": "noise"}).encode()
    hit = json.dumps(index_row(tmp_path, "tool_output", "needle/a", "hit")).encode()
    paths = [write_index(tmp_path, noise + b"\n" + hit),
             write_index(tmp_path / "runs/2026-10-08/z/work", noise + b"\n"),
             write_index(tmp_path / "tasks/2026-10-08/a/work", hit)]
    stats = {"loads": 0, "lookups": 0, "opens": 0}
    real_loads, real_open = json.loads, Path.open
    real_roots = tool_output_paths.tool_output_roots_for_lookup

    def loads(text, *args, **kwargs):
        stats["loads"] += 1
        return real_loads(text, *args, **kwargs)

    def roots(root):
        stats["lookups"] += 1
        return real_roots(root)

    def open_path(path, *args, **kwargs):
        if path in paths:
            stats["opens"] += 1
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(json, "loads", loads)
    monkeypatch.setattr(Path, "open", open_path)
    monkeypatch.setattr(tool_output_paths, "tool_output_roots_for_lookup", roots)
    scope = {"run_id": "needle/a"}
    if mode == "compact":
        result = compact_apply._source_refs(compact_plan(tmp_path, scope))
        assert [item["call_id"] for item in result["tool_outputs"]] == ["hit", "hit"]
    else:
        reader = refs.tool_output_source_refs if mode == "output" else refs.tool_call_source_refs
        reader(tmp_path, scope)
    # 四行沿旧合同C解码后立即释放，业务预筛仍只有两个候选；不建立历史对象列表。
    assert stats == {"loads": 6, "lookups": 1, "opens": 3}


@pytest.mark.parametrize("query,actual", [("[1]", "[ 1 ]"), ("['a']", '["a"]')])
def test_shared_prescreen_keeps_composite_str_values(tmp_path: Path, query: str, actual: str) -> None:
    # str(JSON数组) 不等于原始 JSON 文本；共用安全判定不能静默漏掉这类旧记录。
    write_index(tmp_path, ('{"run_id":' + actual + '}\n').encode())
    batch = control_plane.query_tool_output_refs_for_runs(tmp_path, run_ids=(query,), limit_per_run=5)
    assert len(batch["tool_outputs_by_run"][query]) == 1


@pytest.mark.parametrize("separator", [b"\r", b"\r\n", b"\n"])
def test_legacy_universal_newlines_equal_old_read(tmp_path: Path, separator: bytes) -> None:
    lines = [json.dumps(index_row(tmp_path, kind, "needle/a", kind)).encode()
             for kind in ("tool_call", "tool_output")]
    write_index(tmp_path, separator.join(lines) + b"\r" + lines[0] + b"\r\n")
    expected = old_source_refs(tmp_path, {"run_id": "needle/a"})
    assert refs.tool_call_source_refs(tmp_path, {"run_id": "needle/a"}) == expected["tool_calls"]
    assert refs.tool_output_source_refs(tmp_path, {"run_id": "needle/a"}) == expected["tool_outputs"]
    combined = compact_apply._source_refs(compact_plan(tmp_path, {"run_id": "needle/a"}))
    assert {key: combined[key] for key in expected} == expected


@pytest.mark.parametrize("mode", ["call", "output", "compact"])
@pytest.mark.parametrize("late_file", [False, True])
def test_late_utf8_error_precedes_early_projection_error(tmp_path: Path, mode: str, late_file: bool) -> None:
    kind = "tool_call" if mode == "call" else "tool_output"
    row = {**index_row(tmp_path, kind, "needle/a", "bad"), "size_bytes": "bad-int"}
    write_index(tmp_path, json.dumps(row).encode() + (b"\n" if late_file else b"\n\xff\n"))
    if late_file:
        write_index(tmp_path / "runs/2026-10-08/bad/work", b"\xff\n")
    with pytest.raises(UnicodeDecodeError):
        old_source_refs(tmp_path, {"run_id": "needle/a"})
    with pytest.raises(UnicodeDecodeError):
        if mode == "compact":
            compact_apply._source_refs(compact_plan(tmp_path, {"run_id": "needle/a"}))
        else:
            reader = refs.tool_call_source_refs if mode == "call" else refs.tool_output_source_refs
            reader(tmp_path, {"run_id": "needle/a"})


def test_combined_projection_errors_keep_calls_before_outputs(tmp_path: Path) -> None:
    output = {**index_row(tmp_path, "tool_output", "needle/a", "output-bad"), "size_bytes": "bad-int"}
    call = {**index_row(tmp_path, "tool_call", "needle/a", "call-bad"), "size_bytes": {"bad": 1}}
    write_index(tmp_path, json.dumps(output).encode() + b"\n" + json.dumps(call).encode())
    with pytest.raises(TypeError):
        old_source_refs(tmp_path, {"run_id": "needle/a"})
    with pytest.raises(TypeError):
        compact_apply._source_refs(compact_plan(tmp_path, {"run_id": "needle/a"}))
