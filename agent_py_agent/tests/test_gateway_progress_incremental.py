# LLM: E12 增量读取的回归测试：先在"完整行流"上把行号协议与公开投影钉回原语义（对照未改动的
#   jsonl_lines 整读切片逐项全等，覆盖 since=0/中间/末尾/超末尾），再为快路径、增量扫描、
#   截断/替换重建、半行不消费、索引上限、超长行与空轮询内存各锁一条用例。对比用的旧实现就是
#   仍然存在的 json_io.jsonl_lines，不复制投影逻辑进测试。
# 模块用途: 验证 progress_reader 增量读取在行为等价前提下满足性能与边界要求。
from __future__ import annotations

import json
import os
import tracemalloc

import pytest

from agent_py_agent.agent.common.json_io import jsonl_lines
from agent_py_agent.agent.gateway_parts import progress_reader
from agent_py_agent.agent.gateway_parts.http_handlers import _read_public_progress_events
from agent_py_agent.agent.gateway_parts.progress_reader import read_progress_lines


@pytest.fixture(autouse=True)
def _clear_progress_index_cache():
    # 索引是纯缓存：每个用例前后清空，避免跨用例互相影响（与产品"可重建"语义一致）。
    progress_reader._INDEXES.clear()
    yield
    progress_reader._INDEXES.clear()


def _row(kind: str, **extra: object) -> bytes:
    return json.dumps({"kind": kind, **extra}, ensure_ascii=False).encode("utf-8") + b"\n"


def _commentary_text(text: str) -> str:
    return json.dumps({"kind": "assistant_commentary", "text": text}, ensure_ascii=False)


def _counting_reader(counter: dict[str, int]):
    # 包装正文读取入口：统计一次调用真正读到的字节数（快路径应为 0）。
    original = progress_reader._read_line_bytes

    def _read(f, limit):
        data = original(f, limit)
        counter["bytes"] += len(data)
        return data

    return _read


def _sample_stream() -> bytes:
    # 12 行完整记录（全部以 LF 结尾）：空行、坏行、被过滤的行、CRLF、多字节、未知 kind 都在内。
    return b"".join(
        [
            _row("assistant_commentary", text="开始处理"),
            b"\n",
            b"not-json\n",
            _row("tool_progress", verbose_level="on", progress={"tool": "read_file", "status": "完成"}),
            _row("tool_progress", verbose_level="off", progress={"tool": "read_file", "status": "完成"}),
            _row(
                "tool_progress",
                verbose_level="full",
                progress={"tool": "run_command", "status": "完成", "output": "ok"},
            ),
            _row("turn_resumed", reason="gateway_safe_restart"),
            _row(
                "permission_requested",
                permission={"tool_name": "run_command", "description": "执行 ls"},
            ),
            _commentary_text("中文😀").encode("utf-8") + b"\r\n",
            _row("assistant_commentary", text="   "),
            _row("tool_progress", verbose_level="on", progress="not-a-dict"),
            _row("unknown_kind", x=1),
        ]
    )


@pytest.mark.parametrize("since", [0, 3, 11, 12, 99])
def test_read_matches_legacy_slice_on_complete_rows(tmp_path, since: int) -> None:
    path = tmp_path / "req.chunks.jsonl"
    path.write_bytes(_sample_stream())
    legacy = jsonl_lines(path.read_text(encoding="utf-8"))
    assert len(legacy) == 12

    read_progress_lines(path, 0)  # 预热：模拟此前的轮询已经建好索引

    lines, next_cursor = read_progress_lines(path, since)
    assert lines == tuple(legacy[since : since + 200])
    assert next_cursor == min(len(legacy), since + 200)
    # 同一 since 再读一次（旧区/快路径分支）必须稳定一致。
    assert read_progress_lines(path, since) == (lines, next_cursor)


def test_public_events_keep_whitelist_and_cursor_semantics(tmp_path) -> None:
    path = tmp_path / "req.chunks.jsonl"
    path.write_bytes(_sample_stream())

    events, cursor = _read_public_progress_events(path, 0)

    assert cursor == 12
    assert events == [
        {"kind": "assistant_commentary", "text": "开始处理"},
        {"level": "on", "tool": "read_file", "status": "完成"},
        {"level": "full", "tool": "run_command", "status": "完成", "output": "ok"},
        {"kind": "permission_requested", "tool": "run_command", "summary": "执行 ls"},
        {"kind": "assistant_commentary", "text": "中文😀"},
    ]
    # since 指向被过滤的行（第 4 行 off）：该行不产出事件，但行号照常计入（协议核心）。
    events_mid, cursor_mid = _read_public_progress_events(path, 4)
    assert cursor_mid == 12
    assert events_mid == [
        {"level": "full", "tool": "run_command", "status": "完成", "output": "ok"},
        {"kind": "permission_requested", "tool": "run_command", "summary": "执行 ls"},
        {"kind": "assistant_commentary", "text": "中文😀"},
    ]


def test_eof_fast_path_does_not_read_body_bytes(tmp_path, monkeypatch) -> None:
    path = tmp_path / "req.chunks.jsonl"
    path.write_bytes(_sample_stream())
    counter = {"bytes": 0}
    monkeypatch.setattr(progress_reader, "_read_line_bytes", _counting_reader(counter))

    lines, next_cursor = read_progress_lines(path, 0)
    assert len(lines) == 12 and next_cursor == 12

    counter["bytes"] = 0
    lines_again, next_again = read_progress_lines(path, next_cursor)
    assert counter["bytes"] == 0
    assert (lines_again, next_again) == ((), next_cursor)


def test_append_reads_only_the_new_bytes(tmp_path, monkeypatch) -> None:
    path = tmp_path / "req.chunks.jsonl"
    path.write_bytes(_row("assistant_commentary", text="one"))
    _, next_cursor = read_progress_lines(path, 0)
    assert next_cursor == 1

    appended = _row("assistant_commentary", text="two") + _row("assistant_commentary", text="three")
    with path.open("ab") as handle:
        handle.write(appended)

    counter = {"bytes": 0}
    monkeypatch.setattr(progress_reader, "_read_line_bytes", _counting_reader(counter))
    lines, next_after = read_progress_lines(path, next_cursor)

    assert lines == (
        _commentary_text("two"),
        _commentary_text("three"),
    )
    assert counter["bytes"] == len(appended)
    assert next_after == 3


def test_window_merges_old_range_and_appended_rows(tmp_path) -> None:
    path = tmp_path / "req.chunks.jsonl"
    path.write_bytes(b"".join(_row("assistant_commentary", text=f"old-{i}") for i in range(5)))
    read_progress_lines(path, 0)  # 建索引：complete=5
    with path.open("ab") as handle:
        handle.write(_row("assistant_commentary", text="new-5"))

    lines, next_cursor = read_progress_lines(path, 3)

    assert lines == (
        _commentary_text("old-3"),
        _commentary_text("old-4"),
        _commentary_text("new-5"),
    )
    assert next_cursor == 6


def test_window_is_capped_at_200_rows(tmp_path) -> None:
    path = tmp_path / "req.chunks.jsonl"
    path.write_bytes(b"".join(_row("assistant_commentary", text=f"r{i}") for i in range(250)))

    lines, next_cursor = read_progress_lines(path, 0)
    assert len(lines) == 200 and next_cursor == 200

    lines2, next2 = read_progress_lines(path, 200)
    assert len(lines2) == 50 and next2 == 250


def test_truncated_file_discards_index_and_rebuilds(tmp_path) -> None:
    path = tmp_path / "req.chunks.jsonl"
    path.write_bytes(_row("assistant_commentary", text="old") * 5)
    _, next_cursor = read_progress_lines(path, 0)
    assert next_cursor == 5

    shorter = _row("assistant_commentary", text="new-a") + _row("assistant_commentary", text="new-b")
    path.write_bytes(shorter)

    lines, next_after = read_progress_lines(path, 0)
    assert lines == (_commentary_text("new-a"), _commentary_text("new-b"))
    assert next_after == 2
    # 旧游标越过新末尾：快路径返回空且 next 不越界。
    assert read_progress_lines(path, next_cursor) == ((), 2)


def test_same_length_replacement_discards_index(tmp_path) -> None:
    path = tmp_path / "req.chunks.jsonl"
    # 同长度但行数不同：只有"核对文件身份（inode）后重建"才能给出正确的行数与游标。
    original = b"row-A\nrow-B\n"
    path.write_bytes(original)
    lines, next_cursor = read_progress_lines(path, 0)
    assert (lines, next_cursor) == (("row-A", "row-B"), 2)
    inode_before = os.stat(path).st_ino

    replacement = b"row-CCCCCCC\n"
    assert len(replacement) == len(original)
    staging = tmp_path / "staging.chunks.jsonl"
    staging.write_bytes(replacement)
    os.replace(staging, path)
    assert os.stat(path).st_ino != inode_before

    lines2, next2 = read_progress_lines(path, 0)
    assert (lines2, next2) == (("row-CCCCCCC",), 1)


def test_larger_replacement_discards_index(tmp_path) -> None:
    path = tmp_path / "req.chunks.jsonl"
    path.write_bytes(_row("assistant_commentary", text="small"))
    _, next_cursor = read_progress_lines(path, 0)
    assert next_cursor == 1

    bigger = b"".join(_row("assistant_commentary", text=f"x{i}") for i in range(3))
    staging = tmp_path / "staging.chunks.jsonl"
    staging.write_bytes(bigger)
    os.replace(staging, path)

    lines, next_after = read_progress_lines(path, 0)
    assert lines == (_commentary_text("x0"), _commentary_text("x1"), _commentary_text("x2"))
    assert next_after == 3


def test_partial_tail_is_not_consumed_until_newline_arrives(tmp_path) -> None:
    path = tmp_path / "req.chunks.jsonl"
    first = _row("assistant_commentary", text="one")
    second = _row("assistant_commentary", text="two")
    path.write_bytes(first + second[:-1])

    lines, next_cursor = read_progress_lines(path, 0)
    assert lines == (_commentary_text("one"),)
    assert next_cursor == 1

    with path.open("ab") as handle:
        handle.write(b"\n")
    lines2, next2 = read_progress_lines(path, next_cursor)
    assert lines2 == (_commentary_text("two"),)
    assert next2 == 2


def test_long_line_content_is_preserved_across_reads(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(progress_reader, "_READ_CHUNK_BYTES", 64)
    path = tmp_path / "req.chunks.jsonl"
    long_row = _row("assistant_commentary", text="x" * 200)
    after = _row("assistant_commentary", text="after")
    path.write_bytes(long_row + after)

    lines, next_cursor = read_progress_lines(path, 0)
    assert next_cursor == 2
    assert lines == (_commentary_text("x" * 200), _commentary_text("after"))

    with path.open("ab") as handle:
        handle.write(_row("assistant_commentary", text="more"))
    lines2, next2 = read_progress_lines(path, 2)
    assert lines2 == (_commentary_text("more"),)
    assert next2 == 3


def test_oversize_partial_tail_is_not_consumed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(progress_reader, "_READ_CHUNK_BYTES", 64)
    path = tmp_path / "req.chunks.jsonl"
    path.write_bytes(_row("assistant_commentary", text="y" * 200)[:-1])

    assert read_progress_lines(path, 0) == ((), 0)

    with path.open("ab") as handle:
        handle.write(b"\n")
    assert read_progress_lines(path, 0) == ((_commentary_text("y" * 200),), 1)


def test_line_over_hard_limit_emits_structured_oversize_event(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(progress_reader, "_READ_CHUNK_BYTES", 64)
    monkeypatch.setattr(progress_reader, "_MAX_LINE_BYTES", 256)
    path = tmp_path / "req.chunks.jsonl"
    oversize = _row("assistant_commentary", text="z" * 400)
    after = _row("assistant_commentary", text="after")
    path.write_bytes(oversize + after)

    lines, next_cursor = read_progress_lines(path, 0)
    assert next_cursor == 2
    assert lines == (
        json.dumps(
            {"kind": "progress_oversize", "line": 0, "bytes": len(oversize)},
            ensure_ascii=False,
        ),
        _commentary_text("after"),
    )

    events, cursor = _read_public_progress_events(path, 0)
    assert cursor == 2
    assert events == [
        {"kind": "progress_oversize", "line": 0, "bytes": len(oversize)},
        {"kind": "assistant_commentary", "text": "after"},
    ]


def test_real_writer_commentary_over_one_mib_survives_projection(tmp_path) -> None:
    # MC4-1001 复现转正式回归：真实 writer 写出 >1MiB 的合法 commentary，投影层必须照常返回，
    # 而不是像旧版那样丢弃（基线：1 个公开事件；旧版新版：0 个）。
    from agent_py_agent.agent.gateway_parts.stream_writer import BufferedChunkStreamWriter

    path = tmp_path / "req.chunks.jsonl"
    writer = BufferedChunkStreamWriter(path, delivery_channel="feishu")
    writer.write_model("x" * (1_048_576 + 100))
    writer.write_progress({"tool": "read_file", "phase": "started", "status": "开始"}, "start")
    writer.close()

    legacy = jsonl_lines(path.read_text(encoding="utf-8"))
    assert len(legacy) == 2  # commentary + tool_progress（后者 verbose off，不投影）

    events, cursor = _read_public_progress_events(path, 0)
    assert cursor == 2
    assert len(events) == 1
    assert events[0]["kind"] == "assistant_commentary"
    assert events[0]["text"] == "x" * (1_048_576 + 100)
    assert events[0]["updated_at"] == json.loads(legacy[0])["updated_at"]


def test_index_cache_is_bounded_and_evicts_oldest(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(progress_reader, "_MAX_INDEXED_FILES", 2)
    paths = []
    for index in range(3):
        path = tmp_path / f"req-{index}.chunks.jsonl"
        path.write_bytes(_row("assistant_commentary", text=f"n{index}"))
        read_progress_lines(path, 0)
        paths.append(path)

    keys = list(progress_reader._INDEXES)
    assert len(keys) == 2
    assert str(paths[0]) not in keys


def test_empty_poll_peak_memory_is_kilobyte_scale(tmp_path) -> None:
    path = tmp_path / "big.chunks.jsonl"
    line = _row("tool_progress", verbose_level="off", progress={"tool": "read_file", "status": "完成"})
    repeats = (26 * 1024 * 1024) // len(line)
    with path.open("wb") as handle:
        written = 0
        while written < repeats:
            batch = min(1000, repeats - written)
            handle.write(line * batch)
            written += batch

    _, first_cursor = read_progress_lines(path, 0)
    assert first_cursor == 200  # 单次窗口上限；complete_lines 已推进到文件末尾

    tracemalloc.start()
    lines, next_again = read_progress_lines(path, repeats)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert (lines, next_again) == ((), repeats)
    print(f"empty-poll peak={peak} bytes, rows={repeats}, file={repeats * len(line)} bytes")
    assert peak < 512 * 1024


def test_same_inode_same_length_rewrite_rebuilds_index(tmp_path):
    path = tmp_path / "chunks.jsonl"
    path.write_bytes(_row("assistant_commentary", text="old"))
    before = path.stat()
    first, cursor = read_progress_lines(path, 0)
    assert json.loads(first[0])["text"] == "old"
    path.write_bytes(_row("assistant_commentary", text="new"))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1000000))
    assert path.stat().st_ino == before.st_ino
    again, next_cursor = read_progress_lines(path, 0)
    assert json.loads(again[0])["text"] == "new"
    assert next_cursor == cursor


def test_copytruncate_growth_rebuilds_previous_offsets(tmp_path):
    path = tmp_path / "chunks.jsonl"
    path.write_bytes(b"a" * 49 + b"\n" + b"b" * 49 + b"\n")
    _, cursor = read_progress_lines(path, 0)
    assert cursor == 2
    before = path.stat()
    replacement = (b"c" * 89 + b"\n") * 3
    path.write_bytes(replacement)
    assert path.stat().st_ino == before.st_ino
    rows, cursor = read_progress_lines(path, 0)
    assert rows == tuple(["c" * 89] * 3)
    assert cursor == 3


def test_boundary_recheck_is_bounded_and_idle_poll_reads_nothing(tmp_path, monkeypatch):
    path = tmp_path / "chunks.jsonl"
    path.write_bytes(_row("assistant_commentary", text="old") * 1000)
    _, cursor = read_progress_lines(path, 0)
    calls = []
    original = progress_reader._boundary_fingerprint

    def measure(handle, end):
        calls.append(2 * min(64, end))
        return original(handle, end)

    monkeypatch.setattr(progress_reader, "_boundary_fingerprint", measure)
    assert read_progress_lines(path, 1000) == ((), 1000)
    assert calls == []
    with path.open("ab") as handle:
        handle.write(_row("assistant_commentary", text="new"))
    rows, next_cursor = read_progress_lines(path, 1000)
    assert len(rows) == 1 and next_cursor == 1001
    assert calls and sum(calls) <= 256
