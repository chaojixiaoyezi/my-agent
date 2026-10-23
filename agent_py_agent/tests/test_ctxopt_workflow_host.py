# LLM: 模型出口合成且只捕获正式辅助请求，不访问供应商；不把此单测当在线模型验收。
# 模块用途: 回归Workflow宿主分析、运行身份和无工具权限边界。
import json
from types import SimpleNamespace
from typing import Any

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
    assert request.tools is None and request.tool_choice is None
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


# LLM: 结构化入口精确沿BaseLLMBackend签名，不接tools/request_options；统一辅助出口及分派器均使用真实实现。
# 类用途: 用离线业务响应复现真实后端参数边界，而非替换辅助调用。
class StrictStructuredBackend:
    # LLM: 测试只记录无秘密的离线请求，构造不发网络。
    # 函数用途: 保存一次结构化调用的参数供断言。
    def __init__(self, response):
        self.calls = []
        self.response = response

    # LLM: 普通生成不是本测试的入口，误走该分支必须直接失败。
    # 函数用途: 保留宿主模型能力探测所需方法。
    def generate(self, prompt: str):
        raise AssertionError('expected structured backend')

    # LLM: 禁止用**kwargs吞掉不支持参数；返回值是离线供应商结果，不执行工具。
    # 函数用途: 按正式Base签名接收结构化请求。
    def generate_structured(self, prompt: str, response_schema: dict[str, Any], *,
                            system_prompt: str | None = None, temperature: float | None = None):
        self.calls.append((prompt, response_schema, system_prompt, temperature))
        return self.response


# LLM: 不mock generate_auxiliary_model_response或_call_backend；真实准入、账本和签名检查必须到达此离线后端。
# 函数用途: 锁定无工具结构化分析可以穿过生产辅助调用边界，且坏响应仍失败关闭。
@pytest.mark.parametrize('invalid', [False, True])
def test_host_analysis_reaches_real_structured_boundary(invalid):
    host = agent()
    host.config = SimpleNamespace()
    response = SimpleNamespace(text='{"decision":"alert"}', tool_use_blocks=[{}] if invalid else [])
    backend = host.backend = StrictStructuredBackend(response)
    handler = bootstrap._workflow_analysis_handler(host)
    if invalid:
        with pytest.raises(ValueError, match='incomplete or non-business'):
            handler({'step_id': 'judge'}, AnalysisContext(run_inputs={'value': 9}), ())
    else:
        assert handler({'step_id': 'judge'}, AnalysisContext(run_inputs={'value': 9}), ()) == {'decision': 'alert'}
    assert len(backend.calls) == 1
    prompt, schema, _, _ = backend.calls[0]
    assert json.loads(prompt)['run_inputs'] == {'value': 9}
    assert '不得调用工具' in prompt
    assert schema == {'type': 'object', 'additionalProperties': True}
