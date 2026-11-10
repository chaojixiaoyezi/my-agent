"""规范候选锚点索引的全序、增量更新、迁移续点与失败原头保护。"""
from dataclasses import replace

import pytest

from agent_py_agent.agent.memory_store import candidate_ledger
from agent_py_agent.agent.memory_store import candidates as module
from agent_py_agent.agent.memory_store.candidate_anchor_index import STATUSES, eligible, group_key
from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope
from agent_py_agent.agent.memory_store.candidates import CandidateService


def seed(tmp_path, count=320):
    service = CandidateService(tmp_path / 'candidates.jsonl')
    service.observe_many([CandidateObservation(candidate_type='lesson' if i % 2 else 'hot_rule',
        content=f'公开合成经验{i}', subject_key=f'anchor.{i}', scope=MemoryScope('personal', 'personal') if i % 3 else MemoryScope('global', 'global'),
        origin='user_explicit', promotion_target='lesson' if i % 2 else 'hot', source_message_refs=({'message_id': f'm-{i}'},)) for i in range(count)])
    return service


def expected(service, scopes):
    selected, counts = [], {}
    for row in sorted(service.readonly_rows(statuses=STATUSES), key=lambda row: (row.last_observed_at, row.candidate_id), reverse=True):
        key = (row.scope['scope_type'], row.scope['scope_key'], row.candidate_type)
        if not eligible(row) or key[:2] not in scopes or counts.get(key, 0) == 3:
            continue
        selected.append(row.candidate_id)
        counts[key] = counts.get(key, 0) + 1
        if len(selected) == 12:
            break
    return selected


def mutate(service, operation):
    with module._quota_admission(service.quota_enforcer) as admission, service._locked_candidates():
        rows = service._load_unlocked()
        operation(rows)
        module._write_candidates_unlocked(service.path, rows, admission=admission)


def old_header(service):
    # 真实旧头：旧写者不具有anchor命名空间；正文和其它规范索引保持原内容。
    with module._quota_admission(service.quota_enforcer) as admission, service._locked_candidates():
        rows = service._load_unlocked()
        root = rows.root
        for key, _ in list(rows.index.items(root)):
            if key.startswith(('anchor/', 'anchorroot/')):
                root = rows.index.remove(root, key)
        writes, cleanup = rows.index.finish(root)
        for name, raw in writes.items():
            (service.path.parent / (service.path.name + '.records') / name).write_bytes(raw)
        service.path.write_text(candidate_ledger.manifest_text(root, len(rows)), encoding='utf-8')


def test_exact_global_order_ties_and_warm_body_read_cap(tmp_path, monkeypatch):
    service = seed(tmp_path)
    scopes = {('personal', 'personal'), ('global', 'global')}
    wanted = expected(service, scopes)
    reads = []
    original = candidate_ledger._read_candidate_row
    def read(*args, **kwargs):
        reads.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(candidate_ledger, '_read_candidate_row', read)
    assert [row.candidate_id for row in service.anchor_rows(scopes)] == wanted
    assert len(reads) == 12
    assert service.anchor_rows({('session', 'foreign')}) == ()


def test_status_scope_type_and_time_updates_remove_old_rank(tmp_path):
    service = seed(tmp_path, 80)
    scopes = {('personal', 'personal'), ('global', 'global')}
    top = service.anchor_rows(scopes)[0].candidate_id
    def change(rows):
        position = rows.index_for(top)
        rows[position] = replace(rows[position], status='rejected', status_history=[*rows[position].status_history, module._status_event('rejected', actor='test', note='', at=rows[position].updated_at)])
        rows[1] = replace(rows[1], proposed_action='replace', target_entry_id='existing-target', target_entry_version=1)
        rows[0] = replace(rows[0], last_observed_at='9999-01-01T00:00:00+00:00',
                          scope=MemoryScope('personal', 'personal').to_dict(), candidate_type='lesson', promotion_target='lesson')
    mutate(service, change)
    assert [row.candidate_id for row in service.anchor_rows(scopes)] == expected(service, scopes)
    assert top not in [row.candidate_id for row in service.anchor_rows(scopes)]
    # 原全表删除规划重建同一唯一头，不能留下已删除的ghost锚点。
    with module._quota_admission(service.quota_enforcer) as admission, service._locked_candidates():
        rows = list(service._load_unlocked())[1:]
        module._write_candidates_unlocked(service.path, rows, admission=admission)
    assert [row.candidate_id for row in service.anchor_rows(scopes)] == expected(service, scopes)


def test_old_head_build_is_chunked_resumable_and_concurrent_mutation_is_seen(tmp_path, monkeypatch):
    service = seed(tmp_path, 300)
    old_header(service)
    original = module._write_candidates_unlocked
    steps = []
    def interrupted(path, rows, **kwargs):
        if hasattr(rows, 'anchor_build_end'):
            steps.append(rows.anchor_build_end)
            if len(steps) == 2:
                raise RuntimeError('restart between committed index chunks')
        return original(path, rows, **kwargs)
    monkeypatch.setattr(module, '_write_candidates_unlocked', interrupted)
    scopes = {('personal', 'personal'), ('global', 'global')}
    with pytest.raises(RuntimeError):
        service.anchor_rows(scopes)
    header = candidate_ledger.read_manifest(service.path)
    rows = candidate_ledger.CandidateRows(service.path, header)
    assert rows.index.get(rows.root, 'anchor/progress') == 128
    assert rows.index.get(rows.root, 'anchor/version') == 0
    monkeypatch.setattr(module, '_write_candidates_unlocked', original)
    mutate(service, lambda rows: rows.__setitem__(0, replace(rows[0], last_observed_at='9999-01-01T00:00:00+00:00')))
    restarted = CandidateService(service.path)
    assert [row.candidate_id for row in restarted.anchor_rows(scopes)] == expected(service, scopes)
    assert steps == [128, 256]


def test_bad_anchor_node_is_not_empty_result(tmp_path):
    service = seed(tmp_path, 20)
    header = candidate_ledger.read_manifest(service.path)
    rows = candidate_ledger.CandidateRows(service.path, header)
    reference = rows.index.get(rows.index.get(rows.root, 'anchor/groups'), group_key({'scope_type': 'personal', 'scope_key': 'personal'}, 'lesson'))
    (service.path.parent / (service.path.name + '.records') / reference['ref']['ref']).write_bytes(b'broken')
    with pytest.raises((ValueError, OSError)):
        service.anchor_rows({('personal', 'personal')})


def test_index_build_quota_failure_keeps_original_head_and_all_rows(tmp_path, monkeypatch):
    from contextlib import contextmanager
    service = seed(tmp_path, 30)
    old_header(service)
    before = service.path.read_bytes()
    class Denied:
        def check(self, changes):
            assert any(str(change.path).endswith('.json') for change in changes)
            raise PermissionError('quota denied before immutable-node publication')
    @contextmanager
    def denied(*args):
        yield Denied()
    monkeypatch.setattr(module, '_quota_admission', denied)
    with pytest.raises(PermissionError):
        service.anchor_rows({('personal', 'personal')})
    assert service.path.read_bytes() == before
    assert len(service.readonly_rows()) == 30


def test_writer_can_advance_between_build_chunks_and_index_keeps_new_state(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from threading import Event, Thread, current_thread
    service = seed(tmp_path, 270)
    old_header(service)
    entered, resume = Event(), Event()
    original = module._quota_admission
    results, errors = [], []
    @contextmanager
    def gated(*args):
        with original(*args) as admission:
            yield admission
        if current_thread().name == 'anchor-builder' and not entered.is_set():
            entered.set()
            assert resume.wait(5)
    monkeypatch.setattr(module, '_quota_admission', gated)
    scopes = {('personal', 'personal'), ('global', 'global')}
    def build():
        try:
            results.extend(service.anchor_rows(scopes))
        except BaseException as error:
            errors.append(error)
    worker = Thread(target=build, name='anchor-builder')
    worker.start()
    try:
        assert entered.wait(5)
        mutate(service, lambda rows: rows.__setitem__(0, replace(rows[0], last_observed_at='9999-01-01T00:00:00+00:00')))
    finally:
        resume.set()
        worker.join(10)
    assert not worker.is_alive() and not errors
    assert [row.candidate_id for row in results] == expected(service, scopes)


def test_empty_source_does_not_create_new_persistence(tmp_path):
    service = CandidateService(tmp_path / 'missing' / 'candidates.jsonl')
    assert service.anchor_rows({('personal', 'personal')}) == ()
    assert not service.path.exists()


def test_original_jsonl_converts_without_losing_records_or_anchor_order(tmp_path):
    import json
    service = seed(tmp_path, 31)
    scopes = {('personal', 'personal'), ('global', 'global')}
    wanted = expected(service, scopes)
    records = [row.to_record() for row in service.readonly_rows()]
    service.path.write_text('\n'.join(json.dumps(record) for record in records), encoding='utf-8')
    assert [row.candidate_id for row in service.anchor_rows(scopes)] == wanted
    assert len(service.readonly_rows()) == len(records)
    assert candidate_ledger.read_manifest(service.path) is not None


def test_existing_row_forged_as_another_group_maximum_is_rejected(tmp_path, monkeypatch):
    service = seed(tmp_path, 30)
    original = candidate_ledger.PlannedIndex.get
    foreign = next(row for row in service.readonly_rows() if row.scope['scope_type'] == 'global')
    key = group_key({'scope_type': 'personal', 'scope_key': 'personal'}, 'lesson')
    def forged(index, root, requested, *args):
        value = original(index, root, requested, *args)
        if requested == key and value is not None:
            return {**value, 'maximum': {'anchor_rank': [foreign.last_observed_at, foreign.candidate_id], 'candidate_id': foreign.candidate_id}}
        return value
    monkeypatch.setattr(candidate_ledger.PlannedIndex, 'get', forged)
    with pytest.raises(ValueError):
        service.anchor_rows({('personal', 'personal')})


def test_old_writer_invalidates_seal_and_restart_uses_new_group_generation(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import candidate_anchor_index
    service = seed(tmp_path, 270)
    scopes = {('personal', 'personal'), ('global', 'global')}
    before = service.anchor_rows(scopes)[0]
    # 旧协议写者仍正常更新规范行/状态索引，但不理解anchor维护或seal。
    with monkeypatch.context() as legacy:
        legacy.setattr(candidate_anchor_index, 'plan_anchors', lambda rows, root, changes: root)
        mutate(service, lambda rows: rows.__setitem__(rows.index_for(before.candidate_id), replace(before,
            status='rejected', status_history=[*before.status_history,
                module._status_event('rejected', actor='test', note='', at=before.updated_at)])))
    rows = service._load_unlocked()
    assert not candidate_anchor_index.source_valid(rows)
    original = module._write_candidates_unlocked
    steps = []
    def interrupted(path, rows, **kwargs):
        if hasattr(rows, 'anchor_build_end'):
            steps.append(rows.anchor_build_end)
            if len(steps) == 2:
                raise RuntimeError('cold restart after first rebuilt chunk')
        return original(path, rows, **kwargs)
    monkeypatch.setattr(module, '_write_candidates_unlocked', interrupted)
    with pytest.raises(RuntimeError):
        service.anchor_rows(scopes)
    rows = service._load_unlocked()
    generation = rows.index.get(rows.root, 'anchor/generation')
    assert generation != 'initial' and rows.index.get(rows.root, 'anchor/progress') == 128
    assert candidate_anchor_index.source_valid(rows) and not candidate_anchor_index.ready(rows)
    monkeypatch.setattr(module, '_write_candidates_unlocked', original)
    restarted = CandidateService(service.path)
    assert [row.candidate_id for row in restarted.anchor_rows(scopes)] == expected(service, scopes)
    assert before.candidate_id not in [row.candidate_id for row in restarted.anchor_rows(scopes)]
    assert restarted._load_unlocked().index.get(restarted._load_unlocked().root, 'anchor/generation') == generation


def test_new_mutation_cannot_reseal_stale_old_writer_projection_as_complete(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import candidate_anchor_index
    service = seed(tmp_path, 160)
    scopes = {('personal', 'personal'), ('global', 'global')}
    seen = {row.candidate_id for row in service.anchor_rows(scopes)}
    target = next(row for row in service.readonly_rows() if row.candidate_id not in seen)
    with monkeypatch.context() as legacy:
        legacy.setattr(candidate_anchor_index, 'plan_anchors', lambda rows, root, changes: root)
        mutate(service, lambda rows: rows.__setitem__(rows.index_for(target.candidate_id),
            replace(target, last_observed_at='2099-01-01T00:00:00+00:00')))
    mutate(service, lambda rows: rows.__setitem__(0, replace(rows[0], review_note='new writer mutation')))
    rows = service._load_unlocked()
    assert candidate_anchor_index.source_valid(rows) and not candidate_anchor_index.ready(rows)
    assert [row.candidate_id for row in service.anchor_rows(scopes)] == expected(service, scopes)
    assert service.anchor_rows(scopes)[0].candidate_id == target.candidate_id


def test_repeated_legacy_writer_changes_yield_and_later_recover(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from agent_py_agent.agent.memory_store import candidate_anchor_index
    service = seed(tmp_path, 270)
    old_header(service)
    original = module._quota_admission
    interrupted = []
    @contextmanager
    def admission(*args):
        with original(*args) as value:
            yield value
        if not interrupted:
            interrupted.append(True)
            # 片锁已释放，旧写者仍可提交；不能因此在同一调用里无限重启全史。
            with monkeypatch.context() as legacy:
                legacy.setattr(candidate_anchor_index, 'plan_anchors', lambda rows, root, changes: root)
                mutate(service, lambda rows: rows.__setitem__(0,
                    replace(rows[0], last_observed_at='2099-01-01T00:00:00+00:00')))
    monkeypatch.setattr(module, '_quota_admission', admission)
    with pytest.raises(RuntimeError, match='source changed again'):
        service.anchor_rows({('personal', 'personal'), ('global', 'global')})
    assert interrupted
    monkeypatch.setattr(module, '_quota_admission', original)
    scopes = {('personal', 'personal'), ('global', 'global')}
    assert [row.candidate_id for row in service.anchor_rows(scopes)] == expected(service, scopes)


def test_repeated_rollback_rebuild_keeps_one_reachable_group_catalog(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import candidate_anchor_index
    service = seed(tmp_path, 80)
    scopes = {('personal', 'personal'), ('global', 'global')}
    for year in (2091, 2092, 2093):
        with monkeypatch.context() as legacy:
            legacy.setattr(candidate_anchor_index, 'plan_anchors', lambda rows, root, changes: root)
            mutate(service, lambda rows: rows.__setitem__(0,
                replace(rows[0], last_observed_at=f'{year}-01-01T00:00:00+00:00')))
        assert [row.candidate_id for row in service.anchor_rows(scopes)] == expected(service, scopes)
        rows = service._load_unlocked()
        keys = [key for key, _ in rows.index.items(rows.root)]
        assert keys.count('anchor/groups') == 1
        assert not any(key.startswith('anchorroot/') for key in keys)
        catalog = rows.index.get(rows.root, 'anchor/groups')
        assert len(list(rows.index.items(catalog))) == 4
