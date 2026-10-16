"""真实删除恢复入口：旧add中断、全批时间校验及准确删除回执。"""
import json
import subprocess
import sys

import pytest

from agent_py_agent.agent.conversation.learning_source import user_dialogue_projection
from agent_py_agent.agent.local_storage import LocalStore
from agent_py_agent.tests.memory_confirmation_fixtures import ConfirmedRememberWorkflow
from agent_py_agent.tests.test_memory_remove_crash_recovery import _CRASH_SCRIPT
from agent_py_agent.tests.test_memory_remove_entry_recovery import _crashed_agent
from agent_py_agent.tests.test_remember_learning_source_review import _agent


# LLM: 两次独立进程退出分别卡在add正式提交后、旧格式remove正式提交后；最终只走Remember。
# 函数用途: 缺promotion_ref的已提交add也必须随历史删除清除，不能残留可重新晋升的正文。
def test_legacy_removal_cleans_interrupted_add_candidate(tmp_path):
    prefix, tail = _CRASH_SCRIPT.split('added = ', 1)
    add_line, _, rest = tail.split('\n', 2)
    add_program = prefix + 'agent.memory_candidates.mark_promoted_admitted = lambda *args: os._exit(73)\n' + add_line
    first = subprocess.run([sys.executable, '-c', add_program, str(tmp_path), 'add'],
                           capture_output=True, text=True, timeout=30)
    assert first.returncode == 73, first.stdout + first.stderr
    agent = _agent(tmp_path, user_dialogue_projection(), {}, {})
    [original] = agent.memory_candidates.list()
    assert original.status == 'approved' and not original.promotion_ref and not original.target_entry_id
    program = (prefix + rest).replace('target = agent.memory.all()[0]',
        'params = agent._current_run_params\n'
        'params.request_id = "req-replace-after-crash"\n'
        'params.run_id = "run-replace-after-crash"\n'
        'agent.conversation_store.messages.append(dict(thread_id=params.task_attributes["conversation_thread_id"], '
        'role="user", content="把项目方案替换为：项目新方案保留两份备份", '
        'metadata={"gateway_request_id": params.request_id}))\n'
        'target = agent.memory.all()[0]')
    program = program.replace('        persist(path, text)',
        '        rows = [json.loads(row) for row in text.splitlines()]\n'
        '        for row in rows: row.pop("removed_content_hashes", None)\n'
        '        text = "\\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\\n"\n'
        '        persist(path, text)')
    second = subprocess.run([sys.executable, '-c', program, str(tmp_path), 'after-formal'],
                            capture_output=True, text=True, timeout=30)
    assert second.returncode == 73, second.stdout + second.stderr
    agent = _agent(tmp_path, user_dialogue_projection(), {}, {})
    removed = next(c for c in agent.memory_candidates.list() if c.proposed_action == 'remove')
    result = ConfirmedRememberWorkflow(agent).execute(dict(action='remove', entry_id=removed.target_entry_id, origin='user_explicit'))
    assert result.ok and json.loads(result.output)['saved'], result
    assert all(not c.content for c in agent.memory_candidates.list())
    assert not agent.memory_promotion.promote(original.candidate_id, automatic=True).promoted
    assert agent.memory.all() == []


# LLM: 全批校验必须发生在observe/恢复清理之前；时间错误不能因条目已删除而被忽略。
# 函数用途: 混合恢复与新增时，坏时间整批零副作用。
@pytest.mark.parametrize('times', [dict(valid_until='not-a-date'), dict(valid_from='2030-01-01T00:00:00'),
    dict(valid_from='2031-01-01T00:00:00+00:00', valid_until='2030-01-01T00:00:00+00:00')])
def test_invalid_recovery_time_rejects_entire_batch(tmp_path, times):
    agent, removed = _crashed_agent(tmp_path)
    before = agent.memory_candidates.path.read_bytes(), agent.memory.path.read_bytes()
    result = ConfirmedRememberWorkflow(agent).execute(dict(action='batch', operations=[
        dict(action='add', content='项目使用 UTC 保存时间', kind='project', origin='user_explicit', subject_key='source.review'),
        dict(action='remove', entry_id=removed.target_entry_id, origin='user_explicit', **times)]))
    assert not result.ok, result
    assert (agent.memory_candidates.path.read_bytes(), agent.memory.path.read_bytes()) == before


# LLM: 首次删除确已落盘，真实LocalStore清理抛错后必须报告变更；重试只有清理不能算新变更。
# 函数用途: 删除失败收尾与后续恢复提示不能说“正式记忆没变”或“已经记着”。
def test_remove_cleanup_failure_and_retry_report_actual_effects(tmp_path, monkeypatch):
    agent = _agent(tmp_path, user_dialogue_projection(), {}, {})
    agent.memory.local_store = LocalStore(tmp_path / 'derived')
    tool = ConfirmedRememberWorkflow(agent)
    added = tool.execute(dict(content='项目使用 UTC 保存时间', kind='project', origin='user_explicit', subject_key='source.review'))
    assert added.ok and json.loads(added.output)['saved']
    [target] = agent.memory.all()
    original = agent.memory.local_store.delete_record

    def fail_delete(*args):
        raise OSError('index unavailable')

    monkeypatch.setattr(agent.memory.local_store, 'delete_record', fail_delete)
    result = tool.execute(dict(action='remove', entry_id=target.entry_id, origin='user_explicit'))
    payload = json.loads(result.output)
    assert agent.memory.all() == []
    assert payload['active_memory_changed'] and not payload['saved'], payload
    assert '清理' in payload['hint'] and '正式记忆没变' not in payload['hint']
    monkeypatch.setattr(agent.memory.local_store, 'delete_record', original)
    retried = json.loads(tool.execute(dict(action='remove', entry_id=target.entry_id, origin='user_explicit')).output)
    assert retried['saved'] and not retried['active_memory_changed'], retried
    assert '删除' in retried['hint'] and '已经记着' not in retried['hint']
