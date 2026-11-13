"""隔离原生组合冒烟：仅本地确定性模型驱动，真实任务/审核/脚本/交付/下载。

覆盖V00-02/V14-02原组合入口和V05-06原件核验；不代替真实供应商或外部IM验收。
测试不直写运行记录、审核仓储或交付文件，输出只由正规脚本生成。
"""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.backends.base import EchoBackend, _utc_now_iso
from agent_py_agent.agent.conversation import FakeDeliveryService
from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext
from agent_py_agent.agent.runtime_context import current_subagent_run_id
from agent_py_agent.agent.tooling.runtime_contracts import ProviderToolCapability
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore
from agent_py_agent.cli.chat_client_context import (
    _gateway_request_identity,
    owner_identity_from_config,
)
from agent_py_agent.cli.space_commands import BusinessClient
from agent_py_agent.tests.test_business_v12_background_scheduling import native
from agent_py_agent.tests.test_mega_business_api_actions import native_request
from agent_py_agent.tests.test_v13_w11_packages import _PACKAGE_ID, _package, _Source

_FIXTURES = (native,)
_ROWS = [[159, 7], [270, 3], [40, 11]]
_ORACLE = {'grand_total_cents': 2363, 'row_count': 3}


class _Wire:
    """只使用原凭据和真实Client/Server，保留原请求/响应以便失败定位。"""

    def __init__(self, native):
        self.native, self.receipts = native, []
        self.identity = _gateway_request_identity(owner_identity_from_config(native[1].config))
        self.client = BusinessClient(self.transport)

    def raw(self, method, path, data=None, **headers):
        return native_request(self.native, method, path, data, user=self.identity['user_id'],
            headers={'X-Channel': self.identity['channel'], **headers})

    def transport(self, path, payload, *, timeout):
        status, _, raw = self.raw('POST', path, payload)
        receipt = json.loads(raw)
        self.receipts.append({'request': payload, 'status': status, 'receipt': receipt})
        return status, receipt

    def call(self, action, params=None, **options):
        receipt = self.client.send(self.client.prepare(action, params or {}, **options))
        assert receipt['ok'], (action, receipt)
        return receipt['data']


def _business_entry(native):
    server, agent = native
    from agent_py_agent.agent.gateway_parts.business_host_ports import wire_business_host_ports

    wire_business_host_ports(agent)
    source = _Source({(_PACKAGE_ID, '1.0.0'): _package(_PACKAGE_ID, '1.0.0')})
    agent.business_v13_providers = {'package_source': source}
    wire = _Wire(native)
    space = wire.call('spaces.create', {'name': 'composed-offline-release'})
    sid = space['space_id']
    installed = wire.call('packages.install', {'package_id': _PACKAGE_ID, 'version': '1.0.0', 'space_id': sid})
    assert installed['mounted_space_id'] == sid
    detail = wire.call('spaces.get', {'space_id': sid})
    assert any(row['package_ref'] == _PACKAGE_ID and row['enabled'] for row in detail['space']['mounts'])
    bound = wire.call('interfaces.bind', {'surface_ref': 'terminal', 'name': 'composed-offline-release',
        'space_ids': [sid], 'default_space_id': sid, 'controller': {'mode': 'global'}})
    resolved = wire.call('interfaces.resolve', {'binding_id': bound['binding_id']})
    assert resolved['effective_spaces'] == [sid]
    controller = wire.call('controllers.ensure')
    _prepare_release_definition(SimpleNamespace(agent=agent, wire=wire, context=SimpleNamespace(space_id=sid)))
    accepted = wire.call('tasks.submit', {'space_id': sid, 'instruction': '隔离订单：单价分/数量=' + json.dumps(_ROWS)
        + '。独立审核后运行composed-release工作流，交付总金额整数分与行数；实际release仅一次，原sender收原件。'})
    task = agent.conversation_store.session_tasks.require(accepted['task_id'])
    context = BusinessExecutionContext.from_dict(task.metadata['business_context'])
    assert context.task_id == task.task_id and context.space_id == sid
    assert task.target_thread_id == controller['thread_id']
    return SimpleNamespace(wire=wire, agent=agent, task=task, context=context, installed=installed,
        binding=bound, model_lock=threading.Lock(), main_calls=0, child_calls={}, workflow_run_id='', failures=[],
        review_outputs={}, observed_turn=threading.local(), sender_calls=0, review_dispatch={})


def _tool_reply(backend, actor, index, name, arguments):
    return ModelResponse('', backend.name, tool_use_blocks=[{
        'id': f'composed-{actor}-{index}', 'name': name, 'input': arguments}])


def _install_offline_driver(case, monkeypatch):
    """仅替换模型供应商；每个真实run独立阶段，所有工具/审核/执行仍走原链。"""
    def probe(backend):
        return ProviderToolCapability(provider=backend.name, endpoint='local://composed-smoke', model='offline',
            stream=False, native_supported=True, evidence='contract_test', observed_at=_utc_now_iso())

    def generate(backend, prompt, **kwargs):
        actor = getattr(case.observed_turn, 'actor', '')
        if actor and actor != case.task.task_id and getattr(case.observed_turn, 'thread_id', '') != case.task.sender_thread_id:
            child = case.agent.subagents.load(actor)
        else:
            child = None
        with case.model_lock:
            if child is not None:
                index = case.child_calls.get(actor, 0)
                case.child_calls[actor] = index + 1
            else:
                index = case.main_calls
                case.main_calls += 1
        if child is None and getattr(case.observed_turn, 'thread_id', '') == case.task.sender_thread_id:
            run, release, _ = _pending_candidate(case)
            artifact_id = release.output_artifacts[0]['artifact_id']
            assert run.status == 'succeeded' and artifact_id in prompt + repr(kwargs.get('messages'))
            case.sender_calls += 1
            return ModelResponse('已收到原订单交付：' + artifact_id, backend.name)
        return _composed_model_step(case, backend, child, index, prompt)

    from agent_py_agent.agent.agent_core import tool_model_generation

    original_generate = tool_model_generation._generate_and_remember
    def observe_generation(backend, prompt, state, kwargs):
        # 原timeout-guard线程内读取同一次真实模型状态，不依赖外层线程本地猜身份。
        params = state.params
        case.observed_turn.agent = state.agent
        case.observed_turn.actor = params.run_id
        case.observed_turn.thread_id = (params.task_attributes or {}).get('conversation_thread_id', '')
        try:
            return original_generate(backend, prompt, state, kwargs)
        finally:
            case.observed_turn.agent = None
            case.observed_turn.actor = ''
            case.observed_turn.thread_id = ''
    monkeypatch.setattr(tool_model_generation, '_generate_and_remember', observe_generation)
    monkeypatch.setattr(EchoBackend, 'probe_tool_capability', probe)
    monkeypatch.setattr(EchoBackend, 'generate', generate)


def _assert_original_artifact(case, run, artifact_id):
    step = next(step for step in run.steps if step.step_id == 'release')
    item = next(item for item in step.output_artifacts if item['artifact_id'] == artifact_id)
    result = case.wire.call('artifacts.get', {'artifact_id': artifact_id, 'version_ref': item['sha256']})
    assert result['artifact']['task_id'] == case.task.task_id and result['available'] is True
    status, _, raw = case.wire.raw('GET', f'/client/business/artifacts/{artifact_id}/content?version_ref={item["sha256"]}')
    assert status == 200 and hashlib.sha256(raw).hexdigest() == item['sha256']
    assert len(raw) == item['size_bytes'] and json.loads(raw) == _ORACLE
    return raw


def _pending_candidate(case):
    assert case.workflow_run_id, '原workflows工具尚未发表运行编号'
    run = WorkflowRunStore(case.agent.home_paths.owner_home_dir).load_run(case.workflow_run_id)
    step = next(row for row in run.steps if row.step_id == 'release')
    return run, step, step.escalation_result.get('pending_review') or {}


def _composed_model_step(case, backend, child, index, prompt):
    """离线供应商程序只发原结构化调用；最终状态由真实工具/运行账裁决。"""
    actor = child.id if child is not None else 'main'
    if child is not None:
        # dispatcher初始绑定字段由root提供；这里只接受宿主原结构，不读goal正文猜ID。
        requirement_id = child.attributes['review_requirement_id']
        assert child.role == 'reviewer' and child.root_id == case.task.task_id
        if index == 0:
            return _tool_reply(backend, actor, index, 'manage_requirement_provenance', {
                'action':'query_provenance', 'requirement_id':requirement_id})
        if index == 1:
            received = case.review_outputs[child.id]['workflow_review_material']
            frozen = received['binding']
            assert received['schema'] == 'workflow.review-material.v1' and received['coverage'] == 'complete'
            assert frozen['requirement_id'] == requirement_id
            assert frozen['workflow_run_id'] == case.workflow_run_id and frozen['step_id'] == 'release'
            rows = received['step']['input_snapshot']['rows']
            independent = {'grand_total_cents':sum(price*count for price,count in rows), 'row_count':len(rows)}
            assert independent == _ORACLE
            script = received['script']
            assert hashlib.sha256(script['code'].encode()).hexdigest() == script['code_sha256']
            return _tool_reply(backend, actor, index, 'manage_requirement_provenance', {
                'action':'record_independent_review', 'requirement_id':frozen['requirement_id'],
                'reviewed_input_version':frozen['input_version'], 'review_scope':'workflow_effect.v1',
                'review_verdict':'pass', 'evidence_refs':[f'workflow-run://{frozen["workflow_run_id"]}/{frozen["step_id"]}/{frozen["attempt_id"]}']})
        return ModelResponse('独立审核工具已返回；原执行者沿同一运行继续。', backend.name)
    if index == 0:
        return _tool_reply(backend, actor, index, 'tool_search', {'query':'workflows'})
    if index == 1:
        return _tool_reply(backend, actor, index, 'workflows', {'action':'run', 'workflow_id':'composed-release', 'inputs':{'rows':_ROWS}})
    if index == 2:
        _, step, pending = _pending_candidate(case)
        assert step.error_code == 'WORKFLOW_REVIEW_REQUIRED' and pending
        assert not step.output_artifacts
        assert case.review_dispatch.get('child_run_ids'), case.review_dispatch
        return ModelResponse('等待原宿主派出的独立审核子任务完成，再续同一工作流。', backend.name)
    run, step, _ = _pending_candidate(case)
    if run.status != 'succeeded':
        return _tool_reply(backend, actor, index, 'workflows', {'action':'resume', 'workflow_run_id':run.workflow_run_id})
    artifact = step.output_artifacts[0]
    return ModelResponse(f'实际交付 {artifact["artifact_id"]}；2363分，3行。', backend.name)


def _observe_original_workflow(case, monkeypatch):
    from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool

    original = WorkflowsTool.execute_scoped
    def execute_scoped(tool, params, invocation):
        result = original(tool, params, invocation)
        if params.get('action') == 'run' and params.get('workflow_id') == 'composed-release':
            identifier = result.result_envelope.get('workflow_run_id')
            if identifier:
                case.workflow_run_id = identifier
            payload = json.loads(result.output)
            case.review_dispatch = payload.get('review_dispatch') or result.result_envelope.get('review_dispatch') or {}
        if not result.ok and result.error_code != 'WORKFLOW_REVIEW_REQUIRED':
            case.failures.append(result.output)
        return result
    monkeypatch.setattr(WorkflowsTool, 'execute_scoped', execute_scoped)
    from agent_py_agent.agent.agent_core.orchestration.tools.requirement_provenance_tool import (
        RequirementProvenanceTool,
    )

    original_provenance = RequirementProvenanceTool.execute_scoped
    def observe_provenance(tool, params, invocation):
        result = original_provenance(tool, params, invocation)
        actor = current_subagent_run_id(tool.agent)
        if actor and params.get('action') == 'query_provenance' and result.ok:
            case.review_outputs[actor] = json.loads(result.output)
        return result
    monkeypatch.setattr(RequirementProvenanceTool, 'execute_scoped', observe_provenance)


def _prepare_release_definition(case):
    from agent_py_agent.agent.assets.descriptor import AssetDescriptor
    from agent_py_agent.agent.assets.script_store import ScriptStore

    # 计数和产物都由实际release子进程写，测试仅观察。计数在原SDK授权workspace内。
    code = '''import json,os
from pathlib import Path
ctx=json.loads(os.environ['MY_AGENT_SCRIPT_WORK_CONTEXT_JSON'])
root=Path(ctx['workspace_root'])
counter=root/'composed-release-count.txt'
n=int(counter.read_text())+1 if counter.exists() else 1
counter.write_text(str(n))
rows=json.loads(os.environ['MY_AGENT_SCRIPT_INPUT_JSON'])['rows']
result=dict(grand_total_cents=sum(p*q for p,q in rows),row_count=len(rows))
(Path(os.environ['MY_AGENT_RUN_OUT'])/'release.json').write_text(json.dumps(result,separators=(',',':'))+'\\n')
print(json.dumps(dict(value=result['grand_total_cents'],counter_path=str(counter))))
'''
    ScriptStore(case.agent.home_paths.owner_home_dir).save_script(AssetDescriptor.from_dict({
        'id':'composed-release-script','name':'release','kind':'script','access_targets':[],
        'data_sources':[],'credential_refs':[],'io_contract':{'inputs':['rows'],'outputs':['value','counter_path']}}), code)
    case.wire.call('workflows.create', {'definition':{
        'descriptor':{'id':'composed-release','name':'composed release','kind':'workflow','access_targets':[],
            'data_sources':[],'credential_refs':[],'io_contract':{},
            'metadata':{'workflow_review_policy':{'steps':['release']}}},
        'steps':[{'step_id':'release','name':'release','mode':'deterministic_script','script_id':'composed-release-script'}]},
        'scope':{'scope_mode':'spaces','space_ids':[case.context.space_id]}})


def test_same_order_composed_release_smoke(native, monkeypatch):  # noqa: F811
    import time

    from agent_py_agent.agent.conversation.runtime import COMPLETION_COALESCE_WINDOW_SECONDS
    from agent_py_agent.cli.gateway_loops import _build_background_scheduler
    from agent_py_agent.tests.test_business_v12_background_scheduling import add_echo_profile

    add_echo_profile(native[1])
    case = _business_entry(native)
    _observe_original_workflow(case, monkeypatch)
    _install_offline_driver(case, monkeypatch)
    delivery = FakeDeliveryService()
    scheduler = _build_background_scheduler(case.agent, delivery)
    scheduler.tick_thread(case.task.target_thread_id)
    # 仅等待实际child生命周期；不手动设DONE，不替孩子登记审核。
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        rows = case.agent.subagents.list_runs_for_root_report(case.task.task_id).runs
        if rows and all(row.status in {'DONE','FAILED','CANCELLED'} for row in rows):
            break
        time.sleep(0.02)
    assert set(case.review_dispatch['child_run_ids']) == {row.id for row in rows}
    assert rows and all(row.status == 'DONE' for row in rows), {'children':[(row.id,row.status) for row in rows],
        'main_calls':case.main_calls,'workflow_run_id':case.workflow_run_id,'failures':case.failures}
    resumed = []
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        resumed.extend(scheduler.tick_thread(case.task.target_thread_id, now=time.time()+COMPLETION_COALESCE_WINDOW_SECONDS+1))
        run, release, _ = _pending_candidate(case)
        if run.status == 'succeeded' or case.failures:
            break
        time.sleep(0.02)
    assert run.status == 'succeeded', {'status':run.status, 'tick':repr(resumed), 'main_calls':case.main_calls, 'child_calls':case.child_calls, 'failures':case.failures}
    assert not case.failures, case.failures
    raw = _assert_original_artifact(case, run, release.output_artifacts[0]['artifact_id'])
    assert json.loads(raw) == _ORACLE
    assert Path(release.output_snapshot['counter_path']).read_text() == '1'
    final = case.wire.call('tasks.get', {'task_id':case.task.task_id})
    assert final['task']['status'] == 'done'
    children = case.wire.call('agent_tree.get', {'space_id':case.context.space_id,
        'root_agent_id':case.task.target_thread_id})
    assert all(row.id in json.dumps(children) for row in rows), children
    # 原候选的review关联必须真实独立身份；不把测试driver算成reviewer。
    from agent_py_agent.agent.verification.repository import VerificationEvidenceRepository

    provenance = VerificationEvidenceRepository(case.agent.home_paths.owner_home_dir)
    review_ids = {row.id for row in rows}
    pending = release.escalation_result.get('pending_review') or {}
    reviews = provenance.list_independent_reviews(case.context.owner_id, requirement_id=pending['requirement_id'])
    assert len(reviews) == 1 and reviews[0].reviewer_principal_id in review_ids
    assert reviews[0].reviewer_principal_id != reviews[0].author_principal_id
    assert reviews[0].reviewed_input_version == pending['input_version']
    observed_run = case.wire.call('workflow_runs.get', {'workflow_run_id':run.workflow_run_id})
    assert observed_run['review_refs'] == release.escalation_result['review_refs']
    assert reviews[0].review_id in observed_run['review_refs']
    assert pending['business_context'] == case.context.to_dict()
    original_task = case.agent.conversation_store.session_tasks.require(case.task.task_id)
    assert original_task.sender_thread_id == case.task.sender_thread_id
    scheduler.tick_thread(original_task.sender_thread_id, now=time.time()+COMPLETION_COALESCE_WINDOW_SECONDS+2)
    final = case.wire.call('tasks.get', {'task_id':case.task.task_id})
    assert final['result']['report'] == {'guidance':'consumed','wake':'handled'}
    assert case.sender_calls == 1
    messages, errors = case.agent.conversation_store.messages.recent_report(original_task.sender_thread_id, limit=20)
    assert not errors and any(release.output_artifacts[0]['artifact_id'] in row.content for row in messages)
    # 重连点查仍是同一原件；不重复执行release或补写文件。
    assert _assert_original_artifact(case, WorkflowRunStore(case.agent.home_paths.owner_home_dir).load_run(run.workflow_run_id),
        release.output_artifacts[0]['artifact_id']) == raw
    assert Path(release.output_snapshot['counter_path']).read_text() == '1'


def test_managed_order_closeout_requires_original_link_completion(monkeypatch):
    """活子任务/未读结果保留accepted；只认原TaskLink完成，旧普通派活不新增门。"""
    from agent_py_agent.agent.conversation import (
        im_delegation_delivery,
        runtime,
        session_task_report,
    )

    closed = []
    monkeypatch.setattr(im_delegation_delivery, 'delegated_delivery_task', lambda *_a, **_k: None)
    monkeypatch.setattr(session_task_report, 'close_out_turn', lambda *_a: closed.append(True))
    task = SimpleNamespace(task_id='original-order', metadata={'business_context':{'task_id':'original-order'}})
    monkeypatch.setattr(session_task_report, 'task_for_turn', lambda *_a: task)
    request = SimpleNamespace(thread_id='controller', task_id='original-order')
    result = SimpleNamespace(runtime_status='', error=None, response='本回合回复', execution_identity={})
    for status, error, expected in [('active', None, False), (None, None, False),
                                    ('completed', ValueError('unreadable'), False), ('completed', None, True)]:
        def link_report(thread_id, task_id):
            assert (thread_id, task_id) == ('controller', 'original-order')
            return (SimpleNamespace(status=status) if status is not None else None), error
        agent = SimpleNamespace(conversation_store=SimpleNamespace(tasks=SimpleNamespace(link_report=link_report)))
        before = len(closed)
        runtime._close_out_session_task_turn(agent, request, result, turn_id='original-turn')
        assert (len(closed) == before + 1) is expected
    task.metadata = {}
    runtime._close_out_session_task_turn(agent, request, result, turn_id='original-turn')
    assert len(closed) == 2
