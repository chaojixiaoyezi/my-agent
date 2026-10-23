# LLM: 模型出口合成且只捕获正式辅助请求，不访问供应商；不把此单测当在线模型验收。
# 模块用途: 回归Workflow宿主分析、运行身份和无工具权限边界。
import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import self_map_bootstrap as bootstrap
from agent_py_agent.agent.workflows.models import AnalysisContext


# LLM: 固定同一运行身份和完整输入，不伪造预算或新执行者。
# 函数用途: 构造可信宿主参数。
def agent():
    return SimpleNamespace(backend=SimpleNamespace(generate=lambda **kw: None),
                           _current_run_params=SimpleNamespace(run_id='run-1', task_id='task-1',
                               run_request_id='req-1', task_attributes={'conversation_thread_id': 'thread-1'}))


# LLM: 正式辅助调用负责准入、账本、时限和供应商范围；这里只观察送入的真实上下文与身份。
# 函数用途: 验证宿主没有绕开统一模型出口或暴露工具执行能力。
def test_host_analysis_uses_governed_call(monkeypatch):
    factory = getattr(bootstrap, '_workflow_analysis_handler', None)
    assert callable(factory)
    calls = []
    monkeypatch.setattr(bootstrap, 'generate_auxiliary_model_response',
                        lambda request: calls.append(request) or SimpleNamespace(text='{"decision":"alert"}'))
    host = agent()
    context = AnalysisContext({'collect': {'value': 9}}, {'task': '原始'}, {'mapped': 8},
                              [{'id': 'demo', 'body_markdown': '完整正文' * 100}], [{'source_id': 'logs'}])
    result = factory(host)({'step_id': 'judge'}, context, ('owner:demo',))
    assert result == {'decision': 'alert'}
    request, = calls
    assert request.agent is host and request.purpose == 'workflow_analysis'
    assert (request.run_id, request.task_id, request.request_id, request.thread_id) == (
        'run-1', 'task-1', 'req-1', 'thread-1')
    assert request.tools == [] and request.tool_choice == 'none'
    material = json.loads(request.prompt)
    assert material['run_inputs'] == {'task': '原始'} and material['step_inputs'] == {'mapped': 8}
    assert material['prior_outputs'] == {'collect': {'value': 9}}
    assert material['skill_details'][0]['body_markdown'] == '完整正文' * 100


# LLM: 无模型或无活跃身份不得发起另一个匿名模型调用，坏响应不当业务成功。
# 函数用途: 验证宿主分析器失败关闭。
@pytest.mark.parametrize('response', [SimpleNamespace(text='not json'), SimpleNamespace(text='[]'),
    SimpleNamespace(text='{}', truncated=True), SimpleNamespace(text='{}', tool_use_blocks=[{}]),
    SimpleNamespace(text='{}', runtime_status='failed')])
def test_host_analysis_rejects_invalid_response(monkeypatch, response):
    factory = getattr(bootstrap, '_workflow_analysis_handler', None)
    assert callable(factory)
    monkeypatch.setattr(bootstrap, 'generate_auxiliary_model_response', lambda request: response)
    with pytest.raises((ValueError, RuntimeError)):
        factory(agent())({}, AnalysisContext(), ())
    assert factory(SimpleNamespace(backend=None)) is None
