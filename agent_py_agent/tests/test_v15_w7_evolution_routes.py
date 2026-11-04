"""收口冻结、确定性分类、字面量提升和独立来源复用的合同测试。"""
import ast
import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.mutation_ledger import (
    AssetMutationLedger,
    evolution_value_hash,
    freeze_asset_mutations,
)
from agent_py_agent.agent.assets.revision_history import blob_put
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.turn_extractor import get_extraction_proposal, save_evolution_draft
from agent_py_agent.agent.capability.asset_evolution import (
    AssetEvolutionService,
    classify_evolution_target,
    evolution_family_fingerprint,
    find_evolution_family,
)
from agent_py_agent.agent.capability.asset_evolution_routes import (
    lift_evolution_literals,
    route_new_script,
    write_evolution_json,
)
from agent_py_agent.agent.capability.skill_learning_request import (
    TRACE_ARGS_CHARS,
    build_learning_request,
)
from agent_py_agent.tests.test_v15_w7_replay_gate import InProcessReplayRunner


# LLM: 用完整顺序事实构造已验证target，不靠模型声明verified跳过执行证据。
# 函数用途: 创建本测试的冻结目标。
def verified_target(code, *, bug_fix=False, literals=()):
    sha = hashlib.sha256(code.encode()).hexdigest()
    facts = []
    if bug_fix:
        facts = [{'schema_version': 'asset_mutation.v1', 'sha256': 'a' * 64},
                 {'schema_version': 'asset_execution.v1', 'sha256': 'a' * 64, 'ok': False, 'error_code': 'SCRIPT_OUTPUT_INVALID'}]
    execution = {'schema_version': 'asset_execution.v1', 'sha256': sha, 'ok': True,
                 'call_id': 'verified-call', 'input_hash': evolution_value_hash({}),
                 'output_hash': evolution_value_hash({'result': '04:27'})}
    facts += [{'schema_version': 'asset_mutation.v1', 'sha256': sha}, execution]
    return {'kind': 'script', 'asset_id': None, 'path': 'sample.py', 'existing': False, 'verified': True,
            'final_sha256': sha, 'sha_history': [row['sha256'] for row in facts], 'facts': facts,
            'execution': execution, 'literals': list(literals), 'io_contract': {}, 'access_targets': []}


# LLM: 查询失败仍原子写tagged请求，但任何后台路线都不能产出候选或教训。
# 函数用途: 验证阻断标签与学习材料硬门。
def test_query_failed_tag_blocks_learning_and_evolution(tmp_path):
    state = {}
    receipt = {'ok': True, 'source_check': {'query_health': {'analysis_blocked': True}}}
    AssetMutationLedger().observe(state, 'query', {}, receipt, owner_home=tmp_path, workspace_root=tmp_path)
    req = freeze_asset_mutations(tmp_path, state, run_id='blocked')
    assert req['evidence_tags'] == ['query_failed'] and req['targets'] == []
    result = AssetEvolutionService(tmp_path).process('blocked')
    assert result == {'status': 'query_failed', 'notices': [], 'routes': []}
    ctx = SimpleNamespace(do_save=True, context_scope='default', source='foreground', tool_rounds=8,
                          archive_tool_calls=[{'result': receipt}])
    assert build_learning_request(ctx, 1) is None
    assert TRACE_ARGS_CHARS == 300
    assert not (tmp_path / 'data' / 'asset_evolution' / 'candidates').exists()
    assert not (tmp_path / 'memory').exists()


# LLM: 后改字节不是原成功执行的版本，即使有旧成功回执也不能冻结。
# 函数用途: 验证冻结重读同sha及时间顺序。
def test_freeze_rejects_later_mutation(tmp_path):
    work, home = tmp_path / 'work', tmp_path / 'owner'
    work.mkdir()
    home.mkdir()
    ledger, state = AssetMutationLedger(), {}
    path = work / 'a.py'
    path.write_text('pass\n')
    ledger.observe(state, 'write_file', {'path': 'a.py'}, {'ok': True}, owner_home=home, workspace_root=work)
    ledger.observe(state, 'run_command', {'command': 'python3 a.py'}, {'ok': True, 'exit_code': 0}, owner_home=home, workspace_root=work)
    path.write_text('print(1)\n')
    assert freeze_asset_mutations(home, state, run_id='stale') is None
    ledger.observe(state, 'edit_file', {'path': 'a.py'}, {'ok': True}, owner_home=home, workspace_root=work)
    assert freeze_asset_mutations(home, state, run_id='edited') is None


# LLM: 分类只看有序失败/变更/成功及完整新增字面量，不解析自然语言总结。
# 函数用途: 覆盖三种分类及失败目标。
@pytest.mark.parametrize('bug,literals,expected', [(False, (), 'verified'), (True, (), 'bug_fix'),
                                                (False, ('04:27',), 'specialized'), (True, ('04:27',), 'bug_fix')])
def test_classification_table(bug, literals, expected):
    code = 'def main(inputs):\n return {"result": "04:27"}\n'
    target = verified_target(code, bug_fix=bug, literals=literals)
    result = classify_evolution_target(target, code)
    assert result['classification'] == expected and result['verified'] is True
    target['facts'][-1]['ok'] = False
    assert classify_evolution_target(target, code)['verified'] is False


# LLM: 常量变化不分裂族，IO字段名变化必须分裂；同族命中正式Owner脚本后走补丁路线。
# 函数用途: 验证AST归一及同族去重。
def test_family_fingerprint_and_owner_dedup(tmp_path):
    a = 'def main(inputs):\n return {"result": "04:27", "count": 1}\n'
    b = a.replace('04:27', '12:00').replace('1}', '2}')
    family = evolution_family_fingerprint(a, {'outputs': ['result', 'count']})
    assert evolution_family_fingerprint(b, {'outputs': ['count', 'result']}) == family
    assert evolution_family_fingerprint(a, {'outputs': ['changed']}) != family
    ScriptStore(tmp_path).save_script(AssetDescriptor('fixed', 'script', 'fixed'), a)
    assert find_evolution_family(tmp_path, evolution_family_fingerprint(b)) == 'fixed'


# LLM: 完整字面量可提升，子串或main外常量不可提升；默认执行语义必须相同。
# 函数用途: 对提升规则做表驱动检查。
@pytest.mark.parametrize('literal,expression,lifted', [('04:27', '"04:27"', True),
    ('04:27', '"time=04:27"', False), ('job48', '"job48"', True), ('2026-11-04', '"2026-11-04"', True)])
def test_literal_lifting_table(literal, expression, lifted):
    source = f'VALUE = {expression}\ndef main(inputs):\n return {{"result": {expression}}}\n'
    result, defaults = lift_evolution_literals(source, [literal])
    assert bool(defaults) is lifted
    tree = ast.parse(result)
    assert isinstance(tree.body[0].value, ast.Constant)
    original_ns, lifted_ns = {}, {}
    exec(source, original_ns)
    exec(result, lifted_ns)
    assert lifted_ns['main']({}) == original_ns['main']({})


# LLM: 原输出hash由捕获账提供，提升后不等价绝不产生草稿。
# 函数用途: 验证等价失败硬门及相同族第二来源判定。
def test_route2_equivalence_and_second_independent_occurrence(tmp_path):
    code = '"""State invariant check."""\ndef main(inputs):\n return {"result": "04:27"}\n'
    target = verified_target(code, literals=['04:27'])
    now = datetime.now(timezone.utc).isoformat()
    request = {'run_id': 'r1', 'thread_id': 't1', 'created_at': now, 'member_of': [{'kind': 'skill', 'id': 'check'}]}
    family = evolution_family_fingerprint(code)
    target['execution']['output_hash'] = evolution_value_hash({'result': 'wrong'})
    assert route_new_script(tmp_path, request, target, code, family, runner_factory=InProcessReplayRunner)['reason'] == 'equivalence_mismatch'
    assert not (tmp_path / 'data' / 'asset_extractions').exists()
    target['execution']['output_hash'] = evolution_value_hash({'result': '04:27'})
    first = route_new_script(tmp_path, request, target, code, family, runner_factory=InProcessReplayRunner)
    assert first['status'] == 'draft' and first['independent_reuse'] is False
    same = route_new_script(tmp_path, {**request, 'run_id': 'r2'}, target, code, family, runner_factory=InProcessReplayRunner)
    assert same['independent_reuse'] is False
    second = route_new_script(tmp_path, {**request, 'run_id': 'r3', 'thread_id': 't2'}, target, code, family, runner_factory=InProcessReplayRunner)
    assert second['independent_reuse'] is True
    proposal = get_extraction_proposal(tmp_path, first['proposal_id'])
    assert proposal['candidate_scripts'][0]['descriptor']['metadata']['member_of'] == request['member_of']


# LLM: 过期草稿不能由确认入口发布，第二次独立采用必须处于有效期内。
# 函数用途: 验证十四天惰性到期。
def test_draft_expiry(tmp_path):
    now = datetime.now(timezone.utc)
    script = {'script_id': 'draft', 'title': 'draft', 'language': 'python', 'entry_source': 'def main(inputs):\n return {}\n',
              'input_schema': {'properties': {}}, 'output_schema': {'properties': {}}}
    proposal = save_evolution_draft(tmp_path, 'a' * 64, script, run_id='r', thread_id='t', created_at=(now - timedelta(days=15)).isoformat())
    with pytest.raises(ValueError, match='EXTRACTION_PROPOSAL_EXPIRED'):
        get_extraction_proposal(tmp_path, proposal['proposal_id'])


# LLM: 后台只消费准确run请求；重跑不创建第二份草稿，不再次发通知。
# 函数用途: 验证服务幂等与freeze请求的消费。
def test_background_service_and_independent_auto_promotion(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability import asset_evolution_routes as route_module
    from agent_py_agent.agent.workflows import script_runner

    monkeypatch.setattr(script_runner, 'ScriptRunner', InProcessReplayRunner)
    monkeypatch.setattr(route_module, 'original_evolution_inputs', lambda home, request, execution: {})
    # route_new_script局部从workflows导入，保留真实stage/store，仅替换执行器。
    import agent_py_agent.agent.workflows as workflows
    monkeypatch.setattr(workflows, 'ScriptRunner', InProcessReplayRunner)
    code = 'def main(inputs):\n return {"result": "04:27"}\n'
    target = verified_target(code, literals=['04:27'])
    blob_put(tmp_path, code.encode())
    request = {'schema_version': 'asset_evolution_request.v1', 'run_id': 'r1', 'thread_id': 't1',
               'created_at': datetime.now(timezone.utc).isoformat(), 'targets': [target], 'evidence_tags': []}
    write_evolution_json(tmp_path, ('data', 'asset_evolution', 'requests', 'r1.json'), request)
    service = AssetEvolutionService(tmp_path, maintenance_mode='autonomous')
    first = service.process('r1')
    assert first['routes'][0]['status'] == 'draft'
    assert service.process('r1')['duplicate'] is True
    second_request = {**request, 'run_id': 'r2', 'thread_id': 't2'}
    write_evolution_json(tmp_path, ('data', 'asset_evolution', 'requests', 'r2.json'), second_request)
    second = service.process('r2')
    assert second['routes'][0]['status'] == 'promoted'
    assert ScriptStore(tmp_path).get_script(second['routes'][0]['asset_id']) is not None


# LLM: verified标志不是执行证明，历史中已存在的字面量不是本次新引入的专化。
# 函数用途: 证伪缺执行事实与旧字面量的错误分类。
def test_classification_requires_success_and_new_literal():
    code = 'def main(inputs):\n return {"result": "04:27"}\n'
    target = verified_target(code, literals=['04:27'])
    target['facts'].pop()
    assert classify_evolution_target(target, code)['verified'] is False
    target = verified_target(code, literals=['04:27'])
    prior = {'schema_version': 'asset_mutation.v1', 'sha256': 'a' * 64,
             'literal_hashes': [hashlib.sha256(b'04:27').hexdigest()]}
    target['facts'].insert(0, prior)
    assert classify_evolution_target(target, code)['specialized'] is False


# LLM: 同回合两次执行不能被last_execution折叠，否则连续失败回滚门永远只计一次。
# 函数用途: 验证freeze保留所有有界执行事实且不把失败当候选。
def test_freeze_preserves_two_execution_failures(tmp_path):
    ScriptStore(tmp_path).save_script(AssetDescriptor('trial', 'script', 'trial'), 'def main(inputs):\n return {}\n')
    ledger, state = AssetMutationLedger(), {}
    for call_id in ('failed-a', 'failed-b'):
        ledger.observe(state, 'scripts', {'action': 'run', 'script_id': 'trial'},
                       {'ok': False, 'error_code': 'SCRIPT_OUTPUT_INVALID'},
                       owner_home=tmp_path, workspace_root=tmp_path, call_id=call_id)
    request = freeze_asset_mutations(tmp_path, state, run_id='two-failures')
    assert [row['execution']['call_id'] for row in request['executions']] == ['failed-a', 'failed-b']
    assert request['targets'] == []


# LLM: 接真实工具观察接缝、收口冻结、原学习FIFO及演进服务；只将OS执行器换成显式受控执行器。
# 函数用途: 验证失败修复后的新脚本在无模型调用时形成草稿和一次教训候选。
def test_capture_finalize_background_lane_without_model(tmp_path, monkeypatch):
    import agent_py_agent.agent.workflows as workflows
    from agent_py_agent.agent.agent_core import _finalization_service as finalization
    from agent_py_agent.agent.agent_core import _tool_loop_service as loop
    from agent_py_agent.agent.capability.skill_learning import SkillLearningService
    from agent_py_agent.agent.conversation import tool_discovery
    from agent_py_agent.agent.workflows import script_runner
    from agent_py_agent.tests.test_skill_learning import _ctx, _runtime

    runtime = _runtime(tmp_path)
    work = tmp_path / 'work'
    work.mkdir()
    agent = SimpleNamespace(root=work, home_paths=runtime.home, config=SimpleNamespace(enable_self_learning=True))
    params = SimpleNamespace(live_archive_state={}, run_id='seam-run', attempt_id='attempt',
                             in_use_method_ids=('owner:check',))
    monkeypatch.setattr(loop, '_record_tool_call_ir_if_native', lambda record, rendered: None)
    monkeypatch.setattr(tool_discovery, 'persist_discovered_tools', lambda agent, params, archive: None)
    monkeypatch.setattr(script_runner, 'ScriptRunner', InProcessReplayRunner)
    monkeypatch.setattr(workflows, 'ScriptRunner', InProcessReplayRunner)

    # LLM: 回执是本测试明确控制的执行事实，不声称已运行真实shell或OS沙箱。
    # 函数用途: 经产品post-archive挂点观察一次工具结果。
    def observe(tool, arguments, output, *, ok=True):
        record = SimpleNamespace(params=params, call=SimpleNamespace(tool_name=tool, arguments=arguments, call_id=tool + str(ok)),
                                 result=SimpleNamespace(ok=ok, output=output, error_code='' if ok else 'SCRIPT_OUTPUT_INVALID'))
        loop._record_tool_post_archive(agent, record, {'tool': tool}, 'unchanged')

    path = work / 'sample.py'
    path.write_text('def main(inputs):\n raise ValueError("invalid")\n')
    observe('write_file', {'path': 'sample.py'}, {})
    observe('run_command', {'command': 'python3 sample.py'}, {'return_code': 1}, ok=False)
    code = 'def main(inputs):\n return {"result": "04:27"}\n'
    path.write_text(code)
    observe('edit_file', {'path': 'sample.py'}, {})
    observe('run_command', {'command': 'python3 sample.py'}, {'return_code': 0, 'stdout': '{"result":"04:27"}'})
    ctx = _ctx('seam-run', attempt_id='attempt', user_prompt='核对 04:27 的结果', tool_rounds=2,
                tool_runtime_evidence={})
    finalization._freeze_turn_asset_mutations(agent, ctx)
    assert ctx.tool_runtime_evidence['asset_evolution_run_id'] == 'seam-run'
    service = SkillLearningService(runtime.store, runtime.service.settings, runtime.service.runtime)
    assert service.enqueue_from_finalize(ctx) == 'queued'
    result = service.run_pending()
    assert runtime.backend.prompts == [] and result.status == 'skipped'
    assert any(row['status'] == 'draft' for row in result.evolution_notices)
    assert not runtime.store.pending_requests()
    from agent_py_agent.agent.capability.asset_evolution_routes import read_evolution_json

    request = read_evolution_json(runtime.home.owner_home_dir, ('data', 'asset_evolution', 'requests', 'seam-run.json'))
    assert request['member_of'] == [{'kind': 'skill', 'id': 'check'}]
    assert request['targets'][0]['bug_fix'] is True
    assert any(row['status'] == 'lesson_candidate' for row in request['result']['routes'])
