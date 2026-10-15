"""有限只读编排必须沿真ToolExecutor，权限/取消/失败/覆盖不因包装丢失。"""
import json
import threading
from dataclasses import replace

import pytest

from agent_py_agent.agent.common.cancellation import CancellationToken
from agent_py_agent.agent.tooling.models import BaseTool, ToolHandlerOutcome
from agent_py_agent.agent.tooling.program_data import pointer, project
from agent_py_agent.agent.tooling.tool_program import ToolProgramTool
from agent_py_agent.tests._tool_runtime_harness import (
    execute_canonical_test_call,
    make_test_model_spec,
    make_test_runtime_policy,
)


class Probe(BaseTool):
    def __init__(self, name='probe', effect='read_only', gate=None):
        self.model_spec = make_test_model_spec(name, input_schema={'type':'object','properties':{
            'value':{}, 'fail':{'type':'boolean'}},'additionalProperties':False})
        self.runtime_policy = make_test_runtime_policy(effect)
        self.calls, self.gate = [], gate

    def execute(self, params):
        self.calls.append(params)
        if self.gate:
            self.gate.wait(timeout=3)
        return ToolHandlerOutcome(self.model_spec.name, not params.get('fail'),json.dumps(params.get('value',[])),
            error_code='PATH_ACCESS_DENIED' if params.get('fail') else '')


def run(tmp_path, steps, *, probe=None, **options):
    probe = probe or Probe()
    tools = {probe.model_spec.name:probe,'tool_program':ToolProgramTool(lambda:True)}
    execution = execute_canonical_test_call(tmp_path,tools=tools,tool_name='tool_program',
        arguments={'steps':steps},**options)
    return execution.result, probe


def test_batch_foreach_projection_has_coverage_and_original_calls(tmp_path):
    result, probe = run(tmp_path,[
        {'id':'source','tool':'probe','arguments':{'value':[{'ip':'a','n':1},{'ip':'a','n':2},{'ip':'b','n':1}]}},
        {'id':'page','data':{'$ref':'source#/json'},'project':{'unique_by':'/ip','fields':['/ip'],'limit':1}},
        {'id':'read','tool':'probe','foreach':{'$ref':'page#/rows'},'arguments':{'value':{'$ref':'item#/~1ip'}}},
    ])
    assert result.ok, result.output
    data = result.metadata['handler_details']['program']
    assert data['stats']['tool_calls'] == 2
    assert len(probe.calls) == 2 and probe.calls[-1]['value'] == 'a'
    assert data['calls'][0]['call_id'] == 'test-call/0:0'


def test_independent_reads_overlap_using_original_parallel_policy(tmp_path):
    gate = threading.Barrier(3)
    result, probe = run(tmp_path,[{'id':'reads','calls':[{'tool':'probe','arguments':{'value':i}} for i in range(3)]}],
        probe=Probe(gate=gate))
    assert result.ok, result.output
    assert len(probe.calls)==3
    assert result.metadata['handler_details']['program']['stats']['execution_groups']==1


@pytest.mark.parametrize('effect',['mutating','dangerous'])
def test_program_cannot_smuggle_mutating_action(tmp_path,effect):
    result,probe=run(tmp_path,[{'id':'write','tool':'probe','arguments':{}}],probe=Probe(effect=effect))
    assert not result.ok and not probe.calls


def test_unknown_unloaded_and_recursive_tools_never_enter_handler(tmp_path):
    for name in ('missing','tool_program'):
        result,probe=run(tmp_path,[{'id':'bad','tool':name,'arguments':{}}])
        assert not result.ok and not probe.calls


def test_projection_cannot_hide_failed_call(tmp_path):
    result,_=run(tmp_path,[{'id':'failed','tool':'probe','arguments':{'fail':True}},
        {'id':'empty','data':[]}])
    payload=result.metadata['handler_details']['program']
    assert not result.ok and payload['calls'][0]['error_code']=='PATH_ACCESS_DENIED'
    assert payload['result']==[]


def test_cancellation_shared_with_all_internal_calls(tmp_path):
    token=CancellationToken()
    probe=Probe()
    original=probe.execute
    def execute(params):
        result=original(params)
        token.cancel('test')
        return result
    probe.execute=execute
    result,probe=run(tmp_path,[{'id':'one','tool':'probe','arguments':{}},
        {'id':'two','tool':'probe','arguments':{}}],probe=probe,cancellation_token=token)
    assert not result.ok and len(probe.calls)==1
    assert result.metadata['handler_details']['program']['calls'][1]['status']=='cancelled'


def test_host_gate_and_input_internal_parameter_guard_still_run(tmp_path):
    probe=Probe()
    probe.runtime_policy=replace(probe.runtime_policy,input_policy=replace(probe.runtime_policy.input_policy,
        internal_parameters=('secret_scope',)))
    result,_=run(tmp_path,[{'id':'read','tool':'probe','arguments':{'secret_scope':'forged'}}],probe=probe)
    assert not result.ok and not probe.calls


def test_large_result_not_silently_clipped_and_errors_keep_evidence(tmp_path):
    result,_=run(tmp_path,[{'id':'read','tool':'probe','arguments':{'value':'x'*18000}}])
    payload=result.metadata['handler_details']['program']
    assert result.ok and payload['result'] is None and payload['result_truncated']
    assert payload['result_chars']>16000 and payload['calls'][0]['status']=='succeeded'


def test_budget_and_bad_references_do_not_launch_extra_work(tmp_path):
    result,probe=run(tmp_path,[{'id':'items','data':list(range(33))},
        {'id':'read','tool':'probe','foreach':{'$ref':'items#'},'arguments':{'value':{'$ref':'item#'}}}])
    assert not result.ok and not probe.calls
    result,probe=run(tmp_path,[{'id':'read','tool':'probe','arguments':{'value':{'$ref':'future#'}}}])
    assert not result.ok and not probe.calls


def test_group_count_and_page_values_are_explicit():
    result=project([{'a':1},{'a':1},{'a':2}],{'count_by':'/a','offset':0,'limit':1})
    assert result['rows']==[{'value':1,'count':2}]
    assert not result['coverage']['complete'] and result['coverage']['input']==3
    assert pointer({'a/b':{'~key':4}},'/a~1b/~0key')==4


def test_disabled_program_does_not_bypass_through_old_snapshot(tmp_path):
    flag=[True]
    tool=ToolProgramTool(lambda:flag[0])
    from agent_py_agent.agent.tooling.models import ToolInvocationContext
    flag[0]=False
    assert not tool.availability().available
    assert not tool.execute_scoped({'steps':[{'id':'x','data':[]}]},ToolInvocationContext(None)).ok


@pytest.mark.parametrize('kind',['user','group'])
def test_program_keeps_real_owner_wall_and_network_disable(tmp_path, kind):
    from agent_py_agent.tests.test_im_credentials_security_matrix import _formal_call
    from agent_py_agent.tests.test_ingestion_task_credential_scope import _active, _agent
    agent=_agent(tmp_path, kind, full=True)
    _active(agent)
    foreign=_agent(tmp_path,'group' if kind=='user' else 'user')
    private=foreign.effective_workspace_root/'foreign.txt'
    private.parent.mkdir(parents=True,exist_ok=True)
    private.write_text('SYNTHETIC_FOREIGN_NOT_VISIBLE')
    result=_formal_call(agent,'tool_program',{'steps':[{'id':'read','tool':'read_file',
        'arguments':{'path':str(private)}}]})
    assert not result.ok and 'SYNTHETIC_FOREIGN_NOT_VISIBLE' not in result.output
    payload=result.metadata.get('handler_details',{}).get('program',{})
    assert payload and payload['calls'][0]['error_code']


def test_readonly_internal_result_uses_original_host_audit_sink(tmp_path):
    seen=[]
    result,_=run(tmp_path,[{'id':'read','tool':'probe','arguments':{}}],
        trusted_run_context={'nested_tool_result_sink':lambda call,result:seen.append((call.run_id,call.attempt_id,result.status))})
    assert result.ok and seen == [('test-run','test-attempt','succeeded')]


@pytest.mark.parametrize('mode,requests,rounds',[('serial',5,4),('batch',3,2),('program',2,1)])
def test_original_agent_fake_model_measures_deterministic_chain(tmp_path, record_property, mode, requests, rounds):
    from agent_py_agent.agent.backends import BaseBackend, ModelResponse
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.tooling.runtime_contracts import ProviderToolCapability
    class Replay(BaseBackend):
        name='program-replay'
        def __init__(self):
            self.calls=[]
        def probe_tool_capability(self):
            return ProviderToolCapability(provider=self.name,endpoint='local://program-replay',model='fake',
                stream=False,native_supported=True,evidence='test_declared_native')
        def generate(self,prompt,on_chunk=None,**kwargs):
            self.calls.append(kwargs.get('messages'))
            index=len(self.calls)-1
            if mode=='program' and index==0:
                return ModelResponse('',self.name,tool_use_blocks=[{'id':'batch','name':'tool_program',
                    'input':{'steps':[{'id':'source','tool':'probe','arguments':{'value':[0,1,2]}},
                        {'id':'reads','tool':'probe','foreach':{'$ref':'source#/json'},
                         'arguments':{'value':{'$ref':'item#'}}},
                        {'id':'result','data':{'$ref':'reads#'},'project':{'fields':['/json']}}]}}])
            if mode!='program' and index==0:
                return ModelResponse('',self.name,tool_use_blocks=[{'id':'source','name':'probe','input':{'value':[0,1,2]}}])
            if mode=='serial' and 1<=index<=3 or mode=='batch' and index==1:
                numbers=[index-1] if mode=='serial' else [0,1,2]
                return ModelResponse('',self.name,tool_use_blocks=[{'id':f'read-{n}','name':'probe','input':{'value':n}} for n in numbers])
            return ModelResponse('已核对三项。',self.name)
    agent=SimpleAgent(AgentConfig(enable_tools=True,prompt_files=[],memory_path='memory.jsonl',
        tool_default_deferral_enabled=False,tool_catalog_deferred_categories=[]),tmp_path)
    agent.tools.register(Probe())
    backend=Replay()
    agent.backend=backend
    result=agent.run('把三项数据一起核对一下，告诉我结果。',save=False)
    assert result.response=='已核对三项。' and result.tool_rounds==rounds
    assert len(backend.calls)==requests
    assert len(agent.tools.tools['probe'].calls)==4
    assert sorted(call['value'] for call in agent.tools.tools['probe'].calls[1:])==[0,1,2]
    record_property('model_requests',len(backend.calls))
    record_property('tool_rounds',result.tool_rounds)
    record_property('tool_calls',len(agent.tools.tools['probe'].calls))
    record_property('model_input_chars',sum(len(json.dumps(messages,ensure_ascii=False)) for messages in backend.calls))
    final=json.dumps(backend.calls[-1],ensure_ascii=False)
    if mode=='program':
        assert 'execution_groups' in final and 'test-call' not in final
