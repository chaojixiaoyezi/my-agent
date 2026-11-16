"""学习欠账坏字段逐条隔离，不能回滚健康回填或永久丢掉损坏责任。"""
import json

import pytest

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.session_task_pending_index import (
    LearningDebtShapeError,
    learning_repair_owed,
)
from agent_py_agent.agent.conversation.session_tasks import SessionTask


@pytest.mark.parametrize('value', [1, 'pending', ['pending'], False, None])
def test_invalid_enqueue_is_reported_and_healthy_debt_continues(tmp_path, value):
    tasks = ConversationStore(tmp_path / 'conversations').session_tasks
    for task_id, enqueue in [('bad', value), ('healthy', {'status': 'pending'})]:
        task = SessionTask(task_id, 'a', 'b', 'method', status='done', created_at=1,
                           metadata={'task_learning_enqueue': enqueue})
        tasks._path(task_id).write_text(json.dumps(task.to_dict()))
    original = tasks._path('bad').read_bytes()
    page = tasks.pending_learning_page()
    assert page['coverage'] == 'complete'
    assert [task.task_id for task in page['items']] == ['healthy']
    [error] = page['load_errors']
    assert error['code'] == 'SESSION_TASK_LEARNING_METADATA_INVALID'
    assert error['field'] == 'metadata.task_learning_enqueue'
    assert error['actual_type'] == type(value).__name__
    assert error['effect_outcome'] == 'not_started'
    assert error['next_action']
    assert tasks._path('bad').read_bytes() == original
    with tasks._learning_index()._connection() as conn:
        assert conn.execute("SELECT 1 FROM entries WHERE path='bad.json'").fetchone()
        assert conn.execute("SELECT 1 FROM task_references WHERE path='bad.json'").fetchone()
    # 跨实例仍可观察错误；原位修复不发布索引也能重新发现债务。
    reopened = ConversationStore(tmp_path / 'conversations').session_tasks
    assert reopened.pending_learning_page()['load_errors']
    payload = json.loads(original)
    payload['metadata']['task_learning_enqueue'] = {'status': 'pending'}
    tasks._path('bad').write_text(json.dumps(payload))
    page = reopened.pending_learning_page()
    assert {task.task_id for task in page['items']} == {'bad', 'healthy'}
    assert page['load_errors'] == []


def test_bad_learning_field_does_not_roll_back_bounded_backfill(tmp_path):
    tasks = ConversationStore(tmp_path / 'conversations').session_tasks
    for number in range(260):
        task = SessionTask(f'task-{number:03}', 'a', 'b', 'method', status='done', created_at=number + 1,
                           metadata={'task_learning_enqueue': 13 if number % 19 == 0 else {'status': 'pending'}})
        tasks._path(task.task_id).write_text(json.dumps(task.to_dict()))
    seen, errors = set(), set()
    cursor = None
    for _ in range(50):
        page = tasks.pending_learning_page(cursor=cursor)
        seen.update(task.task_id for task in page['items'])
        errors.update(error['path'] for error in page['load_errors'])
        cursor = page['next_cursor']
        if page['coverage'] == 'complete' and cursor is None:
            break
    assert len(seen) == 246
    assert len(errors) == 14
    with tasks._learning_index()._connection() as conn:
        assert conn.execute('SELECT count(*) FROM task_references').fetchone()[0] == 260
        assert conn.execute('SELECT count(*) FROM entries').fetchone()[0] == 260


def test_direct_debt_check_never_converts_bad_shape_to_no_debt():
    with pytest.raises(LearningDebtShapeError, match='metadata.task_learning_enqueue'):
        learning_repair_owed({'status': 'done', 'metadata': {'task_learning_enqueue': 13}})


def test_previous_false_scalar_omission_is_rebuilt_without_source_change(tmp_path):
    tasks = ConversationStore(tmp_path / 'conversations').session_tasks
    task = SessionTask('old-false', 'a', 'b', 'method', status='done',
                       metadata={'task_learning_enqueue': False})
    path = tasks._path(task.task_id)
    path.write_text(json.dumps(task.to_dict()))
    tasks.pending_learning_page()
    pending = tasks._learning_index()
    with pending._connection() as conn:
        pending._put_meta(conn, 'references_schema', 1)
        conn.execute("DELETE FROM entries WHERE path='old-false.json'")
    before = path.read_bytes()
    reopened = ConversationStore(tmp_path / 'conversations').session_tasks
    page = reopened.pending_learning_page()
    assert [error['code'] for error in page['load_errors']] == ['SESSION_TASK_LEARNING_METADATA_INVALID']
    assert path.read_bytes() == before
    with reopened._learning_index()._connection() as conn:
        assert conn.execute("SELECT 1 FROM entries WHERE path='old-false.json'").fetchone()
