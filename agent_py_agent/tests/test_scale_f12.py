"""W8 #115：有界入站轮询、规范文件权威、恢复与保留期回归。"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from agent_py_agent.agent.adapter import ingress
from agent_py_agent.agent.adapter.delivery import (
    GatewayReplyDeliveryStore,
    GatewayReplyDeliveryWorker,
)
from agent_py_agent.agent.adapter.ingress import (
    GatewayAdapterDeliveryWorker,
    GatewayIngressAdvance,
    GatewayIngressStore,
    build_gateway_ingress_record,
)
from agent_py_agent.tests.test_scale_f11 import _count_read_bytes


# LLM: 测试输入使用合成身份，时间由调用者明确指定。
# 函数用途: 构造可重放的规范消息。
def _record(number, *, now=None):
    return build_gateway_ingress_record(channel="test", user_id="actor", conversation_id="room",
        provider_message_id=f"message-{number}", content="hello", metadata={}, timestamp=1.0, now=now)


# LLM: 外部运输只用本地回调替代，持久状态与执行循环均用真实实现。
# 函数用途: 创建推进至完成态的原生 worker。
def _worker(store, seen=None):
    seen = seen if seen is not None else []
    return GatewayAdapterDeliveryWorker(store,
        reply_worker=GatewayReplyDeliveryWorker(GatewayReplyDeliveryStore(None),
            poll_response=lambda _r: None, deliver_response=lambda _r, _t: True),
        prepare_payload=lambda r: {"prompt": r.content},
        submit_payload=lambda r: seen.append(r.ingress_id) or {"request_id": r.ingress_id},
        advance_submission=lambda _r: GatewayIngressAdvance("placeholder_ready", "typing"),
        handoff_reply=lambda _r: None)


# LLM: 仅离线准备；正文/索引/发现戳同步，等价原writer已登记全量历史；单拍不包含数万次fsync建样本。
# 函数用途: 创建全量真实完成历史，保留七天到期字段。
def _seed_completed_history(store, size):
    store._ensure_dirs()
    rows = [replace(_record(n, now=1793740000.0), state="completed") for n in range(size)]
    with store._index.connect() as db:
        for row in rows:
            path = store._record_path(row.ingress_id)
            path.write_text(ingress.json.dumps(row.to_dict()), encoding="utf-8")
            path.chmod(0o600)
            # 原规范writer已留下同名锁文件；否则首次审计会因新建锁文件触发另一批旧目录发现。
            path.with_name(path.name + ".lock").touch(mode=0o600)
        db.executemany("INSERT INTO ingress VALUES (?,?,?,?,?)", [
            (row.ingress_id, row.state, row.created_at, row.created_at, row.updated_at+7*86400) for row in rows])
    assert store.put_if_absent(rows[0]) == (rows[0], False, False)
    assert store.put_if_absent(rows[-1]) == (rows[-1], False, False)
    store._index.set("dir_records", store._directory_stamp(store.root / "ingress" / "records"))


# LLM: 仅单线程规模观测；保留真实RLock，嵌套持锁只计最外层，不计等待时间。
# 类用途: 为原store锁测量可复核的持有时长。
class _MeasuredLock:
    # LLM: 原锁仍是唯一互斥对象。
    # 函数用途: 初始化只读度量状态。
    def __init__(self, lock):
        self.lock, self.depth, self.elapsed = lock, 0, 0.0

    # LLM: 先获得原锁，最外层开始计时。
    # 函数用途: 进入原store锁。
    def __enter__(self):
        self.lock.acquire()
        if self.depth == 0:
            self.started = time.perf_counter()
        self.depth += 1
        return self

    # LLM: 异常时也释放原锁，嵌套不重复累计。
    # 函数用途: 记录持锁时间并退出原锁。
    def __exit__(self, *_args):
        self.depth -= 1
        if self.depth == 0:
            self.elapsed += time.perf_counter()-self.started
        self.lock.release()

    # LLM: 原_is_owned等探针委托实际锁。
    # 函数用途: 保留原锁其它接口。
    def __getattr__(self, name):
        return getattr(self.lock, name)


# LLM: 同样活跃集保留原全部工作计数和FD断言，补实际字节量及最外层持锁时间。
# 函数用途: 实测一千至一万完成态下轮询工作量与描述符差。
@pytest.mark.parametrize("active", [0, 1, 8])
def test_poll_cost_flat_with_completed_history(tmp_path, monkeypatch, active):
    observed = []
    byte_counts, held_times = [], []
    for size in (1000, 10000):
        store = GatewayIngressStore(tmp_path / str(size))
        _seed_completed_history(store, size)
        for n in range(active):
            assert store.put_if_absent(_record(size+n))[1]
        reads = []
        decodes = []
        opens = []
        real_read, real_load = store._read_record, ingress.json.loads
        real_open, real_fdopen = Path.open, os.fdopen
        byte_count, measured = [0], _MeasuredLock(store._lock)

        # LLM: 此计数与原读取入口绑定，锁内读取也记录同一实际路径。
        # 函数用途: 观测规范文件读取数量。
        def read(path):
            reads.append(store._lock._is_owned())
            return real_read(path)

        # LLM: 不用耗时阈值代替真实工作计数。
        # 函数用途: 观测实际 JSON 解码次数。
        def load(*args, **kwargs):
            decodes.append(1)
            return real_load(*args, **kwargs)

        # LLM: 直接计数 Python 文件入口，不把读取函数调用当作实际 open 的替代证据。
        # 函数用途: 观测轮询中规范文件和原子写临时文件的打开数量。
        def opened(path, *args, **kwargs):
            opens.append(store._lock._is_owned())
            return _count_read_bytes(real_open(path, *args, **kwargs), byte_count)

        # LLM: 原no-follow打开及close不变，只观测原句柄。
        # 函数用途: 覆盖描述符读口的字节量。
        def fdopened(*args, **kwargs):
            return _count_read_bytes(real_fdopen(*args, **kwargs), byte_count)

        with monkeypatch.context() as patch:
            patch.setattr(store, "_read_record", read)
            patch.setattr(ingress.json, "loads", load)
            patch.setattr(Path, "open", opened)
            patch.setattr(os, "fdopen", fdopened)
            patch.setattr(store, "_lock", measured)
            before = _fd_count()
            assert _worker(store).run_once() == active
            assert _fd_count() == before
        observed.append((len(reads), len(decodes), sum(reads), len(opens), sum(opens)))
        byte_counts.append(byte_count[0])
        held_times.append(measured.elapsed)
        assert len(reads) <= 256 + active * 12
        assert byte_count[0] < 1024*1024 and measured.elapsed < 5
    print("scale_f12", {"active": active, "work_1k_10k": observed,
        "python_bytes_read": byte_counts, "lock_held_s": held_times, "fd_delta": 0})
    assert observed[0] == observed[1]
    assert abs(byte_counts[0]-byte_counts[1]) < 8192


# LLM: 使用标准库直接观测开放描述符，不引入 psutil。
# 函数用途: 计算当前进程有效描述符数量。
def _fd_count():
    return sum(_is_open(fd) for fd in range(1024))


# LLM: fstat 不新开描述符，探测无效描述符只产生预期异常。
# 函数用途: 判断一个描述符是否有效。
def _is_open(fd):
    try:
        os.fstat(fd)
        return True
    except OSError:
        return False


# LLM: pending 保留非完成态语义，独立 due 查询避免较早退避行饿死后续消息。
# 函数用途: 验证诊断合同、批次上限和调度公平性。
def test_pending_keeps_backoff_but_due_avoids_starvation(tmp_path):
    store = GatewayIngressStore(tmp_path)
    for n in range(140):
        store.put_if_absent(replace(_record(n, now=1.0+n), next_attempt_at=time.time()+600))
    ready = _record(999, now=500.0)
    store.put_if_absent(ready)
    pending = store.pending()
    assert 0 < len(pending) <= 128
    assert pending == sorted(pending, key=lambda r: (r.created_at, r.ingress_id))
    assert all(r.next_attempt_at > time.time() for r in pending)
    seen = []
    assert _worker(store, seen).run_once() == 1
    assert seen == [ready.ingress_id]


# LLM: 运行循环而非 start 承担过期清理；窗口内重放与未完成责任均保留。
# 函数用途: 验证七天边界与不重启清理。
def test_expiry_during_uptime_preserves_window_and_unfinished(tmp_path):
    store = GatewayIngressStore(tmp_path)
    now = time.time()
    expired = replace(_record(1, now=now-8*86400), state="completed")
    boundary = replace(_record(2, now=now-7*86400+30), state="completed")
    pending = replace(_record(3, now=now-9*86400), next_attempt_at=now+600)
    for row in (expired, boundary, pending):
        store.put_if_absent(row)
    assert _worker(store).run_once() == 0
    ids = {r.ingress_id for r in store.records()}
    assert expired.ingress_id not in ids
    assert ids == {boundary.ingress_id, pending.ingress_id}
    assert store.put_if_absent(boundary) == (boundary, False, False)


# LLM: 新测试先在旧实现上真实失败，后续扩充恢复交错检查不改变旧断言。
# 函数用途: 验证持续清理固定批次而非一次读删全部历史。
def test_expiry_is_a_fixed_batch(tmp_path, monkeypatch):
    store = GatewayIngressStore(tmp_path)
    for n in range(300):
        store.put_if_absent(replace(_record(n, now=time.time()-8*86400), state="completed"))
    reads = []
    original = store._read_record
    monkeypatch.setattr(store, "_read_record", lambda p: reads.append(p) or original(p))
    store.prune_terminal_records()
    assert 0 < len(reads) <= 128
    assert len(store.records()) >= 172


# LLM: 只模拟一次投影提交错误；恢复不得靠全目录重扫发现已经发布的责任。
# 函数用途: 验证写前 intent 跨新 store 恢复 canonical→index 缺口。
def test_intent_recovers_canonical_published_before_index_failure(tmp_path, monkeypatch):
    store = GatewayIngressStore(tmp_path)
    store._ensure_dirs()
    original = store._index.settle
    calls = []

    # LLM: 原文件已真实写入后再报索引错误。
    # 函数用途: 模拟一次投影确认失败。
    def settle(*args, **kwargs):
        if not calls:
            calls.append(1)
            raise OSError("simulated index commit error")
        return original(*args, **kwargs)

    monkeypatch.setattr(store._index, "settle", settle)
    row = _record(1)
    with pytest.raises(OSError, match="index commit"):
        store.put_if_absent(row)
    assert store._record_path(row.ingress_id).is_file()
    recovered = GatewayIngressStore(tmp_path)
    assert recovered.pending() == [row]
    assert recovered.put_if_absent(row) == (row, False, False)
    assert recovered._index.audit_ids(intents=True) == []


# LLM: 规范发布前、replace 后错误都需要以当前文件核读，不重放未确认后像。
# 函数用途: 验证两类写入边界保留正确 CAS 与重复语义。
@pytest.mark.parametrize("published", [False, True])
def test_intent_recovers_write_boundary_errors(tmp_path, monkeypatch, published):
    store = GatewayIngressStore(tmp_path)
    row = _record(1)
    original = ingress._write_private_json

    # LLM: 原子写之前或之后仅抛一次普通 IO 异常。
    # 函数用途: 保留真实规范文件的前像或后像。
    def write(path, payload):
        if published:
            original(path, payload)
        raise OSError("simulated canonical write error")

    with monkeypatch.context() as patch:
        patch.setattr(ingress, "_write_private_json", write)
        with pytest.raises(OSError, match="canonical write"):
            store.put_if_absent(row)
    recovered = GatewayIngressStore(tmp_path)
    assert recovered.pending() == ([row] if published else [])
    assert recovered.put_if_absent(row) == (row, not published, False)


# LLM: 索引中的完成态不能盖过规范未完责任；坏文件保持在磁盘且不能被清理。
# 函数用途: 验证 keyset 审计修复过时索引并保守处理损坏规范。
def test_canonical_audit_and_corrupt_records_are_conservative(tmp_path):
    store = GatewayIngressStore(tmp_path)
    row = replace(_record(1), state="completed")
    store.put_if_absent(row)
    pending = replace(row, state="prepared")
    ingress._write_private_json(store._record_path(row.ingress_id), pending.to_dict())
    assert store.pending() == []
    store.maintenance()
    assert store.pending() == [pending]
    path = store._record_path(row.ingress_id)
    path.write_text("{broken", encoding="utf-8")
    assert store.pending() == []
    store.prune_terminal_records(now=time.time()+9*86400)
    assert path.read_text() == "{broken"
    ingress._write_private_json(path, pending.to_dict())
    for _ in range(3):
        if store.pending():
            break
    assert store.pending() == [pending]
    store.close()


# LLM: 旧记录目录没有索引时，流式游标必须逐批前进而不是每轮重扫已消费前缀。
# 函数用途: 验证旧目录回填预算、重启后继续收敛和资源释放。
def test_legacy_backfill_converges_in_fixed_batches_and_closes_fds(tmp_path, monkeypatch):
    directory = tmp_path / "ingress" / "records"
    directory.mkdir(parents=True)
    rows = [_record(n) for n in range(300)]
    for row in rows:
        ingress._write_private_json(directory / f"{row.ingress_id}.json", row.to_dict())
    baseline = _fd_count()
    store = GatewayIngressStore(tmp_path)
    store.maintenance()
    store.close()
    assert _fd_count() == baseline
    store = GatewayIngressStore(tmp_path)
    counts = []
    original = store._read_record
    visits = []
    monkeypatch.setattr(store, "_read_record", lambda p: visits.append(p.stem) or original(p))
    for _ in range(12):
        before = len(visits)
        store.maintenance()
        counts.append(len(visits)-before)
        if store._index.get("legacy") == "0" and not store._scans:
            break
    assert max(counts) <= 256
    assert set(visits) == {r.ingress_id for r in rows}
    assert len(store.records()) == 300
    store.close()
    assert _fd_count() == baseline
    restarted = GatewayIngressStore(tmp_path)
    assert len(restarted.pending()) == 128
    assert _fd_count() == baseline


# LLM: 两个 spool 即使消息身份相同也互不影响，重放不能覆盖另一根或新 CAS。
# 函数用途: 验证隔离根、同进程交错 CAS 与轮询并发登记。
def test_two_spools_and_interleaved_put_cas(tmp_path):
    first = GatewayIngressStore(tmp_path / "first")
    other = GatewayIngressStore(tmp_path / "other")
    row = _record(1)
    first.put_if_absent(row)
    other.put_if_absent(row)
    entered, release = threading.Event(), threading.Event()

    # LLM: 回调阻塞期间新入站仍应能拿到存储锁。
    # 函数用途: 建立可重复的 poll/put 交错。
    def prepare(record):
        entered.set()
        assert release.wait(3)
        return {"prompt": record.content}

    worker = _worker(first)
    worker._prepare_payload = prepare
    thread = threading.Thread(target=worker.run_once)
    thread.start()
    try:
        assert entered.wait(3)
        extra = _record(2)
        assert first.put_if_absent(extra) == (extra, True, False)
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert first.pending() == [extra]
    assert other.pending() == [row]
    updated = replace(extra, next_attempt_at=time.time()+30)
    assert first.compare_and_swap(extra, updated)
    assert not GatewayIngressStore(tmp_path / "first").compare_and_swap(extra, replace(extra, next_attempt_at=5.0))


# LLM: 并行进程必须走同一 OS 路径锁而不是只依赖 store 内的 RLock。
# 函数用途: 子进程竞争首次写入和同一快照 CAS。
def _cas_process(root, barrier, results):
    store = GatewayIngressStore(Path(root))
    row = _record(1, now=1.0)
    barrier.wait(10)
    created = store.put_if_absent(row)[1]
    barrier.wait(10)
    changed = store.compare_and_swap(row, replace(row, next_attempt_at=123.0))
    results.put((created, changed))


# LLM: 双进程创建与 CAS 各只能有一个赢家，进程退出后无悬空连接。
# 函数用途: 验证真实跨进程互斥。
def test_process_cas_has_one_winner(tmp_path):
    import multiprocessing
    context = multiprocessing.get_context("spawn")
    barrier, results = context.Barrier(2), context.Queue()
    processes = [context.Process(target=_cas_process, args=(str(tmp_path), barrier, results)) for _ in range(2)]
    try:
        for process in processes:
            process.start()
        values = [results.get(timeout=15) for _ in processes]
        for process in processes:
            process.join(15)
            assert process.exitcode == 0
        assert sum(value[0] for value in values) == 1
        assert sum(value[1] for value in values) == 1
        assert GatewayIngressStore(tmp_path).pending()[0].next_attempt_at == 123.0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)
        results.close()
        results.join_thread()


# LLM: 已确认提交的阶段不能在 handoff 中断后再次提交；只有未确认的 handoff 可重试。
# 函数用途: 验证真实 worker 重启跨租约恢复与回调重放。
def test_worker_restart_replays_handoff_not_submission(tmp_path, monkeypatch):
    first = GatewayIngressStore(tmp_path)
    seen = []
    worker = _worker(first, seen)
    row = _record(1)
    worker.enqueue(row)

    # LLM: BaseException 模拟进程阶段中断，不被普通暂态处理吞掉。
    # 类用途: 结束当前执行入口以留下规范租约。
    class Halt(BaseException):
        pass

    # LLM: 中断只发生在提交已确认之后。
    # 函数用途: 保留未确认的原 handoff 阶段。
    def handoff(_row):
        raise Halt()

    worker._handoff_reply = handoff
    with pytest.raises(Halt):
        worker.run_once()
    restarted = _worker(GatewayIngressStore(tmp_path), seen)
    future = time.time()+120
    monkeypatch.setattr(ingress.time, "time", lambda: future)
    assert restarted.run_once() == 1
    assert seen == [row.ingress_id]
    assert restarted.store.records()[0].state == "completed"


# LLM: 候选过期不意味着规范仍然可删，投影可以比实际规范状态旧。
# 函数用途: 确认过期清理不能误删规范未完责任或窗口内回执。
def test_expiry_rechecks_canonical_under_path_lock(tmp_path):
    store = GatewayIngressStore(tmp_path)
    old = replace(_record(1, now=time.time()-8*86400), state="completed")
    store.put_if_absent(old)
    unfinished = replace(old, state="prepared")
    path = store._record_path(old.ingress_id)
    ingress._write_private_json(path, unfinished.to_dict())
    store.prune_terminal_records()
    assert path.is_file() and store.pending() == [unfinished]
    assert store.compare_and_swap(unfinished, old)
    retained = replace(old, updated_at=time.time())
    ingress._write_private_json(path, retained.to_dict())
    store.prune_terminal_records()
    assert store.records() == [retained]


# LLM: 从首次登记到完成再到七天后清理均由原入口推进，不手写终态证明。
# 函数用途: 验证规范 ingress 的完整本地生命周期。
def test_ingress_worker_lifecycle_through_expiry(tmp_path):
    store = GatewayIngressStore(tmp_path)
    row = _record(1)
    worker = _worker(store)
    assert worker.enqueue(row) == (row, True, False)
    assert worker.run_once() == 1
    completed, = store.records()
    assert completed.state == "completed"
    assert store.put_if_absent(row) == (completed, False, False)
    store.prune_terminal_records(now=completed.updated_at+7*86400)
    assert store.records() == [completed]
    store.prune_terminal_records(now=completed.updated_at+7*86400+1)
    assert store.records() == []


# LLM: 子进程真实退出不经过 Python finally，验证写前 intent 已经耐久而非仅保存在内存。
# 函数用途: 核验规范已发布、投影尚未确认时的进程中断恢复。
def test_process_exit_between_canonical_and_index_commit(tmp_path):
    import subprocess
    import sys

    script = """
import os, sys
from pathlib import Path
from agent_py_agent.agent.adapter.ingress import GatewayIngressStore, build_gateway_ingress_record
store = GatewayIngressStore(Path(sys.argv[1]))
store._ensure_dirs()
store._index.settle = lambda *args, **kwargs: os._exit(23)
store.put_if_absent(build_gateway_ingress_record(channel='test', user_id='actor',
    conversation_id='room', provider_message_id='message-1', content='hello', metadata={},
    timestamp=1.0, now=1.0))
"""
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 23, result.stdout + result.stderr
    recovered = GatewayIngressStore(tmp_path)
    expected = _record(1, now=1.0)
    assert recovered.pending() == [expected]
    assert recovered.put_if_absent(expected) == (expected, False, False)
    with recovered._index.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 0


# LLM: 首次就绪与退避重试必须在同一时间轴，持续新消息不得饿死更早到期的原责任。
# 函数用途: 复现零 due 时间让已到期重试排在所有新消息之后的问题。
def test_due_retry_is_not_starved_by_continuous_new_arrivals(tmp_path):
    store = GatewayIngressStore(tmp_path)
    now = time.time()
    retry = replace(_record("retry", now=now-100), next_attempt_at=now-50)
    store.put_if_absent(retry)
    for tick in range(3):
        for n in range(128):
            store.put_if_absent(_record(f"new-{tick}-{n}", now=now-10+tick))
        assert store.due(now)[0].ingress_id == retry.ingress_id


# LLM: 旧目录发现也由 pending 有界推进，不能先返回空再指望调用者碰巧启动 worker。
# 函数用途: 验证没有 SQLite 投影的旧责任在直接调用 pending 时可见。
def test_first_pending_discovers_legacy_responsibility(tmp_path):
    row = _record("legacy", now=1)
    directory = tmp_path / "ingress" / "records"
    directory.mkdir(parents=True)
    ingress._write_private_json(directory / f"{row.ingress_id}.json", row.to_dict())
    assert GatewayIngressStore(tmp_path).pending() == [row]


# LLM: 每拍都重建 store 模拟反复重启；发现游标必须落盘且任何拍末不保留 FD。
# 函数用途: 验证迁移余量真正缩小，不因重启回到同一个目录前缀。
def test_legacy_discovery_survives_every_tick_restart_without_fd_retention(tmp_path):
    directory = tmp_path / "ingress" / "records"
    directory.mkdir(parents=True)
    rows = [_record(n, now=1) for n in range(400)]
    for row in rows:
        ingress._write_private_json(directory / f"{row.ingress_id}.json", row.to_dict())
    baseline = _fd_count()
    known = 0
    for _ in range(20):
        store = GatewayIngressStore(tmp_path)
        store.maintenance()
        assert _fd_count() == baseline
        with store._index.connect() as db:
            count = db.execute("SELECT COUNT(*) FROM ingress").fetchone()[0]
        assert count >= known
        known = count
        if store._index.get("legacy") == "0":
            break
    assert known == len(rows)
    assert _fd_count() == baseline


# LLM: 索引只是可重建提示，不能沿链接写到其它路径；失败必须发生在 SQLite 或 chmod 副作用前。
# 函数用途: 验证现有索引、父目录和已打开索引的换链均被拒绝。
@pytest.mark.parametrize("location", ["database", "directory", "reconnect"])
def test_index_rejects_links_without_changing_target(tmp_path, location):
    import sqlite3

    root = tmp_path / "spool"
    target = tmp_path / "other"
    target.mkdir()
    victim = target / "index.sqlite3"
    with sqlite3.connect(victim) as db:
        db.execute("CREATE TABLE sentinel (value TEXT)")
        db.execute("INSERT INTO sentinel VALUES ('untouched')")
    victim.chmod(0o644)
    before_bytes = victim.read_bytes()
    before = victim.stat()
    store = GatewayIngressStore(root)
    if location == "directory":
        root.mkdir()
        (root / "ingress").symlink_to(target, target_is_directory=True)
    else:
        store._ensure_dirs()
        database = root / "ingress" / "index.sqlite3"
        database.unlink()
        database.symlink_to(victim)
        if location == "database":
            store = GatewayIngressStore(root)
    with pytest.raises((OSError, ValueError)):
        store.pending()
    assert victim.read_bytes() == before_bytes
    assert victim.stat().st_mode == before.st_mode
    assert sorted(p.name for p in target.iterdir()) == ["index.sqlite3"]


# LLM: 投影键不是可信文件路径，不能等到读取后才比较正文身份。
# 函数用途: 验证候选越目录键在打开规范路径之前被拒绝。
def test_index_candidate_cannot_escape_canonical_directory(tmp_path, monkeypatch):
    store = GatewayIngressStore(tmp_path)
    store._ensure_dirs()
    victim = tmp_path / "outside.json"
    victim.write_text("keep", encoding="utf-8")
    opened = []
    original = store._read_record
    monkeypatch.setattr(store, "_read_record", lambda p: opened.append(p) or original(p))
    with store._index.connect() as db:
        db.execute("INSERT INTO ingress VALUES ('../../outside', 'prepared', 1, 1, NULL)")
    with pytest.raises((OSError, ValueError)):
        store.pending()
    assert opened == []
    assert victim.read_text() == "keep"
