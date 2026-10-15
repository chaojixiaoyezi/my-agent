"""预算必须在引用复制/并发保留之前生效，身份安装复用原并行协议。"""
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_py_agent.agent.tooling import program_data
from agent_py_agent.agent.tooling.program_data import DataBudget, resolve
from agent_py_agent.tests.test_tool_program import Probe, run


def test_oversized_call_batch_rejected_before_resolve(tmp_path, monkeypatch):
    copies = []
    original = program_data.deepcopy
    monkeypatch.setattr(program_data, 'deepcopy', lambda value: (copies.append(value), original(value))[1])
    result, probe = run(tmp_path, [
        {'id':'source','data':[1, 2]},
        {'id':'bad','calls':[{'tool':'probe','arguments':{'value':{'$ref':'source#'}}} for _ in range(33)]},
    ])
    assert not result.ok and not probe.calls and not copies


def test_reference_expansion_reserves_before_copy(monkeypatch):
    copies = []
    monkeypatch.setattr(program_data, 'deepcopy', lambda value: copies.append(value))
    with pytest.raises(ValueError):
        resolve({'$ref':'source#'}, {'source':'x'*100}, budget=DataBudget(20))
    assert not copies


def test_repeated_small_refs_share_cumulative_budget():
    with pytest.raises(ValueError):
        resolve([{'$ref':'source#'}]*10, {'source':'x'*20}, budget=DataBudget(100))


def test_concurrent_capture_overflow_retains_all_call_facts(tmp_path):
    result, probe = run(tmp_path, [{'id':'reads','calls':[{'tool':'probe','arguments':{}} for _ in range(3)]}],
        probe=LargeProbe())
    assert not result.ok and len(probe.calls) == 3
    facts = result.metadata['handler_details']['program']
    assert len(facts['calls']) == 3 and facts['program_error'] == 'ValueError'


class LargeProbe(Probe):
    def execute(self, params):
        return super().execute({'value':'x'*500_000})


def test_original_thread_context_installed_and_restored_on_failure():
    from agent_py_agent.agent.agent_core.tool_loop.round_execution import (
        _capture_parallel_thread_context,
        run_with_parallel_thread_context,
    )
    from agent_py_agent.agent.runtime_context import ThreadLocalAgentAttribute
    class Agent:
        _current_run_params = ThreadLocalAgentAttribute('_current_run_params')
    agent = Agent()
    marker = object()
    agent._current_run_params = marker
    context = _capture_parallel_thread_context(agent)
    def worker():
        previous = object()
        agent._current_run_params = previous
        def invoke():
            assert agent._current_run_params is marker
            raise ValueError('failure')
        with pytest.raises(ValueError):
            run_with_parallel_thread_context(agent, context, invoke)
        assert agent._current_run_params is previous
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(worker).result()
    assert agent._current_run_params is marker


def test_nested_dispatch_uses_host_thread_runner(tmp_path):
    from agent_py_agent.agent.agent_core.tool_loop.round_execution import (
        _capture_parallel_thread_context,
        run_with_parallel_thread_context,
    )
    from agent_py_agent.agent.core import SimpleAgent
    agent = SimpleAgent.__new__(SimpleAgent)
    marker = object()
    agent._current_run_params = marker
    context = _capture_parallel_thread_context(agent)
    class BoundProbe(Probe):
        def execute(self, params):
            assert agent._current_run_params is marker
            return super().execute(params)
    result, probe = run(tmp_path, [{'id':'reads','calls':[{'tool':'probe','arguments':{}} for _ in range(2)]}],
        probe=BoundProbe(), trusted_run_context={'nested_tool_thread_runner':
            lambda invoke: run_with_parallel_thread_context(agent, context, invoke)})
    assert result.ok and len(probe.calls) == 2


def test_child_archive_parameters_and_lineage_match_actual_child(monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.tool_call_runtime import _archive_runtime_tool_output
    from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
    from agent_py_agent.agent.tooling.runtime_contracts import ToolCall
    observed = []
    monkeypatch.setattr('agent_py_agent.agent.agent_core.tool_call_archive_record.archive_tool_output_projection',
        lambda *args, **kwargs: observed.append((args[2], args[3], kwargs['model_call'])))
    parent = ToolCall(call_id='parent',tool_name='tool_program',arguments={'steps':[]},source_protocol='native',schema_hash='sha256:test',run_id='run',turn_id='turn',attempt_id='attempt')
    child = ToolCall(call_id='parent/0:1',tool_name='probe',arguments={'value':4},source_protocol='native',schema_hash='sha256:test',run_id='run',turn_id='turn',attempt_id='attempt')
    request = SimpleNamespace(agent=object(),request=SimpleNamespace(params=object()),call=parent,
        model_call=parent,output_observer=None)
    _archive_runtime_tool_output(request,child,ToolHandlerOutcome('probe',True,'{}'))
    assert observed[0][2] == child
    assert observed[0][1].result_envelope['tool_program_lineage'] == {
        'parent_call_id':'parent','step_key':'0:1'}


def test_live_view_counts_utf8_bytes_and_keeps_failure_refs_in_archive():
    import json

    from agent_py_agent.agent.tooling.tool_program import ToolProgramTool
    calls = [{'call_id':f'parent/{i}', 'parent_call_id':'parent', 'tool_name':'probe',
        'status':'failed','error_code':'PATH_ACCESS_DENIED','reported_error_code':'',
        'duration_ms':1,'refs':[{'ref':'archive:'+'x'*1000}]} for i in range(32)]
    payload = {'result':'中'*6000,'calls':calls,'stats':{'tool_calls':32},'skipped_steps':[],
        'program_error':'','result_truncated':False,'result_chars':6000}
    live = ToolProgramTool._live_payload(payload)
    assert len(json.dumps(live,ensure_ascii=False).encode()) <= 16000
    assert live['result'] is None and all(row['status']=='failed' for row in live['calls'])
    assert all(row['refs_omitted']==1 for row in live['calls'])
    assert payload['result'] == '中'*6000 and len(payload['calls'][0]['refs']) == 1
