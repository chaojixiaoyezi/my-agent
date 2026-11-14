"""线程回收只消费 canonical 直接子文件，保持坏源拒绝语义。"""
import json
from datetime import datetime, timezone

import pytest

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.retention import MemoryRetentionService
from agent_py_agent.agent.memory_store.retention_scan_budget import RetentionScanBudget
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home


@pytest.mark.parametrize('budgeted', [False, True])
@pytest.mark.parametrize('fault', ['none', 'corrupt', 'symlink'])
def test_canonical_thread_boundary(tmp_path, budgeted, fault, monkeypatch):
    home = ensure_my_agent_home(tmp_path / 'home')
    root = tmp_path / 'conversations'
    store = ConversationStore(root)
    thread = store.threads.get_or_create({
        'canonical_user_id': 'synthetic', 'channel': 'internal',
        'channel_conversation_id': 'synthetic', 'channel_user_id': 'synthetic',
        'now': 1794589734.0,
    })
    store.tasks.update_thread_index(thread.thread_id, 'synthetic-task', 'done', 1794589734.0)
    assert tuple((root / 'threads' / 'task_history').rglob('*.json'))
    # 故意不可解析的嵌套投影不属于canonical线程，不能左右清理安全门。
    nested = root / 'threads' / 'task_history' / 'unrelated.json'
    nested.write_text('{broken projection')
    # 读取计数守住目录边界：历史有多少成员都不得进入pin扫描热路径。
    from agent_py_agent.agent.memory_store import retention_scan
    original_entries = retention_scan._PinProjection._entries

    def entries(builder, path, **kwargs):
        assert not path.is_relative_to(root / 'threads' / 'task_history')
        yield from original_entries(builder, path, **kwargs)

    monkeypatch.setattr(retention_scan._PinProjection, '_entries', entries)
    canonical = root / 'threads' / f'{thread.thread_id}.json'
    if fault == 'corrupt':
        canonical.write_text(json.dumps({'thread_id': 'wrong-id'}))
    elif fault == 'symlink':
        target = tmp_path / 'target.json'
        canonical.rename(target)
        canonical.symlink_to(target)
    service = MemoryRetentionService(
        home_paths=home, candidates=CandidateService(home.owner_memory_candidates_jsonl),
        conversation_roots=(root,),
    )
    budget = RetentionScanBudget(100000, 100000000, float('inf')) if budgeted else None
    report = service.plan(now=datetime(2026, 11, 13, tzinfo=timezone.utc), scan_budget=budget)
    errors = [error for error in report.errors if error.error_code == 'MEMORY_RETENTION_PIN_SOURCE_UNREADABLE']
    assert bool(errors) == (fault != 'none'), report.errors
    assert canonical.exists()
