# LLM: 仅在pytest临时账本观测真实扫描器的I/O；不把合成规模外推生产容量，保留同描述符地址与原字节合同。
# 模块用途: 防止Compact冷扫退回无缓冲逐字节readline，并约束跨块和超长行行为。
from __future__ import annotations

import json
import os
from collections import OrderedDict
from pathlib import Path

import pytest

from agent_py_agent.agent.conversation import compact_checkpoint_scan as scan


# LLM: 代理真实只读文件描述符，仅计量调用，不替换解析或缓存判断；退出时关闭原句柄。
# 类用途: 区分批量readinto与旧readline，核对一次读缓冲的真实上限。
class CountedRaw:
    # LLM: 只持有本次测试句柄及计量对象，不读写文件正文。
    # 函数用途: 将同一描述符的所有操作转交原文件。
    def __init__(self, handle, counts):
        self.handle = handle
        self.counts = counts

    # LLM: 进入本包装但保留真实FD；所有seek/read仍落到原句柄。
    # 函数用途: 让原scanner继续使用上下文管理关闭文件。
    def __enter__(self):
        self.handle.__enter__()
        return self

    # LLM: 异常也通过原文件上下文释放描述符。
    # 函数用途: 保证测试计量不改变关闭语义。
    def __exit__(self, *args):
        return self.handle.__exit__(*args)

    # LLM: 只记录readinto请求长度，返回真实系统读取结果。
    # 函数用途: 测量冷扫的块数和最大缓冲。
    def readinto(self, buffer):
        self.counts['readinto'].append(len(buffer))
        return self.handle.readinto(buffer)

    # LLM: 记录旧逐行入口，仍执行真实readline用于先红对照。
    # 函数用途: 检出无缓冲readline重新进入热扫描路径。
    def readline(self, *args):
        self.counts['readline'] += 1
        return self.handle.readline(*args)

    # LLM: seek/tell/fileno/read等未经修改地调用真实句柄。
    # 函数用途: 不用模拟FD掩盖同描述符复核问题。
    def __getattr__(self, name):
        return getattr(self.handle, name)


# LLM: 使用跨64KiB边界的UTF8、CRLF与无LF尾记录；只替换打开包装，不模拟解析结果。
# 函数用途: 同时核对真实字节偏移、last-wins及冷扫系统读取量。
@pytest.mark.parametrize('ending', [b'\n', b'\r\n', b''])
def test_compact_cold_scan_uses_bounded_readinto(tmp_path, monkeypatch, ending):
    path = tmp_path / 'checkpoints.jsonl'
    row = {'checkpoint_id': 'old', 'summary': '界' * 22000}
    last = {'checkpoint_id': 'old', 'summary': '最终摘要\u0085\u2028'}
    path.write_bytes(json.dumps(row, ensure_ascii=False).encode() + b'\r\n' +
                     b'\n' + json.dumps(last, ensure_ascii=False).encode() + ending)
    counts = {'readinto': [], 'readline': 0}
    original = Path.open

    # LLM: 只为目标账本包装真实无缓冲读取，不碰pytest或其它文件。
    # 函数用途: 收集scanner自己的读取次数，排除构造样本的I/O。
    def opened(self, *args, **kwargs):
        handle = original(self, *args, **kwargs)
        return CountedRaw(handle, counts) if self == path else handle

    monkeypatch.setattr(Path, 'open', opened)
    with scan.checkpoint_row_lookup(path) as lookup:
        assert lookup('old') == last
        assert lookup('missing') is None
    assert counts['readline'] == 0
    assert counts['readinto'] and max(counts['readinto']) <= 65536
    assert len(counts['readinto']) <= (path.stat().st_size + 65535) // 65536 + 1


# LLM: 超长记录必须显式失败，不允许把尾段截掉后假称空账本或完整结果。
# 函数用途: 为流式切块设单行内存预算，避免单个恶意JSON行吞掉整个进程内存。
def test_compact_oversized_row_has_explicit_budget_error(tmp_path):
    path = tmp_path / 'checkpoints.jsonl'
    path.write_text(json.dumps({'checkpoint_id': 'huge', 'summary': 'x' * (8 * 1024 * 1024)}))
    with pytest.raises(OSError, match='record exceeds byte budget'):
        with scan.checkpoint_row_lookup(path):
            pass


# LLM: 仅为真实文件设置旧mtime使其满足原缓存可信窗口，不替换指纹或解析。
# 函数用途: 在不睡眠的情况下建立可复用检查点索引。
def _age_checkpoint(path):
    os.utime(path, ns=(1_000_000_000, 1_000_000_000))


# LLM: 追加必须先证明旧前缀未变，再只解析新物理行；所选行仍进行原字节复查。
# 函数用途: 防止每次追加重解析所有旧JSON。
def test_append_reuses_verified_prefix_without_reparsing(tmp_path, monkeypatch):
    monkeypatch.setattr(scan, '_CHECKPOINT_OFFSETS', OrderedDict())
    path = tmp_path / 'checkpoints.jsonl'
    path.write_bytes(b''.join(json.dumps({'checkpoint_id': str(i)}).encode() + b'\n' for i in range(20)))
    _age_checkpoint(path)
    with scan.checkpoint_row_lookup(path) as lookup:
        assert lookup('0') == {'checkpoint_id': '0'}
    with path.open('ab') as handle:
        handle.write(b'{"checkpoint_id":"new"}\n')
    _age_checkpoint(path)
    parsed = []
    original = scan._parse_row

    # LLM: 计量真实解析调用，不伪造JSON结果。
    # 函数用途: 分辨原始前缀哈希核对与重复JSON解码。
    def counted(raw):
        parsed.append(raw)
        return original(raw)

    monkeypatch.setattr(scan, '_parse_row', counted)
    with scan.checkpoint_row_lookup(path) as lookup:
        assert lookup('new') == {'checkpoint_id': 'new'}
    assert len(parsed) == 2


# LLM: 建索引阶段即受预算约束，索引不完整不能被解释为不存在；仍保留last-wins。
# 函数用途: 对超过索引预算的账本验证恒定空间回源。
def test_offset_index_is_bounded_during_build_and_missing_ids_fall_back(tmp_path, monkeypatch):
    monkeypatch.setattr(scan, '_CHECKPOINT_OFFSETS', OrderedDict())
    monkeypatch.setattr(scan, '_CHECKPOINT_INDEX_MAX_ROWS', 3, raising=False)
    path = tmp_path / 'checkpoints.jsonl'
    path.write_bytes(b''.join(json.dumps({'checkpoint_id': str(i), 'value': i}).encode() + b'\n'
                             for i in range(20)) + b'{"checkpoint_id":"0","value":99}\n')
    _age_checkpoint(path)
    observed = []
    original = scan._cache_offsets

    # LLM: 观察真正交给缓存准入的索引，不替换查找或扫描行为。
    # 函数用途: 检查构建期而非仅驻留期的硬上限。
    def bounded(path, stamp, index):
        observed.append(len(index if isinstance(index, dict) else index.offsets))
        return original(path, stamp, index)

    monkeypatch.setattr(scan, '_cache_offsets', bounded)
    with scan.checkpoint_row_lookup(path) as lookup:
        assert lookup('19') == {'checkpoint_id': '19', 'value': 19}
        assert lookup('0') == {'checkpoint_id': '0', 'value': 99}
        assert lookup('missing') is None
    assert observed and max(observed) <= 3


# LLM: 首尾与mtime不变的中部损坏仍须阻断追加复用，不能只hash文件两端。
# 函数用途: 检查追加优化没有掩盖旧的坏orphan。
def test_append_checks_entire_original_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(scan, '_CHECKPOINT_OFFSETS', OrderedDict())
    path = tmp_path / 'checkpoints.jsonl'
    prefix = b'{"checkpoint_id":"first","padding":"' + b'x' * 70000 + b'"}\n'
    middle = b'{"checkpoint_id":"middle"}\n'
    suffix = b'{"checkpoint_id":"last","padding":"' + b'x' * 70000 + b'"}\n'
    path.write_bytes(prefix + middle + suffix)
    _age_checkpoint(path)
    with scan.checkpoint_row_lookup(path):
        pass
    with path.open('r+b') as handle:
        handle.seek(len(prefix))
        handle.write(b'!')
        handle.seek(0, 2)
        handle.write(b'{"checkpoint_id":"new"}\n')
    _age_checkpoint(path)
    with pytest.raises(OSError, match='ledger is unreadable'):
        with scan.checkpoint_row_lookup(path):
            pass


# LLM: 无LF尾记录不是可复用追加边界，合法接行与非法拼接沿原严格解析处理。
# 函数用途: 防止缓存把一次物理记录拆成两条有效记录。
@pytest.mark.parametrize('valid', [False, True])
def test_unterminated_last_row_is_not_an_append_boundary(tmp_path, monkeypatch, valid):
    monkeypatch.setattr(scan, '_CHECKPOINT_OFFSETS', OrderedDict())
    path = tmp_path / 'checkpoints.jsonl'
    path.write_bytes(b'{"checkpoint_id":"old"}')
    _age_checkpoint(path)
    with scan.checkpoint_row_lookup(path):
        pass
    with path.open('ab') as handle:
        handle.write((b'\n' if valid else b'') + b'{"checkpoint_id":"new"}\n')
    _age_checkpoint(path)
    if valid:
        with scan.checkpoint_row_lookup(path) as lookup:
            assert lookup('new') == {'checkpoint_id': 'new'}
    else:
        with pytest.raises(OSError, match='ledger is unreadable'):
            with scan.checkpoint_row_lookup(path):
                pass
# LLM: 复用真实两代封印与提交链，只延后mtime以满足既有缓存准入，不替换resolver结果。
# 函数用途: 建立可在L1/L2命中的真实摘要测试输入。
def _summary_cache_case(tmp_path):
    from agent_py_agent.tests.test_conversation_compact_wave5 import compact_case

    return compact_case(tmp_path)


# LLM: 冻结层必须递归不可变；只检查缓存内部对象，不把公开可变DTO当成缓存。
# 函数用途: 检出冻结dataclass内部残留的字典和列表。
def _contains_mutable_summary(value):
    from collections.abc import Mapping
    from dataclasses import fields, is_dataclass

    if isinstance(value, (dict, list)):
        return True
    if isinstance(value, Mapping):
        return any(_contains_mutable_summary(item) for item in value.values())
    if is_dataclass(value):
        return any(_contains_mutable_summary(getattr(value, field.name)) for field in fields(value))
    if isinstance(value, (tuple, frozenset)):
        return any(_contains_mutable_summary(item) for item in value)
    return False


# LLM: 实际warm resolve、Applied和deepcopy必须保留值但不无条件复制历史refs；不涉及白名单外活动投影。
# 函数用途: 钉住热缓存整图复制与嵌套可变引用污染两个缺口。
def test_summary_cache_frozen_warm_copy_and_legacy_isolation(tmp_path, monkeypatch):
    from copy import deepcopy

    from agent_py_agent.agent.conversation import compact_summary_view as summaries

    monkeypatch.setattr(summaries, '_VERIFIED_CHAINS', OrderedDict())
    monkeypatch.setattr(summaries, '_SUMMARY_VIEWS', OrderedDict())
    agent, thread, _path, scope, _rows = _summary_cache_case(tmp_path)
    first = summaries.resolve_compact_summary_view(agent, thread, scope)
    key = summaries._checkpoint_cache_key(agent, thread)
    assert key is not None and first.summary == 'summary-2'
    chain = summaries._cached_summary(summaries._VERIFIED_CHAINS, key)
    selected = summaries._cached_summary(summaries._SUMMARY_VIEWS,
        (key, scope, summaries._backend_view_key(agent.backend)))
    assert chain is not None and selected is not None
    assert not _contains_mutable_summary(chain)
    assert not _contains_mutable_summary(selected)

    copies = []
    original = getattr(summaries, 'deepcopy', deepcopy)

    # LLM: 旁听仍执行原复制，不能以假结果让复制计数通过。
    # 函数用途: 观察热resolver与Applied是否调用整图deepcopy。
    def observed(value, *args):
        copies.append(type(value))
        return original(value, *args)

    monkeypatch.setattr(summaries, 'deepcopy', observed, raising=False)
    warm = summaries.resolve_compact_summary_view(agent, thread, scope)
    applied = summaries.AppliedCompactContext(thread.thread_id, scope, warm)
    clone = deepcopy(warm)
    assert copies == []
    assert type(warm.operation_evidence) is dict
    warm.operation_evidence['nested']['n'] = -1
    assert applied.view.operation_evidence == clone.operation_evidence == {'nested': {'n': 2}}
    assert summaries.resolve_compact_summary_view(agent, thread, scope).operation_evidence == {'nested': {'n': 2}}


# LLM: 缓存预算不能repr整图；过深输入只放弃缓存，不允许无限递归或业务读取假成功。
# 函数用途: 核对真实缓存计费入口不用序列化，并守住深度硬界。
def test_summary_cache_cost_no_repr_and_depth_bound(tmp_path, monkeypatch):
    import builtins

    from agent_py_agent.agent.conversation import compact_summary_view as summaries

    monkeypatch.setattr(summaries, '_VERIFIED_CHAINS', OrderedDict())
    monkeypatch.setattr(summaries, '_SUMMARY_VIEWS', OrderedDict())
    calls = []

    # LLM: 保留repr原行为，仅记录调用事实。
    # 函数用途: 捕捉缓存预算偷偷重建整份历史字符串。
    def observed(value):
        calls.append(type(value))
        return builtins.repr(value)

    monkeypatch.setattr(summaries, 'repr', observed, raising=False)
    agent, thread, _path, scope, _rows = _summary_cache_case(tmp_path)
    view = summaries.resolve_compact_summary_view(agent, thread, scope)
    assert view.summary == 'summary-2' and calls == []
    nested = []
    for _ in range(80):
        nested = [nested]
    key = ('depth-probe',)
    summaries._remember_summary(summaries._VERIFIED_CHAINS, key, {'nested': nested})
    assert summaries._cached_summary(summaries._VERIFIED_CHAINS, key) is None


# LLM: 原dataclass按值比较区分list与tuple，冻结不能让仅容器类型变化绕过来源变化检查。
# 函数用途: 钉住嵌套列表被改成元组后仍误判相等的回归。
def test_summary_equality_preserves_list_tuple_distinction():
    from copy import deepcopy

    from agent_py_agent.agent.conversation.compact_summary_view import CompactSummaryView

    view = CompactSummaryView(operation_evidence={'items': [1]})
    changed = deepcopy(view)
    changed.operation_evidence['items'] = (1,)
    assert view != changed


# LLM: 真线程只在首次解冻处交错，不修改缓存或伪造返回值；同一外壳的导出只能发布一个。
# 函数用途: 验证并发首次读取返回同一对象且不会覆盖另一线程已完成的修改。
def test_summary_concurrent_first_export_publishes_one_object(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from agent_py_agent.agent.conversation import compact_summary_view as summaries

    view = summaries.CompactSummaryView(operation_evidence={'items': [1]})
    core = object.__getattribute__(view, '_snapshot').values['operation_evidence']
    original = summaries._thaw_json
    started, release = Event(), Event()

    # LLM: 仅第一个顶层解冻暂停；第二个仍执行原真实解冻，以复现发布覆盖。
    # 函数用途: 精确安排先开始者晚发布的交错。
    def observed(value):
        if value is core and not started.is_set():
            started.set()
            assert release.wait(5)
        return original(value)

    monkeypatch.setattr(summaries, '_thaw_json', observed)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(lambda: view.operation_evidence)
        assert started.wait(5)
        second = pool.submit(lambda: view.operation_evidence).result(timeout=5)
        second['items'].append(2)
        release.set()
        result = first.result(timeout=5)
    assert result is second and view.operation_evidence is second
    assert view.operation_evidence['items'] == [1, 2]


# LLM: 尾部窗口必须按最近物理记录保留；重复ID更新地址和顺序，容量触顶不保留过时的早期窗口。
# 函数用途: 独立约束行数与字节两个准入维度的last-wins索引。
@pytest.mark.parametrize('budget', ['rows', 'bytes'])
def test_full_offset_index_retains_latest_window(tmp_path, monkeypatch, budget):
    monkeypatch.setattr(scan, '_CHECKPOINT_OFFSETS', OrderedDict())
    monkeypatch.setattr(scan, '_CHECKPOINT_INDEX_MAX_ROWS', 2 if budget == 'rows' else 100)
    if budget == 'bytes':
        monkeypatch.setattr(scan, '_CHECKPOINT_INDEX_MAX_BYTES', 256 + 2 * (512 + 4))
    path = tmp_path / 'tail.jsonl'
    path.write_bytes(b''.join(json.dumps({'checkpoint_id': str(i)}).encode() + b'\n' for i in range(6))
                     + b'{"checkpoint_id":"4","value":"last"}\n')
    with path.open('rb', buffering=0) as handle:
        index = scan._build_index(handle, path.stat().st_size)
    assert tuple(index.offsets) == ('5', '4')
    assert index.complete is False
    with scan.checkpoint_row_lookup(path) as lookup:
        assert lookup('4') == {'checkpoint_id': '4', 'value': 'last'}
        assert lookup('0') == {'checkpoint_id': '0'}
        assert lookup('missing') is None


# LLM: 同时覆盖有序字典、dict子类及自定义Mapping；只测试现有公开外壳，不放宽未知对象的拒绝合同。
# 函数用途: 验证冻结映射保留内容、公开解冻为普通dict且嵌套修改互相隔离。
@pytest.mark.parametrize('mapping_kind', ['ordered', 'subclass', 'custom'])
def test_summary_mapping_inputs_freeze_and_thaw_without_data_loss(mapping_kind):
    from collections import UserDict
    from copy import deepcopy

    from agent_py_agent.agent.conversation import compact_summary_view as summaries
    from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE

    # LLM: 使用真正dict子类而非替换冻结实现；继承原容器读写语义。
    # 类用途: 覆盖type(value)精确比较遗漏的兼容输入。
    class DictSubclass(dict):
        pass

    factory = {'ordered': OrderedDict, 'subclass': DictSubclass, 'custom': UserDict}[mapping_kind]
    original = factory({'nested': factory({'items': [1, {'n': 2}]})})
    view = summaries.CompactSummaryView(operation_evidence=original)
    snapshot = object.__getattribute__(view, '_snapshot')
    assert not _contains_mutable_summary(snapshot)
    with pytest.raises(TypeError):
        snapshot.values['operation_evidence']['nested']['extra'] = True
    original['nested']['items'][1]['n'] = 99
    applied = summaries.AppliedCompactContext('thread-mapping', THREAD_COMPACT_SCOPE, view)
    copied = deepcopy(view)
    assert type(view.operation_evidence) is dict
    assert type(view.operation_evidence['nested']) is dict
    assert view.operation_evidence == {'nested': {'items': [1, {'n': 2}]}}
    view.operation_evidence['nested']['items'][1]['n'] = -1
    assert applied.view.operation_evidence == copied.operation_evidence == {'nested': {'items': [1, {'n': 2}]}}


# LLM: 真SQLite地址投影只保存本次固定文件范围；重复ID可能不在尾窗，仍须last-wins并逐行hash复核。
# 函数用途: 核对批量回源只做一次、真实字节地址正确、同长修改拒绝及临时资源正常/异常均释放。
@pytest.mark.parametrize('change', [False, True])
def test_overflow_projection_last_wins_offsets_and_cleanup(tmp_path, monkeypatch, change):
    import sqlite3

    monkeypatch.setattr(scan, '_CHECKPOINT_OFFSETS', OrderedDict())
    monkeypatch.setattr(scan, '_CHECKPOINT_INDEX_MAX_ROWS', 1)
    path = tmp_path / 'overflow.jsonl'
    old = json.dumps({'checkpoint_id': 'old', 'value': '初始'}, ensure_ascii=False).encode() + b'\r\n'
    duplicate = json.dumps({'checkpoint_id': 'old', 'value': '最终'}, ensure_ascii=False).encode() + b'\r\n'
    path.write_bytes(old + duplicate + b'{"checkpoint_id":"tail"}\n')
    databases, directories = [], []
    original_connect = scan.sqlite3.connect

    # LLM: 保留真实SQLite连接，只旁听临时投影路径和生命周期，不伪造查询结果。
    # 函数用途: 证明关闭上下文后连接已关闭且磁盘投影已删除。
    def connected(database, *args, **kwargs):
        result = original_connect(database, *args, **kwargs)
        databases.append(result)
        directories.append(Path(database).parent)
        return result

    monkeypatch.setattr(scan.sqlite3, 'connect', connected)
    with scan.checkpoint_row_lookup(path) as lookup:
        assert lookup('old') == {'checkpoint_id': 'old', 'value': '最终'}
        assert lookup('missing') is None
        assert lookup('old') == {'checkpoint_id': 'old', 'value': '最终'}
        assert len(databases) == 1
        if change:
            with path.open('r+b') as handle:
                handle.seek(len(old))
                handle.write(duplicate.replace('最终'.encode(), '改动'.encode()))
            with pytest.raises(OSError, match='source changed'):
                lookup('old')
    assert directories and all(not directory.exists() for directory in directories)
    for database in databases:
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            database.execute('SELECT 1')


# LLM: 地址预算溢出不是跳过坏orphan的理由；即使head恰在尾窗也必须先验证整个固定账本。
# 函数用途: 在极小索引预算下保留损坏候选的严格拒绝行为。
def test_full_tail_index_does_not_hide_bad_orphan(tmp_path, monkeypatch):
    monkeypatch.setattr(scan, '_CHECKPOINT_INDEX_MAX_ROWS', 1)
    path = tmp_path / 'bad-orphan.jsonl'
    path.write_bytes(b'{"checkpoint_id":"old"}\n{\n{"checkpoint_id":"head"}\n')
    with pytest.raises(OSError, match='ledger is unreadable'):
        with scan.checkpoint_row_lookup(path):
            pass
