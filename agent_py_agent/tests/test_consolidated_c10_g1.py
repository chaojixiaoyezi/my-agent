"""Wave 3 consolidated tests (test_consolidated_c10_g1.py).

Merged sources:
- test_v15_rework_asset_history_recovery.py: R5：真实工具回滚、提交后单次 I/O 错误与冷实例恢复。
- test_v15_rework_r1_evolution_identity.py: R1：公开工具、真实子进程、原冻结请求与学习 FIFO 的身份回归。
- test_v15_rework_r2_published_contracts.py: 发布身份的真实存储/工具/执行往返；外部模型与IM不属于本切片。
- test_v15_rework_wake_recovery.py: R6：单次迁移读盘失败后的同进程恢复，真实 Client/Gateway/Store/消费者。
- test_v15_w11_background_agent_quota.py: W11：额度沿canonical身份及跨进程门，重放不重新占槽。
- test_v15_w11_unassigned_agents.py: W11：真实null归属及身份/当前工作两个独立观察层。
- test_v15_w12_compact_latency.py: 验证摘要输出预算、缓存前缀和一次选区；模型调用只用本地替身。
"""

import errno
import hashlib
import json
import multiprocessing
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from threading import Event, Lock, Thread
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import (
    _finalization_service as finalization,
)
from agent_py_agent.agent.agent_core import (
    _tool_loop_service as tool_loop,
)
from agent_py_agent.agent.assets import revision_history as history
from agent_py_agent.agent.assets.datasource_store import DatasourceStore
from agent_py_agent.agent.assets.descriptor import (
    AccessTarget,
    AssetDescriptor,
    DataSourceSpec,
    IOContract,
)
from agent_py_agent.agent.assets.mutation_ledger import AssetMutationLedger, freeze_asset_mutations
from agent_py_agent.agent.assets.revision_history import get_version, latest_version, record_version
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.workflow_store import (
    WorkflowDefinition,
    WorkflowStepSpec,
    WorkflowStore,
)
from agent_py_agent.agent.assets.workspace_assets import commit_asset_batch, promote_to_owner
from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.backends.tool_protocol_adapter import anthropic_tool_choice
from agent_py_agent.agent.capability.asset_evolution import (
    AssetEvolutionService,
    evolution_family_fingerprint,
    find_evolution_family,
)
from agent_py_agent.agent.capability.asset_evolution_replay import evolution_descriptor
from agent_py_agent.agent.capability.asset_evolution_routes import (
    read_evolution_json,
    route_existing_script,
)
from agent_py_agent.agent.capability.skill_learning import SkillLearningService
from agent_py_agent.agent.capability.skill_learning_store import MAX_REQUEST_ATTEMPT_COUNT
from agent_py_agent.agent.conversation import (
    compact,
)
from agent_py_agent.agent.conversation import (
    compact_request_budget as budget,
)
from agent_py_agent.agent.conversation import (
    store_wake_publication as publication,
)
from agent_py_agent.agent.conversation.auxiliary_model_call import generate_auxiliary_model_response
from agent_py_agent.agent.conversation.background_agents import BackgroundAgentService
from agent_py_agent.agent.conversation.business_contracts import BackgroundAgentIdentity
from agent_py_agent.agent.conversation.compact_message_source import CompactMessageSource
from agent_py_agent.agent.conversation.compact_projection import ConversationCompactProjection
from agent_py_agent.agent.conversation.compact_provider_surface import (
    ConversationCompactProviderSurface,
)
from agent_py_agent.agent.conversation.host_actor import HostChannelActor
from agent_py_agent.agent.conversation.session_messaging import queue_owned_background_message
from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft, SessionTaskUpdate
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.prompting_parts.cache_layout import CacheStructuredPrompt
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.agent.tooling.runtime_contracts import ToolChoice
from agent_py_agent.agent.tooling.scripts_tool import ScriptsTool
from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool
from agent_py_agent.agent.workflows import datasource_runtime as runtime
from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.agent.workflows.pinned_definitions import (
    historical_pin_available,
    resolve_definition,
)
from agent_py_agent.agent.workflows.script_runner import ScriptRunner
from agent_py_agent.tests.test_business_v12_f3_agents import business_host as business_host
from agent_py_agent.tests.test_compact_cache_surface_fit import _capturing_backend, _compact_request
from agent_py_agent.tests.test_compact_calibrated_candidate_gate import _calibration
from agent_py_agent.tests.test_compact_capacity_facts import _transcript_case
from agent_py_agent.tests.test_compact_request_budget import _request, _request_between_budgets
from agent_py_agent.tests.test_mega_business_api_actions import native_http as native_http
from agent_py_agent.tests.test_skill_learning import _ctx, _output, _runtime
from agent_py_agent.tests.test_v14_r1_task_wake_bootstrap import (
    _fd_count,
    _legacy_root,
    _tool_dispatch,
    _w5_unknown_index,
)
from agent_py_agent.tests.test_v14_w5_background_wakes import (
    assign,
    finish_retirement,
    producer_service,
    runtime_case,
    task_wakes,
)
from agent_py_agent.tests.test_v14_w5_model_set_adoption import (
    business_wire as business_wire,
)
from agent_py_agent.tests.test_v14_w5_model_set_adoption import (
    completed,
)
from agent_py_agent.tests.test_v15_w4_tool_surface import call
from agent_py_agent.tests.test_v15_w6_harness_guardrails import CountingRunner, result

# ==========================================================================
# Source: agent_py_agent/tests/test_v15_rework_asset_history_recovery.py
# R5：真实工具回滚、提交后单次 I/O 错误与冷实例恢复。
#
# 审计归档未附 probes/test_asset_history_regressions.py；按任务规格重建三个断言。
# 基线必须在绑定相等和 B 历史存在断言处失败，而非配置或 CAS 检查失败。
# ==========================================================================

def _asset_history_call(tool, params):
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
    data = _asset_history_call(WorkflowsTool(tmp_path), {'action': 'rollback', 'workflow_id': 'flow',
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
    data = _asset_history_call(ScriptsTool(tmp_path), {'action': 'update', 'script_id': 'demo',
        'code': code.decode(), 'expected_revision': 1})
    assert data['version_recording']['recorded'] is False
    assert ScriptStore(tmp_path).get_script('demo')['code'] == code.decode()
    assert len(faults) == 1
    fresh = ScriptsTool(tmp_path)
    if operation == 'save':
        _asset_history_call(fresh, {'action': 'update', 'script_id': 'demo', 'code': 'print("C")\n', 'expected_revision': 2})
    else:
        _asset_history_call(fresh, {'action': 'delete', 'script_id': 'demo'})
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
    data = _asset_history_call(WorkflowsTool(tmp_path), {'action': 'save', 'workflow_id': 'flow',
        'definition': changed.to_dict(), 'expected_revision': 1})
    assert data['version_recording']['recorded'] is False and len(faults) == 1
    debt = history.pending_publication(tmp_path, 'workflow', 'flow')
    assert debt['phase'] == 'committed' and debt['snapshot']['side_files'] == {'WORKFLOW.md': body}
    scripts.save_script(script_descriptor(), 'print(2)\n')
    assert WorkflowStore(tmp_path).get_workflow('flow').body_markdown == body
    if operation == 'delete':
        _asset_history_call(WorkflowsTool(tmp_path), {'action': 'delete', 'workflow_id': 'flow'})
    else:
        _asset_history_call(WorkflowsTool(tmp_path), {'action': 'save', 'workflow_id': 'flow',
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
    _asset_history_call(WorkflowsTool(tmp_path), {'action': 'rollback', 'workflow_id': 'flow',
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
    _asset_history_call(WorkflowsTool(tmp_path), {'action': 'save', 'workflow_id': 'flow',
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
    data = _asset_history_call(ScriptsTool(tmp_path), {'action': 'update', 'script_id': 'demo', 'code': 'print(2)\n'})
    assert data['version_recording']['recorded'] is False and faults == ['demo']
    assert history.latest_version(tmp_path, 'script', 'demo')['version'] == 2
    assert history.pending_publication(tmp_path, 'script', 'demo')['phase'] == 'committed'
    _asset_history_call(ScriptsTool(tmp_path), {'action': 'update', 'script_id': 'demo', 'code': 'print(3)\n'})
    assert [row['version'] for row in history.list_versions(tmp_path, 'script', 'demo')] == [3, 2, 1]
    assert history.pending_publication(tmp_path, 'script', 'demo') is None


def test_incomplete_pin_evidence_is_not_cleared_or_replaced_by_later_script(tmp_path):
    # 原有非致命旁账语义仍在；没有可靠依赖身份时禁止删掉唯一正文或猜将来的pin。
    scripts = ScriptStore(tmp_path)
    scripts.save_script(script_descriptor(), 'print(1)\n')
    value = definition('incomplete')
    value = replace(value, steps=(replace(value.steps[0], script_id='missing'),))
    data = _asset_history_call(WorkflowsTool(tmp_path), {'action': 'save', 'workflow_id': 'flow', 'definition': value.to_dict()})
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
    first = _asset_history_call(WorkflowsTool(tmp_path, engine=engine), {'action': 'run', 'workflow_id': 'flow', 'version': 1})
    assert first['status'] == 'succeeded' and first['final_output'] == {'version': 1}
    scripts.save_script(script_descriptor(), 'print(\'{"version":2}\')\n')
    workflows.save_workflow(definition('B'))
    second = _asset_history_call(WorkflowsTool(tmp_path, engine=engine), {'action': 'run', 'workflow_id': 'flow', 'version': 2})
    assert second['status'] == 'succeeded' and second['final_output'] == {'version': 2}
    writer, faults = history._write_json, []

    def write(home, parts, value):
        if tuple(parts[-3:]) == ('workflow', 'flow', 'v3.json') and not faults:
            faults.append('restore')
            raise OSError('simulated I/O error')
        return writer(home, parts, value)

    monkeypatch.setattr(history, '_write_json', write)
    restored = _asset_history_call(WorkflowsTool(tmp_path), {'action': 'rollback', 'workflow_id': 'flow',
        'to_version': 1, 'expected_revision': 2})
    assert restored['version_recording']['recorded'] is False and faults == ['restore']
    scripts.save_script(script_descriptor(), 'print(\'{"version":3}\')\n')
    _asset_history_call(WorkflowsTool(tmp_path), {'action': 'save', 'workflow_id': 'flow',
        'definition': definition('A').to_dict(), 'expected_revision': 3})
    assert history.latest_version(tmp_path, 'workflow', 'flow')['pins'] == target['pins']
    fresh_engine = WorkflowEngine(tmp_path)
    third = _asset_history_call(WorkflowsTool(tmp_path, engine=fresh_engine), {'action': 'run', 'workflow_id': 'flow', 'version': 3})
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



# ==========================================================================
# Source: agent_py_agent/tests/test_v15_rework_r1_evolution_identity.py
# R1：公开工具、真实子进程、原冻结请求与学习 FIFO 的身份回归。
# ==========================================================================

_SOURCE = '''import json, os, sys
inputs = json.load(sys.stdin)
rows = inputs.get("rows", [1])
output = {"execution_pid": os.getpid(), "source_report": {
    "stage": "probe+fetch", "discovered_sources": ["local-source"],
    "sources": [{"source_id": "local-source", "status": "ok",
                 "hit_count": len(rows), "http_status": None, "error_code": ""}]}}
if not inputs.get("historical_fail", True):
    output["result"] = len(rows)
print(json.dumps(output))
'''
_CURRENT = _SOURCE.replace('"historical_fail", True', '"fail", False')


def _case(tmp_path):
    """只使用本地进程和 Python 根守卫，不宣称验证了原生 OS 沙箱。"""
    runtime = _runtime(tmp_path, _output('skip'))
    home, work = runtime.home.owner_home_dir, tmp_path / 'work'
    work.mkdir()
    DatasourceStore(home).save_datasource({
        'datasource_id': 'local-source', 'kind': 'custom', 'title': '本地输入样本'})
    runner = ScriptRunner(home, workspace_root=work, use_sandbox=False, restrict_to_roots=True)
    tool = ScriptsTool(home, workspace_root=work, runner=runner)
    service = SkillLearningService(runtime.store, runtime.service.settings, runtime.service.runtime)
    agent = SimpleNamespace(root=work, home_paths=runtime.home,
                            config=SimpleNamespace(enable_self_learning=True))
    return SimpleNamespace(home=home, work=work, tool=tool, agent=agent,
                           service=service, runtime=runtime, store=ScriptStore(home))


def _wire(case, arguments):
    """客户端解析公开工具实际序列化结果，不手造脚本回执。"""
    result = case.tool.execute(arguments)
    value = json.loads(result.output)
    assert value == result.result_envelope
    return result, value


def _observe(case, state, arguments, result, call_id, *, run_id='capture'):
    params = SimpleNamespace(live_archive_state=state, run_id=run_id,
                             attempt_id='attempt-' + run_id, in_use_method_ids=())
    call = SimpleNamespace(tool_name='scripts', arguments=arguments, call_id=call_id)
    record = SimpleNamespace(params=params, call=call, result=result)
    tool_loop._observe_turn_asset_mutations(case.agent, record, {})


def _register(case, code=_SOURCE, *, inputs=(), state=None, run_id='capture'):
    args = {'action': 'register', 'script_id': 'sample', 'code': code,
            'descriptor': {'name': '身份检查', 'access_targets': [], 'credential_refs': [],
                'data_sources': [{'source_id': 'local-source', 'system': 'custom',
                                  'query_scope': {'access': 'read'}}],
                'io_contract': {'inputs': list(inputs), 'outputs': ['result', 'execution_pid']}}}
    result, value = _wire(case, args)
    assert result.ok, value
    if state is not None:
        _observe(case, state, args, result, 'register', run_id=run_id)
    return value


def _promote(case):
    result = route_existing_script(case.home, 'sample', _CURRENT, maintenance_mode='autonomous')
    assert result['status'] == 'promoted', result
    return case.store.get_script('sample')


def _run(case, *, version=None, inputs=None, state=None, call_id='execute', run_id='capture'):
    args = {'action': 'run', 'script_id': 'sample', 'inputs': inputs or {}}
    if version is not None:
        args['version'] = version
    result, value = _wire(case, args)
    assert value.get('output_data', {}).get('execution_pid') != os.getpid()
    assert value.get('output_data', {}).get('execution_pid'), value
    assert value['execution_definition_ref']
    if state is not None:
        before = deepcopy(value)
        _observe(case, state, args, result, call_id, run_id=run_id)
        assert json.loads(result.output) == before
    return args, result, value


def _enqueue(case, run_id):
    ctx = _ctx(run_id, attempt_id='attempt-' + run_id, tool_rounds=2,
               tool_runtime_evidence={}, archive_tool_calls=[])
    finalization._freeze_turn_asset_mutations(case.agent, ctx)
    assert ctx.tool_runtime_evidence['asset_evolution_run_id'] == run_id
    assert case.service.enqueue_from_finalize(ctx) == 'queued'
    return read_evolution_json(case.home, ('data', 'asset_evolution', 'requests', run_id + '.json'))


def _trial(case):
    return read_evolution_json(case.home, ('data', 'asset_evolution', 'trials', 'sample.json'))


@pytest.mark.parametrize('historical_ok', [False, True])
def test_historical_wire_receipts_cannot_count_against_promoted_trial(tmp_path, historical_ok):
    case, state = _case(tmp_path), {}
    _register(case)
    current = _promote(case)
    _, _, working = _run(case)
    assert working['ok'] is True
    old = get_version(case.home, 'script', 'sample', 1)
    for index in range(2):
        _, _, value = _run(case, version=1, inputs={'historical_fail': not historical_ok},
                           state=state, call_id=f'history-{index}', run_id='history')
        assert value['ok'] is historical_ok
        assert value['source_check']['query_health']['analysis_blocked'] is False
        assert value['execution_definition_ref'] == old['execution_definition_ref']
    request = _enqueue(case, 'history')
    assert len(request['executions']) == 2
    for row in request['executions']:
        assert row['execution']['sha256'] == old['code_sha256']
        assert row['execution']['execution_definition_ref'] == old['execution_definition_ref']
    assert case.service.run_pending().status == 'skipped'
    assert case.store.get_script('sample')['revision'] == current['revision']
    assert latest_version(case.home, 'script', 'sample')['version'] == 2
    assert _trial(case)['failures'] == 0 and _trial(case)['seen_calls'] == []
    assert case.runtime.backend.prompts == []


def test_current_failures_rollback_but_historical_success_does_not_reset_trial(tmp_path):
    case = _case(tmp_path)
    _register(case)
    current = _promote(case)
    assert _trial(case).get('promoted_definition_ref') == current['execution_definition_ref']
    for run_id, version, inputs in (
        ('current-a', None, {'fail': True}),
        ('old-success', 1, {'historical_fail': False}),
        ('current-b', None, {'fail': True}),
    ):
        _, _, value = _run(case, version=version, inputs=inputs, state={},
                           call_id=run_id, run_id=run_id)
        assert value['source_check']['query_health']['analysis_blocked'] is False
        _enqueue(case, run_id)
        assert case.service.run_pending().status == 'skipped'
        if run_id != 'current-b':
            assert _trial(case)['failures'] == 1
            assert case.store.get_script('sample')['code'] == _CURRENT
        duplicate = AssetEvolutionService(case.home).process(run_id)
        assert duplicate['duplicate'] is True and duplicate['notices'] == []
    assert case.store.get_script('sample')['code'] == _SOURCE
    assert latest_version(case.home, 'script', 'sample')['version'] == 3
    assert _trial(case)['active'] is False


@pytest.mark.parametrize('change', ['code', 'contract', 'summary'])
def test_edit_between_execution_and_observation_never_reattributes_bytes(tmp_path, change):
    case, state = _case(tmp_path), {}
    _register(case, _CURRENT, state=state)
    args, receipt, value = _run(case)
    before = case.store.get_script('sample')
    descriptor = evolution_descriptor(before)
    if change == 'contract':
        descriptor = replace(descriptor, io_contract=IOContract(outputs=('result',)))
    elif change == 'summary':
        descriptor = replace(descriptor, summary='新的说明')
    code = _CURRENT + '# later\n' if change == 'code' else _CURRENT
    case.store.save_script(descriptor, code, expected_revision=before['revision'])
    _observe(case, state, args, receipt, 'before-edit')
    execution = state['asset_mutations']['targets']['script:sample']['last_execution']
    assert execution['execution_definition_ref'] == value['execution_definition_ref']
    if change != 'summary':
        assert execution.get('verified') is False
        assert execution['sha256'] is None
    request = freeze_asset_mutations(case.home, state, run_id='intervening-edit')
    if change != 'summary':
        assert request['targets'] == []
    else:
        assert request['targets'][0]['verified'] is True


@pytest.mark.parametrize('change', ['contract', 'summary'])
def test_same_code_new_descriptor_supersedes_trial_before_failure_count(tmp_path, change):
    case = _case(tmp_path)
    _register(case)
    current = _promote(case)
    for index in range(2):
        _run(case, inputs={'fail': True}, state={}, call_id=f'failure-{index}', run_id=f'newer-{index}')
    descriptor = evolution_descriptor(current)
    if change == 'contract':
        descriptor = replace(descriptor, io_contract=IOContract(outputs=('result',)))
    else:
        descriptor = replace(descriptor, summary='较新的描述符')
    newer = case.store.save_script(descriptor, _CURRENT, expected_revision=current['revision'])
    for index in range(2):
        _enqueue(case, f'newer-{index}')
        assert case.service.run_pending().status == 'skipped'
    assert case.store.get_script('sample')['revision'] == newer['revision']
    assert latest_version(case.home, 'script', 'sample')['version'] == 3
    assert _trial(case)['active'] is False and _trial(case)['failures'] == 0


def test_workspace_source_keeps_precedence_and_cannot_fail_owner_trial(tmp_path):
    case, state = _case(tmp_path), {}
    _register(case)
    current = _promote(case)
    overlay = ScriptStore(case.home, workspace_root=case.work)
    workspace_code = _SOURCE + '# workspace\n'
    workspace = overlay.save_script(evolution_descriptor(current), workspace_code, target_scope='workspace')
    for index in range(2):
        _, _, value = _run(case, state=state, call_id=f'workspace-{index}', run_id='workspace')
        assert value['execution_definition_ref'] == workspace['execution_definition_ref']
        assert value['ok'] is False
    request = _enqueue(case, 'workspace')
    for row in request['executions']:
        assert row['execution']['sha256'] == hashlib.sha256(workspace_code.encode()).hexdigest()
        assert row['execution']['source_scope'] == 'workspace'
    assert case.service.run_pending().status == 'skipped'
    assert case.store.get_script('sample')['revision'] == current['revision']
    assert _trial(case)['failures'] == 0


def test_query_blocked_current_failure_is_not_a_trial_failure(tmp_path):
    case = _case(tmp_path)
    _register(case)
    _promote(case)
    for index in range(2):
        run_id = f'blocked-{index}'
        _, _, value = _run(case, inputs={'rows': [], 'fail': True}, state={}, call_id=run_id, run_id=run_id)
        assert value['source_check']['query_health']['analysis_blocked'] is True
        request = freeze_asset_mutations(case.home, case.agent._asset_mutation_states[(run_id, 'attempt-' + run_id)], run_id=run_id)
        assert request['evidence_tags'] == ['query_failed'] and request['executions'] == []
        assert AssetEvolutionService(case.home).process(run_id)['status'] == 'query_failed'
    assert _trial(case)['failures'] == 0


@pytest.mark.parametrize('inputs', [
    ['a', 'b'], [{'name': 'a', 'type': 'integer'}, {'type': 'string', 'name': 'b'}],
    ['a', {'name': 'b', 'default': {'nested': [1, True, None]}}],
    [None, True, 1, 1.5, ['a'], {'other': 'value'}],
])
def test_family_fingerprint_is_total_and_order_insensitive_for_accepted_inputs(inputs):
    contract = IOContract(inputs=tuple(inputs)).to_dict()
    try:
        first = evolution_family_fingerprint(_CURRENT, contract)
        reordered = evolution_family_fingerprint(_CURRENT, {**contract, 'inputs': inputs[::-1]})
    except (TypeError, ValueError) as exc:
        pytest.fail(f'accepted input contract must have a fingerprint: {type(exc).__name__}')
    assert first == reordered and len(first) == 64


@pytest.mark.parametrize('left,right', [
    ({'inputs': [{'name': 'a', 'type': 'string'}]}, {'inputs': [{'name': 'a', 'type': 'integer'}]}),
    ({'inputs': [{'name': 'a', 'required': False}]}, {'inputs': [{'name': 'a', 'required': True}]}),
    ({'outputs': ['result'], 'output_types': {'result': 'string'}},
     {'outputs': ['result'], 'output_types': {'result': 'integer'}}),
    ({'outputs': ['result'], 'nullable_outputs': []}, {'outputs': ['result'], 'nullable_outputs': ['result']}),
])
def test_family_fingerprint_preserves_material_contract_differences(left, right):
    assert evolution_family_fingerprint(_CURRENT, left) != evolution_family_fingerprint(_CURRENT, right)


def _queued_structured_target(tmp_path):
    case, state = _case(tmp_path), {}
    fields = [{'name': 'rows', 'type': 'array'}, {'name': 'fail', 'type': 'boolean'}]
    _register(case, _CURRENT, inputs=fields, state=state, run_id='structured')
    _, _, value = _run(case, state=state, run_id='structured')
    assert value['ok'] is True
    request = _enqueue(case, 'structured')
    assert len(request['targets']) == 1 and request['targets'][0]['verified'] is True
    assert case.service.enqueue_from_finalize(_ctx('next-valid')) == 'queued'
    paths = case.runtime.store.pending_requests()
    assert len(paths) == 2
    return case, request, paths[0], paths[1]


def test_structured_input_real_freeze_and_fifo_head_complete_before_next_request(tmp_path):
    case, request, head, next_path = _queued_structured_target(tmp_path)
    assert find_evolution_family(case.home, evolution_family_fingerprint(
        _CURRENT, request['targets'][0]['io_contract'])) == 'sample'
    result = case.service.run_pending(expected_request_path=head)
    assert result.status == 'skipped' and not head.exists()
    assert case.runtime.store.pending_requests() == [next_path]
    assert case.runtime.backend.prompts == []
    assert case.service.run_pending(expected_request_path=next_path).status == 'skipped'
    assert not case.runtime.store.pending_requests() and len(case.runtime.backend.prompts) == 1


def test_deterministic_evolution_failure_has_bounded_retry_and_correlated_terminal_event(tmp_path, monkeypatch):
    case, _, head, next_path = _queued_structured_target(tmp_path)
    original = case.runtime.store.read_request(head)

    def fail_route(self, request):
        raise TypeError('deterministic contract processing failure')

    monkeypatch.setattr(AssetEvolutionService, '_route_request', fail_route)
    for attempt in range(1, MAX_REQUEST_ATTEMPT_COUNT + 1):
        # 重新装配消费者，验证 attempts 来自原请求而不是进程内计数。
        service = SkillLearningService(case.runtime.store, case.service.settings, case.service.runtime)
        try:
            result = service.run_pending(expected_request_path=head)
        except TypeError:
            pytest.fail('deterministic evolution failure escaped without FIFO progress')
        assert result.request_key == original['request_key']
        assert result.code == 'SKILL_LEARNING_EVOLUTION_FAILED'
        if attempt < MAX_REQUEST_ATTEMPT_COUNT:
            assert result.status == 'retry'
            saved = case.runtime.store.read_request(head)
            assert saved['attempts'] == attempt
            assert {key: value for key, value in saved.items() if key != 'attempts'} == {
                key: value for key, value in original.items() if key != 'attempts'}
        else:
            assert result.status == 'failed' and not head.exists()
    event = case.runtime.store.events(limit=1)[0]
    assert (event['event'], event['code'], event['reason']) == (
        'failed', 'SKILL_LEARNING_EVOLUTION_FAILED', 'TypeError')
    assert event['request_key'] == original['request_key'] and event['run_id'] == original['run_id']
    assert case.runtime.store.pending_requests() == [next_path]
    assert case.service.run_pending(expected_request_path=next_path).status == 'skipped'
    assert not case.runtime.store.pending_requests()


def test_historical_effective_permissions_are_verified_without_history_scan(tmp_path, monkeypatch):
    from agent_py_agent.agent.assets import mutation_ledger, revision_history

    case = _case(tmp_path)
    _register(case)
    before = case.store.get_script('sample')
    descriptor = evolution_descriptor(before)
    descriptor = replace(descriptor, data_sources=(replace(descriptor.data_sources[0], query_scope={}),))
    case.store.save_script(descriptor, _SOURCE, expected_revision=before['revision'])
    historical = latest_version(case.home, 'script', 'sample')
    _promote(case)
    args, receipt, value = _run(case, version=historical['version'])
    # 历史执行使用当前与原权限求交；实际引用可能不同于未求交的发布引用。
    assert value['execution_definition_ref'] != historical['execution_definition_ref']
    versions, reads = [], []
    get = mutation_ledger.get_version
    read = mutation_ledger.read_bytes_beneath

    def exact_version(home, kind, asset_id, version):
        versions.append((kind, asset_id, version))
        return get(home, kind, asset_id, version)

    def bounded_blob(home, parts, *, max_bytes):
        reads.append((parts, max_bytes))
        return read(home, parts, max_bytes=max_bytes)

    def no_enumeration(*args, **kwargs):
        pytest.fail('capture must not enumerate accumulated version history')

    monkeypatch.setattr(mutation_ledger, 'get_version', exact_version)
    monkeypatch.setattr(mutation_ledger, 'read_bytes_beneath', bounded_blob)
    monkeypatch.setattr(revision_history, '_names', no_enumeration)
    state = {}
    _observe(case, state, args, receipt, 'effective-history')
    fact = state['asset_mutations']['targets']['script:sample']['last_execution']
    assert fact['verified'] is True and fact['sha256'] == historical['code_sha256']
    assert fact['execution_definition_ref'] == value['execution_definition_ref']
    assert fact['published_definition_ref'] == historical['execution_definition_ref']
    assert versions == [('script', 'sample', historical['version'])]
    assert len(reads) == 2
    assert [limit for _, limit in reads] == [AssetMutationLedger.max_capture_bytes, 4194304]
    assert AssetMutationLedger.max_targets == 32 and AssetMutationLedger.max_facts_per_target == 64


def test_duplicate_original_call_in_two_requests_is_idempotent(tmp_path):
    case = _case(tmp_path)
    _register(case)
    _promote(case)
    args, receipt, _ = _run(case, inputs={'fail': True})
    for run_id in ('delivery-a', 'delivery-b'):
        _observe(case, {}, args, receipt, 'original-call', run_id=run_id)
        request = _enqueue(case, run_id)
        assert request['executions'][0]['execution']['call_id'] == 'original-call'
        assert case.service.run_pending().status == 'skipped'
        assert _trial(case)['failures'] == 1 and _trial(case)['seen_calls'] == ['original-call']
        assert case.store.get_script('sample')['code'] == _CURRENT


def test_restore_cas_does_not_overwrite_edit_after_trial_identity_check(tmp_path, monkeypatch):
    case, state = _case(tmp_path), {}
    _register(case)
    _promote(case)
    for index in range(2):
        _run(case, inputs={'fail': True}, state=state, call_id=f'cas-{index}', run_id='cas')
    request = _enqueue(case, 'cas')
    saved, original = [], ScriptStore.save_script

    def edit_before_restore(self, descriptor, code, **options):
        if self.owner_home == case.home and code == _SOURCE and not saved:
            current = self.get_script('sample')
            newer = replace(evolution_descriptor(current), summary='恢复提交前的新说明')
            saved.append(original(self, newer, _CURRENT, expected_revision=current['revision']))
        return original(self, descriptor, code, **options)

    monkeypatch.setattr(ScriptStore, 'save_script', edit_before_restore)
    assert case.service.run_pending().status == 'retry'
    assert len(saved) == 1 and case.store.get_script('sample')['revision'] == saved[0]['revision']
    assert case.store.get_script('sample')['code'] == _CURRENT
    assert _trial(case)['failures'] == 1
    assert case.service.run_pending().status == 'skipped'
    assert _trial(case)['active'] is False and _trial(case)['failures'] == 1
    after = read_evolution_json(case.home, ('data', 'asset_evolution', 'requests', 'cas.json'))
    assert after['executions'] == request['executions']
    assert latest_version(case.home, 'script', 'sample')['version'] == 3


def test_owner_fifo_has_one_active_processor_with_deterministic_interleaving(tmp_path, monkeypatch):
    case, _, head, next_path = _queued_structured_target(tmp_path)
    entered, release = threading.Event(), threading.Event()
    lock, counts = threading.Lock(), {'active': 0, 'peak': 0}
    original = AssetEvolutionService._route_request

    def held_route(self, request):
        with lock:
            counts['active'] += 1
            counts['peak'] = max(counts['peak'], counts['active'])
        try:
            entered.set()
            assert release.wait(10), 'test must release original processing'
            return original(self, request)
        finally:
            with lock:
                counts['active'] -= 1

    monkeypatch.setattr(AssetEvolutionService, '_route_request', held_route)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(case.service.run_pending, expected_request_path=head)
        try:
            assert entered.wait(10)
            other = SkillLearningService(case.runtime.store, case.service.settings, case.service.runtime)
            assert other.run_pending(expected_request_path=head).status == 'busy'
            assert case.runtime.store.pending_requests() == [head, next_path]
        finally:
            release.set()
        assert first.result(10).status == 'skipped'
    assert counts == {'active': 0, 'peak': 1}
    assert case.runtime.store.pending_requests() == [next_path]
    assert case.service.run_pending(expected_request_path=next_path).status == 'skipped'


@pytest.mark.parametrize('kind', ['outcome', 'context', 'tool', 'bounded', 'interrupt', 'cancel', 'io', 'quota', 'still-running', 'runtime'])
def test_evolution_control_or_uncertain_outcome_keeps_original_request(tmp_path, monkeypatch, kind):
    from asyncio import CancelledError

    from agent_py_agent.agent.backends.bounded_call import BoundedCallExitUnconfirmedError
    from agent_py_agent.agent.common.request_execution_ports import (
        RequestExecutionContextUnavailable,
        RequestExecutionOutcomeUncertainError,
    )
    from agent_py_agent.agent.common.tool_write_context import ToolWriteContextUnavailable
    from agent_py_agent.agent.memory_store.curator_backend import CuratorModelStillRunningError
    from agent_py_agent.agent.user_space.owner_quota import OwnerQuotaUnavailable

    case, _, head, next_path = _queued_structured_target(tmp_path)
    error = {
        'outcome': RequestExecutionOutcomeUncertainError(
            {'id': 'request', 'execution_attempt_id': 'attempt', 'lease_epoch': 1, 'lease_owner': 'attempt'}, 'evolution'),
        'context': RequestExecutionContextUnavailable('original execution missing'),
        'tool': ToolWriteContextUnavailable('original write source missing'),
        'bounded': BoundedCallExitUnconfirmedError('original-call', 'worker_exit_unconfirmed'),
        'interrupt': InterruptedError('original processing interrupted'),
        'cancel': CancelledError(),
        'io': OSError('original storage unavailable'),
        'quota': OwnerQuotaUnavailable('authoritative usage unavailable'),
        'still-running': CuratorModelStillRunningError('original worker still running'),
        'runtime': RuntimeError('unclassified processing outcome'),
    }[kind]

    def refuse(self, request):
        raise error

    monkeypatch.setattr(AssetEvolutionService, '_route_request', refuse)
    before = head.read_bytes()
    ledger = case.runtime.store.ledger_path
    events = ledger.read_bytes() if ledger.exists() else None
    if kind == 'quota':
        result = case.service.run_pending(expected_request_path=head)
        assert (result.status, result.code) == ('failed', 'SKILL_LEARNING_STORAGE_LIMIT')
    elif kind == 'still-running':
        assert case.service.run_pending(expected_request_path=head).status == 'busy'
    else:
        with pytest.raises(type(error)) as raised:
            case.service.run_pending(expected_request_path=head)
        assert raised.value is error
    assert head.read_bytes() == before
    assert case.runtime.store.pending_requests() == [head, next_path]
    assert (ledger.read_bytes() if ledger.exists() else None) == events
    assert case.runtime.backend.prompts == []
    with case.runtime.store.run_lock():
        assert case.runtime.store.read_request(head)['attempts'] == 0


def test_real_restore_committed_readback_failure_keeps_request_and_attempts(tmp_path, monkeypatch):
    from agent_py_agent.agent.assets import AssetPostCommitReadbackError

    case, state = _case(tmp_path), {}
    _register(case)
    current = _promote(case)
    for index in range(2):
        _run(case, inputs={'fail': True}, state=state, call_id=f'readback-{index}', run_id='readback')
    frozen = _enqueue(case, 'readback')
    head = case.runtime.store.pending_requests()[0]
    before = head.read_bytes()
    ledger = case.runtime.store.ledger_path
    events = ledger.read_bytes() if ledger.exists() else None
    original, committed = ScriptStore._readback_after_commit, []

    def fail_projection(record):
        raise OSError('single post-commit projection read unavailable')

    def post_commit_readback(self, asset_id):
        if self.owner_home != case.home:
            return original(self, asset_id)
        committed.append(asset_id)
        # 保留原Store提交、读回与异常包装，只在已提交后的展示读取注入一次真实I/O故障。
        with monkeypatch.context() as patch:
            patch.setattr(self, '_project_record', fail_projection)
            return original(self, asset_id)

    monkeypatch.setattr(ScriptStore, '_readback_after_commit', post_commit_readback)
    fd_before = _descriptor_count()
    with pytest.raises(AssetPostCommitReadbackError) as raised:
        case.service.run_pending(expected_request_path=head)
    assert isinstance(raised.value.__cause__, OSError)
    assert committed == ['sample']
    restored = case.store.get_script('sample')
    assert restored['code'] == _SOURCE and restored['revision'] > current['revision']
    assert head.read_bytes() == before and case.runtime.store.read_request(head)['attempts'] == 0
    assert (ledger.read_bytes() if ledger.exists() else None) == events
    assert read_evolution_json(case.home, ('data', 'asset_evolution', 'requests', 'readback.json')) == frozen
    assert _trial(case)['active'] is True
    with case.runtime.store.run_lock():
        assert case.runtime.store.pending_requests() == [head]
    fd_after = _descriptor_count()
    print(json.dumps({'scenario': 'committed-readback-unknown', 'fd_before': fd_before,
                      'fd_after': fd_after, 'fd_delta': fd_after - fd_before}))
    assert fd_after == fd_before


def _descriptor_count():
    """POSIX 直接观察 FD；Windows 使用同进程句柄计数，不修改运行时注册表。"""
    if os.name == 'nt':
        import ctypes
        count = ctypes.c_ulong()
        kernel = ctypes.windll.kernel32
        assert kernel.GetProcessHandleCount(kernel.GetCurrentProcess(), ctypes.byref(count))
        return count.value
    with os.scandir('/dev/fd') as entries:
        return sum(1 for _ in entries)


@pytest.mark.parametrize('fail', [False, True])
def test_real_execution_observe_and_process_release_descriptors(tmp_path, fail):
    case = _case(tmp_path)
    _register(case, _CURRENT)
    _run(case)  # 先完成解释器/摘要仓库的常规初始化，再测相同入口。
    before = _descriptor_count()
    state = {}
    _, _, value = _run(case, inputs={'fail': fail}, state=state, call_id='measured-call')
    assert value['ok'] is not fail
    assert value['source_check']['query_health']['analysis_blocked'] is False
    request = freeze_asset_mutations(case.home, state, run_id='fd-measured')
    assert request['executions'][0]['execution']['verified'] is True
    assert AssetEvolutionService(case.home).process('fd-measured')['status'] == 'processed'
    after = _descriptor_count()
    print(json.dumps({'scenario': 'contract-failure' if fail else 'success',
                      'fd_before': before, 'fd_after': after, 'fd_delta': after - before}))
    assert after - before == 0



# ==========================================================================
# Source: agent_py_agent/tests/test_v15_rework_r2_published_contracts.py
# 发布身份的真实存储/工具/执行往返；外部模型与IM不属于本切片。
# ==========================================================================

CODE = 'import json\nprint(json.dumps({"value":7}))\n'


def published_script(home):
    descriptor = AssetDescriptor('probe', 'script', '固定查询', io_contract={'outputs': ['value']},
        access_targets=(AccessTarget('local', 'file', 'input.json', extra={'read_only': True}),))
    store = ScriptStore(home)
    row = store.save_script(descriptor, CODE)
    return store, descriptor, row, latest_version(home, 'script', 'probe')


def frozen_pin(row, published, *, version=True, reference=True):
    pin = {'script_id': 'probe', 'revision': row['revision'], 'code_sha256': published['code_sha256'],
           'source_scope': row['source_scope']}
    if version:
        pin['version'] = published['version']
    if reference:
        pin['execution_definition_ref'] = published['execution_definition_ref']
    return pin


@pytest.mark.parametrize('blocking', [False, True])
@pytest.mark.parametrize('reference', [False, True])
def test_normal_summary_fold_does_not_invalidate_execution(tmp_path, blocking, reference):
    store, _, row, published = published_script(tmp_path)
    pin = frozen_pin(row, published, version=False, reference=reference)
    runner = ScriptRunner(tmp_path)
    first = runtime.run_script_with_datasources(tmp_path, 'probe', runner=runner, expected_script_pin=pin)
    assert first.ok and first.output_data == {'value': 7}, first
    if blocking:
        store.record_run_summary('probe', 'succeeded', 1, blocking=True, run_id='explicit-fold')
    else:
        store.fold_run_summaries('probe')
    folded = store.get_script('probe')
    assert folded['revision'] > row['revision']
    assert folded['execution_definition_ref'] == row['execution_definition_ref']
    assert latest_version(tmp_path, 'script', 'probe')['version'] == published['version']
    second = runtime.run_script_with_datasources(tmp_path, 'probe', runner=runner, expected_script_pin=pin)
    assert second.ok and second.output_data == {'value': 7}, second
    assert second.source_check['pin_resolution'] == {'mode': 'current'}


def test_public_script_version_keeps_same_code_historical_contract(tmp_path):
    store, descriptor, _, first = published_script(tmp_path)
    current = store.save_script(replace(descriptor, io_contract={'outputs': ['later']}), CODE)
    second = latest_version(tmp_path, 'script', 'probe')
    assert second['version'] > first['version'] and second['code_sha256'] == first['code_sha256']
    assert second['execution_definition_ref'] != first['execution_definition_ref']
    output = call(ScriptsTool(tmp_path, runner=ScriptRunner(tmp_path)),
                  {'action': 'run', 'script_id': 'probe', 'version': first['version']})
    assert output['ok'] and output['output_data'] == {'value': 7}, output
    assert output['source_check']['pin_resolution']['version'] == first['version']
    assert store.get_script('probe')['io_contract'] == current['io_contract']


def test_public_same_code_history_does_not_gain_current_retry_permission(tmp_path, monkeypatch):
    targets = (AccessTarget('api', 'http', 'https://example.invalid', method='POST'),)
    old = AssetDescriptor('probe', 'script', '固定副作用', access_targets=targets,
        metadata={'retry_policy': {'idempotent': False}})
    store = ScriptStore(tmp_path)
    store.save_script(old, CODE)
    first = latest_version(tmp_path, 'script', 'probe')
    store.save_script(replace(old, metadata={'retry_policy': {'idempotent': True}}), CODE)
    second = latest_version(tmp_path, 'script', 'probe')
    assert first['code_sha256'] == second['code_sha256']
    assert first['execution_definition_ref'] == second['execution_definition_ref']
    runner = CountingRunner([result(error_code='ECONNRESET')])
    monkeypatch.setattr(runtime.time, 'sleep', lambda delay: None)
    outcome = ScriptsTool(tmp_path, runner=runner).execute(
        {'action': 'run', 'script_id': 'probe', 'version': first['version']})
    assert not outcome.ok
    output = json.loads(outcome.output)
    assert not output['ok'] and output['error_code'] == 'ECONNRESET', output
    assert len(runner.calls) == 1


@pytest.mark.parametrize('selection,attempts', [('current', 3), ('version', 3), ('old_ref', 1), ('fresh_ref', 3)])
def test_summary_fold_only_withholds_unproven_legacy_retry_permission(tmp_path, monkeypatch, selection, attempts):
    descriptor = AssetDescriptor('probe', 'script', '固定副作用',
        access_targets=(AccessTarget('api', 'http', 'https://example.invalid', method='POST'),),
        metadata={'retry_policy': {'idempotent': True}})
    store = ScriptStore(tmp_path)
    row = store.save_script(descriptor, CODE)
    published = latest_version(tmp_path, 'script', 'probe')
    store.record_run_summary('probe', 'succeeded', 1, blocking=True, run_id='fold')
    current = store.get_script('probe')
    assert current['revision'] > row['revision']
    pin = None if selection == 'current' else frozen_pin(
        current if selection == 'fresh_ref' else row, published, version=selection == 'version')
    runner = CountingRunner([result(error_code='ECONNRESET')])
    monkeypatch.setattr(runtime.time, 'sleep', lambda delay: None)
    value = runtime.run_script_with_datasources(tmp_path, 'probe', runner=runner, expected_script_pin=pin)
    assert not value.ok and value.error_code == 'ECONNRESET'
    assert len(runner.calls) == attempts
    assert store.get_script('probe')['metadata']['retry_policy']['idempotent'] is True


@pytest.mark.parametrize('old_idempotent,current_idempotent', [(False, True), (True, False)])
def test_explicit_same_code_version_intersects_both_retry_permissions(tmp_path, monkeypatch,
        old_idempotent, current_idempotent):
    old = AssetDescriptor('probe', 'script', '固定副作用',
        access_targets=(AccessTarget('api', 'http', 'https://example.invalid', method='POST'),),
        metadata={'retry_policy': {'idempotent': old_idempotent}})
    store = ScriptStore(tmp_path)
    row = store.save_script(old, CODE)
    published = latest_version(tmp_path, 'script', 'probe')
    store.save_script(replace(old, metadata={'retry_policy': {'idempotent': current_idempotent}}), CODE)
    runner = CountingRunner([result(error_code='ECONNRESET')])
    monkeypatch.setattr(runtime.time, 'sleep', lambda delay: None)
    value = runtime.run_script_with_datasources(tmp_path, 'probe', runner=runner,
        expected_script_pin=frozen_pin(row, published))
    assert not value.ok and value.error_code == 'ECONNRESET' and len(runner.calls) == 1
    assert value.source_check['pin_resolution']['version'] == published['version']


def test_public_same_code_history_keeps_query_semantics_under_current_permissions(tmp_path):
    DatasourceStore(tmp_path).save_datasource({'datasource_id': 'events', 'kind': 'custom', 'title': '事件'})
    old = AssetDescriptor('probe', 'script', '固定查询', io_contract={'outputs': ['window', 'access']},
        access_targets=(AccessTarget('local', 'file', 'input.json', extra={'read_only': True}),),
        data_sources=(DataSourceSpec('events', 'custom', query_scope={'window': 'old', 'access': 'read'}),))
    code = '''import json,os
from agent_py_agent.agent.workflows.datasource_runtime import current_datasource_bindings
print(json.dumps({"window":json.loads(os.environ["MY_AGENT_DATA_SOURCES_JSON"])[0]["query_scope"]["window"],
                  "access":current_datasource_bindings()["events"]["access"]}))
'''
    store = ScriptStore(tmp_path)
    store.save_script(old, code)
    first = latest_version(tmp_path, 'script', 'probe')
    store.save_script(replace(old, data_sources=(DataSourceSpec('events', 'custom',
        query_scope={'window': 'new', 'access': 'read_write'}),)), code)
    output = call(ScriptsTool(tmp_path, runner=ScriptRunner(tmp_path)),
                  {'action': 'run', 'script_id': 'probe', 'version': first['version']})
    assert output['ok'] and output['output_data'] == {'window': 'old', 'access': 'read'}, output
    assert output['source_check']['pin_resolution']['version'] == first['version']
    assert store.get_script('probe')['data_sources'][0]['query_scope'] == {'window': 'new', 'access': 'read_write'}


def test_explicit_version_without_ref_never_uses_new_same_sha_contract(tmp_path):
    store, descriptor, row, old = published_script(tmp_path)
    store.save_script(replace(descriptor, io_contract={'outputs': ['later']}), CODE)
    value = runtime.run_script_with_datasources(tmp_path, 'probe', runner=ScriptRunner(tmp_path),
        expected_script_pin=frozen_pin(row, old, reference=False))
    assert value.ok and value.output_data == {'value': 7}, value
    assert value.source_check['pin_resolution']['version'] == old['version']


def test_corrupt_script_blob_rejects_workflow_before_run_creation(tmp_path):
    _, _, row, script = published_script(tmp_path)
    WorkflowStore(tmp_path).save_workflow(WorkflowDefinition(AssetDescriptor('frozen', 'workflow', '冻结流程'), (
        WorkflowStepSpec('one', '第一步', 'deterministic_script', script_id='probe'),)))
    workflow = latest_version(tmp_path, 'workflow', 'frozen')
    sha = script['code_sha256']
    (tmp_path / 'data/asset_evolution/blobs' / sha[:2] / sha).write_bytes(b'corrupt')
    assert historical_pin_available(tmp_path, frozen_pin(row, script)) is False
    output = WorkflowEngine(tmp_path).run_workflow('frozen', definition_version=workflow['version'])
    assert output['error_code'] == 'ASSET_HISTORY_BLOB_MISSING' and output['effect_outcome'] == 'not_started'
    assert not list((tmp_path / 'data/workflow_runs').glob('*/run.json'))


def test_published_workflow_selects_script_version_not_latest_matching_sha(tmp_path):
    scripts, descriptor, _, old_script = published_script(tmp_path)
    workflows = WorkflowStore(tmp_path)
    workflows.save_workflow(WorkflowDefinition(AssetDescriptor('frozen', 'workflow', '冻结流程'), (
        WorkflowStepSpec('one', '第一步', 'deterministic_script', script_id='probe', on_error='fail'),
        WorkflowStepSpec('two', '第二步', 'deterministic_script', script_id='probe', on_error='fail'))))
    workflow_record = latest_version(tmp_path, 'workflow', 'frozen')
    assert workflow_record['pins']['scripts']['probe']['version'] == old_script['version']
    scripts.save_script(replace(descriptor, io_contract={'outputs': ['later']}), CODE)
    _, frozen = resolve_definition(tmp_path, 'frozen', workflow_record['version'])
    assert {pin['execution_definition_ref'] for pin in frozen.values()} == {old_script['execution_definition_ref']}
    assert {pin['version'] for pin in frozen.values()} == {old_script['version']}
    engine = WorkflowEngine(tmp_path)
    run = engine.run_workflow('frozen', definition_version=workflow_record['version'])
    assert run.status == 'succeeded' and run.final_output == {'value': 7}, run
    assert engine.store.load_workflow_snapshot(run.workflow_run_id)['_frozen_scripts'] == frozen


@pytest.mark.parametrize('missing', ['version', 'reference', 'payload_blob', 'code_sha256', 'side_blob'])
def test_explicit_pin_requires_exact_complete_history_even_if_current_code_matches(tmp_path, missing):
    _, _, row, _ = published_script(tmp_path)
    published = record_version(tmp_path, 'script', 'probe', payload=row, code=row['code'].encode(),
        side_files={'rows.json': 'frozen-rows'}, storage_revision=row['revision'],
        execution_definition_ref=row['execution_definition_ref'])['record']
    pin = frozen_pin(row, published)
    if missing == 'version':
        pin['version'] += 100
    elif missing == 'reference':
        pin['execution_definition_ref'] = 'f' * 64
    else:
        digest = published['side_blobs']['rows.json'] if missing == 'side_blob' else published[missing]
        (tmp_path / 'data/asset_evolution/blobs' / digest[:2] / digest).unlink()
    assert historical_pin_available(tmp_path, pin) is False
    runner = CountingRunner([result()])
    value = runtime.run_script_with_datasources(tmp_path, 'probe', expected_script_pin=pin, runner=runner)
    assert not value.ok and value.error_code == 'SCRIPT_REVISION_DRIFT'
    assert value.source_check['pin_resolution'] == {'mode': 'missing', 'reason': 'history_blob_missing'}
    assert not runner.calls


def test_exact_version_cannot_be_rebound_to_same_sha_different_reference(tmp_path):
    store, descriptor, row, old = published_script(tmp_path)
    store.save_script(replace(descriptor, io_contract={'outputs': ['later']}), CODE)
    new = latest_version(tmp_path, 'script', 'probe')
    pin = {**frozen_pin(row, old), 'execution_definition_ref': new['execution_definition_ref']}
    assert historical_pin_available(tmp_path, pin) is False
    runner = CountingRunner([result()])
    value = runtime.run_script_with_datasources(tmp_path, 'probe', expected_script_pin=pin, runner=runner)
    assert not value.ok and not runner.calls
    assert get_version(tmp_path, 'script', 'probe', old['version']) == old


def test_real_recurring_schedule_runs_again_after_normal_summary_fold(tmp_path):
    from agent_py_agent.tests.test_plan_w8_schedule_target import asset_case

    case = asset_case(tmp_path)
    repository = case.agent.scheduler_repository
    home = case.agent.home_paths.owner_home_dir
    initial = case.scripts.get_script('scheduled_script')
    published = latest_version(home, 'script', 'scheduled_script')
    frozen = []
    for due in (1000, 1600):
        case.agent.scheduler_service.enqueue_ready_runs(now=due)
        queued = repository.queued_runs(now=due)
        assert len(queued) == 1
        frozen.append(json.loads(json.dumps(queued[0])))
        case.scheduler.tick(now=due + 1)
        case.scheduler.tick(now=due + 2)
        history, errors = repository.history()
        assert errors == [] and len(history) == len(frozen)
        by_id = {row['run_id']: row for row in history}
        for before in frozen:
            row = by_id[before['run_id']]
            assert row['status'] == 'done', row
            for key in ('owner', 'job_id', 'scheduled_for', 'execution_definition_ref', 'execution_target'):
                assert row[key] == before[key]
            assert row['model_call_count'] == 0
            assert row['structured_result']['output_data'] == {'ok': True, 'value': 7}
        if due == 1000:
            # 真实runner已经非阻塞记摘要；仅显式折叠才改变canonical存储revision。
            summary = case.scripts.get_script('scheduled_script')
            assert summary['runtime_summary']['run_count'] == 1
            assert summary['revision'] == initial['revision']
            folded = case.scripts.fold_run_summaries('scheduled_script')
            assert folded['revision'] > initial['revision']
            assert folded['execution_definition_ref'] == initial['execution_definition_ref']
            assert latest_version(home, 'script', 'scheduled_script') == published
    assert len({row['run_id'] for row in frozen}) == 2
    assert case.agent.backend.prompts == []


@pytest.mark.parametrize('later_versions', [0, 24])
def test_exact_workflow_pin_reads_each_snapshot_once_without_history_scan(tmp_path, monkeypatch, later_versions):
    from agent_py_agent.agent.assets import revision_history
    from agent_py_agent.agent.workflows import pinned_definitions

    scripts, descriptor, _, published = published_script(tmp_path)
    WorkflowStore(tmp_path).save_workflow(WorkflowDefinition(AssetDescriptor('frozen', 'workflow', '冻结流程'), (
        WorkflowStepSpec('one', '第一步', 'deterministic_script', script_id='probe'),
        WorkflowStepSpec('two', '第二步', 'deterministic_script', script_id='probe'))))
    workflow = latest_version(tmp_path, 'workflow', 'frozen')
    for number in range(later_versions):
        scripts.save_script(descriptor, f'print({{"value": {number}}})\n')
    records, blobs = [], []
    original_get, original_blob = pinned_definitions.get_version, pinned_definitions.blob_get

    def read_record(home, kind, identifier, version):
        records.append((kind, identifier, version))
        return original_get(home, kind, identifier, version)

    def read_blob(home, digest):
        blobs.append(digest)
        return original_blob(home, digest)

    def no_scan(*args, **kwargs):
        pytest.fail('explicit version path must not load or scan the history index')

    monkeypatch.setattr(pinned_definitions, 'get_version', read_record)
    monkeypatch.setattr(pinned_definitions, 'blob_get', read_blob)
    monkeypatch.setattr(pinned_definitions, 'find_version', no_scan)
    monkeypatch.setattr(revision_history, '_load_index', no_scan)
    _, frozen = resolve_definition(tmp_path, 'frozen', workflow['version'])
    assert records == [('workflow', 'frozen', workflow['version']), ('script', 'probe', published['version'])]
    assert blobs == [workflow['payload_blob'], *workflow['side_blobs'].values(),
                     published['payload_blob'], published['code_sha256'], *published['side_blobs'].values()]
    assert frozen['one'] == frozen['two']


def open_handle_count():
    """POSIX数真实FD；Windows使用对应的进程句柄计数，不建立额外监测进程。"""
    if os.name == 'nt':
        import ctypes
        count = ctypes.c_ulong()
        kernel = ctypes.windll.kernel32
        assert kernel.GetProcessHandleCount(kernel.GetCurrentProcess(), ctypes.byref(count))
        return count.value
    return len(os.listdir('/dev/fd'))


def test_historical_execution_releases_handles_on_success_and_prepare_error(tmp_path, monkeypatch):
    from agent_py_agent.agent.workflows import script_runner

    store, descriptor, row, published = published_script(tmp_path)
    store.save_script(descriptor, 'print({"value": 8})\n')
    pin = frozen_pin(row, published)
    runner = ScriptRunner(tmp_path)
    # 先经过一次原生准备、执行和摘要路径，把一次性模块加载排除出泄漏计数。
    warm = runtime.run_script_with_datasources(tmp_path, 'probe', expected_script_pin=pin, runner=runner)
    assert warm.ok and warm.output_data == {'value': 7}, warm
    before = open_handle_count()
    success = runtime.run_script_with_datasources(tmp_path, 'probe', expected_script_pin=pin, runner=runner)
    assert success.ok and success.output_data == {'value': 7}
    assert open_handle_count() - before == 0
    prepared = []

    def unavailable(command, folder, environment, input_json, timeout, capture):
        prepared.append(Path(folder))
        raise OSError('simulated process preparation error')

    monkeypatch.setattr(script_runner, '_execute', unavailable)
    before = open_handle_count()
    failure = runtime.run_script_with_datasources(tmp_path, 'probe', expected_script_pin=pin, runner=runner)
    assert not failure.ok and len(prepared) == 1
    assert open_handle_count() - before == 0
    assert not prepared[0].exists() and not list((tmp_path / 'tmp/script_exec').iterdir())
    assert runtime.script_execution_record(tmp_path, 'probe', None) is None
    assert runtime.script_history_record(tmp_path, 'probe', None) is None



# ==========================================================================
# Source: agent_py_agent/tests/test_v15_rework_wake_recovery.py
# R6：单次迁移读盘失败后的同进程恢复，真实 Client/Gateway/Store/消费者。
# ==========================================================================

def _unknown_after_normal_turn(case):
    """保留真实已处理发布账，将唯一派生库降为受支持的旧 unknown schema。"""
    host, _client, _sid, view, model, scheduler = case
    store, thread_id = host.conversation_store, view["agent_id"]
    earlier = assign(case, "r6-before-migration")
    scheduler.tick_thread(thread_id)
    assert store.session_tasks.require(earlier).status == "done" and model.calls == 1
    record = next(store.storage.wake_dedupe_dir.glob("*.json"))
    proof = record.read_bytes()
    store.storage.wake_task_index_path.unlink()
    _w5_unknown_index(store.storage)
    return record, proof


def _transient_migration(case, monkeypatch, record):
    """只在一个原发布记录的文件读取边界制造一次 EIO，不替换生产恢复路线。"""
    host = case[0]
    store = host.conversation_store
    read_text, failures = Path.read_text, []

    def read(path, *args, **kwargs):
        if path == record and not failures:
            failures.append(path)
            raise OSError(errno.EIO, "simulated I/O error")
        return read_text(path, *args, **kwargs)

    before = _fd_count()
    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", read)
        reopened = ConversationStore(store.storage.root)
    assert failures == [record]
    assert publication._task_index_coverage(reopened.storage) == "unknown"
    assert _fd_count() == before


def _queued_without_wake(case, action):
    host, client, _sid, view, _model, _scheduler = case
    store, thread_id = host.conversation_store, view["agent_id"]
    current = completed(client, "background_agents.get", {"agent_id": thread_id})["agent"]
    if action == "restore":
        parked = completed(client, "background_agents.retire", {"agent_id": thread_id, "disposition": "drain"},
            expected_revision=current["revision"])["agent"]
        assert parked["identity_state"] == "retired"
    else:
        parked = completed(client, "background_agents.suspend", {"agent_id": thread_id},
            expected_revision=current["revision"])["agent"]
    task_id = _tool_dispatch(host, view, "整理一份简短文字结果：R6迁移恢复")
    assert store.session_tasks.require(task_id).status == "queued"
    assert task_wakes(store, task_id) == []
    return task_id, parked["revision"]


def _transition(client, thread_id, action, revision):
    params = {"agent_id": thread_id}
    if action == "retire":
        params["disposition"] = "drain"
    return completed(client, "background_agents." + action, params, expected_revision=revision)


@pytest.mark.parametrize("boundary", ["writable-open", "resume", "restore", "retire"])
def test_transient_migration_recovers_automatically_with_explicit_rebuild_control(business_wire, monkeypatch, boundary):
    case = runtime_case(business_wire)
    host, client, _sid, view, model, scheduler = case
    store, thread_id = host.conversation_store, view["agent_id"]
    # 先建立原服务实例；后续恢复不能靠重新构造宿主或进程清掉内存节流。
    service, actor = producer_service(case)
    record, proof = _unknown_after_normal_turn(case)
    action = "resume" if boundary == "writable-open" else boundary
    task_id, revision = _queued_without_wake(case, action)
    task_before = store.session_tasks.require(task_id)
    _transient_migration(case, monkeypatch, record)
    before = _fd_count()
    if boundary == "writable-open":
        for _ in range(3):
            ConversationStore(store.storage.root)
    _transition(client, thread_id, action, revision)
    automatic = [wake.wake_signal_id for wake in task_wakes(store, task_id)]
    automatic_coverage = publication._task_index_coverage(store.storage)
    service._republish_pending_work(store.threads.require(thread_id), [task_before], actor=actor)
    assert [wake.wake_signal_id for wake in task_wakes(store, task_id)] == automatic
    assert _fd_count() == before
    # 即使自动恢复是红灯，也先证明同一份 canonical 可由显式重建恢复并真正执行。
    store.wakes.rebuild_task_responsibility_index()
    service._republish_pending_work(store.threads.require(thread_id), [task_before], actor=actor)
    live = task_wakes(store, task_id)
    assert len(live) == 1 and record.read_bytes() == proof
    assert model.calls == 1
    scheduler.tick_thread(thread_id)
    recovered = store.session_tasks.require(task_id)
    assert recovered.status == "done" and model.calls == 2 and model.peak == 1
    assert (recovered.sender_thread_id, recovered.target_thread_id, recovered.body_guidance_id) == (
        task_before.sender_thread_id, task_before.target_thread_id, task_before.body_guidance_id)
    if action == "retire":
        finish_retirement(case)
    # 新存储实例再次观察和调度，终态与原handled证明不能倒退或再调模型。
    fresh = ConversationStore(store.storage.root)
    assert fresh.session_tasks.require(task_id).status == "done"
    scheduler.tick_thread(thread_id)
    assert model.calls == 2 and task_wakes(fresh, task_id) == []
    assert automatic_coverage == "complete", "healthy writable recovery kept unknown coverage"
    assert len(automatic) == 1, "explicit rebuild succeeded but automatic recovery stranded the queued task"
    assert automatic == [live[0].wake_signal_id]


def test_complete_responsibility_recovery_keeps_point_access(tmp_path, monkeypatch):
    from agent_py_agent.tests.test_v14_w5_background_wakes import (
        responsibility_raise,
        responsibility_report,
        responsibility_store,
    )

    store = responsibility_store(tmp_path)
    for number in range(80):
        signal = responsibility_raise(store, f"history-{number}", task=f"stask-old-{number}")
        store.wakes.mark_handled(signal.wake_signal_id)
    live = responsibility_raise(store)
    reads, read_text = [], Path.read_text

    def read(path, *args, **kwargs):
        reads.append(path)
        return read_text(path, *args, **kwargs)

    def no_scan(*args, **kwargs):
        raise AssertionError("complete point access must not rebuild or scan canonical history")

    before = _fd_count()
    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", read)
        patch.setattr(Path, "glob", no_scan)
        patch.setattr(Path, "rglob", no_scan)
        patch.setattr(publication, "_rebuild_task_index_locked", no_scan)
        store.wakes.recover_task_responsibility_index(blocking=False)
        rows, error = responsibility_report(store)
    assert error is None and [row["wake_signal_id"] for row in rows] == [live.wake_signal_id]
    assert reads == [store.storage.wake_dedupe_path(live.thread_id, live.dedupe_key), store.storage.wake_signal_path(live)]
    assert _fd_count() == before
    with closing(publication._task_index_connection(store.storage)) as connection:
        assert connection.execute("SELECT count(*) FROM locators").fetchone()[0] == 1


@pytest.mark.parametrize("action", ["resume", "restore", "retire"])
def test_unfinished_recovery_is_visible_and_same_wire_request_can_retry(business_wire, monkeypatch, action):
    case = runtime_case(business_wire)
    host, client, _sid, view, model, scheduler = case
    store, thread_id = host.conversation_store, view["agent_id"]
    record, proof = _unknown_after_normal_turn(case)
    task_id, revision = _queued_without_wake(case, action)
    params = {"agent_id": thread_id, **({"disposition": "drain"} if action == "retire" else {})}
    request = client.prepare("background_agents." + action, params, expected_revision=revision)
    failures, read_text = [], Path.read_text

    def read(path, *args, **kwargs):
        if path == record:
            failures.append(path)
            raise OSError(errno.EIO, "simulated I/O error")
        return read_text(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", read)
        result = client.send(request)
    assert result["ok"] is False and result["http_status"] == 503, result
    assert result["error"]["code"] == "OUTCOME_UNKNOWN"
    assert failures == [record]
    task = store.session_tasks.require(task_id)
    assert task.status == "queued" and store.guidance.receipt(task.body_dedupe_key).status == "pending"
    assert task_wakes(store, task_id) == [] and model.calls == 1
    identity = store.threads.require(thread_id).metadata["background_agent"]
    replay = client.send(request)
    assert replay["http_status"] == 200 and replay["state"] == "completed", replay
    assert store.threads.require(thread_id).metadata["background_agent"] == identity
    assert publication._task_index_coverage(store.storage) == "complete"
    assert len(task_wakes(store, task_id)) == 1 and record.read_bytes() == proof
    scheduler.tick_thread(thread_id)
    assert store.session_tasks.require(task_id).status == "done" and model.calls == 2 and model.peak == 1
    if action == "retire":
        finish_retirement(case)


def _unknown_store(tmp_path):
    root, live = _legacy_root(tmp_path)
    store = ConversationStore(root, initialize=False)
    _w5_unknown_index(store.storage)
    return store, live


def test_concurrent_recovery_and_nonblocking_contention_close_all_handles(tmp_path, monkeypatch):
    store, live = _unknown_store(tmp_path)
    record = store.storage.wake_dedupe_path(live.thread_id, live.dedupe_key)
    reading, release, competing = Event(), Event(), Event()
    read_text, failures, builds = Path.read_text, [], []
    rebuild = publication._rebuild_task_index_locked

    def counted(storage):
        builds.append(storage.root)
        return rebuild(storage)

    def read(path, *args, **kwargs):
        if path == record:
            reading.set()
            assert release.wait(5), "test did not release canonical read"
        return read_text(path, *args, **kwargs)

    def recover(*, competitor=False):
        try:
            if competitor:
                competing.set()
            store.wakes.recover_task_responsibility_index()
        except BaseException as exc:  # noqa: BLE001 - 在线程收口后交回主线程
            failures.append(exc)

    before = _fd_count()
    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", read)
        patch.setattr(publication, "_rebuild_task_index_locked", counted)
        first, second = Thread(target=recover, daemon=True), Thread(target=lambda: recover(competitor=True), daemon=True)
        first.start()
        try:
            assert reading.wait(5)
            # 先在竞争者尚未打开自己的FD时测非阻塞清理，避免把它的合法在途FD当成本次泄漏。
            busy_fds = _fd_count()
            with pytest.raises(BlockingIOError):
                store.wakes.recover_task_responsibility_index(blocking=False)
            assert _fd_count() == busy_fds
            second.start()
            assert competing.wait(5)
        finally:
            release.set()
            first.join(5)
            if second.ident is not None:
                second.join(5)
    assert not first.is_alive() and not second.is_alive() and not failures, failures
    assert builds == [store.storage.root]
    assert _fd_count() == before
    assert store.wakes.task_responsibility_report(live.thread_id, "stask-live") == (
        [{"wake_signal_id": live.wake_signal_id, "phase": "published"}], None)


@pytest.mark.parametrize("missing", [False, True], ids=["legacy-unknown", "missing-index"])
@pytest.mark.parametrize("interruption", [OSError(errno.EIO, "simulated I/O error"), KeyboardInterrupt()], ids=["EIO", "cancel"])
def test_failed_or_cancelled_migration_releases_retry_and_resources(tmp_path, monkeypatch, missing, interruption):
    store, live = _unknown_store(tmp_path)
    if missing:
        store.storage.wake_task_index_path.unlink()
    record = store.storage.wake_dedupe_path(live.thread_id, live.dedupe_key)
    read_text, failures = Path.read_text, []

    def read(path, *args, **kwargs):
        if path == record and not failures:
            failures.append(path)
            raise interruption
        return read_text(path, *args, **kwargs)

    before = _fd_count()
    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", read)
        if isinstance(interruption, OSError):
            # ensure_dirs保留非致命打开合同，读者仍明确unknown。
            ConversationStore(store.storage.root)
        else:
            with pytest.raises(KeyboardInterrupt):
                ConversationStore(store.storage.root)
    assert failures == [record] and publication._task_index_coverage(store.storage) == "unknown"
    assert store.wakes.task_responsibility_report(live.thread_id, "stask-live")[1] is not None
    assert _fd_count() == before
    ConversationStore(store.storage.root)
    assert publication._task_index_coverage(store.storage) == "complete"
    assert _fd_count() == before


def test_readonly_unknown_report_does_not_repair_or_mutate(tmp_path, monkeypatch):
    store, live = _unknown_store(tmp_path)
    before = {path: path.read_bytes() for path in store.storage.root.rglob("*") if path.is_file()}

    def no_scan(*args, **kwargs):
        raise AssertionError("read-only unknown coverage must not initiate writable recovery")

    fds = _fd_count()
    with monkeypatch.context() as patch:
        patch.setattr(publication, "_rebuild_task_index_locked", no_scan)
        patch.setattr(Path, "glob", no_scan)
        patch.setattr(Path, "rglob", no_scan)
        readonly = ConversationStore(store.storage.root, initialize=False)
        for _ in range(3):
            rows, error = readonly.wakes.task_responsibility_report(live.thread_id, "stask-live")
            assert rows == [] and error is not None
    assert _fd_count() == fds
    assert {path: path.read_bytes() for path in store.storage.root.rglob("*") if path.is_file()} == before


def test_recovery_rejects_shared_to_exclusive_upgrade(tmp_path):
    store, live = _unknown_store(tmp_path)
    before = _fd_count()
    with store.wakes.task_responsibility_guard(live.thread_id, "stask-live"):
        with pytest.raises(RuntimeError, match="task guard"):
            store.wakes.recover_task_responsibility_index(blocking=False)
    store.wakes.recover_task_responsibility_index()
    assert publication._task_index_coverage(store.storage) == "complete" and _fd_count() == before


def test_corrupt_source_throttle_is_once_per_index_generation(tmp_path, monkeypatch):
    store, live = _unknown_store(tmp_path)
    record = store.storage.wake_dedupe_path(live.thread_id, live.dedupe_key)
    proof = record.read_bytes()
    record.write_text("{", encoding="utf-8")
    rebuild, builds = publication._rebuild_task_index_locked, []

    def counted(storage):
        builds.append(storage.root)
        return rebuild(storage)

    before = _fd_count()
    with monkeypatch.context() as patch:
        patch.setattr(publication, "_rebuild_task_index_locked", counted)
        for _ in range(3):
            ConversationStore(store.storage.root)
        assert len(builds) == 1
        for _ in range(2):
            with pytest.raises(DataCorruptionError):
                store.wakes.recover_task_responsibility_index()
        assert len(builds) == 1 and record.read_text() == "{"
        assert store.wakes.task_responsibility_report(live.thread_id, "stask-live")[1] is not None
        # 显式原子重建是新代，不允许旧代的坏账节流阻断下一代健康迁移。
        record.write_bytes(proof)
        store.wakes.rebuild_task_responsibility_index()
        store.storage.wake_task_index_path.unlink()
        _w5_unknown_index(store.storage)
        ConversationStore(store.storage.root)
        assert len(builds) == 3 and publication._task_index_coverage(store.storage) == "complete"
    assert _fd_count() == before


def test_writable_recovery_preserves_original_io_cause(tmp_path, monkeypatch):
    store, live = _unknown_store(tmp_path)
    record = store.storage.wake_dedupe_path(live.thread_id, live.dedupe_key)
    read_text = Path.read_text
    error = OSError(errno.EIO, "simulated I/O error")

    def read(path, *args, **kwargs):
        if path == record:
            raise error
        return read_text(path, *args, **kwargs)

    before = _fd_count()
    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", read)
        with pytest.raises(DataCorruptionError) as caught:
            store.wakes.recover_task_responsibility_index()
    assert caught.value.__cause__ is error
    assert _fd_count() == before


@pytest.mark.parametrize("action", ["resume", "restore", "retire"])
def test_recovery_preserves_existing_live_wake_and_executes_once(business_wire, monkeypatch, action):
    case = runtime_case(business_wire)
    host, client, _sid, view, model, scheduler = case
    store, thread_id = host.conversation_store, view["agent_id"]
    record, _proof = _unknown_after_normal_turn(case)
    task_id, revision = _queued_without_wake(case, action)
    # 原真实队列保留的工作责任；恢复不能凭当前身份revision另发一条。
    live = store.wakes.raise_signal({"thread_id": thread_id, "reason": "session_task", "urgency": "normal",
        "dedupe_key": f"session_task:{task_id}:original", "retain_handled": True,
        "metadata": {"session_task_id": task_id}})
    _transient_migration(case, monkeypatch, record)
    _transition(client, thread_id, action, revision)
    assert task_wakes(store, task_id) == [live]
    scheduler.tick_thread(thread_id)
    assert store.session_tasks.require(task_id).status == "done" and model.calls == 2 and model.peak == 1
    assert store.storage.wake_handled_dir.joinpath(live.wake_signal_id + ".json").is_file()
    if action == "retire":
        finish_retirement(case)


def test_failed_rebuild_between_recovery_and_task_guard_is_not_silent_success(business_wire, monkeypatch):
    case = runtime_case(business_wire)
    host, client, _sid, view, model, scheduler = case
    store, thread_id = host.conversation_store, view["agent_id"]
    record, _proof = _unknown_after_normal_turn(case)
    task_id, revision = _queued_without_wake(case, "resume")
    store.wakes.recover_task_responsibility_index()
    request = client.prepare("background_agents.resume", {"agent_id": thread_id}, expected_revision=revision)
    reading, checked, release = Event(), Event(), Event()
    read_text, coverage = Path.read_text, publication._task_index_coverage
    rebuild_errors, results, failures = [], [], []

    def read(path, *args, **kwargs):
        if path == record and not reading.is_set():
            reading.set()
            assert release.wait(5)
            raise OSError(errno.EIO, "simulated I/O error")
        return read_text(path, *args, **kwargs)

    def observed(storage, **kwargs):
        value = coverage(storage, **kwargs)
        if storage.root == store.storage.root and value == "complete" and reading.is_set():
            checked.set()
        return value

    def rebuild():
        try:
            store.wakes.rebuild_task_responsibility_index()
        except DataCorruptionError as exc:
            rebuild_errors.append(exc)
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)

    def resume():
        try:
            results.append(client.send(request))
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", read)
        patch.setattr(publication, "_task_index_coverage", observed)
        writer, producer = Thread(target=rebuild, daemon=True), Thread(target=resume, daemon=True)
        writer.start()
        try:
            assert reading.wait(5)
            producer.start()
            assert checked.wait(5), "producer did not read old complete coverage while rebuild held index EX"
        finally:
            release.set()
            writer.join(5)
            if producer.ident is not None:
                producer.join(5)
    assert not writer.is_alive() and not producer.is_alive() and not failures, failures
    assert len(rebuild_errors) == 1 and isinstance(rebuild_errors[0].__cause__, OSError)
    assert len(results) == 1 and results[0]["ok"] is False and results[0]["http_status"] == 503, results
    assert store.session_tasks.require(task_id).status == "queued" and task_wakes(store, task_id) == []
    replay = client.send(request)
    assert replay["ok"] is True and replay["state"] == "completed", replay
    assert len(task_wakes(store, task_id)) == 1
    scheduler.tick_thread(thread_id)
    assert store.session_tasks.require(task_id).status == "done" and model.calls == 2 and model.peak == 1


@pytest.mark.parametrize("terminal", ["done", "failed", "cancelled"])
def test_terminal_transition_during_recovery_never_revives_task(business_wire, monkeypatch, terminal):
    case = runtime_case(business_wire)
    host, client, _sid, view, model, scheduler = case
    store, thread_id = host.conversation_store, view["agent_id"]
    record, _proof = _unknown_after_normal_turn(case)
    task_id, revision = _queued_without_wake(case, "resume")
    reading, release = Event(), Event()
    read_text, failures, results = Path.read_text, [], []

    def read(path, *args, **kwargs):
        if path == record and not reading.is_set():
            reading.set()
            assert release.wait(5)
        return read_text(path, *args, **kwargs)

    def resume():
        try:
            results.append(_transition(client, thread_id, "resume", revision))
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", read)
        worker = Thread(target=resume, daemon=True)
        worker.start()
        try:
            assert reading.wait(5)
            store.session_tasks.advance(task_id, SessionTaskUpdate(status="accepted"))
            ended = store.session_tasks.advance(task_id, SessionTaskUpdate(status=terminal))
        finally:
            release.set()
            worker.join(5)
    assert not worker.is_alive() and not failures and len(results) == 1, failures
    assert store.session_tasks.require(task_id).to_dict() == ended.to_dict()
    assert task_wakes(store, task_id) == []
    fresh = ConversationStore(store.storage.root)
    scheduler.tick_thread(thread_id)
    assert fresh.session_tasks.require(task_id).to_dict() == ended.to_dict() and model.calls == 1



# ==========================================================================
# Source: agent_py_agent/tests/test_v15_w11_background_agent_quota.py
# W11：额度沿canonical身份及跨进程门，重放不重新占槽。
# ==========================================================================

# LLM: 子进程只替换模型选择，不运行模型；真实Store、锁和null空间准入保持。
# 函数用途: 给并发进程提交一个后台身份并返回结构化失败码。
def _process_create(root, home, actor, key, start, output):
    class Service(BackgroundAgentService):
        # LLM: 测试不依赖模型凭据，身份创建没有模型执行。
        # 函数用途: 提供固定测试模型引用。
        def _profile(self, requested=None):
            return "model-A"

    host = SimpleNamespace(conversation_store=ConversationStore(root),
        home_paths=SimpleNamespace(owner_id="owner", owner_home_dir=home),
        config=SimpleNamespace(max_background_agents=2))
    start.wait(20)
    try:
        result = Service(host).create({"space_id": None, "name": key}, actor=actor, idempotency_key=key)
        output.put(("created", result["agent"]["agent_id"]))
    except Exception as error:
        output.put((getattr(error, "error_code", type(error).__name__), str(error)))


# LLM: 读取公开只读summary，不从列表页长推算额度。
# 函数用途: 取得当前Owner跨空间的身份额度。
def _summary(host, actor):
    from agent_py_agent.agent.conversation.background_agent_quota import (
        background_agent_quota_summary,
    )
    return background_agent_quota_summary(host, actor=actor)


# LLM: pytest仅构造配置并检查参数注册表，验证后台额度默认值、零值及非法类型/负数边界；不写部署配置或运行模型。
# 函数用途: 检查后台助手额度属于不可由普通用户改写的部署安全参数。
def test_config_default_validation_and_admin_boundary():
    from agent_py_agent.agent.settings.config import AgentConfig
    from agent_py_agent.agent.settings.parameter_registry import parameter_registry
    assert AgentConfig().max_background_agents == AgentConfig().max_subagents
    assert AgentConfig(max_background_agents=0).max_background_agents == 0
    for invalid in (-1, True, "8", 1.5):
        with pytest.raises((ValueError, TypeError)):
            AgentConfig(max_background_agents=invalid)
    spec = parameter_registry()["max_background_agents"]
    assert spec.tier == "deployment" and not spec.writable
    assert spec.safety == "boundary"


# LLM: pytest在临时宿主用线程池跨空间竞争同一Owner配额，并创建总控及重放成功请求；只写测试Store，断言超额拒绝、重放不重复占槽且总控不计数。
# 函数用途: 验证并发创建仍遵守跨空间统一额度，幂等重放不多建助手。
def test_atomic_thread_limit_replays_and_cross_space_usage(business_host):
    host, actor = business_host
    host.config.max_background_agents = 2
    service = BackgroundAgentService(host)

    # LLM: 供本测试线程池与重放循环共用；向临时服务提交稳定幂等键，只将准确额度拒绝及其详情转为None，其它失败不能冒充预期拒绝。
    # 函数用途: 尝试创建一个助手，并核对配额已满时的结构化错误。
    def create(index):
        try:
            return service.create({"space_id": "space-" + str(index), "name": "worker"},
                actor=actor, idempotency_key=str(index))
        except Exception as error:
            assert getattr(error, "error_code", None) == "BACKGROUND_AGENT_LIMIT_REACHED"
            assert error.details["used"] == error.details["limit"] == 2
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        replies = list(pool.map(create, range(8)))
    assert sum(reply is not None for reply in replies) == 2
    for index, result in enumerate(replies):
        if result:
            replay = create(index)
            assert replay["agent"]["agent_id"] == result["agent"]["agent_id"]
    service.ensure_controller(actor=actor)
    summary = _summary(host, actor)
    assert (summary["limit"], summary["used"], summary["available"], summary["coverage"]) == (2, 2, 0, "complete")


# LLM: pytest在临时Store创建、退休、恢复及挂起身份并调整内存额度；验证恢复需空槽、降限不杀既有身份、挂起仍计数及零限额与重放语义，不运行模型。
# 函数用途: 检查额度变化与身份生命周期之间的占槽和幂等规则。
def test_restore_checks_limit_and_lowering_never_kills_existing(business_host):
    host, actor = business_host
    host.config.max_background_agents = 2
    service = BackgroundAgentService(host)
    first = service.create({"space_id": "space-A", "name": "first"}, actor=actor, idempotency_key="a")["agent"]
    retired = service.transition(first["agent_id"], "retire", actor=actor,
        expected_revision=first["revision"], idempotency_key="retire")["agent"]
    assert retired["identity_state"] == "retired"
    second = service.create({"space_id": "space-B", "name": "second"}, actor=actor, idempotency_key="b")["agent"]
    third = service.create({"space_id": "space-C", "name": "third"}, actor=actor, idempotency_key="c")["agent"]
    host.config.max_background_agents = 1
    with pytest.raises(ValueError, match="BACKGROUND_AGENT_LIMIT_REACHED"):
        service.transition(first["agent_id"], "restore", actor=actor,
            expected_revision=retired["revision"], idempotency_key="restore")
    summary = _summary(host, actor)
    assert summary["used"] == 2 and summary["available"] == 0 and summary["over_limit"] is True
    assert service.get(second["agent_id"], actor=actor)["agent"]["identity_state"] == "available"
    suspended = service.transition(third["agent_id"], "suspend", actor=actor,
        expected_revision=third["revision"], idempotency_key="suspend")["agent"]
    assert _summary(host, actor)["used"] == 2
    service.transition(third["agent_id"], "resume", actor=actor,
        expected_revision=suspended["revision"], idempotency_key="resume")
    host.config.max_background_agents = 0
    restored = service.transition(first["agent_id"], "restore", actor=actor,
        expected_revision=retired["revision"], idempotency_key="restore")
    host.config.max_background_agents = 1
    replay = service.transition(first["agent_id"], "restore", actor=actor,
        expected_revision=retired["revision"], idempotency_key="restore")
    assert replay["agent"]["revision"] == restored["agent"]["revision"]


# LLM: pytest替换临时服务的索引读页，分别注入partial覆盖及加载错误；只检查配额summary保留None与真实覆盖，不把空页当Owner总量为零。
# 函数用途: 验证资料不完整或损坏时配额数量明确显示未知。
def test_unknown_or_partial_counts_never_claim_zero(business_host, monkeypatch):
    host, actor = business_host
    service = BackgroundAgentService(host)
    monkeypatch.setattr(service, "_index", lambda: SimpleNamespace(page=lambda **k: {
        "items": [], "next_cursor": None, "coverage": "partial", "load_errors": []}))
    result = service.quota_summary(actor=actor)
    assert result["coverage"] == "partial" and result["used"] is None and result["available"] is None
    monkeypatch.setattr(service, "_index", lambda: SimpleNamespace(page=lambda **k: {
        "items": [], "next_cursor": None, "coverage": "complete", "load_errors": [{"error": "unreadable"}]}))
    result = service.quota_summary(actor=actor)
    assert result["coverage"] == "unknown" and result["used"] is None


# LLM: pytest先在临时Store建立身份和额度索引，再让任何历史扫描直接失败；验证后续摘要沿活跃引用计数并拒绝外Owner，不启动任务。
# 函数用途: 检查额度摘要不反复扫历史名册，也不能跨用户读取。
def test_summary_uses_active_references_not_historical_listing(business_host, monkeypatch):
    host, actor = business_host
    service = BackgroundAgentService(host)
    service.create({"space_id": "space-A", "name": "worker"}, actor=actor, idempotency_key="a")
    assert _summary(host, actor)["used"] == 1
    monkeypatch.setattr(BackgroundAgentService, "_index", lambda self: pytest.fail("history scan after bootstrap"))
    assert _summary(host, actor)["used"] == 1
    with pytest.raises(PermissionError):
        _summary(host, replace(actor, owner_id="other"))


# LLM: pytest在同一临时Store启动spawn子进程同步竞争创建额度；子进程使用固定模型引用而不执行模型，汇总创建/拒绝结果并核退出码，覆盖跨进程锁边界。
# 函数用途: 验证多个进程同时创建助手时也不会突破统一额度。
def test_limit_is_cross_process_not_only_thread_lock(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    source = store.threads.create(owner_id="owner", canonical_user_id="local-agent")
    actor = HostChannelActor("owner", source.thread_id, "entry", "local", "local", "user")
    context = multiprocessing.get_context("spawn")
    start, output = context.Event(), context.Queue()
    processes = [context.Process(target=_process_create, args=(store.storage.root, tmp_path, actor, str(i), start, output))
                 for i in range(6)]
    for process in processes:
        process.start()
    start.set()
    results = [output.get(timeout=30) for _ in processes]
    for process in processes:
        process.join(30)
        assert process.exitcode == 0
    assert sum(code == "created" for code, _ in results) == 2, results
    assert sum(code == "BACKGROUND_AGENT_LIMIT_REACHED" for code, _ in results) == 4, results


# LLM: pytest在临时无归属身份上写入未完成任务使退休停在retiring，再故意损坏该canonical线程；断言责任未清仍占槽、损坏后摘要未知而非零。
# 函数用途: 检查退役中的助手继续计入额度，坏记录不能被当成空额度。
def test_retiring_is_counted_and_corrupt_canonical_never_becomes_zero(business_host):
    from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft

    host, actor = business_host
    host.config.max_background_agents = 1
    service = BackgroundAgentService(host)
    agent = service.create({"space_id": None, "name": "worker"}, actor=actor, idempotency_key="a")["agent"]
    host.conversation_store.session_tasks.create(SessionTaskDraft(actor.source_thread_id, agent["agent_id"], "pending"))
    retired = service.transition(agent["agent_id"], "retire", actor=actor,
        expected_revision=agent["revision"], idempotency_key="retire")["agent"]
    assert retired["identity_state"] == "retiring"
    assert _summary(host, actor)["unassigned"] == _summary(host, actor)["used"] == 1
    host.conversation_store.storage.thread_path(agent["agent_id"]).write_text("{bad", encoding="utf-8")
    summary = _summary(host, actor)
    assert summary["coverage"] == "unknown" and summary["used"] is None and summary["available"] is None


# LLM: pytest向临时Store写入一批普通历史线程并多次读取配额；断言首轮为partial/未知、后续有界回填推进到complete，不把中途未覆盖的记录判为零。
# 函数用途: 验证旧历史的额度索引分批补齐，而不是一次扫描全部记录。
def test_legacy_backfill_is_bounded_and_progresses_instead_of_scanning_all(business_host):
    host, actor = business_host
    for _ in range(140):
        host.conversation_store.threads.create(owner_id=actor.owner_id, canonical_user_id="local-agent")
    summary = _summary(host, actor)
    assert summary["coverage"] == "partial" and summary["used"] is None
    for _ in range(20):
        summary = _summary(host, actor)
        if summary["coverage"] == "complete":
            break
    assert summary["coverage"] == "complete" and summary["used"] == 0



# ==========================================================================
# Source: agent_py_agent/tests/test_v15_w11_unassigned_agents.py
# W11：真实null归属及身份/当前工作两个独立观察层。
# ==========================================================================

# LLM: pytest仅构造/恢复身份DTO并通过公开动作schema验证显式null参数；不认证、不创建线程或授予权限，覆盖create/list/assign/submit同一可空合同。
# 函数用途: 检查无归属身份和公开业务参数都能保留真正的None。
def test_null_identity_and_public_schema_roundtrip():
    from agent_py_agent.agent.plugin_host.business_action_specs import business_action_spec
    from agent_py_agent.agent.plugin_host.schemas import validate_object
    identity = BackgroundAgentIdentity("agent", None, None, None, "available", "reuse", 1)
    assert BackgroundAgentIdentity.from_dict(identity.to_dict()).space_id is None
    for action, params in (("background_agents.create", {"name": "worker", "space_id": None}),
            ("background_agents.list", {"space_id": None}),
            ("tasks.assign", {"agent_id": "agent", "space_id": None, "instruction": "work"}),
            ("tasks.submit", {"space_id": None, "instruction": "work"})):
        assert validate_object(params, business_action_spec(action).params_schema)["space_id"] is None


# LLM: pytest经工具入口及领域服务写入临时有空间/无归属身份，读取并区分省略、null和具体空间筛选，同时验证跨Owner及不可用空间拒绝；不运行模型。
# 函数用途: 检查无归属助手可创建和查询，且不会与有空间助手混淆。
def test_null_create_get_list_and_explicit_null_filter(business_host):
    host, actor = business_host
    service = BackgroundAgentService(host)
    root = service.create({"space_id": None, "name": "root worker"}, actor=actor, idempotency_key="null")["agent"]
    scoped = service.create({"space_id": "space-A", "name": "scoped"}, actor=actor, idempotency_key="scoped")["agent"]
    assert root["space_id"] is None
    assert service.get(root["agent_id"], actor=actor)["agent"]["space_id"] is None
    assert {row["agent_id"] for row in service.list({}, actor=actor)["items"]} == {root["agent_id"], scoped["agent_id"]}
    assert [row["agent_id"] for row in service.list({"space_id": None}, actor=actor)["items"]] == [root["agent_id"]]
    assert [row["agent_id"] for row in service.list({"space_id": "space-A"}, actor=actor)["items"]] == [scoped["agent_id"]]
    with pytest.raises(PermissionError):
        service.get(root["agent_id"], actor=replace(actor, owner_id="other"))
    with pytest.raises(PermissionError):
        service.create({"space_id": "denied", "name": "denied"}, actor=actor, idempotency_key="denied")


# LLM: pytest在临时无归属身份上实际派活并重放幂等键，核canonical上下文的null空间和准确task_id，再拒绝错空间；真实Store记录任务与唤醒，fixture禁止模型调用。
# 函数用途: 验证无归属派活保持原任务归属，重放不另建任务或借用默认空间。
def test_unassigned_task_binding_never_selects_a_default_space(business_host):
    host, actor = business_host
    service = BackgroundAgentService(host)
    agent = service.create({"space_id": None, "name": "worker"}, actor=actor, idempotency_key="a")["agent"]
    request = {"agent_id": agent["agent_id"], "space_id": None, "instruction": "work"}
    result = service.assign(request, actor=actor, idempotency_key="task")
    task = host.conversation_store.session_tasks.require(result["task_id"])
    assert task.metadata["business_context"]["space_id"] is None
    assert task.metadata["business_context"]["task_id"] == task.task_id
    assert service.assign(request, actor=actor, idempotency_key="task")["task_id"] == task.task_id
    with pytest.raises(ValueError, match="SPACE_MISMATCH"):
        service.assign({**request, "space_id": "space-A"}, actor=actor, idempotency_key="wrong")


# LLM: pytest在临时Store依次观察空闲身份、真实排队任务及注入的claim读错；要求工作态分别idle/waiting/unknown，查询不得推进队列或把available当running。
# 函数用途: 检查助手能接单与是否正在工作是两件独立的事。
def test_work_state_does_not_treat_available_as_running(business_host, monkeypatch):
    host, actor = business_host
    service = BackgroundAgentService(host)
    agent = service.create({"space_id": "space-A", "name": "worker"}, actor=actor, idempotency_key="a")["agent"]
    assert agent["identity_state"] == "available" and agent["work_state"] == "idle"
    task = host.conversation_store.session_tasks.create(SessionTaskDraft(actor.source_thread_id, agent["agent_id"], "queued"))
    observed = service.get(agent["agent_id"], actor=actor)["agent"]
    assert observed["work_state"] == "waiting"
    assert host.conversation_store.session_tasks.require(task.task_id).status == "queued"
    monkeypatch.setattr(service.store.claims, "load_report", lambda identifier: ({}, {"error": "unreadable"}))
    observed = service.list({}, actor=actor)["items"][0]
    assert observed["identity_state"] == "available" and observed["work_state"] == "unknown"


# LLM: pytest使用临时真实空间服务和可撤销授权回调；对照显式unassigned与旧默认选择，并核异Owner及撤权后复核拒绝，不建执行任务或调用模型。
# 函数用途: 确认即使只有一个空间，明确的无归属选择也不会自动挂到该空间。
def test_real_null_context_does_not_inherit_the_only_space(tmp_path):
    from agent_py_agent.agent.conversation.business_access import BusinessActor, BusinessError
    from agent_py_agent.agent.conversation.business_context import (
        resolve_business_context,
        revalidate_business_context,
    )
    from agent_py_agent.agent.conversation.business_spaces import BusinessSpaceService

    service = BusinessSpaceService(tmp_path / "owner", owner_id="owner", enabled=True)
    allowed = [True]
    actor = BusinessActor("owner", "user", service, lambda action, facts: allowed[0])
    sole = service.create({"name": "only space"}, actor=actor, idempotency_key="space")
    options = dict(actor=actor, task_id="stask-null", requested_space_id=None, project_ref=None, binding_id=None)
    unassigned = resolve_business_context(**options, unassigned=True)
    assert unassigned.space_id is None and unassigned.space_revision is None
    assert resolve_business_context(**options).space_id == sole["space_id"], "旧默认选择合同不改变"
    revalidate_business_context(actor=actor, context=unassigned)
    with pytest.raises(BusinessError, match="SPACE_SCOPE_MISMATCH"):
        revalidate_business_context(actor=actor, context=replace(unassigned, owner_id="other"))
    allowed[0] = False
    with pytest.raises(BusinessError, match="BUSINESS_ACCESS_DENIED"):
        revalidate_business_context(actor=actor, context=unassigned)


# LLM: pytest在临时Store/运行库建立准确任务、claim、run和attempt绑定；原入口激活同一pending attempt，核created父run的合法执行及终态拒绝，只写测试存储。
# LLM: 原任务用accepted表示受理，running仅属于claim与运行库；不扩展原SessionTask状态机。
# 函数用途: 检查运行中展示需要完整且仍有效的任务与执行证据。
def test_work_state_requires_exact_live_claim_task_and_runtime(business_host, tmp_path):
    from agent_py_agent.agent.conversation.business_contracts import (
        BusinessContextIntent,
        BusinessExecutionContext,
    )
    from agent_py_agent.agent.conversation.claim_execution_binding import (
        ClaimExecutionBinding,
        bind_claim_execution,
        publish_claim_runtime_authority,
    )
    from agent_py_agent.agent.conversation.session_tasks import (
        SESSION_TASK_ACCEPTED,
        SessionTaskUpdate,
    )
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository

    host, actor = business_host
    service = BackgroundAgentService(host)
    agent = service.create({"space_id": None, "name": "worker"}, actor=actor, idempotency_key="a")["agent"]
    store = host.conversation_store
    task = store.session_tasks.create(SessionTaskDraft(actor.source_thread_id, agent["agent_id"], "work"),
        business_context_intent=BusinessContextIntent(actor.owner_id, None, None, None),
        business_context_factory=lambda identifier: BusinessExecutionContext(actor.owner_id, None, None, identifier,
            None, None, (), (), "business-context.v1"))
    context = BusinessExecutionContext.from_dict(task.metadata["business_context"])
    repo = RuntimeRepository(tmp_path / "runtime.db")
    run = repo.record_run_creation(owner_id=actor.owner_id, conversation_task_id="runtime-root", run_id="exact-run",
        thread_id=agent["agent_id"], attempt_status="pending",
        task_run_metadata={"business_context": context.to_dict(), "session_task_ref": task.task_id})
    store.session_tasks.bind_execution(task.task_id, task_run_id=run["task_run_id"], target_thread_id=agent["agent_id"])
    store.session_tasks.advance(task.task_id, SessionTaskUpdate(status=SESSION_TASK_ACCEPTED, conversation_request_id="invocation-exact"))
    assert store.session_tasks.require(task.task_id).status == SESSION_TASK_ACCEPTED
    host.subagents = SimpleNamespace(runtime_db=repo)
    claim = store.claims.acquire({"thread_id": agent["agent_id"], "task_id": "runtime-root", "lease_seconds": 300})
    with bind_claim_execution(ClaimExecutionBinding(store.claims, agent["agent_id"], claim["claim_id"], agent["agent_id"])):
        publish_claim_runtime_authority({"invocation_run_id": "invocation-exact", "task_id": "runtime-root",
            "run_id": "exact-run", "agent_run_id": run["agent_run_id"], "attempt_id": run["attempt_id"]})
    assert repo.get_agent_run(run["agent_run_id"])["status"] == "created"
    assert repo.get_attempt(run["attempt_id"])["status"] == "pending"
    observed = service.get(agent["agent_id"], actor=actor)
    assert observed["agent"]["work_state"] == "unknown", "running claim不能把未启动的pending attempt当作执行存活"
    assert observed["current_execution"] is None
    # 沿真实runner入口激活准确current attempt；父run仍可为created，不靠手改父run制造执行证据。
    attempt = repo.create_attempt(run["agent_run_id"], reuse_pending=True, reject_running=True,
        expected_pending_attempt_id=run["attempt_id"])
    assert attempt["attempt_id"] == run["attempt_id"] and attempt["status"] == "running"
    assert repo.get_agent_run(run["agent_run_id"])["status"] == "created"
    observed = service.get(agent["agent_id"], actor=actor)
    assert observed["agent"]["work_state"] == "running"
    assert observed["current_execution"]["task_id"] == task.task_id
    assert observed["current_execution"]["run_id"] == "exact-run"
    assert observed["current_execution"]["attempt_id"] == run["attempt_id"]
    # 父run随后更新为running，精确执行关联仍保持不变。
    with repo.transaction() as connection:
        connection.execute("UPDATE agent_runs SET status='running' WHERE agent_run_id=?", (run["agent_run_id"],))
    observed = service.get(agent["agent_id"], actor=actor)
    assert observed["agent"]["work_state"] == "running"
    assert observed["current_execution"]["task_id"] == task.task_id
    with repo.transaction() as connection:
        connection.execute("UPDATE agent_runs SET status='cancelled' WHERE agent_run_id=?", (run["agent_run_id"],))
        connection.execute("UPDATE agent_attempts SET status='cancelled' WHERE attempt_id=?", (run["attempt_id"],))
    observed = service.get(agent["agent_id"], actor=actor)
    assert observed["agent"]["work_state"] == "unknown", "终态执行不能因旧running claim而继续显示运行"
    assert observed["current_execution"] is None


# LLM: pytest复用本地HTTP Gateway fixture，经真实业务客户端准备/发送请求；临时配置下核null身份、列表配额及超额409/not_started，不外推外网可达或模型执行。
# 函数用途: 检查无归属和额度合同能完整穿过客户端与Gateway运输层。
def test_null_dto_and_quota_roundtrip_through_real_client_and_gateway(native_http, tmp_path):
    from agent_py_agent.agent.conversation import ConversationStore
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
    from agent_py_agent.cli.chat_client_context import GatewayChatClientAgent
    from agent_py_agent.cli.space_commands import business_client_for_agent

    server, base = native_http
    base.config.model_backend = "echo"
    base.config.max_background_agents = 1
    base.conversation_store = ConversationStore(base.home_paths.owner_home_dir / "conversations")
    client_agent = GatewayChatClientAgent(SimpleNamespace(), SimpleNamespace(
        gateway_port=server.server.server_address[1], gateway_auth_token=""), tmp_path, [tmp_path],
        SimpleNamespace(root=base.home_paths.root), owner_identity=OwnerIdentity.local_main())
    client = business_client_for_agent(client_agent)
    created = client.send(client.prepare("background_agents.create", {"space_id": None, "name": "wire worker"}, idempotency_key="wire-null-create"))
    assert created["ok"] is True, created
    identifier = created["data"]["agent"]["agent_id"]
    assert created["data"]["agent"]["space_id"] is None
    listed = client.call("background_agents.list", {"space_id": None})
    assert listed["ok"] is True, listed
    assert [row["agent_id"] for row in listed["data"]["items"]] == [identifier]
    assert listed["data"]["quota"]["used"] == listed["data"]["quota"]["limit"] == 1
    refused = client.send(client.prepare("background_agents.create", {"space_id": None, "name": "over limit"}, idempotency_key="wire-null-refuse"))
    assert refused["http_status"] == 409 and refused["error"]["code"] == "BACKGROUND_AGENT_LIMIT_REACHED", refused
    assert refused["effect_outcome"] == "not_started"


# LLM: 通过正式消息入口追加真实guidance；get/list均须看到已受理待办，不能依赖不存在的SessionTask。
# 函数用途: 验证消息尚未消费时显示等待，而不是空闲。
@pytest.mark.parametrize("surface", ["get", "list"])
def test_work_state_waits_for_accepted_unconsumed_message(business_host, surface):
    host, actor = business_host
    service = BackgroundAgentService(host)
    agent = service.create({"space_id": "space-A", "name": "worker"}, actor=actor,
        idempotency_key="message-worker")["agent"]
    reply = queue_owned_background_message(
        host,
        actor=actor,
        target=service._thread(agent["agent_id"], actor),
        text="请处理此项工作",
        input_kind="message",
        operation_key=service._operation_key(actor, "pending-work"),
    )
    assert reply["receipt_state"] == "accepted"
    page = service.store.session_tasks.list_page(target_thread_id=agent["thread_id"], limit=100)
    assert page["coverage"] == "complete" and page["items"] == []
    pending, errors = service.store.guidance.pending_report("thread", agent["thread_id"], limit=100)
    assert not errors and len(pending) == 1
    observed = service.get(agent["agent_id"], actor=actor)["agent"] if surface == "get" else service.list({}, actor=actor)["items"][0]
    assert observed["identity_state"] == "available"
    assert observed["work_state"] == "waiting"


# LLM: 仅注入guidance读取的坏账/未覆盖报告；没有SessionTask不等于该来源已确认无待办。
# 函数用途: 验证消息观察失败或不完整时保持未知，不把它显示为空闲。
@pytest.mark.parametrize("surface", ["get", "list"])
@pytest.mark.parametrize("code", ["GUIDANCE_QUEUE_INVALID", "GUIDANCE_INDEX_INCOMPLETE"])
def test_work_state_keeps_unreadable_message_queue_unknown(business_host, monkeypatch, surface, code):
    host, actor = business_host
    service = BackgroundAgentService(host)
    agent = service.create({"space_id": "space-A", "name": "worker"}, actor=actor,
        idempotency_key="unreadable-worker")["agent"]
    observations = []

    # LLM: 保留精确目标和正数读取上限，并替换为真实报告形状；只用于模拟底层读取失败。
    # 函数用途: 记录有界消息查询，返回坏账而非伪造空队列成功。
    def unreadable(target_type, target_id, *, limit):
        observations.append((target_type, target_id, limit))
        return [], [{"code": code}]

    monkeypatch.setattr(service.store.guidance, "pending_report", unreadable)
    observed = service.get(agent["agent_id"], actor=actor)["agent"] if surface == "get" else service.list({}, actor=actor)["items"][0]
    assert observed["identity_state"] == "available"
    assert observed["work_state"] == "unknown"
    assert len(observations) == 1
    assert observations[0][:2] == ("thread", agent["thread_id"])
    assert 0 < observations[0][2] <= 100


# LLM: pytest经真实canonical创建口对照显式无归属、旧默认选择及Owner/任务/空间/项目/绑定拒绝；仅写临时Store，成功重放不得再调用factory。
# 函数用途: 验证允许null归属不会放松既有任务身份与冻结选择校验。
@pytest.mark.parametrize(
    "requested,selected,foreign_owner,wrong_task,project_ref,binding_id,rejection",
    [
        (None, None, False, False, None, None, None),
        (None, "space-A", False, False, None, None, None),
        ("space-A", "space-A", False, False, None, None, None),
        ("space-A", None, False, False, None, None, "owner/space mismatch"),
        ("space-A", "space-B", False, False, None, None, "selection conflicts"),
        (None, None, True, False, None, None, "owner/space mismatch"),
        (None, None, False, True, None, None, "task_id does not match"),
        ("space-A", "space-A", False, False, "foreign-project", None, "selection conflicts"),
        ("space-A", "space-A", False, False, None, "foreign-binding", "selection conflicts"),
    ],
)
def test_canonical_null_context_preserves_scope_and_idempotency_guards(
        business_host, requested, selected, foreign_owner, wrong_task, project_ref, binding_id, rejection):
    from agent_py_agent.agent.conversation.business_contracts import (
        BusinessContextIntent,
        BusinessExecutionContext,
    )

    host, actor = business_host
    target = BackgroundAgentService(host).create({"space_id": None, "name": "canonical-worker"},
        actor=actor, idempotency_key="canonical-worker")["agent"]
    store = host.conversation_store.session_tasks
    draft = SessionTaskDraft(actor.source_thread_id, target["thread_id"], "canonical-work",
        dedupe_key="canonical-work")
    intent = BusinessContextIntent(actor.owner_id, requested, None, None)
    calls = []

    # LLM: 只由真实创建器传入预留task_id；按参数构造正常或明确冲突的DTO并记录调用，不能替代canonical校验或创建任务。
    # 函数用途: 为边界用例提供可审计的冻结上下文。
    def factory(task_id):
        calls.append(task_id)
        return BusinessExecutionContext("foreign-owner" if foreign_owner else actor.owner_id,
            selected, project_ref, "foreign-task" if wrong_task else task_id,
            None if selected is None else 1, binding_id, (), (), "business-context.v1")

    try:
        task = store.create(draft, business_context_intent=intent, business_context_factory=factory)
    except ValueError as exc:
        assert rejection is not None, f"valid canonical context was rejected: {exc}"
        assert rejection in str(exc)
        assert store.list_page(target_thread_id=target["thread_id"], limit=100)["items"] == []
    else:
        assert rejection is None, "conflicting context was committed"
        assert task.metadata["business_context"]["space_id"] == selected
        assert task.metadata["business_context"]["task_id"] == task.task_id
        assert store.require(task.task_id).metadata == task.metadata
        replay = store.create(draft, business_context_intent=intent, business_context_factory=factory)
        assert replay.task_id == task.task_id and replay.metadata == task.metadata
        assert calls == [task.task_id]



# ==========================================================================
# Source: agent_py_agent/tests/test_v15_w12_compact_latency.py
# 验证摘要输出预算、缓存前缀和一次选区；模型调用只用本地替身。
# ==========================================================================

# LLM: 调用只记录实际请求选项，列表在浅拷贝后仍共用；不发 HTTP。
# 类用途: 观察压缩请求实际使用的输出上限。
class RecordingBackend:
    # LLM: 每个用例独立保存请求，主请求上限故意大于摘要上限。
    # 函数用途: 准备可观察的本地后端。
    def __init__(self, cap):
        self.max_tokens = cap
        self.calls = []

    # LLM: 保留真实辅助调用签名，返回纯文本，不实现任何工具执行。
    # 函数用途: 捕获实际下发的输出上限及工具面。
    def generate(self, prompt, *, messages=None, tools=None, tool_choice=None, on_chunk=None, request_options=None):
        cap = getattr(request_options, 'max_output_tokens', None) or self.max_tokens
        self.calls.append((cap, tools, tool_choice))
        return ModelResponse(text='已验证的事实摘要', backend='local')


# LLM: 必须验证供应商入口的实际参数，而非仅断言常量存在。
# 函数用途: 摘要封顶由统一可配置预留决定，不抬高较小上限或修改共享后端。
@pytest.mark.parametrize('normal,expected', [(16384, 16384), (1024, 1024)])
def test_summary_output_cap_is_request_local(normal, expected):
    request = _request('已完成的事实')
    backend = RecordingBackend(normal)
    agent = SimpleNamespace(backend=backend, config=SimpleNamespace(model_context_window_tokens=100000))
    response = budget.generate_bounded_compact_response(replace(request, agent=agent))
    assert response.text == '已验证的事实摘要'
    assert backend.calls[0][0] == expected
    assert backend.max_tokens == normal


# LLM: 冻结主请求选择策略及 schema，前台不能用另一个 auto/none 改写缓存面。
# 函数用途: 逐字段比较传入与发出的前台前缀。
@pytest.mark.parametrize('choice', [None, ToolChoice.auto('foreground'), ToolChoice.specific('read_file')])
def test_foreground_tools_choice_and_system_are_unchanged(monkeypatch, choice):
    request = replace(_request('按原样总结'), tool_choice=choice)
    original = deepcopy((request.tools, request.tool_choice, request.messages, request.system_instruction))
    calls = []
    monkeypatch.setattr(budget, 'generate_auxiliary_model_response', lambda r: calls.append(r) or ModelResponse(text='事实', backend='local'))
    budget.generate_bounded_compact_response(request)
    assert len(calls) == 1
    assert (calls[0].tools, calls[0].messages, calls[0].system_instruction) == (original[0], original[2], original[3])
    assert anthropic_tool_choice(calls[0].tool_choice or ToolChoice.auto()) == anthropic_tool_choice(choice or ToolChoice.auto())
    if choice is not None:
        assert calls[0].tool_choice == choice
    assert (request.tools, request.tool_choice, request.messages, request.system_instruction) == original


# LLM: 请求大小处于原始无工具预算之外，但冻结校准后可容纳，禁止误切分或丢工具。
# 函数用途: 工具调用响应只触发一次缓存安全修复，并使用校准计量。
def test_tool_reply_repair_uses_calibrated_same_prefix(monkeypatch):
    base = _request('')
    request = _request_between_budgets(base, budget.compact_summary_budget(base.agent), budget.compact_cache_surface_budget(base.agent))
    calls = []

    # LLM: 第一条结构化工具块是无效摘要，不得执行；第二条提供正文。
    # 函数用途: 捕获两次摘要请求并控制响应形状。
    def generate(candidate):
        calls.append(candidate)
        return ModelResponse(text='事实' if len(calls) > 1 else '', backend='local',
                             tool_use_blocks=[] if len(calls) > 1 else [{'id': 'unused', 'name': 'read_file', 'input': {}}])

    monkeypatch.setattr(budget, 'generate_auxiliary_model_response', generate)
    source = CompactMessageSource(lambda: iter(request.messages), preserve_prefix=True)
    result = budget.generate_bounded_compact_response(replace(request, messages=None), message_source=source, calibration=_calibration())
    assert result.text == '事实' and len(calls) == 2
    for candidate in calls:
        assert candidate.tools == request.tools
        assert anthropic_tool_choice(candidate.tool_choice or ToolChoice.auto()) == anthropic_tool_choice(request.tool_choice or ToolChoice.auto())
        assert candidate.messages == request.messages and candidate.system_instruction == request.system_instruction


# LLM: 闲置标志来自宿主调用，不从提示文案推断；通过真正摘要构造入口观察无工具请求。
# 函数用途: 闲置摘要允许冷请求，但不修改前台冻结面。
def test_idle_summary_drops_tools_only_for_idle_call(monkeypatch):
    surface = ConversationCompactProviderSurface(stable_prompt_prefix='稳定前缀', tools=({'name': 'read_file'},), system_instruction='原系统')
    agent = _request('').agent
    calls = []
    monkeypatch.setattr(budget, 'generate_auxiliary_model_response', lambda r: calls.append(r) or ModelResponse(text='事实', backend='local'))
    compact._summarize(agent, '', {}, [], call=compact._CompactSummaryCall(provider_surface=surface, idle_compact=True))
    assert len(calls) == 1 and calls[0].tools == [] and calls[0].tool_choice.mode == 'none'
    assert surface.tools == ({'name': 'read_file'},) and surface.system_instruction == '原系统'


# LLM: 指令只要求事实归档，仍保留完整用户要求、未完成请求和证据要求。
# 函数用途: 防止收紧摘要时丢掉必需章节。
def test_summary_instruction_is_direct_factual_archival():
    text = compact._conversation_summary_instruction({}, custom_instructions='', cache_safe=True)
    assert 'factual archival' in text
    assert 'meta commentary' in text
    assert 'User requirements' in text and 'Pending user requests' in text
    assert 'verified results' in text and 'Do not call tools' in text


# LLM: 真正 Store、候选、投影及 CAS 不替换；较软恢复目标不能导致第二次模型摘要。
# 函数用途: 在目标水位未达到但仍可容纳时只调用一次摘要模型。
def test_split_point_is_selected_before_the_single_summary_call(tmp_path, monkeypatch):
    agent, store, thread = _transcript_case(tmp_path, monkeypatch, turns=6)
    agent.backend.max_tokens = 64
    calls = []
    monkeypatch.setattr(budget, 'generate_auxiliary_model_response', lambda r: calls.append(r) or ModelResponse(text='真实已完成事实', backend='local'))
    surface = ConversationCompactProviderSurface('稳定前缀', (), '原系统')
    from agent_py_agent.agent.agent_core.model.context_pressure import model_request_input_ceiling
    from agent_py_agent.agent.agent_core.runtime.context_compactor import runtime_compact_policy

    policy = runtime_compact_policy(agent)
    ceiling = min(policy.trigger_tokens, model_request_input_ceiling(agent, policy.context_window_tokens))
    candidate_tokens = (policy.recovery_target_tokens + ceiling) // 2
    assert policy.recovery_target_tokens < candidate_tokens < ceiling

    # LLM: 该投影器是宿主的纯计量输入；候选落在软目标与硬上限之间。
    # 函数用途: 产生可提交但仍高于恢复目标的完整下一请求大小。
    def project(view):
        return ConversationCompactProjection(candidate_tokens if view.is_candidate else 12000, object())

    result = compact.prepare_conversation_context(agent, store, thread, options=compact.ConversationCompactOptions(
        current_prompt='继续原任务', provider_surface=surface, request_projector=project))
    assert result.compacted and len(calls) == 1
    assert result.projected_tokens == candidate_tokens
    assert store.threads.load(thread.thread_id).compact_generation == 1


# LLM: 用真实 Chat/Responses 序列化器，仅替换 HTTP 传输；比较前台载荷及有界摘要的真实字节字段。
# 函数用途: 统一摘要上限真正到达供应商载荷，工具、选择策略和历史前缀不变。
@pytest.mark.parametrize('provider,model,sequence_key,output_key', [
    ('openai_compatible', 'deepseek-v4-flash', 'messages', 'max_tokens'),
    ('openai_responses', 'gpt-6.1-sol', 'input', 'max_output_tokens'),
])
def test_real_provider_payload_has_cap_and_unchanged_foreground_prefix(provider, model, sequence_key, output_key):
    backend, agent, payloads = _capturing_backend(
        provider, model_name=model, api_base='https://fixture.invalid/v1', model_context_window_tokens=272000)
    normal_cap = backend.max_tokens
    request = replace(_compact_request(agent), tool_choice=ToolChoice.auto('foreground'))
    foreground = replace(request, purpose='auxiliary', prompt=CacheStructuredPrompt('稳定前缀', ''))
    generate_auxiliary_model_response(foreground)
    response = budget.generate_bounded_compact_response(request)
    assert response.text and len(payloads) == 2
    before, after = payloads
    from agent_py_agent.agent.agent_core.model.call_runtime import (
        compact_summary_output_reserve_tokens,
    )

    assert before[output_key] == normal_cap and after[output_key] == compact_summary_output_reserve_tokens(agent)
    assert backend.max_tokens == normal_cap
    assert after['tools'] == before['tools'] and after['tool_choice'] == before['tool_choice']
    assert after.get('instructions') == before.get('instructions')
    assert after[sequence_key][:len(before[sequence_key])] == before[sequence_key]
    assert after[sequence_key][-1]['role'] == 'user'
