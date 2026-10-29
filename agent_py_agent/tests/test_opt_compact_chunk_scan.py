# LLM: 仅在pytest临时账本观测真实扫描器的I/O；不把合成规模外推生产容量，保留同描述符地址与原字节合同。
# 模块用途: 防止Compact冷扫退回无缓冲逐字节readline，并约束跨块和超长行行为。
from __future__ import annotations

import json
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


# LLM: 不改变系统时间；连续调用真实近期文件，观察扫描器读取起点而非猜测缓存命中。
# 函数用途: 证明新追加只扫描尾部，不受两秒信任窗口或旧记录数量影响。
@pytest.mark.parametrize('rows', [4, 131075])
def test_recent_append_scans_only_new_tail(tmp_path, monkeypatch, rows):
    path = tmp_path / 'recent.jsonl'
    with path.open('wb') as handle:
        for index in range(rows):
            handle.write(json.dumps({'checkpoint_id': str(index)}).encode() + b'\n')
    starts = []
    original = scan._checkpoint_byte_rows

    # LLM: 委托原始分块扫描，不伪造行和地址。
    # 函数用途: 记录真实描述符开始扫描的位置。
    def tracked(handle, through):
        starts.append(handle.tell())
        yield from original(handle, through)

    monkeypatch.setattr(scan, '_checkpoint_byte_rows', tracked)
    with scan.checkpoint_row_lookup(path) as lookup:
        assert lookup(str(rows - 1))['checkpoint_id'] == str(rows - 1)
    before = path.stat().st_size
    with path.open('ab') as handle:
        handle.write(b'{"checkpoint_id":"new"}\n')
    with scan.checkpoint_row_lookup(path) as lookup:
        assert lookup('new') == {'checkpoint_id': 'new'}
    assert starts == [0, before]


# LLM: 完整但无LF的末行不是永久记录边界，追加换行后必须重新解析该行，不能生成伪行。
# 函数用途: 回归无LF末行、换inode、截短及等长改写后的索引失效。
def test_incremental_cache_preserves_physical_row_boundaries(tmp_path):
    path = tmp_path / 'boundaries.jsonl'
    path.write_bytes(b'{"checkpoint_id":"one"}')
    with scan.checkpoint_row_lookup(path) as lookup:
        assert lookup('one') is not None
    with path.open('ab') as handle:
        handle.write(b'\n{"checkpoint_id":"two"}\n')
    with scan.checkpoint_row_lookup(path) as lookup:
        assert lookup('one') is not None
        assert lookup('two') is not None
    path.write_bytes(b'{"checkpoint_id":"new"}\n')
    with scan.checkpoint_row_lookup(path) as lookup:
        assert lookup('new') is not None
        assert lookup('one') is None
    replacement = path.with_suffix('.replacement')
    replacement.write_bytes(b'{"checkpoint_id":"alt"}\n')
    replacement.replace(path)
    with scan.checkpoint_row_lookup(path) as lookup:
        assert lookup('alt') is not None
        assert lookup('new') is None
