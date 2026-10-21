# LLM: 本片只经工具公开入口验证行为；所有资产和进程均在pytest临时Owner内，不接网络或模型。
# 模块用途: 守住脚本CRUD、源评估、工作流引用、Skill绑定、失败续跑和工具副作用声明。
import importlib
import importlib.util
import json

import pytest


# LLM: 未实现时用断言报红，不把导入异常当业务证据；接口构造不执行资产。
# 函数用途: 加载两个工具公共类型供定向测试。
@pytest.fixture
def api():
    names = [f'agent_py_agent.agent.tooling.{name}_tool' for name in ('scripts', 'workflows')]
    assert all(importlib.util.find_spec(name) is not None for name in names), '尚未实现脚本/工作流工具'
    return tuple(getattr(importlib.import_module(name), cls) for name, cls in zip(names, ('ScriptsTool', 'WorkflowsTool')))


# LLM: 请求样本采用c4的平铺描述符，无真实凭据或Owner路径。
# 函数用途: 提供完整标准提取Key和可增减的数据源。
def descriptor(asset_id='collect', kind='script'):
    return {'id': asset_id, 'kind': kind, 'name': '采集', 'access_targets': [],
            'data_sources': [{'source_id': 'apps', 'system': 'logs',
                              'query_scope': {'active_appnames': ['A', 'B', 'C'], 'region': 'west'},
                              'evaluated_candidates': []}],
            'credential_refs': [], 'io_contract': {'inputs': ['number'], 'outputs': ['value']}}


# LLM: 工具返回的ok和JSON同时核对，不以调用未抛异常充成功。
# 函数用途: 运行一次预期成功的工具动作并读取完整结构化输出。
def call(tool, action, **params):
    outcome = tool.execute({'action': action, **params})
    assert outcome.ok, outcome.output
    return json.loads(outcome.output)


# LLM: 真实脚本只输出对象；helper经register而非测试直接补资产文件。
# 函数用途: 通过本片工具创建一个能运行的固化脚本。
def register(tool, asset_id='collect', code='print(\'{"value": 1}\')', **options):
    return call(tool, 'register', script_id=asset_id, descriptor=descriptor(asset_id), code=code, **options)


# LLM: 工作流与c4 WorkflowDefinition同形，所有引用由工具保存时重算。
# 函数用途: 合成固定采集和可失败分析的两步链路。
def definition():
    return {'descriptor': descriptor('daily', 'workflow'), 'body_markdown': '# 每日链路\n', 'steps': [
        {'step_id': 'fetch', 'name': '采集', 'mode': 'deterministic_script', 'script_id': 'collect'},
        {'step_id': 'judge', 'name': '分析', 'mode': 'llm_skill_analysis', 'skill_ids': ['skill-A']}]}


# LLM: 列表须含全部标准Key且不带代码；重复注册与更新缺失不得隐式upsert。
# 函数用途: 验证脚本完整CRUD、持久化往返和Owner隔离。
def test_script_crud_and_summary(api, tmp_path):
    scripts, _ = api
    tool = scripts(tmp_path / 'one')
    register(tool)
    rows = call(tool, 'list')['scripts']
    assert len(rows) == 1
    assert {'id', 'name', 'access_targets', 'data_sources', 'credential_refs', 'used_by', 'runtime_summary'} <= rows[0].keys()
    assert 'code' not in rows[0]
    assert call(tool, 'get', script_id='collect')['code'] == 'print(\'{"value": 1}\')'
    assert not tool.execute({'action': 'register', 'script_id': 'collect', 'descriptor': descriptor(), 'code': 'pass'}).ok
    assert not tool.execute({'action': 'update', 'script_id': 'absent', 'code': 'pass'}).ok
    call(tool, 'update', script_id='collect', code='print(\'{"value": 2}\')')
    assert call(scripts(tmp_path / 'one'), 'get', script_id='collect')['code'].endswith('2}\')')
    assert call(scripts(tmp_path / 'two'), 'list')['scripts'] == []
    assert not scripts(tmp_path / 'two').execute({'action': 'get', 'script_id': 'collect'}).ok
    assert call(tool, 'delete', script_id='collect')['deleted'] is True
    assert call(tool, 'list')['scripts'] == []


# LLM: 标准Key缺失必须在写盘前拒绝，不能依赖dataclass默认值遮盖漏提取。
# 函数用途: 逐项验证register强制字段及ID一致性。
@pytest.mark.parametrize('field', ['name', 'access_targets', 'data_sources', 'credential_refs', 'io_contract'])
def test_required_extraction_keys(api, tmp_path, field):
    tool = api[0](tmp_path)
    data = descriptor()
    del data[field]
    result = tool.execute({'action': 'register', 'script_id': 'collect', 'descriptor': data, 'code': 'pass'})
    assert not result.ok and result.error_code == 'TOOL_INVALID_ARGUMENTS'
    assert not (tmp_path / 'scripts/collect/script.json').exists()


# LLM: 不允许参数中的ID或路径跨Owner；无效配置不写入半份元数据。
# 函数用途: 锁住路径穿越与可执行入口边界。
@pytest.mark.parametrize('params', [{'script_id': '../evil'}, {'entrypoint': '../evil.py'},
                                   {'entrypoint': 'script.json'}, {'timeout_seconds': 0},
                                   {'interpreter': 'python3 -c pass'}, {'descriptor': descriptor('wrong')}])
def test_invalid_script_registration(api, tmp_path, params):
    args = {'action': 'register', 'script_id': 'collect', 'descriptor': descriptor(), 'code': 'pass', **params}
    result = api[0](tmp_path).execute(args)
    assert not result.ok
    assert not (tmp_path / 'scripts/collect/script.json').exists()


# LLM: 源评估应保留不活跃备忘；只有active决定把候选加入指定列表，其他源字段不丢。
# 函数用途: 验证源范围增删、候选幂等替换和新源激活。
def test_source_patch_and_evaluated_candidates(api, tmp_path):
    tool = api[0](tmp_path)
    register(tool)
    call(tool, 'update_sources', script_id='collect', source_id='apps', scope_key='active_appnames', add=['E'], remove=['B'])
    candidate = {'candidate_id': 'D', 'decision': 'ignored', 'reason': '仅静态日志', 'sample_count': 3}
    call(tool, 'record_candidate', script_id='collect', source_id='apps', scope_key='active_appnames', candidate=candidate)
    saved = call(tool, 'get', script_id='collect')['data_sources'][0]
    assert saved['query_scope'] == {'active_appnames': ['A', 'C', 'E'], 'region': 'west'}
    assert saved['evaluated_candidates'][0]['decision'] == 'ignored'
    call(tool, 'update_sources', script_id='collect', source_id='apps', scope_key='active_appnames',
         candidate={**candidate, 'decision': 'active'})
    saved = call(tool, 'get', script_id='collect')['data_sources'][0]
    assert saved['query_scope']['active_appnames'] == ['A', 'C', 'E', 'D']
    assert [item['decision'] for item in saved['evaluated_candidates']] == ['ignored', 'active']
    assert all(item['sample_count'] == 3 for item in saved['evaluated_candidates'])
    assert not tool.execute({'action': 'update_sources', 'script_id': 'collect', 'source_id': 'absent', 'add': ['X']}).ok


# LLM: 单次覆盖只传runner；真实退出/JSON错误须使工具失败，不能把结构化业务失败报成功。
# 函数用途: 验证脚本真实执行、临时源覆盖及失败回执。
def test_script_run_and_override(api, tmp_path):
    tool = api[0](tmp_path)
    register(tool, code='import os,json; print(json.dumps({"value": json.loads(os.environ["MY_AGENT_DATA_SOURCES_JSON"])}))')
    path = tmp_path / 'scripts/collect/script.json'
    before = json.loads(path.read_text())['data_sources']
    result = call(tool, 'run', script_id='collect', override_data_sources={'active_appnames': ['Z']})
    assert result['output_data'] == {'value': {'active_appnames': ['Z']}}
    assert result['exit_code'] == 0 and result['duration_ms'] >= 0 and not result['needs_repair']
    assert json.loads(path.read_text())['data_sources'] == before
    call(tool, 'update', script_id='collect', code='import sys; sys.exit(9)')
    failed = tool.execute({'action': 'run', 'script_id': 'collect'})
    data = json.loads(failed.output)
    assert not failed.ok and data['exit_code'] == 9 and data['needs_repair'] is True


# LLM: 保存时覆盖陈旧refs；Skill只能挂分析/混合步，解绑后移除不再被其他步骤使用的引用。
# 函数用途: 验证工作流CRUD、说明文件、引用重算和Skill动态绑定。
def test_workflow_crud_refs_and_skills(api, tmp_path):
    _, workflows = api
    tool = workflows(tmp_path)
    data = definition()
    data['descriptor']['refs'] = {'scripts': ['stale'], 'skills': ['stale']}
    saved = call(tool, 'save', workflow_id='daily', definition=data)
    assert saved['descriptor']['refs']['scripts'] == ['collect']
    assert saved['descriptor']['refs']['skills'] == ['skill-A']
    assert (tmp_path / 'workflows/daily/WORKFLOW.md').read_text() == '# 每日链路\n'
    assert len(call(tool, 'list')['workflows']) == 1
    call(tool, 'bind_skill', workflow_id='daily', step_id='judge', skill_id='skill-B')
    call(tool, 'bind_skill', workflow_id='daily', step_id='judge', skill_id='skill-B')
    call(tool, 'unbind_skill', workflow_id='daily', step_id='judge', skill_id='skill-A')
    saved = call(workflows(tmp_path), 'get', workflow_id='daily')
    assert saved['steps'][1]['skill_ids'] == ['skill-B']
    assert saved['descriptor']['refs']['skills'] == ['skill-B']
    assert not tool.execute({'action': 'bind_skill', 'workflow_id': 'daily', 'step_id': 'fetch', 'skill_id': 'X'}).ok
    assert call(tool, 'delete', workflow_id='daily')['deleted'] is True
    assert call(tool, 'list')['workflows'] == []


# LLM: 实际引擎失败后重建工具再续跑；成功前缀不得重跑，inspect只读原账本。
# 函数用途: 从工具端到端验证失败断点与恢复后的每步输出。
def test_workflow_run_inspect_and_resume(api, tmp_path, monkeypatch):
    scripts, workflows = api
    module = importlib.import_module('agent_py_agent.agent.workflows')
    executions, original = [], module.ScriptRunner.run_script
    # LLM: 实际执行结果不替换，计数点覆盖工具到runner的真实调用。
    # 函数用途: 验证恢复分析步骤不会重新运行采集脚本。
    def observe(runner, script_id, *args, **kwargs):
        executions.append(script_id)
        return original(runner, script_id, *args, **kwargs)
    monkeypatch.setattr(module.ScriptRunner, 'run_script', observe)
    register(scripts(tmp_path))
    tool = workflows(tmp_path, engine=module.WorkflowEngine(tmp_path))
    call(tool, 'save', workflow_id='daily', definition=definition())
    outcome = tool.execute({'action': 'run', 'workflow_id': 'daily', 'inputs': {'number': 2}})
    first = json.loads(outcome.output)
    assert not outcome.ok and first['status'] == 'failed'
    assert [s['status'] for s in first['steps']] == ['succeeded', 'failed']
    run_id = first['workflow_run_id']
    assert call(workflows(tmp_path), 'inspect_run', workflow_run_id=run_id)['steps'] == first['steps']
    engine = importlib.import_module('agent_py_agent.agent.workflows').WorkflowEngine(
        tmp_path, analysis_handler=lambda spec, outputs, skills: {'decision': 'alert'})
    resumed = call(workflows(tmp_path, engine=engine), 'resume', workflow_run_id=run_id)
    assert resumed['status'] == 'succeeded' and resumed['final_output'] == {'decision': 'alert'}
    assert resumed['steps'][0] == first['steps'][0]
    assert executions == ['collect']
    assert not workflows(tmp_path / 'other').execute({'action': 'inspect_run', 'workflow_run_id': run_id}).ok


# LLM: 符号链接叶不得成为读写跳板，失败后外部哨兵逐字保持。
# 函数用途: 验证同Owner路径内的代码链接也拒绝读取或覆盖。
def test_symlink_code_refused(api, tmp_path):
    tool = api[0](tmp_path / 'home')
    register(tool)
    marker = tmp_path / 'marker'
    marker.write_text('keep')
    leaf = tmp_path / 'home/scripts/collect/main.py'
    leaf.unlink()
    leaf.symlink_to(marker)
    assert not tool.execute({'action': 'get', 'script_id': 'collect'}).ok
    assert not tool.execute({'action': 'update', 'script_id': 'collect', 'code': 'new'}).ok
    assert marker.read_text() == 'keep'


# LLM: 程序执行和删除必须走危险动作门，读取不能被误标成写；工具可被原Runtime正常冻结。
# 函数用途: 核对真实Schema、副作用和默认收起声明。
def test_tool_policies_and_schemas(api, tmp_path):
    from agent_py_agent.agent.tooling.models import ToolRuntime, tool_effect_for_runtime_policy
    for cls, reads in zip(api, (('list', 'get'), ('list', 'get', 'inspect_run'))):
        tool = cls(tmp_path)
        ToolRuntime(tool.model_spec, tool.runtime_policy, tool)
        assert tool.model_spec.hints.default_deferred
        for action in reads:
            assert tool_effect_for_runtime_policy(tool.runtime_policy, {'action': action}) == 'read_only'
        for action in ('run', 'delete'):
            assert tool_effect_for_runtime_policy(tool.runtime_policy, {'action': action}) == 'dangerous'
    assert 'override_data_sources' in api[0](tmp_path).model_spec.input_schema['properties']


# LLM: 纯参数预检不能创建Owner或调用后端；未知动作、错类型与条件缺参须在claim前拒绝。
# 函数用途: 验证直接调用及宿主preclaim验证共用参数规则。
@pytest.mark.parametrize(('which', 'params'), [
    (0, {'action': 'run'}), (0, {'action': 'register', 'script_id': 'x', 'code': 'pass'}),
    (0, {'action': 'record_candidate', 'script_id': 'x', 'source_id': 'apps'}),
    (0, {'action': 'run', 'script_id': 'x', 'override_data_sources': 'bad'}),
    (1, {'action': 'resume'}), (1, {'action': 'bind_skill', 'workflow_id': 'w', 'step_id': 's'}),
    (0, {'action': 'list', 'owner_home': '/not-authorized'}), (1, {'action': 'launch'}),
])
def test_preclaim_rejects_without_io(api, tmp_path, which, params):
    home = tmp_path / 'must-not-be-created'
    tool = api[which](home)
    result = tool.validate_invocation(params, None)
    assert result is not None and not result.ok
    assert not home.exists()
    assert not tool.execute(params).ok
    assert not home.exists()


# LLM: 注入对象也必须绑定当前Owner，不可把另一Owner的Store/Engine当正常依赖。
# 函数用途: 验证构造器拒绝明显跨Owner的领域实例。
def test_injected_backend_owner_must_match(api, tmp_path):
    from agent_py_agent.agent.assets.script_store import ScriptStore
    from agent_py_agent.agent.workflows import WorkflowEngine
    with pytest.raises(ValueError):
        api[0](tmp_path / 'one', store=ScriptStore(tmp_path / 'two'))
    with pytest.raises(ValueError):
        api[1](tmp_path / 'one', engine=WorkflowEngine(tmp_path / 'two'))


# LLM: 资产包暂缺时显式协议注入仍能工作；不改变正式领域入口或静默吞依赖损坏。
# 函数用途: 验证没有assets模块时的JSON Store接缝与缺依赖失败出口。
def test_lightweight_store_without_assets(api, tmp_path, monkeypatch):
    module = importlib.import_module('agent_py_agent.agent.tooling.scripts_tool')
    original = module.importlib.import_module
    data = {}

    # LLM: 仅模拟包尚未写入，其他导入走原解释器；不吞真实产品异常。
    # 函数用途: 制造可选模块缺失的精确观察点。
    def missing(name):
        if name.startswith('agent_py_agent.agent.assets'):
            raise ModuleNotFoundError(name=name)
        return original(name)

    # LLM: 小型协议后端明确持同形JSON，不执行脚本、不写真实Owner。
    # 类用途: 模拟独立实现的ScriptStore协议。
    class Store:
        # LLM: 返回独立记录，防止工具就地修改权威字典。
        # 函数用途: 读取一个脚本或表示不存在。
        def get_script(self, script_id):
            return json.loads(json.dumps(data.get(script_id)))

        # LLM: 只接工具已验证的描述符和代码，保持script.json形状。
        # 函数用途: 接受纯JSON描述符的存储协议。
        def save_script(self, desc, code, **options):
            data[desc['id']] = {**desc, **options, 'code': code}
            return data[desc['id']]

    monkeypatch.setattr(module.importlib, 'import_module', missing)
    tool = api[0](tmp_path, store=Store())
    register(tool)
    assert call(tool, 'get', script_id='collect')['code'] == 'print(\'{"value": 1}\')'
    assert not api[0](tmp_path).execute({'action': 'list'}).ok


# LLM: 从真实执行失败分类而不是日志文字得出工具失败；参数不能让退出码被掩盖。
# 函数用途: 覆盖JSON输出错误、缺字段和超时路径。
@pytest.mark.parametrize('code', ['print("not-json")', 'print("{}")', 'import time;time.sleep(3)'])
def test_script_run_repairable_errors(api, tmp_path, code):
    tool = api[0](tmp_path)
    register(tool, code=code, timeout_seconds=0.1)
    result = tool.execute({'action': 'run', 'script_id': 'collect'})
    body = json.loads(result.output)
    assert not result.ok and body['needs_repair'] is True and body['error_code']
    assert body['duration_ms'] >= 0


# LLM: 工作流显式回退只重跑后缀，空或非法步骤不能改变原运行。
# 函数用途: 验证from_step_id的真实续跑行为及坏目标拒绝。
def test_workflow_explicit_resume_from_step(api, tmp_path):
    from agent_py_agent.agent.workflows import WorkflowEngine
    register(api[0](tmp_path))
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda *args: {'done': True})
    tool = api[1](tmp_path, engine=engine)
    call(tool, 'save', workflow_id='daily', definition=definition())
    first = call(tool, 'run', workflow_id='daily')
    resumed = call(tool, 'resume', workflow_run_id=first['workflow_run_id'], from_step_id='judge')
    assert resumed['resumed_from_step_id'] == 'judge' and resumed['steps'][0] == first['steps'][0]
    assert not tool.execute({'action': 'resume', 'workflow_run_id': first['workflow_run_id'], 'from_step_id': 'absent'}).ok


# LLM: 使用原唯一ToolExecutor，不把直接execute当作已验证全宿主；拒绝回执不得执行脚本。
# 函数用途: 验证正式工具快照的列表读取与危险运行在宿主门前拒绝。
def test_canonical_executor_reads_and_denied_run(api, tmp_path):
    from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
    from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call
    tool = api[0](tmp_path)
    register(tool, code='from pathlib import Path;Path("ran").write_text("unexpected");print(\'{"value": 1}\')')
    listed = execute_canonical_test_call(tmp_path, tools={'scripts': tool}, tool_name='scripts', arguments={'action': 'list'})
    assert listed.result.ok and listed.result.handler_executed
    assert json.loads(listed.result.output)['scripts'][0]['id'] == 'collect'
    denied = execute_canonical_test_call(tmp_path, tools={'scripts': tool}, tool_name='scripts',
        arguments={'action': 'run', 'script_id': 'collect'},
        pre_handler_gate=lambda call: ToolHandlerOutcome('scripts', False, 'denied', error_code='TOOL_PERMISSION_DENIED'))
    assert not denied.result.ok and not denied.result.handler_executed
    assert not (tmp_path / 'scripts/collect/ran').exists()


# LLM: 轻量协议存在时也必须先守住工具ID边界，不把路径校验全部推给可选依赖。
# 函数用途: 证明路径样式编号在无IO预检就被拒绝。
@pytest.mark.parametrize(('which', 'params'), [
    (0, {'action': 'get', 'script_id': '../escape'}),
    (0, {'action': 'update', 'script_id': 'collect', 'entrypoint': '../evil.py'}),
    (1, {'action': 'resume', 'workflow_run_id': '../../other'}),
    (1, {'action': 'bind_skill', 'workflow_id': 'daily', 'step_id': 'judge', 'skill_id': '../secret'}),
])
def test_preclaim_rejects_path_ids(api, tmp_path, which, params):
    tool = api[which](tmp_path / 'must-not-be-created')
    refused = tool.validate_invocation(params, None)
    assert refused is not None and not refused.ok
    assert not (tmp_path / 'must-not-be-created').exists()


# LLM: 显式注入的执行器不能被默认模块的内部依赖绑住；模拟缺依赖仅用于验证导入顺序。
# 函数用途: 复现并锁住独立复核指出的runner注入问题。
def test_injected_runner_does_not_import_default_backend(api, tmp_path, monkeypatch):
    from types import SimpleNamespace
    module = importlib.import_module('agent_py_agent.agent.tooling.scripts_tool')
    original = module.importlib.import_module
    tool = api[0](tmp_path)
    register(tool)

    # LLM: 仅拒绝默认工作流模块导入，资产模块仍真实运行。
    # 函数用途: 制造默认实现内部缺依赖的环境。
    def broken(name):
        if name == 'agent_py_agent.agent.workflows':
            raise ModuleNotFoundError(name='synthetic_missing_dependency')
        return original(name)

    # LLM: 回调只模拟宿主已注入的执行协议，返回明确业务成功与输出。
    # 函数用途: 观察工具是否真正使用显式runner。
    def run_script(script_id, inputs, **options):
        return {'ok': True, 'duration_ms': 1, 'exit_code': 0, 'output_data': {'value': 8}, 'needs_repair': False}

    monkeypatch.setattr(module.importlib, 'import_module', broken)
    injected = api[0](tmp_path, runner=SimpleNamespace(run_script=run_script))
    assert call(injected, 'run', script_id='collect')['output_data'] == {'value': 8}


# LLM: active与remove同一候选不能同时持久化；冲突请求必须保持原权威范围和评估都未变。
# 函数用途: 复现并锁住独立复核指出的候选矛盾操作。
def test_candidate_active_remove_conflict_is_rejected(api, tmp_path):
    tool = api[0](tmp_path)
    register(tool)
    before = call(tool, 'get', script_id='collect')
    result = tool.execute({'action': 'record_candidate', 'script_id': 'collect', 'source_id': 'apps',
        'scope_key': 'active_appnames', 'candidate': {'candidate_id': 'D', 'decision': 'active',
        'reason': 'useful', 'sample_count': 1}, 'remove': ['D']})
    assert not result.ok
    assert call(tool, 'get', script_id='collect') == before


# LLM: 两工具实例须共享读改写锁；barrier只放大原读与提交窗口，绝不替实现合并数据。
# 函数用途: 复现并锁住独立复核指出的并发列表增量丢失。
def test_concurrent_source_adds_preserve_both_updates(api, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, BrokenBarrierError

    from agent_py_agent.agent.assets.script_store import ScriptStore
    register(api[0](tmp_path))
    barrier = Barrier(2)

    # LLM: 保留真实领域Store读写，只在公开读返回后短暂同步，使旧实现必定读到同一前像。
    # 类用途: 可重复触发工具读改写竞争，不修改正式Store。
    class RacingStore(ScriptStore):
        # LLM: 固定边界等待有上限；已序列化时第一调用超时后继续，第二调用读取最新值。
        # 函数用途: 对齐未加锁的两个读取，暴露丢更新。
        def get_script(self, script_id):
            value = super().get_script(script_id)
            try:
                barrier.wait(timeout=0.2)
            except BrokenBarrierError:
                pass
            return value

    # LLM: 每线程使用独立工具和Store实例，验证共享锁而非实例内锁。
    # 函数用途: 提交一项不相交的源增量。
    def add(value):
        return call(api[0](tmp_path, store=RacingStore(tmp_path)), 'update_sources', script_id='collect',
                    source_id='apps', scope_key='active_appnames', add=[value])

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert len(list(pool.map(add, ['E', 'F']))) == 2
    saved = call(api[0](tmp_path), 'get', script_id='collect')
    assert set(saved['data_sources'][0]['query_scope']['active_appnames']) == {'A', 'B', 'C', 'E', 'F'}


# LLM: 默认工具应提供结构化分析而非真实模型推理；技能只从本Owner目录匹配。
# 函数用途: 验证分析与混合步骤开箱即用，按ID、名称、标签发现技能。
@pytest.mark.parametrize('mode', ['llm_skill_analysis', 'hybrid'])
@pytest.mark.parametrize('match', ['id', 'name', 'summary', 'refs', 'fallback'])
def test_default_workflow_skill_analysis(api, tmp_path, mode, match):
    root = tmp_path / 'skills' / 'audit'
    root.mkdir(parents=True)
    (root / 'SKILL.md').write_text('---\nid: audit-id\nname: audit-name\ndescription: 日志异常分析\nrefs: {"skills": ["safety"]}\n---\n只读分析')
    register(api[0](tmp_path))
    spec = {'step_id': 'judge', 'name': '分析', 'mode': mode}
    if mode == 'hybrid':
        spec['script_id'] = 'collect'
    if match in ('id', 'name', 'fallback'):
        spec['skill_ids'] = [{'id': 'audit-id', 'name': 'audit-name', 'fallback': 'missing-id'}[match]]
    else:
        spec['skill_tags'] = ['日志异常' if match == 'summary' else 'safety']
    data = definition()
    data['steps'] = [spec]
    tool = api[1](tmp_path)
    call(tool, 'save', workflow_id='daily', definition=data)
    run = call(tool, 'run', workflow_id='daily')
    expected = ['missing-id'] if match == 'fallback' else ['audit-id']
    assert run['steps'][0]['resolved_skill_ids'] == expected
    assert run['steps'][0]['llm_involved'] is False
    assert run['final_output'] == {'step_id': 'judge', 'mode': mode, 'resolved_skills': expected,
                                   'analyzed_keys': ['judge'] if mode == 'hybrid' else [], 'status': 'analyzed'}


# LLM: 同形JSON字符串不是原生结构，预检拒绝时不能创建Owner或进入后端。
# 函数用途: 锁住dict/list参数不做字符串二次解析的现有合同。
@pytest.mark.parametrize(('which', 'params'), [
    (0, {'action': 'run', 'script_id': 'x', 'inputs': '{}'}),
    (0, {'action': 'run', 'script_id': 'x', 'override_data_sources': '[]'}),
    (0, {'action': 'update_sources', 'script_id': 'x', 'source_id': 'a', 'add': '["x"]'}),
    (1, {'action': 'save', 'workflow_id': 'x', 'definition': '{}'}),
    (1, {'action': 'run', 'workflow_id': 'x', 'inputs': '{}'}),
])
def test_native_json_parameters_only(api, tmp_path, which, params):
    home = tmp_path / 'absent'
    outcome = api[which](home).execute(params)
    assert not outcome.ok and outcome.error_code == 'TOOL_INVALID_ARGUMENTS'
    assert not home.exists()


# LLM: 未提供超时时沿用脚本配置，显式覆盖仅本次有效，不写回script.json。
# 函数用途: 用真实短超时配置验证工具没有硬编码默认30秒。
def test_run_inherits_script_timeout(api, tmp_path):
    tool = api[0](tmp_path)
    register(tool, code='import time;time.sleep(.2);print(\'{"value": 1}\')', timeout_seconds=.05)
    result = tool.execute({'action': 'run', 'script_id': 'collect'})
    assert not result.ok and json.loads(result.output)['error_code'] == 'SCRIPT_TIMEOUT'
    assert call(tool, 'run', script_id='collect', timeout_seconds=2)['output_data'] == {'value': 1}
    assert call(tool, 'get', script_id='collect')['timeout_seconds'] == .05


# LLM: refs空字段的键名和序列化标点不是技能标签，不得扩大匹配结果。
# 函数用途: 验证只出现于refs结构键名中的词不会误命中。
def test_default_resolver_does_not_match_reference_keys(api, tmp_path):
    root = tmp_path / 'skills' / 'audit'
    root.mkdir(parents=True)
    (root / 'SKILL.md').write_text('---\nname: audit\ndescription: 日志分析\n---\n正文')
    data = definition()
    data['steps'] = [{'step_id': 'judge', 'name': '分析', 'mode': 'llm_skill_analysis', 'skill_tags': ['scripts']}]
    tool = api[1](tmp_path)
    call(tool, 'save', workflow_id='daily', definition=data)
    run = call(tool, 'run', workflow_id='daily')
    assert run['steps'][0]['resolved_skill_ids'] == []
