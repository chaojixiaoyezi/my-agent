"""独审18：墓碑可见性、无数据库行的正文、向量重建与删除交错。"""
import json
import subprocess
import sys

import pytest

from agent_py_agent.agent.conversation.learning_source import user_dialogue_projection
from agent_py_agent.agent.local_storage import LocalStore
from agent_py_agent.agent.memory_store.generation_manifest import MemoryGenerationStore
from agent_py_agent.agent.memory_store.read_index import MemoryReadIndex
from agent_py_agent.tests.memory_confirmation_fixtures import ConfirmedRememberWorkflow
from agent_py_agent.tests.test_memory_remove_crash_recovery import _CRASH_SCRIPT
from agent_py_agent.tests.test_remember_learning_source_review import _agent
from agent_py_agent.tests.test_vector_snapshot_consistency import _seeded


# LLM: 子进程先真实搜索留下touch，再从Remember正式删除后退出；新进程恢复不调用内部promote。
# 函数用途: 删除墓碑不能被访问事件遮挡，已缓存touch也不应随删除提交落盘。
def test_search_then_crash_remove_recovers_via_remember(tmp_path):
    program = _CRASH_SCRIPT.replace('    from agent_py_agent.agent.memory_store import jsonl',
        '    assert agent.memory.search("备份")\n'
        '    assert target.entry_id in agent.memory._pending_access\n'
        '    from agent_py_agent.agent.memory_store import jsonl')
    result = subprocess.run([sys.executable, '-c', program, str(tmp_path), 'after-formal'],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 73, result.stdout + result.stderr
    agent = _agent(tmp_path, user_dialogue_projection(), {}, {})
    removed = next(c for c in agent.memory_candidates.list() if c.proposed_action == 'remove')
    outcome = ConfirmedRememberWorkflow(agent).execute(dict(action='remove', entry_id=removed.target_entry_id, origin='user_explicit'))
    assert outcome.ok and json.loads(outcome.output)['saved'], outcome
    assert all(not c.content for c in agent.memory_candidates.list())
    # 恢复收尾发表同代根后 memory.jsonl 只剩根标记；已删条目在已提交事实里只能剩墓碑，缓存的访问不得落盘。
    events = MemoryGenerationStore(agent.memory.path).events()
    assert events is not None
    assert [e.action for e in events if e.entry_id == removed.target_entry_id] == ['remove']


# LLM: SQLite写行前失败保留实际外置正文；不靠直接伪造路径构造孤儿状态。
# 函数用途: 无索引行不等于没有隐私，删除仍需擦除规范记录路径。
def test_localstore_deletes_content_left_before_sql_insert(tmp_path, monkeypatch):
    store = LocalStore(tmp_path / 'derived')
    record_id = store.make_record_id('memory', 'entry-orphan')

    def fail_row(*args):
        raise OSError('before sql insert')

    monkeypatch.setattr(store, '_upsert_record_row', fail_row)
    with pytest.raises(OSError):
        store.upsert_record(source_type='memory', source_id='entry-orphan', title='test', content='ORPHAN_PRIVATE')
    path = store._content_file(record_id)
    assert path.read_text() == 'ORPHAN_PRIVATE'
    assert store.get_record(record_id) is None
    assert store.delete_record(record_id)
    assert not path.exists() or not path.read_text()


# LLM: 在真实重建的嵌入完成阶段插入另一仓库实例删除，原replace_all和持久化均真实执行。
# 函数用途: 旧的active快照不得在删除完成之后重新写回向量正文。
def test_rebuild_snapshot_cannot_restore_deleted_private_content(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store.jsonl import JsonlMemory
    memory = _seeded(tmp_path, ['REBUILD_PRIVATE'])
    [target] = memory.all()
    embed = memory._embed_rebuild_rows

    def delete_after_embedding(records):
        result = embed(records)
        JsonlMemory(memory.path).remove(target.entry_id)
        assert memory.all() == []
        return result

    monkeypatch.setattr(memory, '_embed_rebuild_rows', delete_after_embedding)
    result = memory.rebuild_vectors()
    assert not result['ok'] and result['rebuilt'] == 0, result
    assert 'REBUILD_PRIVATE' not in memory._vector_store_path().read_text()


# LLM: 留下真实提交的旧对象，在另一实例删除完成后补跑原投影写入，不伪造权威状态。
# 函数用途: 延迟的单条LocalStore索引不得重新保存已经删除的正文。
def test_delayed_local_projection_cannot_restore_removed_content(tmp_path):
    from agent_py_agent.agent.memory_store.jsonl import JsonlMemory
    store = LocalStore(tmp_path / 'derived')
    memory = JsonlMemory(tmp_path / 'memory.jsonl', local_store=store)
    target = memory.add('user', 'LATE_LOCAL_PRIVATE', kind='fact')
    JsonlMemory(memory.path, local_store=store).remove(target.entry_id)
    # 文本投影已归owner内读索引且只从已提交事实重建；旧实例补跑投影不能把已删正文写回任何派生库。
    memory.index_all()
    record_id = store.make_record_id('memory', target.entry_id)
    assert store.get_record(record_id) is None
    path = store._content_file(record_id)
    assert not path.exists() or not path.read_text()
    read_index = MemoryReadIndex(memory.path).path
    assert not read_index.exists() or b'LATE_LOCAL_PRIVATE' not in read_index.read_bytes()
    assert memory.search_report('LATE_LOCAL_PRIVATE', 5)[0] == []


# LLM: 删除插在真实单条embedding返回之后，使用独立JsonlMemory实例；向量持久写仍走原入口。
# 函数用途: 慢embedding完成不能在删除后把正文写回向量库。
def test_delayed_vector_projection_cannot_restore_removed_content(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store.jsonl import JsonlMemory
    memory = _seeded(tmp_path, ['LATE_VECTOR_PRIVATE'])
    [target] = memory.all()
    embed = memory._embedder.embed

    def delete_after_embedding(texts):
        vectors = embed(texts)
        JsonlMemory(memory.path).remove(target.entry_id)
        return vectors

    monkeypatch.setattr(memory._embedder, 'embed', delete_after_embedding)
    memory._index_vector(target)
    assert memory.all() == []
    assert 'LATE_VECTOR_PRIVATE' not in memory._vector_store_path().read_text()
