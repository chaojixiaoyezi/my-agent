"""gwp-w4: 验证 read_text_lines_cached 对超过 64 KiB 的文件不入缓存且及时驱逐旧小文件条目。"""

from __future__ import annotations

import gc
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_py_agent.agent.common import cache_freshness, json_io, text_file_window

pytestmark = pytest.mark.regression


def _backdate_file(path: Path, *, seconds_ago: int = 10) -> None:
    """将文件 mtime 拨到 2 秒信任窗口之外，确保大小门限是唯一拒绝缓存的原因。"""
    old_ns = time.time_ns() - seconds_ago * 1_000_000_000
    os.utime(path, ns=(old_ns, old_ns))


def _write_sized_jsonl(path: Path, target_bytes: int, *, prefix: str = "line") -> list[str]:
    """写入总字节数精确为 target_bytes 的 UTF-8 JSONL 记录（每条以 LF 结尾）。"""
    assert target_bytes >= 32
    lines: list[str] = []
    written = 0
    idx = 0
    while written < target_bytes:
        remaining = target_bytes - written
        if remaining <= 256:
            # 最后一行精确补齐到 target_bytes（含末尾 '\n'）。
            body_prefix = f'{{"i":{idx},"tag":"{prefix}","pad":"'
            body_suffix = '"}'
            pad_len = max(0, remaining - 1 - len(body_prefix) - len(body_suffix))
            if len(body_prefix) + len(body_suffix) + 1 <= remaining:
                line = f"{body_prefix}{'x' * pad_len}{body_suffix}"
            else:
                line = "x" * (remaining - 1)
        else:
            line = f'{{"i":{idx},"tag":"{prefix}","pad":"{"a" * 200}"}}'
        lines.append(line)
        written += len(line.encode("utf-8")) + 1
        idx += 1
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert path.stat().st_size == target_bytes
    return lines


@pytest.fixture(autouse=True)
def _isolated_text_lines_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(json_io, "_TEXT_LINES_CACHE", {})


def test_large_multi_mib_files_never_cached_and_release_after_unlink_gc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """读取 8 个 >64 KiB 的多 MiB 文件并在删除后 gc.collect()，缓存不留任何条目或大字符串。"""
    read_calls: list[str] = []
    real_read = json_io.read_jsonl_text_lines

    def counted_read(path: Path) -> tuple[str, ...]:
        read_calls.append(str(path))
        return real_read(path)

    monkeypatch.setattr(json_io, "read_jsonl_text_lines", counted_read)
    monkeypatch.setattr(cache_freshness, "cache_entry_trustworthy", lambda _mtime_ns: True)

    file_size = 2 * 1024 * 1024  # 2 MiB > 64 KiB，8 个文件共 16 MiB
    paths: list[Path] = []
    expected_counts: list[int] = []

    def _create_and_read_all() -> None:
        for idx in range(8):
            ledger = tmp_path / f"large_{idx}.jsonl"
            expected = _write_sized_jsonl(ledger, file_size, prefix=f"large-{idx}")
            _backdate_file(ledger)
            paths.append(ledger)
            expected_counts.append(len(expected))

            first = json_io.read_text_lines_cached(ledger)
            second = json_io.read_text_lines_cached(ledger)
            assert len(first) == len(expected)
            assert first[0] == expected[0]
            assert first[-1] == expected[-1]
            assert second == first
            assert str(ledger) not in json_io._TEXT_LINES_CACHE

    _create_and_read_all()
    assert len(read_calls) == 16, "大文件不得命中缓存，每次读取都应直读磁盘"
    assert len(json_io._TEXT_LINES_CACHE) == 0

    for ledger in paths:
        ledger.unlink()
    gc.collect()

    assert json_io._TEXT_LINES_CACHE == {}
    assert gc.get_referents(json_io._TEXT_LINES_CACHE) == []
    for ledger in paths:
        with pytest.raises(FileNotFoundError):
            json_io.read_text_lines_cached(ledger)


def test_chunk_bytes_boundary_small_cached_vs_over_limit_uncached(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """精确边界：<= 64 KiB 命中缓存，> 64 KiB（含 64 KiB + 1B）直接返回且不入缓存。"""
    chunk_bytes = text_file_window._CHUNK_BYTES
    assert chunk_bytes == 64 * 1024

    read_calls: list[str] = []
    real_read = json_io.read_jsonl_text_lines

    def counted_read(path: Path) -> tuple[str, ...]:
        read_calls.append(str(path))
        return real_read(path)

    monkeypatch.setattr(json_io, "read_jsonl_text_lines", counted_read)
    monkeypatch.setattr(cache_freshness, "cache_entry_trustworthy", lambda _mtime_ns: True)

    exact_small = tmp_path / "exact_64k.jsonl"
    expected_small = _write_sized_jsonl(exact_small, chunk_bytes, prefix="exact")
    _backdate_file(exact_small)

    just_over = tmp_path / "over_64k_plus_1.jsonl"
    expected_over = _write_sized_jsonl(just_over, chunk_bytes + 1, prefix="over")
    _backdate_file(just_over)

    s1 = json_io.read_text_lines_cached(exact_small)
    s2 = json_io.read_text_lines_cached(exact_small)
    assert s1 == tuple(expected_small)
    assert s2 is s1, "<= 64 KiB 稳定文件必须命中缓存并返回同一不可变 tuple"
    assert str(exact_small) in json_io._TEXT_LINES_CACHE

    o1 = json_io.read_text_lines_cached(just_over)
    o2 = json_io.read_text_lines_cached(just_over)
    assert o1 == tuple(expected_over)
    assert o2 == o1
    assert str(just_over) not in json_io._TEXT_LINES_CACHE

    assert read_calls == [str(exact_small), str(just_over), str(just_over)]


def test_small_cached_file_evicts_stale_entry_when_growing_past_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """曾入缓存的小文件长超 64 KiB 后再次读取，必须返回新内容并驱逐原缓存条目。"""
    monkeypatch.setattr(cache_freshness, "cache_entry_trustworthy", lambda _mtime_ns: True)

    ledger = tmp_path / "growing.jsonl"
    ledger.write_text('{"step":1}\n{"step":2}\n', encoding="utf-8")
    _backdate_file(ledger)

    small_first = json_io.read_text_lines_cached(ledger)
    small_second = json_io.read_text_lines_cached(ledger)
    assert small_first == ('{"step":1}', '{"step":2}')
    assert small_second is small_first
    assert str(ledger) in json_io._TEXT_LINES_CACHE

    # 追加记录使文件体积越过 64 KiB，同时把 mtime 拨旧以确保不会仅靠时间窗口绕开缓存。
    extra_line = '{"step":"grow","payload":"' + ("g" * 1024) + '"}\n'
    repeat_count = (text_file_window._CHUNK_BYTES // len(extra_line.encode("utf-8"))) + 4
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(extra_line * repeat_count)
    assert ledger.stat().st_size > text_file_window._CHUNK_BYTES
    _backdate_file(ledger)

    grown = json_io.read_text_lines_cached(ledger)
    assert len(grown) == 2 + repeat_count
    assert grown[:2] == ('{"step":1}', '{"step":2}')
    assert grown[-1] == extra_line.rstrip("\n")
    assert str(ledger) not in json_io._TEXT_LINES_CACHE, "超限后必须摘除原先小文件遗留的缓存条目"

    # 若后续被原子替换回 <= 64 KiB 的小文件，仍可正常重新入缓存并命中。
    replacement = tmp_path / "growing.jsonl.tmp"
    replacement.write_text('{"step":"shrunk"}\n', encoding="utf-8")
    os.replace(replacement, ledger)
    _backdate_file(ledger)

    shrunk_first = json_io.read_text_lines_cached(ledger)
    shrunk_second = json_io.read_text_lines_cached(ledger)
    assert shrunk_first == ('{"step":"shrunk"}',)
    assert shrunk_second is shrunk_first
    assert str(ledger) in json_io._TEXT_LINES_CACHE


def test_total_cached_source_bytes_bounded_by_max_count_times_chunk_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """混合读取 70 个 64 KiB 文件和 8 个超大文件，缓存条数 <= 64 且总源字节 <= 4 MiB。"""
    monkeypatch.setattr(cache_freshness, "cache_entry_trustworthy", lambda _mtime_ns: True)
    chunk_bytes = text_file_window._CHUNK_BYTES

    for idx in range(70):
        small = tmp_path / f"small_{idx}.jsonl"
        _write_sized_jsonl(small, chunk_bytes, prefix=f"s{idx}")
        _backdate_file(small)
        json_io.read_text_lines_cached(small)

    for idx in range(8):
        large = tmp_path / f"big_{idx}.jsonl"
        _write_sized_jsonl(large, chunk_bytes * 4, prefix=f"b{idx}")
        _backdate_file(large)
        json_io.read_text_lines_cached(large)

    assert len(json_io._TEXT_LINES_CACHE) == json_io._TEXT_LINES_CACHE_MAX_COUNT == 64
    cached_bytes = sum(signature[2] for signature, _lines in json_io._TEXT_LINES_CACHE.values())
    assert all(signature[2] <= chunk_bytes for signature, _lines in json_io._TEXT_LINES_CACHE.values())
    assert cached_bytes <= json_io._TEXT_LINES_CACHE_MAX_COUNT * chunk_bytes == 4 * 1024 * 1024


def test_json_io_and_text_file_window_have_no_import_cycle() -> None:
    """两种导入顺序在干净进程内均不得触发循环导入。"""
    for stmt in (
        "import agent_py_agent.agent.common.text_file_window, agent_py_agent.agent.common.json_io",
        "import agent_py_agent.agent.common.json_io, agent_py_agent.agent.common.text_file_window",
    ):
        proc = subprocess.run(
            [sys.executable, "-c", stmt],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"import cycle or error: {proc.stderr}"
