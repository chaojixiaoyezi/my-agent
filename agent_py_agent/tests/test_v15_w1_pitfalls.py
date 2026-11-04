"""验证pitfall侧账本，不改动描述符或其他资产状态。"""
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from agent_py_agent.agent.assets import pitfalls


# LLM: 去重以清洗后的casefold内容计算，不以证据或时间重复造条目。
# 函数用途: 验证清洗、内容寻址ID及去重。
def test_pitfall_sanitize_and_deduplicate(tmp_path):
    entry = pitfalls.add_known_pitfall(tmp_path, 'script', 'demo', text='  First\nSecond\x00  ',
        evidence_ref='receipt:one', source='user', recorded_at='2026-11-01T00:00:00+00:00')
    assert isinstance(entry, dict)
    assert entry['text'] == 'FirstSecond'
    assert entry['pitfall_id'] == hashlib.sha256(b'firstsecond').hexdigest()
    assert entry['source'] == 'user' and entry['evidence_ref'] == 'receipt:one'
    assert pitfalls.add_known_pitfall(tmp_path, 'script', 'demo', text='FIRSTSECOND') is None
    path = tmp_path / 'data' / 'asset_evolution' / 'pitfalls' / 'script' / 'demo.json'
    data = json.loads(path.read_text())
    assert data['schema_version'] == 'asset_pitfalls.v1' and data['entries'] == [entry]
    assert not (tmp_path / 'scripts').exists()
    long = pitfalls.add_known_pitfall(tmp_path, 'script', 'demo', text='z' * 300)
    assert len(long['text']) == 240
    assert pitfalls.add_known_pitfall(tmp_path, 'script', 'demo', text='\x00\r\n') is None


# LLM: 存储FIFO最多二十条，读口倒序窗口不修改存储。
# 函数用途: 验证容量、默认读取五条及隔离。
def test_pitfall_fifo_and_newest_window(tmp_path):
    assert pitfalls.read_known_pitfalls(tmp_path, 'workflow', 'empty') == []
    for number in range(24):
        pitfalls.add_known_pitfall(tmp_path, 'workflow', 'demo', text=f'entry {number}')
    entries = pitfalls.read_known_pitfalls(tmp_path, 'workflow', 'demo', limit=99)
    assert isinstance(entries, list)
    assert [item['text'] for item in entries] == [f'entry {number}' for number in reversed(range(4, 24))]
    assert pitfalls.read_known_pitfalls(tmp_path, 'workflow', 'demo') == entries[:5]
    assert pitfalls.read_known_pitfalls(tmp_path, 'workflow', 'demo', limit=0) == []
    assert pitfalls.read_known_pitfalls(tmp_path, 'script', 'demo') == []


# LLM: 两个写者必须锁内重读，不能用锁外旧快照互相覆盖。
# 函数用途: 验证并发新增无丢条目。
def test_pitfall_concurrent_writers(tmp_path):
    barrier = Barrier(2)

    # LLM: 同时调用真实写入接口，不模拟存储结果。
    # 函数用途: 竞争写入一条pitfall。
    def add(text):
        barrier.wait(timeout=10)
        return pitfalls.add_known_pitfall(tmp_path, 'script', 'demo', text=text)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(add, ('first', 'second')))
    assert all(isinstance(item, dict) for item in results)
    assert {row['text'] for row in pitfalls.read_known_pitfalls(tmp_path, 'script', 'demo')} == {'first', 'second'}


# LLM: 非法kind或用户路径不参与账本寻址。
# 函数用途: 验证侧账本身份边界。
@pytest.mark.parametrize('kind,asset_id', [('other', 'demo'), ('script', '../outside')])
def test_pitfall_identity_validation(tmp_path, kind, asset_id):
    with pytest.raises(ValueError):
        pitfalls.add_known_pitfall(tmp_path, kind, asset_id, text='text')
    with pytest.raises(ValueError):
        pitfalls.read_known_pitfalls(tmp_path, kind, asset_id)


# LLM: 只接受route3和user两个明确来源，不以正文推断来源。
# 函数用途: 验证来源枚举。
def test_pitfall_source_validation(tmp_path):
    with pytest.raises(ValueError):
        pitfalls.add_known_pitfall(tmp_path, 'script', 'demo', text='text', source='unknown')
