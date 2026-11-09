"""只读候选pin扫描不保留可变工作副本；关闭的投影立刻释放临时引用。"""
from __future__ import annotations

import json
import os
import weakref
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.memory_store import candidates
from agent_py_agent.agent.memory_store import retention_scan as scan
from agent_py_agent.agent.memory_store.candidate_ledger import PlannedIndex, manifest_text
from agent_py_agent.agent.memory_store.retention_models import MemoryRetentionPolicy
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.tests.test_memory_candidates import candidate


def _ledger(tmp_path, count):
    home = home_paths(tmp_path / 'home')
    path = home.owner_memory_candidates_jsonl
    path.parent.mkdir(parents=True)
    home.owner_retention_json.write_text(json.dumps(MemoryRetentionPolicy().to_dict()))
    index, root = PlannedIndex(path), None
    for number in range(count):
        row = candidate(number)
        root = index.put(root, 'row/' + row.candidate_id, {'position': number, 'record': index.save_row(row)})
        root = index.put(root, 'pos/' + str(number), row.candidate_id)
    writes, _cleanup = index.finish(root)
    directory = path.parent / (path.name + '.records')
    directory.mkdir()
    for name, raw in writes.items():
        (directory / name).write_bytes(raw)
    path.write_text(manifest_text(root, count))
    return home, path


@pytest.mark.parametrize('consumer', ['slice', 'synchronous'])
def test_pin_readers_keep_only_current_candidate_alive(tmp_path, monkeypatch, consumer):
    home, path = _ledger(tmp_path, 256)
    alive, peak, parsed = [], [], []
    original = candidates._parse_candidate_line
    def parse(raw):
        row = original(raw)
        alive.append(weakref.ref(row))
        parsed.append(1)
        count = sum(reference() is not None for reference in alive)
        peak.append(count)
        assert count <= 3, 'readonly scan retained earlier candidate bodies'
        return row
    monkeypatch.setattr(candidates, '_parse_candidate_line', parse)
    if consumer == 'slice':
        def read(source):
            yield None
            return source.read_bytes()
        projection = SimpleNamespace(_read=read)
        rows = scan._PinProjection._candidate_rows(projection, path)
        selected = [row['candidate_id'] for row in rows if row is not None]
        assert len(set(selected)) == 256
    else:
        errors = []
        pins, complete = scan.current_retention_pins(home, (), errors, now=datetime.now(timezone.utc))
        assert complete and not errors
        assert {'message-0', 'message-255'} <= pins.ids
    assert len(parsed) == 256 and max(peak) <= 3
    assert not any(reference() is not None for reference in alive)


def _projection(tmp_path):
    class Context:
        pass
    context = Context()
    context.__dict__.update(home=home_paths(tmp_path / 'home'), conversation_roots=(),
        now=datetime.now(timezone.utc), policy=MemoryRetentionPolicy(), config=None,
        scan_budget=None, discovery_context=None)
    snapshot = scan._PinProjection(context)
    context.pin_projection = snapshot
    return snapshot, context


def test_closed_projection_releases_cycle_and_cache_without_touching_returned_pins(tmp_path):
    class Marker:
        pass
    snapshot, context = _projection(tmp_path)
    marker = Marker()
    cached, held_context = weakref.ref(marker), weakref.ref(context)
    snapshot.contents['files']['fixture'] = (None, marker, None, 1)
    snapshot.contents['bytes'] = 1
    pins = scan._RetentionPins(context.home.owner_home_dir, ids={'protected'})
    pins.paths.add(tmp_path / 'protected')
    snapshot.value = (pins, True)
    snapshot.pending = ((pins, True), {})
    source = tmp_path / 'fd'
    source.write_bytes(b'fixture')
    fd = os.open(source, os.O_RDONLY)
    snapshot.contents['fds'][source] = (None, fd)
    del marker, context
    snapshot.close()
    snapshot.close()
    assert cached() is None and held_context() is None
    assert snapshot.value is None and snapshot.pending is None
    assert snapshot.contents['files'] == {} and snapshot.contents['bytes'] == 0
    assert pins.ids == {'protected'} and tmp_path / 'protected' in pins.paths
    with pytest.raises(OSError):
        os.fstat(fd)


def test_closed_projection_cannot_reuse_even_previously_complete_pins(tmp_path):
    snapshot, context = _projection(tmp_path)
    snapshot.value = (scan._RetentionPins(context.home.owner_home_dir), True)
    snapshot.close()
    with pytest.raises(OSError, match='closed'):
        snapshot.current([])


@pytest.mark.parametrize('damage', ['version', 'count', 'missing_position', 'row_position', 'row_identity', 'row_bytes'])
def test_readonly_stream_keeps_full_identity_and_seal_rejection(tmp_path, damage):
    from agent_py_agent.agent.memory_store.candidate_ledger import (
        iter_candidate_rows,
        read_manifest,
    )

    _home, path = _ledger(tmp_path, 4)
    header = read_manifest(path)
    if damage == 'version':
        header['candidate_ledger'] = 'unknown-version'
    elif damage == 'count':
        header['count'] = True
    else:
        index = PlannedIndex(path)
        root = header['root']
        identifier = index.get(root, 'pos/0')
        entry = index.get(root, 'row/' + identifier)
        if damage == 'missing_position':
            root = index.put(root, 'pos/0', 'absent-candidate')
        elif damage == 'row_position':
            root = index.put(root, 'row/' + identifier, {**entry, 'position': 1})
        elif damage == 'row_identity':
            other_id = index.get(root, 'pos/1')
            other = index.get(root, 'row/' + other_id)
            root = index.put(root, 'row/' + identifier, {**entry, 'record': other['record']})
        else:
            (path.parent / (path.name + '.records') / entry['record']['ref']).write_bytes(b'corrupt')
        writes, _cleanup = index.finish(root)
        for name, raw in writes.items():
            (path.parent / (path.name + '.records') / name).write_bytes(raw)
        header['root'] = root
    before = path.read_bytes()
    with pytest.raises((ValueError, OSError)):
        list(iter_candidate_rows(path, header))
    assert path.read_bytes() == before


def test_explicit_full_export_still_returns_every_record_without_mutable_working_copy(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import candidate_ledger

    _home, path = _ledger(tmp_path, 128)
    header = candidate_ledger.read_manifest(path)
    expected = [row.to_record() for row in candidate_ledger.CandidateRows(path, header)]
    def forbidden(*_args, **_kwargs):
        pytest.fail('readonly full export built mutable candidate baselines')
    monkeypatch.setattr(candidates, 'CandidateRows', forbidden)
    _probe, actual = candidates._parse_candidate_ledger(path)
    assert [row.to_record() for row in actual] == expected
    actual[0].content = 'caller changed private result'
    assert next(candidate_ledger.iter_candidate_rows(path, header)).to_record() == expected[0]


def test_close_failure_still_releases_retired_context_and_preserves_returned_pins(tmp_path, monkeypatch):
    snapshot, context = _projection(tmp_path)
    context_ref = weakref.ref(context)
    pins = scan._RetentionPins(context.home.owner_home_dir, ids={'protected'})
    snapshot.value = (pins, True)
    failure = OSError('close failed after physical close')
    def failed(_descriptors):
        raise failure
    monkeypatch.setattr(scan.reference_pins, '_close_source_descriptors', failed)
    del context
    with pytest.raises(OSError) as caught:
        snapshot.close()
    assert caught.value is failure
    assert context_ref() is None and snapshot.value is None
    assert pins.ids == {'protected'}
    snapshot.close()
