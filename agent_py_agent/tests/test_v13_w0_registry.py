"""W0 注册器、静态分片、实时授权和冻结兼容的回归。"""
import sys
from dataclasses import replace
from types import ModuleType

import pytest

from agent_py_agent.agent.conversation.business_access import BusinessError
from agent_py_agent.agent.gateway_parts import business_api as api
from agent_py_agent.agent.gateway_parts import business_api_v13 as v13
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.tests import test_v13_w0_harness as harness
from agent_py_agent.tests.test_v13_w0_harness import v13_call

# 绑定原夹具对象，保留 pytest 发现语义，避免导入名与测试参数触发重定义。
v13_host = harness.v13_host
v13_native = harness.v13_native


# LLM: 只注入测试动作表，不替代真实dispatcher或HTTP信封。
# 函数用途: 为静态注册器准备一个闭合动作。
def action(name='sample.read', *, ready=None, admin=False, aliases=(), handler=None):
    empty = {'type': 'object', 'properties': {}, 'additionalProperties': False}
    return v13.V13Action(name, 'read', empty, empty,
        handler or (lambda call: {}), ready=ready, admin=admin, aliases=aliases)


# LLM: 所有测试仍走原execute_business，未自建替代信封。
# 函数用途: 以当前可信host调用一个真实业务动作。
def invoke(host, name, params=None, authorize=None):
    context = api.bind_business_context(host, authorize=authorize or (lambda name, facts: True))
    return api.execute_business({'action': name, 'request_id': 'registry-request', 'params': params or {}},
        context, api.BusinessServices(host))


# LLM: 未知动作、错误参数和坏返回值均应在正式边界失败。
# 函数用途: 验证闭合schema前后校验和未知动作码。
def test_v13_unknown_params_and_result_validation(v13_host, monkeypatch):
    assert invoke(v13_host, 'missing.extension')[1]['error']['code'] == 'UNSUPPORTED_ACTION'
    assert invoke(v13_host, 'system.capabilities.extensions', {'extra': 1})[0] == 400
    bad = action(handler=lambda call: {'extra': 1})
    monkeypatch.setitem(v13._ACTIONS, bad.name, bad)
    status, body = invoke(v13_host, bad.name)
    assert status == 400 and body['error']['code'] == 'INVALID_SCHEMA'
    assert body['data'] == {}


# LLM: 发现按规范动作ready与实时权限过滤，别名必须独立获准。
# 函数用途: 验证扩展发现不污染冻结目录。
def test_extensions_discovery_lists_only_ready_and_authorized_v13_actions(v13_host, monkeypatch):
    v13.v13_action_specs()
    monkeypatch.setattr(v13, '_ACTIONS', {'system.capabilities.extensions': v13._ACTIONS['system.capabilities.extensions']})
    monkeypatch.setattr(v13, 'ALIASES', {})
    for item in (action(), action('not.ready', ready=lambda call: False), action('denied.read'),
            action('admin.read', admin=True)):
        monkeypatch.setitem(v13._ACTIONS, item.name, item)
    monkeypatch.setitem(v13.ALIASES, 'sample.alias', 'sample.read')
    v13_host.business_native_admin = lambda: False
    status, body = invoke(v13_host, 'system.capabilities.extensions',
        authorize=lambda name, facts: name != 'denied.read')
    assert status == 200, body
    assert body['data']['supported_actions'] == ['sample.read', 'system.capabilities.extensions']
    assert body['data']['aliases'] == [{'alias': 'sample.alias', 'canonical': 'sample.read'}]
    frozen = invoke(v13_host, 'system.capabilities')[1]['data']
    assert 'sample.read' not in frozen['supported_actions']
    assert 'system.capabilities.extensions' not in frozen['supported_actions']


# LLM: HTTP测试从真实Gateway进入，不能由本地函数结果外推路由可用。
# 函数用途: 验证扩展HTTP信封、无任务读口及功能关闭。
def test_real_native_extension_route(v13_native):
    status, body = v13_call(v13_native, 'system.capabilities.extensions', {}, user='business-alice')
    assert status == 200 and body['error'] is None
    status, body = v13_call(v13_native, 'async_jobs.get', {'job_id': 'absent'}, user='business-alice')
    assert status == 404 and body['error']['code'] == 'RESOURCE_NOT_FOUND'
    v13_native[1].config.business_spaces_enabled = False
    status, body = v13_call(v13_native, 'system.capabilities.extensions', {}, user='business-alice')
    assert status == 503 and body['error']['code'] == 'HOST_NOT_READY'


# LLM: 新码不能改旧码映射，含显式HostAPIError与原领域错误。
# 函数用途: 验证五个扩展错误状态。
@pytest.mark.parametrize('code,status', [('VERIFICATION_FAILED',403), ('STATE_CONFLICT',409),
    ('QUERY_REJECTED',400), ('LIMIT_EXCEEDED',400), ('RATE_LIMITED',429)])
def test_new_error_codes_only(code, status):
    assert api._error_code(BusinessError(code))[:2] == (code, status)
    assert api._error_code(HostAPIError(code))[:2] == (code, status)
    assert v13.v13_error_status('CURSOR_EXPIRED') is None
    assert api._error_code(BusinessError('CURSOR_EXPIRED'))[1] == 410
    assert api._error_code(HostAPIError('RESOURCE_NOT_FOUND', 404))[1] == 404


# LLM: 假分片只通过既有静态列表导入，导入内部缺依赖不能静默吞掉。
# 函数用途: 验证静态分片注册及schema、job kind、collection合并。
def test_static_slice_protocol_and_schema_closure(monkeypatch):
    module = ModuleType(v13.V13_SLICE_MODULES[0])
    module.V13_ACTIONS = (action(aliases=('sample.alias',)),)
    module.V13_DEFS = {'V13Example': {'type': 'string'}}
    module.V13_JOB_KINDS = {'example': {'result_schema': {'type':'object'}, 'finalize': dict}}
    module.V13_COLLECTIONS = {'example_rows': 'owner'}
    monkeypatch.setitem(sys.modules, module.__name__, module)
    actions, aliases, defs, jobs, collections = v13._load_registry()
    assert actions['sample.read'].handler is module.V13_ACTIONS[0].handler
    assert aliases['sample.alias'] == 'sample.read'
    assert jobs['example'] is module.V13_JOB_KINDS['example']
    assert defs['V13Example'] == {'type':'string'} and collections['example_rows'] == 'owner'
    monkeypatch.setitem(v13.V13_DEFS, 'V13Example', {'type':'object', 'properties': {'id': {'$ref':'#/$defs/Id'}}})
    schema = v13.v13_schema({'$ref':'#/$defs/V13Example'})
    assert set(schema['$defs']) == {'V13Example', 'Id'}
    with pytest.raises(ValueError):
        v13.v13_schema({'$ref':'#/$defs/V13Absent'})


# LLM: 两个分片对同一事实只能同值，动作和别名不能重名。
# 函数用途: 验证导入时拒绝所有契约冲突。
@pytest.mark.parametrize('conflict', ['action','alias','defs','job','collection'])
def test_registry_conflicts_raise(monkeypatch, conflict):
    first, second = (ModuleType(name) for name in v13.V13_SLICE_MODULES[:2])
    for index, module in enumerate((first, second)):
        module.V13_ACTIONS = (action('sample.' + str(index), aliases=('same.alias',) if conflict == 'alias' else ()),)
        module.V13_DEFS, module.V13_JOB_KINDS, module.V13_COLLECTIONS = {}, {}, {}
        monkeypatch.setitem(sys.modules, module.__name__, module)
    if conflict == 'action':
        second.V13_ACTIONS = first.V13_ACTIONS
    elif conflict == 'defs':
        first.V13_DEFS, second.V13_DEFS = {'V13X': {'type':'string'}}, {'V13X': {'type':'boolean'}}
    elif conflict == 'job':
        first.V13_JOB_KINDS, second.V13_JOB_KINDS = {'sample': {'x':True}}, {'sample': {'x':False}}
    elif conflict == 'collection':
        first.V13_COLLECTIONS, second.V13_COLLECTIONS = {'sample':'owner'}, {'sample':'main'}
    with pytest.raises(ValueError):
        v13._load_registry()


# LLM: 权限随调用末核，复制dataclass或业务角色标签不产生管理员能力。
# 函数用途: 验证context与原生admin门不能伪造。
def test_live_context_and_native_admin_only(v13_host):
    context = api.bind_business_context(v13_host, authorize=lambda name, facts: True)
    services = api.BusinessServices(v13_host)
    call = v13.V13Call('system.capabilities.extensions', {}, 'r', context.owner_id, context.subject_id,
        context, services, services.owner_home)
    assert v13.v13_require_admin(call) is True
    with pytest.raises(HostAPIError):
        v13.v13_require_access(replace(call, context=replace(context)))
    v13_host.business_native_admin = lambda: False
    v13_host.rbac_roles = ['admin']
    with pytest.raises(HostAPIError) as error:
        v13.v13_require_admin(call)
    assert error.value.code == 'BUSINESS_ACCESS_DENIED'


# LLM: 细节只用冻结key/value标量schema；复杂或非法结构不能破坏信封。
# 函数用途: 验证结构化覆盖细节与错误文案分离。
def test_structured_error_details(v13_host, monkeypatch):
    # LLM: 只从合成动作抛出带覆盖细节的领域错误，真实分派器仍负责生成错误信封。
    # 函数用途: 提供用于核对结构化错误细节的失败动作。
    def fail(call):
        raise BusinessError('HOST_NOT_READY', details={'coverage':'unknown','source':'usage'})
    monkeypatch.setitem(v13._ACTIONS, 'sample.read', action(handler=fail))
    status, body = invoke(v13_host, 'sample.read')
    assert status == 503
    assert body['error']['details'] == [{'key':'coverage','value':'unknown'}, {'key':'source','value':'usage'}]


# LLM: 包入口不能预先加载分片、数据库或注册器。
# 函数用途: 验证bootstrap只有模块文档。
def test_business_spaces_bootstrap_is_docstring_only():
    import ast

    import agent_py_agent.agent.business_spaces as package
    tree = ast.parse(__import__('pathlib').Path(package.__file__).read_text())
    assert len(tree.body) == 1 and isinstance(tree.body[0], ast.Expr)
