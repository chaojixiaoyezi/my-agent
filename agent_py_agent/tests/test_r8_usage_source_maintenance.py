"""真实维护入口和跨进程目录续点，查询不得替后台补建索引。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.models import ThreadModelUsageEvent
from agent_py_agent.agent.conversation.usage_source_maintenance import maintain_usage_sources_lane
from agent_py_agent.agent.user_space.directory_paging import directory_page
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.agent.user_space.owner_maintenance import run_owner_storage_maintenance_slice
from agent_py_agent.agent.user_space.storage_lanes import (
    StorageLaneBudget,
    StorageLaneRequest,
    validate_lane_receipt,
)


@pytest.mark.skipif(os.name == 'nt', reason='原生目录FD续点只适用于POSIX维护通道')
def test_directory_checkpoint_survives_process_restart_with_small_receipt(tmp_path):
    for number in range(260):
        (tmp_path / (f'{number:04}-' + 'x' * 180)).mkdir()
    first, cursor, done = directory_page(tmp_path, (), {}, 1)
    assert not done and len(first) == 1
    assert len(json.dumps(cursor)) < 8192
    script = '''import json,sys
from pathlib import Path
from agent_py_agent.agent.user_space.directory_paging import directory_page
print(json.dumps(directory_page(Path(sys.argv[1]), (), json.loads(sys.argv[2]), 1)))
'''
    raw = subprocess.check_output([sys.executable, '-c', script, str(tmp_path), json.dumps(cursor)],
                                  cwd=Path(__file__).resolve().parents[2], text=True, timeout=30)
    second, cursor, done = json.loads(raw)
    assert second and first != second
    found = {*first, *second}
    while not done:
        names, cursor, done = directory_page(tmp_path, (), json.loads(json.dumps(cursor)), 7)
        assert len(names) <= 7 and len(json.dumps(cursor)) < 8192
        assert not found.intersection(names)
        found.update(names)
    assert len(found) == 260


@pytest.mark.skipif(os.name == 'nt', reason='原生owner维护目录FD合同为POSIX')
def test_real_owner_lane_backfills_cold_usage_without_new_appends(tmp_path):
    home = ensure_my_agent_home(tmp_path / 'host')
    root = home.owner_workspace_dir / 'runtime/workspaces/old-project/conversations'
    store = ConversationStore(root)
    thread = store.threads.get_or_create({'canonical_user_id': home.owner_id, 'channel': 'internal'})
    path = store.storage.model_usage_dir / f'{thread.thread_id}.jsonl'
    rows = [ThreadModelUsageEvent(f'event-{i}', thread.thread_id, f'request-{i}', '', '', 'test',
                                  {'schema': 'model_call_summary.v1', 'total_tokens': 1}, 1000).to_dict()
            for i in range(350)]
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    before = path.read_bytes()
    retention = SimpleNamespace(applied=True, legal_hold=False, load_errors=[])
    previous = {'storage_maintenance': {'continuation_pending': True, 'pending_kinds': ['usage_sources']}}
    receipts = []
    for _ in range(100):
        state = run_owner_storage_maintenance_slice(home, previous, retention, now=time.time(),
                                                    quota_max_ms=10, quota_max_entries=128)
        assert not state['errors']
        receipt = state['lane_reports'].get('usage_sources')
        if receipt:
            receipts.append(receipt)
            assert receipt['items'] <= 4
        if 'usage_sources' not in state['pending_kinds']:
            break
        previous = {'storage_maintenance': state}
    else:
        pytest.fail('有界owner维护应最终追平旧用量，不等待新的模型消费')
    assert receipts and not any(receipt.get('errors') for receipt in receipts)
    fresh = ConversationStore(root, initialize=False)
    assert [identifier for identifier, _ in fresh.model_usage.window_source_plan(since=999, until=1001).rows()] == [thread.thread_id]
    events, errors = fresh.model_usage.events_window_report(thread.thread_id, since=999, until=1001)
    assert not errors and len(list(events)) == 350
    assert path.read_bytes() == before


@pytest.mark.parametrize('held', [True, False])
def test_hold_or_exhausted_budget_does_not_create_projection(tmp_path, held):
    home = ensure_my_agent_home(tmp_path / 'host')
    root = home.owner_home_dir / 'conversations'
    root.mkdir(exist_ok=True)
    (root / 'model_usage').mkdir(exist_ok=True)
    before = sorted(str(path.relative_to(root)) for path in root.rglob('*'))
    request = StorageLaneRequest('usage_sources', home, 1, {}, StorageLaneBudget(4 if held else 0, 8192,
                                time.monotonic() + 1), held, False, None, None)
    result = validate_lane_receipt(maintain_usage_sources_lane(request))
    assert result['items'] == 0
    assert sorted(str(path.relative_to(root)) for path in root.rglob('*')) == before


@pytest.mark.skipif(os.name == 'nt', reason='原生owner维护目录FD合同为POSIX')
def test_missing_usage_directories_do_not_enter_write_lane(tmp_path):
    home = ensure_my_agent_home(tmp_path / 'host')
    root = home.owner_workspace_dir / 'runtime/workspaces/empty-project/conversations'
    root.mkdir(parents=True)
    before = sorted(str(path.relative_to(home.owner_home_dir)) for path in home.owner_home_dir.rglob('*'))
    commits, cursor = [], {}
    def commit(operation):
        commits.append(operation)
        return operation()
    for _ in range(8):
        request = StorageLaneRequest('usage_sources', home, time.time(), cursor,
                                    StorageLaneBudget(4, 8192, time.monotonic() + 1), False, True, None, commit)
        result = validate_lane_receipt(maintain_usage_sources_lane(request))
        assert not result.get('errors')
        cursor = result['cursor']
        if result['complete']:
            break
    else:
        pytest.fail('empty usage source discovery did not finish')
    assert commits == []
    assert sorted(str(path.relative_to(home.owner_home_dir)) for path in home.owner_home_dir.rglob('*')) == before
