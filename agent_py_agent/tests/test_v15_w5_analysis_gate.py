"""结构化查询健康门与隔离参数合同，不调用外部模型。"""

from dataclasses import replace

import pytest

from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.agent.workflows.models import ScriptExecutionResult
from agent_py_agent.tests.test_v15_w5_foreach_batches import batch_flow
from agent_py_agent.tests.test_workflow_script_engine import script, step, workflow


# LLM: 仅替代W6运行seam；工作流创建、依赖解析及账本均为真实实现。
# 函数用途: 构造携带结构化query_health的脚本结果。
def result(health):
    return ScriptExecutionResult('collect', True, 0, 1, 1, 2, {'value': 1}, '', '',
                                 source_check={'query_health': health})


# LLM: 模型回调只记录是否真实分派，禁止以自然语言输出判门。
# 函数用途: 锁定阻断、健康、畸形结构及错误策略。
@pytest.mark.parametrize('health,blocked', [
    ({'analysis_blocked': True, 'block_reason': 'QUERY_FAILED_NO_DATA',
      'degraded_sources': [{'source_id': 'apps'}]}, True),
    ({'analysis_blocked': False, 'block_reason': None}, False),
    (None, False), ([], False), ({'analysis_blocked': 'true', 'block_reason': 'x'}, False),
    ({'analysis_blocked': True}, False)])
@pytest.mark.parametrize('mode', ['llm_skill_analysis', 'llm_prompt', 'hybrid'])
def test_query_health_gate_never_dispatches_blocked_analysis(tmp_path, monkeypatch, health, blocked, mode):
    script(tmp_path)
    monkeypatch.setattr('agent_py_agent.agent.workflows.engine.run_script_with_datasources',
                        lambda *args, **kwargs: result(health))
    seen = []
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda spec, context, skills: seen.append(True) or {'decision': 'ok'})
    judge = step('judge', mode=mode, script_id='collect' if mode == 'hybrid' else '', on_error='fail',
                 inputs_map={'value': {'step_id': 'collect', 'key': 'value'}})
    run = engine.run_workflow(workflow(step(), judge))
    if blocked:
        assert seen == [] and run.status == 'failed'
        assert run.steps[-1].error_code == 'ANALYSIS_BLOCKED_NO_DATA'
        assert run.steps[-1].output_snapshot['data_retrieval_failed'] is True
        assert run.steps[-1].output_snapshot['source_ids'] == ['apps']
    else:
        assert seen == [True] and run.status == 'succeeded'
    if isinstance(health, dict):
        assert run.steps[0].escalation_result['query_health'] == health


# LLM: 合同由传给分析器的上下文验证，不只检查配置是否存在。
# 函数用途: 验证默认、显式和非法隔离模式的替换回执。
@pytest.mark.parametrize('configured,expected,replaced', [
    (None, 'stateless_skill', False), ('stateless_skill', 'stateless_skill', False),
    ('lightweight_subagent', 'lightweight_subagent', False), ('invalid', 'stateless_skill', True)])
def test_analysis_isolation_is_passed_to_handler(tmp_path, configured, expected, replaced):
    seen = []

    # LLM: 模拟分析器只接收引擎给出的隔离请求，不读取当前会话。
    # 函数用途: 捕获实际派发的隔离参数。
    def analyze(spec, context, skills):
        seen.append(getattr(context, 'run_params', {}))
        return {'decision': 'ok'}

    config = {} if configured is None else {'analysis_isolation': configured}
    run = WorkflowEngine(tmp_path, analysis_handler=analyze).run_workflow(
        workflow(step('judge', mode='llm_skill_analysis', config=config)))
    assert run.status == 'succeeded'
    assert seen == [{'analysis_isolation': expected}]
    assert bool(run.steps[0].escalation_result.get('analysis_isolation_replaced')) is replaced


# LLM: 每批脚本结果有独立健康事实，坏批不得污染健康批的分派资格。
# 函数用途: 验证hybrid逐批门、阻断统计和继续策略。
def test_hybrid_foreach_gates_only_blocked_batch(tmp_path, monkeypatch):
    script(tmp_path)
    seen = []

    # LLM: W6 seam根据本批输入给出机器事实，不以模型输出作判断。
    # 函数用途: 让第二批的取数明确失败。
    def execute(home, script_id, inputs, **options):
        blocked = inputs['batch'][0] == 2
        return replace(result({'analysis_blocked': blocked, 'block_reason': 'QUERY_FAILED_NO_DATA' if blocked else None}),
                       output_data={'values': inputs['batch']})

    monkeypatch.setattr('agent_py_agent.agent.workflows.engine.run_script_with_datasources', execute)
    value = batch_flow(policy='continue', mode='aggregate')
    value['steps'][0].update(mode='hybrid', script_id='collect')
    run = WorkflowEngine(tmp_path, analysis_handler=lambda spec, context, skills:
        seen.append(context.step_inputs['batch'][0]) or {'values': context.step_inputs['batch']}).run_workflow(
            value, {'rows': list(range(6))})
    assert run.status == 'succeeded' and seen == [0, 4]
    assert run.steps[0].batches_summary == {'total': 3, 'completed': 2, 'failed': 0, 'blocked': 1, 'skipped': 0}
    assert run.steps[0].batches[1]['analysis_blocked'] is True
    assert run.final_output == {'values': [0, 1, 4, 5]}
