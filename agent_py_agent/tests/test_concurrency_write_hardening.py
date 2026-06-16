"""并发写文件加锁/原子化加固回归(H4 / H5 / H8)。

背景:仓里已有原子写 + 加锁原语(io/jsonl.append_jsonl 的线程锁+flock 双层、
common/json_io.write_json_file_atomic 的 temp+os.replace),但三条关键路径没用上:

- H4 OptimisticLock.release/set_version:读-查-写之间无 OS 级锁,纯 TOCTOU,
  两进程同读 version=N、同过检查、同写 N+1 → 丢更新(两边都自以为成功)。
- H5 AuditLogger._write_to_file:裸 open("a")+write,并发 log 行内交错出半行 JSON。
- H8 gateway daemon_metadata._write_json_file:裸 write_text 非原子,写一半崩溃留半截
  JSON;半截 scoped-lock 锁文件会让 acquire_scoped_lock 永久拒绝接管,gateway 卡死。

所有断言基于确定性事实(最终版本计数、每行 JSON 可解析、文件内容完整),
不靠 sleep 碰运气。并发用 threading 模拟即可(无需真多进程)。
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_py_agent.agent.audit.logger import (
    AuditAction,
    AuditLogger,
    AuditStatus,
    LogParams,
)
from agent_py_agent.agent.concurrency.exceptions import ConcurrencyConflictError
from agent_py_agent.agent.concurrency.optimistic_lock import OptimisticLock
from agent_py_agent.agent.gateway_parts.daemon_metadata import (
    _read_json_file,
    _write_json_file,
)


class _Config:
    """OptimisticLock 只读 subagent_workspace。"""

    def __init__(self, workspace: Path):
        self.subagent_workspace = str(workspace)


# ---------------------------------------------------------------------------
# H4:OptimisticLock 读-改-写在 flock 临界区内,并发不丢更新
# ---------------------------------------------------------------------------


def test_h4_concurrent_release_no_lost_update(tmp_path: Path):
    """N 个线程对同一 task 做"读当前版本 → release(CAS+1)",带冲突重试。

    真正的 CAS 应当:每次成功的 release 恰好把版本 +1,冲突的重试直到成功。
    最终版本必须 == 起始版本(1) + 成功次数;因为每个线程都坚持重试到成功,
    成功次数 == 线程数 → 最终版本 == 1 + N。若 release 是 TOCTOU(旧实现),
    两线程会同读 N 同写 N+1,丢更新,最终版本 < 1 + N → 测试失败。
    """
    lock = OptimisticLock(_Config(tmp_path))
    task_id = "task-h4"
    threads = 24

    # 初始化到版本 1。
    assert lock.acquire(task_id) == 1

    success = threading.Semaphore(0)
    barrier = threading.Barrier(threads)

    def worker(_idx: int) -> None:
        barrier.wait()  # 尽量让所有线程在同一时刻冲进临界区,最大化竞态。
        while True:
            current = lock.acquire(task_id)
            try:
                lock.release(task_id, current)
                success.release()
                return
            except ConcurrencyConflictError:
                # 别人抢先 +1 了,读最新版本重试,绝不放弃。
                continue

    with ThreadPoolExecutor(max_workers=threads) as pool:
        list(pool.map(worker, range(threads)))

    # 每个线程都恰好成功一次。
    acquired = 0
    while success.acquire(blocking=False):
        acquired += 1
    assert acquired == threads

    # 关键不丢更新断言:每次成功 +1,起始 1,故最终版本 == 1 + N。
    assert lock.acquire(task_id) == 1 + threads

    # 落盘文件必须是完整合法 JSON(原子写,绝无半截)。
    lock_path = tmp_path / task_id / "task.json.lock"
    data = json.loads(lock_path.read_text(encoding="utf-8"))
    assert data["version"] == 1 + threads


def test_h4_concurrent_set_version_last_write_complete(tmp_path: Path):
    """并发 set_version(无条件覆写)下,文件任何时刻读出来都是完整 JSON,
    且最终值是某个写入过的合法版本(原子 temp+replace,不会读到半截)。"""
    lock = OptimisticLock(_Config(tmp_path))
    task_id = "task-h4-set"
    lock_path = tmp_path / task_id / "task.json.lock"
    versions = list(range(1, 201))

    read_errors: list[str] = []
    stop = threading.Event()

    def reader() -> None:
        # 持续并发读,任何一次读到半截 JSON 都记错。
        while not stop.is_set():
            if lock_path.exists():
                try:
                    json.loads(lock_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError) as exc:
                    read_errors.append(repr(exc))

    reader_thread = threading.Thread(target=reader)
    reader_thread.start()
    try:
        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(lambda v: lock.set_version(task_id, v), versions))
    finally:
        stop.set()
        reader_thread.join(timeout=5)

    assert read_errors == [], f"reader saw torn JSON: {read_errors[:3]}"
    final = json.loads(lock_path.read_text(encoding="utf-8"))
    assert final["version"] in versions


def test_h4_release_conflict_still_raises(tmp_path: Path):
    """加锁后,过期 expected_version 仍必须抛 ConcurrencyConflictError(对外行为不变)。"""
    lock = OptimisticLock(_Config(tmp_path))
    task_id = "task-h4-conflict"
    v1 = lock.acquire(task_id)
    lock.release(task_id, v1)  # 版本 → 2
    with pytest.raises(ConcurrencyConflictError) as ctx:
        lock.release(task_id, v1)  # 用旧版本号
    assert ctx.value.task_id == task_id
    assert ctx.value.expected_version == v1
    assert ctx.value.actual_version == 2


# ---------------------------------------------------------------------------
# H5:审计并发 log 不损坏,每行都是完整可解析 JSON
# ---------------------------------------------------------------------------


def _audit_logger(tmp_path: Path) -> AuditLogger:
    class MockConfig:
        audit_log_path = str(tmp_path / "audit")

    return AuditLogger(MockConfig())


def test_h5_concurrent_log_no_torn_lines(tmp_path: Path):
    """多线程并发 log → 审计文件每一行都是完整 JSON,总行数 == 写入条数,无交错。"""
    logger = _audit_logger(tmp_path)
    audit_file = tmp_path / "audit" / "audit.jsonl"
    threads = 16
    per_thread = 25
    total = threads * per_thread

    def worker(idx: int) -> None:
        for n in range(per_thread):
            logger.log(
                LogParams(
                    action=AuditAction.CREATE_TASK,
                    user_id=f"user-{idx}",
                    channel="chat",
                    target_type="task",
                    target_id=f"task-{idx}-{n}",
                    status=AuditStatus.SUCCESS,
                    # details 里塞中文 + 较长字符串,放大行内交错损坏的概率。
                    details={"note": "并发审计写入压力测试" * 4, "idx": idx, "n": n},
                )
            )

    with ThreadPoolExecutor(max_workers=threads) as pool:
        list(pool.map(worker, range(threads)))

    raw_lines = audit_file.read_text(encoding="utf-8").splitlines()
    # 条数完全一致:没有丢行、没有把两条挤进一行。
    assert len(raw_lines) == total, f"expected {total} lines, got {len(raw_lines)}"

    seen_targets = set()
    for line in raw_lines:
        # 每行都必须独立可解析(裸 open+write 并发会在这里炸出半行)。
        record = json.loads(line)
        assert record["action"] == "CREATE_TASK"
        seen_targets.add(record["target_id"])
    # 每条记录都完整且唯一,无内容串扰。
    assert len(seen_targets) == total


def test_h5_audit_format_unchanged(tmp_path: Path):
    """审计记录格式不变:仍是 ensure_ascii=False 的中文原文、一行一条 JSON 对象,
    字段集合与 to_dict 完全一致。"""
    logger = _audit_logger(tmp_path)
    audit_file = tmp_path / "audit" / "audit.jsonl"

    entry = logger.log(
        LogParams(
            action=AuditAction.DISPATCH,
            user_id="用户甲",
            channel="飞书",
            target_type="task",
            target_id="任务-1",
            status=AuditStatus.SUCCESS,
            details={"备注": "中文不转义"},
        )
    )

    text = audit_file.read_text(encoding="utf-8")
    # ensure_ascii=False:中文以原文落盘,不是 \uXXXX。
    assert "用户甲" in text
    assert "中文不转义" in text
    assert "\\u" not in text
    lines = text.splitlines()
    assert len(lines) == 1
    on_disk = json.loads(lines[0])
    # 落盘内容与 entry.to_dict() 完全一致(返回的 entry 也没变)。
    assert on_disk == entry.to_dict()


# ---------------------------------------------------------------------------
# H8:daemon_metadata._write_json_file 原子写,并发/重复写不留半截
# ---------------------------------------------------------------------------


def test_h8_write_json_file_roundtrips(tmp_path: Path):
    """对外行为不变:写进去能原样读回(数据格式不变)。"""
    target = tmp_path / "pid.json"
    payload = {"pid": 4321, "kind": "my-agent-gateway", "updated_at": "2026-06-16T00:00:00+00:00"}
    _write_json_file(target, payload)
    assert _read_json_file(target) == payload


def test_h8_concurrent_write_never_torn(tmp_path: Path):
    """多线程并发 _write_json_file 同一文件 + 并发读:读端永远拿到完整 JSON,
    最终内容是某次完整写入的结果(temp+os.replace 原子语义)。"""
    target = tmp_path / "scope.lock"
    payloads = [{"pid": i, "updated_at": f"t-{i}", "blob": "x" * 200} for i in range(1, 201)]

    read_errors: list[str] = []
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            if target.exists():
                try:
                    json.loads(target.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError) as exc:
                    read_errors.append(repr(exc))

    reader_thread = threading.Thread(target=reader)
    reader_thread.start()
    try:
        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(lambda p: _write_json_file(target, p), payloads))
    finally:
        stop.set()
        reader_thread.join(timeout=5)

    assert read_errors == [], f"reader saw torn JSON: {read_errors[:3]}"
    final = _read_json_file(target)
    assert final in payloads


def test_h8_atomic_no_partial_on_replace_failure(tmp_path: Path, monkeypatch):
    """模拟"写入中断":让 os.replace 抛错(等价于落盘瞬间崩溃)。原子契约要求
    目标文件保持旧的完整内容,绝不出现半截 JSON——因为新内容只存在于临时文件,
    replace 失败时目标根本没被触碰。"""
    target = tmp_path / "status.json"
    _write_json_file(target, {"version": 1, "state": "good"})
    before = target.read_text(encoding="utf-8")

    real_replace = Path.replace

    def boom(self: Path, dst):
        # 只针对本目录的 .tmp → 目标 的 replace 抛错,模拟崩溃/磁盘满。
        if self.name.startswith(".") and self.name.endswith(".tmp"):
            raise OSError("simulated crash during replace")
        return real_replace(self, dst)

    monkeypatch.setattr(Path, "replace", boom)

    with pytest.raises(OSError):
        _write_json_file(target, {"version": 2, "state": "interrupted"})

    # 目标文件未被破坏:仍是旧的完整 JSON,可解析,内容是写入失败前的版本。
    after = target.read_text(encoding="utf-8")
    assert after == before
    assert json.loads(after) == {"version": 1, "state": "good"}
    # 不留临时文件残骸(.tmp 已清理)。
    leftover = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftover == []
