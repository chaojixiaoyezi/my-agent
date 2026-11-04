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

    # LLM: W6 seam替身保留显式参数合同，不屏蔽签名错误。
    # 函数用途: 返回本例健康信封。
    def execute(home, script_id, inputs, *, expected_script_pin=None, runner=None, workspace_root=None,
                timeout_seconds=None, override_data_sources=None, options=None):
        return result(health)

    monkeypatch.setattr('agent_py_agent.agent.workflows.engine.run_script_with_datasources',
                        execute)
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
    def execute(home, script_id, inputs, *, expected_script_pin=None, runner=None, workspace_root=None,
                timeout_seconds=None, override_data_sources=None, options=None):
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


# LLM: 无映射的旧分析合同确实携带全部前步，不能据空dependencies误称没有输入依赖。
# 函数用途: 证伪独立复核中关于隐式前步上下文的假设。
def test_unmapped_analysis_really_receives_prior_outputs(tmp_path, monkeypatch):
    script(tmp_path)
    seen = []

    # LLM: 仅替代seam，保留真实查询健康投影及旧分析上下文。
    # 函数用途: 提供一份健康的前步输出。
    def execute(home, script_id, inputs, *, expected_script_pin=None, runner=None, workspace_root=None,
                timeout_seconds=None, override_data_sources=None, options=None):
        return result({'analysis_blocked': False, 'block_reason': None})

    monkeypatch.setattr('agent_py_agent.agent.workflows.engine.run_script_with_datasources', execute)
    run = WorkflowEngine(tmp_path, analysis_handler=lambda spec, context, skills:
        seen.append(context.prior_outputs) or {'decision': 'ok'}).run_workflow(
            workflow(step(), step('judge', mode='llm_skill_analysis')))
    assert run.status == 'succeeded' and seen == [{'collect': {'value': 1}}]


# LLM: 不同源列表的批号没有健康关联权；父输入的明确阻断不能被同号健康批覆盖。
# 函数用途: 锁住独立复核发现的错误批号关联反例。
def test_unrelated_batch_index_cannot_override_blocked_input():
    from agent_py_agent.agent.workflows.analysis_gate import apply_analysis_gate
    from agent_py_agent.agent.workflows.models import StepExecutionRecord

    source = StepExecutionRecord('source', 'source', 'deterministic_script', 'succeeded', 0, 0, 0, False,
        escalation_result={'query_health': {'analysis_blocked': True, 'block_reason': 'QUERY_FAILED_NO_DATA'}},
        batches=[{'index': 0, 'query_health': {'analysis_blocked': False, 'block_reason': None}}])
    target = StepExecutionRecord('judge', 'judge', 'llm_skill_analysis', 'running', 0, 0, 0, False)
    spec = {'step_id': 'judge', 'on_error': 'fail', '_batch_index': 0,
            'inputs_map': {'value': {'step_id': 'source', 'key': 'value'}}}
    assert apply_analysis_gate(target, spec, (source,)) is True
    assert target.error_code == 'ANALYSIS_BLOCKED_NO_DATA'


# LLM: 父步骤也消费所有本批脚本健康，不能只在嵌套rows保留阻断导致下游误判。
# 函数用途: 验证continue模式的完整健康投影。
def test_parent_foreach_health_retains_any_blocked_batch(tmp_path, monkeypatch):
    script(tmp_path)

    # LLM: 第二批明确取数失败，第三批健康不应抹掉它。
    # 函数用途: 构造先后不同健康的真实批链。
    def execute(home, script_id, inputs, *, expected_script_pin=None, runner=None, workspace_root=None,
                timeout_seconds=None, override_data_sources=None, options=None):
        blocked = inputs['batch'][0] == 2
        return result({'analysis_blocked': blocked, 'block_reason': 'QUERY_FAILED_NO_DATA' if blocked else None})

    monkeypatch.setattr('agent_py_agent.agent.workflows.engine.run_script_with_datasources', execute)
    value = batch_flow(policy='continue', mode='aggregate')
    value['steps'][0].update(mode='hybrid', script_id='collect')
    run = WorkflowEngine(tmp_path, analysis_handler=lambda spec, context, skills: {'value': 1}).run_workflow(
        value, {'rows': list(range(6))})
    assert run.steps[0].escalation_result.get('query_health', {}).get('analysis_blocked') is True


# LLM: foreach的子分析上下文与父步骤都必须保留隔离选择的替换事实。
# 函数用途: 验证顺序及并发批的非法隔离值不丢失回执。
@pytest.mark.parametrize('parallel', [1, 2])
def test_foreach_notes_invalid_isolation_on_parent_record(tmp_path, parallel):
    seen = []
    value = batch_flow(parallel=parallel)
    value['steps'][0]['config'] = {'analysis_isolation': 'invalid'}
    run = WorkflowEngine(tmp_path, analysis_handler=lambda spec, context, skills:
        seen.append(context.run_params['analysis_isolation']) or {'value': 1}).run_workflow(
            value, {'rows': [1, 2, 3]})
    assert run.status == 'succeeded' and seen == ['stateless_skill', 'stateless_skill']
    assert run.steps[0].escalation_result.get('analysis_isolation_replaced') is True
