# LLM: 实例锁只保护引用和代次；文件检查与复制在锁外，旧读取不得越过失效代次发布。
# 模块用途: 验证调度账本缓存的读取兼容、输出隔离及失效竞争边界。
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


# LLM: 冷发布与热可变命中均不得在实例缓存锁内执行文件检查、深冻结或深解冻。
# 函数用途: 旁听真实读取入口，验证重工作移出实例锁。
@pytest.mark.parametrize('entry', ['_load_store_unlocked', '_store_snapshot'])
def test_store_payload_checks_and_copies_are_outside_instance_lock(tmp_path, monkeypatch, entry):
    repo = _store(tmp_path)
    frozen, thawed, checked, stated = [], [], [], []
    original_freeze = module._freeze_store_payload
    original_thaw = module._thaw_store_payload
    original_check = module.regular_file_exists_beneath
    original_stamp = module._store_payload_stamp

    # LLM: 保留真实冻结，锁内调用直接暴露为测试失败。
    # 函数用途: 记录冷发布锁外递归冻结。
    def freeze(value):
        assert not repo._store_payload_cache_lock.locked()
        frozen.append(value)
        return original_freeze(value)

    # LLM: 保留真实解冻及输出隔离，锁内调用直接暴露为测试失败。
    # 函数用途: 记录兼容入口热命中的锁外解冻。
    def thaw(value):
        assert not repo._store_payload_cache_lock.locked()
        thawed.append(value)
        return original_thaw(value)

    # LLM: 仍执行原no-follow普通文件检查，不以mock成功替代安全边界。
    # 函数用途: 记录锁外文件检查。
    def check(*args, **kwargs):
        assert not repo._store_payload_cache_lock.locked()
        checked.append(args)
        return original_check(*args, **kwargs)

    # LLM: 完整文件代次检查也必须处于缓存锁外。
    # 函数用途: 记录真实stat入口。
    def stamp(path):
        assert not repo._store_payload_cache_lock.locked()
        stated.append(path)
        return original_stamp(path)

    monkeypatch.setattr(module, '_freeze_store_payload', freeze)
    monkeypatch.setattr(module, '_thaw_store_payload', thaw)
    monkeypatch.setattr(module, 'regular_file_exists_beneath', check)
    monkeypatch.setattr(module, '_store_payload_stamp', stamp)
    load = getattr(repo, entry)
    assert load()['jobs'] == {}
    assert load()['jobs'] == {}
    assert frozen and thawed and checked and stated


# LLM: 解冻旧命中期间的失效必须由末尾CAS看见，不能返回已撤销缓存。
# 函数用途: 验证热命中在锁外解冻之后重新核对引用和代次。
def test_store_payload_hit_invalidated_during_copy_is_discarded(tmp_path, monkeypatch):
    repo = _store(tmp_path)
    repo._load_store_unlocked()
    old = repo._cached_store_payload
    stamp = module._store_payload_stamp(repo.store_path)
    original_copy = module._thaw_store_payload
    observed = []

    # LLM: 先断言锁外，避免旧实现因同步失效而死锁；再触发真实失效。
    # 函数用途: 在热副本产生期间撤销当前缓存代次。
    def copy(value, *args, **kwargs):
        if value is old:
            assert not repo._store_payload_cache_lock.locked()
            observed.append(value)
            repo._invalidate_store_payload()
        return original_copy(value, *args, **kwargs)

    monkeypatch.setattr(module, '_thaw_store_payload', copy)
    assert repo._store_payload_cache_hit(stamp) is None
    assert observed == [old]
    assert repo._cached_store_payload is None


# LLM: 失效令牌必须在读取前捕获，不能仅在remember开始时取新代次。
# 函数用途: 验证读取过程失效后旧flight不会重新发布。
@pytest.mark.parametrize('entry', ['_load_store_unlocked', '_store_snapshot'])
def test_store_payload_invalidation_during_read_prevents_old_publication(tmp_path, monkeypatch, entry):
    repo = _store(tmp_path)
    original_read = module.read_atomic_store_bytes
    reads = []

    # LLM: 返回真实读取字节，但在读取返回前撤销原缓存代次。
    # 函数用途: 建立读取已开始、发布尚未开始的失效窗口。
    def read(path):
        raw = original_read(path)
        reads.append(path)
        repo._invalidate_store_payload()
        return raw

    monkeypatch.setattr(module, 'read_atomic_store_bytes', read)
    assert getattr(repo, entry)()['jobs'] == {}
    assert reads
    assert repo._cached_store_payload is None


# LLM: remember的锁外冻结同样受读取前失效代次约束，不能覆盖新代。
# 函数用途: 验证冷发布冻结期间失效会丢弃待发布快照。
def test_store_payload_invalidation_during_publication_copy_prevents_publish(tmp_path, monkeypatch):
    repo = _store(tmp_path)
    original_copy = module._freeze_store_payload
    copied = []

    # LLM: 原始解析结果仍返回调用者，但冻结期间撤销的缓存不得再发布。
    # 函数用途: 在冷发布深冻结时触发真实失效。
    def copy(value, *args, **kwargs):
        assert not repo._store_payload_cache_lock.locked()
        copied.append(value)
        repo._invalidate_store_payload()
        return original_copy(value, *args, **kwargs)

    monkeypatch.setattr(module, '_freeze_store_payload', copy)
    assert repo._load_store_unlocked()['jobs'] == {}
    assert copied
    assert repo._cached_store_payload is None


# LLM: 检查真实热缓存内部深层容器，调用者不能通过共享引用修改owner或嵌套事实。
# 函数用途: 验证缓存发布的是深不可变快照，而非可变整账本副本。
def test_cached_payload_is_deeply_immutable(tmp_path):
    from agent_py_agent.tests.test_scheduler_scan_costs import _run_row

    repo = _store(tmp_path)
    row = _run_row('srun_selected', 'queued')
    row['execution_context'] = {'evidence': ['original']}
    _write_store(repo.store_path, {'srun_selected': row})
    os.utime(repo.store_path, (time.time() - 5, time.time() - 5))
    repo._load_store_unlocked()
    cached = repo._cached_store_payload
    with pytest.raises(TypeError):
        cached['owner']['id'] = 'foreign'
    with pytest.raises(TypeError):
        cached['runs']['srun_selected']['execution_context']['evidence'][0] = 'changed'


# LLM: 真实千行账本先预热后测量；只查询一个run不得复制无关行，公开嵌套字典仍可修改且相互隔离。
# 函数用途: 用实际分配峰值约束热单记录读取成本，而非仅检查实现是否用了某函数名。
def test_hot_selected_run_does_not_copy_entire_payload(tmp_path):
    import tracemalloc

    from agent_py_agent.tests.test_scheduler_scan_costs import _run_row

    repo = _store(tmp_path)
    rows = {f'srun_{i}': _run_row(f'srun_{i}', 'queued') for i in range(1000)}
    rows['srun_0']['execution_context'] = {'evidence': ['original']}
    _write_store(repo.store_path, rows)
    os.utime(repo.store_path, (time.time() - 5, time.time() - 5))
    assert repo.get_active_run('srun_0')['execution_context']['evidence'] == ['original']
    tracemalloc.start()
    try:
        result = repo.get_active_run('srun_0')
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 128 * 1024, peak
    result['execution_context']['evidence'].append('caller')
    assert repo.get_active_run('srun_0')['execution_context']['evidence'] == ['original']


# LLM: 两个真实冷读在解析处交叠；同实例只解析一次，不同owner的冷读不等待它。
# 函数用途: 验证按仓库隔离的singleflight及公开可变副本隔离。
@pytest.mark.parametrize('entry', ['_load_store_unlocked', '_store_snapshot'])
def test_cold_payload_singleflight_deduplicates_without_blocking_other_owner(tmp_path, monkeypatch, entry):
    import concurrent.futures
    import threading

    repo = _store(tmp_path / 'one')
    other = _store(tmp_path / 'two')
    entered, follower_missed, release = threading.Event(), threading.Event(), threading.Event()
    parses = []
    original_parse = module.SchedulerRepository._load_store_from_bytes_unlocked
    original_hit = module.SchedulerRepository._store_payload_cache_hit

    # LLM: 真解析前暂停leader；保留全部schema及owner检查。
    # 函数用途: 制造重叠的冷读取并统计完整解析次数。
    def parse(self, data):
        if self is repo:
            parses.append(1)
            entered.set()
            assert release.wait(5)
        return original_parse(self, data)

    # LLM: 在真实未命中之后通知主线程，不依赖sleep或线程调度猜测。
    # 函数用途: 确认follower已观察到冷缓存。
    def hit(self, stamp, **kwargs):
        result = original_hit(self, stamp, **kwargs)
        if self is repo and threading.current_thread().name.startswith('follower') and result is None:
            follower_missed.set()
        return result

    monkeypatch.setattr(module.SchedulerRepository, '_load_store_from_bytes_unlocked', parse)
    monkeypatch.setattr(module.SchedulerRepository, '_store_payload_cache_hit', hit)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as leader_pool, \
            concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix='follower') as follower_pool, \
            concurrent.futures.ThreadPoolExecutor(max_workers=1) as other_pool:
        first = leader_pool.submit(getattr(repo, entry))
        try:
            assert entered.wait(5)
            second = follower_pool.submit(getattr(repo, entry))
            assert follower_missed.wait(5)
            assert other_pool.submit(getattr(other, entry)).result(timeout=5)['jobs'] == {}
        finally:
            release.set()
        a, b = first.result(timeout=5), second.result(timeout=5)
    assert len(parses) == 1
    a['jobs']['caller'] = {'nested': [1]}
    assert b['jobs'] == {}


# LLM: singleflight不缓存异常；失败必须释放等待门，下一次仍走真实读取。
# 函数用途: 验证冷解析失败后可重试，不遗留永久进行中的flight。
def test_cold_payload_singleflight_recovers_after_parse_failure(tmp_path, monkeypatch):
    repo = _store(tmp_path)
    original = module.SchedulerRepository._load_store_from_bytes_unlocked
    attempts = []

    # LLM: 仅首次注入原领域错误，下一次保留真实解析。
    # 函数用途: 模拟一次失败且不破坏事实文件。
    def parse(self, data):
        attempts.append(1)
        if len(attempts) == 1:
            raise SchedulerStateError('injected parse failure')
        return original(self, data)

    monkeypatch.setattr(module.SchedulerRepository, '_load_store_from_bytes_unlocked', parse)
    with pytest.raises(SchedulerStateError, match='injected parse failure'):
        repo._load_store_unlocked()
    assert repo._load_store_unlocked()['jobs'] == {}
    assert len(attempts) == 2
