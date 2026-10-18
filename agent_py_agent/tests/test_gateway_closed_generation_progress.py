# LLM: 仅新临时目录与受控线程；v2为纯格式fixture，不冒充原producer关闭或fresh P2，原reader/心跳/归档writer保持。
# 模块用途: 核旧闭包消费与本代责任自然推进分界，旧固定来源、历史和危险路径仍必须拒绝；须双OS新包pyc实跑。
import hashlib
import json
import threading
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.nofollow_fs import write_bytes_atomic_beneath
from agent_py_agent.agent.gateway_parts import daemon_metadata
from agent_py_agent.agent.gateway_parts import multiprocess_generation_metadata as history
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.agent.gateway_parts.lease_service import refresh_processing_lease_report
from agent_py_agent.agent.gateway_parts.multiprocess_records import serialize_record
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.recovery import terminalize_gateway_request_file


# LLM: 手造v2仅隔离消费格式合同，不签关闭事实；身份边界是明确fake，绝不探测真实进程或创建Agent。
# 函数用途: 给原reader准备不可变旧摘要、本代processing和完整schema，所有文件仅属于本测试。
@pytest.fixture
def generation_case(tmp_path, monkeypatch):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    paths.processing.mkdir(parents=True)
    oldref = 'admission/reservations/old.json'
    oldraw = b'{"route":{"c_run_id":"old-c"},"state":"released"}'
    write_bytes_atomic_beneath(paths.root, tuple(oldref.split('/')), oldraw)
    evidence = b'{"synthetic":"old-evidence"}'
    eref = 'workers/takeover_evidence/' + hashlib.sha256(evidence).hexdigest() + '.json'
    write_bytes_atomic_beneath(paths.root, tuple(eref.split('/')), evidence)
    identity = {'schema_version': 'multiprocess_process_identity.v1', 'host_id': 'component-only',
                'pid': 999999990, 'start_time': 'synthetic-old-birth'}
    registry = {'schema_version': 'multiprocess_worker_registry.v1', 'c_run_id': 'old-c', 'worker_count': 1,
                'process_identity': identity, 'http_port': 1, 'workers': [{'worker': 0,
                'instance_id': 'old-instance', 'process_identity': {**identity, 'pid': 999999991},
                'executor_ledger_ref': 'workers/executors/w0'}]}
    record = {'schema_version': 'multiprocess_c_generation.v2', 'c_run_id': 'old-c', 'worker_count': 1,
              'closure': {'registry': registry, 'registry_sha256': hashlib.sha256(serialize_record(registry)).hexdigest(),
              'role_markers': {'gateway_state.json': 'a' * 64, 'workers/w0.json': 'b' * 64},
              'source_files': {oldref: hashlib.sha256(oldraw).hexdigest()},
              'tree_evidence': {eref: hashlib.sha256(evidence).hexdigest()}}}
    raw = serialize_record(record)
    write_bytes_atomic_beneath(paths.root, history.generation_metadata_parts('old-c'), raw)
    request = paths.processing / 'component-current.json'
    write_json_file_atomic(request, {'id': request.stem, 'kind': 'ask', 'status': 'processing',
        'route': {'c_run_id': 'new-c'}, 'execution_attempt_id': 'component-attempt', 'lease_epoch': 1,
        'lease_owner': 'component-worker', 'lease_started_at': 1.0, 'lease_heartbeat_at': 1.0, 'updated_at': 1.0})
    monkeypatch.setattr(daemon_metadata, 'process_identity_is_live', lambda _identity: False)
    return SimpleNamespace(paths=paths, request=request, record=record, raw=raw, oldref=oldref,
                           eref=eref, committed=(), covered=())


# LLM: 仅调用真实consumer；committed/covered是本次固定tuple，不在两读之间重新取来源。
# 函数用途: 保持原历史原字节绑定及准确count/current参数。
def consume(case):
    return history.require_closed_generation(case.paths, 'old-c', 1, 'new-c', case.raw,
                                             committed=case.committed, covered=case.covered)


# LLM: 包装只在真实首库存返回后放行原writer，第二库存仍完整原读；不替换返回值、不重试、不授许可。
# 函数用途: 用有限线程屏障让本测试明确发表发生于两个完整快照之间，结束后严格回收本线程。
def interleaved(case, monkeypatch, action):
    entered, finished = threading.Event(), threading.Event()
    errors, snapshots = [], []
    original = history.generation_source_files

    # LLM: 仅运行本测试固定原writer；任何异常交回主线程，不能形成后台遗留。
    # 函数用途: 在首库存屏障后发表一次临时责任变化。
    def writer():
        try:
            assert entered.wait(5)
            action()
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    # LLM: 返回值始终来自原完整reader，不用合成库存替代产品判定。
    # 函数用途: 记录真实快照并在首读后等待受控writer完成。
    def snapshot(paths):
        actual = original(paths)
        snapshots.append(actual)
        if len(snapshots) == 1:
            entered.set()
            assert finished.wait(5)
            assert not errors
        return actual

    worker = threading.Thread(target=writer, name='closed-generation-current-writer')
    worker.start()
    try:
        with monkeypatch.context() as child:
            child.setattr(history, 'generation_source_files', snapshot)
            result = consume(case)
        return result, snapshots
    finally:
        entered.set()
        worker.join(5)
        assert not worker.is_alive()
        assert not errors


# LLM: 原W请求心跳只持原T并核同attempt/epoch；无Agent、后台心跳线程或模型副作用。
# 函数用途: 在规范processing文件原子更新真实租约投影。
def heartbeat(case):
    report = refresh_processing_lease_report(case.request, request_id=case.request.stem,
        worker_id='component-worker', execution_attempt_id='component-attempt', lease_epoch=1)
    assert report.ok and report.load_error is None


@pytest.mark.parametrize('progress', ['heartbeat', 'new', 'terminal'])
def test_current_progress_preserves_old_closure_consumption(generation_case, monkeypatch, progress):
    case = generation_case
    canonical = case.paths.terminal / case.request.name
    response = {'id': case.request.stem, 'kind': 'ask', 'status': 'done', 'ok': True, 'response': 'synthetic complete'}

    def publish():
        if progress == 'heartbeat':
            heartbeat(case)
        elif progress == 'new':
            write_json_file_atomic(case.paths.processing / 'component-next.json',
                                   {'id': 'component-next', 'route': {'c_run_id': 'new-c'}})
        else:
            archived = terminalize_gateway_request_file(case.paths, case.request, case.paths.done,
                case.request.stem, terminal_response=response,
                expected_execution_attempt_id='component-attempt', expected_lease_epoch=1)
            assert archived == canonical and not case.request.exists()
            assert json.loads(canonical.read_bytes())['terminal_response'] == response

    result, snapshots = interleaved(case, monkeypatch, publish)
    assert result == (case.record, case.raw)
    assert len(snapshots) == 2 and snapshots[0] != snapshots[1]
    for actual in snapshots:
        history._require_closed_files(case.record['closure']['source_files'], actual, 'new-c')
    assert history.read_generation_metadata(case.paths.root, 'old-c') == (case.record, case.raw)
    if progress == 'terminal':
        assert not case.request.exists()
        assert json.loads(canonical.read_bytes())['terminal_response'] == response


def test_stable_sources_and_pre_snapshot_heartbeat_keep_original_contract(generation_case):
    case = generation_case
    assert consume(case) == (case.record, case.raw)
    heartbeat(case)
    assert consume(case) == (case.record, case.raw)


@pytest.mark.parametrize('fixed', ['expected', 'covered', 'committed'])
@pytest.mark.parametrize('fault', ['change', 'remove', 'missing_before', 'wrong_initial'])
def test_fixed_sources_cannot_be_reclassified_as_current(generation_case, monkeypatch, fixed, fault):
    case = generation_case
    reference = case.oldref
    if fixed != 'expected':
        reference = ('admission/reservations/covered.json' if fixed == 'covered' else
                     'control_operations/gwctl-msg-' + 'c' * 32 + '.json')
        raw = b'{"route":{"c_run_id":"new-c"},"value":1}'
        write_bytes_atomic_beneath(case.paths.root, tuple(reference.split('/')), raw)
        if fixed == 'covered':
            case.covered = ((reference, hashlib.sha256(raw).hexdigest()),)
        else:
            case.committed = ((reference, raw),)
    path = case.paths.root.joinpath(*reference.split('/'))

    def change():
        if fault == 'change':
            write_bytes_atomic_beneath(case.paths.root, tuple(reference.split('/')),
                                      b'{"route":{"c_run_id":"new-c"},"value":2}')
        else:
            path.unlink()

    if fault in {'missing_before', 'wrong_initial'}:
        if fault == 'missing_before':
            path.unlink()
        else:
            write_bytes_atomic_beneath(case.paths.root, tuple(reference.split('/')),
                                      b'{"route":{"c_run_id":"new-c"},"value":2}')
        reason = 'generation_closed_source_changed' if fixed == 'expected' else 'generation_closure_sources_changed'
        with pytest.raises(ValueError, match='^' + reason + '$'):
            consume(case)
    else:
        with pytest.raises(ValueError, match='^generation_closure_sources_changed$'):
            interleaved(case, monkeypatch, change)


@pytest.mark.parametrize('fault', ['current_becomes_old', 'new_old', 'old_removed'])
def test_noncurrent_delta_never_borrows_current_permission(generation_case, monkeypatch, fault):
    case = generation_case
    if fault == 'old_removed':
        write_json_file_atomic(case.request, {'route': {'c_run_id': 'unknown-old-c'}})
        with pytest.raises(ValueError, match='^generation_closed_source_added$'):
            consume(case)
        return

    def publish():
        path = case.request if fault == 'current_becomes_old' else case.paths.processing / 'unknown-old.json'
        write_json_file_atomic(path, {'route': {'c_run_id': 'unknown-old-c'}})

    with pytest.raises(ValueError, match='^generation_closed_source_added$'):
        interleaved(case, monkeypatch, publish)


@pytest.mark.parametrize('fault', ['history', 'evidence'])
def test_old_history_and_tree_evidence_remain_immutable(generation_case, monkeypatch, fault):
    case = generation_case

    def change():
        parts = history.generation_metadata_parts('old-c') if fault == 'history' else tuple(case.eref.split('/'))
        raw = case.raw + b' ' if fault == 'history' else b'{"changed":true}'
        write_bytes_atomic_beneath(case.paths.root, parts, raw)

    reason = 'generation_closure_sources_changed' if fault == 'history' else 'generation_tree_evidence_changed'
    with pytest.raises(ValueError, match='^' + reason + '$'):
        interleaved(case, monkeypatch, change)


@pytest.mark.parametrize('liveness', [True, None])
def test_old_identity_requires_confirmed_absence(generation_case, monkeypatch, liveness):
    monkeypatch.setattr(daemon_metadata, 'process_identity_is_live', lambda _identity: liveness)
    with pytest.raises(ValueError, match='^old_role_lifecycle_unproven$'):
        consume(generation_case)


@pytest.mark.parametrize('conflict', ['expected_covered', 'expected_committed', 'covered_committed'])
def test_fixed_source_intersections_cannot_override_original_binding(generation_case, conflict):
    case = generation_case
    if conflict == 'expected_covered':
        case.covered = ((case.oldref, '0' * 64),)
    elif conflict == 'expected_committed':
        case.committed = ((case.oldref, b'{"different":true}'),)
    else:
        ref = 'control_operations/gwctl-msg-' + 'd' * 32 + '.json'
        raw = b'{"route":{"c_run_id":"new-c"},"value":1}'
        write_bytes_atomic_beneath(case.paths.root, tuple(ref.split('/')), raw)
        case.covered = ((ref, hashlib.sha256(raw).hexdigest()),)
        case.committed = ((ref, b'{"route":{"c_run_id":"new-c"},"value":2}'),)
    with pytest.raises(ValueError, match='^generation_closure_sources_changed$'):
        consume(case)
