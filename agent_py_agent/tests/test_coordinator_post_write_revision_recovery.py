"""真实原C写后revision确认失败：不丢责任、不重捕来源、不扩大写权限。"""
import ast
import errno
import inspect
import textwrap

import pytest

from agent_py_agent.agent.gateway_parts import multiprocess_admission as admission
from agent_py_agent.agent.gateway_parts import multiprocess_executor_active_index as active
from agent_py_agent.agent.gateway_parts.multiprocess_executor_runtime import _reservation
from agent_py_agent.agent.gateway_parts.multiprocess_route import owner_digest
from agent_py_agent.agent.gateway_parts.request_worker import AdmissionLimits
from agent_py_agent.tests.test_gateway_claim_transition_scope import c_actual as c_actual
from agent_py_agent.tests.test_gateway_prepared_route_immutability import context as context
from agent_py_agent.tests.test_rework_v14_admission_io_recovery import fd_count, ordinary_case
from agent_py_agent.tests.test_scale_native_executor_retirement import original_c_source


def fail_post_write_revision_once(index, monkeypatch):
    """只替换一次元数据读依赖；规范写者、原锁、holder及CAS全部保持真实。"""
    original = index._attempt_revisions
    error = OSError(errno.EIO, 'one post-write revision read failure')
    calls = []

    def read(attempt):
        assert index.controller._mutex._is_owned()
        assert index.lease._lifecycle_gate._is_owned()
        calls.append(attempt['kind'])
        if len(calls) == 1:
            # 故障一定发生在原正式写已成功之后，不能以写前失败充当该反例。
            assert index._attempt_current(attempt) == attempt['row']
            raise error
        return original(attempt)

    monkeypatch.setattr(index, '_attempt_revisions', read)
    return error, calls


@pytest.mark.parametrize('reused', [False, True])
def test_coordinator_p3_next_write_recovers_after_one_post_write_revision_read_error(
    context, monkeypatch, reused
):
    gate, path, ownership = ordinary_case(context)
    controller = context.controller()
    index = active.index_for(controller)
    limits = AdmissionLimits(8, 8)
    if reused:
        def interrupted_publication(*_args):
            raise OSError(errno.EIO, 'prepare existing P3 without stamped route')

        with monkeypatch.context() as patch:
            patch.setattr(gate, '_publish_stamped', interrupted_publication)
            with pytest.raises(OSError):
                gate.admit(path, ownership, limits)
        assert gate.occupied_snapshot()['total'] == 1

    before = fd_count()
    with monkeypatch.context() as patch:
        _error, calls = fail_post_write_revision_once(index, patch)
        result = gate.admit(path, ownership, limits)
    assert result == {'status': 'blocked', 'reason': 'reservation_write_failed'}
    assert 1 <= len(calls) <= 3 and set(calls) == {'p3'}
    committed = index._p3(path.stem)
    assert committed is not None and committed['state'] == 'occupied'
    assert controller._all_quarantined is False

    # 同一个gate/controller正常重试，必须实际完成原P3与路由发表，而不只检查缓存字段。
    retried = gate.admit(path, ownership, limits)
    assert retried['status'] == 'admitted', retried
    assert gate.occupied_snapshot()['total'] == 1
    assert index._p3(path.stem) == retried['reservation']
    assert not index._write_attempts and controller._all_quarantined is False
    assert gate.admit(path, ownership, limits)['reservation'] == retried['reservation']
    index.check_sources()
    assert fd_count() == before


@pytest.mark.parametrize('operation', ['reserve', 'advance'])
def test_coordinator_executor_next_write_recovers_after_one_post_write_revision_read_error(
    c_actual, monkeypatch, operation
):
    with original_c_source(c_actual) as (source, claim):
        index = active.index_for(source._sources[4])
        binding = {'owner_sha': owner_digest(source.owner), 'request_id': claim.request_id,
                   'execution_attempt_id': claim.execution_attempt_id,
                   'lease_epoch': claim.lease_epoch, 'tool_call_id': 'revision-first'}
        first = _reservation(source, binding, 'shell', 'revision-first')
        if operation == 'advance':
            source.reserve(first)
        before = fd_count()
        with monkeypatch.context() as patch:
            error, calls = fail_post_write_revision_once(index, patch)
            with pytest.raises(OSError) as raised:
                if operation == 'reserve':
                    source.reserve(first)
                else:
                    source.advance(first, 'abandoned')
        assert raised.value is error
        assert 1 <= len(calls) <= 3 and set(calls) == {'executor'}
        actual = source.read(first['reservation_id'])
        assert actual['state'] == ('reserved' if operation == 'reserve' else 'abandoned')

        # 不复用第一个reservation：同一原来源必须还能提交新的真实P1。
        second = _reservation(source, {**binding, 'tool_call_id': 'revision-second'},
                              'shell', 'revision-second')
        assert source.reserve(second) == second
        assert index.executors[active._executor_key(actual)]['row'] == actual
        assert index.executors[active._executor_key(actual)]['holder'] is source
        assert not index._write_attempts and index.controller._all_quarantined is False
        if operation == 'reserve':
            actual = source.advance(actual, 'abandoned')
        assert actual['state'] == 'abandoned'
        assert source.advance(second, 'abandoned')['state'] == 'abandoned'
        index.check_sources()
        assert fd_count() == before


@pytest.mark.parametrize('changed', ['namespace', 'canonical'])
def test_coordinator_post_write_read_error_does_not_accept_later_foreign_change(
    context, monkeypatch, changed
):
    gate, path, ownership = ordinary_case(context)
    index = active.index_for(context.controller())
    before = fd_count()
    with monkeypatch.context() as patch:
        _error, calls = fail_post_write_revision_once(index, patch)
        assert gate.admit(path, ownership, AdmissionLimits(8, 8))['status'] == 'blocked'
    assert 1 <= len(calls) <= 3 and set(calls) == {'p3'}
    committed = index._p3(path.stem)
    assert committed is not None and committed['state'] == 'occupied'
    if changed == 'namespace':
        (context.paths.root / 'admission' / 'reservations' / 'foreign-entry').write_text('foreign')
    else:
        # 同目录原位改写，不依赖目录mtime变化；合法JSON仍须完整canonical等值核验。
        import json

        filename = context.paths.root / 'admission' / 'reservations' / (path.stem + '.json')
        filename.write_text(json.dumps({**committed, 'request_id': 'foreign-request'}))
    called = []
    result = index.controller.launch_owner_writer(owner_digest(ownership[0]), lambda: called.append(True))
    assert result['status'] == 'blocked' and called == []
    assert index.controller._all_quarantined is True
    assert fd_count() == before


def test_coordinator_revision_fallback_guard_rejects_missing_snapshot_fixture():
    """静态守门和错误fixture共用检查器，不以删除运行断言换绿。"""
    def protected(source):
        tree = ast.parse(textwrap.dedent(source))
        scope = next(node for node in ast.walk(tree)
                     if isinstance(node, ast.FunctionDef) and node.name == '_write_scope')
        success = next(node.orelse for node in scope.body if isinstance(node, ast.Try))
        for number, node in enumerate(success):
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if (not isinstance(target, ast.Subscript) or not isinstance(target.value, ast.Name)
                    or target.value.id != 'attempt' or not isinstance(target.slice, ast.Constant)
                    or target.slice.value != 'revision' or not isinstance(node.value, ast.Call)
                    or not isinstance(node.value.func, ast.Name) or node.value.func.id != 'dict'
                    or len(node.value.args) != 1):
                continue
            original = node.value.args[0]
            if not (isinstance(original, ast.Attribute) and isinstance(original.value, ast.Name)
                    and original.value.id == 'self' and original.attr == '_revision'):
                continue
            later_nodes = [child for later in success[number + 1:] for child in ast.walk(later)]
            if any(isinstance(child, ast.Assign)
                   and any(ast.dump(other) == ast.dump(target) for other in child.targets)
                   for child in later_nodes):
                return False
            return any(isinstance(call, ast.Call)
                       and getattr(call.func, 'attr', '') == '_attempt_revisions'
                       for call in later_nodes)
        return False

    assert protected(inspect.getsource(active.ExecutorActiveIndex._write_scope))
    assert not protected('def _write_scope(self, key, attempt):\n'
                         ' try:\n  yield\n except BaseException:\n  raise\n else:\n'
                         '  attempt["revision"] = self._attempt_revisions(attempt)\n'
                         '  self._reconcile_write(key)\n')
    assert not protected('def _write_scope(self, key, attempt):\n'
                         ' try:\n  yield\n except BaseException:\n  raise\n else:\n'
                         '  attempt["revision"] = dict(self._revision)\n'
                         '  attempt["revision"] = self._attempt_revisions(attempt)\n'
                         '  self._reconcile_write(key)\n')


@pytest.mark.parametrize('window', ['sample', 'accept'])
def test_coordinator_recovery_does_not_rebaseline_foreign_append_inside_confirmation(
    context, monkeypatch, window
):
    gate, path, ownership = ordinary_case(context)
    index = active.index_for(context.controller())
    original = index._attempt_revisions
    foreign = context.paths.root / 'admission' / 'reservations' / 'foreign-inside-confirmation'
    calls, accepted = [], []
    error = OSError(errno.EIO, 'one read failure after the first post-write sample')

    def read(attempt):
        calls.append(attempt['kind'])
        if len(calls) == 1:
            assert index._attempt_current(attempt) == attempt['row']
            accepted.append(dict(index._revision))
            if window == 'sample':
                foreign.write_text('not part of the original accepted write')
        if len(calls) == 2:
            raise error
        actual = original(attempt)
        if window == 'accept' and len(calls) == 4:
            # 正确的最后一次比较仍见冻结值；在末接纳采样前才发生陌生变化。
            foreign.write_text('not part of the original accepted write')
        return actual

    before = fd_count()
    with monkeypatch.context() as patch:
        patch.setattr(index, '_attempt_revisions', read)
        try:
            result = gate.admit(path, ownership, AdmissionLimits(8, 8))
        except ValueError as refused:
            assert str(refused) == 'responsibility_namespace_changed'
            result = {'status': 'blocked'}
    assert result['status'] == 'blocked'
    called = []
    decision = index.controller.launch_owner_writer(owner_digest(ownership[0]), lambda: called.append(True))
    assert decision['status'] == 'blocked' and called == [], (decision, window)
    assert index._revision == accepted[0], '陌生变化不得被重新采样后纳入已接受版本'
    assert index._write_attempts and foreign.is_file()
    assert fd_count() == before
