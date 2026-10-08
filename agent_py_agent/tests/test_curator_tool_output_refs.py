"""curator 工具输出引用批量读取器的等价、单遍扫描、内存与预筛测试。

来源：mc3-e11a 生产热修——旧 curator 路径对每个 run_id 各做一次全量控制面查询（每批重复
glob 全部索引根、整读整解析），批内 run 越多重复扫描越多。新读取器一次调用只 glob、只顺序
扫描一遍索引，逐行流式读取，按 run 集合收有界匹配行。
本文件验证：①与旧逐 run 路径输出逐条等价（含 CRLF/混合行尾索引）；②每 index 文件每批只打开
一次；③内存峰值有界；④字节预筛不改变结果（含转义、空格、非 ASCII run_id 与 ensure_ascii 两种写法）。
"""

from __future__ import annotations

import json
import tracemalloc
from pathlib import Path

import pytest

from agent_py_agent.agent.memory_archive import control_plane
from agent_py_agent.agent.memory_archive.control_plane import (
    MemoryControlPlaneQueryOptions,
    query_memory_control_plane,
    query_tool_output_refs_for_runs,
)
from agent_py_agent.agent.memory_store.curator_inputs import (
    CuratorAuditInput,
    CuratorToolReferenceSource,
    _bounded_tool_output_ref,
    _tool_reference_error,
)

# 批量读取器内存断言上限（32MB）：只约束新路径峰值（旧路径整读整解析必然远超此值）。
_MEMORY_LIMIT_BYTES = 32 * 1024 * 1024
# 内存测试规模：40 根 × 500 行 × 约 10KB ≈ 200MB 合成索引，只有少量匹配行。
_BULK_ROOT_COUNT = 40
_BULK_ROWS_PER_ROOT = 500
_BULK_PADDING_CHARS = 10_000
# 超长非匹配行（约 2MB）：验证峰值只跟最长单行加匹配行有关，不跟索引总量有关。
_OVERSIZE_PADDING_CHARS = 2_000_000


# 函数用途: 构造一条只带 run/call 身份的真实 CuratorAuditInput，供批量 read 使用。
def _audit_event(run_id: str, call_id: str) -> CuratorAuditInput:
    return CuratorAuditInput(
        event_id=f"evt-{run_id}-{call_id}",
        event_type="tool_call",
        created_at="2026-10-08T00:00:00+00:00",
        status="ok",
        operation_id="",
        tool_call_id=call_id,
        tool_name="run_command",
        tool_success=True,
        error_code="",
        effect_outcome="succeeded",
        session_id="",
        thread_id="",
        request_id="",
        task_id="",
        run_id=run_id,
        content_hash="",
        source_ref="",
        artifact_ref="",
        artifact_hash="",
        artifact_size_bytes=0,
        preview="",
    )


# 函数用途: 生成一条工具输出索引行（可切换 ensure_ascii 写法）。
def _tool_output_line(
    run_id: str,
    call_id: str,
    artifact_path: Path,
    *,
    ensure_ascii: bool = False,
) -> str:
    payload = {
        "kind": "tool_output",
        "call_id": call_id,
        "run_id": run_id,
        "path": str(artifact_path),
        "sha256": "a" * 64,
        "size_bytes": 42,
    }
    return json.dumps(payload, ensure_ascii=ensure_ascii) + "\n"


# 函数用途: 在 owner 树的相对位置写入一个 index.jsonl（自动建目录）。
def _write_index(root: Path, relative: str, content: str) -> Path:
    path = root / relative / "index.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# 函数用途: 批量读取器的真实入口包装（与 core.py 接线相同）。
def _batch_query(owner_root: Path, run_ids: tuple[str, ...], limit: int) -> dict[str, object]:
    return query_tool_output_refs_for_runs(owner_root, run_ids=run_ids, limit_per_run=limit)


# LLM: 参照实现只存在于测试里——复刻旧版逐 run 查询 + 逐行转换的完整行为，用来证明批量读取器
#   输出与旧路径逐条相同；产品代码不保留旧分支。
# 函数用途: 旧版逐 run 全量查询加逐行转换的测试参照。
def _legacy_read(
    owner_root: Path,
    events: tuple[CuratorAuditInput, ...],
    *,
    limit_per_run: int,
) -> tuple[dict[tuple[str, str], dict[str, object]], list[dict[str, object]]]:
    refs: dict[tuple[str, str], dict[str, object]] = {}
    errors: list[dict[str, object]] = []
    run_ids = sorted({event.run_id for event in events if event.run_id and event.tool_call_id})
    for run_id in run_ids:
        run_refs, run_errors = _legacy_read_run(owner_root, run_id, limit_per_run)
        refs.update(run_refs)
        errors.extend(run_errors)
    return refs, errors


# 函数用途: 参照版单个 run 的全量查询与截断（旧 read 循环体上半段）。
def _legacy_read_run(
    owner_root: Path,
    run_id: str,
    limit_per_run: int,
) -> tuple[dict[tuple[str, str], dict[str, object]], list[dict[str, object]]]:
    refs: dict[tuple[str, str], dict[str, object]] = {}
    errors: list[dict[str, object]] = []
    try:
        result = query_memory_control_plane(
            owner_root,
            MemoryControlPlaneQueryOptions(
                run_id=run_id,
                limit=max(1, min(2_048, int(limit_per_run))),
            ),
        )
    except (OSError, UnicodeError, ValueError) as exc:
        errors.append(_tool_reference_error(exc, run_id=run_id))
        return refs, errors
    if result.get("ok") is not True:
        errors.append(_tool_reference_error(ValueError("control plane failed"), run_id=run_id))
        return refs, errors
    rows = result.get("tool_outputs")
    if not isinstance(rows, list):
        errors.append(_tool_reference_error(ValueError("invalid tool_outputs"), run_id=run_id))
        return refs, errors
    for row in rows:
        ref, error = _legacy_read_row(row, run_id, owner_root)
        if error is not None:
            errors.append(error)
            continue
        if ref:
            refs[(run_id, str(ref["tool_call_id"]))] = ref
    return refs, errors


# 函数用途: 参照版单行的转换与错误投影（旧 read 循环体下半段）。
def _legacy_read_row(
    row: object,
    run_id: str,
    owner_root: Path,
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    if not isinstance(row, dict):
        return None, _tool_reference_error(ValueError("invalid tool output row"), run_id=run_id)
    if str(row.get("kind") or "") == "control_plane_decode_error":
        return None, {
            "context": "memory_curator.tool_reference_read",
            "error_code": "TOOL_REFERENCE_READ_FAILED",
            "error_type": "InvalidJsonlRow",
            "run_id": run_id,
            "line": int(row.get("line_number") or 0),
        }
    try:
        ref = _bounded_tool_output_ref(row, owner_root=owner_root, run_id=run_id)
    except (OSError, ValueError) as exc:
        return None, _tool_reference_error(exc, run_id=run_id)
    return (ref or None), None


# LLM: 合成多根 owner 树覆盖全部查找位置：顶层 blobs 根、runs/*/*/work、tasks/*/*/work，
#   外加"目录存在但没有 index.jsonl"的根；行里混入坏 JSON、空行与越界 artifact 路径。
# 函数用途: 建一棵供等价对照使用的多根 owner 工具输出索引树。
def _build_equivalence_tree(root: Path) -> None:
    inside = root / "blobs" / "tool_outputs" / "artifact-a.txt"
    override = root / "blobs" / "tool_outputs" / "artifact-override.txt"
    outside = root.parent / "outside-owner.txt"
    _write_index(
        root,
        "blobs/tool_outputs",
        _tool_output_line("run-a", "call-a1", inside)
        + _tool_output_line("run-b", "call-b1", inside)
        + "{not-json\n"
        + "\n"
        + _tool_output_line("run-a", "call-a2", inside),
    )
    _write_index(
        root,
        "runs/2026-10-08/aaaa/work/blobs/tool_outputs",
        _tool_output_line("run-a", "call-a1", override)
        + _tool_output_line("run-b", "call-b2", inside),
    )
    _write_index(
        root,
        "runs/2026-10-08/bbbb/work/blobs/tool_outputs",
        _tool_output_line("run-c", "call-c1", inside)
        + _tool_output_line("run-other", "call-x1", inside),
    )
    _write_index(
        root,
        "tasks/2026-10-08/cccc/work/blobs/tool_outputs",
        _tool_output_line("run-c", "call-c2", outside)
        + _tool_output_line("run-b", "call-b3", inside),
    )
    (root / "runs/2026-10-08/dddd/work/blobs/tool_outputs").mkdir(parents=True, exist_ok=True)


# LLM: 逐 run 对照必须覆盖"截断前 N 条"和"limit<=0 不截断"两种语义，以及多个 run 在多个根
#   交错出现的顺序；行列表逐条相等才算等价（不比较 dict 身份）。
# 函数用途: 验证批量读取器每个 run 的行列表与旧逐 run 全量查询逐条相同。
@pytest.mark.parametrize("limit", [0, 1, 2, 10])
def test_batch_reader_matches_per_run_queries(tmp_path: Path, limit: int) -> None:
    _build_equivalence_tree(tmp_path)
    run_ids = ("run-a", "run-b", "run-c", "run-missing")
    batch = query_tool_output_refs_for_runs(tmp_path, run_ids=run_ids, limit_per_run=limit)
    assert batch["ok"] is True
    rows_by_run = batch["tool_outputs_by_run"]
    assert sorted(rows_by_run) == sorted(run_ids)
    for run_id in run_ids:
        legacy = query_memory_control_plane(
            tmp_path,
            MemoryControlPlaneQueryOptions(run_id=run_id, limit=limit),
        )
        assert rows_by_run[run_id] == legacy["tool_outputs"]


# LLM: read 层对照覆盖 clamp 边界（0 和 2500）、正常截断、以及 int() 失败时"整批每个 run 各得
#   一条同类型错误"的旧行为；refs 的覆盖顺序也随 limit 变化（截断后重复 call 只剩前一条）。
# 函数用途: 验证批量 read 与旧逐 run read 参照的 refs 与 errors 逐条相同。
@pytest.mark.parametrize("limit_per_run", [0, 2, 2_500, "abc"])
def test_batch_read_matches_legacy_read(tmp_path: Path, limit_per_run: object) -> None:
    _build_equivalence_tree(tmp_path)
    events = (
        _audit_event("run-a", "call-a1"),
        _audit_event("run-b", "call-b1"),
        _audit_event("run-c", "call-c1"),
        _audit_event("run-missing", "call-x1"),
    )
    source = CuratorToolReferenceSource(tmp_path, query=_batch_query)
    new_refs, new_errors = source.read(events, limit_per_run=limit_per_run)  # type: ignore[arg-type]
    legacy_refs, legacy_errors = _legacy_read(tmp_path, events, limit_per_run=limit_per_run)  # type: ignore[arg-type]
    assert new_refs == legacy_refs
    assert new_errors == legacy_errors


# 函数用途: 验证重复 call 行在未截断时按行序后者覆盖前者，与旧路径一致。
def test_batch_read_later_rows_override_earlier(tmp_path: Path) -> None:
    _build_equivalence_tree(tmp_path)
    events = (_audit_event("run-a", "call-a1"),)
    source = CuratorToolReferenceSource(tmp_path, query=_batch_query)
    refs, errors = source.read(events, limit_per_run=10)
    legacy_refs, legacy_errors = _legacy_read(tmp_path, events, limit_per_run=10)
    assert refs == legacy_refs
    assert errors == legacy_errors
    assert refs[("run-a", "call-a1")]["artifact_ref"] == str(
        (tmp_path / "blobs" / "tool_outputs" / "artifact-override.txt").resolve()
    )


# 函数用途: 验证索引含非法 UTF-8 字节时，批量读取器与旧路径同样失败、read 层同样每 run 一条错误。
def test_non_utf8_index_fails_like_legacy(tmp_path: Path) -> None:
    _write_index(
        tmp_path,
        "runs/2026-10-08/aaaa/work/blobs/tool_outputs",
        _tool_output_line("run-a", "call-a1", tmp_path / "blobs/tool_outputs/artifact.txt"),
    )
    bad_dir = tmp_path / "runs/2026-10-08/bbbb/work/blobs/tool_outputs"
    bad_dir.mkdir(parents=True, exist_ok=True)
    (bad_dir / "index.jsonl").write_bytes(
        b'{"kind": "tool_output", "run_id": "run-a"}\n\xff\xfe bad bytes\n'
    )

    with pytest.raises(UnicodeDecodeError):
        query_tool_output_refs_for_runs(tmp_path, run_ids=("run-a",), limit_per_run=5)
    with pytest.raises(UnicodeDecodeError):
        query_memory_control_plane(tmp_path, MemoryControlPlaneQueryOptions(run_id="run-a", limit=5))

    events = (_audit_event("run-a", "call-a1"),)
    source = CuratorToolReferenceSource(tmp_path, query=_batch_query)
    new_refs, new_errors = source.read(events, limit_per_run=5)
    legacy_refs, legacy_errors = _legacy_read(tmp_path, events, limit_per_run=5)
    assert new_refs == {} and legacy_refs == {}
    assert new_errors == legacy_errors
    assert [item["error_type"] for item in new_errors] == ["UnicodeDecodeError"]


# LLM: 打开次数用 Path.open 计数（新读取器走 path.open("rb")，旧路径 read_text 内部也走
#   Path.open）；对照同一棵树上"5 个 run 的批量调用 = 每文件 1 次"与"3 次逐 run 查询 = 每文件 3 次"。
# 函数用途: 验证每个索引文件每批只被打开一次，打开次数不随 run 数增长。
def test_batch_reader_scans_each_index_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _build_equivalence_tree(tmp_path)
    index_paths = [
        path for path in control_plane.tool_output_index_paths_for_lookup(tmp_path) if path.exists()
    ]
    assert len(index_paths) == 4

    opened: list[str] = []
    real_open = Path.open

    def counting_open(self: Path, *args: object, **kwargs: object):
        if self.name == "index.jsonl":
            opened.append(str(self))
        return real_open(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "open", counting_open)

    lookups: list[str] = []
    real_lookup = control_plane.tool_output_index_paths_for_lookup

    def counting_lookup(root: object) -> object:
        lookups.append(str(root))
        return real_lookup(root)  # type: ignore[arg-type]

    monkeypatch.setattr(control_plane, "tool_output_index_paths_for_lookup", counting_lookup)

    batch = query_tool_output_refs_for_runs(
        tmp_path,
        run_ids=("run-a", "run-b", "run-c", "run-missing", "run-other"),
        limit_per_run=10,
    )
    assert batch["ok"] is True
    assert len(lookups) == 1
    assert sorted(opened) == sorted(str(path) for path in index_paths)
    assert len(opened) == len(index_paths)

    opened.clear()
    for run_id in ("run-a", "run-b", "run-c"):
        query_memory_control_plane(tmp_path, MemoryControlPlaneQueryOptions(run_id=run_id, limit=10))
    legacy_counts = {path: opened.count(path) for path in set(opened)}
    assert set(legacy_counts) == {str(path) for path in index_paths}
    assert all(count == 3 for count in legacy_counts.values())


# 函数用途: 验证预筛条件：只有全为 JSON 不转义的可见 ASCII 才生成字节串，否则整行解析。
def test_prescreen_needles_skip_unsafe_run_ids() -> None:
    assert control_plane._tool_ref_prescreen_needles(("run-safe",)) == (b"run-safe",)
    assert control_plane._tool_ref_prescreen_needles(("run safe",)) == ()
    assert control_plane._tool_ref_prescreen_needles(('run"quote',)) == ()
    assert control_plane._tool_ref_prescreen_needles(("run\\backslash",)) == ()
    assert control_plane._tool_ref_prescreen_needles(("运行-任务-1",)) == ()
    assert control_plane._tool_ref_prescreen_needles(()) == ()


# LLM: ensure_ascii 两种写法各测一遍：安全 ASCII 逐字节出现；引号/反斜杠会被转义成 \" 与 \\，
#   非 ASCII 会被写成 \uXXXX，原始字节串不出现——这类 run_id 必须整行解析才能找到。
# 函数用途: 验证含转义字符/空格/非 ASCII 的 run_id 在两种 JSON 写法下都能找到且与旧路径一致。
@pytest.mark.parametrize(
    "run_id", ["run-safe-1", 'run"quote', "run\\backslash", "运行-任务-1", "run space"]
)
def test_escaped_or_non_ascii_run_ids_still_match(tmp_path: Path, run_id: str) -> None:
    artifact = tmp_path / "blobs" / "tool_outputs" / "artifact.txt"
    for ensure_ascii in (False, True):
        content = _tool_output_line(run_id, "call-1", artifact, ensure_ascii=ensure_ascii)
        _write_index(tmp_path, "blobs/tool_outputs", content)
        batch = query_tool_output_refs_for_runs(tmp_path, run_ids=(run_id,), limit_per_run=5)
        legacy = query_memory_control_plane(
            tmp_path,
            MemoryControlPlaneQueryOptions(run_id=run_id, limit=5),
        )
        rows = batch["tool_outputs_by_run"][run_id]
        assert rows == legacy["tool_outputs"]
        assert len(rows) == 1
        assert rows[0]["run_id"] == run_id


# 函数用途: 验证 run_id 出现在 scope 嵌套位置时照常匹配（复用 _record_value 的 scope 分支）。
def test_scope_nested_run_id_still_matches(tmp_path: Path) -> None:
    artifact = tmp_path / "blobs" / "tool_outputs" / "artifact.txt"
    line = (
        json.dumps(
            {
                "kind": "tool_output",
                "call_id": "call-scope",
                "scope": {"run_id": "run-scope"},
                "path": str(artifact),
                "sha256": "b" * 64,
                "size_bytes": 5,
            }
        )
        + "\n"
    )
    _write_index(tmp_path, "blobs/tool_outputs", line)
    batch = query_tool_output_refs_for_runs(tmp_path, run_ids=("run-scope",), limit_per_run=5)
    legacy = query_memory_control_plane(
        tmp_path,
        MemoryControlPlaneQueryOptions(run_id="run-scope", limit=5),
    )
    rows = batch["tool_outputs_by_run"]["run-scope"]
    assert rows == legacy["tool_outputs"]
    assert len(rows) == 1


# 函数用途: 验证预筛字节命中但结构不匹配的行不被收集（预筛只是性能提示，不是判断依据）。
def test_prescreen_hit_without_structural_match_is_not_collected(tmp_path: Path) -> None:
    artifact = tmp_path / "blobs" / "tool_outputs" / "artifact.txt"
    line = (
        json.dumps(
            {
                "kind": "tool_output",
                "call_id": "call-mixed",
                "run_id": "run-b",
                "path": str(artifact),
                "sha256": "c" * 64,
                "size_bytes": 3,
                "note": "mentions run-a here",
            }
        )
        + "\n"
    )
    _write_index(tmp_path, "blobs/tool_outputs", line)
    batch = query_tool_output_refs_for_runs(tmp_path, run_ids=("run-a",), limit_per_run=5)
    legacy = query_memory_control_plane(
        tmp_path,
        MemoryControlPlaneQueryOptions(run_id="run-a", limit=5),
    )
    assert batch["tool_outputs_by_run"]["run-a"] == legacy["tool_outputs"] == []


# 函数用途: 写一个根的合成索引行（0 号根含超长非匹配行），返回该根字节数。
def _write_bulk_root(tmp_path: Path, index: int, padding: str) -> int:
    path = tmp_path / f"runs/2026-10-08/root{index:03d}/work/blobs/tool_outputs/index.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [_bulk_line(tmp_path, index, row, padding) for row in range(_BULK_ROWS_PER_ROOT)]
    if index == 0:
        lines.append(_oversize_line(tmp_path))
    content = "".join(lines)
    path.write_text(content, encoding="utf-8")
    return len(content.encode("utf-8"))


# 函数用途: 生成一条约 10KB 的合成工具输出行；每 10 个根的第一行是匹配 run-hit。
def _bulk_line(tmp_path: Path, index: int, row: int, padding: str) -> str:
    run_value = "run-hit" if index % 10 == 0 and row == 0 else f"run-bulk-{row % 7}"
    return (
        json.dumps(
            {
                "kind": "tool_output",
                "call_id": f"call-{index}-{row}",
                "run_id": run_value,
                "path": str(tmp_path / "blobs" / "tool_outputs" / f"artifact-{index}-{row}.txt"),
                "sha256": "d" * 64,
                "size_bytes": 7,
                "padding": padding,
            }
        )
        + "\n"
    )


# 函数用途: 生成约 2MB 的超长非匹配行（含 run-hit 字节串，验证预筛命中但不收集且峰值有界）。
def _oversize_line(tmp_path: Path) -> str:
    return (
        json.dumps(
            {
                "kind": "tool_output",
                "call_id": "call-oversize",
                "run_id": "run-bulk-oversize",
                "path": str(tmp_path / "blobs" / "tool_outputs" / "artifact-oversize.txt"),
                "sha256": "e" * 64,
                "size_bytes": 9,
                "padding": "y" * _OVERSIZE_PADDING_CHARS + " mentions run-hit",
            }
        )
        + "\n"
    )


# LLM: 规模按任务书"约 200MB"设计：40 根 × 500 行 × 约 10KB，另加一条 2MB 超长非匹配行。
#   tracemalloc 分别测两条路径峰值：新路径只保留匹配行加单行缓冲；旧路径整读整解析全部行。
# 函数用途: 验证批量读取器峰值低于 32MB 常量上限，且远低于旧整读路径的实测峰值。
def test_batch_reader_memory_stays_bounded(tmp_path: Path) -> None:
    padding = "x" * _BULK_PADDING_CHARS
    total_bytes = sum(
        _write_bulk_root(tmp_path, index, padding) for index in range(_BULK_ROOT_COUNT)
    )
    assert total_bytes > 180 * 1024 * 1024

    tracemalloc.start()
    tracemalloc.reset_peak()
    batch = query_tool_output_refs_for_runs(tmp_path, run_ids=("run-hit",), limit_per_run=10)
    _, peak_new = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert len(batch["tool_outputs_by_run"]["run-hit"]) == 4
    assert peak_new < _MEMORY_LIMIT_BYTES, f"批量读取峰值 {peak_new} 字节超过上限"

    tracemalloc.start()
    tracemalloc.reset_peak()
    legacy = query_memory_control_plane(
        tmp_path,
        MemoryControlPlaneQueryOptions(run_id="run-hit", limit=10),
    )
    _, peak_old = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert len(legacy["tool_outputs"]) == 4
    assert peak_old > _MEMORY_LIMIT_BYTES, f"旧路径峰值 {peak_old} 字节低于预期（数据未生效？）"
    assert peak_old > peak_new
    print(f"\n[memory] batch_peak={peak_new} legacy_peak={peak_old} index_bytes={total_bytes}")


# LLM: 真实写入器在 Windows 上以文本模式追加 \n，落盘是 CRLF（跨平台同步后还会出现 CRLF/LF 混合
#   行尾）；旧 read_text 的 universal newlines 与新路径"行尾去一个 \r"必须在这两种格式上逐条等价。
#   第 3 行是含 run-a 字节的坏 JSON：两路径都应把它过滤掉，而第 4 行照常找到（行边界处理正确）。
# 函数用途: 验证 CRLF 与混合行尾索引文件在两路径下结果逐条等价、截断语义一致。
def test_crlf_written_index_matches_legacy(tmp_path: Path) -> None:
    artifact = tmp_path / "blobs" / "tool_outputs" / "artifact.txt"
    path = tmp_path / "blobs/tool_outputs/index.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        _tool_output_line("run-a", "call-a1", artifact).replace("\n", "\r\n").encode("utf-8")
        + _tool_output_line("run-other", "call-x1", artifact).encode("utf-8")
        + b'{"run_id": "run-a", broken\r\n'
        + _tool_output_line("run-a", "call-a2", artifact).replace("\n", "\r\n").encode("utf-8")
    )

    batch = query_tool_output_refs_for_runs(tmp_path, run_ids=("run-a",), limit_per_run=5)
    legacy = query_memory_control_plane(
        tmp_path,
        MemoryControlPlaneQueryOptions(run_id="run-a", limit=5),
    )
    rows = batch["tool_outputs_by_run"]["run-a"]
    assert rows == legacy["tool_outputs"]
    assert [row["call_id"] for row in rows] == ["call-a1", "call-a2"]

    truncated = query_tool_output_refs_for_runs(tmp_path, run_ids=("run-a",), limit_per_run=1)
    assert [row["call_id"] for row in truncated["tool_outputs_by_run"]["run-a"]] == ["call-a1"]
