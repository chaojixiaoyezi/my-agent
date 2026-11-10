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


@pytest.mark.parametrize('order', ['a_first', 'b_first'])
def test_conflicting_ids_for_one_path_stay_unresolved_in_either_order(order):
    # G7-R1：同一路径两个不同登记ID不按元数据顺序挑选；完全相同的重复项仍去重。
    one, two, art_c = '/srv/a/one.md', '/srv/a/two.md', 'art_' + 'c' * 20
    conflict = [_item(one, ART_A), _item(one, ART_B)]
    items = (conflict if order == 'a_first' else conflict[::-1]) + [_item(two, art_c), _item(two, art_c)]
    task = SimpleNamespace(result_refs=(one, two), metadata={'result_artifacts': items})
    assert public_result_refs(task) == ([art_c], {'state': 'partial', 'unresolved_count': 1})
    only = SimpleNamespace(result_refs=(one,), metadata={'result_artifacts': items})
    assert public_result_refs(only) == ([], {'state': 'partial', 'unresolved_count': 1})


def test_terminal_result_identity_is_write_once_and_report_stays_consistent(native):  # noqa: F811
    # G7-R2：新的非终态→终态写定映射与结果路径；同状态重放换成另一映射/路径被忽略（不报错），相同映射幂等，
    #   已发布的结果回报与公开ID保持一致。
    from agent_py_agent.agent.conversation.session_tasks import SessionTaskUpdate

    _, host = native
    first_path, other_path = PATHS['short'], '/srv/b/other.md'
    task_id = _finish(native, 'g7-write-once', [first_path], [_item(first_path)])
    store = host.conversation_store.session_tasks
    replay = store.advance(task_id, SessionTaskUpdate(status='done', result_refs=(other_path,),
                                                      result_artifacts=(_item(other_path, ART_B),)))
    stored = store.require(task_id)
    assert replay.metadata['result_artifacts'] == stored.metadata['result_artifacts'] == [_item(first_path)]
    assert replay.result_refs == stored.result_refs == (first_path,)
    same = store.advance(task_id, SessionTaskUpdate(status='done', result_refs=(first_path,),
                                                    result_artifacts=(_item(first_path),)))
    assert same.metadata['result_artifacts'] == [_item(first_path)] and same.result_refs == (first_path,)
    assert ok(native, 'tasks.get', {'task_id': task_id})['result_refs'] == [ART_A]
    reports = [entry for entry in host.conversation_store.guidance.pending('thread', stored.sender_thread_id, limit=0)
               if entry.metadata.get('session_task_id') == task_id]
    assert len(reports) == 1 and first_path in reports[0].message and other_path not in reports[0].message


def test_legacy_terminal_record_accepts_exactly_one_mapping_backfill(native):  # noqa: F811
    from agent_py_agent.agent.conversation.session_tasks import SessionTaskUpdate

    _, host = native
    path = PATHS['short']
    legacy = _finish(native, 'g7-legacy-backfill', [path])
    store = host.conversation_store.session_tasks
    assert 'result_artifacts' not in store.require(legacy).metadata
    filled = store.advance(legacy, SessionTaskUpdate(status='done', result_artifacts=(_item(path),)))
    assert filled.metadata['result_artifacts'] == [_item(path)]
    store.advance(legacy, SessionTaskUpdate(status='done', result_artifacts=(_item(path, ART_B),)))
    assert store.require(legacy).metadata['result_artifacts'] == [_item(path)]
    assert ok(native, 'tasks.get', {'task_id': legacy})['result_refs'] == [ART_A]


def test_real_registry_id_round_trips_to_public_ref_and_ids_are_not_version_pins(native, tmp_path):  # noqa: F811
    # MSG-024 证据：真实本地登记簿→原交付投影→运行时提取→终态→公开ID→按ID回读；登记ID按(run,task,agent,path)稳定，
    #   同路径重登记不换ID但sha256变，故公开ID不是版本钉；冻结的交付sha256留在内部元数据可核漂移。
    from agent_py_agent.agent.agent_core._finalization_service import (
        _delivery_artifact_from_registry_ref,
    )
    from agent_py_agent.agent.artifacts.registry import (
        ArtifactRegistration,
        register_artifact,
        resolve_artifact_record,
    )

    _, host = native
    workspace = tmp_path / 'delivery-ws'
    target = workspace / 'out' / '季度报告.md'
    target.parent.mkdir(parents=True)
    target.write_text('v1', encoding='utf-8')
    registration = ArtifactRegistration(workspace_root=workspace, path=target, run_id='run-g7', task_id='g7-real',
                                         agent_id='worker')
    record = register_artifact(registration)
    item = _delivery_artifact_from_registry_ref(record.to_dict(), (workspace.resolve(),), frozenset())
    artifacts = delivery_result_artifacts(SimpleNamespace(delivery_artifacts=[item]))
    task_id = _finish(native, 'g7-real-registry', [item['path']], artifacts)
    detail = ok(native, 'tasks.get', {'task_id': task_id})
    assert detail['result_refs'] == [record.artifact_id] and record.artifact_id.startswith('art_')
    readback = resolve_artifact_record(workspace, record.artifact_id)
    persisted = host.conversation_store.session_tasks.require(task_id).metadata['result_artifacts']
    assert (readback.path, readback.sha256) == (item['path'], item['sha256']) == (persisted[0]['path'],
                                                                                 persisted[0]['sha256'])
    target.write_text('v2 changed', encoding='utf-8')
    second = register_artifact(registration)
    assert second.artifact_id == record.artifact_id and second.sha256 != record.sha256
    assert resolve_artifact_record(workspace, record.artifact_id).sha256 == second.sha256
    assert ok(native, 'tasks.get', {'task_id': task_id})['result_refs'] == [record.artifact_id]
    assert host.conversation_store.session_tasks.require(task_id).metadata['result_artifacts'][0]['sha256'] == record.sha256
