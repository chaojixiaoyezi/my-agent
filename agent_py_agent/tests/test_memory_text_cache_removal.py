"""Delete recovery must clear text-hash vectors, including without an embedder."""
import json
import subprocess
import sys

from agent_py_agent.agent.retrieval import text_vector_cache as vectors
from agent_py_agent.tests.memory_confirmation_fixtures import ConfirmedRememberWorkflow
from agent_py_agent.tests.test_memory_remove_shared_content import _indexed_agent, _remove_request


# LLM: 真实正式库与缓存，索引文本包含keywords_en；另设活跃对照键，不能通过清空整份缓存使测试变绿。
# 函数用途: 创建一个待删条目与必须保留的对照缓存。
def _cached_target(path):
    agent = _indexed_agent(path)
    tool = ConfirmedRememberWorkflow(agent)
    added = tool.execute(dict(content='项目使用 UTC 保存时间', kind='project', origin='user_explicit', subject_key='cache.target'))
    assert added.ok and json.loads(added.output)['saved']
    [target] = agent.memory.all()
    [target, control] = agent.memory.apply_batch([
        dict(action='replace', entry_id=target.entry_id, expected_version=target.version, content=target.content,
             attributes={**target.attributes, 'keywords_en': ['timezone', 'UTC']}),
        dict(action='add', content='对照条目不应删除', kind='fact', attributes={'keywords_en': ['control']})])
    assert target.attributes['keywords_en'] == ['timezone', 'UTC']
    cache = agent.memory._text_vector_cache()
    keys = [vectors.text_cache_key(fingerprint, vectors.index_text(row.content, row.attributes))
            for row in (target, control) for fingerprint in ('original', 'previous-model')]
    cache.put({key: [0.1, 0.2] for key in keys})
    assert not cache.last_write_error
    return agent, target, set(keys[:2]), set(keys[2:])


_CRASH = '''
import os, sys
from pathlib import Path
from agent_py_agent.tests.test_memory_text_cache_removal import _cached_target
from agent_py_agent.tests.test_memory_remove_shared_content import _remove_request
from agent_py_agent.tests.memory_confirmation_fixtures import ConfirmedRememberWorkflow
from agent_py_agent.agent.memory_store import jsonl
agent, target, _, _ = _cached_target(Path(sys.argv[1]))
_remove_request(agent, 'cache-crash')
persist = jsonl.write_private_text_file_atomic_unlocked
def crash(path, text):
    persist(path, text)
    if path == agent.memory.path and '"action": "remove"' in text:
        os._exit(73)
jsonl.write_private_text_file_atomic_unlocked = crash
ConfirmedRememberWorkflow(agent).execute(dict(action='remove', entry_id=target.entry_id, origin='user_explicit'))
raise AssertionError('crash was not injected')
'''


# LLM: 真实子进程在墓碑持久化后退出；恢复进程关闭embedding，必须只清孤儿hash并保留活跃对照。
# 函数用途: 验证崩溃重启后的文本向量清理，不依赖已删除正文或当前模型指纹。
def test_crash_recovery_removes_text_vectors_without_embedder(tmp_path):
    crashed = subprocess.run([sys.executable, '-c', _CRASH, str(tmp_path)], capture_output=True, text=True, timeout=30)
    assert crashed.returncode == 73, crashed.stdout + crashed.stderr
    agent = _indexed_agent(tmp_path)
    agent.memory._embedder = None
    removal = next(c for c in agent.memory_candidates.list() if c.proposed_action == 'remove')
    cache_path = agent.memory.path.parent / 'memory_text_vectors.json'
    before = json.loads(cache_path.read_text())
    [control] = agent.memory.all()
    keep = {vectors.text_cache_key(fp, vectors.index_text(control.content, control.attributes))
            for fp in ('original', 'previous-model')}
    assert keep < set(before)
    formal = agent.memory.path.read_bytes()
    payload = json.loads(ConfirmedRememberWorkflow(agent).execute(dict(action='remove', entry_id=removal.target_entry_id, origin='user_explicit')).output)
    assert payload['saved'] and not payload['active_memory_changed'], payload
    assert set(json.loads(cache_path.read_text())) == keep
    assert agent.memory.path.read_bytes() == formal


# LLM: 缓存落盘返回False而非抛异常，正式删除仍已提交；连续恢复与解除故障后的回执必须分开。
# 函数用途: 防止吞掉缓存写失败后宣称隐私清理完整成功。
def test_text_vector_write_failure_is_cleanup_pending(tmp_path, monkeypatch):
    agent, target, removed, keep = _cached_target(tmp_path)
    _remove_request(agent, 'cache-write-failure')
    tool = ConfirmedRememberWorkflow(agent)
    with monkeypatch.context() as patch:
        patch.setattr(vectors, '_write_atomic_unlocked', lambda *args: False)
        first = json.loads(tool.execute(dict(action='remove', entry_id=target.entry_id, origin='user_explicit')).output)
        assert first['active_memory_changed'] and not first['saved'], first
        assert first['results'][0]['reason_code'] == 'REMOVE_CLEANUP_PENDING'
        retry = json.loads(tool.execute(dict(action='remove', entry_id=target.entry_id, origin='user_explicit')).output)
        assert not retry['active_memory_changed'] and not retry['saved'], retry
    formal = agent.memory.path.read_bytes()
    recovered = json.loads(tool.execute(dict(action='remove', entry_id=target.entry_id, origin='user_explicit')).output)
    assert recovered['saved'] and not recovered['active_memory_changed'], recovered
    remaining = set(json.loads((agent.memory.path.parent / 'memory_text_vectors.json').read_text()))
    assert not remaining & removed
    assert keep <= remaining
    assert agent.memory.path.read_bytes() == formal


# LLM: 当前真实embedder指纹，两条活跃事实共享index_text；正常删除链不能在孤儿回收后再删共享键。
# 函数用途: 验证保留其它活跃同文事实的文本向量。
def test_normal_remove_retains_active_same_text_cache(tmp_path):
    agent = _indexed_agent(tmp_path)
    tool = ConfirmedRememberWorkflow(agent)
    for key in ('cache.same-a', 'cache.same-b'):
        result = tool.execute(dict(content='项目使用 UTC 保存时间', kind='project', origin='user_explicit', subject_key=key))
        assert result.ok and json.loads(result.output)['saved']
    first, other = agent.memory.all()
    fingerprint = vectors.embedder_fingerprint(agent.memory._embedder)
    key = vectors.text_cache_key(fingerprint, vectors.index_text(first.content, first.attributes))
    assert key == vectors.text_cache_key(fingerprint, vectors.index_text(other.content, other.attributes))
    cache = agent.memory._text_vector_cache()
    cache.put({key: [0.1, 0.2]})
    _remove_request(agent, 'cache-same-delete')
    payload = json.loads(tool.execute(dict(action='remove', entry_id=first.entry_id, origin='user_explicit')).output)
    assert payload['saved'], payload
    assert [row.entry_id for row in agent.memory.all()] == [other.entry_id]
    assert key in json.loads((agent.memory.path.parent / 'memory_text_vectors.json').read_text())
