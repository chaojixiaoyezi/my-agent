"""RES-W2: real v3 commits survive ledger damage without hiding identity faults."""
from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import sys
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common import json_io, nofollow_fs
from agent_py_agent.agent.common.json_io import locked_json_path
from agent_py_agent.agent.conversation import compact_checkpoint as checkpoints
from agent_py_agent.agent.conversation import compact_checkpoint_scan as scan
from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE
from agent_py_agent.agent.conversation.compact_summary_view import resolve_compact_summary_view
from agent_py_agent.tests.test_compact_evidence_offload import _offloaded
from agent_py_agent.tests.test_compact_scoped_checkpoint import (
    _checkpoint_path,
    _commit_transcript,
)
from agent_py_agent.tests.test_compact_scoped_checkpoint import compact_case as _compact_case

compact_case = _compact_case


# LLM: Fixture chains use the production candidate writer and the canonical thread CAS, never synthetic checkpoint rows.
# 函数用途: Commit one thread-scoped summary with a distinct source and clock.
def _commit(case, label):
    case.clock[0] += 1
    return _commit_transcript(case, scope=THREAD_COMPACT_SCOPE, summary=label, content=label, publish=True)[0]


# LLM: Archive addressing must be the same exact-id hash as production, with no directory search.
# 函数用途: Locate the cold copy selected by an already committed head.
def _copy_path(case, checkpoint_id):
    name = hashlib.sha256(checkpoint_id.encode()).hexdigest() + '.checkpoint.json'
    return case.root / 'compact' / 'archive' / case.thread_id / name


# LLM: Both the carried-view head reader and the full chain reader must survive a damaged ledger without writing it.
# 函数用途: Damage the selected hot head or ancestor so both strict and skip parsers must use a cold copy.
@pytest.mark.parametrize('fault', ['torn', 'middle'])
def test_damaged_ledger_uses_cold_copies(compact_case, caplog, fault):
    case = compact_case
    first = _commit(case, 'first')
    head = _commit(case, 'head')
    path = _checkpoint_path(case)
    raw = path.read_bytes()
    lines = raw.splitlines(keepends=True)
    damaged = lines[0] + lines[1][:20] if fault == 'torn' else b'not json\n' + b''.join(lines[1:])
    path.write_bytes(damaged)
    view = resolve_compact_summary_view(case.agent, head, THREAD_COMPACT_SCOPE)
    assert (view.summary, view.checkpoint_id) == ('head', head.compact_checkpoint_id)
    chain = checkpoints.committed_compact_checkpoint_chain(case.agent, head)
    assert [row['checkpoint_id'] for row in chain] == [first.compact_checkpoint_id, head.compact_checkpoint_id]
    assert path.read_bytes() == damaged
    assert 'COMPACT_LEDGER_COLD_FALLBACK' in caplog.text
    assert f'thread={case.thread_id}' in caplog.text


# LLM: A missing hot/cold row fails closed under either scanner contract; injected entry errors remain identity-exact below.
# 函数用途: Accept only the strict parser's unreadable error or the skip parser's missing-chain error.
@pytest.mark.parametrize('missing', ['head', 'ancestor'])
def test_unavailable_copy_preserves_original_ledger_error(compact_case, missing):
    case = compact_case
    first = _commit(case, 'first')
    head = _commit(case, 'head')
    path = _checkpoint_path(case)
    lines = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(lines[0] + lines[1][:20] if missing == 'head' else b'not json\n' + lines[1])
    _copy_path(case, (head if missing == 'head' else first).compact_checkpoint_id).unlink()
    with pytest.raises(OSError) as caught:
        checkpoints.committed_compact_checkpoint_chain(case.agent, head)
    assert str(caught.value) in ('conversation compact checkpoint ledger is unreadable',
        'conversation compact checkpoint chain is incomplete')
    if missing == 'head':
        with pytest.raises(OSError) as caught:
            resolve_compact_summary_view(case.agent, head, THREAD_COMPACT_SCOPE)
        assert str(caught.value) in ('conversation compact checkpoint ledger is unreadable',
            'conversation compact checkpoint chain is incomplete')


# LLM: The three allowed failures are caught only while entering the scan, before any record lookup or validation.
# 函数用途: Check both readers' exception boundaries with the exact original exception object.
@pytest.mark.parametrize('reader', ['latest', 'chain'])
@pytest.mark.parametrize('message', [
    'conversation compact checkpoint ledger is unreadable',
    'conversation compact checkpoint record exceeds byte budget',
    'conversation compact checkpoint ledger was truncated',
])
def test_entry_failure_falls_back_but_walk_failure_does_not(compact_case, monkeypatch, reader, message):
    case = compact_case
    head = _commit(case, 'head')
    original = OSError(message)

    # LLM: This fake fails on context entry, matching the verified scanner contract.
    # 函数用途: Inject an allowed scan-entry failure without changing the archive.
    @contextmanager
    def broken_entry(*_args, **_kwargs):
        raise original
        yield  # pragma: no cover - retains the contextmanager protocol

    # LLM: The caller's public reader determines whether only the head or the complete chain is requested.
    # 函数用途: Exercise exactly the two production read entry points.
    def read():
        if reader == 'latest':
            return checkpoints.load_latest_committed_compact_checkpoint(case.agent, head)
        return checkpoints.committed_compact_checkpoint_chain(case.agent, head)[-1]

    monkeypatch.setattr(scan, 'checkpoint_row_lookup', broken_entry)
    assert read()['checkpoint_id'] == head.compact_checkpoint_id
    _copy_path(case, head.compact_checkpoint_id).unlink()
    with pytest.raises(OSError) as caught:
        read()
    assert caught.value is original

    # LLM: A matching error text raised after entry must propagate, not start another fallback.
    # 函数用途: Detect accidentally broad exception guards around the lookup or walk.
    @contextmanager
    def broken_walk(*_args, **_kwargs):
        def lookup(_checkpoint_id):
            raise original
        yield lookup

    monkeypatch.setattr(scan, 'checkpoint_row_lookup', broken_walk)
    with pytest.raises(OSError) as caught:
        read()
    assert caught.value is original


# LLM: Cold copy validation remains outside the scan-entry exception handler.
# 函数用途: Distinguish sealed identity mutations from summary-byte digest mutations without hiding either fault.
@pytest.mark.parametrize('tamper,error', [('identity', 'content identity mismatches'), ('summary', 'summary digest mismatches')])
def test_cold_identity_fault_is_not_hidden(compact_case, tamper, error):
    case = compact_case
    head = _commit(case, 'head')
    path = _copy_path(case, head.compact_checkpoint_id)
    row = json.loads(path.read_bytes())
    if tamper == 'identity':
        row['created_at'] += 1
    else:
        row['summary'] = 'tampered'
    path.write_text(json.dumps(row))
    _checkpoint_path(case).write_bytes(b'{')
    with pytest.raises(OSError, match=error):
        resolve_compact_summary_view(case.agent, head, THREAD_COMPACT_SCOPE)


# LLM: A real subsequent compaction seals only the damaged suffix under the existing writer lock.
# 函数用途: Verify the new head comes from a readable ledger and the torn bytes survive in a private sidecar.
def test_compaction_seals_torn_tail_before_append(compact_case, caplog):
    case = compact_case
    _commit(case, 'first')
    path = _checkpoint_path(case)
    prefix = path.read_bytes()
    path.write_bytes(prefix + b'{')
    head = _commit(case, 'repaired')
    with scan.checkpoint_row_lookup(path) as lookup:
        assert lookup(head.compact_checkpoint_id)['summary'] == 'repaired'
    assert path.read_bytes().startswith(prefix)
    sidecars = list(path.parent.glob(f'.{path.name}.torn-*'))
    assert len(sidecars) == 1 and sidecars[0].read_bytes() == b'{'
    if os.name == 'posix':
        assert sidecars[0].stat().st_mode & 0o777 == 0o600
    assert 'COMPACT_LEDGER_TORN_TAIL_SEALED' in caplog.text


# LLM: The high-water threshold is reduced only inside this test; valid production records and rotation stay real.
# 函数用途: Rotate a ledger containing garbage and retain all committed generations via cold copies.
def test_rotation_skips_garbage_once_and_rewrites_cleanly(compact_case, monkeypatch, caplog):
    case = compact_case
    _commit(case, 'first')
    _commit(case, 'second')
    path = _checkpoint_path(case)
    lines = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(lines[0] + b'not json\n[]\n' + b''.join(lines[1:]))
    monkeypatch.setattr(checkpoints, '_ACTIVE_CHECKPOINT_HIGH_WATER_COUNT', 2)
    monkeypatch.setattr(checkpoints, 'MAX_ACTIVE_COMPACT_CHECKPOINTS', 2)
    head = _commit(case, 'third')
    rows = [json.loads(line) for line in path.read_bytes().splitlines()]
    assert [row['generation'] for row in rows] == [2, 3]
    assert [row['generation'] for row in checkpoints.committed_compact_checkpoint_chain(case.agent, head)] == [1, 2, 3]
    assert sum('COMPACT_LEDGER_ROW_SKIPPED' in record.message for record in caplog.records) >= 1


# LLM: Missing evidence has a dedicated OSError subtype; corruption and no-follow faults must retain their old refusals.
# 函数用途: Exercise the operation-evidence payload after a real heavy-payload commit.
@pytest.mark.parametrize('fault', ['missing', 'corrupt', 'symlink'])
def test_operation_payload_absence_is_distinct_from_corruption(compact_case, fault):
    case = compact_case
    thread, raw, _evidence, _cipher = _offloaded(case)
    path = case.root / 'compact' / 'evidence' / case.thread_id / raw['operation_evidence_ref']['ref']
    if fault == 'corrupt':
        path.write_bytes(b'{}')
    else:
        saved = path.with_suffix('.saved')
        path.rename(saved)
        if fault == 'symlink':
            path.symlink_to(saved)
    if fault == 'missing':
        with pytest.raises(FileNotFoundError) as caught:
            resolve_compact_summary_view(case.agent, thread, THREAD_COMPACT_SCOPE)
        assert str(caught.value) == 'conversation compact cold payload is missing'
    else:
        with pytest.raises(OSError) as caught:
            resolve_compact_summary_view(case.agent, thread, THREAD_COMPACT_SCOPE)
        assert not isinstance(caught.value, FileNotFoundError)
        if fault == 'corrupt':
            assert str(caught.value) == 'conversation compact cold payload digest or length mismatches'


# LLM: Readers never recreate deleted compact subdirectories; only the next writer rebuilds its own managed paths.
# 函数用途: Check ledger/archive deletion and an older-ledger restore without losing the newer committed head.
@pytest.mark.parametrize('fault', ['ledger_directory', 'archive_directory', 'older_ledger'])
def test_subdirectory_loss_and_older_restore_recover_on_next_write(compact_case, fault):
    case = compact_case
    _commit(case, 'first')
    path = _checkpoint_path(case)
    older = path.read_bytes()
    head = _commit(case, 'second')
    root = case.root / 'compact'
    if fault == 'older_ledger':
        path.write_bytes(older)
        removed = None
    else:
        removed = root / ('conversations' if fault == 'ledger_directory' else 'archive')
        shutil.rmtree(removed)
    view = resolve_compact_summary_view(case.agent, head, THREAD_COMPACT_SCOPE)
    assert view.summary == 'second' and view.checkpoint_id == head.compact_checkpoint_id
    if removed is not None:
        assert not removed.exists()
    new_head = _commit(case, 'third')
    assert resolve_compact_summary_view(case.agent, new_head, THREAD_COMPACT_SCOPE).summary == 'third'
    with scan.checkpoint_row_lookup(path) as lookup:
        assert lookup(new_head.compact_checkpoint_id)['generation'] == 3
    assert _copy_path(case, new_head.compact_checkpoint_id).is_file()


# LLM: A no-LF object is completed, but invalid UTF-8, non-objects and partial JSON are preserved only as private evidence.
# 函数用途: Verify the tail helper's exact bytes, permissions and return values under its required lock.
@pytest.mark.parametrize('tail,action', [(b'', ''), (b'{"candidate":1}', 'completed'),
    (b'{', 'quarantined'), (b'\xff', 'quarantined'), (b'[]', 'quarantined')])
def test_tail_seal_preserves_committed_prefix(compact_case, tail, action):
    path = _checkpoint_path(compact_case)
    prefix = b'{"committed":1}\n'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(prefix + tail)
    with locked_json_path(path):
        assert checkpoints._seal_torn_ledger_tail(path) == action
    expected = prefix + tail + b'\n' if action == 'completed' else prefix
    assert path.read_bytes() == expected
    sidecars = list(path.parent.glob(f'.{path.name}.torn-*'))
    assert len(sidecars) == (1 if action == 'quarantined' else 0)
    if sidecars:
        assert sidecars[0].read_bytes() == tail
        if os.name == 'posix':
            assert sidecars[0].stat().st_mode & 0o777 == 0o600


# LLM: No ledger is a normal first write, and an empty ledger requires no repair or sidecar.
# 函数用途: Keep missing and empty files as no-op tail seals.
@pytest.mark.parametrize('empty_file', [False, True])
def test_missing_or_empty_tail_is_noop(compact_case, empty_file):
    path = _checkpoint_path(compact_case)
    if empty_file:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    with locked_json_path(path):
        assert checkpoints._seal_torn_ledger_tail(path) == ''
    assert path.exists() is empty_file


# LLM: Sealing must not introduce a new write-through-symlink surface before the normal private append rejects it.
# 函数用途: Keep a symlinked ledger target byte-for-byte untouched.
def test_tail_seal_refuses_symlinked_leaf(compact_case):
    case = compact_case
    path = _checkpoint_path(case)
    path.parent.mkdir(parents=True, exist_ok=True)
    target = case.root / 'outside-ledger'
    target.write_bytes(b'private\n{')
    path.symlink_to(target)
    with locked_json_path(path), pytest.raises(OSError):
        checkpoints._seal_torn_ledger_tail(path)
    assert target.read_bytes() == b'private\n{'


# LLM: The continuation authorizes an exact-id hot retry only when the immutable copy is absent.
# 函数用途: Verify read-only recovery, the explicit no-retry flag, and exhaustion when both stores are gone.
def test_missing_copy_hot_retry_is_exact_and_read_only(compact_case):
    case = compact_case
    head = _commit(case, 'head')
    copy = _copy_path(case, head.compact_checkpoint_id)
    copy.unlink()
    path = _checkpoint_path(case)
    before = path.read_bytes()
    root = case.root / 'compact'
    assert checkpoints._cold_checkpoint(root, case.thread_id, head.compact_checkpoint_id, fallback_hot=False) is None
    assert checkpoints._cold_checkpoint(root, case.thread_id, head.compact_checkpoint_id)['summary'] == 'head'
    assert path.read_bytes() == before and not copy.exists()
    path.unlink()
    assert checkpoints._cold_checkpoint(root, case.thread_id, head.compact_checkpoint_id) is None


# LLM: A present but damaged or symlinked cold copy never takes the missing-copy hot fallback.
# 函数用途: Keep immutable-copy integrity and no-follow refusals even with a valid hot head.
@pytest.mark.parametrize('fault', ['garbage', 'symlink'])
def test_present_cold_fault_never_uses_valid_hot_row(compact_case, fault):
    case = compact_case
    head = _commit(case, 'head')
    copy = _copy_path(case, head.compact_checkpoint_id)
    if fault == 'garbage':
        copy.write_bytes(b'not json')
    else:
        saved = copy.with_suffix('.saved')
        copy.rename(saved)
        copy.symlink_to(saved)
    with pytest.raises(OSError):
        checkpoints._cold_checkpoint(case.root / 'compact', case.thread_id, head.compact_checkpoint_id)


# LLM: The hot fallback cannot trust a keyed lookup alone; thread and v3 content identity remain mandatory.
# 函数用途: Refuse valid JSON hot records with foreign identity or a changed sealed timestamp.
@pytest.mark.parametrize('fault', ['thread', 'identity'])
def test_hot_retry_keeps_record_validation(compact_case, fault):
    case = compact_case
    head = _commit(case, 'head')
    _copy_path(case, head.compact_checkpoint_id).unlink()
    path = _checkpoint_path(case)
    row = json.loads(path.read_bytes())
    if fault == 'thread':
        row['thread_id'] = 'foreign-thread'
    else:
        row['created_at'] += 1
    path.write_text(json.dumps(row) + '\n')
    with pytest.raises(OSError, match='identity mismatches'):
        checkpoints._cold_checkpoint(case.root / 'compact', case.thread_id, head.compact_checkpoint_id)


# LLM: A failed evidence write must precede any ledger truncation and prevent the canonical CAS from being reached.
# 函数用途: Prove a sealing failure leaves the damaged bytes and previous committed head intact.
def test_failed_tail_quarantine_does_not_advance_head(compact_case, monkeypatch):
    case = compact_case
    head = _commit(case, 'head')
    path = _checkpoint_path(case)
    damaged = path.read_bytes() + b'{'
    path.write_bytes(damaged)
    original = OSError('injected quarantine failure')

    # LLM: Inject only at the local quarantine boundary, not the shared filesystem helpers or real CAS.
    # 函数用途: Keep the production read and candidate assembly paths real while refusing the destructive seal.
    def fail(_path, _tail):
        raise original

    monkeypatch.setattr(checkpoints, '_save_torn_ledger_tail', fail)
    with pytest.raises(OSError) as caught:
        _commit(case, 'uncommitted')
    assert caught.value is original
    assert path.read_bytes() == damaged
    current = case.store.threads.require(case.thread_id)
    # The real source-message append updates activity, but no compact state or other thread field may move.
    assert current.updated_at == case.clock[0]
    assert replace(current, updated_at=head.updated_at) == head


# LLM: Equal-millisecond sidecar collisions must fail closed rather than overwrite earlier evidence.
# 函数用途: Preserve both the source and an existing quarantine file on an exclusive-create collision.
def test_tail_sidecar_collision_preserves_source_and_evidence(compact_case):
    case = compact_case
    path = _checkpoint_path(case)
    path.parent.mkdir(parents=True, exist_ok=True)
    damaged = b'{"committed":1}\n{'
    path.write_bytes(damaged)
    sidecar = path.with_name(f'.{path.name}.torn-{int(case.clock[0] * 1000)}')
    sidecar.write_bytes(b'previous evidence')
    with locked_json_path(path), pytest.raises(FileExistsError):
        checkpoints._seal_torn_ledger_tail(path)
    assert path.read_bytes() == damaged and sidecar.read_bytes() == b'previous evidence'


# LLM: Probe the active decoder's recursion guard with bounded doubling; no interpreter-wide limit changes or skipped assertions.
# 函数用途: Preserve undecodable tail bytes across Python versions, including complete but unparseable deep objects.
@pytest.mark.parametrize('shape', ['incomplete_array', 'incomplete_object', 'deep_object'])
def test_depth_exhausted_tail_is_preserved_before_quarantine(compact_case, shape):
    case = compact_case
    # Decoder guards differ across CPython versions and need not follow the Python recursion limit.
    depth = max(10_000, sys.getrecursionlimit() + 50)
    for _ in range(8):
        try:
            json.loads(b'[' * depth + b']' * depth)
        except RecursionError:
            break
        depth *= 2
    else:
        pytest.fail('JSON decoder did not reach RecursionError within eight depth probes')
    tail = b'[' * depth
    if shape != 'incomplete_array':
        tail = b'{"value":' + tail
    if shape == 'deep_object':
        tail += b'0' + b']' * depth + b'}'
    with pytest.raises(RecursionError):
        json.loads(tail)
    path = _checkpoint_path(case)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(tail)
    head = _commit(case, 'first committed checkpoint')
    with scan.checkpoint_row_lookup(path) as lookup:
        assert lookup(head.compact_checkpoint_id)['summary'] == 'first committed checkpoint'
    sidecars = list(path.parent.glob(f'.{path.name}.torn-*'))
    assert len(sidecars) == 1 and sidecars[0].read_bytes() == tail
    if os.name == 'posix':
        assert sidecars[0].stat().st_mode & 0o777 == 0o600


# LLM: Missing-hot warnings are emitted only after a successful cold read; an entry warning must not be repeated.
# 函数用途: Pin both public readers' healthy, missing-row and already-warned paths without changing scanner policy.
@pytest.mark.parametrize('reader', ['latest', 'chain'])
@pytest.mark.parametrize('mode', ['hot', 'missing', 'entry'])
def test_cold_fallback_warning_covers_missing_hot_without_duplicates(compact_case, monkeypatch, caplog, reader, mode):
    case = compact_case
    head = _commit(case, 'head')
    path = _checkpoint_path(case)
    if mode == 'missing':
        path.write_bytes(b'')
    elif mode == 'entry':
        # LLM: The same entry error can still select the real immutable cold copy without a hot retry.
        # 函数用途: Exercise the previously logged scan-entry branch independently of the active parser policy.
        @contextmanager
        def broken_entry(*_args, **_kwargs):
            raise OSError('conversation compact checkpoint ledger is unreadable')
            yield  # pragma: no cover - retains the contextmanager protocol

        monkeypatch.setattr(scan, 'checkpoint_row_lookup', broken_entry)
    before = path.read_bytes()
    caplog.clear()
    if reader == 'latest':
        row = checkpoints.load_latest_committed_compact_checkpoint(case.agent, head)
    else:
        row = checkpoints.committed_compact_checkpoint_chain(case.agent, head)[-1]
    assert row['checkpoint_id'] == head.compact_checkpoint_id and row['summary'] == 'head'
    warnings = [record.getMessage() for record in caplog.records if 'COMPACT_LEDGER_COLD_FALLBACK' in record.getMessage()]
    assert len(warnings) == (0 if mode == 'hot' else 1)
    if warnings:
        reason = 'missing_hot_row' if mode == 'missing' else 'conversation compact checkpoint ledger is unreadable'
        assert warnings == [f'COMPACT_LEDGER_COLD_FALLBACK thread={case.thread_id} reason={reason}']
    assert path.read_bytes() == before


# 只替换被测模块的能力表，不删除进程级 os 属性；原 portable 分支仍执行真实文件操作。
@contextmanager
def _portable_checkpoint_io(monkeypatch):
    descriptors, handles, opened_paths = set(), [], {}

    def tracked_open(path, *args, **kwargs):
        descriptor = os.open(path, *args, **kwargs)
        descriptors.add(descriptor)
        opened_paths[descriptor] = Path(path)
        return descriptor

    def tracked_fdopen(descriptor, *args, **kwargs):
        handle = os.fdopen(descriptor, *args, **kwargs)
        handles.append(handle)
        return handle

    portable = SimpleNamespace(**{key: value for key, value in vars(os).items()
                                 if key not in {'O_NOFOLLOW', 'O_DIRECTORY'}})
    portable.supports_dir_fd = set()
    portable.open, portable.fdopen = tracked_open, tracked_fdopen
    portable.opened_paths = opened_paths
    with monkeypatch.context() as patch:
        for module in (checkpoints, nofollow_fs, json_io):
            patch.setattr(module, 'os', portable)
        try:
            yield portable
        finally:
            fd_delta = 0
            for descriptor in descriptors:
                try:
                    os.fstat(descriptor)
                except OSError as error:
                    assert error.errno == errno.EBADF
                else:
                    fd_delta += 1
            assert fd_delta == 0
            assert all(handle.closed for handle in handles)


# 与现有 scoped fixture 共用真实 writer/CAS；live 请求使用四元来源身份，不伪造 checkpoint 行。
def _commit_portable_writer(case, writer, label):
    if writer == 'transcript':
        return _commit(case, label)
    from agent_py_agent.agent.conversation.live_tool_compact import (
        LiveToolCompactBinding,
        LiveToolCompactCommitRequest,
        commit_live_tool_compact,
    )

    case.clock[0] += 1
    thread = case.store.threads.require(case.thread_id)
    return commit_live_tool_compact(
        case.agent, LiveToolCompactBinding(store=case.store, thread=thread),
        LiveToolCompactCommitRequest(
            summary=label, source_tool_call_ids=(label,), retained_tool_call_ids=(),
            projected_tokens_before=900, projected_tokens_after=300, policy=case.policy,
            request_id='summary-request', attempt_id='summary-attempt',
            source_tool_refs=({'run_id': 'run', 'attempt_id': 'source-attempt',
                               'turn_id': 'turn', 'call_id': label},),
        ),
    )


@pytest.mark.parametrize('writer', ['transcript', 'live'])
@pytest.mark.parametrize('state', ['missing', 'empty', 'healthy', 'no_lf', 'torn'])
def test_portable_public_writers_preserve_ledger_and_close_resources(compact_case, monkeypatch, writer, state):
    case = compact_case
    path = _checkpoint_path(case)
    prefix, tail = b'', b'{"unfinished":"private'
    if state in {'healthy', 'no_lf', 'torn'}:
        _commit(case, 'base')
        prefix = path.read_bytes()
        if state == 'no_lf':
            path.write_bytes(prefix[:-1])
        elif state == 'torn':
            path.write_bytes(prefix + tail)
    elif state == 'empty':
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    inode = path.stat().st_ino if path.exists() else None
    before = case.store.threads.require(case.thread_id)
    with _portable_checkpoint_io(monkeypatch):
        assert not nofollow_fs._supports_dir_fd()
        if state == 'missing':
            with locked_json_path(path):
                assert checkpoints._seal_torn_ledger_tail(path) == ''
            assert not path.exists()
        head = _commit_portable_writer(case, writer, 'portable')
        chain = checkpoints.committed_compact_checkpoint_chain(case.agent, head)
        assert chain[-1]['checkpoint_id'] == head.compact_checkpoint_id
        assert chain[-1]['source_kind'] == ('transcript' if writer == 'transcript' else 'live_tool_ir')
        assert chain[-1]['previous_checkpoint_id'] == before.compact_checkpoint_id
        assert resolve_compact_summary_view(case.agent, head, THREAD_COMPACT_SCOPE).summary == 'portable'
    raw = path.read_bytes()
    assert raw.startswith(prefix) and raw.endswith(b'\n')
    assert len(raw.splitlines()) == (2 if prefix else 1)
    assert head.compact_generation == before.compact_generation + 1
    assert case.store.threads.require(case.thread_id) == head
    if inode is not None:
        assert path.stat().st_ino == inode
    sidecars = list(path.parent.glob(f'.{path.name}.torn-*'))
    assert len(sidecars) == (1 if state == 'torn' else 0)
    if sidecars:
        assert sidecars[0].read_bytes() == tail


# 成功与单次 I/O 报错都核对全部所观察 FD/handle；失败只能留下冷候选，不能获得 head 权威。
@pytest.mark.parametrize('writer', ['transcript', 'live'])
@pytest.mark.parametrize('failure', ['evidence_fsync', 'parent_sync'])
def test_portable_seal_failure_preserves_head_and_closes_resources(compact_case, monkeypatch, writer, failure):
    case = compact_case
    head = _commit(case, 'base')
    path = _checkpoint_path(case)
    tail = b'{"unfinished":"private'
    damaged = path.read_bytes() + tail
    path.write_bytes(damaged)
    original = OSError(errno.EIO, 'simulated evidence sync error')
    calls = []
    with _portable_checkpoint_io(monkeypatch) as portable:
        def fail_evidence_sync(descriptor):
            target = portable.opened_paths[descriptor]
            if target.name.startswith(f'.{path.name}.torn-'):
                assert target.read_bytes() == tail and path.read_bytes() == damaged
                calls.append(target)
                raise original
            return os.fsync(descriptor)

        def fail_parent_sync(sidecar):
            assert sidecar.read_bytes() == tail and path.read_bytes() == damaged
            calls.append(sidecar)
            if os.name == 'nt':
                # Windows 原 helper 没有目录 FD；仍验证调用边界的原异常透传。
                raise original
            with monkeypatch.context() as sync_patch:
                def fail_directory_fsync(_descriptor):
                    raise original
                sync_patch.setattr(portable, 'fsync', fail_directory_fsync)
                json_io._sync_parent_directory(sidecar)

        if failure == 'evidence_fsync':
            portable.fsync = fail_evidence_sync
        else:
            monkeypatch.setattr(checkpoints, '_sync_parent_directory', fail_parent_sync, raising=False)
        with pytest.raises(OSError) as caught:
            _commit_portable_writer(case, writer, 'uncommitted')
        assert caught.value is original and len(calls) == 1
        current = case.store.threads.require(case.thread_id)
        assert replace(current, updated_at=head.updated_at) == head
        assert [row['checkpoint_id'] for row in checkpoints.committed_compact_checkpoint_chain(case.agent, current)] == [head.compact_checkpoint_id]
    assert path.read_bytes() == damaged
    sidecars = list(path.parent.glob(f'.{path.name}.torn-*'))
    assert len(sidecars) == 1 and sidecars[0].read_bytes() == tail
    assert len(list(_copy_path(case, head.compact_checkpoint_id).parent.glob('*.checkpoint.json'))) == 2
