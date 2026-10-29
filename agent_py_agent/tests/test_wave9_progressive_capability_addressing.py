# LLM: 用真实Agent/冻结快照/线程与节点JSON验证渐进寻址；仅在临时目录准备输入，查询不能写盘、解密或联网。
# 模块用途: 守住12领域总览、4个按需分块、权限边界与Level0唯一注入及瘦身合同。
from dataclasses import replace
from pathlib import Path

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.capability.skill_snapshot import SkillSnapshot, SkillSnapshotEntry
from agent_py_agent.agent.prompting_parts.builder import (
    SELF_MAP_GUIDANCE,
    PromptBuilder,
    PromptBuildRequest,
    ToolSections,
)
from agent_py_agent.agent.tooling.self_map_registry import SectionDescriptor
from agent_py_agent.tests.test_self_map_runtime import _bytes, _read
from agent_py_agent.tests.test_self_map_runtime import make_agent as make_agent

_TOP = {'directories', 'tools', 'skills', 'packages', 'plugins', 'mcp', 'memory', 'models', 'config', 'work', 'permissions', 'context'}
_LEVEL1 = {'remote_peers', 'settings', 'schedules', 'assets'}


# LLM: 原Store的裸get_or_create支持未归属兼容记录；测试显式补当前owner身份，不能用裸记录冒充已认证通道。
# 函数用途: 在准备阶段创建真实同owner会话和绑定，不属于被测只读查询。
def owner_thread(agent, name, channel='local'):
    row = agent.conversation_store.threads.get_or_create({'channel': channel, 'channel_conversation_id': name})
    return agent.conversation_store.threads.update_atomic(row.thread_id, lambda current: replace(current,
        owner_id=agent.home_paths.owner_id, owner_home=str(agent.home_paths.owner_home_dir)))


# LLM: 原默认描述符仍进入总览，专项只登记一次；总览不能偷读隐藏分块来膨胀成本。
# 函数用途: 钉住总览12项、完整枚举16项及默认include标志。
def test_level1_registered_but_overview_stays_exactly_twelve(make_agent, monkeypatch):
    agent = make_agent()
    tool = agent.tools.tools['self_map']
    assert 'include_in_overview' in SectionDescriptor.__dataclass_fields__
    assert SectionDescriptor.__dataclass_fields__['include_in_overview'].default is True
    descriptors = tool.sections.descriptors()
    assert {d.id for d in descriptors} == _TOP | _LEVEL1
    assert {d.id for d in tool.sections.overview_descriptors()} == _TOP
    for descriptor in descriptors:
        if descriptor.id in _LEVEL1:
            assert descriptor.include_in_overview is False
    assert set(tool.model_spec.input_schema['properties']['section']['enum']) == _TOP | _LEVEL1
    # LLM: 替换隐藏领域的实际reader，防总览先偷读再过滤输出；不改变descriptor的可见标志。
    # 函数用途: 确认无参总览不会读取Level1来源。
    def forbidden_reader(_context):
        pytest.fail('overview read hidden Level1 source')

    for descriptor in descriptors:
        if descriptor.id in _LEVEL1:
            monkeypatch.setitem(tool.sections._sections, descriptor.id, replace(descriptor, reader=forbidden_reader, summary_reader=forbidden_reader))
    assert {row['section'] for row in _read(agent)['sections']} == _TOP


# LLM: 节点库只写测试自己的临时文件；加入明确秘密哨兵以验证模型回执没有密文、散列或异常正文。
# 函数用途: 建立owner/host节点公开字段与禁用、ACL、同名覆盖样本。
def peer_records(agent):
    import json
    home = agent.home_paths
    common = {'endpoint_url': 'https://example.test/remote-agent/push', 'enabled': True,
              'allowed_private_hosts': ['10.0.0.1'], 'token_ciphertext': 'CIPHERTEXT_NEVER_PROJECT', 'token_hash': 'SECRET_DIGEST_NEVER_PROJECT'}
    owner = {'mine': common, 'same': common | {'endpoint_url': 'https://owner.test/remote-agent/push'},
             'disabled': common | {'enabled': False}}
    host = {'host-default': common, 'same': common | {'endpoint_url': 'https://host.test/remote-agent/push'},
            'denied': common | {'allowed_callers': ['other-owner']}, 'host-disabled': common | {'enabled': False},
            'explicit': common | {'allowed_callers': [home.owner_id]}}
    for path, rows in [(home.owner_home_dir / 'config/remote_endpoints.json', owner),
                       (home.root / 'remote_push/endpoints.json', host)]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'endpoints': rows}))
    return owner, host


# LLM: 远端列表和work均用真实节点JSON；禁止DNS、网络和密码学入口，公共查询前后文件字节/mtime不变。
# 函数用途: 验证owner优先、host ACL与禁用语义及零秘密泄漏。
@pytest.mark.parametrize('kind', ['main', 'user'])
def test_remote_peers_public_metadata_acl_and_work_summary(make_agent, tmp_path, monkeypatch, kind):
    import json
    agent = make_agent(kind=kind)
    peer_records(agent)
    monkeypatch.setattr('socket.getaddrinfo', lambda *a, **k: pytest.fail('DNS'))
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **k: pytest.fail('network'))
    monkeypatch.setattr('agent_py_agent.agent.user_space.task_credentials._cipher', lambda *a, **k: pytest.fail('cipher'))
    before = _bytes(tmp_path)
    value = _read(agent, 'remote_peers')
    rows = {row['peer_id']: row for row in value['items']}
    expected = {'mine', 'same', 'disabled', 'explicit'} | ({'host-default'} if kind == 'main' else set())
    assert set(rows) == expected
    assert rows['same']['scope'] == 'owner' and 'owner.test' in rows['same']['endpoint_url']
    assert rows['explicit']['scope'] == 'host'
    assert rows['disabled']['state'] == 'disabled' and rows['disabled']['enabled'] is False
    detail = _read(agent, 'remote_peers', item='peer:mine')['item']
    assert detail['allowed_private_hosts'] == ['10.0.0.1']
    work = _read(agent, 'work')
    assert {row['peer_id'] for row in work['items'] if row.get('kind') == 'remote_peer'} == expected - {'disabled'}
    assert not any(word in json.dumps((value, work)) for word in ['CIPHERTEXT_NEVER_PROJECT', 'SECRET_DIGEST_NEVER_PROJECT', 'token_ciphertext', 'token_hash'])
    assert _bytes(tmp_path) == before


# LLM: 子代理和隔离范围必须在任何文件读取之前拒绝节点库存；即使共享home和工具名也不能借父级别名。
# 函数用途: 核对四类受限上下文不读父节点、策略与调度仓库。
@pytest.mark.parametrize('scope,child', [('default', True), ('isolated', False), ('task_local', False), ('control_plane', False)])
def test_restricted_context_never_reads_owner_level1_sources(make_agent, monkeypatch, scope, child):
    from agent_py_agent.agent.runtime_context import (
        restore_current_subagent_context,
        set_current_subagent_context,
    )
    agent = make_agent()
    peer_records(agent)
    agent._current_run_params = RunParams(context_scope=scope)
    prior = set_current_subagent_context(agent, run_id='restricted-child') if child else None
    monkeypatch.setattr(agent.scheduler_repository, 'list_jobs', lambda: pytest.fail('parent schedules'))
    monkeypatch.setattr('agent_py_agent.agent.gateway_parts.remote_push_service.read_push_json', lambda *a, **k: pytest.fail('parent endpoints'))
    monkeypatch.setattr('agent_py_agent.agent.conversation.compact_threshold_policy.resolve_thread_compact_policy', lambda *a, **k: pytest.fail('parent compact policy'))
    try:
        assert _read(agent, 'remote_peers')['items'] == []
        assert _read(agent, 'settings')['items'] == []
        assert _read(agent, 'schedules')['items'] == []
        assert not any(row.get('kind') == 'remote_peer' for row in _read(agent, 'work')['items'])
    finally:
        if child:
            restore_current_subagent_context(agent, prior)


# LLM: 通过真实AgentConfig的已解析来源及ThreadStore元数据形成四层策略，不伪造resolve返回值，不写目录查询结果。
# 函数用途: 核对管理员默认、用户、会话、强制比例与闲置参数的准确来源。
@pytest.mark.parametrize('user,thread,forced,expected,source', [
    (None, None, False, 80, 'admin_default'), (67, None, False, 67, 'user'),
    (67, 73, False, 73, 'thread'), (67, 73, True, 80, 'admin_forced'),
])
def test_settings_uses_real_multilevel_resolver(make_agent, tmp_path, user, thread, forced, expected, source):
    agent = make_agent(memory_compact_idle_trigger_percent=65, memory_compact_idle_seconds=600)
    if user is not None:
        agent.config.memory_compact_trigger_percent = user
        agent.config.config_sources = {'memory_compact_trigger_percent': {'compact_admin_default_percent': 80}}
    agent.config.force_compact_trigger_percent = forced
    row = owner_thread(agent, 'settings')
    if thread is not None:
        row = agent.conversation_store.threads.update_atomic(row.thread_id, lambda current: replace(current, metadata={'settings': {'compact_trigger_percent': thread}}))
    agent._current_run_params = RunParams(task_attributes={'conversation_thread_id': row.thread_id})
    before = _bytes(tmp_path)
    value = _read(agent, 'settings', item='compact_policy')['item']
    assert value['trigger_percent'] == expected and value['source_layer'] == source
    assert value['admin_force_enabled'] is forced
    assert value['idle_compact_enabled'] is True and value['idle_trigger_percent'] == 65 and value['idle_seconds'] == 600
    assert _bytes(tmp_path) == before


# LLM: 关闭阈值或时长不能误显示闲置开启；只看当前scope公开策略，不触发compact或初始化缺失线程。
# 函数用途: 覆盖闲置关闭与未知线程不得回退owner默认的边界。
@pytest.mark.parametrize('threshold,seconds', [(0, 600), (60, 0)])
def test_settings_idle_disabled_and_missing_thread_is_unknown(make_agent, threshold, seconds):
    agent = make_agent(memory_compact_idle_trigger_percent=threshold, memory_compact_idle_seconds=seconds)
    assert _read(agent, 'settings', item='compact_policy')['item']['idle_compact_enabled'] is False
    agent._current_run_params = RunParams(task_attributes={'conversation_thread_id': 'missing-exact-thread'})
    value = _read(agent, 'settings')
    assert value['completeness'] == 'partial' and value['state'] == 'unavailable'


# LLM: 调度公开投影复用原仓库和source_thread_id/delivery唯一落点；不输出prompt或收件人身份，也不建立第二套策略。
# 函数用途: 验证新分块与work共享准确的同通道顺延、双线和多通道只读摘要。
@pytest.mark.parametrize('mode,follow', [('origin', True), ('local', False), ('dual_deliver', True), ('multi', False)])
def test_schedules_project_original_delivery_and_binding(make_agent, tmp_path, mode, follow):
    from agent_py_agent.agent.conversation.tool_discovery import persist_discovered_tools
    from agent_py_agent.agent.scheduler.repository import SchedulerJobCreateRequest
    agent = make_agent()
    thread = owner_thread(agent, 'schedule')
    delivery = {'mode': mode, 'follow_latest_channel_session': follow}
    if mode == 'multi':
        delivery['targets'] = [{'target_kind': 'local'}]
    job, _ = agent.scheduler_repository.create_job(SchedulerJobCreateRequest(name='private-title', prompt='PRIVATE-SCHEDULE-BODY',
        thread_id=thread.thread_id, source_thread_id=thread.thread_id, schedule={'kind': 'every', 'every_seconds': 300, 'anchor_at': 100.0}, delivery=delivery))
    agent._current_run_params = RunParams(task_attributes={'conversation_thread_id': thread.thread_id})
    persist_discovered_tools(agent, agent._current_run_params, {'tool_result_envelope': {'tool_search': {'loaded_tool_names': ['schedule']}}})
    before = _bytes(tmp_path)
    for section in ['schedules', 'work']:
        row = _read(agent, section, item='schedule:' + job['job_id'])['item']
        assert row['bound_thread_id'] == thread.thread_id
        assert row['delivery_policy'] == {'mode': mode, 'follow_active_thread': follow, 'channels': ['local'] if mode == 'multi' else []}
        assert row['next_read'] == {'tool': 'schedule', 'arguments': {'action': 'get', 'job_id': job['job_id']}}
        assert 'PRIVATE' not in str(row) and 'private-title' not in str(row)
    assert _bytes(tmp_path) == before


# LLM: 群actor认证必须先于仓库读取；新寻址不能绕过原work入口的组授权门。
# 函数用途: 验证未绑定群actor不能枚举监控。
def test_schedules_group_actor_gate_is_reused(make_agent, monkeypatch):
    agent = make_agent(kind='group')
    monkeypatch.setattr(agent.scheduler_repository, 'list_jobs', lambda: pytest.fail('unbound group inventory'))
    value = _read(agent, 'schedules', allowed=['self_map', 'schedule'])
    assert value['completeness'] == 'partial' and value['state'] == 'unavailable'


# LLM: 普通Skill数量只来自同owner冻结快照；包汇总沿原summary，不加载方法正文、构建历史或执行脚本。
# 函数用途: 核对资产计数、公开入口及收起/禁用状态。
def test_assets_summary_and_original_tool_navigation(make_agent, monkeypatch):
    agent = make_agent()
    entries = tuple(SkillSnapshotEntry('owner:' + name, name, 'metadata', '/not-read/' + name, 'owner', 'testing', 'a' * 64) for name in ['first', 'second'])
    agent._current_skill_snapshot = SkillSnapshot(entries, (), 'fixture', agent.home_paths.owner_id, str(agent.tools.workspace_root))
    monkeypatch.setattr(agent.skills_service, 'snapshot_for', lambda *a, **k: pytest.fail('reload/body'))
    agent.tools.disabled_tool_names.add('scripts')
    value = _read(agent, 'assets')
    rows = {row['id']: row for row in value['items']}
    assert rows['skills']['count'] == 2 and rows['skills']['completeness'] == 'complete'
    assert rows['packages']['counts']['authorized'] == 0
    assert rows['scripts']['state'] == 'disabled'
    for name in ['scripts', 'workflows', 'datasources']:
        assert rows[name]['next_read']['tool'] in {name, 'tool_search'}
    assert rows['skills']['next_read'] == {'tool': 'self_map', 'arguments': {'section': 'skills'}}
    assert rows['packages']['next_read'] == {'tool': 'self_map', 'arguments': {'section': 'packages'}}


# LLM: 无Skill快照或跨owner绑定保持unknown/partial，不把未观测伪报0；工具集合来自本次授权而非父级。
# 函数用途: 守住资产摘要的未知计数和受限工具面。
@pytest.mark.parametrize('foreign', [False, True])
def test_assets_unknown_snapshot_never_becomes_zero(make_agent, foreign):
    agent = make_agent()
    if foreign:
        agent._current_skill_snapshot = SkillSnapshot((), (), 'foreign', 'other-owner', str(agent.tools.workspace_root))
    value = _read(agent, 'assets', allowed=['self_map', 'tool_search'])
    if foreign:
        assert value['completeness'] == 'partial' and value['state'] == 'unavailable'
    else:
        skills = next(row for row in value['items'] if row['id'] == 'skills')
        assert skills['count'] is None and skills['completeness'] == 'partial'


# LLM: 目录只能投影元信息；模型列表接口必须无写入、无DNS/HTTP、无Skill正文和原注册工具执行。
# 函数用途: 验证四个Level1真实入口查询前后文件和目录集合均不变。
def test_all_level1_queries_are_read_only(make_agent, tmp_path, monkeypatch):
    agent = make_agent()
    monkeypatch.setattr('socket.getaddrinfo', lambda *a, **k: pytest.fail('DNS'))
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **k: pytest.fail('network'))
    monkeypatch.setattr(agent.skills_service, 'snapshot_for', lambda *a, **k: pytest.fail('Skill discovery'))
    before, dirs = _bytes(tmp_path), {str(p) for p in tmp_path.rglob('*') if p.is_dir()}
    for section in sorted(_LEVEL1):
        _read(agent, section)
    assert _bytes(tmp_path) == before
    assert {str(p) for p in tmp_path.rglob('*') if p.is_dir()} == dirs


# LLM: 损坏或链接来源不能被伪装成完整空节点库；仅公开安全错误码，不能回显秘密或错误正文。
# 函数用途: 保护远端元信息坏文件的partial语义。
@pytest.mark.parametrize('damage', ['invalid_json', 'bad_shape', 'symlink'])
def test_remote_peer_source_failure_is_not_empty_success(make_agent, tmp_path, damage):
    agent = make_agent()
    path = agent.home_paths.owner_home_dir / 'config/remote_endpoints.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    if damage == 'symlink':
        other = tmp_path / 'not-a-peer-registry'
        other.write_text('SECRET_BAD_SOURCE')
        path.symlink_to(other)
    else:
        path.write_text('SECRET_BAD_SOURCE' if damage == 'invalid_json' else '{"endpoints": []}')
    result = _read(agent, 'remote_peers')
    assert result['completeness'] == 'partial' and result['total'] is None
    assert 'SECRET_BAD_SOURCE' not in str(result)


# LLM: Level0仍只在开关及实际工具面均可用时注入；预算用项目离线估算器标为估算，不冒充供应商精确token。
# 函数用途: 钉住四入口、数量/清单/权限语义、原337估算token与413字符冗余版的减量。
def test_level0_index_is_compact_and_not_duplicated(make_agent):
    from agent_py_agent.agent.memory_archive.tokens import estimate_tokens
    from agent_py_agent.agent.prompting_parts.builder import _home_directory_guide
    assert all(name in SELF_MAP_GUIDANCE for name in _LEVEL1)
    assert all(name in SELF_MAP_GUIDANCE for name in ['counts', 'items', 'unknown', 'partial', 'path', 'next_read', 'tool_search', 'skill_search'])
    assert len(SELF_MAP_GUIDANCE) < 413
    assert 150 <= estimate_tokens(SELF_MAP_GUIDANCE) <= 250
    agent = make_agent()
    builder = PromptBuilder(agent.config, agent.tools.workspace_root, home_paths=agent.home_paths)
    assert '同一件事沿用原目录' in _home_directory_guide(builder)
    for available, enabled in [(True, True), (False, True), (True, False)]:
        agent.config.enable_self_map = enabled
        prepared = builder.prepare_render_input(PromptBuildRequest('query', [], tools=ToolSections(self_map_available=available)))
        assert prepared.system_prompt.count(SELF_MAP_GUIDANCE) == int(available and enabled)
    assert len(Path('agent_py_agent/prompts/default.md').read_text()) < 1287


# LLM: 多线目标采用原认证目录生成的binding_id，不自行拼接或授予地址权限；读取时只展示channel，不泄露chat/account。
# 函数用途: 证明只有binding_id的真实双线策略也能正确显示通道摘要。
def test_schedule_binding_only_targets_resolve_public_channels(make_agent, tmp_path):
    from agent_py_agent.agent.scheduler.delivery_policy import available_delivery_targets
    from agent_py_agent.agent.scheduler.repository import SchedulerJobCreateRequest
    agent = make_agent()
    first = owner_thread(agent, 'first')
    owner_thread(agent, 'second', 'web')
    targets = available_delivery_targets(agent)
    assert {t['channel'] for t in targets} == {'local', 'web'}
    delivery = {'mode': 'dual_deliver', 'targets': [{'target_kind': 'channel_binding', 'binding_id': t['binding_id']} for t in targets]}
    job, _ = agent.scheduler_repository.create_job(SchedulerJobCreateRequest('双线', 'PRIVATE', first.thread_id,
        {'kind': 'every', 'every_seconds': 300, 'anchor_at': 100.0}, source_thread_id=first.thread_id, delivery=delivery))
    before = _bytes(tmp_path)
    item = _read(agent, 'schedules', item='schedule:' + job['job_id'])['item']
    assert item['delivery_policy']['channels'] == ['local', 'web']
    assert 'binding_id' not in str(item) and 'account_id' not in str(item) and 'chat_id' not in str(item)
    assert _bytes(tmp_path) == before


# LLM: 直接走默认不带limit入口及原详情模板，不能用显式100条完整页替代真实Level1短页的预算和导航观察点。
# 函数用途: 验证四类默认页和它们的精确详情回读，查询前后不写任何业务文件。
@pytest.mark.parametrize('section', ['remote_peers', 'settings', 'schedules', 'assets'])
def test_level1_default_page_and_original_detail_navigation(make_agent, tmp_path, section):
    import json

    from agent_py_agent.agent.scheduler.repository import SchedulerJobCreateRequest
    from agent_py_agent.agent.tooling.models import ToolInvocationContext
    agent = make_agent()
    peer_records(agent)
    row = owner_thread(agent, 'default-page')
    agent._current_run_params = RunParams(task_attributes={'conversation_thread_id': row.thread_id})
    job, _ = agent.scheduler_repository.create_job(SchedulerJobCreateRequest('default', 'PRIVATE', row.thread_id,
        {'kind': 'every', 'every_seconds': 300, 'anchor_at': 100.0}, source_thread_id=row.thread_id))
    entry = SkillSnapshotEntry('owner:default', 'default', 'metadata', '/never-read/body', 'owner', 'testing', 'b' * 64)
    agent._current_skill_snapshot = SkillSnapshot((entry,), (), 'default-page', agent.home_paths.owner_id, str(agent.tools.workspace_root))
    invocation = ToolInvocationContext(agent.tools.runtime_snapshot())
    tool = agent.tools.tools['self_map']
    before = _bytes(tmp_path)
    payload = tool.execute_scoped({'section': section}, invocation)
    assert payload.ok, payload.output
    value = json.loads(payload.output)
    assert value['section'] == section and value['returned'] > 0
    if section == 'assets':
        assert value['counts']['ordinary_skills'] == 1
    if 'item_columns' in value:
        index = value['item_columns'].index('next_read')
        template = value['detail_next_read']
        assert template['tool'] == 'self_map'
        details = []
        for short in value['items']:
            params = template['arguments'] | short[index]
            outcome = tool.execute_scoped(params, invocation)
            assert outcome.ok, outcome.output
            details.append(json.loads(outcome.output)['item'])
    else:
        details = value['items']
    wanted = {'remote_peers': 'peer:mine', 'settings': 'compact_policy', 'schedules': 'schedule:' + job['job_id'], 'assets': 'skills'}[section]
    chosen = next(item for item in details if item['id'] == wanted)
    assert {'remote_peers': 'endpoint_url', 'settings': 'trigger_percent', 'schedules': 'delivery_policy', 'assets': 'count'}[section] in chosen
    assert _bytes(tmp_path) == before


# LLM: 普通用户只读自己的生效策略，不借settings读管理员参数表或暴露全局治理编辑入口。
# 函数用途: 验证用户/群的管理员字段裁剪及原user_config权限导航。
@pytest.mark.parametrize('kind', ['user', 'group'])
def test_settings_nonadmin_fields_are_cropped(make_agent, monkeypatch, kind):
    agent = make_agent(kind=kind, memory_compact_trigger_percent=69)
    thread = owner_thread(agent, 'own-settings')
    agent._current_run_params = RunParams(task_attributes={'conversation_thread_id': thread.thread_id})
    monkeypatch.setattr('agent_py_agent.agent.settings.self_map_settings.listed_parameters', lambda: pytest.fail('admin parameter inventory'))
    value = _read(agent, 'settings')
    policy = next(row for row in value['items'] if row['id'] == 'compact_policy')
    assert policy['trigger_percent'] == 69 and 'admin_default_percent' not in policy
    assert policy['configuration_tool_state'] == 'deferred'
    assert policy['configuration_scope'] == ('user_and_thread' if kind == 'user' else 'thread')
    for row in value['items']:
        if row['id'] == 'user_config':
            assert row['scope'] == 'decision-only'
            assert row['next_read'] == {'tool': 'tool_search', 'arguments': {'query': 'user_config', 'limit': 1}}
            assert row.get('read_parameters') == {'action': 'decision_read'}


# LLM: 沿settings返回的next_read在main/user/group三类身份上真实调用tool_search并继续调用compact_policy，验证权限边界。
# 函数用途: 证明压缩策略导航在三类身份下均能命中真实工具并按原权限裁剪执行。
@pytest.mark.parametrize('kind', ['main', 'user', 'group'])
def test_settings_next_read_navigation_and_compact_policy_execution(make_agent, kind):
    import json

    from agent_py_agent.agent.conversation.host_actor import HostChannelActor
    agent = make_agent(kind=kind, memory_compact_trigger_percent=72)
    thread = owner_thread(agent, 'nav-settings', channel='local' if kind == 'main' else 'feishu')
    attrs = {'conversation_thread_id': thread.thread_id}
    actor = None
    if kind == 'group':
        actor = HostChannelActor(
            owner_id=agent.home_paths.owner_id,
            source_thread_id=thread.thread_id,
            entry_request_id='req-1',
            channel='feishu',
            conversation_id='nav-settings',
            actor_id='actor-1',
        )
    agent._current_run_params = RunParams(task_attributes=attrs, host_channel_actor=actor)
    item = _read(agent, 'settings', item='compact_policy')['item']
    target = item['configuration_tool']
    assert target == 'compact_policy' and target in agent.tools.tools
    entry = item['next_read']
    search_res = agent.tools.tools[entry['tool']].execute(entry['arguments'])
    assert search_res.ok, search_res.output
    search_payload = json.loads(search_res.output)
    assert search_payload['matched_count'] > 0 and 'compact_policy' in search_payload['loaded_for_next_model_call']
    assert item['after_expansion'] == {'tool': 'compact_policy', 'arguments': {'action': 'status'}}
    after = item['after_expansion']
    status_res = agent.tools.tools[after['tool']].execute(after['arguments'])
    assert status_res.ok, status_res.output
    status_payload = json.loads(status_res.output)
    assert status_payload['trigger_percent'] == 72
    assert ('admin_default_percent' in status_payload) is (kind == 'main')
    set_res = agent.tools.tools['compact_policy'].execute({'action': 'set_thread', 'trigger_percent': 64})
    assert set_res.ok, set_res.output
    assert json.loads(set_res.output)['trigger_percent'] == 64
    admin_res = agent.tools.tools['compact_policy'].execute({'action': 'admin_force', 'admin_force_enabled': True, 'trigger_percent': 85})
    if kind == 'main':
        assert admin_res.ok, admin_res.output
        locked = agent.tools.tools['compact_policy'].execute({'action': 'set_thread', 'trigger_percent': 60})
        assert not locked.ok and locked.error_code == 'COMPACT_POLICY_LOCKED_BY_ADMIN'
    else:
        assert not admin_res.ok and admin_res.error_code == 'TOOL_PERMISSION_DENIED'


# LLM: 验证read_public_push_endpoints在数据源出口即剥离token_ciphertext与token_hash，目录层拿到的字典不含凭据键。
# 函数用途: 守住远端节点公开元数据读取入口不向目录层传递任何密文或摘要字段。
def test_read_public_push_endpoints_strips_credentials_at_source(make_agent):
    from agent_py_agent.agent.gateway_parts.remote_push_service import read_public_push_endpoints
    agent = make_agent()
    peer_records(agent)
    rows = read_public_push_endpoints(agent.home_paths.owner_home_dir / 'config', 'remote_endpoints.json')
    assert 'mine' in rows
    for row in rows.values():
        assert 'token_ciphertext' not in row and 'token_hash' not in row
        assert set(row) <= {'endpoint_url', 'enabled', 'allowed_private_hosts', 'allowed_callers'}


# LLM: 验证compact_policy在批量CAS版本冲突时不静默覆盖、多用户中途冲突保留partial=True与effect_outcome='failed'、
#   受管用户自助写走ManagedUserGovernanceService.set_self_user_compact_policy公开入口，且权威提交后镜像写失败如实报告failed与GOVERNANCE_MIRROR_FAILED。
# 函数用途: 守住compact_policy写入口的CAS并发约束、公开服务自助边界与写后部分失败回执合同。
def test_compact_policy_batch_cas_and_mirror_failure_outcome(make_agent, monkeypatch):
    import inspect
    import json

    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    from agent_py_agent.agent.tooling import compact_policy_tool
    from agent_py_agent.agent.user_space.local_user_lifecycle import LocalUserLifecycle
    from agent_py_agent.agent.user_space.local_user_store import LocalUserStore
    from agent_py_agent.agent.user_space.managed_user_governance import (
        ManagedUserGovernanceService,
        ManagedUserScopeDeniedError,
    )
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
    from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call

    assert '_GovernanceControl' not in inspect.getsource(compact_policy_tool)

    admin = make_agent()
    users = LocalUserStore(admin.home_paths.root)
    lifecycle = LocalUserLifecycle(users)
    lifecycle.create('alice', idempotency_key='r3-review-create-alice-0001')
    lifecycle.create('bob', idempotency_key='r5-review-create-bob-0001')
    lifecycle.create('carol', idempotency_key='r5-review-create-carol-0001')
    user = SimpleAgent(
        replace(admin.config, my_agent_owner_kind='user', my_agent_owner_id='alice'),
        users.owner_home('alice').workspace_dir,
    )
    for ag in (admin, user):
        th = owner_thread(ag, f'cas-mirror-{ag.home_paths.owner_kind}')
        ag._current_run_params = RunParams(task_attributes={'conversation_thread_id': th.thread_id})
    service = ManagedUserGovernanceService(admin.home_paths.root)
    alice_self_service = ManagedUserGovernanceService(
        admin.home_paths.root,
        actor_owner=OwnerIdentity.local_user('alice'),
    )
    with pytest.raises(ManagedUserScopeDeniedError):
        service.set_self_user_compact_policy('alice', 72)
    with pytest.raises(ManagedUserScopeDeniedError):
        alice_self_service.set_self_user_compact_policy('bob', 72)

    prior = service.set_user_compact_policy('alice', {'default_trigger_percent': 71}, expected_revision=0)
    stale = execute_registry_test_call(
        admin.tools,
        'compact_policy',
        {'action': 'admin_batch_users', 'user_ids': ['alice'], 'trigger_percent': 69, 'expected_revision': 0},
        call_id='r3-batch-stale',
    )
    assert not stale.ok and stale.error_code == 'REVISION_CONFLICT'
    assert service.get_user_compact_policy('alice') == prior

    for idx, alias_revs in enumerate(
        (
            {'alice': 0, 'local/alice': 1},
            {'local/alice': 1, 'alice': 0},
            {'alice': 1, 'local/alice': 1},
        )
    ):
        dup_alias = execute_registry_test_call(
            admin.tools,
            'compact_policy',
            {
                'action': 'admin_batch_users',
                'user_ids': ['alice'],
                'trigger_percent': 69,
                'expected_revisions': alias_revs,
            },
            call_id=f'r4-batch-alias-{idx}',
        )
        assert not dup_alias.ok and dup_alias.error_code == 'TOOL_INVALID_ARGUMENTS'
        assert dup_alias.effect_outcome == 'not_started'
        assert service.get_user_compact_policy('alice') == prior

    user_stale = execute_registry_test_call(
        user.tools,
        'compact_policy',
        {'action': 'set_user', 'trigger_percent': 65, 'expected_revision': 0},
        call_id='r3-user-stale',
    )
    assert not user_stale.ok and user_stale.error_code == 'REVISION_CONFLICT'

    bob_prior = service.get_user_compact_policy('bob')
    carol_prior = service.get_user_compact_policy('carol')
    partial_batch = execute_registry_test_call(
        admin.tools,
        'compact_policy',
        {
            'action': 'admin_batch_users',
            'user_ids': ['alice', 'bob', 'carol'],
            'trigger_percent': 74,
            'expected_revisions': {'alice': prior['revision'], 'bob': 99, 'carol': 0},
        },
        call_id='r5-batch-partial-conflict',
    )
    assert not partial_batch.ok and partial_batch.error_code == 'REVISION_CONFLICT'
    assert partial_batch.effect_outcome == 'failed'
    partial_payload = json.loads(partial_batch.output)
    assert partial_payload['partial'] is True
    assert [it['status'] for it in partial_payload['items']] == ['applied', 'revision_conflict', 'not_attempted']
    assert service.get_user_compact_policy('alice')['default_trigger_percent'] == 74
    assert service.get_user_compact_policy('bob') == bob_prior
    assert service.get_user_compact_policy('carol') == carol_prior

    mirror_path = admin.home_paths.root / 'owners/local/alice/config/governance_policy.json'
    mirror_before = mirror_path.read_bytes()
    original_write = LocalUserStore.write_json

    # LLM: 仅在alice的可见镜像路径注入写失败，保留宿主权威文件写入成功以验证写后回执。
    # 函数用途: 模拟宿主authority已落盘而用户镜像写入失败的确定性故障。
    def fail_mirror(store, parts, value):
        if tuple(parts) == ('owners', 'local', 'alice', 'config', 'governance_policy.json'):
            raise HostAPIError('STORE_UNAVAILABLE', 503)
        return original_write(store, parts, value)

    monkeypatch.setattr(LocalUserStore, 'write_json', fail_mirror)
    for role, ag, expected_pct, params in (
        ('admin', admin, 67, {'action': 'admin_set_user', 'target_owner_id': 'alice', 'trigger_percent': 67}),
        ('self', user, 66, {'action': 'set_user', 'trigger_percent': 66}),
        ('batch', admin, 68, {'action': 'admin_batch_users', 'user_ids': ['alice'], 'trigger_percent': 68}),
    ):
        res = execute_registry_test_call(ag.tools, 'compact_policy', params, call_id=f'r3-mirror-{role}')
        assert not res.ok
        assert res.error_code == 'GOVERNANCE_MIRROR_FAILED'
        assert res.effect_outcome == 'failed' and res.effect_outcome != 'not_started'
        assert service.get_user_compact_policy('alice')['default_trigger_percent'] == expected_pct
        assert mirror_path.read_bytes() == mirror_before


# LLM: 在临时原Store中构造已存在的错归属记录，查询必须在调用策略解析器前失败关闭，不回落owner默认。
# 函数用途: 验证错owner或错home的精确线程不会泄漏或误报当前生效策略。
@pytest.mark.parametrize('field', ['owner_id', 'owner_home'])
def test_settings_existing_foreign_thread_does_not_fallback(make_agent, tmp_path, monkeypatch, field):
    agent = make_agent()
    thread = owner_thread(agent, 'foreign-settings')
    agent.conversation_store.threads.update_atomic(thread.thread_id, lambda current: replace(current, **{
        field: 'local/another' if field == 'owner_id' else str(tmp_path / 'foreign-home')}))
    agent._current_run_params = RunParams(task_attributes={'conversation_thread_id': thread.thread_id})
    monkeypatch.setattr('agent_py_agent.agent.conversation.compact_threshold_policy.resolve_thread_compact_policy', lambda *a, **k: pytest.fail('foreign thread fallback'))
    before = _bytes(tmp_path)
    value = _read(agent, 'settings')
    assert value['completeness'] == 'partial' and value['state'] == 'unavailable' and value['items'] == []
    assert _bytes(tmp_path) == before


# LLM: 通过真实全局治理文件复核设置读口而非伪造resolver结果；查询不修改治理、线程或压缩代次。
# 函数用途: 验证持久管理员强制策略及会话覆盖仍沿原四层解析器。
@pytest.mark.parametrize('forced,expected,source', [(True, 88, 'admin_forced'), (False, 71, 'thread')])
def test_settings_reads_persisted_global_governance(make_agent, tmp_path, forced, expected, source):
    import json
    agent = make_agent()
    thread = owner_thread(agent, 'persisted-policy')
    agent.conversation_store.threads.update_atomic(thread.thread_id, lambda row: replace(row, metadata={'settings': {'compact_trigger_percent': 71}}))
    agent._current_run_params = RunParams(task_attributes={'conversation_thread_id': thread.thread_id})
    path = agent.home_paths.root / 'data/managed_user_governance/compact_force_policy.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'schema_version': 'compact_admin_force.v1', 'revision': 1,
        'force_compact_trigger_percent': forced, 'trigger_percent': 88}))
    before = _bytes(tmp_path)
    value = _read(agent, 'settings', item='compact_policy')['item']
    assert value['trigger_percent'] == expected and value['source_layer'] == source
    assert value['admin_force_enabled'] is forced
    assert _bytes(tmp_path) == before


# LLM: 治理损坏不能静默解除管理员锁或回落默认；目录只暴露安全不可用状态，不泄露异常正文。
# 函数用途: 验证损坏的全局压缩治理保持未知而非正常默认值。
def test_settings_corrupt_governance_is_unavailable(make_agent):
    agent = make_agent()
    path = agent.home_paths.root / 'data/managed_user_governance/compact_force_policy.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"schema_version":"PRIVATE-BAD-GOVERNANCE"}')
    value = _read(agent, 'settings')
    assert value['completeness'] == 'partial' and value['state'] == 'unavailable'
    assert value['items'] == [] and 'PRIVATE' not in str(value)
