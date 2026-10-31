"""同正文交叉删除不得抹去已提交墓碑的派生清理责任。"""
import json
import subprocess
import sys

from agent_py_agent.agent.conversation.learning_source import user_dialogue_projection
from agent_py_agent.agent.local_storage import LocalStore
from agent_py_agent.agent.settings.embedding_profile import embedding_identity
from agent_py_agent.tests._hashing_embedder import LocalHashingEmbedder
from agent_py_agent.tests.memory_confirmation_fixtures import ConfirmedRememberWorkflow
from agent_py_agent.tests.test_remember_learning_source_review import _agent


# LLM: 真实持久LocalStore/VectorStore，仅embedding为离线测试实现，不替换删除路径。
# 函数用途: 为崩溃进程和恢复进程连接相同的派生存储。
def _indexed_agent(path):
    agent = _agent(path, user_dialogue_projection(), {}, {})
    agent.memory.local_store = LocalStore(path / 'derived')
    agent.memory._embedder = LocalHashingEmbedder(dim=64)
    agent.memory._vector_identity = embedding_identity('shared-content-test', agent.memory._embedder)
    return agent


# LLM: 消息由宿主存储生成，使用新请求身份，不向Remember伪造引用或来源资格。
# 函数用途: 新建明确的用户删除请求。
def _remove_request(agent, request):
    params = agent._current_run_params
    params.request_id = request
    params.run_id = request + '-run'
    agent.conversation_store.messages.append(dict(
        thread_id=params.task_attributes['conversation_thread_id'], role='user',
        content='删除项目保存时间规则', metadata={'gateway_request_id': request}))


_CRASH_SHARED = '''
import json, os, sys
from pathlib import Path
from agent_py_agent.tests.test_memory_remove_shared_content import _indexed_agent, _remove_request
from agent_py_agent.tests.memory_confirmation_fixtures import ConfirmedRememberWorkflow
from agent_py_agent.agent.memory_store import jsonl
agent = _indexed_agent(Path(sys.argv[1]))
tool = ConfirmedRememberWorkflow(agent)
for subject in ('source.review.a', 'source.review.b'):
    result = tool.execute(dict(content='项目使用 UTC 保存时间', kind='project', origin='user_explicit', subject_key=subject))
    assert result.ok and json.loads(result.output)['saved'], result
assert len(agent.memory.all()) == 2
target = next(row for row in agent.memory.all() if row.attributes['subject_key'] == 'source.review.a')
# 正式写入不再双写旧LocalStore投影；按升级前遗留的真实副本播种A，删除恢复必须擦除它。
agent.memory.local_store.upsert_record(source_type='memory', source_id=target.entry_id,
                                       title='project:user', content=target.content)
_remove_request(agent, 'req-remove-a')
persist = jsonl.write_private_text_file_atomic_unlocked
removal_written = []
# 删除墓碑可能先写入独立永久账，再发表正式根；在墓碑落盘后的正式根写完即退出。
def interrupted(path, text):
    persist(path, text)
    if '"action": "remove"' in text:
        removal_written.append(path)
    if path == agent.memory.path and removal_written:
        os._exit(73)
jsonl.write_private_text_file_atomic_unlocked = interrupted
result = tool.execute(dict(action='remove', entry_id=target.entry_id, origin='user_explicit'))
raise AssertionError(result)
'''


# LLM: 正式删除A提交后真实退出；B删除脱敏A候选后，仅通过Remember恢复A，不重放正式写入。
# 函数用途: 相同正文的另一条记忆不能让已删除条目的残留索引失去清理入口。
def test_other_removal_redaction_does_not_drop_committed_cleanup(tmp_path):
    crashed = subprocess.run([sys.executable, '-c', _CRASH_SHARED, str(tmp_path)],
                             capture_output=True, text=True, timeout=30)
    assert crashed.returncode == 73, crashed.stdout + crashed.stderr
    agent = _indexed_agent(tmp_path)
    tool = ConfirmedRememberWorkflow(agent)
    removed = next(c for c in agent.memory_candidates.list() if c.proposed_action == 'remove')
    entry_id = removed.target_entry_id
    local = agent.memory.local_store
    local_id = local.make_record_id('memory', entry_id)
    assert local.get_record(local_id) is not None
    assert entry_id in agent.memory._vector_store().ids()
    [other] = agent.memory.all()
    _remove_request(agent, 'req-remove-b')
    result = tool.execute(dict(action='remove', entry_id=other.entry_id, origin='user_explicit'))
    assert result.ok and json.loads(result.output)['saved'], result
    altered = agent.memory_candidates.get(removed.candidate_id)
    assert altered.status == 'superseded' and altered.content_redacted_at and not altered.content
    before = agent.memory.path.read_bytes()
    retried = tool.execute(dict(action='remove', entry_id=entry_id, origin='user_explicit'))
    assert retried.ok, retried
    payload = json.loads(retried.output)
    assert payload['saved'] and not payload['active_memory_changed'], payload
    assert agent.memory.path.read_bytes() == before
    assert local.get_record(local_id) is None
    assert not local._content_file(local_id).exists()
    assert entry_id not in agent.memory._vector_store().ids()
    assert all(not c.content for c in agent.memory_candidates.list())
