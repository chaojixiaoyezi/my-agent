"""父收口只继承准确runtime归属与成功完成的原子登记，不根据输出路径收养文件。"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._finalization_service import _structured_delivery_artifacts
from agent_py_agent.agent.artifacts.registry import ArtifactRegistration, register_artifact
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.manager_runner_result_payload import RecordRunnerResultParams
from agent_py_agent.agent.user_space.owner_resolver import ensure_owner_home


def world(tmp_path):
    owner = ensure_owner_home(tmp_path / 'home')
    manager = SubAgentManager(workspace=owner.home_dir / 'subagents', workspace_root=owner.home_dir,
        owner_id=owner.owner_id, owner_home_dir=str(owner.home_dir))
    parent = manager.create_run(goal='交付', thought='', plan=[], role='coordinator')
    repo = manager.runtime_db
    root = repo.parent_execution_ref(owner_id=owner.owner_id, run_id=parent.id)
    output = owner.home_dir / 'output'
    output.mkdir()
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_id=owner.owner_id, owner_home_dir=owner.home_dir), subagents=manager)
    ctx = SimpleNamespace(run_id=parent.id, task_id=parent.id, attempt_id=root.observed_attempt_id,
        task_attributes={'run_workspace': {'output_dir': str(output)}}, delivery_contract=None, archive_tool_calls=[])
    return SimpleNamespace(owner=owner, manager=manager, parent=parent, repo=repo, agent=agent, ctx=ctx, output=output)


def parent_versions(w):
    root = Path(w.parent.agent_run_workspace_dir)
    # 本例只核宿主当前attempt的自身登记；不借尚未启动的子代manager签发执行身份。
    w.agent = SimpleNamespace(home_paths=w.agent.home_paths)
    w.ctx.attempt_id = 'current-host-attempt'
    w.ctx.task_attributes['run_workspace']['task_root'] = str(root)
    path = w.output / 'parent.txt'
    path.write_text('first')
    old = register_artifact(ArtifactRegistration(root, path, run_id=w.ctx.run_id, task_id=w.ctx.task_id,
        agent_id=w.owner.owner_id, created_by_tool='write_file')).to_dict()
    path.write_text('last!')
    new = register_artifact(ArtifactRegistration(root, path, run_id=w.ctx.run_id, task_id=w.ctx.task_id,
        agent_id=w.owner.owner_id, created_by_tool='write_file')).to_dict()
    return root, path, old, new


@pytest.mark.parametrize('legacy_archive', [False, True])
def test_parent_closeout_uses_latest_same_run_registered_version(tmp_path, legacy_archive):
    w = world(tmp_path)
    _root, path, old, new = parent_versions(w)
    # 回拨墙钟不能压掉与当代canonical逐字段相符的新登记。
    new['updated_at'] = old['updated_at'] - 100
    scope = {} if legacy_archive else {'attempt_id': w.ctx.attempt_id}
    w.ctx.archive_tool_calls = [{**scope, 'artifact_registry_refs': [old]},
                               {**scope, 'artifact_registry_refs': [new]}]
    result = _structured_delivery_artifacts(w.ctx, agent=w.agent)
    assert len(result) == 1 and result[0]['path'] == str(path)
    assert result[0]['sha256'] == new['sha256'] != old['sha256']
    assert result[0]['size_bytes'] == 5
    # 晚到的旧归档不把已登记新版退回旧digest。
    w.ctx.archive_tool_calls.append({**scope, 'artifact_registry_refs': [old]})
    assert _structured_delivery_artifacts(w.ctx, agent=w.agent) == result


@pytest.mark.parametrize('variant', ['foreign_attempt', 'foreign_run', 'foreign_actor', 'missing_current_ref'])
def test_parent_closeout_never_falls_back_to_old_or_foreign_version(tmp_path, variant):
    w = world(tmp_path)
    _root, _path, old, new = parent_versions(w)
    latest = dict(new)
    row = {'attempt_id': w.ctx.attempt_id, 'artifact_registry_refs': [latest]}
    if variant == 'foreign_attempt':
        row['attempt_id'] = 'different-attempt'
    elif variant == 'foreign_run':
        latest['run_id'] = 'different-run'
    elif variant == 'foreign_actor':
        latest['agent_id'] = 'different-owner'
    w.ctx.archive_tool_calls = [{'attempt_id': w.ctx.attempt_id, 'artifact_registry_refs': [old]}]
    if variant != 'missing_current_ref':
        w.ctx.archive_tool_calls.append(row)
    assert _structured_delivery_artifacts(w.ctx, agent=w.agent) == []
    diagnostics = w.ctx.task_attributes['child_delivery_diagnostics']
    assert diagnostics[-1]['error_code'] == 'DELIVERY_ARTIFACT_VERSION_MISMATCH'
    assert diagnostics[-1]['coverage'] == 'partial'


def child_product(w, *, status='DONE', parent=None):
    parent = parent or w.parent
    child = w.manager.create_run(goal='制作', thought='', plan=[], parent_id=parent.id, root_id=parent.id)
    identity = w.repo.parent_execution_ref(owner_id=w.owner.owner_id, run_id=child.id)
    runtime = {'agent_run_id': identity.agent_run_id, 'attempt_id': identity.observed_attempt_id}
    path = w.output / (child.id + '.csv')
    path.write_text('label,value\nalpha,7\nbeta,9\n')
    record = register_artifact(ArtifactRegistration(Path(child.agent_run_workspace_dir), path,
        run_id=child.id, task_id=parent.id, agent_id=w.owner.owner_id, source='tool_result', created_by_tool='write_file'))
    w.manager.runner_result.record_runner_result(RecordRunnerResultParams(run_id=child.id,
        dry_run=False, status=status, ok=status == 'DONE', message='收口', response='制作完成', turn_end_reason='completed', actual_tools=['write_file']))
    if status == 'DONE':
        w.repo.settle_agent_run(agent_run_id=runtime['agent_run_id'], status='done', attempt_id=runtime['attempt_id'])
    return child, runtime, record, path


def test_completed_child_real_registry_reaches_parent_delivery_and_deduplicates(tmp_path):
    w = world(tmp_path)
    _child, _runtime, record, path = child_product(w)
    w.ctx.archive_tool_calls = [{'artifact_registry_refs': [record.to_dict()]}]
    result = _structured_delivery_artifacts(w.ctx, agent=w.agent)
    assert len(result) == 1
    assert (result[0]['artifact_id'], result[0]['path'], result[0]['sha256'], result[0]['size_bytes']) == (
        record.artifact_id, str(path), record.sha256, 27)
    w.ctx.archive_tool_calls = []
    assert _structured_delivery_artifacts(w.ctx, agent=w.agent) == result


@pytest.mark.parametrize('change', ['failed', 'new_attempt', 'version', 'deleted', 'owner', 'root', 'undeclared'])
def test_parent_never_borrows_unproved_child_or_newer_version(tmp_path, change):
    w = world(tmp_path)
    child, runtime, record, path = child_product(w, status='FAILED' if change == 'failed' else 'DONE')
    if change == 'new_attempt':
        w.repo.create_attempt(runtime['agent_run_id'])
    elif change in ('version', 'deleted'):
        if change == 'version':
            path.write_text('new unreviewed version')
        else:
            path.unlink()
        register_artifact(ArtifactRegistration(Path(child.agent_run_workspace_dir), path,
            run_id=child.id, task_id=child.root_id, agent_id=w.owner.owner_id,
            source='tool_result', created_by_tool='write_file'))
    elif change in ('owner', 'root'):
        def mutate(value):
            if change == 'owner':
                value.owner = 'local/other'
            else:
                value.root_id = 'different-task'
        w.manager.mutate(child.id, mutate)
    elif change == 'undeclared':
        w.ctx.task_attributes = {}
    assert _structured_delivery_artifacts(w.ctx, agent=w.agent) == []


def test_other_parent_completed_child_and_unregistered_paths_do_not_grant_delivery(tmp_path):
    w = world(tmp_path)
    other = w.manager.create_run(goal='另一任务', thought='', plan=[], role='coordinator')
    w.repo.record_run_creation(owner_id=w.owner.owner_id, run_id=other.id, thread_id='thread')
    child_product(w, parent=other)
    (w.output / 'not-registered.csv').write_text('exists but not registered')
    assert _structured_delivery_artifacts(w.ctx, agent=w.agent) == []


def test_public_receipt_child_obligation_uses_same_host_context_and_bounded_collector(tmp_path):
    from agent_py_agent.agent.agent_core.tool_loop.background_public_receipt import (
        _has_pending_delivery_artifacts,
    )

    w = world(tmp_path)
    child_product(w)
    internal = w.owner.home_dir / 'internal'
    internal.mkdir()
    w.ctx.task_attributes['run_workspace']['output_dir'] = str(internal)
    assert not _has_pending_delivery_artifacts(w.ctx, agent=w.agent)
    w.ctx.delivery_contract = {'task_workspace': {'output_dir': str(w.output)}}
    assert _has_pending_delivery_artifacts(w.ctx, agent=w.agent)
    assert not _has_pending_delivery_artifacts(w.ctx)


def test_child_external_registry_is_rejected_before_reading(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core import child_delivery_artifacts

    w = world(tmp_path)
    child, _runtime, _record, _path = child_product(w)
    child_id = child.id
    outside = tmp_path / 'outside' / child_id
    forged = w.manager.load(child_id)
    forged.agent_run_workspace_dir = str(outside)
    monkeypatch.setattr(w.manager, 'load', lambda _run_id: forged)

    def forbidden_read(_root, *, max_bytes=None):
        pytest.fail('外部登记簿不得进入读取器')

    monkeypatch.setattr(child_delivery_artifacts, 'latest_artifact_records_report', forbidden_read)
    assert _structured_delivery_artifacts(w.ctx, agent=w.agent) == []
    assert w.ctx.task_attributes['child_delivery_diagnostics'][0]['error_code'] == 'CHILD_DELIVERY_WORKSPACE_SCOPE_MISMATCH'


def test_child_lookup_query_is_indexed_and_does_not_sort_historical_children(tmp_path, monkeypatch):
    from contextlib import contextmanager

    w = world(tmp_path)
    child_product(w)
    statements = []
    original = w.repo._runtime_connection

    @contextmanager
    def observed_connection():
        with original() as db:
            db.set_trace_callback(statements.append)
            yield db

    monkeypatch.setattr(w.repo, '_runtime_connection', observed_connection)
    assert len(_structured_delivery_artifacts(w.ctx, agent=w.agent)) == 1
    query = next(sql for sql in statements if sql.startswith('SELECT candidates.run_id'))
    with original() as db:
        details = [row['detail'] for row in db.execute('EXPLAIN QUERY PLAN ' + query)]
    assert any('idx_agent_runs_parent_task_status' in detail for detail in details)
    assert not any('TEMP B-TREE' in detail for detail in details)
    assert 'LIMIT 65' in query


def test_huge_child_registry_reads_only_byte_budget_and_reports_partial(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core import child_delivery_artifacts
    from agent_py_agent.agent.artifacts import registry

    w = world(tmp_path)
    child, _runtime, _record, _path = child_product(w)
    root = Path(child.agent_run_workspace_dir)
    with (root / registry.REGISTRY_RELATIVE_PATH).open('ab') as stream:
        stream.write(b' ' * 8_389_167)
    reads, byte_counts = [], []
    from agent_py_agent.agent.common import nofollow_fs

    original_os_read = nofollow_fs.os.read
    original = registry.read_bytes_beneath
    active = [False]

    def measured_read(root, parts, **kwargs):
        reads.append(kwargs['max_bytes'])
        active[0] = True
        try:
            return original(root, parts, **kwargs)
        finally:
            active[0] = False

    def measured_os_read(fd, count):
        raw = original_os_read(fd, count)
        if active[0]:
            byte_counts.append(len(raw))
        return raw

    monkeypatch.setattr(registry, 'read_bytes_beneath', measured_read)
    monkeypatch.setattr(nofollow_fs.os, 'read', measured_os_read)
    assert _structured_delivery_artifacts(w.ctx, agent=w.agent) == []
    budget = child_delivery_artifacts._CHILD_REGISTRY_MAX_BYTES
    assert reads == [budget]
    assert sum(byte_counts) == budget + 1
    diagnostic = w.ctx.task_attributes['child_delivery_diagnostics'][0]
    assert diagnostic['error_code'] == 'CHILD_DELIVERY_REGISTRY_BUDGET_EXCEEDED'
    assert diagnostic['coverage'] == 'partial'
    from agent_py_agent.agent.agent_core.tool_loop.background_public_receipt import (
        _has_pending_delivery_artifacts,
    )

    assert _has_pending_delivery_artifacts(w.ctx, agent=w.agent)


def test_all_children_share_one_total_read_budget(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core import child_delivery_artifacts

    w = world(tmp_path)
    for _ in range(5):
        child_product(w)
    monkeypatch.setattr(child_delivery_artifacts, '_CHILD_REGISTRY_MAX_BYTES', 1024)
    monkeypatch.setattr(child_delivery_artifacts, '_CHILD_REGISTRY_TOTAL_MAX_BYTES', 2048)
    reads = []
    original = child_delivery_artifacts.latest_artifact_records_report

    def observed(root, *, max_bytes=None):
        report = original(root, max_bytes=max_bytes)
        reads.append(report.bytes_read)
        return report

    monkeypatch.setattr(child_delivery_artifacts, 'latest_artifact_records_report', observed)
    _structured_delivery_artifacts(w.ctx, agent=w.agent)
    assert sum(reads) <= 2048 and len(reads) < 5
    codes = [row['error_code'] for row in w.ctx.task_attributes['child_delivery_diagnostics']]
    assert 'CHILD_DELIVERY_TOTAL_READ_BUDGET_EXCEEDED' in codes


def test_unknown_child_artifact_obligation_cannot_be_silently_cancelled(tmp_path, monkeypatch):
    from dataclasses import replace

    from agent_py_agent.agent.agent_core import child_delivery_artifacts
    from agent_py_agent.agent.agent_core.tool_loop.background_public_receipt import (
        prepare_background_public_receipt,
    )
    from agent_py_agent.tests.test_background_public_receipt_contract import (
        _body,
        _params,
        _response,
    )

    w = world(tmp_path)
    child_product(w)
    monkeypatch.setattr(child_delivery_artifacts, '_CHILD_REGISTRY_MAX_BYTES', 128)
    params = _params()
    params = replace(params, run_id=w.ctx.run_id, task_id=w.ctx.task_id, attempt_id=w.ctx.attempt_id,
        task_attributes=w.ctx.task_attributes,
        delivery_contract={**params.delivery_contract, 'task_workspace': {'output_dir': str(w.output)}})
    response, _ = _response(params, _body('internal_only', ''))
    action, result = prepare_background_public_receipt(params, response, agent=w.agent)
    assert action == 'finish' and result.runtime_status == 'failed'
    assert result.runtime_reason == 'BACKGROUND_RECEIPT_PUBLIC_OBLIGATION'
    assert params.task_attributes['child_delivery_diagnostics'][0]['coverage'] == 'partial'
