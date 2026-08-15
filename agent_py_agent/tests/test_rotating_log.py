"""通用文件轮转 + retention 单测。核心断言:total_line_count 永远 == 总写入行数。"""

import os
import time

from agent.common.rotating_log import (
    RotatePolicy,
    append_with_rotation,
    iter_all_lines,
    online_line_count,
    total_line_count,
)


def _write_lines(path, policy, n, start=0):
    for i in range(start, start + n):
        append_with_rotation(path, f"line{i}\n", policy)


def test_no_rotation_when_disabled(tmp_path):
    p = tmp_path / "a.log"
    _write_lines(p, RotatePolicy(), 100)  # max_bytes=0 关闭轮转
    assert online_line_count(p) == 100
    assert total_line_count(p) == 100
    assert list(p.parent.glob("a.log.[0-9]*")) == []  # 无历史段


def test_rotation_by_size(tmp_path):
    p = tmp_path / "a.log"
    _write_lines(p, RotatePolicy(max_bytes=20, backup_count=10), 30)
    assert total_line_count(p) == 30
    assert len(list(p.parent.glob("a.log.[0-9]*"))) >= 1  # 产生历史段


def test_total_line_count_survives_pruning(tmp_path):
    """核心:backup_count=2 删了最老段后,total_line_count 仍 == 总写入(pruned 累计补回)。"""
    p = tmp_path / "a.log"
    _write_lines(p, RotatePolicy(max_bytes=20, backup_count=2), 100)
    assert online_line_count(p) < 100  # 老段被删,在线 < 总数
    assert total_line_count(p) == 100  # 但累计口径不丢账


def test_gzip_compression(tmp_path):
    p = tmp_path / "a.log"
    _write_lines(p, RotatePolicy(max_bytes=20, backup_count=10, compress=True), 30)
    assert len(list(p.parent.glob("a.log.*.gz"))) >= 1  # 历史段被 gzip
    assert total_line_count(p) == 30  # gz 段行数也数得到


def test_iter_all_lines_in_order(tmp_path):
    p = tmp_path / "a.log"
    _write_lines(p, RotatePolicy(max_bytes=20, backup_count=20, compress=True), 30)
    lines = [ln.strip() for ln in iter_all_lines(p)]
    assert lines == [f"line{i}" for i in range(30)]  # 最老→最新顺序还原


def test_max_total_bytes_budget(tmp_path):
    p = tmp_path / "a.log"
    _write_lines(p, RotatePolicy(max_bytes=20, backup_count=100, max_total_bytes=40), 100)
    seg_bytes = sum(s.stat().st_size for s in p.parent.glob("a.log.[0-9]*"))
    assert seg_bytes <= 60  # 历史段总字节受预算约束(预算 40 + 一段容差)
    assert total_line_count(p) == 100  # 仍不丢账


def test_retention_by_age(tmp_path):
    p = tmp_path / "a.log"
    pol = RotatePolicy(max_bytes=20, backup_count=100, retention_seconds=1000)
    _write_lines(p, pol, 30)
    old = time.time() - 5000  # 把历史段 mtime 设为超龄
    for seg in p.parent.glob("a.log.[0-9]*"):
        os.utime(seg, (old, old))
    append_with_rotation(p, "trigger\n", pol)  # 触发 retention
    assert total_line_count(p) == 31  # 超龄段被删但不丢账
    assert online_line_count(p) <= 6  # 在线历史段被清(只剩主文件几行)
