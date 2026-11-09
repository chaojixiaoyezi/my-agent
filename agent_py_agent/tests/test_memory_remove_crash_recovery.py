"""真实 Remember 删除跨库提交在进程中断后必须恢复隐私清理。"""
import subprocess
import sys
from pathlib import Path

import pytest

from agent_py_agent.agent.conversation.learning_source import user_dialogue_projection
from agent_py_agent.tests.test_remember_learning_source_review import _agent

# 旧调用方不传源码根时，使用生成脚本的当前工作树，而不是子进程 cwd 或本机安装。
_CRASH_SCRIPT = '''
import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[3] if len(sys.argv) > 3 else __REPO_ROOT__)
from agent_py_agent.tests.memory_confirmation_fixtures import ConfirmedRememberWorkflow
from agent_py_agent.agent.conversation.learning_source import user_dialogue_projection
from agent_py_agent.tests.test_remember_learning_source_review import _agent
agent = _agent(Path(sys.argv[1]), user_dialogue_projection(), {}, {})
tool = ConfirmedRememberWorkflow(agent)
added = json.loads(tool.execute(dict(content="项目使用 UTC 保存时间", kind="project", origin="user_explicit", subject_key="source.review")).output)
assert added["active_memory_changed"], added
target = agent.memory.all()[0]
if sys.argv[2] == "after-formal":
    replaced = json.loads(tool.execute(dict(action="replace", entry_id=target.entry_id,
        content="项目新方案保留两份备份", origin="user_explicit")).output)
    assert replaced["active_memory_changed"], replaced
    params = agent._current_run_params
    params.request_id = "req-remove-after-replace"
    params.run_id = "run-remove-after-replace"
    agent.conversation_store.messages.append(dict(
        thread_id=params.task_attributes["conversation_thread_id"], role="user",
        content="删除项目保存时间和备份方案", metadata={"gateway_request_id": params.request_id}))
    from agent_py_agent.agent.memory_store import jsonl
    persist = jsonl.write_private_text_file_atomic_unlocked
    def crash_after_formal(path, text):
        persist(path, text)
        if path == agent.memory.path and '"action": "remove"' in text:
            os._exit(73)
    jsonl.write_private_text_file_atomic_unlocked = crash_after_formal
original = agent.memory_candidates.mark_promoted_admitted
def crash(candidate_id, admission, review):
    if sys.argv[2] == "after-mark":
        original(candidate_id, admission, review)
    os._exit(73)
agent.memory_candidates.mark_promoted_admitted = crash
if sys.argv[2] in {"default-target", "legacy-default-target"}:
    # 重建旧版同状态补全空操作产生的持久状态；新进程恢复不打桩。
    if sys.argv[2] == "legacy-default-target":
        agent.memory_candidates.fill_long_term_target = lambda candidate, reviewer: candidate
    from dataclasses import replace
    from agent_py_agent.agent.memory_store.curator import MemoryCuratorService
    from agent_py_agent.agent.memory_store.curator_models import MemoryCuratorConfig
    from agent_py_agent.tests.test_curator_observation_identity import _Backend, _candidate, _output
    message = agent.conversation_store.messages.append(dict(
        thread_id=agent._current_run_params.task_attributes["conversation_thread_id"],
        role="user", content="删除项目保存时间规则"))
    draft = dict(_candidate(message.message_id, target.content, "source.review"),
        proposed_action="remove", target_entry_id=target.entry_id, promotion_target="none")
    deps = replace(agent.memory_curator.dependencies, backend=_Backend(_output(message, [draft])),
        model_router=None, annotate_batch=None)
    # 本例显式只策展不自动晋升，避免默认批量提交绕过下方单条确认的崩溃边界。
    result = MemoryCuratorService(config=MemoryCuratorConfig(interval_seconds=60, turn_threshold=1,
        timeout_seconds=2, max_retries=0, auto_promotion_policy="manual_only"), dependencies=deps).run(reason="admin")
    assert result.status == "succeeded" and result.candidates == 1, result
    assert result.formal_commits == 0 and not result.promotion_results, result
    assert agent.memory.all() == [target], result
    from agent_py_agent.tests.memory_confirmation_fixtures import confirm_candidate
    [pending] = [c for c in agent.memory_candidates.list() if c.proposed_action == 'remove']
    assert pending.status == "pending_review" and pending.promotion_target == "none", pending
    result = confirm_candidate(agent.memory_promotion, pending.candidate_id)
else:
    result = tool.execute(dict(action="remove", entry_id=target.entry_id, origin="user_explicit"))
raise AssertionError(f"crash boundary not reached: {result}")
'''.replace('__REPO_ROOT__', repr(str(Path(__file__).resolve().parents[2])))


# LLM: 旧/新参数形式都须导入当前源码；真实进程退出覆盖各删除阶段，manual_only仅布置候选；legacy重建旧补全空操作，恢复不打桩。
# 函数用途: 验证历史正文清理及旧版默认落点均能在新进程恢复。
@pytest.mark.parametrize('point', ['after-formal', 'before-mark', 'after-mark', 'default-target', 'legacy-default-target'])
@pytest.mark.parametrize('explicit_repo_root', [False, True], ids=['legacy-argv', 'explicit-repo-root'])
def test_real_remove_crash_recovers_without_private_candidate(tmp_path, point, explicit_repo_root, monkeypatch):
    repo_root = Path(__file__).resolve().parents[2]
    # 从非仓库目录且不继承源码搜索路径启动，避免本机安装或 cwd 掩盖参数兼容缺口。
    monkeypatch.setenv('PYTHONSAFEPATH', '1')
    monkeypatch.delenv('PYTHONPATH', raising=False)
    command = [sys.executable, '-c', _CRASH_SCRIPT, str(tmp_path), point]
    if explicit_repo_root:
        command.append(str(repo_root))
    result = subprocess.run(command, capture_output=True, text=True, timeout=30, cwd=tmp_path)
    assert result.returncode == 73, result.stdout + result.stderr
    agent = _agent(tmp_path, user_dialogue_projection(), {}, {})
    assert agent.memory.all() == []
    [removed] = [c for c in agent.memory_candidates.list() if c.proposed_action == 'remove']
    assert removed.promotion_target == ('none' if point == 'legacy-default-target' else 'long_term')
    source = f'candidate:{removed.candidate_id}'
    assert agent.memory.has_committed_removal(removed.target_entry_id, removed.target_entry_version, source)
    assert not agent.memory.has_committed_removal(removed.target_entry_id, removed.target_entry_version, source + '-other')
    assert not agent.memory.has_committed_removal(removed.target_entry_id, removed.target_entry_version + 1, source)
    assert not agent.memory.has_committed_removal(removed.target_entry_id + '-other', removed.target_entry_version, source)
    before_formal = agent.memory.path.read_bytes()
    if point == 'after-mark':
        assert removed.content == '' and not removed.source_message_refs
    retry = agent.memory_promotion.promote(removed.candidate_id, automatic=True)
    assert retry.promoted, retry
    assert agent.memory.path.read_bytes() == before_formal
    assert '项目使用 UTC 保存时间' not in ''.join(path.read_text() for path in agent.memory_candidates.path.with_name(agent.memory_candidates.path.name + '.records').rglob('*') if path.is_file() and path.suffix in {'.jsonl', '.json', '.row'})
    assert '项目新方案保留两份备份' not in ''.join(path.read_text() for path in agent.memory_candidates.path.with_name(agent.memory_candidates.path.name + '.records').rglob('*') if path.is_file() and path.suffix in {'.jsonl', '.json', '.row'})
    for row in agent.memory_candidates.list():
        assert not row.content and row.content_redacted_at
        assert not any([row.evidence_refs, row.source_message_refs, row.source_tool_refs,
                        row.source_artifact_refs, row.learning_source, row.learning_source_coverage])
    cleaned = agent.memory_candidates.get(removed.candidate_id)
    assert cleaned.content == '' and cleaned.content_redacted_at
    assert not any([cleaned.source_message_refs, cleaned.source_tool_refs, cleaned.source_artifact_refs,
                    cleaned.evidence_refs, cleaned.learning_source, cleaned.learning_source_coverage])
    assert agent.memory.all() == []
