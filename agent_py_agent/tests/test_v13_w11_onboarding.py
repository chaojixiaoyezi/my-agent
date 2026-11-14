"""W11初始化引导与品牌：真实Gateway HTTP、原生Store与设置来源回归。"""
from __future__ import annotations

import http.client
import json
from pathlib import Path

import pytest

from agent_py_agent.agent.business_spaces.v13_store import V13Store
from agent_py_agent.agent.gateway_parts.business_api_v13 import v13_action_spec
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    home_paths_with_owner,
    resolve_owner_home,
)
from agent_py_agent.tests.test_v13_w0_harness import (  # noqa: F401
    v13_call,
    v13_native,
    v13_space,
)

# 1x1透明PNG头足以让原上传路由识别image/png；不引入外部图片依赖。
_PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 16


# LLM: 只用原Gateway的本地凭据和真实环回连接，不往JSON塞身份字段。
# 函数用途: 发送一笔真实HTTP请求并返回状态、头和正文。
def _request(server, method, path, body=None, *, user='business-alice', headers=None):
    server = server[0] if isinstance(server, tuple) else server
    request_headers = {'X-Gateway-Token': server._local_client_credential, 'X-User-Id': user,
                       'X-Channel': 'feishu', **(headers or {})}
    if body is not None and 'Content-Type' not in request_headers:
        request_headers['Content-Type'] = 'application/json'
    connection = http.client.HTTPConnection('127.0.0.1', server.server.server_address[1], timeout=20)
    try:
        connection.request(method, path, body, request_headers)
        response = connection.getresponse()
        content = response.read()
        return response.status, dict(response.getheaders()), content
    finally:
        connection.close()


# LLM: 断言仍走统一dispatcher的标准信封和动作result schema，不只看HTTP200。
# 函数用途: 发送一笔真实HTTP业务调用并解码信封。
def _call(server, action, params=None, *, user='business-alice', key=None, revision=None, headers=None):
    request = {'action': action, 'request_id': 'w11-' + action, 'params': params or {}}
    if key is not None:
        request['idempotency_key'] = key
    if revision is not None:
        request['expected_revision'] = revision
    status, _, raw = _request(server, 'POST', '/client/business', json.dumps(request).encode(),
                              user=user, headers=headers)
    return status, json.loads(raw)


# LLM: 完成结果必须在发送前由当前动作schema验证，错误行单独断言状态与错误码。
# 函数用途: 取出已校验的completed数据。
def _completed(server, action, params=None, **options):
    status, body = _call(server, action, params, **options)
    assert status == 200, body
    assert body['state'] == 'completed' and body['error'] is None
    return v13_action_spec(action).validate_result(body['data'])


# LLM: 失败必须同时核HTTP状态和稳定错误码，不能把任意4xx当通过。
# 函数用途: 断言指定的失败信封。
def _failed(server, action, params, code, status, **options):
    actual_status, body = _call(server, action, params, **options)
    assert actual_status == status, body
    assert body['state'] == 'failed' and body['error']['code'] == code and body['data'] == {}
    return body


# LLM: 样例写入后从真实owner目录定位Store，路径来自本次HTTP实际落盘而非推断。
# 函数用途: 返回当前测试owner的home与owner_id。
def _owner_home(agent):
    marker = next(Path(agent.home_paths.root).rglob('data/business_v13/onboarding.json'))
    state = json.loads(marker.read_text(encoding='utf-8'))
    owner_id = next(iter(state['records'].values()))['owner_id']
    return marker.parents[2], owner_id


# LLM: 统一用生产身份解析重建同一owner的home_paths，模型目录摘要才会与实时调用一致。
# 函数用途: 从真实owner目录反推可复用的home_paths。
def _owned_paths(agent, owner_home):
    relative = Path(owner_home).relative_to(Path(agent.home_paths.root)).parts
    if (len(relative) == 5 and relative[0] == 'owners' and relative[1] == 'providers'
            and relative[3] == 'users'):
        identity = OwnerIdentity.provider_user(relative[2], relative[4])
    else:
        identity = OwnerIdentity.local_main()
    return home_paths_with_owner(agent.home_paths, resolve_owner_home(agent.home_paths.root, identity))


# LLM: 生产宿主带会话库时W14产物观察端口才就绪；测试用原工厂补齐同一接线，不伪造响应。
# 函数用途: 为真实Gateway启用原会话库与投影。
def _enable_projection(server):
    from agent_py_agent.agent.gateway_parts.owner_conversation_store import owner_conversation_store
    agent = server[1]
    if getattr(agent, 'conversation_store', None) is None:
        agent.conversation_store = owner_conversation_store(agent, agent.home_paths, initialize=True)


# LLM: 记录读取使用原Store公开list，不手写第二份持久格式。
# 函数用途: 读取owner集合中的规范记录。
def _rows(owner_home, owner_id, collection):
    store = V13Store(owner_home, owner_id=owner_id, subject_id='reviewer')
    return store.list(collection)


# LLM: 品牌logo必须先经原产物路由上传，这里构造真实multipart请求体。
# 函数用途: 上传一个测试文件并返回ArtifactView。
def _upload(server, space_id, data, *, name='logo.png', key='w11-logo-upload-key'):
    boundary = 'w11boundary'
    parts = [
        ('--' + boundary + '\r\nContent-Disposition: form-data; name="space_id"\r\n\r\n'
         + space_id + '\r\n').encode(),
        ('--' + boundary + '\r\nContent-Disposition: form-data; name="idempotency_key"\r\n\r\n'
         + key + '\r\n').encode(),
        ('--' + boundary + '\r\nContent-Disposition: form-data; name="file"; filename="' + name
         + '"\r\nContent-Type: application/octet-stream\r\n\r\n').encode() + data + b'\r\n',
        ('--' + boundary + '--\r\n').encode(),
    ]
    status, _, raw = _request(server, 'POST', '/client/business/artifacts', b''.join(parts),
                              headers={'Content-Type': 'multipart/form-data; boundary=' + boundary})
    assert status == 201, raw
    return json.loads(raw)


# LLM: 空owner的真实事实全部来自来源读取；创建空间后同一入口必须反映变化。
# 函数用途: 验证示例空参数的状态清单与空间步骤。
def test_status_example_empty_params_real_facts(v13_native):  # noqa: F811
    fresh = _completed(v13_native, 'onboarding.status', {})
    assert [item['step'] for item in fresh['steps']] == [
        'llm_configured', 'connector_added', 'space_created',
        'sample_data_loaded', 'first_rule_enabled', 'branding_set']
    assert fresh['completed_steps'] == []
    assert fresh['has_connectors'] is False and fresh['has_llm'] is False
    assert fresh['sample_data_loaded'] is False
    assert fresh['coverage'] == 'complete'
    space_id = v13_space(v13_native)
    after = _completed(v13_native, 'onboarding.status', {})
    assert after['completed_steps'] == ['space_created']
    assert after['steps'][2] == {'step': 'space_created', 'done': True}
    assert space_id


# LLM: 模型服务商和连接器必须由原设置目录与数据源Store决定，而不是写死的布尔值。
# 函数用途: 验证状态读取真实模型provider与DatasourceStore，且按owner隔离。
def test_status_reads_real_model_and_connector_sources(v13_native, tmp_path):  # noqa: F811
    from agent_py_agent.agent.assets.datasource_store import DatasourceStore
    from agent_py_agent.agent.plugin_host.model_settings_v2_handlers import ModelSettingsV2Handlers

    v13_space(v13_native)
    base = v13_native[1].home_paths
    seeded_paths = home_paths_with_owner(
        base,
        resolve_owner_home(base.root, OwnerIdentity.provider_user('feishu', 'business-alice')),
    )
    V13Store(seeded_paths.owner_home_dir, owner_id=seeded_paths.owner_id, subject_id='reviewer').transaction(
        'onboarding',
        lambda records: records.update({
            seeded_paths.owner_id: {
                'owner_id': seeded_paths.owner_id,
                'sample_batch_id': 'sample-batch-seeded',
                'revision': 1,
            }
        }) or {'seeded': 1},
        idempotency_key='w11-status-seed-key',
        params_digest='w11-status-seed',
    )
    owner_home, owner_id = _owner_home(v13_native[1])
    paths = _owned_paths(v13_native[1], owner_home)
    assert paths.owner_id == owner_id and paths.owner_home_dir == owner_home
    ModelSettingsV2Handlers(paths).save_provider({
        'provider_id': 'example', 'display_name': 'Example', 'api_base': 'https://example.invalid/v1',
        'api_key': 'TEST_KEY_NOT_A_CREDENTIAL', 'models': [{
            'model_name': 'agent-model', 'model_backend': 'openai_compatible',
            'model_context_window_tokens': 32768}]})
    log = tmp_path / 'sample.log'
    log.write_text('sample line\n', encoding='utf-8')
    DatasourceStore(owner_home).save_datasource({
        'datasource_id': 'sample-logs', 'kind': 'file', 'title': 'sample-logs',
        'options': {'path': str(log), 'device_name': 'fw-01',
                    'log_source_name': 'sample.log', 'system': 'logs'}})
    data = _completed(v13_native, 'onboarding.status', {})
    assert data['has_llm'] is True and data['has_connectors'] is True
    assert {'llm_configured', 'connector_added'} <= set(data['completed_steps'])
    other = _completed(v13_native, 'onboarding.status', {}, user='business-bob')
    assert other['has_llm'] is False and other['has_connectors'] is False
    assert other['completed_steps'] == []
    assert base.owner_id


# LLM: 品牌初始为空、CAS按记录版本、logo只用冻结artifacts.get核过owner和图片类型。
# 函数用途: 验证品牌读取、更新、CAS、幂等与原生logo内容地址。
def test_branding_get_update_logo_artifact(v13_native):  # noqa: F811
    _enable_projection(v13_native)
    empty = _completed(v13_native, 'branding.get', {})
    assert empty == {'branding': {'configured': False, 'product_name': None, 'primary_color': None,
                                  'logo': None, 'logo_url': None, 'revision': 0, 'updated_at': None}}
    space_id = v13_space(v13_native)
    image = _upload(v13_native, space_id, _PNG)
    probe = _call(v13_native, 'artifacts.get',
                  {'artifact_id': image['artifact_id'], 'version_ref': image['version_ref']})
    assert probe[0] == 200 and probe[1]['data']['available'] is True, probe
    text = _upload(v13_native, space_id, b'not an image', name='note.txt',
                   key='w11-text-upload-key')
    _failed(v13_native, 'branding.update', {'logo_artifact_id': text['artifact_id'],
                                            'logo_version_ref': text['version_ref']},
            'INVALID_SCHEMA', 400, key='w11-branding-text-key', revision=0)
    # 此夹具没有完整旧run登记覆盖证明；未知ID保持可核验的503，不能冒充已证不存在。
    missing = _failed(v13_native, 'branding.update', {'logo_artifact_id': 'artifact-missing',
                                                     'logo_version_ref': 'version-missing'},
                      'HOST_NOT_READY', 503, key='w11-branding-missing-key', revision=0)
    details = {item['key']: item['value'] for item in missing['error']['details']}
    assert details['reason'] == 'ARTIFACT_REGISTRY_DISCOVERY_INCOMPLETE'
    assert details['next_action'] and missing['effect_outcome'] == 'not_started'
    assert _completed(v13_native, 'branding.get', {}) == empty
    params = {'product_name': 'AiSOC 租户', 'primary_color': '#1a2b3c',
              'logo_artifact_id': image['artifact_id'], 'logo_version_ref': image['version_ref']}
    brand = _completed(v13_native, 'branding.update', params,
                       key='w11-branding-first-key', revision=0)['branding']
    assert brand['configured'] is True and brand['revision'] == 1
    assert brand['product_name'] == 'AiSOC 租户' and brand['primary_color'] == '#1a2b3c'
    assert brand['logo'] == {key: image[key] for key in
                             ('artifact_id', 'version_ref', 'name', 'mime_type', 'size_bytes', 'content_sha256')}
    assert brand['logo_url'] == ('/client/business/artifacts/' + image['artifact_id']
                                 + '/content?version_ref=' + image['version_ref'])
    assert _completed(v13_native, 'branding.get', {})['branding'] == brand
    replay = _completed(v13_native, 'branding.update', params,
                        key='w11-branding-first-key', revision=0)['branding']
    assert replay == brand
    _failed(v13_native, 'branding.update', {'product_name': '过期写入'},
            'REVISION_CONFLICT', 409, key='w11-branding-stale-key', revision=0)
    second = _completed(v13_native, 'branding.update', {'product_name': 'AiSOC 新名称'},
                        key='w11-branding-second-key', revision=1)['branding']
    assert second['revision'] == 2 and second['product_name'] == 'AiSOC 新名称'
    assert second['logo'] == brand['logo'] and second['primary_color'] == '#1a2b3c'

    status, headers, content = _request(v13_native, 'GET', brand['logo_url'])
    assert status == 200 and headers['Content-Type'] == 'image/png' and content == _PNG
    foreign_status, _, foreign_content = _request(v13_native, 'GET', brand['logo_url'], user='business-bob')
    assert foreign_status == 503 and _PNG not in foreign_content
    foreign_error = json.loads(foreign_content)['error']
    assert foreign_error['code'] == 'HOST_NOT_READY'
    assert foreign_error['message'] == 'ARTIFACT_REGISTRY_DISCOVERY_INCOMPLETE'
    assert foreign_error['outcome'] == 'not_started'
    other = _completed(v13_native, 'branding.get', {}, user='business-bob')
    assert other['branding']['configured'] is False


# LLM: 四个动作都经同一原生入口返回闭合信封；非法信封和未知动作按冻结错误码拒绝。
# 函数用途: 验证成功信封字段、必需幂等键/版本与INVALID_SCHEMA/UNSUPPORTED_ACTION。
def test_envelopes(v13_native):  # noqa: F811
    space_id = v13_space(v13_native)
    status, body = _call(v13_native, 'onboarding.status', {})
    assert status == 200
    assert set(body) == {'request_id', 'operation_id', 'state', 'data', 'error',
                         'allowed_actions', 'warnings', 'effect_outcome'}
    assert body['effect_outcome'] == 'none'
    for action, params, options in (
            ('branding.get', {}, {}),
            ('branding.update', {'product_name': '信封品牌'}, {'key': 'w11-envelope-brand-key', 'revision': 0})):
        status, body = _call(v13_native, action, params, **options)
        assert status == 200 and body['state'] == 'completed' and body['error'] is None
        assert set(body) == {'request_id', 'operation_id', 'state', 'data', 'error',
                             'allowed_actions', 'warnings', 'effect_outcome'}
        v13_action_spec(action).validate_result(body['data'])
    _failed(v13_native, 'onboarding.status', {'unexpected': 1}, 'INVALID_SCHEMA', 400)
    _failed(v13_native, 'branding.update', {'product_name': '缺键'}, 'INVALID_SCHEMA', 400)
    _failed(v13_native, 'branding.update', {'product_name': '缺版本'},
            'INVALID_SCHEMA', 400, key='w11-envelope-no-revision')
    _failed(v13_native, 'onboarding.unknown', {}, 'UNSUPPORTED_ACTION', 400)
    assert space_id
