"""Q3：owner账本稳定读取缓存，外部改写/损坏/权限边界与返回副本不能漂移。"""
import json
import os
import time

import pytest

from agent_py_agent.agent.scheduler import repository as module
from agent_py_agent.agent.scheduler.repository import SchedulerStateError
from agent_py_agent.tests.test_scheduler_scan_costs import _repository, _write_store


# LLM: 写真实有效空账本并越过原粗时间窗口，读取仍走正式no-follow入口。
# 函数用途: 准备同一owner的稳定store.json。
def _store(tmp_path):
    repo = _repository(tmp_path / "scheduler")
    _write_store(repo.store_path, {})
    stamp = time.time_ns() - 5_000_000_000
    os.utime(repo.store_path, ns=(stamp, stamp))
    return repo


# LLM: 旁听真实原子读取和JSON解析，不用计时推断缓存收益。
# 函数用途: 统计当前入口实际发生的完整读取与解析次数。
def _observe(monkeypatch):
    reads, parses = [], []
    read, parse = module.read_atomic_store_bytes, module.SchedulerRepository._load_store_from_bytes_unlocked

    # LLM: 保留真实no-follow读取和异常。
    # 函数用途: 统计账本字节读取。
    def read_store(path):
        reads.append(path)
        return read(path)

    # LLM: 保留原schema及owner验证，不绕过实际解析。
    # 函数用途: 统计账本JSON解析。
    def parse_store(self, data):
        parses.append(1)
        return parse(self, data)

    monkeypatch.setattr(module, "read_atomic_store_bytes", read_store)
    monkeypatch.setattr(module.SchedulerRepository, "_load_store_from_bytes_unlocked", parse_store)
    return reads, parses


# LLM: 真实两个入口都必须跳过重复读取与解析，副本深层可变数据不得污染缓存。
# 函数用途: 验证稳定账本三次查询只读取和解析一次，并隔离调用方修改。
@pytest.mark.parametrize("entry", ["_load_store_unlocked", "_store_snapshot"])
def test_stable_store_reuses_payload_and_returns_deep_copy(tmp_path, monkeypatch, entry):
    repo = _store(tmp_path)
    reads, parses = _observe(monkeypatch)
    load = getattr(repo, entry)
    first = load()
    first["jobs"]["injected"] = {"nested": ["caller mutation"]}
    assert load()["jobs"] == {}
    assert load()["jobs"] == {}
    assert len(reads) == len(parses) == 1


# LLM: 同尺寸恢复mtime不能让原地新内容被掩盖；inode/ctime额外守卫应使缓存失效。
# 函数用途: 验证外部原地或原子换代立即可见。
@pytest.mark.parametrize("replacement", [False, True])
def test_store_payload_cache_sees_same_size_restored_mtime_rewrite(tmp_path, replacement):
    repo = _store(tmp_path)
    old = repo._store_snapshot()
    stamp = repo.store_path.stat()
    raw = repo.store_path.read_bytes()
    updated = raw.replace(b'1000.0', b'2000.0')
    assert len(updated) == len(raw) and updated != raw
    if replacement:
        staged = repo.store_path.with_name("staged.json")
        staged.write_bytes(updated)
        os.utime(staged, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        staged.replace(repo.store_path)
    else:
        repo.store_path.write_bytes(updated)
        os.utime(repo.store_path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert old["updated_at"] == 1000.0
    assert repo._store_snapshot()["updated_at"] == 2000.0


# LLM: 缓存不能把坏schema/坏JSON或改过的owner冒充上一代健康账本。
# 函数用途: 验证外部破坏及owner变化仍按原入口报错。
@pytest.mark.parametrize("change", ["json", "schema", "owner", "identity"])
def test_store_payload_cache_never_hides_invalid_current_state(tmp_path, change):
    repo = _store(tmp_path)
    repo._store_snapshot()
    if change == "identity":
        repo.owner["id"] = "different-owner"
    else:
        data = json.loads(repo.store_path.read_text())
        if change == "schema":
            data["schema_version"] = "broken"
        elif change == "owner":
            data["owner"]["id"] = "different-owner"
        repo.store_path.write_text("[broken" if change == "json" else json.dumps(data))
    with pytest.raises(SchedulerStateError):
        repo._store_snapshot()


# LLM: 同目标symlink不是允许的受管普通文件，即使命中旧内容也必须继续拒绝链接。
# 函数用途: 验证热缓存不绕过原no-follow边界。
def test_store_payload_cache_does_not_accept_symlink(tmp_path):
    repo = _store(tmp_path)
    repo._store_snapshot()
    moved = repo.store_path.with_name("original.json")
    repo.store_path.rename(moved)
    repo.store_path.symlink_to(moved)
    with pytest.raises(SchedulerStateError):
        repo._store_snapshot()


# LLM: 原调用方用局部字典执行写入，缓存既不能跟随局部变化，也不能忽略成功原子写的新一代。
# 函数用途: 验证正式写入口后立即读回新版本。
def test_store_payload_cache_observes_formal_writer(tmp_path):
    repo = _store(tmp_path)
    store = repo._load_store_unlocked()
    store["updated_at"] = 3000.0
    with repo.quota_enforcer.admission() as admission:
        repo._write_store_unlocked(store, admission)
    written = json.loads(repo.store_path.read_text())
    assert written["updated_at"] != 1000.0
    assert repo._store_snapshot()["updated_at"] == written["updated_at"]


# LLM: 用开关准确模拟读取跨过2秒保护窗口；读取前不可信的代次不能在发布时被重新认定可信。
# 函数用途: 验证冷读取必须冻结读取开始前的粗时间窗口判定。
def test_store_payload_does_not_cache_if_only_trusted_after_read(tmp_path, monkeypatch):
    repo = _store(tmp_path)
    phase, reads = [False], []
    original_read = module.read_atomic_store_bytes
    monkeypatch.setattr(module, "cache_entry_trustworthy", lambda _stamp: phase[0])

    # LLM: 真实字节仍从原读口取得，只在读完时切换时间窗口状态。
    # 函数用途: 记录读次数并模拟解析期间越过可信时间窗。
    def read(path):
        reads.append(1)
        raw = original_read(path)
        phase[0] = True
        return raw

    monkeypatch.setattr(module, "read_atomic_store_bytes", read)
    repo._load_store_unlocked()
    repo._load_store_unlocked()
    assert len(reads) == 2
