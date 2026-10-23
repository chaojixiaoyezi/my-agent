# LLM: 合同与真实临时脚本验证分开，不把关闭沙箱的子进程当平台隔离验收。
# 模块用途: 回归输出非空、类型持久化、网络推导和共享解释器前缀。
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent import plugin_sandbox
from agent_py_agent.agent.assets import descriptor
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.workflows import script_runner


# LLM: 显式可空只豁免已存在字段的null，不能豁免字段缺失。
# 函数用途: 校验两种既有必选输出入口。
@pytest.mark.parametrize('field', ['outputs', 'expected_output_keys'])
def test_null_requires_explicit_contract(field):
    contract = {field: ['value']}
    assert script_runner.parse_script_output('{"value":null}', contract)[1] == 'SCRIPT_NULL_OUTPUT_VALUE'
    contract['nullable_outputs'] = ['value']
    assert script_runner.parse_script_output('{"value":null}', contract) == ({'value': None}, '')
    assert script_runner.parse_script_output('{}', contract)[1] == 'SCRIPT_MISSING_OUTPUT_KEYS'


# LLM: bool不能因Python子类关系冒充number或integer，不进行隐式转换。
# 函数用途: 验证JSON输出类型逐值严格校验。
@pytest.mark.parametrize(('name', 'value', 'valid'), [
    ('integer', 1, True), ('integer', True, False), ('integer', 1.0, False),
    ('number', 1.5, True), ('number', False, False), ('boolean', False, True),
    ('boolean', 0, False), ('string', 'text', True), ('string', 3, False),
    ('array', [], True), ('array', {}, False), ('object', {}, True),
    ('object', [], False), ('null', None, True), ('null', 0, False),
])
def test_json_output_types(name, value, valid):
    contract = {'outputs': ['value'], 'output_types': {'value': name},
                'nullable_outputs': ['value'] if name == 'null' else []}
    assert script_runner.parse_script_output(json.dumps({'value': value}), contract) == (
        {'value': value}, '' if valid else 'SCRIPT_OUTPUT_TYPE_MISMATCH')


# LLM: 非法声明不能静默忽略或拆字符串当字段列表。
# 函数用途: 校验输出合同形状和类型名。
@pytest.mark.parametrize('contract', [
    {'outputs': 'value'}, {'nullable_outputs': 'value'}, {'nullable_outputs': None},
    {'output_types': None}, {'output_types': []}, {'output_types': {'value': 'int'}},
    {'output_types': {'value': True}}, {'output_types': {1: 'string'}},
])
def test_bad_output_contract(contract):
    with pytest.raises(ValueError):
        script_runner.parse_script_output('{"value":1}', contract)


# LLM: 使用正式描述符保存并从真实子进程读回，禁止只修内部parser而丢失持久化合同。
# 函数用途: 验证输出类型与可空声明经Store保存后实际生效。
@pytest.mark.parametrize(('value', 'nullable', 'error'), [
    (None, [], 'SCRIPT_NULL_OUTPUT_VALUE'), (None, ['value'], ''),
    (3, [], 'SCRIPT_OUTPUT_TYPE_MISMATCH'), ('ok', [], ''),
])
def test_persisted_output_contract(tmp_path, value, nullable, error):
    asset = descriptor.AssetDescriptor.from_dict({
        'id': 'probe', 'kind': 'script', 'name': '合同',
        'io_contract': {'outputs': ['value'], 'nullable_outputs': nullable, 'output_types': {'value': 'string'}}})
    store = ScriptStore(tmp_path)
    store.save_script(asset, f'print({json.dumps({"value": value})!r})')
    saved = store.get_script('probe')['io_contract']
    assert saved['nullable_outputs'] == nullable and saved['output_types'] == {'value': 'string'}
    result = script_runner.ScriptRunner(tmp_path, use_sandbox=False).run_script('probe')
    assert result.exit_code == 0 and result.output_data == {'value': value}
    assert result.ok is (not error) and result.error_code == error


# LLM: type优先，kind只回退，分类不授予网络权限。
# 函数用途: 对比共享网络分类和runner入口。
@pytest.mark.parametrize(('target', 'expected'), [
    (descriptor.AccessTarget('db', 'tcp', 'db.internal:5432'), True),
    ({'type': 'TcP'}, True), ({'kind': 'tcp'}, True),
    ({'type': 'file', 'kind': 'tcp', 'endpoint': 'report.json'}, False),
    ({'type': 'file', 'endpoint': 'HTTPS://example.invalid/data'}, True),
    (SimpleNamespace(kind='tcp', endpoint='db:5432'), True),
])
def test_network_contract(target, expected):
    helper = getattr(descriptor, 'access_target_requires_network', None)
    assert callable(helper)
    assert helper(target) is expected
    assert script_runner._network_access({'access_targets': [target]}, None) is expected
    assert script_runner._network_access({'access_targets': [target]}, False) is False


# LLM: 插件保持严格目录就绪策略，runner保留跳过不存在目录的既有行为。
# 函数用途: 验证前缀解析为共享入口且不意外扩大插件安装根。
def test_python_prefix_contract(tmp_path, monkeypatch):
    shared = getattr(plugin_sandbox, 'resolve_python_runtime_prefixes', None)
    assert callable(shared) and getattr(script_runner, 'resolve_python_runtime_prefixes', None) is shared
    prefix, runtime = tmp_path/'prefix', tmp_path/'runtime'
    prefix.mkdir()
    (runtime/'bin').mkdir(parents=True)
    executable = runtime/'bin/python3'
    executable.write_text('placeholder')
    alias = tmp_path/'alias'
    alias.symlink_to(prefix, target_is_directory=True)
    monkeypatch.setattr(plugin_sandbox.sys, 'prefix', str(alias))
    monkeypatch.setattr(plugin_sandbox.sys, 'base_prefix', str(prefix))
    monkeypatch.setattr(plugin_sandbox.sys, 'executable', str(executable))
    assert shared() == (alias.absolute(), prefix.resolve())
    assert runtime.resolve() not in shared()
    selected, roots = script_runner._interpreter_runtime('python3')
    assert Path(selected) == executable and runtime.resolve() in roots
