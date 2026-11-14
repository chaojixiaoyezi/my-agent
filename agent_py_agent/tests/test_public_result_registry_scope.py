"""公开引用核当前原登记归属与状态；旧ID保留，缺证明不声明complete。"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._finalization_service import (
    _delivery_artifact_from_registry_ref,
)
from agent_py_agent.agent.artifacts.registry import (
    ArtifactRegistration,
    register_artifact,
    registry_path,
)
from agent_py_agent.agent.conversation.business_observability import public_result_refs
from agent_py_agent.agent.conversation.execution_threads import ensure_execution_thread
from agent_py_agent.agent.conversation.runtime import delivery_result_artifacts
from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft, SessionTaskUpdate
from agent_py_agent.tests.test_unassigned_session_registered_artifacts import prepared


def delivered(tmp_path, *, root_alias=False, legacy=False):
    w = prepared(tmp_path)
    task = w.store.session_tasks.require(w.task.task_id)
    root, child, path = w.root, 'child-registered', w.path
    if root_alias:
        target = ensure_execution_thread(w.agent, task.sender_thread_id, 'alias-result', kind='delegated')
        task = w.store.session_tasks.create(SessionTaskDraft(task.sender_thread_id, target.thread_id, '根run交付'))
        source_task = 'canonical-root-run'
        w.store.session_tasks.bind_turn(task.task_id, turn_id=source_task)
        root = w.source.home / 'runs' / 'alias-result'
        root.mkdir(parents=True)
        for identifier in (task.task_id, source_task):
            w.store.tasks.bind({'thread_id': target.thread_id, 'task_id': identifier, 'task_path': str(root)})
        child = 'alias-child-run'
        child_root = root / 'work/agents' / child
        child_root.mkdir(parents=True)
        w.store.tasks.bind({'thread_id': target.thread_id, 'task_id': child, 'task_path': str(child_root)})
        path = root / 'output.csv'
        path.write_bytes(b'label,value\na,1\n')
        record = register_artifact(ArtifactRegistration(child_root, path, run_id=child, task_id=source_task))
    elif legacy:
        record = register_artifact(ArtifactRegistration(root, path, task_id=task.task_id))
    else:
        record = w.record
    value = _delivery_artifact_from_registry_ref(record.to_dict(), (path.parent,), frozenset())
    artifacts = delivery_result_artifacts(SimpleNamespace(delivery_artifacts=[value]))
    if legacy:
        artifacts = tuple({key: row[key] for key in ('path', 'artifact_id', 'sha256', 'size_bytes')} for row in artifacts)
    w.store.session_tasks.advance(task.task_id, SessionTaskUpdate(status='done', result_refs=(str(path),), result_artifacts=artifacts))
    task = w.store.session_tasks.require(task.task_id)
    return w, task, record, Path(record.path), (root if legacy else (root / 'work/agents' / child))


@pytest.mark.parametrize('root_alias', [False, True])
def test_child_source_survives_canonical_delivery_and_root_alias(tmp_path, root_alias):
    w, task, record, _path, _root = delivered(tmp_path, root_alias=root_alias)
    snapshot = task.metadata['result_artifacts'][0]
    assert (snapshot['task_id'], snapshot['run_id']) == (record.task_id, record.run_id)
    if root_alias:
        assert record.task_id != task.task_id and record.task_id == task.conversation_request_id
    refs, coverage = public_result_refs(task, store=w.store, owner_root=w.source.home)
    assert refs == [record.artifact_id] and coverage['state'] == 'complete'


@pytest.mark.parametrize('mutation', ['deleted', 'wrong_task', 'wrong_run', 'raw_registry_missing', 'raw_body_missing'])
def test_changed_registration_never_marks_old_task_complete(tmp_path, mutation):
    w, task, record, path, root = delivered(tmp_path)
    if mutation == 'raw_registry_missing':
        registry_path(root).unlink()
    elif mutation == 'raw_body_missing':
        path.unlink()
    else:
        register_artifact(ArtifactRegistration(root, path, artifact_id=record.artifact_id,
            task_id='different-task' if mutation == 'wrong_task' else record.task_id,
            run_id='sibling-run' if mutation == 'wrong_run' else record.run_id,
            status='deleted' if mutation == 'deleted' else 'ready'))
    refs, coverage = public_result_refs(task, store=w.store, owner_root=w.source.home)
    assert refs == [record.artifact_id] and coverage['state'] == 'partial'


def test_old_four_fields_stay_usable_when_original_task_registry_proves_scope(tmp_path):
    w, task, record, _path, _root = delivered(tmp_path, legacy=True)
    assert set(task.metadata['result_artifacts'][0]) == {'path', 'artifact_id', 'sha256', 'size_bytes'}
    refs, coverage = public_result_refs(task, store=w.store, owner_root=w.source.home)
    assert refs == [record.artifact_id] and coverage['state'] == 'complete'


def test_relative_legacy_ref_never_becomes_owner_or_download_authority():
    task = SimpleNamespace(result_refs=('out.csv',), metadata={'result_artifacts': [{
        'path': 'out.csv', 'artifact_id': 'art_legacy', 'sha256': 'a'*64, 'size_bytes': 1}]})
    refs, coverage = public_result_refs(task)
    assert refs == ['art_legacy'] and coverage['state'] == 'partial'


def test_8k_bad_rows_are_rejected_before_expanded_diagnostics(tmp_path, monkeypatch):
    import tracemalloc

    from agent_py_agent.agent.artifacts import registry

    root = tmp_path / 'bad-rows'
    path = registry.registry_path(root)
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{\n' * 4096)
    parsed = []
    original = registry._record_from_payload

    def counted(payload):
        parsed.append(1)
        return original(payload)

    monkeypatch.setattr(registry, '_record_from_payload', counted)
    tracemalloc.start()
    try:
        report = registry.latest_artifact_records_report(root, max_bytes=262144)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert report.bytes_read == 8192 and not parsed and not report.records
    assert len(report.errors) == 1 and report.errors[0]['error_code'] == 'ARTIFACT_REGISTRY_ROW_BUDGET_EXCEEDED'
    assert report.rows_read == 1025 and peak < 1024 * 1024


def test_legal_row_boundary_preserves_latest_status_hash_and_source(tmp_path):
    from agent_py_agent.agent.artifacts import registry

    root = tmp_path / 'legal-rows'
    root.mkdir()
    rows = []
    for number in range(8):
        path = root / str(number)
        path.write_text(str(number))
        rows.append(registry.register_artifact(registry.ArtifactRegistration(root, path,
            task_id='original-root', run_id='original-child', status='ready')))
    raw = registry.registry_path(root).read_bytes()
    for ending in (b'\n', b'\r\n'):
        registry.registry_path(root).write_bytes(raw.replace(b'\n', ending))
        exact = registry.latest_artifact_records_report(root, max_bytes=262144, max_rows=8, max_diagnostics=2)
        assert not exact.errors and exact.rows_read == 8
        assert {key: value.to_dict() for key, value in exact.records.items()} == {row.artifact_id: row.to_dict() for row in rows}
    limited = registry.latest_artifact_records_report(root, max_bytes=262144, max_rows=7, max_diagnostics=2)
    assert not limited.records and limited.errors[0]['error_code'] == 'ARTIFACT_REGISTRY_ROW_BUDGET_EXCEEDED'


def test_bad_diagnostics_stop_parsing_at_budget(tmp_path, monkeypatch):
    from agent_py_agent.agent.artifacts import registry

    root = tmp_path / 'diagnostics'
    path = registry.registry_path(root)
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{\n' * 8)
    calls = []
    original = registry.json.loads

    def observed(text):
        calls.append(text)
        return original(text)

    monkeypatch.setattr(registry.json, 'loads', observed)
    report = registry.latest_artifact_records_report(root, max_bytes=262144, max_rows=8, max_diagnostics=2)
    assert len(calls) == 2 and len(report.errors) == 2 and not report.records
    assert report.rows_read == 8
    assert report.errors[-1]['error_code'] == 'ARTIFACT_REGISTRY_DIAGNOSTIC_BUDGET_EXCEEDED'


def test_request_row_budget_is_shared_and_not_reset_by_cache_eviction(tmp_path):
    from agent_py_agent.agent.conversation import business_observability as observation

    first, second = delivered(tmp_path/'first', legacy=True), delivered(tmp_path/'second', legacy=True)
    with observation.observation_request():
        observation._observation_request_data.get().registry_budget.remaining_rows = 2
        assert public_result_refs(first[1], store=first[0].store, owner_root=first[0].source.home)[1]['state'] == 'complete'
        for number in range(1100):
            observation._observation_cached(('evict-row', number), lambda: None)
        refs, coverage = public_result_refs(second[1], store=second[0].store, owner_root=second[0].source.home)
        assert refs == [second[2].artifact_id] and coverage['state'] == 'partial'
        assert coverage['diagnostics'][0]['error_code'] == 'ARTIFACT_REGISTRY_ROW_BUDGET_EXCEEDED'


def test_request_diagnostics_budget_stays_shared_after_first_bad_registry(tmp_path):
    from agent_py_agent.agent.conversation import business_observability as observation

    first, second = delivered(tmp_path/'diag-first', legacy=True), delivered(tmp_path/'diag-second', legacy=True)
    with registry_path(first[4]).open('ab') as stream:
        stream.write(b'{\n')
    with observation.observation_request():
        budget = observation._observation_request_data.get().registry_budget
        budget.remaining_diagnostics = 1
        first_refs, first_coverage = public_result_refs(first[1], store=first[0].store, owner_root=first[0].source.home)
        assert first_refs == [first[2].artifact_id] and first_coverage['state'] == 'partial'
        assert budget.remaining_diagnostics == 0
        _, second_coverage = public_result_refs(second[1], store=second[0].store, owner_root=second[0].source.home)
        assert second_coverage['diagnostics'][0]['error_code'] == 'ARTIFACT_REGISTRY_DIAGNOSTIC_BUDGET_EXCEEDED'


@pytest.mark.parametrize('sample,reason', [
    ('four_mib', 'ARTIFACT_REGISTRY_READ_BUDGET_EXCEEDED'),
    ('bad_rows', 'ARTIFACT_REGISTRY_DIAGNOSTIC_BUDGET_EXCEEDED'),
    ('many_rows', 'ARTIFACT_REGISTRY_ROW_BUDGET_EXCEEDED'),
])
def test_registered_reader_reports_bounded_unknown_not_absence(tmp_path, monkeypatch, sample, reason):
    from agent_py_agent.agent.conversation import registered_artifact_observation as reader
    from agent_py_agent.agent.conversation.business_observability import ObservationSourceError
    w = prepared(tmp_path)
    path = registry_path(w.child_root)
    path.write_bytes(b'x' * (4 * 1024 * 1024) if sample == 'four_mib' else b'{\n' * (4096 if sample == 'bad_rows' else 8193))
    read = reader.os.read
    observed = []
    def counted(fd, size):
        raw = read(fd, size)
        if reader.os.fstat(fd).st_ino == path.stat().st_ino:
            observed.append(len(raw))
        return raw
    monkeypatch.setattr(reader.os, 'read', counted)
    with pytest.raises(ObservationSourceError) as failure:
        reader.registered_artifact(w.source, w.record.artifact_id, w.record.sha256, authority=w.authority)
    assert failure.value.details['reason'] == reason
    assert failure.value.details['coverage'] == 'partial'
    assert sum(observed) == (0 if sample == 'four_mib' else path.stat().st_size)


def test_registered_reader_preserves_large_legal_log_and_original_hash(tmp_path, monkeypatch):
    import hashlib

    from agent_py_agent.agent.conversation import registered_artifact_observation as reader
    w = prepared(tmp_path)
    path = registry_path(w.child_root)
    raw = path.read_bytes() * 1500
    assert 262144 < len(raw) < 2 * 1024 * 1024
    path.write_bytes(raw)
    original = reader.os.read
    observed = []
    def counted(fd, size):
        value = original(fd, size)
        if reader.os.fstat(fd).st_ino == path.stat().st_ino:
            observed.append(len(value))
        return value
    monkeypatch.setattr(reader.os, 'read', counted)
    facts, record, content = reader.registered_artifact(w.source, w.record.artifact_id, w.record.sha256, authority=w.authority)
    assert len(content) == 27 and hashlib.sha256(content).hexdigest() == record.sha256
    assert facts.task_id == w.task.task_id
    assert sum(observed) >= len(raw) and sum(observed) <= len(raw) + len(content)


def test_explicit_orphan_child_never_falls_back_to_parent_registry(tmp_path):
    w, task, record, path, _ = delivered(tmp_path, legacy=True)
    orphan = 'declared-child-without-authority'
    record = register_artifact(ArtifactRegistration(w.root, path, task_id=task.task_id, run_id=orphan))
    artifact = {key: record.to_dict()[key] for key in ('path', 'artifact_id', 'sha256', 'size_bytes', 'task_id', 'run_id')}
    # 原终态快照由宿主收口形状构造；不为测试伪造TaskLink或runtime执行行。
    from dataclasses import replace
    task = replace(task, metadata={**task.metadata, 'result_artifacts': [artifact]})
    refs, coverage = public_result_refs(task, store=w.store, owner_root=w.source.home)
    assert refs == [record.artifact_id]
    assert coverage['state'] == 'partial'
    assert coverage['diagnostics'][0]['error_code'] == 'RESULT_ARTIFACT_SOURCE_UNPROVEN'
