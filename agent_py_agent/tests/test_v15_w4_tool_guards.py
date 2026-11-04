"""通过工具入口验证CAS和宿主元数据边界。"""
import json

import pytest

from agent_py_agent.agent.assets.asset_metadata import HOST_MANAGED_METADATA_KEYS
from agent_py_agent.agent.tooling.scripts_tool import ScriptsTool
from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool
from agent_py_agent.tests.test_v15_w4_tool_surface import call, script_params, workflow_params


# LLM: G3从模型请求剥离，可信Store写者不受模型参数影响。
# 函数用途: 验证strip回执和source_system保留。
@pytest.mark.parametrize('kind', ['script', 'workflow'])
def test_host_metadata_is_stripped_from_model_saves(tmp_path, kind):
    metadata = dict.fromkeys(HOST_MANAGED_METADATA_KEYS, 'supplied')
    metadata['source_system'] = 'source-a'
    tool = ScriptsTool(tmp_path) if kind == 'script' else WorkflowsTool(tmp_path)
    data = call(tool, script_params(metadata=metadata) if kind == 'script' else workflow_params(metadata=metadata))
    assert data['stripped_metadata_keys'] == sorted(HOST_MANAGED_METADATA_KEYS)
    descriptor = data if kind == 'script' else data['descriptor']
    assert not (set(descriptor['metadata']) & set(HOST_MANAGED_METADATA_KEYS))
    assert descriptor['metadata']['source_system'] == 'source-a'


# LLM: 两次持同一revision更新必须依次成功/冲突，不能因为G1而跳过原Store的CAS。
# 函数用途: 验证共享脚本更新缺revision拒绝及实际冲突。
def test_dependent_script_requires_revision_and_preserves_cas(tmp_path):
    scripts, workflows = ScriptsTool(tmp_path), WorkflowsTool(tmp_path)
    call(scripts, script_params())
    call(workflows, workflow_params())
    result = scripts.execute({'action': 'update', 'script_id': 'demo', 'code': 'print(2)\n'})
    assert not result.ok
    data = json.loads(result.output)
    assert data['error_code'] == 'ASSET_REVISION_REQUIRED'
    assert data['current_revision'] == 1
    assert {'kind': 'workflow', 'id': 'flow'} in data['dependents']
    call(scripts, {'action': 'update', 'script_id': 'demo', 'code': 'print(2)\n', 'expected_revision': 1})
    stale = scripts.execute({'action': 'update', 'script_id': 'demo', 'code': 'print(3)\n', 'expected_revision': 1})
    assert not stale.ok and json.loads(stale.output)['error_code'] == 'ASSET_REVISION_CONFLICT'
    assert scripts._store().get_script('demo')['code'] == 'print(2)\n'


# LLM: 注册表只归W12；行为断言与注册断言分开，不能由前一个失败遮住CAS验证。
# 函数用途: 显式保留两个待集成错误码的注册门。
@pytest.mark.parametrize('code', ['ASSET_REVISION_REQUIRED', 'ASSET_DRAFT_NOT_FOUND'])
def test_declared_action_error_registered(code):
    from agent_py_agent.agent.tooling.models import ToolHandlerOutcome

    assert ToolHandlerOutcome('scripts', False, '{}', error_code=code).error_code == code
