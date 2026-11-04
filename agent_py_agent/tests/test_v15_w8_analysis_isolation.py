"""无状态研判的输入边界及轻量子代理条件降级。"""
import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import self_map_bootstrap as bootstrap
from agent_py_agent.agent.workflows.analysis_isolation import resolve_analysis_isolation


# LLM: 纯选择器不发起调用，未知模式保持安全的无状态缺省。
# 函数用途: 核对默认、显式及条件降级的机器事实。
@pytest.mark.parametrize('params,requested,note', [
    ({}, 'stateless_skill', ''),
    ({'analysis_isolation': 'stateless_skill'}, 'stateless_skill', ''),
    ({'analysis_isolation': 'lightweight_subagent'}, 'lightweight_subagent', 'minimal_profile_entry_unavailable'),
    ({'analysis_isolation': 'future'}, 'stateless_skill', ''),
])
def test_mode_selection_records_fallback(params, requested, note):
    assert resolve_analysis_isolation(params) == {
        'requested': requested, 'mode': 'stateless_skill', 'note': note}


# LLM: 宿主提供准确run身份；模拟模型只返回业务JSON，不执行网络或工具。
# 函数用途: 验证最终请求没有主会话、人格、记忆或工具循环。
def test_analysis_call_contains_only_step_material(tmp_path, monkeypatch):
    sent = []

    # LLM: 请求观察在真实handler边界，不替代隔离材料的构造。
    # 函数用途: 捕获最终辅助请求并返回一个合法业务结果。
    def generate(request):
        sent.append(request)
        return SimpleNamespace(text='{"value":7}')

    monkeypatch.setattr(bootstrap, 'generate_auxiliary_model_response', generate)
    agent = SimpleNamespace(backend=SimpleNamespace(generate=lambda prompt: None),
        session_history=['PRIVATE_HISTORY'], memory_recall=['PRIVATE_MEMORY'], soul='PRIVATE_PERSONA')
    params = {'run_id': 'run-one', 'task_id': 'task-one', 'analysis_isolation': 'lightweight_subagent'}
    context = SimpleNamespace(prior_outputs={'one': {'value': 7}}, run_inputs={}, step_inputs={},
                              skill_details=[{'body': '绑定方法'}], data_sources=[])
    result = bootstrap._workflow_analysis_handler(agent, run_params=params)({'config': {}}, context, ['owner:analysis'])
    assert result == {'value': 7}
    assert len(sent) == 1 and sent[0].run_id == 'run-one'
    assert sent[0].messages is None and sent[0].tools is None
    assert not any(word in sent[0].prompt for word in ('PRIVATE_HISTORY', 'PRIVATE_MEMORY', 'PRIVATE_PERSONA'))
    assert json.loads(sent[0].prompt)['skill_details'] == [{'body': '绑定方法'}]
    assert context.analysis_isolation['note'] == 'minimal_profile_entry_unavailable'
    assert context.analysis_attempts[0]['analysis_isolation']['mode'] == 'stateless_skill'
