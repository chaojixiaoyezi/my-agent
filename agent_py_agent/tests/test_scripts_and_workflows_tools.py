# LLM: 工具入口与真实受限子进程分开验证；网络仅用测试自建回环socket或无IO替身，不访问在线Gateway或模型。
# 模块用途: 守住脚本/工作流合同以及S1–S4的路径、metadata、环境和网络边界。
import importlib
import importlib.util
import json

import pytest


# LLM: FF-04使用真实Store读回代码，展示脱敏不能成为下次持久化的输入。
# 函数用途: 验证更新超时、描述符或代码时，磁盘代码保持原样且展示仍脱敏。
@pytest.mark.parametrize('field', ['timeout_seconds', 'descriptor', 'code'])
def test_script_update_preserves_raw_code_and_redacts_display(tmp_path, api, field):
    from agent_py_agent.agent.assets.script_store import ScriptStore

    tool = api[0](tmp_path / 'owner')
    code = 'token = 1\nprint("{}")\n'
    registered = tool.execute({'action': 'register', 'script_id': 'audit',
                              'descriptor': descriptor('audit'), 'code': code})
    assert registered.ok, registered.output
    replacement = 'token = 2\nprint("new")\n'
    patch = {'timeout_seconds': 30, 'descriptor': {**descriptor('audit'), 'name': '新说明'},
             'code': replacement}
    updated = tool.execute({'action': 'update', 'script_id': 'audit', field: patch[field]})
    assert updated.ok, updated.output
    expected = replacement if field == 'code' else code
    raw = ScriptStore(tool.owner_home).get_script('audit')['code']
    assert raw == expected
    compile(raw, '<stored-script>', 'exec')
    displayed = tool.execute({'action': 'get', 'script_id': 'audit'})
    assert displayed.ok, displayed.output
    assert json.loads(displayed.output)['code'] != expected
    assert '<redacted>' in displayed.output


# LLM: FF-11引用实际SkillsService快照；目录ID与逻辑中文稳定引用分别核对。
# 函数用途: 验证英文目录别名、中文稳定引用及中文真实目录均可保存并绑定工作流。
@pytest.mark.parametrize('directory,reference', [
    ('paper-digest', 'paper-digest'), ('paper-digest', 'workspace:论文摘要'),
    ('论文摘要', '论文摘要'), ('论文摘要', 'workspace:论文摘要'),
    ('中文目录', '中文目录'), ('中文目录', '论文摘要'), ('中文目录', 'workspace:论文摘要'),
])
def test_native_chinese_skill_snapshot_can_save_and_bind(tmp_path, api, directory, reference):
    from agent_py_agent.agent.assets import safe_asset_id
    from agent_py_agent.agent.assets.skill_adapter import get_owner_skill_descriptor
    from agent_py_agent.tests.test_ctxopt_skill_catalog import service

    home, workspace = tmp_path / 'owner', tmp_path / 'workspace'
    path = workspace / '.agents/skills' / directory / 'SKILL.md'
    path.parent.mkdir(parents=True)
    text = '---\nid: paper-digest\nname: 论文摘要\ndescription: 整理每日论文\n---\n生成结构化日报。'
    path.write_text(text)
    snapshot = service(home, workspace).snapshot_for()
    skill = get_owner_skill_descriptor(home, reference, skill_snapshot=snapshot, include_body=True)
    assert skill['metadata']['stable_id'] == 'workspace:论文摘要'
    assert skill['body_markdown'] == text
    assert safe_asset_id(skill['id']) == skill['id']
    discovered = get_owner_skill_descriptor(home, reference, workspace_root=workspace, include_body=True)
    assert discovered['metadata']['stable_id'] == skill['metadata']['stable_id']
    assert discovered['body_markdown'] == text
    tool = api[1](home, skill_snapshot_provider=lambda: snapshot)
    definition = {'descriptor': descriptor('audit', 'workflow'), 'steps': [
        {'step_id': 'judge', 'name': '分析', 'mode': 'llm_skill_analysis', 'skill_ids': [reference]}]}
    saved = tool.execute({'action': 'save', 'workflow_id': 'audit', 'definition': definition})
    assert saved.ok, saved.output
    bound = tool.execute({'action': 'bind_skill', 'workflow_id': 'audit', 'step_id': 'judge', 'skill_id': reference})
    assert bound.ok, bound.output
    stored = tool._store().get_workflow('audit')
    assert stored.steps[0].skill_ids == (reference,)
    assert all(safe_asset_id(key) == key for key in stored.descriptor.refs.skills)
    preview = tool.execute({'action': 'preview', 'workflow_id': 'audit'})
    assert preview.ok, preview.output
    assert json.loads(preview.output)['steps'][0]['resolved_skills'] == [reference]
    assert '生成结构化日报' in preview.output
    assert path.read_text() == text


# LLM: 仅放宽Skill逻辑名称，不放宽脚本/工作流磁盘ID或来源命名域。
# 函数用途: 锁住Unicode稳定引用的合法形状和严格磁盘ID的差别。
@pytest.mark.parametrize('reference', ['论文摘要', 'workspace:论文摘要', 'owner:中文技能',
    'shared:共享技能', 'builtin:内置技能', 'learned:历史技能', 'plugin:example:中文技能'])
def test_unicode_logical_skill_reference_is_not_a_directory_id(reference):
    from agent_py_agent.agent.assets import (
        canonical_skill_name,
        safe_asset_id,
        valid_skill_reference,
    )
    from agent_py_agent.agent.assets.skill_adapter import _skill_id

    assert valid_skill_reference(reference) == reference
    canonical = canonical_skill_name(reference)
    assert canonical == _skill_id(reference.rsplit(':', 1)[-1])
    assert safe_asset_id(canonical) == canonical
    with pytest.raises(ValueError):
        safe_asset_id(reference)


# LLM: Unicode不允许路径、点穿越、控制字符或静默strip；校验不得清洗身份。
# 函数用途: 验证不安全Skill引用仍在进入存储前被拒绝。
@pytest.mark.parametrize('reference', ['workspace:a..b', 'a..b', 'workspace:../x',
    'workspace:中文/技能', 'workspace:中文\\技能', 'workspace: 中文', 'workspace:中文 ',
    'workspace:中文\n', 'owner:中\x00文', 'workspace:中\n文', 'workspace:中\t文',
    'workspace:中:文', 'unknown:中文', ' 论文摘要', '论文摘要 '])
def test_unicode_skill_reference_rejects_unsafe_identity(reference):
    from agent_py_agent.agent.assets import valid_skill_reference

    with pytest.raises(ValueError):
        valid_skill_reference(reference)


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


# LLM: 成功引用必须有可读Skill，不让任意字符串绕过缺失校验。
# 函数用途: 给工具回归建立真实Owner技能。
def readable_skill(home, name):
    path = home / 'skills' / name / 'SKILL.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'---\nname: {name}\ndescription: fixture\n---\n真实分析方法。\n')


# LLM: 实际引擎失败后重建工具续跑，真实Skill存在但缺分析器时保留失败，成功前缀不得重跑。
# 函数用途: 从工具端到端验证失败断点与恢复后的每步输出。
def test_workflow_run_inspect_and_resume(api, tmp_path, monkeypatch):
    readable_skill(tmp_path, 'skill-A')
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


# LLM: 固定资产协议不再猜可选模块；注入Store接收正式描述符，业务行为保持可观测。
# 函数用途: 验证轻量Store注入遵守统一类型合同。
def test_lightweight_store_without_assets(api, tmp_path):
    from agent_py_agent.agent.assets.descriptor import AssetDescriptor
    data = {}

    # LLM: 小型协议后端明确持同形JSON，不执行脚本、不写真实Owner。
    # 类用途: 模拟独立实现的ScriptStore协议。
    class Store:
        # LLM: 返回独立记录，防止工具就地修改权威字典。
        # 函数用途: 读取一个脚本或表示不存在。
        def get_script(self, script_id):
            return json.loads(json.dumps(data.get(script_id)))

        # LLM: 只接工具验证的正式描述符，持久化形状仍是标准JSON。
        # 函数用途: 将统一值对象保存为独立记录。
        def save_script(self, desc, code, **options):
            assert isinstance(desc, AssetDescriptor)
            desc = desc.to_dict()
            data[desc['id']] = {**desc, **options, 'code': code}
            return data[desc['id']]

    tool = api[0](tmp_path, store=Store())
    register(tool)
    assert call(tool, 'get', script_id='collect')['code'] == 'print(\'{"value": 1}\')'
    assert call(api[0](tmp_path), 'list')['scripts'] == []


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
    readable_skill(tmp_path, 'skill-A')
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


# LLM: 显式注入执行器不能构造默认后端；固定导入替代旧importlib可选模块探针。
# 函数用途: 观察默认构造故障是否错误影响注入执行器。
def test_injected_runner_does_not_import_default_backend(api, tmp_path, monkeypatch):
    from types import SimpleNamespace
    module = importlib.import_module('agent_py_agent.agent.workflows.script_runner')
    tool = api[0](tmp_path)
    register(tool)

    # LLM: 默认后端构造明确失败，不能掩盖真实依赖异常。
    # 函数用途: 模拟默认runner依赖不可用。
    def broken(*args, **kwargs):
        raise ModuleNotFoundError(name='synthetic_missing_dependency')

    # LLM: 回调只模拟宿主已注入的执行协议，返回明确业务成功与输出。
    # 函数用途: 观察工具是否真正使用显式runner。
    def run_script(script_id, inputs, **options):
        return {'ok': True, 'duration_ms': 1, 'exit_code': 0, 'output_data': {'value': 8}, 'needs_repair': False}

    monkeypatch.setattr(module, 'ScriptRunner', broken)
    injected = api[0](tmp_path, runner=SimpleNamespace(run_script=run_script))
    assert call(injected, 'run', script_id='collect')['output_data'] == {'value': 8}
    assert not tool.execute({'action': 'run', 'script_id': 'collect'}).ok


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


# LLM: 默认run失败关闭，preview只读；真实回调须取得实际Skill与完整输入。
# 函数用途: 分别验收预览、实际分析及缺Skill硬失败。
@pytest.mark.parametrize('mode', ['llm_skill_analysis', 'hybrid'])
@pytest.mark.parametrize('match', ['id', 'name', 'summary', 'refs', 'fallback'])
def test_default_workflow_skill_analysis(api, tmp_path, mode, match):
    root = tmp_path / 'skills' / 'audit'
    root.mkdir(parents=True)
    (root / 'SKILL.md').write_text('---\nid: audit-id\nname: audit-name\ndescription: 日志异常分析\nrefs: {"skills": ["safety"]}\n---\n只读分析')
    register(api[0](tmp_path))
    spec = {'step_id': 'judge', 'name': '分析', 'mode': mode,
            'inputs_map': {'mapped': {'step_id': '$inputs', 'key': 'task'}}}
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
    refused = tool.execute({'action': 'run', 'workflow_id': 'daily', 'inputs': {'task': 'review'}})
    failed = json.loads(refused.output)
    assert not refused.ok and failed['status'] == 'failed'
    assert failed['steps'][0]['error_code'] == ('SKILL_NOT_FOUND' if match == 'fallback' else 'ANALYZER_UNAVAILABLE')
    assert failed['steps'][0]['llm_involved'] is False
    if match == 'fallback':
        assert not tool.execute({'action': 'preview', 'workflow_id': 'daily'}).ok
        return
    expected = ['audit-id'] if match == 'id' else ['audit-name'] if match == 'name' else ['owner:audit-name']
    preview = call(tool, 'preview', workflow_id='daily')
    assert preview['status'] == 'previewed' and preview['llm_involved'] is False
    assert preview['skill_summaries'] == [{'id': 'audit-id', 'name': 'audit-name',
        'summary': '日志异常分析', 'procedure_preview': '只读分析'}]

    # LLM: 消费真实完整Skill详情与输入，不将预览作为业务输出。
    # 函数用途: 验证工具到引擎的实际分析协议。
    def analyze(step, context, references):
        assert list(references) == expected
        assert context.run_inputs == {'task': 'review'} and context.step_inputs['mapped'] == 'review'
        assert '只读分析' in context.skill_details[0]['body_markdown']
        if mode == 'hybrid':
            assert context.prior_outputs['judge'] == {'value': 1}
        return {'decision': 'alert'}
    active = api[1](tmp_path, analysis_handler=analyze)
    run = call(active, 'run', workflow_id='daily', inputs={'task': 'review'})
    assert run['steps'][0]['resolved_skill_ids'] == expected
    assert run['steps'][0]['llm_involved'] and run['final_output'] == {'decision': 'alert'}


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
    tool = api[1](tmp_path, analysis_handler=lambda *args: {'done': True})
    call(tool, 'save', workflow_id='daily', definition=data)
    run = call(tool, 'run', workflow_id='daily')
    assert run['steps'][0]['resolved_skill_ids'] == []
    assert call(tool, 'preview', workflow_id='daily')['skill_summaries'] == []


# LLM: 工具使用真实资产与运行账本；修复输出必须经过Schema和脚本契约而不是直接改状态。
# 函数用途: 验证工具补丁恢复、摘要回写及全流程修复标记清除。
def test_tool_patched_resume_and_workflow_summary(api, tmp_path):
    scripts, workflows = api
    register(scripts(tmp_path), code='raise SystemExit(4)')
    tool = workflows(tmp_path)
    data = definition()
    data['steps'] = data['steps'][:1]
    call(tool, 'save', workflow_id='daily', definition=data)
    paused = json.loads(tool.execute({'action': 'run', 'workflow_id': 'daily'}).output)
    assert paused['status'] == 'paused_for_resume'
    assert 'patched_step_output' in tool.model_spec.input_schema['properties']
    bad = tool.execute({'action': 'resume', 'workflow_run_id': paused['workflow_run_id'], 'patched_step_output': {}})
    assert not bad.ok and bad.error_code == 'TOOL_INVALID_ARGUMENTS'
    final = call(workflows(tmp_path), 'resume', workflow_run_id=paused['workflow_run_id'], patched_step_output={'value': 12})
    assert final['status'] == 'succeeded' and final['steps'][0]['status'] == 'escalated_recovered'
    summary = call(tool, 'get', workflow_id='daily')['descriptor']['runtime_summary']
    assert summary['run_count'] == 2 and summary['last_status'] == 'succeeded'


# LLM: 显式预览脱敏截断，实际分析器仍须取得完整正文和输入，不能互相冒充。
# 函数用途: 校验静态预览与完整分析详情边界。
def test_default_analysis_loads_real_skill_summaries(api, tmp_path):
    root = tmp_path / 'skills' / 'audit'
    root.mkdir(parents=True)
    (root / 'SKILL.md').write_text('---\nid: audit-id\nname: audit\ndescription: 诊断步骤\n---\n先查错误\npassword="test-only-secret"\n' + '继续只读核验。' * 100)
    tool, data = api[1](tmp_path), definition()
    data['steps'] = [{'step_id': 'judge', 'name': '分析', 'mode': 'llm_skill_analysis', 'skill_tags': ['诊断']}]
    call(tool, 'save', workflow_id='daily', definition=data)
    preview = call(tool, 'preview', workflow_id='daily')
    row, = preview['skill_summaries']
    assert (row['id'], row['name'], row['summary']) == ('audit-id', 'audit', '诊断步骤')
    assert '先查错误' in row['procedure_preview'] and len(row['procedure_preview']) <= 200
    assert 'test-only-secret' not in row['procedure_preview']
    assert preview['llm_involved'] is False
    assert not tool.execute({'action': 'run', 'workflow_id': 'daily'}).ok

    # LLM: 合成分析器只验证完整上下文，不代表在线模型验收。
    # 函数用途: 证明分析正文没有被截成200字预览。
    def analyze(step, context, references):
        assert len(context.skill_details[0]['body_markdown']) > 200
        assert context.run_inputs == {'task': 'review'}
        return {'diagnosis': '核验完成'}
    run = call(api[1](tmp_path, analysis_handler=analyze), 'run', workflow_id='daily', inputs={'task': 'review'})
    assert run['final_output'] == {'diagnosis': '核验完成'} and run['steps'][0]['llm_involved']


# LLM: Full Access下run/resume不应反复弹审批，确定性脚本失败标failed而非unknown，仅进程清理未验证保unknown。
# 函数用途: 回归本地TUI创建脚本与工作流时的审批模式、确定性失败终态与无错暂停契约。
def test_full_access_approval_and_deterministic_effect_outcomes(api, tmp_path):
    from agent_py_agent.agent.local_storage import TOOL_OPERATION_FAILED, TOOL_OPERATION_UNKNOWN
    from agent_py_agent.agent.tooling.action_policy import ActionPolicy, ActionPolicyRequest
    from agent_py_agent.agent.tooling.tool_operation_coordinator import _operation_status_for_result
    from agent_py_agent.tests._tool_runtime_harness import (
        canonical_test_call,
        runtime_snapshot_for_tools,
    )

    scripts_cls, workflows_cls = api
    scripts, workflows = scripts_cls(tmp_path), workflows_cls(tmp_path)
    snapshot = runtime_snapshot_for_tools({'scripts': scripts, 'workflows': workflows}, run_id='r1')
    policy = ActionPolicy()
    for tool_name, args in (('scripts', {'action': 'run', 'script_id': 'collect'}),
                            ('workflows', {'action': 'run', 'workflow_id': 'daily'})):
        inv = canonical_test_call(snapshot, tool_name, args)
        auto_decision = policy.decide(ActionPolicyRequest(
            call=inv, runtime_snapshot=snapshot, workspace_root=tmp_path, approval_mode='auto', require_model_visibility=False))
        assert auto_decision.status == 'allow'
        ask_decision = policy.decide(ActionPolicyRequest(
            call=inv, runtime_snapshot=snapshot, workspace_root=tmp_path, approval_mode='ask', require_model_visibility=False))
        assert ask_decision.status == 'ask'

    # descriptor 缺省自动补齐 id/kind，且确定性退出返回 effect_outcome='failed' 而非 unknown
    desc_without_id_kind = {k: v for k, v in descriptor('collect', 'script').items() if k not in ('id', 'kind')}
    assert scripts.execute({'action': 'register', 'script_id': 'collect', 'descriptor': desc_without_id_kind, 'code': 'import sys; sys.exit(9)'}).ok
    failed = scripts.execute({'action': 'run', 'script_id': 'collect'})
    assert not failed.ok and failed.effect_outcome == 'failed'
    failed.handler_executed = True
    assert _operation_status_for_result(failed) == TOOL_OPERATION_FAILED

    invalid = scripts.execute({'action': 'register', 'script_id': 'bad', 'descriptor': {'name': 'x'}, 'code': 'pass'})
    assert not invalid.ok and invalid.effect_outcome == 'not_started'
    invalid.handler_executed = True
    assert _operation_status_for_result(invalid) == TOOL_OPERATION_FAILED

    from agent_py_agent.agent.tooling.scripts_tool import _outcome
    unverified = _outcome('scripts', {'escalation_context': {'cleanup_error': {'errno': 1}}}, False, 'TOOL_EXECUTION_FAILED')
    unverified.handler_executed = True
    assert unverified.effect_outcome == 'unknown' and _operation_status_for_result(unverified) == TOOL_OPERATION_UNKNOWN


# LLM: 确认值经显式fallback进入部署，原始输入不暗改；真实分析使用工作区Skill正文。
# 函数用途: 验证工作流可按初始输入分支并在无错确认点暂停与续跑。
def test_workflow_inputs_condition_pause_and_workspace_skill_discovery(api, tmp_path):
    scripts_cls, workflows_cls = api
    # LLM: 合成分析器只消费完整方法，不执行实际模型服务或部署。
    # 函数用途: 返回业务摘要而非静态预览。
    def analyze(step, context, references):
        assert '生成结构化日报' in context.skill_details[0]['body_markdown']
        return {'digest': context.skill_details[0]['name']}
    scripts, workflows = scripts_cls(tmp_path), workflows_cls(tmp_path, analysis_handler=analyze)
    ws_skill = tmp_path / 'workspace/.agent/skills/paper-digest'
    ws_skill.mkdir(parents=True)
    (ws_skill / 'SKILL.md').write_text('---\nid: paper-digest\nname: 论文摘要\ndescription: 整理每日论文\n---\n生成结构化日报。')

    call(scripts, 'register', script_id='recommend', descriptor={
        'name': '推荐模型', 'access_targets': [], 'data_sources': [], 'credential_refs': [],
        'io_contract': {'inputs': [], 'outputs': ['selected_model']}},
        code='import json; print(json.dumps({"selected_model": "qwen-7b", "_pause_for_resume": True}))')
    call(scripts, 'register', script_id='deploy', descriptor={
        'name': '部署模型', 'access_targets': [], 'data_sources': [], 'credential_refs': [],
        'io_contract': {'inputs': ['model'], 'outputs': ['deployed']}},
        code='import json, sys; d=json.load(sys.stdin); print(json.dumps({"deployed": d.get("model") or d.get("selected_model")}))')

    wf_def = {
        'descriptor': {'name': '按需确认部署', 'access_targets': [], 'data_sources': [], 'credential_refs': [], 'io_contract': {}},
        'body_markdown': '# 流程\n',
        'steps': [
            {'step_id': 'ask_model', 'name': '推荐并等待确认', 'mode': 'deterministic_script', 'script_id': 'recommend',
             'condition': {'step_id': '$inputs', 'key': 'model', 'equals': ''}},
            {'step_id': 'summarize', 'name': '技能分析', 'mode': 'llm_skill_analysis', 'skill_ids': ['paper-digest']},
            {'step_id': 'run_deploy', 'name': '执行部署', 'mode': 'deterministic_script', 'script_id': 'deploy',
             'inputs_map': {'model': {'step_id': 'ask_model', 'key': 'selected_model',
                                       'fallback': {'step_id': '$inputs', 'key': 'model'}}}},
        ],
    }
    call(workflows, 'save', workflow_id='deploy_flow', definition=wf_def)

    # 1) 未指定 model：在 ask_model 主动无错暂停（ok=True, status='paused_for_resume', needs_repair=False）
    paused_outcome = workflows.execute({'action': 'run', 'workflow_id': 'deploy_flow', 'inputs': {}})
    assert paused_outcome.ok, paused_outcome.output
    paused = json.loads(paused_outcome.output)
    assert paused['status'] == 'paused_for_resume' and paused['needs_repair'] is False
    assert paused['current_step_id'] == 'ask_model' and paused['failed_step_id'] == ''
    assert paused['final_output']['selected_model'] == 'qwen-7b'

    # 确认后续跑：注入 patched_step_output 后继续执行 summarize（发现 workspace/.agent/skills）与 run_deploy
    resumed = call(workflows, 'resume', workflow_run_id=paused['workflow_run_id'], patched_step_output={'selected_model': 'qwen-14b'})
    assert resumed['status'] == 'succeeded'
    assert resumed['steps'][1]['resolved_skill_ids'] == ['paper-digest']
    assert resumed['steps'][1]['output_snapshot'] == {'digest': '论文摘要'}
    assert resumed['final_output'] == {'deployed': 'qwen-14b'}
    assert resumed['steps'][0]['llm_involved'] is False
    assert resumed['steps'][0]['escalation_result']['resolution_origin'] == 'user'

    # 2) 已指定 model：跳过 ask_model，一次跑完无需暂停
    direct = call(workflows, 'run', workflow_id='deploy_flow', inputs={'model': 'deepseek-r1'})
    assert direct['status'] == 'succeeded'
    assert [s['status'] for s in direct['steps']] == ['skipped', 'succeeded', 'succeeded']
    assert direct['final_output'] == {'deployed': 'deepseek-r1'}


# LLM: Linux便携式runtime不在/usr只读根内，ScriptRunner必须将解析后的prefix/base_prefix作为public_read_roots挂入AttemptSandboxSpec。
# 函数用途: 回归Linux发行版软链runtime在bwrap沙箱内执行脚本时的只读挂载与可执行路径解析。
def test_script_runner_mounts_portable_python_runtime_roots(api, tmp_path, monkeypatch):
    from pathlib import Path

    from agent_py_agent.agent.workflows import script_runner

    releases_rt = tmp_path / 'releases' / 'v7' / 'runtime'
    (releases_rt / 'bin').mkdir(parents=True)
    real_bin = releases_rt / 'bin' / 'python3.11'
    real_bin.write_text('#!/bin/sh\n')
    real_bin.chmod(0o755)
    (releases_rt / 'bin' / 'python3').symlink_to('python3.11')
    current_link = tmp_path / 'current'
    current_link.symlink_to(tmp_path / 'releases' / 'v7')

    monkeypatch.setattr(script_runner.sys, 'executable', str(current_link / 'runtime' / 'bin' / 'python3'))
    monkeypatch.setattr(script_runner.sys, 'prefix', str(releases_rt))
    monkeypatch.setattr(script_runner.sys, 'base_prefix', str(releases_rt))

    exe, roots = script_runner._interpreter_runtime('python3')
    assert Path(exe).parent == (releases_rt / 'bin').resolve(strict=False)
    assert releases_rt.resolve(strict=False) in roots

    captured_specs = []
    orig_sandbox = script_runner.AttemptExecutionSandbox

    class _SpySandbox(orig_sandbox):
        def __init__(self, spec):
            captured_specs.append(spec)
            super().__init__(spec)

    monkeypatch.setattr(script_runner, 'AttemptExecutionSandbox', _SpySandbox)
    monkeypatch.setattr(script_runner, '_can_apply_sandbox', lambda *a, **k: False)
    owner = tmp_path / 'owner'
    register(api[0](owner))
    runner = script_runner.ScriptRunner(owner, use_sandbox=True)
    with runner._prepare('collect', {}, 10, None, None):
        pass
    assert len(captured_specs) == 1
    assert releases_rt.resolve(strict=False) in captured_specs[0].public_read_roots


# LLM: 真实Store、冻结代码和独立Python子进程，不启平台沙箱，结果只能证明Python守卫而非内核隔离。
# 函数用途: 执行仅访问合成临时数据的受限脚本。
def _sec_f_guard_run(api, tmp_path, code, *, allowed=(), network=True):
    from agent_py_agent.agent.workflows.script_runner import ScriptRunner

    owner = tmp_path / 'owner'
    register(api[0](owner), code=code)
    result = ScriptRunner(owner, use_sandbox=False, restrict_to_roots=True,
        public_read_roots=allowed, extra_write_roots=allowed).run_script('collect', network_access=network)
    assert result.ok, result.stderr_preview
    return result.output_data['value']


# LLM: 包含/tmp到/private/tmp的宿主别名；仅解析路径，不在这些真实目录创建文件。
# 函数用途: 验证容器、home父目录及严格祖先被识别，具体私有目录不被误删。
@pytest.mark.parametrize('path,home,broad', [
    ('/', '/deep/my/home', True), ('/tmp', '/deep/my/home', True),
    ('/private/tmp', '/deep/my/home', True), ('/private/var', '/deep/my/home', True),
    ('/Users', '/deep/my/home', True), ('/home', '/deep/my/home', True),
    ('/deep/my', '/deep/my/home', True), ('/deep/my', '/deep/my/folder/home', True),
    ('/deep/my/home', '/deep/my/home', False), ('/deep/other', '/deep/my/home', False),
])
def test_sec_f_broad_container_root_classification(path, home, broad):
    from pathlib import Path

    from agent_py_agent.agent.workflows import script_runner

    assert script_runner._is_broad_container_root(Path(path), Path(home)) is broad


# LLM: 不调用网络，只捕获真实_prepare构造的Attempt规格与守卫载荷，网络关闭也须保留守卫端口列表。
# 函数用途: 验证Gateway端口在普通与受限脚本两种规格中接通。
@pytest.mark.parametrize('restricted', [False, True])
@pytest.mark.parametrize('network', [False, True])
def test_sec_f_prepare_projects_gateway_ports(api, tmp_path, monkeypatch, restricted, network):
    from agent_py_agent.agent.attempt import sandbox as attempt
    from agent_py_agent.agent.workflows import script_runner

    monkeypatch.setattr(attempt, '_LOCAL_GATEWAY_PORTS', {65431})
    captured = []
    original = script_runner.AttemptExecutionSandbox

    # LLM: 只记录传入实际沙箱类的不可变规格，不替换规格或假造构造结果。
    # 类用途: 观察真实runner传递的安全策略。
    class Observer(original):
        # LLM: 保留真正构造器，只把规格交给断言观察。
        # 函数用途: 捕获本次沙箱合同。
        def __init__(self, spec):
            captured.append(spec)
            super().__init__(spec)

    monkeypatch.setattr(script_runner, 'AttemptExecutionSandbox', Observer)
    monkeypatch.setattr(script_runner, '_can_apply_sandbox', lambda *a, **k: False)
    owner = tmp_path / 'owner'
    register(api[0](owner))
    runner = script_runner.ScriptRunner(owner, restrict_to_roots=restricted, workspace_root=tmp_path)
    with runner._prepare('collect', {}, 10, None, network) as prepared:
        assert captured[0].deny_gateway_ports == ((65431,) if network else ())
        if restricted:
            cfg = json.loads(prepared[2][2]['MY_AGENT_SCRIPT_GUARD_JSON'])
            assert cfg.get('denied_ports') == [65431]


# LLM: 宽workspace的祖先不能成为私有写禁止根；临时资产全在本测试树内。
# 函数用途: 验证_prepare不会把workspace自身或祖先传成普通写禁止挂载。
def test_sec_f_prepare_excludes_workspace_ancestors(api, tmp_path, monkeypatch):
    from agent_py_agent.agent.workflows import script_runner

    captured = []
    original = script_runner.AttemptExecutionSandbox

    # LLM: 观察真实规格，不更改私有根计算或路径解析。
    # 类用途: 捕获私有根与普通写禁止根。
    class Observer(original):
        # LLM: 保持真实规格类的初始化行为。
        # 函数用途: 记录生产runner交来的规格。
        def __init__(self, spec):
            captured.append(spec)
            super().__init__(spec)

    monkeypatch.setattr(script_runner, 'AttemptExecutionSandbox', Observer)
    monkeypatch.setattr(script_runner, '_can_apply_sandbox', lambda *a, **k: False)
    owner = tmp_path / 'owner'
    register(api[0](owner))
    runner = script_runner.ScriptRunner(owner, restrict_to_roots=True, workspace_root=tmp_path)
    with runner._prepare('collect', {}, 10, None, False):
        assert owner.resolve() in captured[0].write_scope_denied_roots
        assert all(not tmp_path.resolve().is_relative_to(root) for root in captured[0].write_scope_denied_roots)
    canonical_owner = tmp_path / 'agent_home' / 'owners' / 'local' / 'main'
    (canonical_owner / 'workspace').mkdir(parents=True)
    register(api[0](canonical_owner))
    canonical_runner = script_runner.ScriptRunner(
        canonical_owner, restrict_to_roots=True, workspace_root=canonical_owner / 'workspace'
    )
    with canonical_runner._prepare('collect', {}, 10, None, False):
        assert canonical_owner.resolve() in captured[1].private_read_roots
        assert (tmp_path / 'agent_home' / 'owners').resolve() in captured[1].private_read_roots


# LLM: 真实子进程调用所有公开metadata入口及原生posix别名，只看拒绝而不读取任何真实秘密。
# 函数用途: 防止stat、lstat、access与pathlib通过元数据旁路暴露私有文件。
@pytest.mark.parametrize('operation', ['os.stat', 'os.lstat', 'os.access', 'posix.stat', 'posix.lstat',
    'posix.access', 'pathlib.Path.stat', 'pathlib.Path.exists', 'pathlib.Path.is_file'])
def test_sec_f_guard_denies_private_metadata(api, tmp_path, operation):
    private = tmp_path / 'owner' / 'private-marker'
    private.parent.mkdir()
    private.write_text('synthetic-marker')
    if operation.startswith('pathlib'):
        method = operation.rsplit('.', 1)[-1]
        expression = f'pathlib.Path({str(private)!r}).{method}()'
    else:
        expression = f'{operation}({str(private)!r}' + (', os.F_OK)' if operation.endswith('access') else ')')
    boolean = operation.endswith(('exists', 'is_file', 'access'))
    code = f'''import json, os, pathlib, posix
try:
    observed = {expression}
except PermissionError:
    denied = True
else:
    denied = not observed if {boolean!r} else False
print(json.dumps({{"value": denied}}))
'''
    assert _sec_f_guard_run(api, tmp_path, code) is True


# LLM: dir_fd可让相对名绕开绝对路径检查；打开的目录本身合法，拒绝必须发生在metadata调用。
# 函数用途: 覆盖三个OS入口及其原生别名的dir_fd旁路。
@pytest.mark.parametrize('operation', ['os.stat', 'os.lstat', 'os.access', 'posix.stat', 'posix.lstat', 'posix.access'])
def test_sec_f_guard_rejects_metadata_dir_fd(api, tmp_path, operation):
    allowed = tmp_path / 'owner' / 'allowed'
    allowed.mkdir(parents=True)
    (allowed.parent / 'private-marker').write_text('synthetic-marker')
    extra = ', os.F_OK' if operation.endswith('access') else ''
    code = f'''import json, os, posix
fd = os.open({str(allowed)!r}, os.O_RDONLY)
try:
    try:
        {operation}('../private-marker'{extra}, dir_fd=fd)
    except PermissionError:
        denied = True
    else:
        denied = False
finally:
    os.close(fd)
print(json.dumps({{"value": denied}}))
'''
    assert _sec_f_guard_run(api, tmp_path, code, allowed=(allowed,)) is True


# LLM: 祖先metadata、文件描述符、重入解析、tempfile与标准库导入都须继续工作，不能通过一律拒绝修复。
# 函数用途: 验证合法metadata路径与受限脚本的正常运行。
def test_sec_f_guard_allows_ancestor_metadata_and_resolve(api, tmp_path):
    allowed = tmp_path / 'owner' / 'allowed'
    allowed.mkdir(parents=True)
    marker = allowed / 'marker'
    marker.write_text('synthetic-marker')
    code = f'''import json, os, pathlib, tempfile
path = pathlib.Path({str(marker)!r})
for parent in path.parents:
    os.stat(parent)
    os.lstat(parent)
    assert os.access(parent, os.F_OK)
assert path.resolve().is_file()
fd = os.open(path, os.O_RDONLY)
try:
    assert os.stat(fd).st_size == len('synthetic-marker')
finally:
    os.close(fd)
with tempfile.TemporaryFile() as temporary:
    temporary.write(b'ok')
print(json.dumps({{"value": True}}))
'''
    assert _sec_f_guard_run(api, tmp_path, code, allowed=(allowed,)) is True


# LLM: 只准检查获准根内的symlink本身；跟随叶链接或经过父目录链接都不能泄露私有目标metadata。
# 函数用途: 验证lstat、不跟随stat/access与祖先symlink的边界。
@pytest.mark.parametrize('operation,relative,denied', [
    ('os.lstat', 'leaf', False), ('os.stat', 'leaf', True),
    ('os.stat', 'leaf', False), ('os.access', 'leaf', False),
    ('os.lstat', 'parent/private-marker', True),
    ('os.lstat', 'parent/../private/private-marker', True),
    ('os.lstat', 'parent/.', True), ('os.lstat', 'parent/', True),
])
def test_sec_f_guard_metadata_symlink_policy(api, tmp_path, operation, relative, denied):
    allowed, private = tmp_path / 'owner/allowed', tmp_path / 'owner/private'
    allowed.mkdir(parents=True)
    private.mkdir()
    (private / 'private-marker').write_text('synthetic-marker')
    (allowed / 'leaf').symlink_to(private / 'private-marker')
    (allowed / 'parent').symlink_to(private, target_is_directory=True)
    extra = ', os.F_OK' if operation == 'os.access' else ''
    extra += ', follow_symlinks=False' if not denied and operation != 'os.lstat' else ''
    code = f'''import json, os
try:
    {operation}({(str(allowed) + '/' + relative)!r}{extra})
except PermissionError:
    denied = True
else:
    denied = False
print(json.dumps({{"value": denied}}))
'''
    assert _sec_f_guard_run(api, tmp_path, code, allowed=(allowed,)) is denied


# LLM: os.stat不展开用户目录；守卫不能把字面~root错误验证成真实root家目录后访问另一个目标。
# 函数用途: 验证原生OS路径的字面波浪号与符号链接目标一致。
def test_sec_f_guard_literal_tilde_path_is_not_expanded(api, tmp_path):
    allowed = tmp_path / 'owner/allowed'
    allowed.mkdir(parents=True)
    private = allowed.parent / 'private-marker'
    private.write_text('synthetic-marker')
    (allowed / '~root').symlink_to(private)
    code = f'''import json, os
os.chdir({str(allowed)!r})
try:
    os.stat('~root')
except PermissionError:
    denied = True
else:
    denied = False
print(json.dumps({{"value": denied}}))
'''
    assert _sec_f_guard_run(api, tmp_path, code, allowed=(allowed,)) is True


# LLM: 所有网络只到本测试自建回环socket；注册当前真实临时端口，不触碰正在运行的Gateway或外部网络。
# 函数用途: 通过真实TCP/UDP证明四个socket出口在开关两态下均守住Gateway端口。
@pytest.mark.parametrize('operation', ['connect', 'connect_ex', 'sendto', 'create_connection'])
@pytest.mark.parametrize('network', [False, True])
def test_sec_f_guard_blocks_registered_gateway_endpoint(api, tmp_path, operation, network):
    import socket

    from agent_py_agent.agent.attempt import sandbox as attempt

    kind = socket.SOCK_DGRAM if operation == 'sendto' else socket.SOCK_STREAM
    with socket.socket(socket.AF_INET, kind) as server:
        server.bind(('127.0.0.1', 0))
        if kind == socket.SOCK_STREAM:
            server.listen(1)
        port = server.getsockname()[1]
        attempt.register_gateway_bound_port(port)
        call = ('socket.create_connection(address, timeout=0.2)' if operation == 'create_connection' else
                "client.sendto(b'synthetic-marker', address)" if operation == 'sendto' else f'client.{operation}(address)')
        code = f'''import json, socket
address = ('127.0.0.1', {port})
with socket.socket(socket.AF_INET, {int(kind)}) as client:
    client.settimeout(0.2)
    try:
        result = {call}
        if {operation!r} == 'create_connection':
            result.close()
    except PermissionError:
        denied = True
    else:
        denied = False
print(json.dumps({{"value": denied}}))
'''
        try:
            assert _sec_f_guard_run(api, tmp_path, code, network=network) is True
            server.settimeout(0.02)
            with pytest.raises(TimeoutError):
                server.recvfrom(1) if kind == socket.SOCK_DGRAM else server.accept()
        finally:
            attempt.unregister_gateway_bound_port(port)


# LLM: 网络授权不能被修复成一律断网；只连接本测试未登记的回环TCP/UDP端口。
# 函数用途: 验证四种正常网络调用仍保留原返回语义。
@pytest.mark.parametrize('operation', ['connect', 'connect_ex', 'sendto', 'create_connection'])
def test_sec_f_guard_allows_unregistered_endpoint(api, tmp_path, operation):
    import socket

    kind = socket.SOCK_DGRAM if operation == 'sendto' else socket.SOCK_STREAM
    with socket.socket(socket.AF_INET, kind) as server:
        server.bind(('127.0.0.1', 0))
        if kind == socket.SOCK_STREAM:
            server.listen(1)
        port = server.getsockname()[1]
        call = ('socket.create_connection(address, timeout=0.2)' if operation == 'create_connection' else
                "client.sendto(b'synthetic-marker', 0, address)" if operation == 'sendto' else f'client.{operation}(address)')
        code = f'''import json, socket
address = ('127.0.0.1', {port})
with socket.socket(socket.AF_INET, {int(kind)}) as client:
    client.settimeout(0.2)
    result = {call}
    if {operation!r} == 'create_connection':
        result.close()
    elif {operation!r} == 'connect_ex':
        assert result == 0
print(json.dumps({{"value": True}}))
'''
        assert _sec_f_guard_run(api, tmp_path, code) is True
        server.settimeout(0.2)
        if kind == socket.SOCK_DGRAM:
            assert server.recvfrom(100)[0] == b'synthetic-marker'
        else:
            accepted, _ = server.accept()
            accepted.close()


# LLM: 内层在一次用户PathLike转换中探测私有metadata；TLS只能豁免内部Path解析，不能豁免此用户回调。
# 函数用途: 防止重入保护意外放行__fspath__回调，并确保路径只转换一次。
def test_sec_f_guard_checks_user_pathlike_before_tls_exemption(api, tmp_path):
    allowed = tmp_path / 'owner/allowed'
    allowed.mkdir(parents=True)
    marker, private = allowed / 'marker', allowed.parent / 'private-marker'
    marker.write_text('allowed')
    private.write_text('private')
    code = f'''import json, os
calls = []
class PathLike:
    def __fspath__(self):
        try:
            os.stat({str(private)!r})
        except PermissionError:
            calls.append(True)
        else:
            calls.append(False)
        return {str(marker)!r}
assert os.stat(PathLike()).st_size == len('allowed')
print(json.dumps({{"value": calls == [True]}}))
'''
    assert _sec_f_guard_run(api, tmp_path, code, allowed=(allowed,)) is True


# LLM: 真正启动线程，检查TLS不是跨线程的全局豁免，合法stat与私有拒绝同时保持。
# 函数用途: 验证并发脚本线程中的metadata边界。
def test_sec_f_guard_metadata_checks_are_thread_local(api, tmp_path):
    allowed = tmp_path / 'owner/allowed'
    allowed.mkdir(parents=True)
    marker, private = allowed / 'marker', allowed.parent / 'private-marker'
    marker.write_text('allowed')
    private.write_text('private')
    code = f'''import json, os
from concurrent.futures import ThreadPoolExecutor
def check(_):
    assert os.stat({str(marker)!r}).st_size == len('allowed')
    try:
        os.stat({str(private)!r})
    except PermissionError:
        return True
    return False
with ThreadPoolExecutor(max_workers=4) as pool:
    passed = all(pool.map(check, range(12)))
print(json.dumps({{"value": passed}}))
'''
    assert _sec_f_guard_run(api, tmp_path, code, allowed=(allowed,)) is True


# LLM: os的公开能力集合也保存原生函数引用，不能只替换os/posix属性却留下可直接调用的旧stat。
# 函数用途: 验证能力探测集合中的metadata入口同样执行守卫。
@pytest.mark.parametrize('capability', ['supports_dir_fd', 'supports_fd', 'supports_follow_symlinks', 'supports_effective_ids'])
def test_sec_f_guard_metadata_capability_aliases_are_checked(api, tmp_path, capability):
    private = tmp_path / 'owner/private-marker'
    private.parent.mkdir()
    private.write_text('synthetic-marker')
    code = f'''import json, os
outcomes = []
for function in getattr(os, {capability!r}):
    name = function.__name__.removeprefix('_guarded_')
    if name not in ('stat', 'lstat', 'access'):
        continue
    try:
        if name == 'access':
            function({str(private)!r}, os.F_OK)
        else:
            function({str(private)!r})
    except PermissionError:
        outcomes.append(True)
    else:
        outcomes.append(False)
print(json.dumps({{"value": bool(outcomes) and all(outcomes)}}))
'''
    assert _sec_f_guard_run(api, tmp_path, code) is True


# LLM: 仅替换底层四种原始网络调用；真实守卫在独立解释器初始化，因此不会向任何本机或外部服务探测。
# 函数用途: 按地址矩阵验证所有网络包装均在委派前阻断回环Gateway，同时保留非本机或其它端口。
@pytest.mark.parametrize('operation', ['connect', 'connect_ex', 'sendto', 'create_connection'])
@pytest.mark.parametrize('host,port,denied', [
    ('', 65431, True), ('0.0.0.0', 65431, True), ('::', 65431, True), ('::1', 65431, True),
    ('localhost', 65431, True), ('LOCALHOST.', 65431, True), ('127.4.5.6', 65431, True),
    ('::ffff:127.0.0.1', 65431, True), ('::ffff:7f00:1', 65431, True), ('0:0:0:0:0:0:0:1', 65431, True),
    ('2130706433', 65431, True), ('0x7f000001', 65431, True), ('127.0.0.1', 65432, False),
    ('192.0.2.1', 65431, False), ('2001:db8::1', 65431, False),
])
def test_sec_f_guard_network_address_matrix(tmp_path, operation, host, port, denied):
    import os
    import subprocess
    import sys

    from agent_py_agent.agent.workflows import script_runner

    entry = tmp_path / 'entry.py'
    expression = ('socket.create_connection(address)' if operation == 'create_connection' else
        "client.sendto(b'synthetic', 0, address)" if operation == 'sendto' else f'client.{operation}(address)')
    entry.write_text(f'''import json, socket
address = ({host!r}, {port}, 0, 0) if ':' in {host!r} else ({host!r}, {port})
with socket.socket() as client:
    try:
        {expression}
    except PermissionError:
        denied = True
    else:
        denied = False
print(json.dumps([denied, socket._sec_f_native_calls]))
''')
    bootstrap = '''import socket,sys
socket._sec_f_native_calls=[]
def record(*args, **kwargs):
    socket._sec_f_native_calls.append(True)
    return 0
socket.socket.connect=socket.socket.connect_ex=socket.socket.sendto=socket.create_connection=record
exec(sys.stdin.read())
'''
    cfg = {'read_roots': [str(tmp_path)], 'write_roots': [str(tmp_path)],
        'network_access': True, 'denied_ports': [65431]}
    env = {'PATH': os.defpath, 'MY_AGENT_SCRIPT_GUARD_JSON': json.dumps(cfg)}
    result = subprocess.run([sys.executable, '-I', '-B', '-c', bootstrap, str(entry)], cwd=tmp_path,
        env=env, input=script_runner._PYTHON_GUARD_CODE, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [denied, [] if denied else [True]]
