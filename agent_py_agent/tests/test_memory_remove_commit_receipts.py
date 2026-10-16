"""批次前置校验与正式删除后候选I/O失败的真实Remember回执。"""
import json

import pytest

from agent_py_agent.tests.memory_confirmation_fixtures import ConfirmedRememberWorkflow
from agent_py_agent.tests.test_memory_remove_shared_content import _indexed_agent, _remove_request


# LLM: 先建真实持久存储基线，再将合法操作放在负epoch参数前；拒绝必须先于全批副作用。
# 函数用途: 过期时间的底层限制不能到第一条正式提交后才检查。
def test_negative_expiry_rejects_whole_batch_without_partial_commit(tmp_path):
    agent = _indexed_agent(tmp_path)
    tool = ConfirmedRememberWorkflow(agent)
    added = tool.execute(dict(content='项目使用 UTC 保存时间', kind='project', origin='user_explicit', subject_key='source.seed'))
    assert added.ok and json.loads(added.output)['saved']
    files = [agent.memory.path, agent.memory_candidates.path, agent.memory._vector_store_path()]
    before = [path.read_bytes() for path in files]
    before_ids = agent.memory._vector_store().ids()
    outcome = tool.execute(dict(action='batch', operations=[
        dict(action='add', content='项目使用 UTC 保存时间', kind='project', origin='user_explicit', subject_key='source.valid'),
        dict(action='add', content='项目使用 UTC 保存时间', kind='project', origin='user_explicit', subject_key='source.invalid',
             valid_until='1969-12-31T23:59:59+00:00')]))
    assert not outcome.ok and outcome.error_code == 'TOOL_INVALID_ARGUMENTS', outcome
    assert [path.read_bytes() for path in files] == before
    assert agent.memory._vector_store().ids() == before_ids
    assert len(agent.memory.all()) == 1


# LLM: 只在正式提交后的候选清理点注入I/O故障；连续失败恢复不计新提交，解除故障后仅清理。
# 函数用途: 候选脱敏和最终标记失败均须返回准确的提交事实及待清理责任。
@pytest.mark.parametrize('method', ['redact_content_admitted', 'mark_promoted_admitted'])
def test_candidate_cleanup_io_reports_committed_and_recovered_effects(tmp_path, monkeypatch, method):
    agent = _indexed_agent(tmp_path)
    tool = ConfirmedRememberWorkflow(agent)
    added = tool.execute(dict(content='项目使用 UTC 保存时间', kind='project', origin='user_explicit', subject_key='source.review'))
    assert added.ok and json.loads(added.output)['saved']
    [target] = agent.memory.all()
    _remove_request(agent, 'req-candidate-io')

    def fail_candidate_write(*args, **kwargs):
        raise OSError('candidate cleanup unavailable')

    with monkeypatch.context() as patch:
        patch.setattr(agent.memory_candidates, method, fail_candidate_write)
        outcome = tool.execute(dict(action='remove', entry_id=target.entry_id, origin='user_explicit'))
        payload = json.loads(outcome.output)
        assert agent.memory.all() == []
        assert payload['active_memory_changed'] and not payload['saved'], payload
        assert payload['results'][0]['reason_code'] == 'REMOVE_CLEANUP_PENDING', payload
        assert '清理' in payload['hint'] and '正式记忆没变' not in payload['hint']
        before = agent.memory.path.read_bytes()
        retry = json.loads(tool.execute(dict(action='remove', entry_id=target.entry_id, origin='user_explicit')).output)
        assert not retry['active_memory_changed'] and not retry['saved'], retry
        assert retry['results'][0]['reason_code'] == 'REMOVE_CLEANUP_PENDING', retry
        assert agent.memory.path.read_bytes() == before
    recovered = json.loads(tool.execute(dict(action='remove', entry_id=target.entry_id, origin='user_explicit')).output)
    assert recovered['saved'] and not recovered['active_memory_changed'], recovered
    assert agent.memory.path.read_bytes() == before
    assert all(not c.content for c in agent.memory_candidates.list())
    assert not agent.memory._vector_store().ids()
    record_id = agent.memory.local_store.make_record_id('memory', target.entry_id)
    assert agent.memory.local_store.get_record(record_id) is None
    assert not agent.memory.local_store._content_file(record_id).exists()


# LLM: 仅在正式库删除已经落盘后令候选底层读取持续失败，必须保留本次提交事实而不再次依赖坏账本。
# 函数用途: 验证删除清理故障回执不会因重新读取候选而逃逸。
def test_candidate_read_failure_after_remove_keeps_commit_receipt(tmp_path, monkeypatch):
    agent = _indexed_agent(tmp_path)
    tool = ConfirmedRememberWorkflow(agent)
    added = tool.execute(dict(content='项目使用 UTC 保存时间', kind='project', origin='user_explicit', subject_key='source.read-io'))
    assert added.ok and json.loads(added.output)['saved']
    [target] = agent.memory.all()
    _remove_request(agent, 'req-candidate-read-io')
    original = agent.memory_candidates._load_unlocked
    failures = []

    def read_candidates():
        if not agent.memory.all():
            failures.append('post-commit')
            raise OSError('candidate ledger read unavailable after commit')
        return original()

    with monkeypatch.context() as patch:
        patch.setattr(agent.memory_candidates, '_load_unlocked', read_candidates)
        outcome = tool.execute(dict(action='remove', entry_id=target.entry_id, origin='user_explicit'))
        payload = json.loads(outcome.output)
        assert payload['active_memory_changed'] and not payload['saved'], payload
        assert payload['results'][0]['reason_code'] == 'REMOVE_CLEANUP_PENDING', payload
        assert payload['results'][0]['status'] == 'approved', payload
        assert failures == ['post-commit']
        assert not agent.memory.all()
    before = agent.memory.path.read_bytes()
    recovered = json.loads(tool.execute(dict(action='remove', entry_id=target.entry_id, origin='user_explicit')).output)
    assert recovered['saved'] and not recovered['active_memory_changed'], recovered
    assert agent.memory.path.read_bytes() == before


# LLM: 故障仅在mark_promoted真正落盘后启用；成功回执不得重新依赖候选读取。
# 函数用途: 覆盖删除最终终态之后的读取故障，不能丢失已完成提交。
def test_final_marked_remove_does_not_reread_candidate(tmp_path, monkeypatch):
    agent = _indexed_agent(tmp_path)
    tool = ConfirmedRememberWorkflow(agent)
    added = tool.execute(dict(content='项目使用 UTC 保存时间', kind='project', origin='user_explicit', subject_key='source.final-io'))
    assert added.ok and json.loads(added.output)['saved']
    [target] = agent.memory.all()
    _remove_request(agent, 'req-final-candidate-read')
    mark = agent.memory_candidates.mark_promoted_admitted
    get = agent.memory_candidates.get
    marked = []

    def mark_then_fail(*args, **kwargs):
        result = mark(*args, **kwargs)
        marked.append(result)
        return result

    def post_mark_get(*args, **kwargs):
        if marked:
            raise OSError('final read must not be required after commit')
        return get(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(agent.memory_candidates, 'mark_promoted_admitted', mark_then_fail)
        patch.setattr(agent.memory_candidates, 'get', post_mark_get)
        payload = json.loads(tool.execute(dict(action='remove', entry_id=target.entry_id, origin='user_explicit')).output)
    assert marked and payload['saved'] and payload['active_memory_changed'], payload
    assert payload['results'][0]['status'] == 'promoted'
    assert agent.memory.all() == []
