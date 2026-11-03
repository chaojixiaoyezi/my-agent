"""W7 #78/#104：原生 HTTP 工作流主链与冷读/预检/外发幂等回归。"""
from __future__ import annotations

import copy
import json
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.business_spaces import v13_w7_playbooks as playbooks
from agent_py_agent.agent.conversation.business_access import BusinessError
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.tests import test_v13_w0_harness as harness
from agent_py_agent.tests.test_v13_w0_harness import (
    v13_call,
    v13_seed,
    v13_space,
)

v13_host = harness.v13_host
v13_native = harness.v13_native


# LLM: 测试永不落到开发者真实 HOME；fixture 和业务 Store 都在 pytest 临时目录。
# 函数用途: 隔离环境变量并禁止原生脚本或模型作为预检副作用。
@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'os-home'))
    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path / 'agent-home'))


# LLM: 请求调用对象沿原已签发 context；假外部 provider 只用于测试其协议。
# 函数用途: 构造当前 owner 的可信直接调用对象。
def _call(host, action, params, *, key=None):
    from agent_py_agent.agent.gateway_parts import business_api as api
    from agent_py_agent.agent.gateway_parts.business_api_v13 import V13Call
    from agent_py_agent.agent.user_space.owner_resolver import (
        OwnerIdentity,
        home_paths_with_owner,
        resolve_owner_home,
    )
    if not host.home_paths.owner_id:
        host.home_paths = home_paths_with_owner(host.home_paths,
            resolve_owner_home(host.home_paths.root, OwnerIdentity.local_main()))
    context = api.bind_business_context(host, subject_id='playbooks-tester',
                                        authorize=lambda action, facts: True)
    services = api.BusinessServices(host)
    return V13Call(action, params, 'playbooks-' + uuid.uuid4().hex, services.owner_id,
                   'playbooks-tester', context, services, services.owner_home,
                   key or 'playbooks-' + uuid.uuid4().hex, None, lambda: 1793664000.0)


# LLM: 定义只包含冻结字段，条件步骤不会触发任何外部动作。
# 函数用途: 提供可由冻结 workflows.create 保存的最小 DAG。
def _definition(identity='playbook-test'):
    return {'descriptor': {'id': identity, 'kind': 'workflow', 'name': '恢复验证剧本',
                           'summary': '条件检查', 'tags': ['recovery'], 'access_targets': [],
                           'data_sources': [], 'credential_refs': [], 'io_contract': {}},
            'steps': [{'step_id': 'check', 'name': '检查条件', 'mode': 'condition',
                       'condition': True}]}


# LLM: 框架 schema 允许文本或对象条件；测试使用确定性的表达式输入。
# 函数用途: 创建一个有效的纯条件工作流定义。
def _valid_definition(identity='playbook-test'):
    value = _definition(identity)
    value['steps'][0]['condition'] = {'field': 'ready', 'operator': 'eq', 'value': True}
    return value


# LLM: 冻结 CRUD 与新 preview 都从真实 HTTP 入站，没有替换领域处理器。
# 函数用途: 验证模板等价定义的创建、取回、更新、计划预检与不同用户隔离。
def test_native_http_workflow_crud_and_dry_run(v13_native):
    space_id = v13_space(v13_native)
    definition = _valid_definition()
    status, created = v13_call(v13_native, 'workflows.create', {'definition': definition,
        'scope': {'scope_mode': 'spaces', 'space_ids': [space_id]}}, user='business-alice',
        idempotency_key='playbooks-create-workflow')
    assert status == 200, created
    assert created['error'] is None and created['state'] == 'completed'
    workflow_id = created['data']['workflow_ref']
    status, fetched = v13_call(v13_native, 'workflows.get', {'workflow_ref': workflow_id}, user='business-alice')
    assert status == 200 and fetched['data']['workflow']['workflow_ref'] == workflow_id, fetched
    changed = copy.deepcopy(definition)
    changed['descriptor']['summary'] = 'updated summary'
    status, updated = v13_call(v13_native, 'workflows.update', {'workflow_ref': workflow_id, 'definition': changed},
        user='business-alice', idempotency_key='playbooks-update-workflow',
        expected_revision=created['data']['definition_revision'])
    assert status == 200, updated
    before = list(v13_native[1].home_paths.owner_home_dir.rglob('workflow_runs/*.json'))
    status, preview = v13_call(v13_native, 'workflows.dry_run', {'workflow_id': workflow_id,
        'space_id': space_id, 'inputs': {'ready': True}}, user='business-alice')
    assert status == 200 and preview['error'] is None, preview
    assert preview['data']['external_side_effects'] == 'none'
    assert preview['data']['steps'][0]['step_id'] == 'check'
    assert preview['data']['steps'][0]['side_effects'] == 'none'
    assert list(v13_native[1].home_paths.owner_home_dir.rglob('workflow_runs/*.json')) == before
    status, denied = v13_call(v13_native, 'workflows.dry_run', {'workflow_id': workflow_id,
                             'space_id': space_id}, user='business-bob')
    assert status == 404 and denied['error']['code'] == 'RESOURCE_NOT_FOUND', denied


# LLM: 已冻结的错误名字不能在扩展目录复活，跨片路由只保留明确映射。
# 函数用途: 验证旧误名和泛 GraphQL 动作被原 HTTP 闭合入口拒绝。
def test_native_http_frozen_names_graphql_and_route_map(v13_native):
    for action in ('workflows.delete', 'workflows.scope.preview', 'workflows.scope.apply', 'graphql'):
        status, body = v13_call(v13_native, action, {}, user='business-alice')
        assert status == 400 and body['error']['code'] == 'UNSUPPORTED_ACTION', (action, body)
    assert playbooks.FROZEN_NAME_MAPPING['workflows.delete'] == ('workflows.recycle',)
    assert playbooks.ROUTE_MAP['POST /api/v1/playbooks/install-template'] == 'workflows.create'
    assert playbooks.ROUTE_MAP['POST /api/v1/business-context/preview'].startswith('business_context.rules.preview')
    assert 'W11 #74' in playbooks.ROUTE_MAP['POST /api/v1/marketplace/install']


# LLM: 缺 provider 不伪造社区数据，发现目录也必须隐藏这些动作。
# 函数用途: 验证原 HTTP 503 与按真实来源发布支持动作。
def test_native_http_community_unbound_hidden(v13_native):
    status, body = v13_call(v13_native, 'community_playbooks.list', {}, user='business-alice')
    assert status == 503 and body['error']['code'] == 'HOST_NOT_READY', body
    status, capabilities = v13_call(v13_native, 'system.capabilities.extensions', {}, user='business-alice')
    assert status == 200, capabilities
    assert not any(action.startswith('community_playbooks.') for action in capabilities['data']['supported_actions'])


# LLM: 解析、DAG 和目录引用分别失败，不能把模型任意引用当挂载事实。
# 函数用途: 验证纯草案终结器与可信上下文核对函数。
def test_workflow_draft_finalizer_and_reference_guard():
    definition = _valid_definition()
    definition['steps'].append({'step_id': 'analyze', 'name': '分析', 'mode': 'llm_skill_analysis',
                                'skill_ids': ['mounted-skill'], 'script_id': 'mounted-script'})
    result = {'definition': definition, 'referenced': {'skill_ids': ['mounted-skill'],
              'script_ids': ['mounted-script'], 'monitor_rule_ids': []}, 'warnings': []}
    parsed = playbooks.finalize_workflow_draft(json.dumps(result))
    assert parsed == result
    assert playbooks.validate_workflow_draft_references(parsed, result['referenced']) == result
    with pytest.raises(ValueError):
        playbooks.validate_workflow_draft_references(parsed, {'skill_ids': [], 'script_ids': [], 'monitor_rule_ids': []})
    wrong = copy.deepcopy(result)
    wrong['referenced']['script_ids'] = []
    with pytest.raises(ValueError):
        playbooks.finalize_workflow_draft(json.dumps(wrong))
    with pytest.raises((ValueError, HostAPIError)):
        playbooks.finalize_workflow_draft('```json\n{}\n```')
    wrong = copy.deepcopy(result)
    wrong['definition']['steps'][0]['inputs_map'] = {'x': {'step_id': 'absent'}}
    with pytest.raises(HostAPIError):
        playbooks.finalize_workflow_draft(json.dumps(wrong))


# LLM: 原脚本只是目录数据，源代码主动抛错证明发现与预检未执行它。
# 函数用途: 在原 ScriptStore 中创建一个有能力声明的固定脚本。
def _script(call, identity='isolate-endpoint', *, space_id=None, capability='isolate_host', vendor='NativeVendor'):
    from agent_py_agent.agent.assets.descriptor import AssetDescriptor
    from agent_py_agent.agent.assets.script_store import ScriptStore
    metadata = {'capabilities': capability if isinstance(capability, list) else [capability],
                'vendor_id': vendor, 'vendor_name': 'Native vendor',
                'input_schema': {'type': 'object', 'properties': {'host_id': {'type': 'string'}},
                                 'required': ['host_id'], 'additionalProperties': False},
                'risk_level': 'high', 'requires_approval': True}
    if space_id:
        metadata['space_id'] = space_id
    store = ScriptStore(call.owner_home)
    return store.save_script(AssetDescriptor.from_dict({'id': identity, 'kind': 'script', 'name': '隔离目标预检',
        'owner_id': call.owner_id, 'metadata': metadata}), code="raise RuntimeError('must not execute')\n")


# LLM: 当前 owner 目录是原生来源；厂商字段大小写比较不能改变原声明。
# 函数用途: 覆盖脚本发现、能力/厂商筛选、分页和输入预检。
def test_live_script_discovery_and_schema_precheck(v13_host):
    call = _call(v13_host, 'live_actions.discover', {})
    _script(call)
    _script(call, 'another-script', capability='block_ip', vendor='AnotherVendor')
    page = playbooks.discover_live_actions(replace(call, params={'limit': 1}))
    assert page['next_cursor'] is not None and page['coverage'] == 'partial'
    second = playbooks.discover_live_actions(replace(call, params={'limit': 1, 'cursor': page['next_cursor']}))
    assert second['items'][0]['action_ref'] != page['items'][0]['action_ref']
    with pytest.raises(BusinessError) as invalid_cursor:
        playbooks.discover_live_actions(replace(call, params={'limit': 1, 'cursor': page['next_cursor'],
                                                              'vendor_id': 'NativeVendor'}))
    assert invalid_cursor.value.code == 'CURSOR_EXPIRED'
    by_vendor = playbooks.live_actions_by_vendor(replace(call, action='live_actions.by_vendor',
                                               params={'vendor_id': 'nativevendor'}))
    assert [row['source_ref'] for row in by_vendor['items']] == ['isolate-endpoint']
    by_cap = playbooks.live_actions_by_capability(replace(call, action='live_actions.by_capability',
                                                params={'capability': 'isolate_host'}))
    assert by_cap['items'] == by_vendor['items']
    good = replace(call, action='live_actions.dry_run', params={'action_ref': 'script:isolate-endpoint',
                                                              'inputs': {'host_id': 'host-1'}})
    result = playbooks.dry_run_live_action(good)
    assert result['valid'] and result['side_effects'] == 'none' and result['would_require_approval']
    bad = playbooks.dry_run_live_action(replace(good, params={**good.params, 'inputs': {}}))
    assert not bad['valid'] and bad['errors'][0]['code'] == 'INVALID_SCHEMA'
    bad = playbooks.dry_run_live_action(replace(good, params={**good.params, 'target': {'endpoint': 'unbound'}}))
    assert not bad['valid'] and not bad['scope_check']['in_scope']


# LLM: 外部替身只记录明确请求，不填充业务持久化结果或伪造本地安装。
# 类用途: 提供社区目录协议的确定性测试端口。
class _Community:
    # LLM: 定义由测试提供，副本避免 provider 返回值意外修改原存储。
    # 函数用途: 保存公开定义和观察到的提交调用。
    def __init__(self, definition):
        self.definition = copy.deepcopy(definition)
        self.documents = []
        self.get_calls = 0
        self.fail = False

    # LLM: 读取仅返回公开元数据，原 cursor/tag/query 原样可观察。
    # 函数用途: 生成社区列表页面。
    def list(self, kind, *, query, tag, cursor, limit):
        assert kind == 'playbook'
        return {'items': [{'item_id': 'community-recovery', 'kind': kind, 'title': '恢复剧本',
                 'summary': query, 'author': None, 'tags': [tag] if tag else [], 'format': 'workflow',
                 'version': '1', 'updated_at': '2026-11-02T00:00:00+00:00'}], 'next_cursor': None}

    # LLM: 一次 provider get 不是安装；保存必须由冻结 workflows.create 完成。
    # 函数用途: 返回当前公开工作流定义。
    def get(self, kind, item_id):
        assert kind == 'playbook' and item_id == 'community-recovery'
        self.get_calls += 1
        return copy.deepcopy(self.definition)

    # LLM: 即便外发后断连也记录该次调用，重放不可再次外发。
    # 函数用途: 模拟外部确认或回包丢失。
    def submit(self, kind, document):
        assert kind == 'playbook'
        self.documents.append(copy.deepcopy(document))
        if self.fail:
            raise OSError('provider outcome unavailable')
        return {'submission_ref': 'community:submission1', 'state': 'submitted'}


# LLM: 准备对象也沿原冻结入口，不直接手写工作流正文文件。
# 函数用途: 在当前可信调用上下文创建真实空间及工作流。
def _prepare(call, definition):
    from agent_py_agent.agent.gateway_parts.business_api_v13 import v13_call_frozen
    space = v13_call_frozen(call, 'spaces.create', {'name': '社区测试空间'},
                           idempotency_key='playbooks-community-space')
    workflow = v13_call_frozen(call, 'workflows.create', {'definition': definition,
        'scope': {'scope_mode': 'spaces', 'space_ids': [space['space_id']]}},
        idempotency_key='playbooks-community-create')
    return space['space_id'], workflow['workflow_ref']


# LLM: 原集合和原 WorkflowStore 均真实落盘；provider 仅替代外部社区。
# 函数用途: 验证递归脱敏、耐久回放、键冲突和公开提交记录。
def test_community_submit_is_redacted_durable_and_not_sent_twice(v13_host):
    from agent_py_agent.agent.assets.workflow_store import WorkflowStore
    from agent_py_agent.agent.business_spaces.v13_store import v13_store_for
    definition = _valid_definition()
    definition['descriptor']['credential_refs'] = [{'credential_ref': 'private-credential', 'alias': 'api'}]
    definition['descriptor']['access_targets'] = [{'target_id': 'private-api', 'type': 'http',
                                                   'endpoint': 'https://private.invalid/api'}]
    definition['steps'][0]['config'] = {'nested': {'credential_ref': 'private-ref',
        'access_targets': [{'endpoint': 'https://nested.invalid/secret'}], 'password': 'private-password'}}
    provider = _Community(definition)
    v13_host.business_v13_providers['community_catalog'] = provider
    call = _call(v13_host, 'community_playbooks.submit', {})
    space_id, workflow_id = _prepare(call, definition)
    call = replace(call, params={'workflow_id': workflow_id, 'space_id': space_id},
                   idempotency_key='playbooks-submit-idempotent')
    original = WorkflowStore(call.owner_home).get_workflow(workflow_id).to_dict()
    first = playbooks.submit_community_playbook(call)
    assert first['submission']['state'] == 'submitted'
    assert playbooks.submit_community_playbook(call) == first
    assert len(provider.documents) == 1
    outgoing = json.dumps(provider.documents[0], ensure_ascii=False)
    for secret in ('private-credential', 'private-ref', 'private-password', 'https://private.invalid/api',
                   'https://nested.invalid/secret'):
        assert secret not in outgoing
    assert WorkflowStore(call.owner_home).get_workflow(workflow_id).to_dict() == original
    rows = v13_store_for(call).list('community_playbook_submissions')
    assert len(rows) == 1 and rows[0]['submission_id'] == first['submission']['submission_id']
    with pytest.raises(BusinessError) as conflict:
        playbooks.submit_community_playbook(replace(call, params={**call.params, 'title': 'different'}))
    assert conflict.value.code == 'IDEMPOTENCY_CONFLICT'
    assert len(provider.documents) == 1


# LLM: 失败观察与失败原因分开，外发回包未知不能当作没有发生。
# 函数用途: 验证断连后同键只返回未知，不重复 provider.submit。
def test_community_submit_unknown_never_resends(v13_host):
    provider = _Community(_valid_definition())
    provider.fail = True
    v13_host.business_v13_providers['community_catalog'] = provider
    call = _call(v13_host, 'community_playbooks.submit', {})
    space_id, workflow_id = _prepare(call, provider.definition)
    call = replace(call, params={'workflow_id': workflow_id, 'space_id': space_id})
    with pytest.raises(OSError):
        playbooks.submit_community_playbook(call)
    with pytest.raises(HostAPIError) as unknown:
        playbooks.submit_community_playbook(call)
    assert unknown.value.code == 'OUTCOME_UNKNOWN' and len(provider.documents) == 1


# LLM: 本地读取失败尚未外发，不得消耗仅一次的外发身份。
# 函数用途: 验证社区提交在工作流暂不可读后可安全同键恢复。
def test_community_submit_preflight_failure_allows_same_key_retry(v13_host, monkeypatch):
    provider = _Community(_valid_definition())
    v13_host.business_v13_providers['community_catalog'] = provider
    call = _call(v13_host, 'community_playbooks.submit', {})
    space_id, workflow_id = _prepare(call, provider.definition)
    call = replace(call, params={'workflow_id': workflow_id, 'space_id': space_id})
    original = playbooks._authorized_definition

    def unavailable(call):
        raise HostAPIError('HOST_NOT_READY', 503)

    monkeypatch.setattr(playbooks, '_authorized_definition', unavailable)
    with pytest.raises(HostAPIError) as failed:
        playbooks.submit_community_playbook(call)
    assert failed.value.code == 'HOST_NOT_READY'
    assert provider.documents == []
    monkeypatch.setattr(playbooks, '_authorized_definition', original)
    first = playbooks.submit_community_playbook(call)
    assert first['submission']['state'] == 'submitted'
    assert playbooks.submit_community_playbook(call) == first
    assert len(provider.documents) == 1


# LLM: 重放输入使用首次定义，provider 后续内容变化不能创建第二个工作流。
# 函数用途: 验证列表公开投影、真实原生安装和重放定义钉住。
def test_community_install_uses_native_create_and_pins_definition(v13_host):
    from agent_py_agent.agent.assets.workflow_store import WorkflowStore
    provider = _Community(_valid_definition('community-workflow'))
    v13_host.business_v13_providers['community_catalog'] = provider
    call = _call(v13_host, 'community_playbooks.list', {'query': '恢复', 'tag': 'recovery'})
    page = playbooks.list_community_playbooks(call)
    assert page['items'][0]['summary'] == '恢复' and page['coverage'] == 'complete'
    from agent_py_agent.agent.gateway_parts.business_api_v13 import v13_call_frozen
    space = v13_call_frozen(call, 'spaces.create', {'name': '安装范围'},
                           idempotency_key='playbooks-install-space')
    call = replace(call, action='community_playbooks.install', params={'playbook_id': 'community-recovery',
                   'space_id': space['space_id']}, idempotency_key='playbooks-install-once')
    first = playbooks.install_community_playbook(call)
    native = WorkflowStore(call.owner_home).get_workflow(first['workflow_ref'])
    assert native is not None and native.descriptor.id == 'community-workflow'
    provider.definition['descriptor']['summary'] = 'new external version'
    assert playbooks.install_community_playbook(call) == first
    assert provider.get_calls == 1
    assert WorkflowStore(call.owner_home).get_workflow(first['workflow_ref']).descriptor.summary == '条件检查'
    assert len(WorkflowStore(call.owner_home).list_workflows()) == 1
    provider.definition['steps'][0]['inputs_map'] = {'bad': {'step_id': 'missing'}}
    with pytest.raises(HostAPIError):
        playbooks.install_community_playbook(replace(call, idempotency_key='playbooks-invalid-definition'))


# LLM: 已安装包采用真实激活与成员摘要；不改包文件或启动包内代码。
# 函数用途: 构造带合法/坏定义的已启用原生内容包。
def _install_content(call):
    from agent_py_agent.agent.capability.package_provider import _owner_from_home
    from agent_py_agent.agent.plugin_activation import PluginActivationRequest
    from agent_py_agent.agent.plugin_content_activation import PluginContentActivation
    from agent_py_agent.agent.plugin_install_store import PluginInstallStore
    from agent_py_agent.agent.plugin_installation import PluginInstallRequest
    from agent_py_agent.agent.plugin_package import inspect_plugin_package
    from agent_py_agent.tests.test_capability_package import content_bundle
    files = {'CAPABILITY.md': b'# Workflow templates',
             'workflows/recovery.json': json.dumps(_valid_definition('template-recovery')).encode(),
             'workflows/invalid.json': b'{"not":"workflow"}',
             'scripts/never.py': b"raise RuntimeError('must not execute')"}
    store = PluginInstallStore(_owner_from_home(call.owner_home))
    entry = store.install(PluginInstallRequest(inspect_plugin_package(content_bundle(files=files)),
                                               'test-install', 0)).installation
    activation = PluginContentActivation('enable', entry.manifest.plugin_id, entry.package_sha256,
                                         entry.revision, entry.settings_revision)
    return store.change_activation(PluginActivationRequest('enable', entry.revision, activation)).installation


# LLM: 模板选择必须来自真实包目录，且返回定义确实能被冻结 create 使用。
# 函数用途: 验证模板包读取、坏条目覆盖提示和模板安装主链。
def test_templates_use_installed_package_and_report_bad_definition(v13_host):
    from agent_py_agent.agent.assets.workflow_store import WorkflowStore
    from agent_py_agent.agent.gateway_parts.business_api_v13 import v13_call_frozen
    call = _call(v13_host, 'workflows.templates.list', {'tag': 'recovery'})
    entry = _install_content(call)
    page = playbooks.list_templates(call)
    assert page['coverage'] == 'partial' and page['next_cursor'] is None
    assert len(page['items']) == 1
    template = page['items'][0]
    assert template['source'] == 'package' and template['package_id'] == entry.manifest.plugin_id
    assert template['definition']['descriptor']['id'] == 'template-recovery'
    created = v13_call_frozen(call, 'workflows.create', {'definition': template['definition']},
                              idempotency_key='playbooks-install-template')
    assert WorkflowStore(call.owner_home).get_workflow(created['workflow_ref']) is not None


# LLM: 目标由空间内原生 workflow 声明，额外 endpoint/url 不能跟着合法 ID 混入。
# 函数用途: 验证范围命中、未绑定空间、目标越界与附加字段逃逸。
def test_live_action_scope_checks_native_workflow_targets(v13_host):
    definition = _valid_definition()
    definition['descriptor']['access_targets'] = [{'target_id': 'edr-api', 'type': 'http',
                                                   'endpoint': 'https://declared.invalid/api'}]
    call = _call(v13_host, 'live_actions.dry_run', {})
    space_id, workflow_id = _prepare(call, definition)
    _script(call)
    params = {'space_id': space_id, 'action_ref': 'script:isolate-endpoint',
              'inputs': {'host_id': 'h-1'}, 'target': {'target_id': 'edr-api'}}
    good = playbooks.dry_run_live_action(replace(call, params=params))
    assert good['valid'] and good['scope_check'] == {'in_scope': True, 'reasons': []}
    for target in ({'target_id': 'outside'}, {'target_id': 'edr-api', 'endpoint': 'https://outside.invalid/'},
                   {'target_id': 'edr-api', 'url': 'https://outside.invalid/'}):
        bad = playbooks.dry_run_live_action(replace(call, params={**params, 'target': target}))
        assert not bad['valid'] and not bad['scope_check']['in_scope']


# LLM: 真正插件安装表/manifest/目录参与读取，但不启用、不运行插件 wheel。
# 函数用途: 验证插件声明来源与停用可用性，不通过伪造 provider 给产品补目录。
def test_live_plugin_declarations_are_cold_read_from_native_catalog(v13_host):
    from agent_py_agent.agent.capability.package_provider import _owner_from_home
    from agent_py_agent.agent.plugin_install_store import PluginInstallStore
    from agent_py_agent.agent.plugin_installation import PluginInstallRequest
    from agent_py_agent.agent.plugin_package import inspect_plugin_package
    from agent_py_agent.tests.test_plugin_package import _bundle
    call = _call(v13_host, 'live_actions.discover', {})

    # LLM: 仅修改合成包声明，包内模块保留主动抛错的导入陷阱。
    # 函数用途: 将已有测试工具声明为有副作用动作。
    def declare(manifest):
        manifest['actions'][0]['name'] = 'isolate_host'
        manifest['default_action'] = 'isolate_host'
        manifest['tools'][0]['requested_effect'] = 'mutating'

    owner = _owner_from_home(call.owner_home)
    store = PluginInstallStore(owner)
    store.install(PluginInstallRequest(inspect_plugin_package(_bundle(change=declare)), 'plugin-test', 0))
    page = playbooks.discover_live_actions(call)
    rows = [row for row in page['items'] if row['source'] == 'plugin']
    assert len(rows) == 1 and rows[0]['capability'] == 'isolate_host'
    assert rows[0]['source_ref'] == 'sample-peek:read'
    assert not rows[0]['available'] and rows[0]['unavailable_reason'] == 'plugin_unavailable'
    assert not (owner.plugins_dir / 'environments').exists()


# LLM: 使用原 MCP 配置编译和原代理；现存目录容器只提供已发布事实，不连接服务。
# 函数用途: 验证 MCP 声明投影和跨 owner 快照拒用。
def test_live_mcp_uses_already_published_native_proxy(v13_host):
    from agent_py_agent.agent.tooling.mcp_client import MCPServerConfig, MCPToolInfo
    from agent_py_agent.agent.tooling.mcp_registration import build_proxy_tool
    call = _call(v13_host, 'live_actions.discover', {})
    config = MCPServerConfig.from_mapping('nativevendor', {'command': 'must-not-launch',
        'tool_effects': {'isolate_host': 'dangerous'}})
    client = SimpleNamespace(config=config)
    tool = build_proxy_tool(client, 'nativevendor', MCPToolInfo('isolate_host', '隔离声明',
        {'type': 'object', 'properties': {'host_id': {'type': 'string'}}, 'required': ['host_id']}))
    runtime = SimpleNamespace(model_spec=tool.model_spec, exposure=SimpleNamespace(model_visible=True),
                              availability=SimpleNamespace(available=True))
    v13_host.tools = SimpleNamespace(operation_owner_id=call.owner_id, tools={tool.model_spec.name: tool},
        runtime_snapshot=lambda: SimpleNamespace(runtimes=(runtime,)))
    page = playbooks.discover_live_actions(call)
    assert len(page['items']) == 1 and page['items'][0]['source'] == 'mcp'
    assert page['items'][0]['capability'] == 'isolate_host' and page['coverage'] == 'complete'
    v13_host.tools.operation_owner_id = 'local/user/other'
    page = playbooks.discover_live_actions(call)
    assert page['items'] == [] and page['coverage'] == 'partial'


# LLM: 唯一替身是模型派发端，目录脚本和作业快照均由真实 Store 读写。
# 函数用途: 验证仅挂载目录进入 instruction，并钉住独立于 prompt 的可信作业事实。
def test_workflow_draft_context_is_pinned_before_return(v13_host, monkeypatch):
    from agent_py_agent.agent.business_spaces.v13_store import v13_store_for
    from agent_py_agent.agent.gateway_parts import business_api_v13 as foundation
    from agent_py_agent.agent.plugin_host.contracts import DomainResult
    call = _call(v13_host, 'workflows.draft_from_prompt', {})
    space_id, workflow_id = _prepare(call, _valid_definition())
    _script(call, 'mounted-script', space_id=space_id)
    _script(call, 'unmounted-script', space_id='different-space')
    v13_seed(call.owner_home, 'monitor_rules', {'monitor-one': {'rule_id': 'monitor-one',
        'owner_id': call.owner_id, 'space_id': space_id, 'script_id': 'mounted-script', 'name': '当前监控'}})
    original = foundation.v13_call_frozen
    instructions = []

    # LLM: 只有 Skill resolver 的结构事实是合成的，挂载/自动候选刻意不同。
    # 函数用途: 提供可观察的挂载过滤样本。
    def frozen(current, action, params, *, idempotency_key=None, expected_revision=None):
        if action == 'spaces.capabilities.resolve':
            return {'candidates': [{'method_ref': 'mounted-skill', 'kind': 'skill',
                     'availability': {'state': 'available'}, 'reason_codes': ['mounted_first'],
                     'origin': {'source_ref': space_id}}, {'method_ref': 'automatic-skill', 'kind': 'skill',
                     'availability': {'state': 'available'}, 'reason_codes': ['semantic_match'],
                     'origin': {'source_ref': 'owner'}}]}
        return original(current, action, params, idempotency_key=idempotency_key, expected_revision=expected_revision)

    # LLM: 此替身只记录受理作业；不填充任何模型结果，也不代替终结验证。
    # 函数用途: 按 W0 helper 合同接收目录快照并观察首个受理行。
    def start(current, kind, instruction, *, space_id=None, agent_id=None, context_facts=None):
        instructions.append(instruction)
        v13_seed(current.owner_home, 'async_jobs', {'job-draft-test': {'job_id': 'job-draft-test',
            'owner_id': current.owner_id, 'kind': kind, 'space_id': space_id, 'state': 'accepted',
            'context_facts': copy.deepcopy(context_facts),
            'revision': 0, 'updated_at': '2026-11-02T00:00:00Z'}})
        return DomainResult(202, {'state': 'accepted', 'operation_id': 'operation-draft',
                                  'job_id': 'job-draft-test', 'task_id': 'task-draft'})

    monkeypatch.setattr(foundation, 'v13_call_frozen', frozen)
    monkeypatch.setattr(foundation, 'v13_start_model_job', start)
    call = replace(call, params={'space_id': space_id, 'prompt': '创建恢复剧本'})
    accepted = playbooks.draft_from_prompt(call)
    assert accepted.http_status == 202
    row = v13_store_for(call).get('async_jobs', 'job-draft-test')
    facts = row['context_facts']
    assert facts['skill_ids'] == ['mounted-skill'] and facts['script_ids'] == ['mounted-script']
    assert facts['monitor_rule_ids'] == ['monitor-one']
    assert 'unmounted-script' not in instructions[0] and 'automatic-skill' not in instructions[0]
    assert canonical_facts(facts) in instructions[0]


# LLM: 统一 JSON 编码只用于比较实际 instruction 子串。
# 函数用途: 返回目录快照的稳定编码。
def canonical_facts(facts):
    from agent_py_agent.agent.plugin_host.schemas import canonical_json
    return canonical_json(facts)


# LLM: 此测试必须穿过实际通用终结口，不能手动调用 guard 代替接线。
# 函数用途: 证明合法 JSON 但跨目录的引用被终结为 JOB_RESULT_INVALID。
@pytest.mark.parametrize('script_id, expected_state', [('mounted-script', 'completed'), ('not-mounted', 'failed')])
def test_actual_async_job_finalization_rejects_unmounted_reference(v13_host, monkeypatch, script_id, expected_state):
    from agent_py_agent.agent.gateway_parts import business_api_v13 as foundation
    call = _call(v13_host, 'async_jobs.get', {'job_id': 'job-reference-check'})
    definition = _valid_definition()
    definition['steps'][0]['script_id'] = script_id
    model_result = {'definition': definition, 'referenced': {'skill_ids': [],
                    'script_ids': [script_id], 'monitor_rule_ids': []}, 'warnings': []}
    row = {'job_id': 'job-reference-check', 'owner_id': call.owner_id, 'kind': 'workflow_draft',
           'action': 'workflows.draft_from_prompt', 'subject_id': call.subject_id, 'space_id': 'space-test',
           'agent_id': 'agent-test', 'task_id': 'task-test', 'operation_id': 'operation-test',
           'state': 'accepted', 'result': None, 'error': None, 'revision': 1,
           'context_facts': {'skill_ids': [], 'script_ids': ['mounted-script'], 'monitor_rule_ids': []},
           'created_at': '2026-11-02T00:00:00Z', 'updated_at': '2026-11-02T00:00:00Z'}
    v13_seed(call.owner_home, 'async_jobs', {'job-reference-check': row})

    # LLM: 只提供原任务读取和消息内容，终结路由和注册 finalizer 保持产品本身。
    # 函数用途: 模拟一个完成任务的准确 task_id 输出。
    def frozen(current, action, params, *, idempotency_key=None, expected_revision=None):
        if action == 'tasks.get':
            return {'task': {'status': 'done'}}
        assert action == 'messages.list'
        return {'items': [{'task_id': 'task-test', 'role': 'assistant', 'text': json.dumps(model_result)}],
                'next_cursor': None}

    monkeypatch.setattr(foundation, 'v13_call_frozen', frozen)
    result = foundation._async_jobs_get(call)
    assert result['state'] == expected_state
    if expected_state == 'failed':
        assert result['result'] is None and result['error']['code'] == 'JOB_RESULT_INVALID'
    else:
        assert result['result'] == model_result and result['error'] is None


# LLM: 只声称本机真实运输和原后台接单，不把 Echo 当成真实模型草案生成验收。
# 函数用途: 验证草案 accepted 的准确 202 信封且没有保存工作流。
def test_native_http_draft_accepts_without_saving_workflow(v13_native, monkeypatch):
    from agent_py_agent.agent.conversation import session_task_dispatch
    from agent_py_agent.agent.gateway_parts import business_api_v13 as foundation
    frozen = foundation.v13_call_frozen
    report_error = session_task_dispatch.runtime_error_report
    observed = []

    # LLM: 只增加异常发生位置的观察，不改变原派发器返回或状态。
    # 函数用途: 诊断原生接单底层失败，避免将未知错误猜成沙箱问题。
    def report(error, *, context):
        import traceback
        print('W7_NATIVE_DISPATCH_FRAMES', traceback.extract_tb(error.__traceback__)[-8:])
        return report_error(error, context=context)

    monkeypatch.setattr(session_task_dispatch, 'runtime_error_report', report)

    # LLM: 只记录原端口的准确异常链，不改变调用、返回值或就绪判定。
    # 函数用途: 为真实接单失败保留最短领域观察点。
    def trace(current, action, params, *, idempotency_key=None, expected_revision=None):
        try:
            return frozen(current, action, params, idempotency_key=idempotency_key, expected_revision=expected_revision)
        except Exception as error:
            import traceback
            outcome = getattr(error, 'outcome', None)
            observed.append((action, traceback.format_exc(), repr(outcome)))
            if outcome is not None:
                print('W7_NATIVE_DISPATCH', action, getattr(outcome, 'output', ''))
            raise

    monkeypatch.setattr(foundation, 'v13_call_frozen', trace)
    space_id = v13_space(v13_native)
    status, before = v13_call(v13_native, 'workflows.list', {'space_id': space_id}, user='business-alice')
    assert status == 200, before
    status, accepted = v13_call(v13_native, 'workflows.draft_from_prompt',
        {'space_id': space_id, 'prompt': '生成仅检查输入条件的工作流草案'}, user='business-alice',
        idempotency_key='playbooks-draft-http-accepted')
    assert status == 202 and accepted['state'] == 'accepted' and accepted['error'] is None, (accepted, observed)
    assert accepted['data']['job_id'] and accepted['data']['task_id']
    status, after = v13_call(v13_native, 'workflows.list', {'space_id': space_id}, user='business-alice')
    assert status == 200 and after['data']['items'] == before['data']['items'], after


# LLM: 脏声明单列为覆盖不足；重复能力不应污染分页或看起来像多条真实动作。
# 函数用途: 验证坏能力过滤与重复结构声明去重。
def test_live_catalog_bad_declarations_are_partial_not_false_complete(v13_host):
    call = _call(v13_host, 'live_actions.discover', {})
    _script(call, capability=['isolate_host', 'isolate_host', 'bad capability'])
    v13_host.tools = SimpleNamespace(operation_owner_id=call.owner_id, tools={},
                                     runtime_snapshot=lambda: SimpleNamespace(runtimes=()))
    page = playbooks.discover_live_actions(call)
    assert len(page['items']) == 1 and page['coverage'] == 'partial'
    assert page['items'][0]['capability'] == 'isolate_host'


# LLM: 单元替换只观察冻结 preview 边界，不能把假计划当作原运行验收。
# 函数用途: 证明 DryRun 没有其它领域调用，并保留错误与逐步骤副作用投影。
def test_dry_run_only_calls_frozen_preview(v13_host, monkeypatch):
    from agent_py_agent.agent.gateway_parts import business_api_v13 as foundation
    call = _call(v13_host, 'workflows.dry_run', {'workflow_id': 'workflow-ref',
                                              'space_id': 'space-ref', 'inputs': {'x': 1}})
    calls = []

    # LLM: 所有其它动作均失败，强制测试不能用执行或模型代替预检。
    # 函数用途: 提供一个可观察且含被阻断步骤的冻结预览计划。
    def preview(current, action, params, *, idempotency_key=None, expected_revision=None):
        calls.append((action, params))
        assert action == 'workflows.preview'
        return {'plan': [{'step_id': str(index), 'mode': mode, 'would_execute': index != 0}
                         for index, mode in enumerate(('http_request', 'tool_call', 'notification',
                                                       'script', 'agent_task', 'condition_branch', 'llm_prompt'))],
                'required_capabilities': ['execute'], 'errors': []}

    monkeypatch.setattr(foundation, 'v13_call_frozen', preview)
    result = playbooks.dry_run(call)
    assert calls == [('workflows.preview', {'workflow_ref': 'workflow-ref', 'space_id': 'space-ref', 'inputs': {'x': 1}})]
    assert [row['side_effects'] for row in result['steps']] == ['external'] * 5 + ['none', 'unknown']
    assert not result['steps'][0]['would_run'] and result['steps'][0]['blocked_reason']
    assert result['external_side_effects'] == 'none'
