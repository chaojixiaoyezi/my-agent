"""W6冻结代码：真实-I子进程与原runner准备链；不冒称原生平台沙箱验收。"""
import hashlib
import json
from pathlib import Path

import pytest

from agent_py_agent.agent.assets.descriptor import (
    AccessTarget,
    AssetDescriptor,
    CredentialBindingRef,
    DataSourceSpec,
)
from agent_py_agent.agent.assets.revision_history import (
    blob_get,
    get_version,
    record_version,
)
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.workflows import datasource_runtime as runtime
from agent_py_agent.agent.workflows import script_runner
from agent_py_agent.tests.test_v15_w6_harness_guardrails import CountingRunner, result

TRANSIENT_CODE = '''import json,os
print(json.dumps({"value":1,"stage":os.getcwd(),"source_report":{
    "stage":"probe+fetch","discovered_sources":["local-observation"],"sources":[{
        "source_id":"local-observation","status":"transient_error","hit_count":0,
        "http_status":None,"error_code":"ECONNRESET"}]}}))
'''


class CountingLocalRunner(script_runner.ScriptRunner):
    """每次计数都委托原runner执行真实本地进程；只关闭平台沙箱，不伪造结果。"""

    # LLM: 临时Owner内使用原准备、Python守卫、进程执行及清理链，不接外部服务。
    # 函数用途: 保存每次实际执行的合同、历史来源和原结果。
    def __init__(self, home):
        super().__init__(home, use_sandbox=False, restrict_to_roots=True)
        self.calls, self.results = [], []

    # LLM: 观察已绑定的真实Store快照，所有调用仍原样交给父类，无结果字典替身。
    # 函数用途: 计数并记录真实子进程使用的版本及重试许可。
    def run_script(self, script_id, inputs=None, *, timeout_seconds=None, override_data_sources=None,
                   network_access=None, record_summary=True):
        self.calls.append({'record': ScriptStore(self.owner_home).get_script(script_id),
            'history': runtime.script_history_record(self.owner_home, script_id, self.workspace_root),
            'record_summary': record_summary})
        value = super().run_script(script_id, inputs, timeout_seconds=timeout_seconds,
            override_data_sources=override_data_sources, network_access=network_access, record_summary=record_summary)
        self.results.append(value)
        return value


# LLM: 普通Store保存触发正式版本钩子；访问声明不是只读，重试许可只能来自显式幂等策略。
# 函数用途: 发布具有相同代码和执行引用、但重试策略可不同的本地脚本。
def save_transient_version(home, *, idempotent):
    descriptor = AssetDescriptor('probe', 'script', '本地瞬态报告',
        access_targets=(AccessTarget('local', 'file', 'result.json', extra={'read_only': False}),),
        metadata={'retry_policy': {'idempotent': idempotent}})
    return ScriptStore(home).save_script(descriptor, TRANSIENT_CODE, timeout_seconds=10)


# LLM: 编号pin必须点读完整历史；相同代码不允许复用当前重试策略，当前收窄也不能被历史放宽。
# 函数用途: 用真实runner对照编号来源和三种历史/当前策略交集。
@pytest.mark.parametrize('old_policy,current_policy,expected_calls', [(False, True, 1), (True, False, 1), (True, True, 3)])
def test_numbered_equal_code_pin_keeps_policy_intersection(tmp_path, old_policy, current_policy, expected_calls):
    first = save_transient_version(tmp_path, idempotent=old_policy)
    history = get_version(tmp_path, 'script', 'probe', 1)
    save_transient_version(tmp_path, idempotent=current_policy)
    runner = CountingLocalRunner(tmp_path)
    value = runtime.run_script_with_datasources(tmp_path, 'probe', runner=runner,
        expected_script_pin={**pin(first), 'version': 1, 'execution_definition_ref': history['execution_definition_ref']})
    assert value.ok, value.stderr_preview
    assert len(runner.calls) == expected_calls
    assert all(call['history']['version'] == 1 for call in runner.calls)
    assert value.source_check['pin_resolution']['version'] == 1
    assert len(value.source_check['query_health']['retries']) == expected_calls - 1
    assert all(call['record']['metadata']['retry_policy']['idempotent'] is (old_policy and current_policy)
               for call in runner.calls)
    assert not list((tmp_path / 'tmp/script_exec').iterdir())


# LLM: 旧无编号pin只在同存储revision时保留当前重试许可；未知来源仍允许首次执行但不得借新许可。
# 函数用途: 用真实进程验证legacy保守收窄与当前直接执行没有被一刀切禁重试。
@pytest.mark.parametrize('selection,expected_calls', [('legacy', 1), ('current', 3), ('unselected', 3)])
def test_equal_code_legacy_and_current_retry_controls(tmp_path, selection, expected_calls):
    first = save_transient_version(tmp_path, idempotent=False)
    current = save_transient_version(tmp_path, idempotent=True)
    expected = pin(first) if selection == 'legacy' else pin(current) if selection == 'current' else None
    runner = CountingLocalRunner(tmp_path)
    value = runtime.run_script_with_datasources(tmp_path, 'probe', runner=runner, expected_script_pin=expected)
    assert value.ok, value.stderr_preview
    assert len(runner.calls) == expected_calls
    assert all(call['history'] is None for call in runner.calls)
    assert value.source_check['pin_resolution'] == {'mode': 'current'}
    assert len(value.source_check['query_health']['retries']) == expected_calls - 1


# LLM: pin只取宿主已读取的代码身份，不从版本号或说明文本决定正文。
# 函数用途: 为一个脚本记录构造原形状的冻结pin。
def pin(record):
    return {'script_id': 'probe', 'revision': record['revision'],
            'code_sha256': hashlib.sha256(record['code'].encode()).hexdigest(),
            'source_scope': record.get('source_scope', 'owner')}


# LLM: 正式W1 API保存v1，当前Store另存v2；所有文件在本测试Owner中。
# 函数用途: 构造可证明执行版本的历史场景。
def versions(home, *, old=None, current=None, code=None, side_files=None):
    old = old or AssetDescriptor('probe', 'script', '版本查询',
        access_targets=(AccessTarget('local', 'file', 'input.json', extra={'read_only': True}),))
    store = ScriptStore(home)
    store.save_script(old, code or 'import os,json\nprint(json.dumps({"version":1,"stage":os.getcwd()}))')
    first = store.get_script('probe')
    history = record_version(home, 'script', 'probe', payload=first,
        code=first['code'].encode(), side_files=side_files)
    store.save_script(current or old, 'import json\nprint(json.dumps({"version":2}))')
    return store, first, history['record']


# LLM: 默认用原runner、真实隔离Python和原清理链，关闭OS沙箱仅供组件测试。
# 函数用途: 验证历史代码而非当前代码实际产出结果。
def test_historical_sha_executes_v1_and_removes_staging(tmp_path):
    store, first, history = versions(tmp_path)
    value = runtime.run_script_with_datasources(tmp_path, 'probe', expected_script_pin=pin(first),
        runner=script_runner.ScriptRunner(tmp_path, use_sandbox=False, restrict_to_roots=True))
    assert value.ok, value.stderr_preview
    assert value.output_data['version'] == 1
    assert value.source_check['pin_resolution'] == {'mode': 'history', 'version': history['version'],
        'code_sha256': pin(first)['code_sha256']}
    stage = Path(value.output_data['stage'])
    assert not stage.exists() and list((tmp_path / 'tmp/script_exec').iterdir()) == []
    assert json.loads(store.get_script('probe')['code'].split('print(json.dumps(')[1].split('))')[0]) == {'version': 2}


# LLM: 侧文件是历史快照的一部分；相对读取必须以历史代码目录为cwd。
# 函数用途: 验证W1 materialize侧文件没有丢失。
def test_historical_side_files_are_read_in_run_scoped_directory(tmp_path):
    code = 'import json\nfrom pathlib import Path\nprint(json.dumps({"side":Path("rows.json").read_text()}))'
    _, first, _ = versions(tmp_path, code=code, side_files={'rows.json': 'historical-rows'})
    value = runtime.run_script_with_datasources(tmp_path, 'probe', expected_script_pin=pin(first),
        runner=script_runner.ScriptRunner(tmp_path, use_sandbox=False, restrict_to_roots=True))
    assert value.ok and value.output_data == {'side': 'historical-rows'}
    assert not list((tmp_path / 'tmp/script_exec').iterdir())


# LLM: 任何必需blob缺失都不能回退执行v2。
# 函数用途: 验证代码、描述符、侧文件缺失的同一失败合同。
@pytest.mark.parametrize('missing', ['code_sha256', 'payload_blob', 'side_blob'])
def test_missing_history_blob_is_revision_drift_without_execution(tmp_path, missing):
    _, first, history = versions(tmp_path, side_files={'rows.json': 'historical-rows'})
    digest = history['side_blobs']['rows.json'] if missing == 'side_blob' else history[missing]
    (tmp_path / 'data/asset_evolution/blobs' / digest[:2] / digest).unlink()
    runner = CountingRunner([result()])
    value = runtime.run_script_with_datasources(tmp_path, 'probe', expected_script_pin=pin(first), runner=runner)
    assert value.error_code == 'SCRIPT_REVISION_DRIFT' and not value.ok
    assert 'pin_resolution' in value.source_check
    assert value.source_check['pin_resolution'] == {'mode': 'missing', 'reason': 'history_blob_missing'}
    assert not runner.calls


# LLM: 没有记录不能从当前代码或版本展示字段拼出历史。
# 函数用途: 验证历史索引不命中时零执行。
def test_missing_history_record_does_not_fall_back_to_current(tmp_path):
    _, first, _ = versions(tmp_path)
    runner = CountingRunner([result()])
    value = runtime.run_script_with_datasources(tmp_path, 'probe', runner=runner,
        expected_script_pin={**pin(first), 'code_sha256': 'f' * 64})
    assert value.error_code == 'SCRIPT_REVISION_DRIFT' and not runner.calls
    assert 'pin_resolution' in value.source_check and value.source_check['pin_resolution']['mode'] == 'missing'


# LLM: pin完全匹配时不经过历史回退，保持原执行路径。
# 函数用途: 验证当前代码匹配回执。
def test_matching_pin_uses_current_code(tmp_path):
    store, _, _ = versions(tmp_path)
    current = store.get_script('probe')
    value = runtime.run_script_with_datasources(tmp_path, 'probe', expected_script_pin=pin(current),
        runner=script_runner.ScriptRunner(tmp_path, use_sandbox=False))
    assert value.ok and value.output_data['version'] == 2
    assert 'pin_resolution' in value.source_check and value.source_check['pin_resolution']['mode'] == 'current'


# LLM: 当前disabled仍是硬门，历史enabled不能重新授权执行。
# 函数用途: 验证历史回退不能绕过停用。
def test_current_disabled_descriptor_still_blocks_history(tmp_path):
    current = AssetDescriptor('probe', 'script', '停用脚本', enabled=False)
    _, first, _ = versions(tmp_path, current=current)
    value = runtime.run_script_with_datasources(tmp_path, 'probe', expected_script_pin=pin(first),
        runner=script_runner.ScriptRunner(tmp_path, use_sandbox=False))
    assert not value.ok and value.error_code == 'ASSET_DISABLED_BY_OWNER'
    assert 'pin_resolution' in value.source_check and value.source_check['pin_resolution']['mode'] == 'history'


# LLM: 原Python守卫按当前宿主边界拒绝Owner私有读，历史代码没有额外读根。
# 函数用途: 验证历史执行仍受当前读取范围限制。
def test_history_keeps_current_filesystem_boundary(tmp_path):
    secret = tmp_path / 'private.txt'
    code = '''import json,sys
from pathlib import Path
inputs=json.load(sys.stdin)
try:
    Path(inputs["path"]).read_text()
    denied=False
except PermissionError:
    denied=True
print(json.dumps({"version":1,"denied":denied}))
'''
    _, first, _ = versions(tmp_path, code=code)
    secret.write_text('synthetic-private-value')
    value = runtime.run_script_with_datasources(tmp_path, 'probe', {'path': str(secret)},
        expected_script_pin=pin(first), runner=script_runner.ScriptRunner(tmp_path,
            use_sandbox=False, restrict_to_roots=True))
    assert value.ok and value.output_data == {'version': 1, 'denied': True}


# LLM: 历史权限与当前权限取交集；空目标交集不能触发旧runner的默认联网。
# 函数用途: 验证目标、凭据和源访问级别不扩大。
def test_history_intersects_current_permission_contract(tmp_path):
    from agent_py_agent.agent.assets.datasource_store import DatasourceStore

    for identity in ('events-a', 'events-b', 'events-c'):
        DatasourceStore(tmp_path).save_datasource({'datasource_id': identity, 'kind': 'custom', 'title': identity})
    old = AssetDescriptor('probe', 'script', '旧范围',
        access_targets=(AccessTarget('api', 'http', 'https://example.invalid', method='GET'),),
        data_sources=(DataSourceSpec('events-a', 'custom', query_scope={'access': 'read_write'}),
                      DataSourceSpec('events-b', 'custom')),
        credential_refs=(CredentialBindingRef('cref-old'), CredentialBindingRef('cref-shared')))
    current = AssetDescriptor('probe', 'script', '新范围',
        access_targets=(AccessTarget('local', 'file', 'input.json', extra={'read_only': True}),),
        data_sources=(DataSourceSpec('events-a', 'custom', query_scope={'access': 'read'}),
                      DataSourceSpec('events-c', 'custom')),
        credential_refs=(CredentialBindingRef('cref-new'), CredentialBindingRef('cref-shared')))
    _, first, _ = versions(tmp_path, old=old, current=current)
    captured = []

    class CaptureRunner(CountingRunner):
        # LLM: 读取真正绑定到原ScriptStore的执行快照，不从传入pin自造结果。
        # 函数用途: 观察宿主实际下发的最小执行合同。
        def run_script(self, script_id, inputs=None, *, timeout_seconds=None, override_data_sources=None,
                       network_access=None, record_summary=True):
            captured.append((ScriptStore(tmp_path).get_script(script_id), network_access))
            return super().run_script(script_id, inputs, timeout_seconds=timeout_seconds,
                override_data_sources=override_data_sources, network_access=network_access, record_summary=record_summary)

    value = runtime.run_script_with_datasources(tmp_path, 'probe', expected_script_pin=pin(first),
        runner=CaptureRunner([result()]))
    assert value.ok and len(captured) == 1
    record, network = captured[0]
    assert network is False and record['access_targets'] == []
    assert [row['credential_ref'] for row in record['credential_refs']] == ['cref-shared']
    assert [(row['source_id'], row['query_scope']['access']) for row in record['data_sources']] == [('events-a', 'read')]


# LLM: 原runner的异常清理必须覆盖materialize创建的整个父目录。
# 函数用途: 验证进程准备后IO失败仍清理历史staging。
def test_historical_staging_is_removed_on_execution_error(tmp_path, monkeypatch):
    _, first, _ = versions(tmp_path)
    seen = []

    # LLM: 不启动进程，只在真实准备完成后模拟IO异常。
    # 函数用途: 观察真实执行目录并触发原错误收口。
    def unavailable(command, folder, environment, input_json, timeout, capture):
        seen.append(Path(folder))
        raise OSError('simulated I/O error')

    monkeypatch.setattr(script_runner, '_execute', unavailable)
    value = runtime.run_script_with_datasources(tmp_path, 'probe', expected_script_pin=pin(first),
        runner=script_runner.ScriptRunner(tmp_path, use_sandbox=False))
    assert not value.ok and len(seen) == 1
    assert not seen[0].exists() and not list((tmp_path / 'tmp/script_exec').iterdir())


# LLM: v2的幂等声明不证明v1代码安全，不能据此重复旧版本副作用。
# 函数用途: 验证历史执行不能借用当前版本新加的幂等标记。
def test_history_cannot_borrow_current_idempotent_policy(tmp_path, monkeypatch):
    targets = (AccessTarget('api', 'http', 'https://example.invalid', method='POST'),)
    old = AssetDescriptor('probe', 'script', '旧脚本', access_targets=targets,
        metadata={'retry_policy': {'idempotent': False}})
    current = AssetDescriptor('probe', 'script', '当前脚本', access_targets=targets,
        metadata={'retry_policy': {'idempotent': True}})
    _, first, _ = versions(tmp_path, old=old, current=current)
    runner = CountingRunner([result(error_code='ECONNRESET')])
    monkeypatch.setattr(runtime.time, 'sleep', lambda delay: None)
    runtime.run_script_with_datasources(tmp_path, 'probe', expected_script_pin=pin(first), runner=runner)
    assert len(runner.calls) == 1


# LLM: 原SDK拒绝的冲突/未知权限不能在历史合并时被洗成有效授权。
# 函数用途: 用真实历史接缝验证非法权限快照零执行且失败关闭。
@pytest.mark.parametrize('rows', [
    [{'source_id': 'events-a', 'id': 'other', 'access': 'read_write'}],
    [{'id': 'events-a', 'access': 'bogus'}],
    [{'id': 'events-a', 'access': 'read_write', 'query_scope': {'access': 'read'}}],
    [{'id': 'events-a', 'access': 'read'}, {'id': 'events-a', 'access': 'read_write'}],
    [{'id': 'events-a', 'access': 'read', 'query_scope': None}],
])
def test_invalid_history_permissions_are_not_laundered(tmp_path, rows):
    from agent_py_agent.agent.assets.datasource_store import DatasourceStore

    DatasourceStore(tmp_path).save_datasource({'datasource_id': 'events-a', 'kind': 'custom', 'title': 'events'})
    descriptor = AssetDescriptor('probe', 'script', '授权查询', data_sources=(
        DataSourceSpec('events-a', 'custom', query_scope={'access': 'read_write'}),))
    _, first, _ = versions(tmp_path, old=descriptor)
    record_version(tmp_path, 'script', 'probe', payload={**first, 'access_contract': {'data_sources': rows}},
        code=first['code'].encode())
    runner = CountingRunner([result()])
    value = runtime.run_script_with_datasources(tmp_path, 'probe', expected_script_pin=pin(first), runner=runner)
    assert not value.ok and value.error_code == 'SCRIPT_REVISION_DRIFT'
    assert value.source_check['pin_resolution']['mode'] == 'missing' and not runner.calls


# LLM: W1已把坏metadata归一为空；接缝不能借当前True补充历史幂等承诺，纯投影也须容错。
# 函数用途: 核实真实W1归一事实并验证坏类型不抛异常、不获得自动重试。
@pytest.mark.parametrize('bad_metadata', [None, ['invalid'], 'invalid'])
def test_malformed_historical_metadata_does_not_authorize_retry(tmp_path, bad_metadata):
    targets = (AccessTarget('api', 'http', 'https://example.invalid', method='POST'),)
    current = AssetDescriptor('probe', 'script', '当前脚本', access_targets=targets,
        metadata={'retry_policy': {'idempotent': True}})
    store, first, _ = versions(tmp_path, current=current)
    saved = record_version(tmp_path, 'script', 'probe', payload={**first, 'metadata': bad_metadata},
        code=first['code'].encode())
    assert json.loads(blob_get(tmp_path, saved['record']['payload_blob']))['metadata'] == {}
    effective, _ = runtime._historical_execution_record(store.get_script('probe'),
        {**first, 'metadata': bad_metadata}, first['code'].encode())
    assert effective['metadata']['retry_policy']['idempotent'] is False
    runner = CountingRunner([result(error_code='ECONNRESET')])
    value = runtime.run_script_with_datasources(tmp_path, 'probe', expected_script_pin=pin(first), runner=runner)
    assert not value.ok and value.error_code == 'ECONNRESET' and len(runner.calls) == 1
    assert value.source_check['pin_resolution']['mode'] == 'history'


# LLM: 当前可写、历史只读时必须改的是query_scope.access，不能制造SDK双层冲突。
# 函数用途: 用真实本地子进程证明反向权限收窄仍能执行且SDK仅获得read。
def test_history_narrowing_to_read_stays_valid_for_sdk(tmp_path):
    from agent_py_agent.agent.assets.datasource_store import DatasourceStore

    DatasourceStore(tmp_path).save_datasource({'datasource_id': 'events-a', 'kind': 'custom', 'title': 'events'})
    targets = (AccessTarget('local', 'file', 'input.json', extra={'read_only': True}),)
    old = AssetDescriptor('probe', 'script', '历史只读', access_targets=targets,
        data_sources=(DataSourceSpec('events-a', 'custom', query_scope={'access': 'read'}),))
    current = AssetDescriptor('probe', 'script', '当前可写', access_targets=targets,
        data_sources=(DataSourceSpec('events-a', 'custom', query_scope={'access': 'read_write'}),))
    code = '''import json
from agent_py_agent.agent.workflows.datasource_runtime import current_datasource_bindings
print(json.dumps({"version":1,"access":current_datasource_bindings()["events-a"]["access"]}))
'''
    _, first, _ = versions(tmp_path, old=old, current=current, code=code)
    value = runtime.run_script_with_datasources(tmp_path, 'probe', expected_script_pin=pin(first),
        runner=script_runner.ScriptRunner(tmp_path, use_sandbox=False, restrict_to_roots=True))
    assert value.ok, value.stderr_preview
    assert value.output_data == {'version': 1, 'access': 'read'}
    assert value.source_check['pin_resolution']['mode'] == 'history'
