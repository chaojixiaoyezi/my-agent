"""真实stage/store/history上的依赖回放；执行器使用受控内存样本，不声称OS沙箱端到端。"""
import hashlib
import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.assets.descriptor import AccessTarget, AssetDescriptor, IOContract
from agent_py_agent.agent.assets.revision_history import (
    latest_version,
    read_compat_edges,
    record_version,
)
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.capability import asset_evolution_routes as routes
from agent_py_agent.agent.capability.asset_evolution_replay import (
    evolution_contract_matches,
    evolution_descriptor,
    replay_evolution_candidate,
)
from agent_py_agent.agent.workflows.models import StepExecutionRecord


# LLM: 只执行测试内自建代码；真实staging权限/清理和store协议仍照常运行。
# 类用途: 在进程内验证候选行为，避免测试依赖本机sandbox后端。
class InProcessReplayRunner:
    # LLM: staging与sandbox配置必须由产品传入，不接受关闭隔离的测试捷径。
    # 函数用途: 绑定临时store。
    def __init__(self, home, *, use_sandbox, restrict_to_roots):
        assert use_sandbox and restrict_to_roots
        self.store = ScriptStore(home)

    # LLM: 代码仅为本测试fixture；不载入宿主文件、凭据或网络。
    # 函数用途: 执行测试函数main并保留真实返回值。
    def run_script(self, script_id, inputs):
        namespace = {'__name__': 'test_module'}
        try:
            exec(compile(self.store.get_script(script_id)['code'], '<fixture>', 'exec'), namespace)
            return SimpleNamespace(ok=True, output_data=namespace['main'](inputs), error_code='')
        except ValueError:
            return SimpleNamespace(ok=False, output_data={}, error_code='SCRIPT_OUTPUT_INVALID')


# LLM: W4尚未并入该分片基线；此fixture模拟它的store hook，历史协议仍调用真实W1，不改产品store。
# 函数用途: 为独立W7测试装配发布版本记录。
@pytest.fixture
def versioned_store(tmp_path, monkeypatch):
    original = ScriptStore.save_script

    # LLM: 显式保留store参数，测试不导入W4私有实现。
    # 函数用途: 在真实保存后记录真实版本。
    def save(self, descriptor, code, *, entrypoint='main.py', interpreter='python3', timeout_seconds=120,
             target_scope='owner', expected_revision=None):
        result = original(self, descriptor, code, entrypoint=entrypoint, interpreter=interpreter,
                          timeout_seconds=timeout_seconds, target_scope=target_scope, expected_revision=expected_revision)
        payload = evolution_descriptor(result).to_dict() | {key: result[key] for key in ('entrypoint', 'interpreter', 'timeout_seconds')}
        record_version(self.owner_home, 'script', descriptor.id, payload=payload, code=code.encode(),
                       storage_revision=result['revision'], execution_definition_ref=result['execution_definition_ref'])
        return result
    monkeypatch.setattr(ScriptStore, 'save_script', save)
    return ScriptStore(tmp_path), save


# LLM: input/output来自同一StepExecutionRecord，含敏感样本仅留临时工作区。
# 函数用途: 构造可回放成功记录。
def replay_sample(value=3):
    return StepExecutionRecord('step1', 'check', 'deterministic_script', 'succeeded', 1, 2, 1, False,
                               script_id='sample', input_snapshot={'x': value, 'private_input': 'sample-input-only'},
                               output_snapshot={'result': value + 1})


# LLM: 非网络脚本必须精确等价，不能只核存在某个输出键。
# 函数用途: 验证通过与输出变化的失败结论。
@pytest.mark.parametrize('offset,expected', [(1, 'passed'), (2, 'failed')])
def test_exact_replay(tmp_path, offset, expected):
    descriptor = AssetDescriptor('sample', 'script', 'sample', io_contract=IOContract(outputs=('result',), output_types={'result': 'integer'}))
    code = f'def main(inputs):\n    return {{"result": inputs["x"] + {offset}}}\n'
    verdict = replay_evolution_candidate(tmp_path, 'sample', code, descriptor, samples=[replay_sample()],
                                         has_dependents=True, runner_factory=InProcessReplayRunner)
    assert verdict['verdict'] == expected and verdict['replayed'] == 1
    assert 'sample-input-only' not in json.dumps(verdict)
    assert not list((tmp_path / 'data' / 'skill_learning').glob('_stage_*'))


# LLM: 网络依赖只做合同检查且保持inconclusive，不能用一份样本签兼容。
# 函数用途: 验证网络降为提案的硬门。
def test_network_contract_only(tmp_path):
    descriptor = AssetDescriptor('sample', 'script', 'sample',
        access_targets=(AccessTarget('local', 'http', 'https://example.invalid'),),
        io_contract=IOContract(outputs=('result',), output_types={'result': 'integer'}))
    verdict = replay_evolution_candidate(tmp_path, 'sample', 'pass\n', descriptor,
                                         samples=[replay_sample()], has_dependents=True)
    assert verdict['verdict'] == 'inconclusive' and verdict['replayed'] == 0
    assert evolution_contract_matches({'result': True}, descriptor.io_contract.to_dict()) is False


# LLM: 同一真实回放入口复用于Route1，只有autonomous且passed可以提交版本。
# 函数用途: 验证晋升、拒绝、observe和网络提案。
@pytest.mark.parametrize('mode,offset,network,expected', [('autonomous', 1, False, 'promoted'),
    ('autonomous', 2, False, 'proposal'), ('observe', 1, False, 'proposal'), ('autonomous', 1, True, 'proposal')])
def test_route1_promotion_gate(versioned_store, mode, offset, network, expected):
    store, _ = versioned_store
    descriptor = AssetDescriptor('sample', 'script', 'sample', io_contract=IOContract(outputs=('result',)),
        access_targets=(AccessTarget('local', 'http', 'https://example.invalid'),) if network else ())
    store.save_script(descriptor, 'def main(inputs):\n return {"result": inputs["x"] + 1}\n')
    before = store.get_script('sample')

    # LLM: 仅固定样本来源，回放比较、stage和store均用产品实现。
    # 函数用途: 给路由提供真实回放结果。
    def gate(home, script_id, code, desc, *, options=None):
        return replay_evolution_candidate(home, script_id, code, desc, options=options,
            samples=[replay_sample()], has_dependents=True, runner_factory=InProcessReplayRunner)
    code = f'def main(inputs):\n return {{"result": inputs["x"] + {offset}}}\n# checked\n'
    result = routes.route_existing_script(store.owner_home, 'sample', code, maintenance_mode=mode, replay=gate)
    assert result['status'] == expected
    after = store.get_script('sample')
    assert after['revision'] == before['revision'] + (expected == 'promoted')
    if expected == 'promoted':
        assert latest_version(store.owner_home, 'script', 'sample')['version'] == 2
        assert read_compat_edges(store.owner_home, 'script', 'sample')[0]['verdict'] == 'replay_passed'
    else:
        assert not read_compat_edges(store.owner_home, 'script', 'sample')


# LLM: CAS冲突模拟准确另一次store保存；必须再回放一次，第二次冲突只能留提案。
# 函数用途: 验证单次重放上限及最终base。
@pytest.mark.parametrize('conflict_count,expected,gates', [(1, 'promoted', 2), (2, 'proposal', 2)])
def test_cas_conflict_rereplays_once(versioned_store, monkeypatch, conflict_count, expected, gates):
    store, saved = versioned_store
    descriptor = AssetDescriptor('sample', 'script', 'sample')
    store.save_script(descriptor, 'def main(inputs):\n return {"result": 1}\n')
    calls, checks = [], []

    # LLM: 每次冲突都真的保存另一版本，不用假revision冒充并发。
    # 函数用途: 在第一次最终写入之前推进base。
    def conflict(self, desc, code, *, entrypoint='main.py', interpreter='python3', timeout_seconds=120,
                 target_scope='owner', expected_revision=None):
        if code.endswith('# candidate\n') and len(calls) < conflict_count:
            calls.append('conflict')
            current = self.get_script(desc.id)
            saved(self, desc, current['code'] + '# concurrent\n', expected_revision=current['revision'])
            raise ValueError('revision conflict')
        return saved(self, desc, code, entrypoint=entrypoint, interpreter=interpreter,
                     timeout_seconds=timeout_seconds, target_scope=target_scope, expected_revision=expected_revision)

    # LLM: 本用例关注CAS时序；精确回放比较由上面的参数表独立覆盖。
    # 函数用途: 记录每次重放的准确版本。
    def gate(home, script_id, code, desc, *, options=None):
        checks.append(store.get_script(script_id)['revision'])
        return {'verdict': 'passed', 'replayed': 1, 'hashes': []}
    monkeypatch.setattr(ScriptStore, 'save_script', conflict)
    result = routes.route_existing_script(store.owner_home, 'sample', 'def main(inputs):\n return {"result": 1}\n# candidate\n',
                                          maintenance_mode='autonomous', replay=gate)
    assert result['status'] == expected and len(checks) == gates
    assert checks[1] > checks[0]


# LLM: rollback只在当前仍是试用sha时发生；环境失败不能累计，旧内容通过store成为新版本。
# 函数用途: 验证两次失败恢复与环境故障排除。
def test_rollback_and_environment_failures(versioned_store):
    store, _ = versioned_store
    old_code, code = 'def main(inputs):\n return {"ok": True}\n', 'def main(inputs):\n return {"ok": True}\n# candidate\n'
    store.save_script(AssetDescriptor('sample', 'script', 'sample'), old_code)

    # LLM: 晋升前精确回放在其它用例验证，这里仅构造合法试用版本。
    # 函数用途: 签发一次受控passed回放。
    def gate(home, script_id, source, desc, *, options=None):
        return {'verdict': 'passed', 'replayed': 1, 'hashes': []}
    assert routes.route_existing_script(store.owner_home, 'sample', code, maintenance_mode='autonomous', replay=gate)['status'] == 'promoted'
    sha = hashlib.sha256(code.encode()).hexdigest()
    for index, error in enumerate(('SCRIPT_TIMEOUT', 'DATASOURCE_MISSING', 'SANDBOX_UNAVAILABLE', 'CREDENTIAL_EXPIRED')):
        result = routes.observe_evolution_trial(store.owner_home, 'sample', {'call_id': f'env{index}', 'sha256': sha, 'ok': False, 'error_code': error})
        assert result['status'] == 'trial' and result['failures'] == 0
    first = {'call_id': 'bug1', 'sha256': sha, 'ok': False, 'error_code': 'SCRIPT_OUTPUT_INVALID'}
    assert routes.observe_evolution_trial(store.owner_home, 'sample', first)['failures'] == 1
    assert routes.observe_evolution_trial(store.owner_home, 'sample', first) is None
    assert routes.observe_evolution_trial(store.owner_home, 'sample', {**first, 'call_id': 'bug2'})['status'] == 'rolled_back'
    assert store.get_script('sample')['code'] == old_code
    assert latest_version(store.owner_home, 'script', 'sample')['version'] == 3
    assert [edge['verdict'] for edge in read_compat_edges(store.owner_home, 'script', 'sample')] == ['replay_passed', 'replay_failed']


@pytest.fixture
def active_trial(versioned_store):
    """沿真实Store和晋升路由建立试用，回放算法由现有精确比较用例覆盖。"""
    store, _ = versioned_store
    descriptor = AssetDescriptor('sample', 'script', 'sample')
    store.save_script(descriptor, 'def main(inputs):\n return {"ok": True}\n')
    code = 'def main(inputs):\n return {"ok": True}\n# candidate\n'

    def passed(home, script_id, source, desc, *, options=None):
        return {'verdict': 'passed', 'replayed': 1, 'hashes': []}

    result = routes.route_existing_script(store.owner_home, 'sample', code,
        maintenance_mode='autonomous', replay=passed)
    assert result['status'] == 'promoted'
    current = store.get_script('sample')
    execution = {'call_id': 'trial-call', 'sha256': hashlib.sha256(code.encode()).hexdigest(),
                 'ok': False, 'error_code': 'SCRIPT_OUTPUT_INVALID'}
    return store, current, execution, result['version']


@pytest.mark.parametrize('version_mode', ['omitted', 'null', 'current'])
@pytest.mark.parametrize('captured', [False, True])
def test_trial_optional_version_accepts_matching_sha(active_trial, version_mode, captured):
    store, current, execution, version = active_trial
    if version_mode != 'omitted':
        execution['version'] = None if version_mode == 'null' else version
    if captured:
        execution.update(verified=True, source_scope='owner',
            execution_definition_ref=current['execution_definition_ref'],
            published_definition_ref=current['execution_definition_ref'], requested_version=None)
    result = routes.observe_evolution_trial(store.owner_home, 'sample', execution)
    assert result == {'status': 'trial', 'failures': 1}
    assert routes.observe_evolution_trial(store.owner_home, 'sample', execution) is None
    trial = routes.read_evolution_json(store.owner_home,
        ('data', 'asset_evolution', 'trials', 'sample.json'))
    assert trial['failures'] == 1 and trial['seen_calls'] == ['trial-call']
    assert store.get_script('sample') == current


@pytest.mark.parametrize('version', [0, 1, 3, '2'])
@pytest.mark.parametrize('captured', [False, True])
def test_explicit_stale_trial_version_never_counts_same_sha(active_trial, version, captured):
    store, current, execution, _ = active_trial
    execution['version'] = version
    if captured:
        execution.update(verified=True, source_scope='owner',
            execution_definition_ref=current['execution_definition_ref'],
            published_definition_ref=current['execution_definition_ref'], requested_version=None)
    assert routes.observe_evolution_trial(store.owner_home, 'sample', execution) is None
    trial = routes.read_evolution_json(store.owner_home,
        ('data', 'asset_evolution', 'trials', 'sample.json'))
    assert trial['active'] is True and trial['failures'] == 0 and trial['seen_calls'] == []
    assert store.get_script('sample') == current


@pytest.mark.parametrize('identity', [
    {'schema_version': 'asset_execution.v1'}, {'verified': False}, {'verified': True},
    {'source_scope': 'workspace'}, {'execution_definition_ref': 'different'},
    {'published_definition_ref': 'different'},
])
def test_partial_or_unverified_capture_cannot_become_sha_only(active_trial, identity):
    store, current, execution, _ = active_trial
    execution.update(identity)
    assert routes.observe_evolution_trial(store.owner_home, 'sample', execution) is None
    trial = routes.read_evolution_json(store.owner_home,
        ('data', 'asset_evolution', 'trials', 'sample.json'))
    assert trial['failures'] == 0 and trial['seen_calls'] == []
    assert store.get_script('sample') == current


@pytest.mark.parametrize('change', ['code', 'summary'])
def test_sha_only_receipt_does_not_rollback_newer_store_revision(active_trial, change):
    store, current, execution, _ = active_trial
    descriptor = evolution_descriptor(current).to_dict()
    descriptor['summary'] = 'newer summary' if change == 'summary' else current['summary']
    code = current['code'] + '# newer\n' if change == 'code' else current['code']
    newer = store.save_script(AssetDescriptor.from_dict(descriptor), code,
                             expected_revision=current['revision'])
    assert routes.observe_evolution_trial(store.owner_home, 'sample', execution) == {'status': 'superseded'}
    assert routes.observe_evolution_trial(store.owner_home, 'sample',
        {**execution, 'call_id': 'second-call'}) is None
    trial = routes.read_evolution_json(store.owner_home,
        ('data', 'asset_evolution', 'trials', 'sample.json'))
    assert trial['active'] is False and trial['failures'] == 0 and trial['seen_calls'] == []
    assert store.get_script('sample') == newer
