# 模块用途: G7（MSG-017）回归：公开 tasks.get/TaskView/AssignmentView 的 result_refs 只给交付项自带的 owner 内产物登记 ID，
#   不给绝对路径、不截断、不补造；内部 SessionTask 路径与结果回报正文不变。覆盖短路径、长 ASCII、多字节（字符数与 UTF-8
#   字节数分别核对）、旧任务无交付身份、元数据损坏、重复与非法 ID。修复前长/多字节路径使 tasks.get 返回 503
#   DOMAIN_RESULT_INVALID、短路径泄露 owner 目录（见 evidence/g7-repro-before.txt）。只用 tmp 隔离 home。
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.business_observability import public_result_refs
from agent_py_agent.agent.conversation.runtime import delivery_result_artifacts
from agent_py_agent.agent.conversation.session_task_report import TaskTurnOutcome, close_out_turn
from agent_py_agent.tests.test_business_v12_background_scheduling import (  # noqa: F401 - pytest fixture
    assigned_task,
    native,
    ok,
)

ART_A, ART_B = 'art_' + 'a' * 20, 'art_' + 'b' * 20
PATHS = {'short': '/srv/a/r.md', 'long_ascii': '/srv/' + 'x' * 140 + '.md', 'multibyte': '/srv/' + '报告' * 22 + '.md'}


def _item(path, artifact_id=ART_A):
    return {'path': path, 'artifact_id': artifact_id, 'sha256': 'c' * 64, 'size_bytes': 12}


def _finish(native, name, refs, artifacts=()):  # noqa: F811
    _, host = native
    _, task_id = assigned_task(native, name=name)
    turn = 'g7-turn-' + task_id
    host.conversation_store.session_tasks.bind_turn(task_id, turn_id=turn)
    closed = close_out_turn(host, turn, TaskTurnOutcome(ok=True, summary='done', result_refs=tuple(refs),
                                                        result_artifacts=tuple(artifacts)))
    assert closed is not None and closed.status == 'done'
    return task_id


@pytest.mark.parametrize('kind', list(PATHS))
def test_public_result_refs_are_registry_ids_and_internal_paths_stay(native, kind):  # noqa: F811
    _, host = native
    path = PATHS[kind]
    chars, size = len(path), len(path.encode('utf-8'))
    # 字符数与字节数分别成立：长 ASCII 超字符上限；多字节字符数合规但字节数超 128。
    assert {'short': chars <= 128 and size <= 128, 'long_ascii': chars > 128,
            'multibyte': chars <= 128 < size}[kind]
    task_id = _finish(native, 'g7-' + kind.replace('_', '-'), [path], [_item(path)])
    detail = ok(native, 'tasks.get', {'task_id': task_id})
    assert detail['result_refs'] == detail['task']['result_refs'] == detail['assignments'][0]['result_refs'] == [ART_A]
    assert detail['result_ref_coverage'] == detail['task']['result_ref_coverage'] == {
        'state': 'complete', 'unresolved_count': 0}
    assert path not in json.dumps(detail, ensure_ascii=False)
    listed = [row for row in ok(native, 'tasks.list', {})['items'] if row['task_id'] == task_id]
    assert [row['result_refs'] for row in listed] == [[ART_A]]
    stored = host.conversation_store.session_tasks.require(task_id)
    assert stored.result_refs == (path,) and stored.metadata['result_artifacts'] == [_item(path)]
    reports = [entry for entry in host.conversation_store.guidance.pending('thread', stored.sender_thread_id, limit=0)
               if entry.metadata.get('session_task_id') == task_id]
    assert len(reports) == 1 and path in reports[0].message


def test_legacy_duplicate_and_invalid_results_are_counted_never_exposed_as_paths(native):  # noqa: F811
    one, two, three = '/srv/a/one.md', '/srv/a/two.md', '/srv/a/three.md'
    legacy = _finish(native, 'g7-legacy', [one, two])
    detail = ok(native, 'tasks.get', {'task_id': legacy})
    assert detail['result_refs'] == [] and detail['task']['result_refs'] == []
    assert detail['result_ref_coverage'] == {'state': 'legacy_unresolved', 'unresolved_count': 2}
    mixed = _finish(native, 'g7-mixed', [one, two, one, three],
                    [_item(one), _item(two), _item(three, 'x' * 129), 'garbage', {'path': 5, 'artifact_id': ART_B}])
    detail = ok(native, 'tasks.get', {'task_id': mixed})
    assert detail['result_refs'] == [ART_A]
    assert detail['result_ref_coverage'] == {'state': 'partial', 'unresolved_count': 1}
    text = json.dumps(detail, ensure_ascii=False)
    assert all(path not in text for path in (one, two, three))
    empty = _finish(native, 'g7-empty', [])
    detail = ok(native, 'tasks.get', {'task_id': empty})
    assert detail['result_refs'] == [] and 'result_ref_coverage' not in detail


def test_unreadable_metadata_and_runtime_extraction_are_explicit():
    task = SimpleNamespace(result_refs=('/srv/a/one.md',), metadata={'result_artifacts': 'corrupt'})
    assert public_result_refs(task) == ([], {'state': 'unreadable', 'unresolved_count': 1})
    assert public_result_refs(SimpleNamespace(result_refs=(), metadata={})) == ([], None)
    execution = SimpleNamespace(delivery_artifacts=[
        {'path': '/srv/a/one.md', 'artifact_id': ART_A, 'sha256': 'c' * 64, 'size_bytes': 12, 'ok': True},
        {'path': '/srv/a/raw.md', 'artifact_id': '', 'ok': True}, {'artifact_id': ART_B}, 'garbage'])
    assert delivery_result_artifacts(execution) == (
        {'path': '/srv/a/one.md', 'artifact_id': ART_A, 'sha256': 'c' * 64, 'size_bytes': 12},)
    assert delivery_result_artifacts(SimpleNamespace()) == ()
