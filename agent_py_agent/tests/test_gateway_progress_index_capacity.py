# 模块用途: 验证 progress_reader 的全局并发索引容量、单流锁隔离、不持有 path_thread_lock 与有界冷扫描前缀停机。
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common import cache_capacity, json_io
from agent_py_agent.agent.gateway_parts import progress_reader
from agent_py_agent.agent.gateway_parts.progress_reader import read_progress_lines


@pytest.fixture(autouse=True)
def _reset_progress_state():
    cache_capacity.reset_for_tests()
    with progress_reader._INDEX_LOCK:
        progress_reader._INDEXES.clear()
    yield
    with progress_reader._INDEX_LOCK:
        progress_reader._INDEXES.clear()
    cache_capacity.reset_for_tests()


def _row(text: str, *, pad_to: int = 0) -> bytes:
    payload = json.dumps({"kind": "assistant_commentary", "text": text}, ensure_ascii=False).encode("utf-8")
    if pad_to > len(payload) + 1:
        payload = json.dumps(
            {"kind": "assistant_commentary", "text": text, "pad": "x" * (pad_to - len(payload) - 10)},
            ensure_ascii=False,
        ).encode("utf-8")
    return payload + b"\n"


def test_round_robin_over_64_streams_with_configured_inflight_limit_does_zero_byte0_rescans(
    tmp_path, monkeypatch,
) -> None:
    cache_capacity.configure_from_config(SimpleNamespace(gateway_global_inflight_limit=500))
    stream_count = 72
    rows_per_stream = 20
    paths: list[Path] = []
    for idx in range(stream_count):
        path = tmp_path / f"stream-{idx}.chunks.jsonl"
        path.write_bytes(b"".join(_row(f"s{idx}-r{row}") for row in range(rows_per_stream)))
        paths.append(path)

    for path in paths:
        lines, cursor = read_progress_lines(path, 0)
        assert len(lines) == rows_per_stream
        assert cursor == rows_per_stream

    assert len(progress_reader._INDEXES) == stream_count

    original = progress_reader._read_line_bytes
    reads = {"bytes": 0, "calls": 0}

    def counted(handle, limit):
        chunk = original(handle, limit)
        reads["bytes"] += len(chunk)
        reads["calls"] += 1
        return chunk

    monkeypatch.setattr(progress_reader, "_read_line_bytes", counted)

    # 连续两轮越过 64 路上限的轮询必须全部命中 EOF 快路径，零重扫。
    for _ in range(2):
        for path in paths:
            lines, cursor = read_progress_lines(path, rows_per_stream)
            assert lines == ()
            assert cursor == rows_per_stream
    assert reads == {"bytes": 0, "calls": 0}

    # 每路仅追加 1 行后，本轮总读取量必须严格等于追加字节数，绝不从 byte 0 重扫。
    expected_appended_bytes = 0
    for idx, path in enumerate(paths):
        extra = _row(f"s{idx}-appended")
        expected_appended_bytes += len(extra)
        with path.open("ab") as handle:
            handle.write(extra)

    for idx, path in enumerate(paths):
        lines, cursor = read_progress_lines(path, rows_per_stream)
        assert cursor == rows_per_stream + 1
        assert len(lines) == 1
        assert json.loads(lines[0])["text"] == f"s{idx}-appended"

    assert reads["bytes"] == expected_appended_bytes


def test_unconfigured_capacity_keeps_64_file_floor(tmp_path) -> None:
    stream_count = progress_reader._MAX_INDEXED_FILES + 1
    paths: list[Path] = []
    for idx in range(stream_count):
        path = tmp_path / f"floor-{idx}.chunks.jsonl"
        path.write_bytes(_row(f"r{idx}"))
        read_progress_lines(path, 0)
        paths.append(path)

    assert len(progress_reader._INDEXES) == progress_reader._MAX_INDEXED_FILES
    assert str(paths[0]) not in progress_reader._INDEXES
    assert str(paths[-1]) in progress_reader._INDEXES


def test_blocked_scan_on_file_a_does_not_block_poll_on_file_b(tmp_path, monkeypatch) -> None:
    file_a = tmp_path / "a.chunks.jsonl"
    file_b = tmp_path / "b.chunks.jsonl"
    file_c = tmp_path / "c.chunks.jsonl"
    file_a.write_bytes(b"".join(_row(f"a-{i}") for i in range(50)))
    file_b.write_bytes(b"".join(_row(f"b-{i}") for i in range(10)))
    file_c.write_bytes(b"".join(_row(f"c-{i}") for i in range(10)))

    # 预热 file_b 到 EOF；file_c 保持冷态以同时验证另一条流的真实磁盘扫描也不被阻塞。
    _, b_cursor = read_progress_lines(file_b, 0)
    assert b_cursor == 10

    original = progress_reader._read_line_bytes
    entered = threading.Event()
    release = threading.Event()

    def gated(handle, limit):
        if Path(handle.name) == file_a:
            entered.set()
            if not release.wait(timeout=5.0):
                raise RuntimeError("progress reader gate timed out")
        return original(handle, limit)

    monkeypatch.setattr(progress_reader, "_read_line_bytes", gated)

    a_result: list[tuple[tuple[str, ...], int]] = []
    a_errors: list[BaseException] = []

    def run_a():
        try:
            a_result.append(read_progress_lines(file_a, 0))
        except BaseException as exc:  # pragma: no cover - 仅失败诊断
            a_errors.append(exc)

    thread_a = threading.Thread(target=run_a)
    thread_a.start()
    assert entered.wait(timeout=3.0)

    b_done = threading.Event()
    b_result: list[tuple[tuple[str, ...], int]] = []

    def run_b():
        b_result.append(read_progress_lines(file_b, 10))
        b_done.set()

    c_done = threading.Event()
    c_result: list[tuple[tuple[str, ...], int]] = []

    def run_c():
        c_result.append(read_progress_lines(file_c, 0))
        c_done.set()

    thread_b = threading.Thread(target=run_b)
    thread_c = threading.Thread(target=run_c)
    thread_b.start()
    thread_c.start()

    try:
        assert b_done.wait(timeout=1.5), "warm EOF poll on file B was blocked by file A scan"
        assert c_done.wait(timeout=1.5), "cold read on file C was blocked by file A scan"
        assert b_result == [((), 10)]
        assert len(c_result) == 1 and c_result[0][1] == 10 and len(c_result[0][0]) == 10
    finally:
        release.set()
        thread_b.join(timeout=3.0)
        thread_c.join(timeout=3.0)
        thread_a.join(timeout=5.0)

    assert not thread_a.is_alive()
    assert not a_errors
    assert len(a_result) == 1 and a_result[0][1] == 50 and len(a_result[0][0]) == 50


def test_readers_never_acquire_json_io_path_thread_lock(tmp_path, monkeypatch) -> None:
    path = tmp_path / "shared.chunks.jsonl"
    path.write_bytes(b"".join(_row(f"item-{i}") for i in range(10)))

    def fail_path_lock(*_args, **_kwargs):  # pragma: no cover - 仅失败诊断
        raise AssertionError("progress_reader must not acquire json_io.path_thread_lock")

    # 方向 1：写入侧持有 path_thread_lock 时，读侧必须照常完成不阻塞，且不调用 path_thread_lock。
    path_lock = json_io.path_thread_lock(path)
    monkeypatch.setattr(json_io, "path_thread_lock", fail_path_lock)
    read_done = threading.Event()
    read_result: list[tuple[tuple[str, ...], int]] = []
    with path_lock.lock:
        thread = threading.Thread(
            target=lambda: (read_result.append(read_progress_lines(path, 0)), read_done.set()),
        )
        thread.start()
        assert read_done.wait(timeout=1.5), "reader blocked on json_io.path_thread_lock"
        thread.join(timeout=3.0)
    assert len(read_result) == 1 and read_result[0][1] == 10 and len(read_result[0][0]) == 10

    # 方向 2：读侧在 index.lock 内阻塞扫描时，写入侧获取 path_thread_lock 不受阻。
    with progress_reader._INDEX_LOCK:
        progress_reader._INDEXES.clear()

    original = progress_reader._read_line_bytes
    entered = threading.Event()
    release = threading.Event()

    def gated(handle, limit):
        if Path(handle.name) == path:
            entered.set()
            if not release.wait(timeout=5.0):
                raise RuntimeError("reader gate timed out")
        return original(handle, limit)

    monkeypatch.setattr(progress_reader, "_read_line_bytes", gated)

    reader_done = threading.Event()
    reader = threading.Thread(
        target=lambda: (read_progress_lines(path, 0), reader_done.set()),
    )
    reader.start()
    assert entered.wait(timeout=3.0)

    writer_acquired = threading.Event()

    def try_writer_lock():
        with path_lock.lock:
            writer_acquired.set()

    writer = threading.Thread(target=try_writer_lock)
    writer.start()
    try:
        assert writer_acquired.wait(timeout=1.5), "writer path_thread_lock blocked by reader scan"
    finally:
        release.set()
        writer.join(timeout=3.0)
        reader.join(timeout=5.0)
    assert reader_done.is_set()


def test_cold_multi_mib_page_scans_only_prefix_and_continues_seamlessly(tmp_path, monkeypatch) -> None:
    path = tmp_path / "large.chunks.jsonl"
    total_lines = 2560
    line_bytes: list[bytes] = []
    with path.open("wb") as handle:
        for idx in range(total_lines):
            raw = _row(f"line-{idx:04d}", pad_to=2048)
            line_bytes.append(raw)
            handle.write(raw)

    total_size = path.stat().st_size
    assert total_size >= 5 * 1024 * 1024

    original = progress_reader._read_line_bytes
    reads = {"bytes": 0}

    def counted(handle, limit):
        chunk = original(handle, limit)
        reads["bytes"] += len(chunk)
        return chunk

    monkeypatch.setattr(progress_reader, "_read_line_bytes", counted)

    # 1. 冷读第一页（since=0, limit=200）：只扫前 200 行对应前缀，绝不扫完整 5+ MiB。
    rows0, cur0 = read_progress_lines(path, 0, limit=200)
    assert cur0 == 200
    assert len(rows0) == 200
    assert [json.loads(r)["text"] for r in rows0] == [f"line-{i:04d}" for i in range(200)]
    expected_prefix_0 = sum(len(b) for b in line_bytes[:200])
    assert reads["bytes"] == expected_prefix_0
    assert reads["bytes"] < total_size // 8

    # 2. 续读第二页（since=200, limit=200）：从 scanned_bytes 无缝续扫 200..399，不漏行也不误判 EOF。
    reads["bytes"] = 0
    rows1, cur1 = read_progress_lines(path, 200, limit=200)
    assert cur1 == 400
    assert len(rows1) == 200
    assert [json.loads(r)["text"] for r in rows1] == [f"line-{i:04d}" for i in range(200, 400)]
    assert reads["bytes"] == sum(len(b) for b in line_bytes[200:400])

    # 3. 回读已索引页（since=0, limit=200）：不触发 _scan_new_bytes 向前多扫。
    scan_calls = []
    orig_scan = progress_reader._scan_new_bytes
    monkeypatch.setattr(
        progress_reader,
        "_scan_new_bytes",
        lambda *args, **kwargs: (scan_calls.append(1), orig_scan(*args, **kwargs))[1],
    )
    rows0_again, cur0_again = read_progress_lines(path, 0, limit=200)
    assert (rows0_again, cur0_again) == (rows0, 200)
    assert scan_calls == []

    # 4. 跨旧区+新扫描重叠页（since=300, limit=200 -> 300..499）。
    rows_mid, cur_mid = read_progress_lines(path, 300, limit=200)
    assert cur_mid == 500
    assert [json.loads(r)["text"] for r in rows_mid] == [f"line-{i:04d}" for i in range(300, 500)]

    # 5. 分页读完剩余全部行直到 EOF，再空轮询应当 0 字节读取。
    cursor = cur_mid
    collected: list[str] = []
    while cursor < total_lines:
        page, next_cur = read_progress_lines(path, cursor, limit=200)
        assert page
        collected.extend(json.loads(r)["text"] for r in page)
        assert next_cur > cursor
        cursor = next_cur
    assert cursor == total_lines
    assert collected == [f"line-{i:04d}" for i in range(500, total_lines)]

    reads["bytes"] = 0
    assert read_progress_lines(path, total_lines, limit=200) == ((), total_lines)
    assert reads["bytes"] == 0


def test_bounded_scan_preserves_half_line_copytruncate_and_oversize_behavior(tmp_path, monkeypatch) -> None:
    # 1. 恰在 since + limit 边界后存在未完成半行：不得误触发 EOF 快路径或吞掉半行。
    half_path = tmp_path / "half.chunks.jsonl"
    complete = b"".join(_row(f"h-{i}") for i in range(200))
    half_tail = _row("h-200")[:-1]
    half_path.write_bytes(complete + half_tail)

    rows, cur = read_progress_lines(half_path, 0, limit=200)
    assert len(rows) == 200 and cur == 200
    assert read_progress_lines(half_path, 200, limit=200) == ((), 200)

    with half_path.open("ab") as handle:
        handle.write(b"\n")
    rows_tail, cur_tail = read_progress_lines(half_path, 200, limit=200)
    assert cur_tail == 201
    assert len(rows_tail) == 1 and json.loads(rows_tail[0])["text"] == "h-200"

    # 2. 冷分页停在 200 后同 inode copytruncate 扩容重写：边界校验必须重置并重扫新文件。
    ct_path = tmp_path / "copytruncate.chunks.jsonl"
    ct_path.write_bytes(b"".join(_row(f"old-{i}", pad_to=128) for i in range(400)))
    inode_before = os.stat(ct_path).st_ino
    old_rows, old_cur = read_progress_lines(ct_path, 0, limit=200)
    assert old_cur == 200 and json.loads(old_rows[0])["text"] == "old-0"

    ct_path.write_bytes(b"".join(_row(f"new-{i}", pad_to=140) for i in range(500)))
    assert os.stat(ct_path).st_ino == inode_before
    new_rows, new_cur = read_progress_lines(ct_path, 200, limit=200)
    assert new_cur == 400
    assert [json.loads(r)["text"] for r in new_rows] == [f"new-{i}" for i in range(200, 400)]

    # 3. 超过 _MAX_LINE_BYTES 的超限行落在停机边界时，占位事件与后续行游标仍严格正确。
    over_path = tmp_path / "oversize.chunks.jsonl"
    monkeypatch.setattr(progress_reader, "_READ_CHUNK_BYTES", 64)
    monkeypatch.setattr(progress_reader, "_MAX_LINE_BYTES", 256)
    oversize_line = _row("z" * 400)
    over_path.write_bytes(_row("ok-0") + oversize_line + _row("ok-2"))

    p1, c1 = read_progress_lines(over_path, 0, limit=2)
    assert c1 == 2
    assert json.loads(p1[0])["text"] == "ok-0"
    assert json.loads(p1[1]) == {"kind": "progress_oversize", "line": 1, "bytes": len(oversize_line)}
    p2, c2 = read_progress_lines(over_path, 2, limit=2)
    assert c2 == 3 and len(p2) == 1 and json.loads(p2[0])["text"] == "ok-2"


def test_concurrent_pollers_same_stream_serialize_cleanly(tmp_path) -> None:
    path = tmp_path / "concurrent.chunks.jsonl"
    total = 600
    path.write_bytes(b"".join(_row(f"msg-{i:04d}") for i in range(total)))

    errors: list[BaseException] = []
    barrier = threading.Barrier(6)

    def poll_worker(start: int):
        try:
            barrier.wait(timeout=3.0)
            cursor = start
            while cursor < total:
                rows, next_cur = read_progress_lines(path, cursor, limit=75)
                expected_end = min(total, cursor + 75)
                assert next_cur == expected_end
                assert [json.loads(r)["text"] for r in rows] == [
                    f"msg-{i:04d}" for i in range(cursor, expected_end)
                ]
                cursor = next_cur
        except BaseException as exc:  # pragma: no cover - 仅失败诊断
            errors.append(exc)

    threads = [threading.Thread(target=poll_worker, args=((i % 3) * 100,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)
    assert not errors
