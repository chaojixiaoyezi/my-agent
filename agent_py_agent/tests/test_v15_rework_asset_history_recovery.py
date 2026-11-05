"""R5：真实工具回滚、提交后单次 I/O 错误与冷实例恢复。

审计归档未附 probes/test_asset_history_regressions.py；按任务规格重建三个断言。
基线必须在绑定相等和 B 历史存在断言处失败，而非配置或 CAS 检查失败。
"""
import ast
import json
import os
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from threading import Event, Lock, Thread

import pytest

from agent_py_agent.agent.assets import revision_history as history
from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.workflow_store import (
    WorkflowDefinition,
    WorkflowStepSpec,
    WorkflowStore,
)
from agent_py_agent.agent.assets.workspace_assets import commit_asset_batch, promote_to_owner
from agent_py_agent.agent.tooling.scripts_tool import ScriptsTool
from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool
from agent_py_agent.agent.workflows.pinned_definitions import resolve_definition


def call(tool, params):
    result = tool.execute(params)
    assert result.ok, result.output
    return json.loads(result.output)


def script_descriptor(**kwargs):
    return AssetDescriptor('demo', 'script', 'demo', **kwargs)


def definition(body='A'):
    return WorkflowDefinition(AssetDescriptor('flow', 'workflow', 'flow'), (
        WorkflowStepSpec('fetch', 'fetch', 'deterministic_script', script_id='demo'),), body)


def fail_blob_once(monkeypatch, code):
    original = history._write_bytes
    calls = []

    def write(home, parts, data):
        if data == code and not calls:
            calls.append(data)
            raise OSError('simulated I/O error')
        return original(home, parts, data)

    monkeypatch.setattr(history, '_write_bytes', write)
    return calls


def test_workflow_rollback_preserves_target_dependency_pins(tmp_path):
    scripts, workflows = ScriptStore(tmp_path), WorkflowStore(tmp_path)
    scripts.save_script(script_descriptor(), 'print(1)\n')
    workflows.save_workflow(definition())
    target = history.get_version(tmp_path, 'workflow', 'flow', 1)
    path = tmp_path / 'data/asset_evolution/versions/workflow/flow/v1.json'
    original_bytes = path.read_bytes()
    scripts.save_script(script_descriptor(), 'print(2)\n')
    workflows.save_workflow(definition('B'))
    data = call(WorkflowsTool(tmp_path), {'action': 'rollback', 'workflow_id': 'flow',
        'to_version': 1, 'expected_revision': 2})
    assert data['version_recording']['recorded'] is True
    restored = history.latest_version(tmp_path, 'workflow', 'flow')
    assert restored['version'] == 3 and restored['restored_from'] == 1
    assert path.read_bytes() == original_bytes
    assert restored['pins'] == target['pins']
    assert resolve_definition(tmp_path, 'flow', 3)[1] == resolve_definition(tmp_path, 'flow', 1)[1]


@pytest.mark.parametrize('operation', ['save', 'delete'])
def test_single_history_io_failure_retains_committed_preimage(tmp_path, monkeypatch, operation):
    store = ScriptStore(tmp_path)
    store.save_script(script_descriptor(), 'print("A")\n')
    target = history.get_version(tmp_path, 'script', 'demo', 1)
    code = b'print("B")\n'
    faults = fail_blob_once(monkeypatch, code)
    data = call(ScriptsTool(tmp_path), {'action': 'update', 'script_id': 'demo',
        'code': code.decode(), 'expected_revision': 1})
    assert data['version_recording']['recorded'] is False
    assert ScriptStore(tmp_path).get_script('demo')['code'] == code.decode()
    assert len(faults) == 1
    fresh = ScriptsTool(tmp_path)
    if operation == 'save':
        call(fresh, {'action': 'update', 'script_id': 'demo', 'code': 'print("C")\n', 'expected_revision': 2})
    else:
        call(fresh, {'action': 'delete', 'script_id': 'demo'})
    assert len(faults) == 1
    assert history.get_version(tmp_path, 'script', 'demo', 1) == target
    recovered = history.find_version(tmp_path, 'script', 'demo', code_sha256=sha256(code).hexdigest())
    assert recovered is not None, '已提交 B 在后续健康操作后丢失'
    assert history.blob_get(tmp_path, recovered['code_sha256']) == code
    expected = [3, 2, 1] if operation == 'save' else [2, 1]
    assert [row['version'] for row in history.list_versions(tmp_path, 'script', 'demo')] == expected
    assert history.pending_publication(tmp_path, 'script', 'demo') is None


def batch_script(code, **options):
    return {'descriptor': script_descriptor().to_dict(), 'code': code, **options}


def batch_workflow(body):
    value = definition(body)
    return {'descriptor': value.descriptor.to_dict(), 'steps': [step.to_dict() for step in value.steps],
            'body_markdown': body}


@pytest.mark.parametrize('operation', ['save', 'delete'])
def test_workflow_side_and_pins_survive_dependency_change_before_recovery(tmp_path, monkeypatch, operation):
    scripts, workflows = ScriptStore(tmp_path), WorkflowStore(tmp_path)
    scripts.save_script(script_descriptor(), 'print(1)\n')
    workflows.save_workflow(definition('A'))
    first = history.get_version(tmp_path, 'workflow', 'flow', 1)
    body = 'B：准确的侧文件\n'
    faults = fail_blob_once(monkeypatch, body.encode())
    changed = definition(body)
    data = call(WorkflowsTool(tmp_path), {'action': 'save', 'workflow_id': 'flow',
        'definition': changed.to_dict(), 'expected_revision': 1})
    assert data['version_recording']['recorded'] is False and len(faults) == 1
    debt = history.pending_publication(tmp_path, 'workflow', 'flow')
    assert debt['phase'] == 'committed' and debt['snapshot']['side_files'] == {'WORKFLOW.md': body}
    scripts.save_script(script_descriptor(), 'print(2)\n')
    assert WorkflowStore(tmp_path).get_workflow('flow').body_markdown == body
    if operation == 'delete':
        call(WorkflowsTool(tmp_path), {'action': 'delete', 'workflow_id': 'flow'})
    else:
        call(WorkflowsTool(tmp_path), {'action': 'save', 'workflow_id': 'flow',
            'definition': definition('C').to_dict(), 'expected_revision': 2})
    recovered = history.get_version(tmp_path, 'workflow', 'flow', 2)
    assert history.blob_get(tmp_path, recovered['side_blobs']['WORKFLOW.md']) == body.encode()
    assert recovered['pins'] == first['pins']
    assert resolve_definition(tmp_path, 'flow', 2)[1] == resolve_definition(tmp_path, 'flow', 1)[1]
    assert len(faults) == 1 and history.pending_publication(tmp_path, 'workflow', 'flow') is None


def test_owner_batch_retains_commit_decision_and_recovers_scripts_before_workflows(tmp_path, monkeypatch):
    commit_asset_batch([batch_script('print(1)\n'), batch_workflow('A')], owner_home=tmp_path)
    code = b'print(2)\n'
    faults = fail_blob_once(monkeypatch, code)
    data = commit_asset_batch([batch_workflow('B'), batch_script(code.decode())], owner_home=tmp_path)
    assert data['committed'] is True and len(faults) == 1
    journal = json.loads((tmp_path / '.asset-batch.json').read_text())
    assert journal['committed'] is True
    assert [item['kind'] for item in journal['asset_history']] == ['script', 'workflow']
    assert (tmp_path / 'scripts/demo/main.py').read_bytes() == code
    assert history.latest_version(tmp_path, 'script', 'demo')['version'] == 1
    # 新Store首次普通写会先收口旧batch；不得把B workflow改钉到下一次的script C。
    ScriptStore(tmp_path).save_script(script_descriptor(), 'print(3)\n', expected_revision=2)
    recovered = history.get_version(tmp_path, 'workflow', 'flow', 2)
    assert recovered['pins']['scripts']['demo']['version'] == 2
    assert recovered['pins']['scripts']['demo']['code_sha256'] == sha256(code).hexdigest()
    assert history.blob_get(tmp_path, recovered['side_blobs']['WORKFLOW.md']) == b'B'
    WorkflowStore(tmp_path).save_workflow(definition('C'), expected_revision=2)
    assert [row['version'] for row in history.list_versions(tmp_path, 'workflow', 'flow')] == [3, 2, 1]
    assert not (tmp_path / '.asset-batch.json').exists() and len(faults) == 1


def test_promotion_inherits_preimage_retention(tmp_path, monkeypatch):
    home, workspace = tmp_path / 'home', tmp_path / 'project'
    workspace.mkdir()
    ScriptStore(home).save_script(script_descriptor(), 'print(1)\n')
    ScriptStore(home, workspace_root=workspace).save_script(script_descriptor(), 'print(2)\n', target_scope='workspace')
    faults = fail_blob_once(monkeypatch, b'print(2)\n')
    assert promote_to_owner('script', 'demo', workspace_root=workspace, owner_home=home,
                            expected_revision=1)['committed'] is True
    assert not (workspace / '.agent/data/asset_evolution').exists()
    assert ScriptStore(home).delete_script('demo') is True
    recovered = history.find_version(home, 'script', 'demo', code_sha256=sha256(b'print(2)\n').hexdigest())
    assert recovered is not None and len(faults) == 1


def test_same_code_different_contract_is_not_rebound_on_restore(tmp_path):
    scripts, workflows = ScriptStore(tmp_path), WorkflowStore(tmp_path)
    scripts.save_script(script_descriptor(), 'print(1)\n', timeout_seconds=10)
    workflows.save_workflow(definition('A'))
    target = history.latest_version(tmp_path, 'workflow', 'flow')
    scripts.save_script(script_descriptor(), 'print(1)\n', timeout_seconds=20)
    workflows.save_workflow(definition('B'))
    current = history.latest_version(tmp_path, 'workflow', 'flow')
    old_pin, new_pin = target['pins']['scripts']['demo'], current['pins']['scripts']['demo']
    assert old_pin['code_sha256'] == new_pin['code_sha256']
    assert old_pin['execution_definition_ref'] != new_pin['execution_definition_ref']
    call(WorkflowsTool(tmp_path), {'action': 'rollback', 'workflow_id': 'flow',
        'to_version': 1, 'expected_revision': 2})
    assert history.latest_version(tmp_path, 'workflow', 'flow')['pins'] == target['pins']
    assert resolve_definition(tmp_path, 'flow', 3)[1] == resolve_definition(tmp_path, 'flow', 1)[1]


@pytest.mark.parametrize('denial', ['stale_cas', 'target', 'payload', 'side', 'script_code', 'script_payload'])
def test_restore_denial_does_not_change_canonical_or_history(tmp_path, denial):
    ScriptStore(tmp_path).save_script(script_descriptor(), 'print(1)\n')
    store = WorkflowStore(tmp_path)
    store.save_workflow(definition('A'))
    target = history.latest_version(tmp_path, 'workflow', 'flow')
    store.save_workflow(definition('B'))
    before = (tmp_path / 'workflows/flow/workflow.json').read_bytes()
    if denial not in ('stale_cas', 'target'):
        script = history.get_version(tmp_path, 'script', 'demo', 1)
        digest = {'payload': target['payload_blob'], 'side': target['side_blobs']['WORKFLOW.md'],
                  'script_code': script['code_sha256'], 'script_payload': script['payload_blob']}[denial]
        (tmp_path / 'data/asset_evolution/blobs' / digest[:2] / digest).unlink()
    result = WorkflowsTool(tmp_path).execute({'action': 'rollback', 'workflow_id': 'flow',
        'to_version': 99 if denial == 'target' else 1, 'expected_revision': 1 if denial == 'stale_cas' else 2})
    assert not result.ok, result.output
    assert result.effect_outcome != 'confirmed'
    assert (tmp_path / 'workflows/flow/workflow.json').read_bytes() == before
    assert history.latest_version(tmp_path, 'workflow', 'flow')['version'] == 2


def test_model_metadata_cannot_override_pins(tmp_path):
    scripts, workflows = ScriptStore(tmp_path), WorkflowStore(tmp_path)
    scripts.save_script(script_descriptor(), 'print(1)\n')
    workflows.save_workflow(definition('A'))
    target = history.latest_version(tmp_path, 'workflow', 'flow')
    scripts.save_script(script_descriptor(), 'print(2)\n')
    value = definition('B')
    value = replace(value, descriptor=replace(value.descriptor, metadata={
        'pins': target['pins'], 'restoration': target, 'restored_from': 1}))
    call(WorkflowsTool(tmp_path), {'action': 'save', 'workflow_id': 'flow',
        'definition': value.to_dict(), 'expected_revision': 1})
    record = history.latest_version(tmp_path, 'workflow', 'flow')
    assert record['pins']['scripts']['demo']['version'] == 2 and record['restored_from'] is None


@contextmanager
def no_fd_growth():
    directory = '/dev/fd' if os.path.isdir('/dev/fd') else '/proc/self/fd'
    before = len(os.listdir(directory))
    try:
        yield
    finally:
        fd_delta = len(os.listdir(directory)) - before
        assert fd_delta == 0


@pytest.mark.parametrize('outcome', ['success', 'failure', 'cancellation'])
def test_precommit_outcomes_never_publish_candidate_or_leak_fds(tmp_path, monkeypatch, outcome):
    store = ScriptStore(tmp_path)
    store.save_script(script_descriptor(), 'print(1)\n')
    original = store._put
    rejected = []

    def put(asset_id, name, content):
        if name == 'main.py' and content == 'print(2)\n' and outcome != 'success':
            rejected.append(content)
            raise KeyboardInterrupt() if outcome == 'cancellation' else OSError('simulated I/O error')
        return original(asset_id, name, content)

    monkeypatch.setattr(store, '_put', put)
    with no_fd_growth():
        if outcome == 'success':
            store.save_script(script_descriptor(), 'print(2)\n')
        else:
            with pytest.raises(KeyboardInterrupt if outcome == 'cancellation' else OSError):
                store.save_script(script_descriptor(), 'print(2)\n')
            assert rejected == ['print(2)\n']
            assert ScriptStore(tmp_path).get_script('demo')['code'] == 'print(1)\n'
            assert history.latest_version(tmp_path, 'script', 'demo')['version'] == 1
        ScriptStore(tmp_path).save_script(script_descriptor(), 'print(3)\n')
        assert ScriptStore(tmp_path).get_script('demo')['code'] == 'print(3)\n'
        if outcome != 'success':
            assert history.find_version(tmp_path, 'script', 'demo', code_sha256=sha256(b'print(2)\n').hexdigest()) is None
            assert [row['version'] for row in history.list_versions(tmp_path, 'script', 'demo')] == [2, 1]


def test_interrupted_postcommit_callback_recovers_prepared_proof(tmp_path, monkeypatch):
    from agent_py_agent.agent.assets import version_hook

    store = ScriptStore(tmp_path)
    store.save_script(script_descriptor(), 'print(1)\n')
    original = version_hook.record_committed

    def interrupt(*args):
        raise KeyboardInterrupt()

    with no_fd_growth():
        monkeypatch.setattr(version_hook, 'record_committed', interrupt)
        with pytest.raises(KeyboardInterrupt):
            store.save_script(script_descriptor(), 'print(2)\n')
        assert ScriptStore(tmp_path).get_script('demo')['code'] == 'print(2)\n'
        assert history.pending_publication(tmp_path, 'script', 'demo')['phase'] == 'prepared'
        monkeypatch.setattr(version_hook, 'record_committed', original)
        ScriptStore(tmp_path).delete_script('demo')
        assert history.blob_get(tmp_path, history.get_version(tmp_path, 'script', 'demo', 2)['code_sha256']) == b'print(2)\n'


def test_ack_error_retains_proof_and_retry_does_not_duplicate_version(tmp_path, monkeypatch):
    ScriptStore(tmp_path).save_script(script_descriptor(), 'print(1)\n')
    original = history.acknowledge_publication
    faults = []

    def ack(home, kind, asset_id):
        if not faults:
            faults.append(asset_id)
            raise OSError('simulated acknowledgment I/O error')
        return original(home, kind, asset_id)

    monkeypatch.setattr(history, 'acknowledge_publication', ack)
    data = call(ScriptsTool(tmp_path), {'action': 'update', 'script_id': 'demo', 'code': 'print(2)\n'})
    assert data['version_recording']['recorded'] is False and faults == ['demo']
    assert history.latest_version(tmp_path, 'script', 'demo')['version'] == 2
    assert history.pending_publication(tmp_path, 'script', 'demo')['phase'] == 'committed'
    call(ScriptsTool(tmp_path), {'action': 'update', 'script_id': 'demo', 'code': 'print(3)\n'})
    assert [row['version'] for row in history.list_versions(tmp_path, 'script', 'demo')] == [3, 2, 1]
    assert history.pending_publication(tmp_path, 'script', 'demo') is None


def test_incomplete_pin_evidence_is_not_cleared_or_replaced_by_later_script(tmp_path):
    # 原有非致命旁账语义仍在；没有可靠依赖身份时禁止删掉唯一正文或猜将来的pin。
    scripts = ScriptStore(tmp_path)
    scripts.save_script(script_descriptor(), 'print(1)\n')
    value = definition('incomplete')
    value = replace(value, steps=(replace(value.steps[0], script_id='missing'),))
    data = call(WorkflowsTool(tmp_path), {'action': 'save', 'workflow_id': 'flow', 'definition': value.to_dict()})
    assert data['version_recording']['recorded'] is False
    proof = history.pending_publication(tmp_path, 'workflow', 'flow')
    assert proof['snapshot']['error_code']
    scripts.save_script(AssetDescriptor('missing', 'script', 'later'), 'print(2)\n')
    result = WorkflowsTool(tmp_path).execute({'action': 'delete', 'workflow_id': 'flow'})
    assert result.ok
    assert WorkflowStore(tmp_path).get_workflow('flow') is None
    assert history.pending_publication(tmp_path, 'workflow', 'flow') is None
    retained = list((tmp_path / 'data/asset_evolution/pending/workflow').glob('flow.unpublishable.*.json'))
    assert len(retained) == 1
    assert json.loads(retained[0].read_text())['publication'] == proof
    assert history.latest_version(tmp_path, 'workflow', 'flow') is None


@pytest.mark.parametrize('committed', [False, True])
def test_batch_interruption_recovers_only_the_committed_snapshot(tmp_path, monkeypatch, committed):
    from agent_py_agent.agent.assets import workspace_assets as batch

    commit_asset_batch([batch_script('print(1)\n')], owner_home=tmp_path)
    original = batch._finish_batch if committed else batch._put_text

    def finish(root, journal, *, admission=None):
        raise KeyboardInterrupt()

    def put(root, parts, content):
        if tuple(parts) == ('scripts', 'demo', 'main.py') and content == 'print(2)\n':
            raise KeyboardInterrupt()
        return original(root, parts, content)

    monkeypatch.setattr(batch, '_finish_batch' if committed else '_put_text', finish if committed else put)
    with no_fd_growth():
        with pytest.raises(KeyboardInterrupt):
            commit_asset_batch([batch_script('print(2)\n')], owner_home=tmp_path)
        if committed:
            journal = json.loads((tmp_path / '.asset-batch.json').read_text())
            assert journal['committed'] is True
        monkeypatch.setattr(batch, '_finish_batch' if committed else '_put_text', original)
        ScriptStore(tmp_path).save_script(script_descriptor(), 'print(3)\n')
        found = history.find_version(tmp_path, 'script', 'demo', code_sha256=sha256(b'print(2)\n').hexdigest())
        assert (found is not None) is committed
        assert ScriptStore(tmp_path).get_script('demo')['code'] == 'print(3)\n'
        assert not (tmp_path / '.asset-batch.json').exists()


def test_structural_guard_protects_preimage_and_keeps_history_inside_batch_ack():
    """语义结构守卫：销毁前保护、提交后通知、批量ACK前保留日志、回滚记录不能丢弃。"""
    import inspect
    import textwrap

    from agent_py_agent.agent.assets import _AssetStore, workspace_assets

    def syntax(function):
        return ast.parse(textwrap.dedent(inspect.getsource(function))).body[0]

    def calls(node, name):
        return sorted((item for item in ast.walk(node) if isinstance(item, ast.Call)
            and (getattr(item.func, 'id', None) == name or getattr(item.func, 'attr', None) == name)),
            key=lambda item: (item.lineno, item.col_offset))

    write = syntax(_AssetStore._write)
    assert calls(write, 'protect_preimage')[0].lineno < calls(write, 'prepare_commit')[0].lineno < calls(write, '_put')[0].lineno
    assert calls(write, '_put')[-1].lineno < calls(write, '_history_notify')[0].lineno
    delete = syntax(_AssetStore._delete)
    assert calls(delete, 'protect_preimage')[0].lineno < calls(delete, '_put')[0].lineno
    finish = syntax(workspace_assets._finish_batch)
    assert calls(finish, '_history_notify')[0].lineno < calls(finish, '_clean_stages')[0].lineno < calls(finish, 'unlink_file_beneath')[0].lineno
    dispatch = syntax(WorkflowsTool._dispatch)
    restore = next(node for node in ast.walk(dispatch) if isinstance(node, ast.With)
                   and any(calls(item.context_expr, 'restoring_snapshot') for item in node.items))
    assert calls(restore, '_save_definition')
    binding = next(node for node in ast.walk(dispatch) if isinstance(node, ast.Assign)
                   and isinstance(node.value, ast.Call) and getattr(node.value.func, 'id', '') == 'load_snapshot')
    selected = binding.targets[0].elts[0]
    assert isinstance(selected, ast.Name) and selected.id != '_'
    assert calls(restore, 'restoring_snapshot')[0].args[1].id == selected.id


def test_real_workflow_execution_survives_restore_publication_retry(tmp_path, monkeypatch):
    """真实Tool -> Store -> Engine -> ScriptRunner -> 子进程；不替换配置或进程执行。"""
    from agent_py_agent.agent.workflows import WorkflowEngine, script_runner

    scripts, workflows = ScriptStore(tmp_path), WorkflowStore(tmp_path)
    scripts.save_script(script_descriptor(), 'print(\'{"version":1}\')\n')
    workflows.save_workflow(definition('A'))
    target = history.latest_version(tmp_path, 'workflow', 'flow')
    invocations = []
    execute = script_runner._execute

    def observed(*args, **kwargs):
        invocations.append(Path(args[1]))
        return execute(*args, **kwargs)

    monkeypatch.setattr(script_runner, '_execute', observed)
    engine = WorkflowEngine(tmp_path)
    first = call(WorkflowsTool(tmp_path, engine=engine), {'action': 'run', 'workflow_id': 'flow', 'version': 1})
    assert first['status'] == 'succeeded' and first['final_output'] == {'version': 1}
    scripts.save_script(script_descriptor(), 'print(\'{"version":2}\')\n')
    workflows.save_workflow(definition('B'))
    second = call(WorkflowsTool(tmp_path, engine=engine), {'action': 'run', 'workflow_id': 'flow', 'version': 2})
    assert second['status'] == 'succeeded' and second['final_output'] == {'version': 2}
    writer, faults = history._write_json, []

    def write(home, parts, value):
        if tuple(parts[-3:]) == ('workflow', 'flow', 'v3.json') and not faults:
            faults.append('restore')
            raise OSError('simulated I/O error')
        return writer(home, parts, value)

    monkeypatch.setattr(history, '_write_json', write)
    restored = call(WorkflowsTool(tmp_path), {'action': 'rollback', 'workflow_id': 'flow',
        'to_version': 1, 'expected_revision': 2})
    assert restored['version_recording']['recorded'] is False and faults == ['restore']
    scripts.save_script(script_descriptor(), 'print(\'{"version":3}\')\n')
    call(WorkflowsTool(tmp_path), {'action': 'save', 'workflow_id': 'flow',
        'definition': definition('A').to_dict(), 'expected_revision': 3})
    assert history.latest_version(tmp_path, 'workflow', 'flow')['pins'] == target['pins']
    fresh_engine = WorkflowEngine(tmp_path)
    third = call(WorkflowsTool(tmp_path, engine=fresh_engine), {'action': 'run', 'workflow_id': 'flow', 'version': 3})
    assert third['status'] == 'succeeded' and third['final_output'] == {'version': 1}
    assert len(invocations) == 3
    for run in (first, second, third):
        persistent = fresh_engine.store.load_run(run['workflow_run_id'])
        assert persistent.status == 'succeeded'
        assert persistent.definition_version == run['definition_version']
    scripts.set_enabled('demo', False)
    denied = WorkflowsTool(tmp_path, engine=fresh_engine).execute({'action': 'run', 'workflow_id': 'flow', 'version': 3})
    assert not denied.ok and len(invocations) == 3


def test_monitor_composition_override_acknowledges_the_shared_snapshot(tmp_path):
    from agent_py_agent.agent.assets.monitor_composition import save_monitor_composition

    ScriptStore(tmp_path).save_script(script_descriptor(), 'print(1)\n')
    WorkflowStore(tmp_path).save_workflow(definition('base'))
    result = save_monitor_composition(tmp_path, 'watch', 'flow', 1, [])
    record = history.get_version(tmp_path, 'workflow', result['workflow_id'], result['version'])
    assert record['pins']['scripts']['demo']['version'] == 1
    assert history.pending_publication(tmp_path, 'workflow', result['workflow_id']) is None


def test_same_asset_writers_serialize_and_stale_cas_cannot_overwrite(tmp_path, monkeypatch):
    from agent_py_agent.agent.assets import version_hook

    ScriptStore(tmp_path).save_script(script_descriptor(), 'print(1)\n')
    entered, release, attempted = Event(), Event(), Event()
    guard, observed = Lock(), {'active': 0, 'peak': 0, 'calls': 0}
    original = version_hook._publish
    results, errors = [], []

    def publish(store, pending):
        with guard:
            observed['active'] += 1
            observed['calls'] += 1
            observed['peak'] = max(observed['peak'], observed['active'])
        try:
            entered.set()
            assert release.wait(10), 'bounded release timed out'
            return original(store, pending)
        finally:
            with guard:
                observed['active'] -= 1

    def save(code, *, second=False):
        if second:
            attempted.set()
        try:
            results.append(ScriptStore(tmp_path).save_script(script_descriptor(), code, expected_revision=1))
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(version_hook, '_publish', publish)
    first = Thread(target=save, args=('print(2)\n',))
    second = Thread(target=save, args=('print(3)\n',), kwargs={'second': True})
    with no_fd_growth():
        first.start()
        try:
            assert entered.wait(10)
            second.start()
            assert attempted.wait(10)
            assert observed['active'] == 1
        finally:
            release.set()
            first.join(10)
            if second.ident is not None:
                second.join(10)
        assert not first.is_alive() and not second.is_alive()
        assert observed == {'active': 0, 'peak': 1, 'calls': 1}
        assert len(results) == len(errors) == 1
        assert isinstance(errors[0], ValueError) and str(errors[0]).startswith('ASSET_REVISION_CONFLICT:')
        assert ScriptStore(tmp_path).get_script('demo')['code'] == 'print(2)\n'
        assert [row['version'] for row in history.list_versions(tmp_path, 'script', 'demo')] == [2, 1]
