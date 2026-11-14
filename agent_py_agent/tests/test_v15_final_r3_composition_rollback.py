"""真实资产链验证宿主组合写前拒绝及普通工作流历史 pin 恢复。"""
import json
import os

import pytest

from agent_py_agent.agent.assets.monitor_adapter import save_monitor_binding
from agent_py_agent.agent.assets.monitor_composition import save_monitor_composition
from agent_py_agent.agent.assets.revision_history import get_version, latest_version, list_versions
from agent_py_agent.agent.assets.workflow_store import WorkflowStore
from agent_py_agent.agent.tooling.scripts_tool import ScriptsTool
from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool
from agent_py_agent.agent.workflows.pinned_definitions import resolve_definition


# LLM: 测试只调用真实工具和真实临时 Store，失败回执不得冒充成功。
# 函数用途: 调用公开工具并返回其 JSON 回执。
def _call(tool, params):
    result = tool.execute(params)
    assert result.ok, result.output
    return json.loads(result.output)


# LLM: 所有输入都是公开工作流格式，不向历史或 Store 注入替身。
# 函数用途: 构造带真实脚本依赖的工作流。
def _workflow(identifier, body):
    return {'action': 'save', 'workflow_id': identifier, 'definition': {
        'descriptor': {'name': identifier, 'access_targets': [], 'data_sources': [],
                       'credential_refs': [], 'io_contract': {}},
        'body_markdown': body,
        'steps': [{'step_id': 'a', 'name': 'a', 'mode': 'deterministic_script', 'script_id': 's1'}]}}


# LLM: 真实发布 s1/s2、基础 v1/v2、组合及监控绑定；可选保留同 ID 的普通历史。
# 函数用途: 构造能复现 pin 冲突的完整资产链。
def _fixture(home, *, ordinary_history=False):
    scripts, tool, store = ScriptsTool(home), WorkflowsTool(home), WorkflowStore(home)
    for identifier in ('s1', 's2'):
        _call(scripts, {'action': 'register', 'script_id': identifier, 'code': 'print(1)\n',
                       'descriptor': {'name': identifier, 'access_targets': [], 'data_sources': [],
                                      'credential_refs': [], 'io_contract': {}}})
    _call(tool, _workflow('wf-base', 'base v1'))
    if ordinary_history:
        _call(tool, _workflow('monitor-m1', 'ordinary historical definition'))
        assert store.delete_workflow('monitor-m1')
    tail = [{'step_id': 'tail', 'name': 'tail', 'mode': 'deterministic_script', 'script_id': 's2'}]
    first = save_monitor_composition(home, 'm1', 'wf-base', 1, tail)
    binding = save_monitor_binding(home, 'm1', workflow_ids=['wf-base'], appended_steps=tail)
    assert binding['metadata']['composition']['base_version'] == 1
    for identifier in ('s1', 's2'):
        _call(scripts, {'action': 'update', 'script_id': identifier, 'code': 'print(2)\n',
                       'expected_revision': 1})
    _call(tool, {**_workflow('wf-base', 'base v2'), 'expected_revision': 1})
    second = save_monitor_composition(home, 'm1', 'wf-base', 2, tail)
    binding = save_monitor_binding(home, 'm1', pinned_version=2)
    assert second['version'] > first['version']
    assert binding['metadata']['composition']['base_version'] == 2
    assert latest_version(home, 'workflow', 'monitor-m1')['pins']['workflows'] == {'wf-base': {'version': 2}}
    return tool, store, tail


# LLM: 包括空目录、锁文件、canonical、历史、pending 和监控绑定，不忽略任何新增路径。
# 函数用途: 对整个测试 owner 做逐字节快照。
def _tree(home):
    return {str(path.relative_to(home)): None if path.is_dir() else path.read_bytes()
            for path in sorted(home.rglob('*'))}


# LLM: POSIX 计数真实进程描述符；Windows 计数 CRT 打开的描述符，不用不存在的 /proc 跳过断言。
# 函数用途: 取得成功和拒绝路径前后的实际 FD 数量。
def _fd_count():
    if os.name == 'nt':
        import ctypes
        import msvcrt

        count = 0
        for descriptor in range(ctypes.CDLL('ucrtbase')._getmaxstdio()):
            try:
                msvcrt.get_osfhandle(descriptor)
            except OSError:
                continue
            count += 1
        return count
    directory = '/proc/self/fd' if os.path.isdir('/proc/self/fd') else '/dev/fd'
    return len(os.listdir(directory))


# LLM: 三个真实状态分别命中历史 pin、删除后的历史 pin、当前组合 metadata；拒绝必须零落盘。
# 函数用途: 验证结构化拒绝、全盘字节不变、零 FD 增量及之后合法宿主重绑。
@pytest.mark.parametrize('state', ['composition', 'deleted_current', 'ordinary_history'])
def test_composition_rollback_is_rejected_without_mutation(tmp_path, state):
    home = tmp_path / 'owner'
    tool, store, tail = _fixture(home, ordinary_history=state == 'ordinary_history')
    current = store.get_workflow('monitor-m1')
    revision = current.descriptor.revision
    if state == 'deleted_current':
        assert store.delete_workflow('monitor-m1')
        revision = 0
    history = home / 'data/asset_evolution'
    assert (history / 'versions/workflow/monitor-m1.json').is_file()
    assert (home / 'data/monitors/m1.json').is_file()
    assert (home / 'workflows/monitor-m1').exists() == (state != 'deleted_current')
    before = _tree(home)
    descriptors = _fd_count()
    result = tool.execute({'action': 'rollback', 'workflow_id': 'monitor-m1',
                           'to_version': 1, 'expected_revision': revision})
    assert _fd_count() - descriptors == 0
    assert not result.ok, result.output
    assert result.effect_outcome == 'not_started'
    response = json.loads(result.output)
    assert response['error_code'] == 'ASSET_STORE_DIRECT_WRITE_DENIED'
    assert response['workflow_id'] == 'monitor-m1'
    assert response['reason'] == 'monitor_composition_host_managed'
    assert _tree(home) == before
    assert latest_version(home, 'workflow', 'monitor-m1')['pins']['workflows'] == {'wf-base': {'version': 2}}
    # 改变尾部名称，确保下面经过真实成功写盘，而不是只命中重复保存分支。
    changed_tail = [{**tail[0], 'name': 'updated tail'}]
    descriptors = _fd_count()
    saved = save_monitor_composition(home, 'm1', 'wf-base', 2, changed_tail)
    binding = save_monitor_binding(home, 'm1', pinned_version=2, appended_steps=changed_tail)
    assert _fd_count() - descriptors == 0
    assert binding['metadata']['composition']['base_version'] == 2
    assert store.get_workflow('monitor-m1').steps[-1].name == 'updated tail'
    definition, frozen = resolve_definition(home, 'monitor-m1', saved['version'])
    assert definition['descriptor']['id'] == 'monitor-m1'
    assert set(frozen) == {'a', 'tail'}
    assert latest_version(home, 'workflow', 'monitor-m1')['pins']['workflows'] == {'wf-base': {'version': 2}}


# LLM: 即使先继续调用宿主重绑，旧回滚也会暴露真实 pin 冲突，不能仅凭回执看似成功。
# 函数用途: 保留原缺陷的后续重绑反证链。
def test_refused_rollback_keeps_legitimate_rebinding_possible(tmp_path):
    home = tmp_path / 'owner'
    tool, store, tail = _fixture(home)
    revision = store.get_workflow('monitor-m1').descriptor.revision
    result = tool.execute({'action': 'rollback', 'workflow_id': 'monitor-m1',
                           'to_version': 1, 'expected_revision': revision})
    descriptors = _fd_count()
    saved = save_monitor_composition(home, 'm1', 'wf-base', 2, tail)
    binding = save_monitor_binding(home, 'm1', pinned_version=2)
    assert _fd_count() - descriptors == 0
    assert not result.ok, result.output
    assert binding['metadata']['composition']['base_version'] == 2
    assert get_version(home, 'workflow', 'monitor-m1', saved['version'])['pins']['workflows'] == {'wf-base': {'version': 2}}


# LLM: 对照仍走公开 rollback 与 resolve_definition；脚本当前版不同于回滚历史版。
# 函数用途: 验证普通工作流回滚追加版本并完整恢复历史 pins，成功路径 FD 增量也为零。
def test_ordinary_rollback_retains_historical_script_pins(tmp_path):
    home = tmp_path / 'owner'
    tool, store, _ = _fixture(home)
    selected = get_version(home, 'workflow', 'wf-base', 1)
    previous = latest_version(home, 'workflow', 'wf-base')
    assert selected['pins']['scripts']['s1']['code_sha256'] != previous['pins']['scripts']['s1']['code_sha256']
    descriptors = _fd_count()
    _call(tool, {'action': 'rollback', 'workflow_id': 'wf-base', 'to_version': 1,
                 'expected_revision': store.get_workflow('wf-base').descriptor.revision})
    assert _fd_count() - descriptors == 0
    restored = latest_version(home, 'workflow', 'wf-base')
    assert restored['version'] == previous['version'] + 1
    assert restored['restored_from'] == 1
    assert restored['pins'] == selected['pins']
    definition, frozen = resolve_definition(home, 'wf-base', restored['version'])
    assert definition['body_markdown'] == 'base v1'
    assert frozen['a']['version'] == selected['pins']['scripts']['s1']['version']
    assert frozen['a']['code_sha256'] == selected['pins']['scripts']['s1']['code_sha256']
    assert get_version(home, 'workflow', 'wf-base', 1) == selected
    assert [record['version'] for record in list_versions(home, 'workflow', 'wf-base')] == [3, 2, 1]
