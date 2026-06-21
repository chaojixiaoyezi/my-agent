"""审计 #16(part E,medium/稳定)真测:append-only 台账有界化(background_jobs 登记)。

shell._record_background_job 原裸 open('a') 纯 append,后台任务越登记越多永不回收 → 长跑
workspace 该 registry.jsonl 无界涨。改走 append_jsonl_capped:读-改-写持同一把锁,追加后只留
最近 N 条。真写远超上限条数 + 并发写 + 损坏行,断言封顶/留最近/丢最旧/不撕行/容错。
"""

from __future__ import annotations

import threading
from pathlib import Path

from agent_py_agent.agent.common.json_io import append_jsonl_capped, read_jsonl_objects


def test_capped_append_keeps_last_n(tmp_path: Path) -> None:
    p = tmp_path / "registry.jsonl"
    for i in range(1500):
        append_jsonl_capped(p, {"pid": i}, max_records=1000)
    recs = read_jsonl_objects(p)
    assert len(recs) == 1000  # 封顶,不随登记次数无界增长
    assert recs[0]["pid"] == 500  # 最旧 500 条被丢
    assert recs[-1]["pid"] == 1499  # 最近的保留


def test_capped_append_missing_file_and_dirs(tmp_path: Path) -> None:
    p = tmp_path / "sub" / "deep" / "r.jsonl"  # 父目录都不存在
    append_jsonl_capped(p, {"a": 1}, max_records=10)
    assert read_jsonl_objects(p) == [{"a": 1}]  # 自建目录,首条落盘


def test_capped_append_skips_corrupt_lines(tmp_path: Path) -> None:
    p = tmp_path / "r.jsonl"
    p.write_text('{"pid": 1}\n!!not json!!\n{"pid": 2}\n', encoding="utf-8")  # 含损坏行的旧台账
    append_jsonl_capped(p, {"pid": 3}, max_records=10)
    pids = [r["pid"] for r in read_jsonl_objects(p)]
    assert pids == [1, 2, 3]  # 损坏行被跳过,有效记录保留 + 新增


def test_capped_append_concurrent_no_torn_lines(tmp_path: Path) -> None:
    p = tmp_path / "r.jsonl"

    def writer(base: int) -> None:
        for i in range(100):
            append_jsonl_capped(p, {"w": base, "i": i}, max_records=5000)

    threads = [threading.Thread(target=writer, args=(b,)) for b in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    recs = read_jsonl_objects(p)  # 若读-改-写竞态会丢记录或撕行
    assert len(recs) == 800  # 8*100 全部完整落盘,无撕行无丢失(< 5000 上限不裁剪)


def test_record_background_job_caps_registry(tmp_path: Path) -> None:
    from agent_py_agent.agent.tooling import shell

    jobs = tmp_path / ".background_jobs"
    jobs.mkdir()
    n = shell._MAX_BACKGROUND_JOB_RECORDS + 200
    for i in range(n):
        shell._record_background_job(jobs, pid=1000 + i, command=f"cmd {i}", log_path=tmp_path / f"{i}.log")
    recs = read_jsonl_objects(jobs / "registry.jsonl")
    assert len(recs) == shell._MAX_BACKGROUND_JOB_RECORDS  # 登记台账封顶
    assert recs[-1]["pid"] == 1000 + n - 1  # 最近的后台任务在册
