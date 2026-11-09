"""不存在的候选中间节点不签发空写；真实删除逐叶保留原coordinator末门。"""
from __future__ import annotations

import os

import pytest

from agent_py_agent.agent.common import nofollow_fs
from agent_py_agent.agent.memory_store import candidates
from agent_py_agent.agent.memory_store.candidate_ledger import CandidateRows
from agent_py_agent.tests.test_memory_candidates import candidate


@pytest.fixture
def strict_probe():
    if not (nofollow_fs._supports_dir_fd() and getattr(os, 'O_NOFOLLOW', 0) and getattr(os, 'O_DIRECTORY', 0)):
        pytest.skip('严格dir_fd/nofollow探测在此平台不可用；原末门回退另有无条件行为测试')


def _case(tmp_path, monkeypatch):
    path = tmp_path / 'candidates.jsonl'
    directory = tmp_path / 'candidates.jsonl.records'
    directory.mkdir()
    calls = []

    def commit(operation):
        calls.append(True)
        return operation()

    monkeypatch.setattr(candidates, '_coordinator_candidate_write', commit)
    return path, directory, calls


def test_first_real_candidate_plan_does_not_commit_never_published_cleanup(tmp_path, monkeypatch, strict_probe):
    path, directory, calls = _case(tmp_path, monkeypatch)
    rows = CandidateRows(path, {'root': None, 'count': 0})
    rows.append(candidate())
    _root, writes, cleanup = rows.plan()
    assert cleanup and not set(cleanup).intersection(writes)
    for name, raw in writes.items():
        (directory / name).write_bytes(raw)
    manifest = {'cleanup': cleanup}
    candidates._finish_candidate_cleanup(path, manifest, coordinator_io=True)
    assert calls == [] and manifest == {'cleanup': cleanup}
    assert all((directory / name).read_bytes() == raw for name, raw in writes.items())


def test_existing_leaf_is_unlinked_only_inside_its_original_commit(tmp_path, monkeypatch, strict_probe):
    path, directory, calls = _case(tmp_path, monkeypatch)
    target = directory / 'old.json'
    target.write_bytes(b'old immutable bytes')
    inside = []
    original = nofollow_fs.unlink_file_beneath

    def commit(operation):
        inside.append(True)
        try:
            return operation()
        finally:
            inside.pop()

    def unlink(root, parts):
        assert inside == [True]
        calls.append(parts)
        return original(root, parts)

    monkeypatch.setattr(candidates, '_coordinator_candidate_write', commit)
    monkeypatch.setattr(nofollow_fs, 'unlink_file_beneath', unlink)
    candidates._finish_candidate_cleanup(path, {'cleanup': ['missing.json', 'old.json']}, coordinator_io=True)
    assert calls == [('candidates.jsonl.records', 'old.json')] and not target.exists()


def test_existing_leaf_revocation_retains_bytes_and_cleanup_obligation(tmp_path, monkeypatch, strict_probe):
    path, directory, _calls = _case(tmp_path, monkeypatch)
    target = directory / 'old.json'
    target.write_bytes(b'retain')
    failure = PermissionError('revoked at original commit')
    def denied(_operation):
        raise failure
    monkeypatch.setattr(candidates, '_coordinator_candidate_write', denied)
    manifest = {'cleanup': ['old.json']}
    with pytest.raises(PermissionError) as caught:
        candidates._finish_candidate_cleanup(path, manifest, coordinator_io=True)
    assert caught.value is failure and target.read_bytes() == b'retain'
    assert manifest == {'cleanup': ['old.json']}


@pytest.mark.parametrize('kind', ['permission', 'directory', 'symlink', 'parent_symlink'])
def test_unknown_or_unsafe_probe_never_becomes_missing(tmp_path, monkeypatch, kind, strict_probe):
    path, directory, calls = _case(tmp_path, monkeypatch)
    if kind == 'permission':
        failure = PermissionError('probe unavailable')
        def denied(*_args):
            raise failure
        monkeypatch.setattr(candidates, 'open_readonly_file_beneath', denied)
        expected = PermissionError
    else:
        if kind == 'directory':
            (directory / 'old.json').mkdir()
        elif kind == 'symlink':
            (tmp_path / 'outside').write_bytes(b'untouched')
            (directory / 'old.json').symlink_to(tmp_path / 'outside')
        else:
            directory.rmdir()
            directory.symlink_to(tmp_path / 'missing-parent')
        expected = nofollow_fs.NoFollowPathError
    with pytest.raises(expected):
        candidates._finish_candidate_cleanup(path, {'cleanup': ['old.json']}, coordinator_io=True)
    assert calls == []


def test_present_then_removed_still_uses_original_commit(tmp_path, monkeypatch, strict_probe):
    path, directory, calls = _case(tmp_path, monkeypatch)
    target = directory / 'old.json'
    target.write_bytes(b'old')
    def commit(operation):
        calls.append(True)
        target.unlink()
        return operation()
    monkeypatch.setattr(candidates, '_coordinator_candidate_write', commit)
    candidates._finish_candidate_cleanup(path, {'cleanup': ['old.json']}, coordinator_io=True)
    assert calls == [True] and not target.exists()


def test_missing_probe_then_birth_does_not_delete_without_commit_and_replay_can_clean(tmp_path, monkeypatch, strict_probe):
    path, directory, calls = _case(tmp_path, monkeypatch)
    target = directory / 'old.json'
    original = candidates.open_readonly_file_beneath
    def raced(*_args):
        target.write_bytes(b'late birth')
        raise FileNotFoundError('proved missing before birth')
    monkeypatch.setattr(candidates, 'open_readonly_file_beneath', raced)
    manifest = {'cleanup': ['old.json']}
    candidates._finish_candidate_cleanup(path, manifest, coordinator_io=True)
    assert calls == [] and target.read_bytes() == b'late birth'
    monkeypatch.setattr(candidates, 'open_readonly_file_beneath', original)
    candidates._finish_candidate_cleanup(path, manifest, coordinator_io=True)
    assert calls == [True] and not target.exists()


def test_platform_without_strict_probe_keeps_original_commit(tmp_path, monkeypatch):
    path, _directory, calls = _case(tmp_path, monkeypatch)
    monkeypatch.setattr(nofollow_fs, '_supports_dir_fd', lambda: False)
    candidates._finish_candidate_cleanup(path, {'cleanup': ['missing.json']}, coordinator_io=True)
    assert calls == [True]


def test_sealed_cleanup_failure_keeps_replay_proof_until_real_deletions_finish(tmp_path, monkeypatch, strict_probe):
    from agent_py_agent.agent.memory_store.candidate_ledger import seal_cleanup

    path, directory, calls = _case(tmp_path, monkeypatch)
    target = directory / 'old.json'
    target.write_bytes(b'old')
    writes = {}
    reference = seal_cleanup(writes, ['missing.json', 'old.json'])
    for name, raw in writes.items():
        (directory / name).write_bytes(raw)
    proof = directory / reference['ref']
    before = proof.read_bytes()
    def revoked(_operation):
        raise PermissionError('current source revoked')
    monkeypatch.setattr(candidates, '_coordinator_candidate_write', revoked)
    with pytest.raises(PermissionError):
        candidates._finish_candidate_cleanup(path, {'cleanup': reference}, coordinator_io=True)
    assert target.read_bytes() == b'old' and proof.read_bytes() == before
    def replay(operation):
        calls.append(True)
        return operation()
    monkeypatch.setattr(candidates, '_coordinator_candidate_write', replay)
    candidates._finish_candidate_cleanup(path, {'cleanup': reference}, coordinator_io=True)
    assert calls == [True, True] and not target.exists() and not proof.exists()
    candidates._finish_candidate_cleanup(path, {'cleanup': reference}, coordinator_io=True)
    assert calls == [True, True]
