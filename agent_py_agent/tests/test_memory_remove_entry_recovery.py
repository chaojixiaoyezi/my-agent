"""删除崩溃后必须从真实工具入口恢复；旧墓碑和派生正文也在清理范围内。"""
import json
import subprocess
import sys

from agent_py_agent.agent.conversation.learning_source import user_dialogue_projection
from agent_py_agent.agent.local_storage import LocalStore
from agent_py_agent.tests.memory_confirmation_fixtures import ConfirmedRememberWorkflow
from agent_py_agent.tests.test_memory_remove_crash_recovery import _CRASH_SCRIPT
from agent_py_agent.tests.test_remember_learning_source_review import _agent


# LLM: 仅在正式文件原子提交后退出；legacy去掉新版计划字段重建旧格式，indexed启用真实LocalStore。
#   正式写入不再双写LocalStore投影，indexed按升级前遗留的真实SQLite/FTS副本播种被删事实的旧正文。
# 函数用途: 在独立进程中制造已经提交正式删除但尚未收尾的状态。
def _crashed_agent(tmp_path, *, legacy=False, indexed=False):
    program = _CRASH_SCRIPT
    if indexed:
        program = program.replace('tool = ConfirmedRememberWorkflow(agent)',
            'from agent_py_agent.agent.local_storage import LocalStore\n'
            'agent.memory.local_store = LocalStore(Path(sys.argv[1]) / "derived")\n'
            'tool = ConfirmedRememberWorkflow(agent)')
    if legacy:
        program = program.replace('        persist(path, text)',
            '        if path == agent.memory.path:\n'
            '            records = [json.loads(row) for row in text.splitlines()]\n'
            '            for row in records: row.pop("removed_content_hashes", None)\n'
            '            text = "\\n".join(json.dumps(row, ensure_ascii=False) for row in records) + "\\n"\n'
            '        persist(path, text)')
    crashed = subprocess.run([sys.executable, '-c', program, str(tmp_path), 'after-formal'],
                             capture_output=True, text=True, timeout=30)
    assert crashed.returncode == 73, crashed.stdout + crashed.stderr
    agent = _agent(tmp_path, user_dialogue_projection(), {}, {})
    if indexed:
        agent.memory.local_store = LocalStore(tmp_path / 'derived')
    [removed] = [c for c in agent.memory_candidates.list() if c.proposed_action == 'remove']
    if indexed:
        agent.memory.local_store.upsert_record(source_type='memory', source_id=removed.target_entry_id,
                                               title='project:user', content='项目新方案保留两份备份')
    return agent, removed


# LLM: 不直接调用内部promote；自动晋升策略关闭也不能妨碍已提交删除的隐私收尾。
# 函数用途: 用户重试同一删除时能够恢复，不被active目标查找挡在外面。
def test_remember_retry_recovers_committed_remove_from_real_entry(tmp_path):
    agent, removed = _crashed_agent(tmp_path)
    formal_before = agent.memory.path.read_bytes()
    outcome = ConfirmedRememberWorkflow(agent).execute(dict(action='remove', entry_id=removed.target_entry_id,
                                               origin='user_explicit'))
    assert outcome.ok, outcome
    payload = json.loads(outcome.output)
    assert payload['saved'] and not payload['active_memory_changed'], payload
    assert agent.memory.path.read_bytes() == formal_before
    assert all(not c.content for c in agent.memory_candidates.list())


# LLM: 缺计划的旧墓碑必须走精确候选关联，不能把空tuple视为完整清理成功。
# 函数用途: 升级后恢复旧删除不能保留替换前正文。
def test_legacy_tombstone_recovers_all_linked_candidate_versions(tmp_path):
    agent, removed = _crashed_agent(tmp_path, legacy=True)
    assert 'removed_content_hashes' not in agent.memory.path.read_text()
    outcome = agent.memory_promotion.promote(removed.candidate_id, automatic=True)
    assert outcome.promoted, outcome
    assert all(not c.content for c in agent.memory_candidates.list())
    assert '项目使用 UTC 保存时间' not in agent.memory_candidates.path.read_text()


# LLM: 真实SQLite/FTS和外置正文先有命中，恢复后不得残留旧索引；正式召回过滤不能代替擦除。
# 函数用途: 候选恢复必须连同实际派生存储一起完成。
def test_remove_recovery_clears_real_localstore_projection(tmp_path):
    agent, removed = _crashed_agent(tmp_path, indexed=True)
    assert agent.memory.local_store.search('备份', source_type='memory')
    outcome = agent.memory_promotion.promote(removed.candidate_id, automatic=True)
    assert outcome.promoted, outcome
    assert not agent.memory.local_store.search('备份', source_type='memory')
