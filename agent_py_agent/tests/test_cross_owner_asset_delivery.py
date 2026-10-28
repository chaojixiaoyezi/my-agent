# LLM: 仅在pytest临时Owner执行跨用户资产合同；不用真实账号、凭据、通道或运行服务。
# 模块用途: 验证投递安全边界、依赖闭包、默认禁用及真实执行入口。
# Round4: 单job恢复不得覆盖其他调度写入；提交事实不得依赖整个store hash；补充结构化YAML保形拒绝/替换。
# Stage3来源: Argon/c7缺依赖、Vault扫描、通知持久化，及c5 B03短字面量漏检；增加服务/工具真实临时Owner回归。
# 独审补证: 资源文件名秘密、目录FD释放，以及子进程在入箱前后直接退出的通知恢复；不以异常finally模拟进程中断。
# Round2来源: 真实Scheduler安装/启停、通知终态/消费、根缺失404及未加引号短秘密；全部在临时Owner验证，不代表外部通道已送达。
# Round3来源: R3-2/R3-3；真实子进程os._exit覆盖双存储undo、提交后回执重试及due索引，扩充YAML列表/数字PIN扫描；不代表断电或真实worker验收。
from dataclasses import replace

import pytest

from agent_py_agent.agent.assets import topology
from agent_py_agent.agent.assets.descriptor import AssetDescriptor, AssetRefs
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.workflow_store import WorkflowDefinition, WorkflowStore

# LLM: 合成短值不依赖供应商形状或共享日志扫描；大小写和连接符必须走同一敏感键合同。
# 常量用途: 覆盖基本词、camelCase、snake_case、kebab-case及带业务前缀的完整末尾片段。
_H01_SECRET_KEYS = (
    'secret', 'token', 'password', 'passwd', 'pwd', 'passphrase', 'apikey', 'api_key',
    'access_key', 'access_token', 'refresh_token', 'client_secret', 'private_key',
    'auth_token', 'credential', 'webhook', 'cookie', 'bearer', 'authorization',
    'accessToken', 'refreshToken', 'clientSecret', 'apiKey', 'privateKey', 'authToken',
    'serviceClientSecret', 'SERVICE_ACCESS_TOKEN', 'service-access-token',
    'service.apiKey', 'APIKey', 'serviceWebhook', 'passPhrase', 'webHook',
    'servicePassPhrase', 'serviceWebHook',
)


@pytest.mark.parametrize('key', _H01_SECRET_KEYS)
@pytest.mark.parametrize('literal', ['x', '秘密\\"\n尾', '${TOKEN}literal-tail'])
def test_h01_json_key_value_association_rejects_and_replaces(sanitize, tmp_path, key, literal):
    import json

    from agent_py_agent.agent.assets.delivery_contracts import AssetDeliveryError

    value = {'nested': [{'ordinary': '保留', key: literal}], 'port': 8080, 'enabled': True}
    raw = json.dumps(value, ensure_ascii=True).encode()
    with pytest.raises(AssetDeliveryError) as failure:
        sanitize({'config.json': raw}, owner_home=tmp_path)
    assert failure.value.error_code == 'ASSET_DELIVERY_SECRET_DETECTED'
    assert literal not in str(failure.value.to_dict())
    output = sanitize({'config.json': raw}, owner_home=tmp_path,
        allow_secret_placeholder_replace=True)['config.json']
    value['nested'][0][key] = '<REDACTED_SECRET>'
    assert json.loads(output) == value
    assert sanitize({'config.json': output}, owner_home=tmp_path)['config.json'] == output


@pytest.mark.parametrize('key', ['accessToken', 'apiKey', 'clientSecret', 'authorization'])
def test_h01_json_sensitive_container_and_duplicate_keys(sanitize, tmp_path, key):
    import json

    from agent_py_agent.agent.assets.delivery_contracts import AssetDeliveryError

    raw = ('{"' + key + '":["x",{"ordinary":"y"},null,42,false],'
        '"safe":"保留","' + key + '":"${TOKEN}"}').encode()
    with pytest.raises(AssetDeliveryError) as failure:
        sanitize({'config.json': raw}, owner_home=tmp_path)
    assert failure.value.error_code == 'ASSET_DELIVERY_SECRET_DETECTED'
    output = sanitize({'config.json': raw}, owner_home=tmp_path,
        allow_secret_placeholder_replace=True)['config.json']
    pairs = json.loads(output, object_pairs_hook=list)
    assert pairs == [(key, ['<REDACTED_SECRET>', [('ordinary', '<REDACTED_SECRET>')], None, 42, False]),
        ('safe', '保留'), (key, '${TOKEN}')]


@pytest.mark.parametrize('literal', ['', '${TOKEN}', '<CONFIGURED_TOKEN>', '<REDACTED_SECRET>',
    'YOUR_TOKEN', 'CHANGE_ME', '***REDACTED***'])
def test_h01_json_pure_placeholders_and_nonsecret_keys_remain_unchanged(sanitize, tmp_path, literal):
    import json

    value = dict.fromkeys(_H01_SECRET_KEYS, literal)
    value.update(max_tokens=500, token_count='ordinary', password_hint='ordinary',
        secretary='ordinary', timeout=30)
    raw = json.dumps(value).encode()
    assert sanitize({'config.json': raw}, owner_home=tmp_path)['config.json'] == raw
    assert sanitize({'config.json': raw}, owner_home=tmp_path,
        allow_secret_placeholder_replace=True)['config.json'] == raw


@pytest.mark.parametrize('key', _H01_SECRET_KEYS)
@pytest.mark.parametrize('style', ['yaml', 'env', 'code'])
def test_h01_nonjson_key_variants_use_same_contract(sanitize, tmp_path, key, style):
    from agent_py_agent.agent.assets.delivery_contracts import AssetDeliveryError

    text = f'{key}: x\n' if style == 'yaml' else f'{key}="x"\n' if style == 'code' else f'{key}=x\n'
    name = 'main.py' if style == 'code' else 'config.' + style
    with pytest.raises(AssetDeliveryError) as failure:
        sanitize({name: text.encode()}, owner_home=tmp_path)
    assert failure.value.error_code == 'ASSET_DELIVERY_SECRET_DETECTED'
    output = sanitize({name: text.encode()}, owner_home=tmp_path,
        allow_secret_placeholder_replace=True)[name]
    assert b'REDACTED_SECRET' in output
    assert sanitize({name: output}, owner_home=tmp_path)[name] == output


@pytest.mark.parametrize('text', ['--password="x"', '.password="x"', '1-password="x"',
    'ordinary = "clientSecret=\'x\'"'])
def test_h01_key_boundaries_keep_embedded_and_cli_assignments(sanitize, tmp_path, text):
    from agent_py_agent.agent.assets.delivery_contracts import AssetDeliveryError

    with pytest.raises(AssetDeliveryError) as failure:
        sanitize({'main.py': text.encode()}, owner_home=tmp_path)
    assert failure.value.error_code == 'ASSET_DELIVERY_SECRET_DETECTED'


def test_h01_attachment_preview_send_and_install_share_structured_scan(kit):
    import json

    from agent_py_agent.agent.assets.delivery_contracts import AssetDeliveryError

    ScriptStore(kit.main).save_script(AssetDescriptor('config_demo', 'script', '配置附件'), 'print("{}")\n')
    path = kit.main / 'scripts/config_demo/config.json'
    value = {'nested': [{'clientSecret': 'fixture-client-literal', 'apiKey': 'x',
                         'passPhrase': 'phrase', 'webHook': 'hook'}], 'port': 8080}
    raw = json.dumps(value).encode()
    path.write_bytes(raw)
    for action in (lambda: kit.service.preview_bundle('local/main', 'script', 'config_demo'),
            lambda: kit.service.send_bundle('local/main', 'local/alice', 'script', 'config_demo')):
        with pytest.raises(AssetDeliveryError) as failure:
            action()
        assert failure.value.error_code == 'ASSET_DELIVERY_SECRET_DETECTED'
    assert kit.service.list_inbox('local/alice')['deliveries'] == []
    assert path.read_bytes() == raw
    from agent_py_agent.agent.assets.script_store_support import snapshot_delivery_bundle

    frozen, = snapshot_delivery_bundle(kit.main, 'script', 'config_demo',
        allow_secret_placeholder_replace=True)
    value['nested'][0].update(clientSecret='<REDACTED_SECRET>', apiKey='<REDACTED_SECRET>',
        passPhrase='<REDACTED_SECRET>', webHook='<REDACTED_SECRET>')
    assert json.loads(frozen['files']['config.json']) == value
    sent = kit.service.send_bundle('local/main', 'local/alice', 'script', 'config_demo',
        allow_secret_placeholder_replace=True)
    payload = kit.alice / 'data/asset_delivery/inbox' / sent['delivery_id'] / 'payload/script/config_demo/config.json'
    assert json.loads(payload.read_bytes()) == value
    assert kit.service.accept_delivery('local/alice', sent['delivery_id'])['status'] == 'accepted'
    assert json.loads((kit.alice / 'scripts/config_demo/config.json').read_bytes()) == value
    assert ScriptStore(kit.alice).get_script('config_demo')['enabled'] is False
    assert path.read_bytes() == raw


@pytest.mark.parametrize('serialized', [False, True])
@pytest.mark.parametrize('text', [
    'api_key: ${TOKEN}\n  fixture-private-tail\n',
    'api_key:\n- ${TOKEN}\n  fixture-private-tail\n',
    'api_key: ${TOKEN} fixture-private-tail\n',
    'api_key: $TOKEN fixture-private-tail\n',
    'api_key: os.getenv("TOKEN") fixture-private-tail\n',
    'api_key: env.TOKEN\n  fixture-private-tail\n',
    'api_key: ${TOKEN}fixture-private-tail\n',
    'api_key: ${TOKEN fixture-private-tail}\n',
])
def test_round6_variable_prefix_never_shields_literal_tail(sanitize, tmp_path, serialized, text):
    import json

    from agent_py_agent.agent.assets.delivery_contracts import AssetDeliveryError

    name = 'body.json' if serialized else 'fixture.yaml'
    value = text + 'port: 8080\n'
    raw = (json.dumps({'body': value}) if serialized else value).encode()
    with pytest.raises(AssetDeliveryError) as failure:
        sanitize({name: raw}, owner_home=tmp_path)
    assert failure.value.error_code == 'ASSET_DELIVERY_SECRET_DETECTED'
    assert 'fixture-private-tail' not in str(failure.value.to_dict())
    output = sanitize({name: raw}, owner_home=tmp_path, allow_secret_placeholder_replace=True)[name]
    cleaned = json.loads(output)['body'] if serialized else output.decode()
    expected = 'api_key:\n- "***REDACTED***"\n' if text.startswith('api_key:\n-') else 'api_key: "***REDACTED***"\n'
    assert cleaned == expected + 'port: 8080\n'
    assert sanitize({name: output}, owner_home=tmp_path)[name] == output


@pytest.mark.parametrize('literal', ['${TOKEN}', '$TOKEN', '<REDACTED>', 'YOUR_TOKEN', 'CHANGE_ME',
    'os.getenv("TOKEN")', "os.environ.get('TOKEN')", 'os.environ["TOKEN"]', 'env.TOKEN', 'env.get("TOKEN")'])
def test_round6_pure_single_line_references_remain_allowed(sanitize, tmp_path, literal):
    raw = f'api_key: {literal}\nport: 8080\n'.encode()
    assert sanitize({'fixture.yaml': raw}, owner_home=tmp_path)['fixture.yaml'] == raw


@pytest.mark.parametrize('literal', ['${TOKEN}\nfixture-private-tail', '${TOKEN}\rfixture-private-tail',
    '${TOKEN} fixture-private-tail', '${TOKEN}fixture-private-tail', '${TOKEN fixture-private-tail}',
    'os.private_tail', 'os.environ fixture-private-tail', 'os.environ\nfixture-private-tail',
    'env.TOKEN fixture-private-tail', 'os.getenv("TOKEN", "fixture-private-tail")'])
def test_round6_placeholder_requires_a_complete_reference(literal):
    from agent_py_agent.agent.assets.delivery_sanitizer import _placeholder

    assert _placeholder(literal, unquoted=True) is False


@pytest.mark.parametrize('composite', [False, True])
@pytest.mark.parametrize('action', ['accept', 'reject'])
@pytest.mark.parametrize('reason', ['', 'fixture_user_pause'])
def test_round6_manual_at_run_then_pause_is_installation_drift(kit, composite, action, reason):
    import time
    from datetime import datetime, timezone

    from agent_py_agent.agent.assets.delivery_contracts import AssetDeliveryError
    from agent_py_agent.agent.scheduler.monitor_state import _RUNTIME_FIELDS
    from agent_py_agent.agent.scheduler.repository import SchedulerRunFinish

    source = round2_seed_monitor(kit, composite)
    now = time.time()
    schedule = {'kind': 'at', 'at': datetime.fromtimestamp(now + 3600, timezone.utc).isoformat(), 'timezone': 'UTC'}
    rule = {key: value for key, value in source['rules'][0].items() if key not in _RUNTIME_FIELDS} if composite else {}
    patch = {'rules': [{**rule, 'schedule': schedule}]} if composite else {'schedule': schedule}
    source = round2_repo(kit.main, 'local/main').update_job(source['job_id'], patch=patch,
        expected_version=source['version'])
    sent = kit.service.send_bundle('local/main', 'local/alice', 'monitor', source['job_id'])
    round3_crash(kit, sent['delivery_id'], source['job_id'], point='committed', enabled=True)
    repo = round2_repo(kit.alice, 'local/alice')
    installed = repo.get_job(source['job_id'])
    run = repo.reserve_manual_run(source['job_id'], now=now)
    assert run['trigger'] == 'manual'
    claim = repo.claim_run(run['run_id'], lease_seconds=60, now=run['dispatch_after'] + 1)
    repo.mark_run_running(run['run_id'], claim['claim_id'], now=run['dispatch_after'] + 2)
    repo.finish_run(run['run_id'], claim['claim_id'], SchedulerRunFinish('done', now=run['dispatch_after'] + 3))
    advanced = repo.get_job(source['job_id'])
    assert advanced['status'] == 'active' and advanced['run_count'] == 1
    assert advanced['next_run_at'] == installed['next_run_at'] > run['dispatch_after'] + 3
    paused = repo.pause_job(source['job_id'], expected_version=advanced['version'], reason=reason)
    assert paused['status'] == 'paused' and paused['paused_reason'] == (reason or 'manual_pause')
    envelope_path = kit.alice / 'data/asset_delivery/inbox' / sent['delivery_id'] / 'envelope.json'
    paths = (repo.store_path, repo.history_path, envelope_path)
    before = {path: path.read_bytes() for path in paths}
    method = kit.service.accept_delivery if action == 'accept' else kit.service.reject_delivery
    with pytest.raises(AssetDeliveryError) as failure:
        method('local/alice', sent['delivery_id'])
    expected = 'DELIVERY_INSTALLATION_STATE_CHANGED' if action == 'accept' else 'DELIVERY_STATE_CONFLICT'
    assert failure.value.error_code == expected
    assert {path: path.read_bytes() for path in paths} == before
    envelope = kit.service._envelope(kit.alice, 'local/alice', sent['delivery_id'])
    assert envelope['status'] not in {'accepted', 'rejected'}
    assert kit.service._origin_receipt(kit.main, envelope)['status'] not in {'accepted', 'rejected'}


@pytest.mark.parametrize('serialized', [False, True])
@pytest.mark.parametrize('text,secrets', [
    ('password:\n- fixture-pin\n- fixture-tail\nport: 8080\n', ('fixture-pin', 'fixture-tail')),
    ('auth:\n  password:\n  - fixture-pin\n    fixture-tail\n  port: 8080\n', ('fixture-pin', 'fixture-tail')),
    ('- password:\n  - fixture-pin\n  - fixture-tail\n  port: 8080\n', ('fixture-pin', 'fixture-tail')),
    ('password: alpha beta\n  gamma delta\nport: 8080\n', ('alpha beta', 'gamma delta')),
    ('password: alpha,beta\nport: 8080\n', ('alpha', 'beta')),
    ('password: alpha,beta\n  gamma delta # comment\nport: 8080\n', ('alpha', 'beta', 'gamma delta')),
    ('password: null\n  fixture-tail\nport: 8080\n', ('null', 'fixture-tail')),
])
def test_round5_yaml_complete_values(sanitize, tmp_path, serialized, text, secrets):
    import json

    from agent_py_agent.agent.assets.delivery_contracts import AssetDeliveryError

    name = 'body.json' if serialized else 'fixture.yaml'
    raw = (json.dumps({'body': text}) if serialized else text).encode()
    with pytest.raises(AssetDeliveryError) as failure:
        sanitize({name: raw}, owner_home=tmp_path)
    assert failure.value.error_code == 'ASSET_DELIVERY_SECRET_DETECTED'
    output = sanitize({name: raw}, owner_home=tmp_path, allow_secret_placeholder_replace=True)[name]
    cleaned = json.loads(output)['body'] if serialized else output.decode()
    assert all(secret not in cleaned for secret in secrets), cleaned
    assert 'port: 8080' in cleaned
    assert cleaned.count('REDACTED') == (2 if text.count('- fixture') == 2 else 1)
    assert 'gamma delta' not in cleaned and ',beta' not in cleaned


@pytest.mark.parametrize('composite', [False, True])
@pytest.mark.parametrize('one_shot', [False, True])
@pytest.mark.parametrize('action', ['accept', 'reject'])
def test_round5_installed_monitor_own_tick_recovers_receipt(kit, composite, one_shot, action):
    import time
    from datetime import datetime, timezone

    from agent_py_agent.agent.assets.delivery_contracts import AssetDeliveryError
    from agent_py_agent.agent.scheduler.monitor_state import _RUNTIME_FIELDS
    from agent_py_agent.agent.scheduler.repository import SchedulerRunFinish

    source = round2_seed_monitor(kit, composite)
    if one_shot:
        at = datetime.fromtimestamp(time.time() + 3600, timezone.utc).isoformat()
        schedule = {'kind': 'at', 'at': at, 'timezone': 'UTC'}
        rule = {key: value for key, value in source['rules'][0].items() if key not in _RUNTIME_FIELDS} if composite else {}
        patch = {'rules': [{**rule, 'schedule': schedule}]} if composite else {'schedule': schedule}
        source = round2_repo(kit.main, 'local/main').update_job(source['job_id'], patch=patch,
            expected_version=source['version'])
    sent = kit.service.send_bundle('local/main', 'local/alice', 'monitor', source['job_id'])
    round3_crash(kit, sent['delivery_id'], source['job_id'], point='committed', enabled=True)
    repo = round2_repo(kit.alice, 'local/alice')
    installed = repo.get_job(source['job_id'])
    before_envelope = kit.service._envelope(kit.alice, 'local/alice', sent['delivery_id'])
    assert before_envelope['status'] != 'accepted'
    assert not kit.service._installation_matches(kit.alice, before_envelope, previous=True)
    now = installed['next_run_at'] + 1
    run, = repo.reserve_due_runs(now=now)
    claim = repo.claim_run(run['run_id'], lease_seconds=60, now=now)
    repo.mark_run_running(run['run_id'], claim['claim_id'], now=now + 1)
    repo.finish_run(run['run_id'], claim['claim_id'], SchedulerRunFinish('done', now=now + 2))
    advanced = repo.get_job(source['job_id'])
    assert advanced['run_count'] == 1 and advanced['last_run_status'] == 'done'
    assert advanced['status'] == ('paused' if one_shot else 'active')
    if composite:
        assert advanced['rules'][0]['run_count'] == 1
        assert advanced['rules'][0]['enabled'] is not one_shot
    store_before = repo.store_path.read_bytes()
    history_before = repo.history_path.read_bytes()
    if action == 'accept':
        assert kit.service.accept_delivery('local/alice', sent['delivery_id'])['status'] == 'accepted'
    else:
        with pytest.raises(AssetDeliveryError) as failure:
            kit.service.reject_delivery('local/alice', sent['delivery_id'])
        assert failure.value.error_code == 'DELIVERY_STATE_CONFLICT'
    envelope = kit.service._envelope(kit.alice, 'local/alice', sent['delivery_id'])
    assert envelope['status'] == 'accepted'
    assert kit.service._origin_receipt(kit.main, envelope)['status'] == 'accepted'
    assert kit.service.accept_delivery('local/alice', sent['delivery_id'])['status'] == 'accepted'
    assert repo.store_path.read_bytes() == store_before
    assert repo.history_path.read_bytes() == history_before


@pytest.mark.parametrize('change', ['job_definition', 'asset_file', 'manual_pause'])
def test_round5_reject_drifted_installation_fails_closed(kit, change):
    from agent_py_agent.agent.assets.delivery_contracts import AssetDeliveryError

    source = round2_seed_monitor(kit, False)
    sent = kit.service.send_bundle('local/main', 'local/alice', 'monitor', source['job_id'])
    round3_crash(kit, sent['delivery_id'], source['job_id'], point='committed', enabled=True)
    repo = round2_repo(kit.alice, 'local/alice')
    if change == 'job_definition':
        repo.update_job(source['job_id'], patch={'name': '本人修改后的监控'},
            expected_version=repo.get_job(source['job_id'])['version'])
    elif change == 'manual_pause':
        repo.pause_job(source['job_id'], expected_version=repo.get_job(source['job_id'])['version'])
    else:
        path = kit.alice / 'data/monitors' / (source['job_id'] + '.json')
        path.write_bytes(path.read_bytes() + b'\n')
    paths = (repo.store_path, kit.alice / 'data/asset_delivery/inbox' / sent['delivery_id'] / 'envelope.json')
    before = {path: path.read_bytes() for path in paths}
    with pytest.raises(AssetDeliveryError) as failure:
        kit.service.reject_delivery('local/alice', sent['delivery_id'])
    assert failure.value.error_code == 'DELIVERY_STATE_CONFLICT'
    assert {path: path.read_bytes() for path in paths} == before
    envelope = kit.service._envelope(kit.alice, 'local/alice', sent['delivery_id'])
    assert envelope['status'] not in {'accepted', 'rejected'}
    assert kit.service._origin_receipt(kit.main, envelope)['status'] not in {'accepted', 'rejected'}
    with pytest.raises(AssetDeliveryError) as failure:
        kit.service.accept_delivery('local/alice', sent['delivery_id'])
    assert failure.value.error_code == 'DELIVERY_INSTALLATION_STATE_CHANGED'


@pytest.mark.parametrize('field', ['result', 'run_count', 'updated_at', 'last_error'])
@pytest.mark.parametrize('location', ['tool_arguments', 'metadata'])
def test_round5_runtime_names_in_static_configuration_remain_identity(kit, field, location):
    from copy import deepcopy

    from agent_py_agent.agent.assets.script_store_support import _scheduler_job_matches

    expected = round2_seed_monitor(kit, True)
    node = expected.setdefault('metadata', {}) if location == 'metadata' else expected['rules'][0]['tool']['arguments']
    node[field] = 'static-input-before'
    current = deepcopy(expected)
    changed = current['metadata'] if location == 'metadata' else current['rules'][0]['tool']['arguments']
    changed[field] = 'static-input-after'
    assert not _scheduler_job_matches(current, expected)


@pytest.mark.parametrize('serialized', [False, True])
@pytest.mark.parametrize('text,expected', [
    ('password:\n# note\n- fixture-pin\n- fixture-tail\n',
        'password:\n# note\n- "***REDACTED***"\n- "***REDACTED***"\n'),
    ('password:\n- fixture-pin\n# note\n- fixture-tail\n',
        'password:\n- "***REDACTED***"\n# note\n- "***REDACTED***"\n'),
    ('password:\n-\n  fixture-pin\n-\n  fixture-tail\n',
        'password:\n-\n  "***REDACTED***"\n-\n  "***REDACTED***"\n'),
    ('password:\n- # note\n  fixture-pin\n', 'password:\n- # note\n  "***REDACTED***"\n'),
    ('password: null\n  - gamma delta\n', 'password: "***REDACTED***"\n'),
])
def test_round5_yaml_review_boundaries(sanitize, tmp_path, serialized, text, expected):
    import json

    from agent_py_agent.agent.assets.delivery_contracts import AssetDeliveryError

    name = 'body.json' if serialized else 'fixture.yaml'
    value = text + 'port: 8080\n'
    raw = (json.dumps({'body': value}) if serialized else value).encode()
    with pytest.raises(AssetDeliveryError) as failure:
        sanitize({name: raw}, owner_home=tmp_path)
    assert failure.value.error_code == 'ASSET_DELIVERY_SECRET_DETECTED'
    output = sanitize({name: raw}, owner_home=tmp_path, allow_secret_placeholder_replace=True)[name]
    cleaned = json.loads(output)['body'] if serialized else output.decode()
    assert cleaned == expected + 'port: 8080\n'
    assert sanitize({name: output}, owner_home=tmp_path)[name] == output


def test_round5_old_one_shot_run_is_not_new_completion(kit):
    from copy import deepcopy

    from agent_py_agent.agent.assets.script_store_support import _scheduler_job_matches

    expected = round2_seed_monitor(kit, True)
    expected['rules'][0].update(schedule={'kind': 'at', 'at': '2099-01-01T00:00:00+00:00', 'timezone': 'UTC'},
        last_run_at=100, run_count=1, last_run_status='done')
    current = deepcopy(expected)
    current['rules'][0]['enabled'] = False
    assert not _scheduler_job_matches(current, expected)


# LLM: descriptor的未知字段仍严格；新enabled省略兼容旧资产，布尔类型不得宽松转换。
# 函数用途: 钉住启停状态的JSON往返与旧数据默认启用。
def test_descriptor_enabled_roundtrip_and_legacy_default():
    raw = {'id': 'script_a', 'kind': 'script', 'name': '合成脚本'}
    assert getattr(AssetDescriptor.from_dict(raw), 'enabled', None) is True
    disabled = AssetDescriptor.from_dict({**raw, 'enabled': False})
    assert disabled.enabled is False and disabled.to_dict()['enabled'] is False
    with pytest.raises(ValueError):
        AssetDescriptor.from_dict({**raw, 'enabled': 'false'})


# LLM: 直接沿原Store保存，不引入投递影子位置；开关方法必须保存并返回正式描述符。
# 函数用途: 验证脚本禁用状态不会在保存、加载或切换时丢失。
def test_script_store_persists_and_toggles_enabled(tmp_path):
    assert 'enabled' in AssetDescriptor.__dataclass_fields__
    store = ScriptStore(tmp_path)
    descriptor = AssetDescriptor.from_dict({'id': 'sample', 'kind': 'script', 'name': '脚本', 'enabled': False})
    record = store.save_script(descriptor, 'print("{}")\n')
    assert record['enabled'] is False
    result = store.set_enabled('sample', True)
    assert result.enabled is True and store.get_script('sample')['enabled'] is True


# LLM: WorkflowDefinition与其descriptor共享生效状态，任一显式False不得因旧格式缺省True而复活。
# 函数用途: 验证工作流双层序列化、原存储格式及显式开启。
def test_workflow_enabled_is_preserved_in_definition_and_store(tmp_path):
    assert 'enabled' in WorkflowDefinition.__dataclass_fields__
    definition = WorkflowDefinition.from_dict({'descriptor': {'id': 'flow', 'kind': 'workflow', 'name': '流程'},
        'steps': [], 'enabled': False})
    assert definition.enabled is False and definition.descriptor.enabled is False
    store = WorkflowStore(tmp_path)
    assert store.save_workflow(definition).enabled is False
    assert store.get_workflow('flow').descriptor.enabled is False
    assert store.set_enabled('flow', True).enabled is True
    assert WorkflowDefinition(replace(definition.descriptor, enabled=False), ()).enabled is False


# LLM: 提取器只返回根可达实际资产，依赖在前；不能调用用于展示的指数级简单环枚举。
# 函数用途: 验证闭包包含传递依赖、排除无关资产并保持依赖顺序。
def test_closure_is_dependency_first_without_cycle_enumeration(monkeypatch):
    extract = getattr(topology, 'extract_asset_closure', None)
    assert callable(extract), '缺少投递闭包公共入口'
    monkeypatch.setattr(topology, '_cycles', lambda *args: pytest.fail('闭包不得枚举所有简单环'))
    script = AssetDescriptor('collect', 'script', '采集')
    skill = AssetDescriptor('analyze', 'skill', '分析', refs=AssetRefs(scripts=('collect',)))
    flow = AssetDescriptor('flow', 'workflow', '流程', refs=AssetRefs(skills=('analyze',)))
    unused = AssetDescriptor('other', 'script', '无关')
    assert [row.id for row in extract((flow, unused, script, skill), 'workflow:flow')] == ['collect', 'analyze', 'flow']


# LLM: 只有根可达的缺失依赖硬失败；错误必须提供机器码和明确引用，不吞成空闭包。
# 函数用途: 验证缺依赖与不相关缺依赖的区别。
def test_closure_missing_dependency_is_structured():
    extract = getattr(topology, 'extract_asset_closure', None)
    assert callable(extract), '缺少投递闭包公共入口'
    root = AssetDescriptor('flow', 'workflow', '流程', refs=AssetRefs(scripts=('missing',)))
    with pytest.raises(ValueError) as error:
        extract((root,), 'workflow:flow')
    assert error.value.error_code == 'ASSET_DEPENDENCY_MISSING'
    assert error.value.missing_refs == ['script:missing']
    assert [row.id for row in extract((root, AssetDescriptor('ok', 'script', '正常')), 'script:ok')] == ['ok']


# LLM: 执行器替身只记录是否触达真实子进程层；禁用脚本必须在临时执行目录/数据源绑定前拒绝。
# 函数用途: 验证enabled不是仅供展示的标签。
def test_disabled_script_never_reaches_process_execution(tmp_path, monkeypatch):
    from agent_py_agent.agent.workflows import script_runner

    ScriptStore(tmp_path).save_script(AssetDescriptor('stop', 'script', '关闭', enabled=False), 'print("{}")')
    calls = []

    # LLM: 替代有外部副作用的执行叶，不替换资产读取或禁用判定。
    # 函数用途: 记录禁用闸遗漏而不真的启动脚本。
    def execute(*args):
        calls.append(args)
        return 0, '{}', '', '', {}

    monkeypatch.setattr(script_runner, '_execute', execute)
    outcome = script_runner.ScriptRunner(tmp_path, use_sandbox=False).run_script('stop')
    assert outcome.ok is False and outcome.error_code == 'ASSET_DISABLED_BY_OWNER'
    assert calls == [] and not (tmp_path / 'data/script_exec').exists()


# LLM: run与resume均检查当前canonical开关，冻结旧快照不能在Owner停用后绕过运行闸。
# 函数用途: 验证工作流关闭后新建和续跑均拒绝。
@pytest.mark.parametrize('resume', [False, True])
def test_disabled_workflow_rejects_new_and_resumed_runs(tmp_path, resume):
    from agent_py_agent.agent.workflows.engine import WorkflowEngine

    store = WorkflowStore(tmp_path)
    definition = store.save_workflow(WorkflowDefinition(AssetDescriptor('flow', 'workflow', '流程'), ()))
    engine = WorkflowEngine(tmp_path)
    record = engine.run_workflow('flow') if resume else None
    store.set_enabled('flow', False)
    with pytest.raises(ValueError) as error:
        engine.resume_workflow_run(record.workflow_run_id) if resume else engine.run_workflow(definition)
    assert error.value.error_code == 'ASSET_DISABLED_BY_OWNER'


# LLM: 步骤引用关闭的脚本或Skill属于硬门，不走on_error=skip或分析兜底。
# 函数用途: 验证工作流引用资产的真实禁用检查发生在执行前。
@pytest.mark.parametrize('kind', ['script', 'skill'])
def test_workflow_step_cannot_execute_disabled_dependency(tmp_path, monkeypatch, kind):
    from agent_py_agent.agent.assets.workflow_store import WorkflowStepSpec
    from agent_py_agent.agent.workflows.engine import WorkflowEngine

    if kind == 'script':
        ScriptStore(tmp_path).save_script(AssetDescriptor('dep', kind, '依赖', enabled=False), 'print("{}")')
        step = WorkflowStepSpec('s', '步骤', 'deterministic_script', script_id='dep', on_error='skip')
    else:
        path = tmp_path / 'skills/dep/SKILL.md'
        path.parent.mkdir(parents=True)
        path.write_text('---\nname: dep\ndescription: fixture\nenabled: false\n---\n合成技能。')
        step = WorkflowStepSpec('s', '步骤', 'llm_skill_analysis', skill_ids=('dep',), on_error='skip')
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda *args: pytest.fail('禁用Skill不得分析'))
    monkeypatch.setattr(engine.runner, 'run_script', lambda *args, **kwargs: pytest.fail('禁用脚本不得执行'))
    result = engine.run_workflow(WorkflowDefinition(AssetDescriptor('flow', 'workflow', '流程'), (step,)))
    assert result.status == 'failed' and result.steps[0].error_code == 'ASSET_DISABLED_BY_OWNER'


# LLM: 测试沿原SkillsService、adapter和snapshot读口，不创建第二份技能索引或仅假造enabled条目。
# 函数用途: 验证frontmatter及canonical侧文件禁用同样阻断模型注入和读取。
@pytest.mark.parametrize('location', ['frontmatter', 'sidecar'])
def test_disabled_skill_is_not_active_in_adapter_or_snapshot(tmp_path, location):
    import json

    from agent_py_agent.agent.assets.skill_adapter import get_owner_skill_descriptor
    from agent_py_agent.agent.capability.skill_snapshot import SkillSnapshotError
    from agent_py_agent.tests.test_ctxopt_skill_catalog import service

    home = tmp_path / 'owner'
    path = home / 'skills/dep/SKILL.md'
    path.parent.mkdir(parents=True)
    header = '\nenabled: false' if location == 'frontmatter' else ''
    path.write_text(f'---\nname: dep\ndescription: fixture{header}\n---\n合成技能。')
    if location == 'sidecar':
        (path.parent / 'skill_meta.json').write_text(json.dumps({'enabled': False}))
    assert get_owner_skill_descriptor(home, 'dep')['enabled'] is False
    snapshot = service(home, tmp_path / 'workspace').snapshot_for()
    assert snapshot.resolve('owner:dep', enabled_only=False).enabled is False
    assert snapshot.resolve('owner:dep') is None
    with pytest.raises(SkillSnapshotError) as error:
        snapshot.read_body('owner:dep')
    assert error.value.error_code == 'ASSET_DISABLED_BY_OWNER'


# LLM: 缺实现时以明确断言报红，不能把导入错误当扫描器生效证据。
# 函数用途: 取得待实现的原始字节扫描公共入口。
@pytest.fixture
def sanitize():
    import importlib
    import importlib.util

    name = 'agent_py_agent.agent.assets.delivery_sanitizer'
    assert importlib.util.find_spec(name) is not None, '缺少原始字节投递扫描器'
    module = importlib.import_module(name)
    assert callable(getattr(module, 'sanitize_bundle_files', None))
    return module.sanitize_bundle_files


# LLM: 全部密钥均为测试合成，无真实凭据；明确匹配要求的六种形状以及JSON带引号键。
# 函数用途: 验证默认硬拒绝且错误回执绝不回显秘密。
@pytest.mark.parametrize('secret', ['sk-' + 'A' * 24, 'AKIA' + 'B' * 16, 'ghp_' + 'C' * 24,
    'xoxb-' + 'D' * 16, 'eyJ' + 'E' * 16 + '.' + 'F' * 16 + '.' + 'G' * 16,
    'password = "SyntheticPassword123"', '"api_key": "SyntheticApiKey123"'])
def test_secret_scan_rejects_raw_bytes_without_echo(sanitize, tmp_path, secret):
    from agent_py_agent.agent.assets.delivery_contracts import AssetDeliveryError

    with pytest.raises(AssetDeliveryError) as error:
        sanitize({'script/example/main.py': ('# sample\n' + secret).encode()}, owner_home=tmp_path)
    assert error.value.error_code == 'ASSET_DELIVERY_SECRET_DETECTED'
    matches = error.value.details['redacted_matches']
    assert matches and matches[0]['line_number'] == 2
    assert secret not in str(error.value.to_dict())


# LLM: 环境只设置合成变量，不读取或持久化真实环境秘密；允许占位替换必须真正去掉原字节。
# 函数用途: 验证环境秘密匹配、显式替换和主机路径便携化。
def test_secret_replacement_environment_and_paths(sanitize, tmp_path, monkeypatch):
    secret = 'synthetic-only-environment-credential-9284'
    monkeypatch.setenv('FIXTURE_DELIVERY_TOKEN', secret)
    raw = f'value = "{secret}"\npath = "{tmp_path}/workspace/data"\n'.encode()
    result = sanitize({'script/a/main.py': raw}, owner_home=tmp_path, allow_secret_placeholder_replace=True)
    assert secret.encode() not in result['script/a/main.py']
    assert b'${REDACTED_SECRET}' in result['script/a/main.py']
    assert str(tmp_path).encode() not in result['script/a/main.py']
    assert b'${OWNER_HOME}/workspace/data' in result['script/a/main.py']


# LLM: 模板占位符不是秘密；未知格式二进制不应因UTF8解码而损坏。
# 函数用途: 验证安全模板和二进制资源无损保留。
def test_secret_placeholders_and_binary_are_preserved(sanitize, tmp_path):
    text = b'password = "${MY_PASSWORD}"\nsecret = "YOUR_SECRET_VALUE"\ntoken = "<CONFIGURED_TOKEN>"\n'
    files = {'skill/a/SKILL.md': text, 'skill/a/templates/image.bin': bytes([0, 255, 128, 254])}
    assert sanitize(files, owner_home=tmp_path) == files


# LLM: 全部账号为临时根中的严格LocalUserStore记录，身份/目录绑定按原权威schema，不接真实账号或通道。
# 函数用途: 建立三位合成本地用户及一个已知飞书用户供服务测试。
@pytest.fixture
def kit(tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.agent.assets.delivery_service import AssetDeliveryService
    from agent_py_agent.agent.user_space.local_user_store import LocalUser, LocalUserStore

    root = tmp_path / 'host'
    main = root / 'owners/local/main'
    main.mkdir(parents=True)
    store = LocalUserStore(root)
    homes = {}
    for uid in ('alice', 'bob'):
        home = root / 'owners/local' / uid
        home.mkdir(parents=True)
        user = LocalUser(uid, 'enabled', 1, uid, '2026-10-25', '2026-10-25', 1)
        store.save(uid, {'schema': 'local-user.v1', 'user': user.to_dict(),
            'provision': {'phase': 'committed', 'operation_id': 'fixture',
                'home_identity': store.directory_identity(home.relative_to(root).parts)},
            'operations': {'fixture': user.to_dict()}})
        homes[uid] = home
    feishu = root / 'owners/providers/feishu/users/ou_fixture'
    feishu.mkdir(parents=True)
    return SimpleNamespace(root=root, main=main, feishu=feishu, service=AssetDeliveryService(root), **homes)


# LLM: 用真实Store和Skill正文构造完整闭包，测试不绕过领域定义去伪造可安装包。
# 函数用途: 创建一个工作流、脚本及带资源的Skill样本。
def seed_delivery_bundle(kit):
    from agent_py_agent.agent.assets.workflow_store import WorkflowStepSpec

    ScriptStore(kit.main).save_script(AssetDescriptor('collect', 'script', '采集'), 'print("{}")\n')
    path = kit.main / 'skills/guide/SKILL.md'
    path.parent.mkdir(parents=True)
    path.write_text('---\nname: guide\ndescription: fixture\n---\n合成分析方法。\n')
    (path.parent / 'references').mkdir()
    (path.parent / 'references/rules.txt').write_text('合成规则。')
    WorkflowStore(kit.main).save_workflow(WorkflowDefinition(AssetDescriptor('flow', 'workflow', '流程'),
        (WorkflowStepSpec('s', '分析', 'hybrid', script_id='collect', skill_ids=('owner:guide',)),)))


# LLM: 不创建锁的只读入口必须保持冷Owner目录和文件原样；目标排除发送方本人。
# 函数用途: 验证冷读无副作用及准确目标发现。
def test_delivery_cold_reads_and_targets(kit):
    before = {str(path.relative_to(kit.root)): path.read_bytes() for path in kit.root.rglob('*') if path.is_file()}
    targets = kit.service.list_targets('local/main')
    assert targets.get('ok') is True
    assert {row['owner_id'] for row in targets['targets']} == {
        'local/alice', 'local/bob', 'providers/feishu/users/ou_fixture'}
    assert kit.service.list_inbox('local/alice')['deliveries'] == []
    assert kit.service.get_blocklist('local/alice')['blocked_owners'] == []
    assert {str(path.relative_to(kit.root)): path.read_bytes() for path in kit.root.rglob('*') if path.is_file()} == before


# LLM: 正常投递只能入箱，接受后才写canonical资产且默认全部关闭；回执不能代替磁盘及原Store读回。
# 函数用途: 验证闭包发送、独立安装、来源元数据、禁用和显式开启全链路。
def test_delivery_bundle_installs_canonical_assets_disabled_by_default(kit):
    import json

    seed_delivery_bundle(kit)
    preview = kit.service.preview_bundle('local/main', 'workflow', 'flow')
    assert preview.get('ok') is True
    assert preview['closure_summary']['total_assets_count'] == 3
    sent = kit.service.send_bundle('local/main', 'local/alice', 'workflow', 'flow')
    assert sent.get('ok') is True and sent['status'] == 'pending'
    assert not (kit.alice / 'workflows/flow/workflow.json').exists()
    delivery = sent['delivery_id']
    assert (kit.alice / 'data/asset_delivery/inbox' / delivery / 'envelope.json').is_file()
    assert (kit.main / 'data/asset_delivery/outbox' / (delivery + '.json')).is_file()
    accepted = kit.service.accept_delivery('local/alice', delivery)
    assert accepted.get('ok') is True and accepted['status'] == 'accepted'
    assert ScriptStore(kit.alice).get_script('collect')['enabled'] is False
    assert WorkflowStore(kit.alice).get_workflow('flow').enabled is False
    assert (kit.alice / 'skills/guide/references/rules.txt').read_text() == '合成规则。'
    meta = json.loads((kit.alice / 'skills/guide/skill_meta.json').read_text())
    assert meta['enabled'] is False and meta['metadata']['delivery_provenance']['delivery_id'] == delivery
    assert kit.service.toggle_asset('local/alice', 'script', 'collect', True)['ok'] is True
    assert ScriptStore(kit.alice).get_script('collect')['enabled'] is True
    receipt = json.loads((kit.main / 'data/asset_delivery/outbox' / (delivery + '.json')).read_text())
    assert receipt['schema_version'] == 'asset_delivery_receipt.v1' and receipt['status'] == 'accepted'


# LLM: 同包所有ID映射必须统一重写，不能只改根名；原有接收方脚本保留，不隐式覆盖。
# 函数用途: 验证keep_both_rename对工作流步骤和refs的闭包重写。
def test_delivery_conflict_renames_dependencies_and_rewrites_references(kit):
    seed_delivery_bundle(kit)
    ScriptStore(kit.alice).save_script(AssetDescriptor('collect', 'script', '已有'), 'print("old")')
    sent = kit.service.send_bundle('local/main', 'local/alice', 'workflow', 'flow')
    assert sent.get('ok') is True
    result = kit.service.accept_delivery('local/alice', sent['delivery_id'])
    assert result.get('ok') is True
    flow = WorkflowStore(kit.alice).get_workflow('flow')
    renamed = flow.steps[0].script_id
    assert renamed.startswith('collect_from_') and renamed != 'collect'
    assert flow.descriptor.refs.scripts == (renamed,)
    assert ScriptStore(kit.alice).get_script('collect')['code'] == 'print("old")'
    assert ScriptStore(kit.alice).get_script(renamed)['enabled'] is False


# LLM: 屏蔽检查先于入箱和通知，拒绝不安装资产；飞书只确认结构化事件，不冒充真实消息已送达。
# 函数用途: 验证屏蔽、解除、拒绝和飞书通知事件。
def test_delivery_block_reject_and_feishu_event(kit):
    from agent_py_agent.agent.assets.delivery_contracts import AssetDeliveryError

    seed_delivery_bundle(kit)
    assert kit.service.update_blocklist('local/alice', 'add', 'local/main').get('ok') is True
    with pytest.raises(AssetDeliveryError) as error:
        kit.service.send_bundle('local/main', 'local/alice', 'workflow', 'flow')
    assert error.value.error_code == 'DELIVERY_BLOCKED_BY_RECIPIENT'
    assert kit.service.list_inbox('local/alice')['deliveries'] == []
    kit.service.update_blocklist('local/alice', 'remove', 'local/main')
    sent = kit.service.send_bundle('local/main', 'local/alice', 'workflow', 'flow')
    assert kit.service.reject_delivery('local/alice', sent['delivery_id'], '合成拒绝')['status'] == 'rejected'
    assert not (kit.alice / 'scripts/collect/script.json').exists()
    sent = kit.service.send_bundle('local/main', 'providers/feishu/users/ou_fixture', 'workflow', 'flow')
    assert sent.get('ok') is True
    event = sent['notification_event']
    assert event['channel'] == 'feishu' and event['recipient_open_id'] == 'ou_fixture'
    assert '/delivery accept ' + sent['delivery_id'] in event['text']


# LLM: 通过公共服务切换真实canonical侧文件，旧快照仍须按当前状态拒读，不只改展示字典。
# 函数用途: 验证安装后的Skill可以显式启停且不损坏正文/来源记录。
def test_delivery_skill_toggle_updates_runtime_gate(kit, tmp_path):
    from agent_py_agent.agent.assets.skill_adapter import get_owner_skill_descriptor
    from agent_py_agent.agent.capability.skill_snapshot import SkillSnapshotError
    from agent_py_agent.tests.test_ctxopt_skill_catalog import service

    seed_delivery_bundle(kit)
    sent = kit.service.send_bundle('local/main', 'local/alice', 'skill', 'guide')
    kit.service.accept_delivery('local/alice', sent['delivery_id'])
    body = (kit.alice / 'skills/guide/SKILL.md').read_bytes()
    assert kit.service.toggle_asset('local/alice', 'skill', 'guide', True)['ok'] is True
    snapshot = service(kit.alice, tmp_path / 'workspace').snapshot_for()
    assert snapshot.resolve('owner:guide') is not None
    assert get_owner_skill_descriptor(kit.alice, 'guide')['enabled'] is True
    kit.service.toggle_asset('local/alice', 'skill', 'guide', False)
    assert (kit.alice / 'skills/guide/SKILL.md').read_bytes() == body
    with pytest.raises(SkillSnapshotError) as error:
        snapshot.read_body('owner:guide')
    assert error.value.error_code == 'ASSET_DISABLED_BY_OWNER'


# LLM: 工具构造器绑定可信Owner，Schema不得开放sender、角色或自动接收；读动作沿真实领域入口。
# 函数用途: 验证工具身份封闭、普通发送入箱与接收默认关闭。
def test_delivery_tool_binds_owner_and_rejects_forged_identity(kit):
    import importlib.util
    import json

    name = 'agent_py_agent.agent.tooling.asset_delivery_tool'
    assert importlib.util.find_spec(name) is not None, '缺少asset_delivery工具'
    from agent_py_agent.agent.tooling.asset_delivery_tool import AssetDeliveryTool

    seed_delivery_bundle(kit)
    sender = AssetDeliveryTool(kit.root, 'local/main')
    receiver = AssetDeliveryTool(kit.root, 'local/alice')
    props = sender.model_spec.input_schema['properties']
    assert not {'sender_owner_id', 'owner_id', 'sender_role', 'auto_accept', 'auto_enable', 'initial_enabled'} & props.keys()
    invalid = sender.execute({'action': 'send', 'sender_role': 'admin_provision',
        'recipient_owner_id': 'local/alice', 'asset_kind': 'workflow', 'asset_id': 'flow'})
    assert invalid.ok is False and invalid.error_code == 'TOOL_INVALID_ARGUMENTS'
    outcome = sender.execute({'action': 'send', 'recipient_owner_id': 'local/alice',
        'asset_kind': 'workflow', 'asset_id': 'flow'})
    assert outcome.ok is True
    delivery = json.loads(outcome.output)['delivery_id']
    accepted = receiver.execute({'action': 'accept', 'delivery_id': delivery})
    assert accepted.ok is True
    assert WorkflowStore(kit.alice).get_workflow('flow').enabled is False


# LLM: slash解析不接触磁盘，普通文本和伪前缀不获控制权；处理器首个身份来自宿主实参。
# 函数用途: 验证任务单命令语法、明确参数错误与独立领域处理器。
@pytest.mark.parametrize('suffix', ['inbox', 'targets', 'preview workflow:flow',
    'send local/alice workflow:flow 合成备注', 'accept dlv_fixture overwrite',
    'reject dlv_fixture 合成原因', 'enable script:collect', 'disable skill:guide',
    'block local/bob', 'unblock local/bob'])
def test_delivery_slash_parser_and_handler(kit, suffix):
    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control

    command = parse_conversation_control('/delivery ' + suffix)
    assert command is not None and command.kind == 'delivery' and command.valid
    from agent_py_agent.agent.conversation.control_delivery_command import execute_delivery_control

    if suffix == 'targets':
        result = execute_delivery_control(kit.root, 'local/main', command)
        assert result.ok is True and result.kind == 'delivery'
    invalid = parse_conversation_control('/delivery accept x invalid_strategy')
    assert invalid is not None and not invalid.valid
    assert parse_conversation_control('请 /delivery inbox') is None
    assert parse_conversation_control('/deliveryevil inbox') is None


# LLM: 保留真实有限配额锁，仅在第二次准入前抛断言防止旧实现永久挂住测试；不把无限配额结果外推有限配额。
# 函数用途: 复现并阻止Skill切换对同一Owner重复取得不可重入quota锁。
def test_skill_toggle_uses_one_real_quota_admission(kit, monkeypatch):
    from contextlib import contextmanager

    from agent_py_agent.agent.assets import delivery_service
    from agent_py_agent.agent.user_space import owner_quota

    seed_delivery_bundle(kit)
    sent = kit.service.send_bundle('local/main', 'local/alice', 'skill', 'guide')
    kit.service.accept_delivery('local/alice', sent['delivery_id'])
    original = owner_quota.OwnerQuotaEnforcer.admission
    depth = 0

    # LLM: 真实锁仍由原admission取得，只把会挂死的重复调用改为可复现失败。
    # 函数用途: 检测有限配额调用链内的嵌套准入。
    @contextmanager
    def guarded(enforcer):
        nonlocal depth
        assert depth == 0, 'nested non-reentrant owner quota admission'
        depth += 1
        try:
            with original(enforcer) as admission:
                yield admission
        finally:
            depth -= 1

    # LLM: 合成有限配额替代策略读取，不替换实际磁盘计量与文件锁。
    # 函数用途: 给测试Owner启用100MiB原生配额。
    def limited(home):
        return owner_quota.OwnerQuotaEnforcer(home, max_bytes=100 * 1024 * 1024)

    monkeypatch.setattr(owner_quota.OwnerQuotaEnforcer, 'admission', guarded)
    monkeypatch.setattr(owner_quota, 'owner_quota_enforcer_from_policy', limited)
    monkeypatch.setattr(delivery_service, 'owner_quota_enforcer_from_policy', limited)
    assert kit.service.toggle_asset('local/alice', 'skill', 'guide', True)['ok'] is True


# LLM: 故障发生于发件回执真实写点之前，不替换打包/接收方暂存写入；失败不能遗留可累积的跨Owner负载。
# 函数用途: 验证发送途中异常和上次崩溃暂存都可安全清理。
def test_failed_send_cleans_stage_and_next_send_reaps_crash_orphan(kit, monkeypatch):
    seed_delivery_bundle(kit)
    original = kit.service._put_json

    # LLM: 精确模拟本操作发件回执IO失败，不注入其它Owner或静默成功。
    # 函数用途: 在接收方暂存已写完时触发回滚。
    def fail_receipt(home, parts, value, admission):
        if home == kit.main and parts[:3] == ('data', 'asset_delivery', 'outbox'):
            raise OSError('synthetic receipt fault')
        return original(home, parts, value, admission)

    monkeypatch.setattr(kit.service, '_put_json', fail_receipt)
    with pytest.raises(OSError):
        kit.service.send_bundle('local/main', 'local/alice', 'workflow', 'flow')
    staging = kit.alice / 'data/asset_delivery/staging'
    assert not list(staging.iterdir())
    assert kit.service.list_inbox('local/alice')['deliveries'] == []
    orphan = staging / ('0' * 32)
    orphan.mkdir()
    (orphan / 'payload.bin').write_bytes(b'crash fixture')
    monkeypatch.setattr(kit.service, '_put_json', original)
    assert kit.service.send_bundle('local/main', 'local/alice', 'workflow', 'flow')['ok'] is True
    assert not orphan.exists() and not list(staging.iterdir())


# LLM: 仅使用大小写不同的安全ID，不依赖当前文件系统大小写行为；跳过须引用实际保留的canonical身份。
# 函数用途: 验证skip策略不会把同包工作流指向不存在或身份不匹配的资产。
def test_skip_casefold_conflict_rewrites_to_existing_canonical_id(kit):
    from agent_py_agent.agent.assets.workflow_store import WorkflowStepSpec

    ScriptStore(kit.main).save_script(AssetDescriptor('COLLECT', 'script', '来包'), 'print("{}")')
    ScriptStore(kit.alice).save_script(AssetDescriptor('collect', 'script', '已有'), 'print("{}")')
    WorkflowStore(kit.main).save_workflow(WorkflowDefinition(AssetDescriptor('flow', 'workflow', '流程'),
        (WorkflowStepSpec('s', '步骤', 'deterministic_script', script_id='COLLECT'),)))
    sent = kit.service.send_bundle('local/main', 'local/alice', 'workflow', 'flow')
    kit.service.accept_delivery('local/alice', sent['delivery_id'], 'skip')
    assert WorkflowStore(kit.alice).get_workflow('flow').steps[0].script_id == 'collect'
    assert ScriptStore(kit.alice).get_script('collect')['name'] == '已有'


# LLM: 旧入口也是新包声明附件时必须保留；不实际执行导入代码，仅核对canonical资源字节。
# 函数用途: 验证overwrite旧入口清理不会吞掉新脚本模块。
def test_overwrite_preserves_new_resource_named_as_previous_entrypoint(kit):
    ScriptStore(kit.alice).save_script(AssetDescriptor('s', 'script', '旧脚本'), 'print("{}")', entrypoint='helper.py')
    ScriptStore(kit.main).save_script(AssetDescriptor('s', 'script', '新脚本'), 'import helper\nprint("{}")')
    helper = kit.main / 'scripts/s/helper.py'
    helper.write_bytes(b'VALUE = "fixture"\n')
    sent = kit.service.send_bundle('local/main', 'local/alice', 'script', 's')
    assert kit.service.accept_delivery('local/alice', sent['delivery_id'], 'overwrite')['ok'] is True
    assert (kit.alice / 'scripts/s/helper.py').read_bytes() == helper.read_bytes()


# LLM: 用内存附属资源构造casefold重复，避免在大小写不敏感宿主伪造两文件；正式提交叶替身禁止任何坏journal发表。
# 函数用途: 验证最终完整事务计划必须在写盘之前拒绝重复资源目标。
def test_final_delivery_plan_rejects_casefold_duplicate_resource(kit, monkeypatch):
    from agent_py_agent.agent.assets import workspace_assets
    from agent_py_agent.agent.assets.script_store_support import commit_delivery_items
    from agent_py_agent.agent.user_space.owner_quota import OwnerQuotaEnforcer

    monkeypatch.setattr(workspace_assets, '_commit_staged', lambda *args: pytest.fail('ambiguous plan reached commit'))
    draft = {'descriptor': AssetDescriptor('s', 'script', '脚本').to_dict(), 'code': 'print("{}")'}
    with OwnerQuotaEnforcer(kit.alice, max_bytes=0).admission() as admission, pytest.raises(ValueError):
        commit_delivery_items(kit.alice, [draft], {('script', 's'): {'MAIN.py': b'print("fixture")'}}, admission, lambda _: None)
    assert not (kit.alice / '.asset-batch.json').exists()


# LLM: 精确模拟跨Owner两端回执之间的IO失败，不能把pending旧副本当成推翻已拒绝的授权。
# 函数用途: 验证拒绝已写发送方后，接收方回执重试不会变成安装。
def test_failed_reject_ack_cannot_be_reversed_by_accept(kit, monkeypatch):
    from agent_py_agent.agent.assets.delivery_contracts import AssetDeliveryError

    seed_delivery_bundle(kit)
    sent = kit.service.send_bundle('local/main', 'local/alice', 'workflow', 'flow')
    original = kit.service._put_json

    # LLM: 仅拦截最终拒绝回执，来源回执和原pending包裹保持真实。
    # 函数用途: 留下两端状态尚未对齐的拒绝场景。
    def fail_ack(home, parts, value, admission):
        if home == kit.alice and value.get('status') == 'rejected':
            raise OSError('synthetic recipient ack fault')
        return original(home, parts, value, admission)

    monkeypatch.setattr(kit.service, '_put_json', fail_ack)
    with pytest.raises(OSError):
        kit.service.reject_delivery('local/alice', sent['delivery_id'])
    monkeypatch.setattr(kit.service, '_put_json', original)
    with pytest.raises(AssetDeliveryError) as error:
        kit.service.accept_delivery('local/alice', sent['delivery_id'])
    assert error.value.error_code == 'DELIVERY_STATE_CONFLICT'
    assert not (kit.alice / 'workflows/flow/workflow.json').exists()


# LLM: 安装后回执失败不意味着未安装；用户随后编辑的正式资产不得被重命名重装或覆盖。
# 函数用途: 验证不确定提交的重试识别当前canonical变化并失败关闭。
def test_accept_retry_does_not_duplicate_assets_edited_after_commit(kit, monkeypatch):
    from agent_py_agent.agent.assets.delivery_contracts import AssetDeliveryError

    seed_delivery_bundle(kit)
    sent = kit.service.send_bundle('local/main', 'local/alice', 'script', 'collect')
    original = kit.service._resolve_delivery
    monkeypatch.setattr(kit.service, '_resolve_delivery', lambda *args: (_ for _ in ()).throw(OSError('synthetic ack fault')))
    with pytest.raises(OSError):
        kit.service.accept_delivery('local/alice', sent['delivery_id'])
    ScriptStore(kit.alice).set_enabled('collect', True)
    monkeypatch.setattr(kit.service, '_resolve_delivery', original)
    with pytest.raises(AssetDeliveryError) as error:
        kit.service.accept_delivery('local/alice', sent['delivery_id'])
    assert error.value.error_code == 'DELIVERY_INSTALLATION_STATE_CHANGED'
    assert [row['id'] for row in ScriptStore(kit.alice).list_scripts()] == ['collect']


# LLM: data_sources.query_scope和steps.inputs_map属于业务值，不因与资产字段同名而成为可重写引用。
# 函数用途: 验证冲突映射只改显式依赖及工作流步引用。
def test_reference_rewrite_preserves_unrelated_business_values():
    from agent_py_agent.agent.assets.script_store_support import rewrite_delivery_refs

    value = {'kind': 'workflow', 'refs': {'scripts': ['s']},
        'data_sources': [{'query_scope': {'script_id': 's'}}],
        'steps': [{'script_id': 's', 'inputs_map': {'record': {'script_id': 's'}}}]}
    result = rewrite_delivery_refs(value, {'script:s': 's_renamed'}, {})
    assert result['refs']['scripts'] == ['s_renamed'] and result['steps'][0]['script_id'] == 's_renamed'
    assert result['data_sources'] == value['data_sources']
    assert result['steps'][0]['inputs_map'] == value['steps'][0]['inputs_map']


# LLM: 原失败关闭用例随明确授权改为真实Scheduler正向验收，不允许只保存绑定就返回accepted。
# 函数用途: 验证独立绑定模板可安装为真实暂停任务，依赖仍默认关闭。
def test_monitor_template_installs_real_paused_scheduler(kit):
    from agent_py_agent.agent.assets.monitor_adapter import save_monitor_binding

    ScriptStore(kit.main).save_script(AssetDescriptor('collect', 'script', '依赖'), 'print("{}")')
    save_monitor_binding(kit.main, 'job_fixture', name='监控模板', script_ids=['collect'],
        schedule_spec={'kind': 'every', 'every_seconds': 60, 'enabled': False})
    sent = kit.service.send_bundle('local/main', 'local/alice', 'monitor', 'job_fixture')
    assert kit.service.accept_delivery('local/alice', sent['delivery_id'])['status'] == 'accepted'
    job = round2_repo(kit.alice, 'local/alice').get_job('job_fixture')
    assert job['status'] == 'paused' and job['enabled'] is False
    assert job['paused_reason'] == 'asset_delivery_disabled_by_default'
    assert ScriptStore(kit.alice).get_script('collect')['enabled'] is False


# LLM: 采用实际Core的Owner身份字段和canonical目录，不绕过原仓库解析。
# 函数用途: 为Round2验收打开真实调度器。
def round2_repo(home, owner_id):
    from agent_py_agent.agent.scheduler.repository import SchedulerRepository

    return SchedulerRepository(home / 'data/scheduler', owner_provider='local',
        owner_kind='main' if owner_id == 'local/main' else 'user', owner_id=owner_id)


# LLM: 复合定义经过原create_job；运行统计及线程均由真实仓库产生。
# 函数用途: 创建含脚本和工作流依赖的源监控。
def round2_seed_monitor(kit, composite):
    from hashlib import sha256

    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.scheduler.repository import SchedulerJobCreateRequest

    seed_delivery_bundle(kit)
    thread = ConversationStore(kit.main / 'data/conversations').threads.create(
        owner_id='local/main', owner_home=str(kit.main), workspace_root=str(kit.main))
    schedule = {'kind': 'every', 'every_seconds': 60, 'anchor_at': 0}
    action = {'tool_name': 'scripts', 'arguments': {'script_id': 'collect'}}
    rules = [{'id': 'first', 'name': '执行流程', 'use_llm': False, 'schedule': schedule,
        'tool': {'name': 'workflows', 'arguments': {'workflow_id': 'flow'}}, 'precheck': action}] if composite else None
    job, _ = round2_repo(kit.main, 'local/main').create_job(SchedulerJobCreateRequest(
        name='监控合成定义', prompt='仅用于测试', thread_id=thread.thread_id,
        schedule=schedule, precheck=action, rules=rules, skill_refs=[{'stable_id': 'owner:guide',
            'content_sha256': sha256((kit.main / 'skills/guide/SKILL.md').read_bytes()).hexdigest()}]))
    return job


# LLM: 真实Scheduler-only源也须进入闭包；重名后仅引用接收方新ID，不沿用发送方线程。
# 函数用途: 覆盖单任务与复合规则的默认暂停、依赖重写、启用和停用。
@pytest.mark.parametrize('composite', [False, True])
def test_round2_monitor_export_install_toggle_real_scheduler(kit, composite):
    import json
    from hashlib import sha256

    source = round2_seed_monitor(kit, composite)
    ScriptStore(kit.alice).save_script(AssetDescriptor('collect', 'script', '旧脚本'), 'print("old")')
    sent = kit.service.send_bundle('local/main', 'local/alice', 'monitor', source['job_id'])
    result = kit.service.accept_delivery('local/alice', sent['delivery_id'])
    installed = {row['source_id']: row['id'] for row in result['installed_assets']}
    repo = round2_repo(kit.alice, 'local/alice')
    job = repo.get_job(source['job_id'])
    assert job['status'] == 'paused' and job['enabled'] is False
    assert job['owner']['id'] == 'local/alice' and job['thread_id'] != source['thread_id']
    assert job['skill_refs'][0]['content_sha256'] == sha256((kit.alice / 'skills/guide/SKILL.md').read_bytes()).hexdigest()
    action = job['rules'][0]['precheck'] if composite else job['precheck']
    assert action['arguments']['script_id'] == installed['collect'] != 'collect'
    if composite:
        assert job['rules'][0]['tool']['arguments']['workflow_id'] == installed['flow']
    binding_path = kit.alice / 'data/monitors' / (job['job_id'] + '.json')
    before = json.loads(binding_path.read_text())
    for enabled in (True, False):
        result = kit.service.toggle_asset('local/alice', 'monitor', job['job_id'], enabled)
        current = repo.get_job(job['job_id'])
        binding = json.loads(binding_path.read_text())
        assert result['asset']['enabled'] is enabled
        assert binding['enabled'] is enabled and binding['schedule_spec']['enabled'] is enabled
        assert binding['revision'] > before['revision']
        assert current['enabled'] is enabled and current['status'] == ('active' if enabled else 'paused')


# LLM: 原batch真正提交点失败后，监控调度器和同包依赖均不允许残留新任务。
# 函数用途: 覆盖正式batch未提交前Scheduler不抢先发表、故障不残留资产。
def test_round2_monitor_scheduler_rolls_back_with_asset_batch(kit, monkeypatch):
    from agent_py_agent.agent.assets import workspace_assets

    source = round2_seed_monitor(kit, False)
    sent = kit.service.send_bundle('local/main', 'local/alice', 'monitor', source['job_id'])

    # LLM: 只在原事务发表点注入IO故障，不替换Scheduler或整个接收服务。
    # 函数用途: 检查真实监控仅在待提交清单而未进入账本时触发失败。
    def fail_commit(*args, **kwargs):
        assert round2_repo(kit.alice, 'local/alice').list_jobs()[0] == []
        assert ['data', 'scheduler', 'store.json'] not in [entry['parts'] for entry in args[1]]
        assert args[2]['scheduler_jobs'][source['job_id']]['after']['status'] == 'paused'
        raise OSError('synthetic batch failure')

    monkeypatch.setattr(workspace_assets, '_commit_staged', fail_commit)
    with pytest.raises(OSError, match='synthetic batch failure'):
        kit.service.accept_delivery('local/alice', sent['delivery_id'])
    assert round2_repo(kit.alice, 'local/alice').list_jobs()[0] == []
    assert not (kit.alice / 'data/monitors' / (source['job_id'] + '.json')).exists()
    assert not (kit.alice / 'scripts/collect/script.json').exists()


# LLM: 未引号字面量必须在任何clean前被拦截；显式替换仅改值，不泄漏诊断。
# 函数用途: 钉住短YAML/env秘密以及安全非秘密表达式。
@pytest.mark.parametrize('literal', ['password: abc123', 'API_KEY=short1', 'export token=x',
    '- password: abc', '  - api_key: 12345', 'PASSWORD=123456', '- password: 888888',
    'api_key: 987654321', 'password: +123', 'password: -123'])
def test_round2_unquoted_short_secrets(sanitize, tmp_path, literal):
    from agent_py_agent.agent.assets.delivery_contracts import AssetDeliveryError

    with pytest.raises(AssetDeliveryError) as failure:
        sanitize({'skill/a/SKILL.md': (literal + '\n').encode()}, owner_home=tmp_path)
    assert failure.value.error_code == 'ASSET_DELIVERY_SECRET_DETECTED'
    result = sanitize({'script/a/main.py': (literal + '\n').encode()}, owner_home=tmp_path,
        allow_secret_placeholder_replace=True)
    assert b'${REDACTED_SECRET}' in result['script/a/main.py'] and literal.encode() not in result['script/a/main.py']


@pytest.mark.parametrize('value', ['true', 'false', 'null', 'none', 'yes', 'no', '~', '$PASSWORD',
    'os.environ', 'env.PASSWORD', '${PASSWORD}', '<PASSWORD>', 'YOUR_PASSWORD', 'CHANGE_ME'])
@pytest.mark.parametrize('serialized', [False, True])
def test_round2_unquoted_nonsecrets_preserved(sanitize, tmp_path, value, serialized):
    import json

    text = 'password: ' + value + '\n'
    name = 'workflow/a/workflow.json' if serialized else 'skill/a/SKILL.md'
    raw = (json.dumps({'body_markdown': text}) if serialized else text).encode()
    assert sanitize({name: raw}, owner_home=tmp_path)[name] == raw


# LLM: 数字仅在高危凭据键下作为PIN，不把普通业务配置数字都判成秘密。
# 函数用途: 保留正常数字配置，覆盖JSON内编码的列表凭据拒绝与安全替换。
@pytest.mark.parametrize('serialized', [False, True])
def test_round3_yaml_numeric_secret_and_ordinary_numbers(sanitize, tmp_path, serialized):
    import json

    from agent_py_agent.agent.assets.delivery_contracts import AssetDeliveryError

    safe = 'timeout: 30\nretries=3\n- timeout: 60\n'
    text = safe + '- password: abc\n- api_key: 12345\nPASSWORD=123456\n'
    name = 'fixture.json' if serialized else 'fixture.yaml'
    # LLM: 序列化只是输入格式，原业务文本不得先过clean_json。
    # 函数用途: 编码裸文本或含原始正文的JSON。
    def encode(body):
        return (json.dumps({'body': body}) if serialized else body).encode()
    assert sanitize({name: encode(safe)}, owner_home=tmp_path)[name] == encode(safe)
    with pytest.raises(AssetDeliveryError) as failure:
        sanitize({name: encode(text)}, owner_home=tmp_path)
    assert failure.value.error_code == 'ASSET_DELIVERY_SECRET_DETECTED'
    raw = sanitize({name: encode(text)}, owner_home=tmp_path,
        allow_secret_placeholder_replace=True)[name]
    cleaned = json.loads(raw)['body'] if serialized else raw.decode()
    assert cleaned == safe + '- password: ${REDACTED_SECRET}\n- api_key: ${REDACTED_SECRET}\nPASSWORD=${REDACTED_SECRET}\n'


# LLM: 只用临时Owner的原SchedulerDueIndex读取真实行，不领取lease或运行监控。
# 函数用途: 观察alice的唤醒投影，避免通过mock声称双存储和索引一致。
def round3_due_rows(kit):
    import sqlite3

    from agent_py_agent.agent.assets.script_store_support import delivery_scheduler

    repo = delivery_scheduler(kit.alice, 'local/alice')
    with sqlite3.connect(f'file:{repo.due_index.path}?mode=ro', uri=True) as connection:
        return connection.execute("SELECT next_due_at FROM scheduler_due_owners WHERE provider='local' AND owner_kind='user' AND owner_id='alice'").fetchall()


# LLM: 使用真实Scheduler API创建无外部动作的另一任务和queued预留，不实际运行脚本/模型。
# 函数用途: 提供不能被投递undo抹掉的正常调度事实。
def round4_other_job(kit):
    from agent_py_agent.agent.scheduler.repository import SchedulerJobCreateRequest

    repo = round2_repo(kit.alice, 'local/alice')
    job, _ = repo.create_job(SchedulerJobCreateRequest(name='正常任务', prompt='合成正常任务',
        thread_id='thread_unrelated', schedule={'kind': 'every', 'every_seconds': 60, 'anchor_at': 0}))
    return repo, job


@pytest.mark.parametrize('operation', ['install', 'toggle'])
@pytest.mark.parametrize('point', ['partial', 'scheduler'])
def test_round4_scheduler_recovers_before_normal_tick_and_keeps_other_run(kit, operation, point):
    import json

    from agent_py_agent.agent.assets import asset_root_lock

    source = round2_seed_monitor(kit, False)
    repo, other = round4_other_job(kit)
    old_run = repo.reserve_manual_run(other['job_id'])
    sent = kit.service.send_bundle('local/main', 'local/alice', 'monitor', source['job_id'])
    if operation == 'toggle':
        kit.service.accept_delivery('local/alice', sent['delivery_id'], initial_enabled=True)
    crash_point = ('toggle_scheduler' if point == 'scheduler' else 'toggle') if operation == 'toggle' else point
    round3_crash(kit, sent['delivery_id'], source['job_id'], point=crash_point, enabled=False)
    assert (kit.alice / '.asset-batch.json').exists()
    # 使用原仓库正常写口，不能由测试先调用资产恢复替它补救。
    _, second = round4_other_job(kit)
    new_run = repo.reserve_manual_run(second['job_id'])
    assert not (kit.alice / '.asset-batch.json').exists()
    with asset_root_lock(kit.alice):
        store = json.loads(repo.store_path.read_text())
        assert store['jobs'][other['job_id']]['name'] == '正常任务'
        assert store['runs'][old_run['run_id']]['job_id'] == other['job_id']
        assert store['runs'][new_run['run_id']]['job_id'] == second['job_id']
        assert (source['job_id'] in store['jobs']) is (operation == 'toggle')
        if operation == 'toggle':
            assert store['jobs'][source['job_id']]['enabled'] is True


@pytest.mark.parametrize('action', ['accept', 'reject'])
def test_round4_committed_monitor_survives_other_scheduler_ticks(kit, action):
    from agent_py_agent.agent.assets.delivery_contracts import AssetDeliveryError

    source = round2_seed_monitor(kit, False)
    repo, other = round4_other_job(kit)
    sent = kit.service.send_bundle('local/main', 'local/alice', 'monitor', source['job_id'])
    round3_crash(kit, sent['delivery_id'], source['job_id'], point='committed')
    run = repo.reserve_manual_run(other['job_id'])
    if action == 'accept':
        result = kit.service.accept_delivery('local/alice', sent['delivery_id'])
        assert result['status'] == 'accepted'
    else:
        with pytest.raises(AssetDeliveryError) as failure:
            kit.service.reject_delivery('local/alice', sent['delivery_id'])
        assert failure.value.error_code == 'DELIVERY_STATE_CONFLICT'
    envelope = kit.service._envelope(kit.alice, 'local/alice', sent['delivery_id'])
    assert envelope['status'] == 'accepted'
    assert repo.get_job(source['job_id'])['enabled'] is True
    assert repo._store_snapshot()['runs'][run['run_id']]['job_id'] == other['job_id']


@pytest.mark.parametrize('text,values', [
    ('auth: {password: 123456, timeout: 30}\n', ('123456',)),
    ('items: [{api_key: 999999}]\n', ('999999',)),
    ('password:\n  123456\ntimeout: 30\n', ('123456',)),
    ('password:\n  - 123456\n  - short-pin\ntimeout: 30\n', ('123456', 'short-pin')),
    *[(f'secret: {style}\n  line1\n\n  line2\ntimeout: 30\n', ('line1', 'line2'))
      for style in ('|', '>', '|-', '|+', '>-', '>+')],
])
@pytest.mark.parametrize('serialized', [False, True])
def test_round4_structured_yaml_secret_reject_and_redact(sanitize, tmp_path, text, values, serialized):
    import json

    from agent_py_agent.agent.assets.delivery_contracts import AssetDeliveryError

    name = 'workflow.json' if serialized else 'fixture.yaml'
    raw = (json.dumps({'body': text}) if serialized else text).encode()
    with pytest.raises(AssetDeliveryError) as failure:
        sanitize({name: raw}, owner_home=tmp_path)
    assert failure.value.error_code == 'ASSET_DELIVERY_SECRET_DETECTED'
    output = sanitize({name: raw}, owner_home=tmp_path, allow_secret_placeholder_replace=True)[name]
    cleaned = json.loads(output)['body'] if serialized else output.decode()
    assert all(value not in cleaned for value in values)
    assert '***REDACTED***' in cleaned
    if 'secret: ' in text:
        assert cleaned.splitlines()[0] == text.splitlines()[0]
        assert cleaned.splitlines()[1] == '  ***REDACTED***'
    assert sanitize({name: output}, owner_home=tmp_path)[name] == output


@pytest.mark.parametrize('text,expected', [
    ('password: [123456, abc]\n', 'password: "***REDACTED***"\n'),
    ('"password": 123456\n', '"password": "***REDACTED***"\n'),
    ('password: short secret\n', 'password: "***REDACTED***"\n'),
    ('secret: |\n  false\n', 'secret: |\n  ***REDACTED***\n'),
    ('password:\n  first-line\n  second-line\ntimeout: 30\n',
        'password:\n  "***REDACTED***"\ntimeout: 30\n'),
    ('{password:\n  1}\n', '{password:\n  "***REDACTED***"}\n'),
    ('- password: |\n    abc\n  port: 80\n', '- password: |\n    ***REDACTED***\n  port: 80\n'),
])
def test_round4_yaml_scalar_shape_is_preserved(sanitize, tmp_path, text, expected):
    from agent_py_agent.agent.assets.delivery_contracts import AssetDeliveryError

    with pytest.raises(AssetDeliveryError):
        sanitize({'fixture.yaml': text.encode()}, owner_home=tmp_path)
    output = sanitize({'fixture.yaml': text.encode()}, owner_home=tmp_path,
        allow_secret_placeholder_replace=True)['fixture.yaml']
    assert output.decode() == expected


def test_round4_snapshot_recovers_journal_created_before_read_lock(kit, monkeypatch):
    from contextlib import contextmanager

    from agent_py_agent.agent.assets import workspace_assets

    source = round2_seed_monitor(kit, False)
    repo, other = round4_other_job(kit)
    sent = kit.service.send_bundle('local/main', 'local/alice', 'monitor', source['job_id'])
    original, injected = workspace_assets._read_lock, []

    @contextmanager
    def crash_before_read_lock(root, parts):
        if root == kit.alice and tuple(parts) == ('.asset-batch.lock',) and not injected:
            injected.append(True)
            round3_crash(kit, sent['delivery_id'], source['job_id'], point='partial')
        with original(root, parts):
            yield

    monkeypatch.setattr(workspace_assets, '_read_lock', crash_before_read_lock)
    try:
        jobs, errors = repo.list_jobs()
    except Exception as error:
        pytest.fail(f'普通读取应当恢复pending，而非抛出{type(error).__name__}')
    assert not errors and {row['job_id'] for row in jobs} == {other['job_id']}
    assert injected and not (kit.alice / '.asset-batch.json').exists()


def test_round4_toggle_commit_history_failure_is_replayed_without_rollback(kit, monkeypatch):
    import json

    from agent_py_agent.agent.common import json_io

    source = round2_seed_monitor(kit, False)
    identifier = source['job_id']
    sent = kit.service.send_bundle('local/main', 'local/alice', 'monitor', identifier)
    kit.service.accept_delivery('local/alice', sent['delivery_id'], initial_enabled=True)
    repo = round2_repo(kit.alice, 'local/alice')
    run = repo.reserve_manual_run(identifier)
    append = json_io.append_private_jsonl_records

    def fail_history(path, records):
        if path == repo.history_path:
            raise OSError('synthetic history append failure')
        return append(path, records)

    with monkeypatch.context() as patch:
        patch.setattr(json_io, 'append_private_jsonl_records', fail_history)
        with pytest.raises(OSError, match='synthetic history'):
            kit.service.toggle_asset('local/alice', 'monitor', identifier, False)
    assert (kit.alice / '.asset-batch.json').exists()
    assert repo.get_job(identifier)['enabled'] is False
    history = [json.loads(line) for line in repo.history_path.read_text().splitlines()]
    assert len([row for row in history if row['run_id'] == run['run_id']]) == 1
    assert run['run_id'] not in repo._store_snapshot()['runs']
    assert not (kit.alice / '.asset-batch.json').exists()


def test_round4_toggle_hard_exit_after_commit_decision_replays_history(kit):
    import json

    source = round2_seed_monitor(kit, False)
    identifier = source['job_id']
    sent = kit.service.send_bundle('local/main', 'local/alice', 'monitor', identifier)
    kit.service.accept_delivery('local/alice', sent['delivery_id'], initial_enabled=True)
    repo = round2_repo(kit.alice, 'local/alice')
    run = repo.reserve_manual_run(identifier)
    round3_crash(kit, sent['delivery_id'], identifier, point='history', enabled=False)
    journal = json.loads((kit.alice / '.asset-batch.json').read_text())
    assert journal['committed'] is True and journal['scheduler_history']
    assert json.loads(repo.store_path.read_text())['jobs'][identifier]['enabled'] is False
    assert repo.get_job(identifier)['enabled'] is False
    rows = [json.loads(line) for line in repo.history_path.read_text().splitlines()]
    assert len([row for row in rows if row['run_id'] == run['run_id']]) == 1
    assert run['run_id'] not in repo._store_snapshot()['runs']
    assert not (kit.alice / '.asset-batch.json').exists()


# LLM: 故障只注入原文件发表叶或原batch返回点；os._exit不经过Python except/finally，子进程从当前工作树导入。
# 函数用途: 在临时接收方制造真实持久半提交，保留原undo给另一个进程恢复。
def round3_crash(kit, delivery_id, monitor_id, *, point, strategy='keep_both_rename', enabled=True):
    import json
    import subprocess
    import sys
    from pathlib import Path

    code = '''
import json, os, sys
from pathlib import Path
from agent_py_agent.agent.assets import workspace_assets as assets
from agent_py_agent.agent.assets import delivery_service
from agent_py_agent.agent.assets import script_store_support as support
from agent_py_agent.agent.common import json_io
from agent_py_agent.agent.assets.delivery_service import AssetDeliveryService
root, delivery_id, identifier, point, strategy, enabled = json.loads(sys.argv[1])
home = Path(root) / 'owners/local/alice'
service = AssetDeliveryService(root)
put = assets._put_text
direct_put = delivery_service.write_bytes_atomic_beneath
def interrupt_direct(target, parts, content, **kwargs):
    result = direct_put(target, parts, content, **kwargs)
    if Path(target) == home and tuple(parts) == ('data', 'monitors', identifier + '.json'):
        os._exit(79)
    return result
def interrupt_leaf(target, parts, content):
    put(target, parts, content)
    if Path(target) == home and tuple(parts) == ('data', 'monitors', identifier + '.json'):
        os._exit(79)
commit = assets._commit_staged
def interrupt_committed(target, entries, journal, **kwargs):
    commit(target, entries, journal, **kwargs)
    os._exit(79)
apply_jobs = support.apply_scheduler_batch
def interrupt_scheduler(target, jobs, runs, **kwargs):
    apply_jobs(target, jobs, runs, **kwargs)
    if Path(target) == home and not kwargs.get('previous'):
        os._exit(79)
append = json_io.append_private_jsonl_records
def interrupt_history(path, records):
    if Path(path) == home / 'data/scheduler/history.jsonl':
        os._exit(79)
    return append(path, records)
if point == 'committed':
    assets._commit_staged = interrupt_committed
elif point in {'scheduler', 'toggle_scheduler'}:
    support.apply_scheduler_batch = interrupt_scheduler
elif point == 'history':
    json_io.append_private_jsonl_records = interrupt_history
else:
    assets._put_text = interrupt_leaf
    delivery_service.write_bytes_atomic_beneath = interrupt_direct
if point in {'toggle', 'toggle_scheduler', 'history'}:
    service.toggle_asset('local/alice', 'monitor', identifier, enabled)
else:
    service.accept_delivery('local/alice', delivery_id, conflict_strategy=strategy, initial_enabled=enabled)
'''
    args = [str(kit.root), delivery_id, monitor_id, point, strategy, enabled]
    result = subprocess.run([sys.executable, '-c', code, json.dumps(args)],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, timeout=30)
    assert result.returncode == 79, result.stderr.decode()


# LLM: prepared及undo必须含精确job前后事实而非整库字节；恢复旧资产及本job后仅安装一次，不产生第二次rename。
# 函数用途: 覆盖首次/重名/覆盖接收在监控绑定发表后的硬退出及自动恢复。
@pytest.mark.parametrize('collision,strategy', [(False, 'keep_both_rename'), (True, 'keep_both_rename'), (True, 'overwrite')])
def test_round3_accept_hard_exit_restores_dual_store_and_retries(kit, monkeypatch, collision, strategy):
    import json

    from agent_py_agent.agent.assets import workspace_assets

    source = round2_seed_monitor(kit, False)
    if collision:
        first = kit.service.send_bundle('local/main', 'local/alice', 'monitor', source['job_id'])
        kit.service.accept_delivery('local/alice', first['delivery_id'], initial_enabled=True)
    sent = kit.service.send_bundle('local/main', 'local/alice', 'monitor', source['job_id'])
    # 首次冲突改名由现有规则确定；在子进程prepared后再从清单取得真实目标，不推断随机ID。
    # 取现有命名实现的实际映射，不安装或更改正式资产。
    from agent_py_agent.agent.assets.script_store_support import prepare_delivery_items
    envelope = kit.service._envelope(kit.alice, 'local/alice', sent['delivery_id'])
    _, _, installed = prepare_delivery_items(kit.alice, envelope, kit.service._payloads(kit.alice, envelope), strategy, True, 'fixture')
    target = next(row['id'] for row in installed if row['kind'] == 'monitor')
    binding = kit.alice / 'data/monitors' / (target + '.json')
    store_path = kit.alice / 'data/scheduler/store.json'
    previous = (binding.read_bytes() if binding.exists() else None,
        store_path.read_bytes() if store_path.exists() else None)
    round3_crash(kit, sent['delivery_id'], target, point='partial', strategy=strategy)
    journal = json.loads((kit.alice / '.asset-batch.json').read_text())
    assert ['data', 'scheduler', 'store.json'] not in [entry['parts'] for entry in journal['entries']]
    assert journal['scheduler_jobs'][target]['after']['enabled'] is True
    envelope = kit.service._envelope(kit.alice, 'local/alice', sent['delivery_id'])
    assert ['data', 'scheduler', 'store.json'] not in [entry['parts'] for entry in envelope['resolution']['expected_files']]
    assert envelope['resolution']['expected_scheduler_jobs']
    restored, restore = [], workspace_assets._restore_batch

    # LLM: 仅观察真实恢复结果，不替代恢复；在接收重试的根锁内核对旧字节。
    # 函数用途: 证明安装重试确实先恢复两份存储而不是覆盖半写现场。
    def observe_restore(root, entries, scheduler_jobs=None, scheduler_runs=None):
        restore(root, entries, scheduler_jobs, scheduler_runs)
        restored.append(True)
        assert (binding.read_bytes() if binding.exists() else None) == previous[0]
        before_jobs = json.loads(previous[1])['jobs'] if previous[1] is not None else {}
        assert json.loads(store_path.read_bytes())['jobs'] == before_jobs

    monkeypatch.setattr(workspace_assets, '_restore_batch', observe_restore)
    result = kit.service.accept_delivery('local/alice', sent['delivery_id'], conflict_strategy=strategy, initial_enabled=True)
    assert restored == [True] and result['status'] == 'accepted'
    assert kit.service.accept_delivery('local/alice', sent['delivery_id'])['installed_assets'] == result['installed_assets']
    jobs = round2_repo(kit.alice, 'local/alice').list_jobs()[0]
    assert {row['job_id'] for row in jobs} == ({source['job_id'], target} if collision else {target})
    assert json.loads(binding.read_text())['enabled'] is True
    assert round2_repo(kit.alice, 'local/alice').get_job(target)['enabled'] is True
    assert round3_due_rows(kit) and not (kit.alice / '.asset-batch.json').exists()
    assert not list(kit.alice.rglob('*.stage-*'))


# LLM: batch已提交但尚未结算时重试必须只补索引及回执，不能再次重命名或重写store。
# 函数用途: 覆盖提交后、due索引和accepted回执前的真实硬退出。
def test_round3_committed_accept_retry_resyncs_due_without_reinstall(kit):
    import json

    from agent_py_agent.agent.assets.script_store_support import delivery_scheduler

    source = round2_seed_monitor(kit, False)
    first = kit.service.send_bundle('local/main', 'local/alice', 'monitor', source['job_id'])
    kit.service.accept_delivery('local/alice', first['delivery_id'])
    sent = kit.service.send_bundle('local/main', 'local/alice', 'monitor', source['job_id'])
    round3_crash(kit, sent['delivery_id'], source['job_id'], point='committed')
    assert not (kit.alice / '.asset-batch.json').exists()
    envelope = kit.service._envelope(kit.alice, 'local/alice', sent['delivery_id'])
    assert envelope['status'] == 'pending'
    assert ['data', 'scheduler', 'store.json'] not in [entry['parts'] for entry in envelope['resolution']['expected_files']]
    assert envelope['resolution']['expected_scheduler_jobs']
    repo = delivery_scheduler(kit.alice, 'local/alice')
    before = repo.store_path.read_bytes()
    repo._sync_due_index_unlocked({'jobs': {}, 'runs': {}})
    assert round3_due_rows(kit) == []
    result = kit.service.accept_delivery('local/alice', sent['delivery_id'])
    assert result['status'] == 'accepted' and round3_due_rows(kit)
    assert repo.store_path.read_bytes() == before
    installed = next(row['id'] for row in result['installed_assets'] if row['kind'] == 'monitor')
    assert installed != source['job_id']
    assert {row['job_id'] for row in repo.list_jobs()[0]} == {source['job_id'], installed}
    assert json.loads((kit.alice / 'data/monitors' / (installed + '.json')).read_text())['enabled'] is True


# LLM: 硬退出恢复绑定原字节、本job/run原事实及唤醒投影；不要求回滚store时间或整库格式。
# 函数用途: 覆盖启用/停用两方向的双存储耐久undo。
@pytest.mark.parametrize('enabled', [False, True])
def test_round3_toggle_hard_exit_recovers_both_files_and_due_index(kit, enabled):
    import json

    from agent_py_agent.agent.assets import asset_root_lock
    from agent_py_agent.agent.assets.script_store_support import delivery_scheduler

    source = round2_seed_monitor(kit, False)
    identifier = source['job_id']
    sent = kit.service.send_bundle('local/main', 'local/alice', 'monitor', identifier)
    kit.service.accept_delivery('local/alice', sent['delivery_id'], initial_enabled=not enabled)
    repo = delivery_scheduler(kit.alice, 'local/alice')
    binding = kit.alice / 'data/monitors' / (identifier + '.json')
    before = (binding.read_bytes(), repo.store_path.read_bytes())
    round3_crash(kit, sent['delivery_id'], identifier, point='toggle', enabled=enabled)
    assert json.loads(binding.read_text())['enabled'] is enabled
    assert (kit.alice / '.asset-batch.json').exists()
    dirty = json.loads(before[1])
    dirty['jobs'][identifier].update(enabled=enabled, status='active' if enabled else 'paused')
    repo._sync_due_index_unlocked(dirty)
    assert bool(round3_due_rows(kit)) is enabled
    with asset_root_lock(kit.alice):
        assert binding.read_bytes() == before[0]
        current, old = json.loads(repo.store_path.read_bytes()), json.loads(before[1])
        assert current['jobs'] == old['jobs'] and current['runs'] == old['runs']
        assert bool(round3_due_rows(kit)) is not enabled
    assert not (kit.alice / '.asset-batch.json').exists()
    assert not list(kit.alice.rglob('*.stage-*'))
    assert kit.service.toggle_asset('local/alice', 'monitor', identifier, enabled)['enabled'] is enabled
    assert repo.get_job(identifier)['enabled'] is enabled


# LLM: 人为回退通知模拟已结算后写失败，幂等重试应修复而非重装。
# 函数用途: 覆盖初次结算及终态早返的通知恢复。
@pytest.mark.parametrize('status', ['accepted', 'rejected'])
def test_round2_notification_final_state_and_retry(kit, status):
    import json

    seed_delivery_bundle(kit)
    sent = kit.service.send_bundle('local/main', 'local/alice', 'script', 'collect')
    path = kit.alice / 'data/asset_delivery/notifications' / (sent['delivery_id'] + '.json')
    resolve = kit.service.accept_delivery if status == 'accepted' else kit.service.reject_delivery
    for _ in range(2):
        assert resolve('local/alice', sent['delivery_id'])['status'] == status
        event = json.loads(path.read_text())
        assert event['status'] == status and event['resolved_at']
        path.write_text(json.dumps({**event, 'status': 'queued'}))
    path.unlink()
    assert resolve('local/alice', sent['delivery_id'])['status'] == status
    assert json.loads(path.read_text())['status'] == status


# LLM: 消费只持久领取队列，不冒充外部通道送达；终态投递不得再次出队。
# 函数用途: 验证通道/数量过滤、幂等消费和prepared中断对账。
def test_round2_notification_consumer_is_durable_and_filtered(kit):
    import json

    seed_delivery_bundle(kit)
    sent = [kit.service.send_bundle('local/main', 'local/alice', 'script', 'collect') for _ in range(3)]
    kit.service.reject_delivery('local/alice', sent[0]['delivery_id'])
    path = kit.alice / 'data/asset_delivery/notifications' / (sent[1]['delivery_id'] + '.json')
    path.write_text(json.dumps({**json.loads(path.read_text()), 'status': 'prepared'}))
    consume = getattr(kit.service, 'consume_notifications', None)
    assert callable(consume), '缺少生产队列消费入口'
    assert consume('local/alice', channel='feishu')['notifications'] == []
    rows = consume('local/alice', channel='local', limit=1)['notifications']
    assert len(rows) == 1 and rows[0]['status'] == 'dispatched' and rows[0]['dispatched_at']
    assert len(consume('local/alice')['notifications']) == 1
    assert consume('local/alice')['notifications'] == []


# LLM: 队列只能取发送时冻结的接收通道，不能消费被改写路由的记录并交给外部发送器。
# 函数用途: 钉住通知通道、接收人及幂等键的消费完整性。
@pytest.mark.parametrize('key,value', [('channel', 'feishu'), ('recipient_open_id', 'ou_other'),
    ('idempotency_key', 'other')])
def test_round2_consumer_rejects_modified_routing(kit, key, value):
    import json

    from agent_py_agent.agent.assets.delivery_contracts import AssetDeliveryError

    seed_delivery_bundle(kit)
    sent = kit.service.send_bundle('local/main', 'local/alice', 'script', 'collect')
    path = kit.alice / 'data/asset_delivery/notifications' / (sent['delivery_id'] + '.json')
    path.write_text(json.dumps({**json.loads(path.read_text()), key: value}))
    with pytest.raises(AssetDeliveryError) as failure:
        kit.service.consume_notifications('local/alice')
    assert failure.value.error_code == 'DELIVERY_STORE_CORRUPT'


# LLM: 使用原due索引和真实queued运行，不执行脚本/模型；停用应撤下唤醒并取消已有未开工预留。
# 函数用途: 验证默认暂停的运行闸、启用唤醒、停用取消及历史保存。
def test_round2_monitor_due_index_and_queued_cancellation(kit):
    import json
    import time

    from agent_py_agent.agent.scheduler import SchedulerDueIndex
    from agent_py_agent.agent.user_space.home_layout import home_paths

    source = round2_seed_monitor(kit, False)
    sent = kit.service.send_bundle('local/main', 'local/alice', 'monitor', source['job_id'])
    kit.service.accept_delivery('local/alice', sent['delivery_id'])
    repo = round2_repo(kit.alice, 'local/alice')
    index = SchedulerDueIndex(home_paths(kit.root).global_index_dir / 'scheduler_due.sqlite3')
    future = time.time() + 600
    assert repo.reserve_due_runs(now=future) == []
    assert index.claim_due_owners(now=future) == []
    kit.service.toggle_asset('local/alice', 'monitor', source['job_id'], True)
    owners = index.claim_due_owners(now=future)
    assert [(row.provider, row.owner_kind, row.owner_id) for row in owners] == [('local', 'user', 'alice')]
    run = repo.reserve_manual_run(source['job_id'])
    assert run['status'] == 'queued'
    kit.service.toggle_asset('local/alice', 'monitor', source['job_id'], False)
    store = json.loads(repo.store_path.read_text())
    assert run['run_id'] not in store['runs']
    assert any(json.loads(line)['run_id'] == run['run_id'] for line in repo.history_path.read_text().splitlines())
    assert index.claim_due_owners(now=future + 1000) == []


# LLM: 有限配额仍走原锁和计量；guard仅将会死锁的重复准入变成断言，不替换实际安装/启停。
# 函数用途: 验证监控接收和启停不重入Owner配额锁。
def test_round2_monitor_install_and_toggle_share_finite_quota(kit, monkeypatch):
    from contextlib import contextmanager

    from agent_py_agent.agent.assets import delivery_service
    from agent_py_agent.agent.user_space import owner_quota

    source = round2_seed_monitor(kit, False)
    sent = kit.service.send_bundle('local/main', 'local/alice', 'monitor', source['job_id'])
    original, active = owner_quota.OwnerQuotaEnforcer.admission, set()

    # LLM: 不同Owner可按排序同时持锁，同一Owner禁止再次取得不可重入锁。
    # 函数用途: 在真实准入之外检测每个Owner的重复进入。
    @contextmanager
    def guarded(enforcer, **kwargs):
        key = enforcer.owner_root
        assert key not in active, 'nested owner quota admission'
        active.add(key)
        try:
            with original(enforcer, **kwargs) as admission:
                yield admission
        finally:
            active.remove(key)

    # LLM: 只选择合成100MiB上限，计量、文件锁及写入路径均沿产品实现。
    # 函数用途: 为两个测试Owner提供真实有限准入。
    def limited(home):
        return owner_quota.OwnerQuotaEnforcer(home, max_bytes=100 * 1024 * 1024)

    monkeypatch.setattr(owner_quota.OwnerQuotaEnforcer, 'admission', guarded)
    monkeypatch.setattr(owner_quota, 'owner_quota_enforcer_from_policy', limited)
    monkeypatch.setattr(delivery_service, 'owner_quota_enforcer_from_policy', limited)
    assert kit.service.accept_delivery('local/alice', sent['delivery_id'])['status'] == 'accepted'
    assert kit.service.toggle_asset('local/alice', 'monitor', source['job_id'], True)['enabled'] is True
    assert kit.service.toggle_asset('local/alice', 'monitor', source['job_id'], False)['enabled'] is False


# LLM: 原有监控与依赖的覆盖失败必须恢复旧值，随后按同一投递重试，不靠空store掩盖回滚错误。
# 函数用途: 验证真实已有任务的覆盖回滚、会话复用和正常重试。
def test_round2_monitor_overwrite_rollback_and_retry(kit, monkeypatch):
    import json

    from agent_py_agent.agent.assets import workspace_assets

    source = round2_seed_monitor(kit, False)
    first = kit.service.send_bundle('local/main', 'local/alice', 'monitor', source['job_id'])
    kit.service.accept_delivery('local/alice', first['delivery_id'])
    kit.service.toggle_asset('local/alice', 'monitor', source['job_id'], True)
    repo = round2_repo(kit.alice, 'local/alice')
    previous_job = repo.get_job(source['job_id'])
    binding_path = kit.alice / 'data/monitors' / (source['job_id'] + '.json')
    previous_binding = binding_path.read_bytes()
    sent = kit.service.send_bundle('local/main', 'local/alice', 'monitor', source['job_id'])
    original = workspace_assets._commit_staged

    # LLM: 原事务前最后一刻抛错，观测点仍是当前真实Scheduler与绑定。
    # 函数用途: 让覆盖在正式资产发表之前失败。
    def fail_commit(*args, **kwargs):
        assert repo.get_job(source['job_id']) == previous_job
        assert ['data', 'scheduler', 'store.json'] not in [entry['parts'] for entry in args[1]]
        assert args[2]['scheduler_jobs'][source['job_id']]['after']['status'] == 'paused'
        raise OSError('synthetic overwrite failure')

    monkeypatch.setattr(workspace_assets, '_commit_staged', fail_commit)
    with pytest.raises(OSError, match='synthetic overwrite failure'):
        kit.service.accept_delivery('local/alice', sent['delivery_id'], conflict_strategy='overwrite')
    assert repo.get_job(source['job_id']) == previous_job
    assert binding_path.read_bytes() == previous_binding
    monkeypatch.setattr(workspace_assets, '_commit_staged', original)
    result = kit.service.accept_delivery('local/alice', sent['delivery_id'], conflict_strategy='overwrite')
    assert result['status'] == 'accepted' and repo.get_job(source['job_id'])['status'] == 'paused'
    assert json.loads(binding_path.read_text())['enabled'] is False


# LLM: 走真实发送与冻结payload，不把对裸文本的检测通过外推JSON编码后的工作流正文也已替换。
# 函数用途: 验证三类资产里的未引号短秘密既能拒绝，也能在显式替换后完整移除。
@pytest.mark.parametrize('kind', ['script', 'skill', 'workflow'])
@pytest.mark.parametrize('literal', ['password: abc123', 'API_KEY=short1'])
def test_round2_unquoted_secret_service_payload_has_no_original(kit, kind, literal):
    import json

    from agent_py_agent.agent.assets.delivery_contracts import AssetDeliveryError

    identifier = 'fixture'
    if kind == 'script':
        ScriptStore(kit.main).save_script(AssetDescriptor(identifier, kind, '合成脚本'), literal + '\n')
    elif kind == 'skill':
        path = kit.main / 'skills/fixture/SKILL.md'
        path.parent.mkdir(parents=True)
        path.write_text('---\nname: fixture\ndescription: fixture\n---\n' + literal + '\n')
    else:
        WorkflowStore(kit.main).save_workflow(WorkflowDefinition(AssetDescriptor(identifier, kind, '合成流程'),
            (), body_markdown=literal + '\n'))
        path = kit.main / 'workflows/fixture/workflow.json'
        metadata = json.loads(path.read_text())
        # 真实Store会先clean正文；将合成原始字节写回临时副本，才能验投递是否在clean前硬拒绝。
        metadata['body_markdown'] = literal + '\n'
        path.write_text(json.dumps(metadata, ensure_ascii=False))
        assert literal in json.loads(path.read_text())['body_markdown']
    with pytest.raises(AssetDeliveryError) as failure:
        kit.service.send_bundle('local/main', 'local/alice', kind, identifier)
    assert failure.value.error_code == 'ASSET_DELIVERY_SECRET_DETECTED'
    sent = kit.service.send_bundle('local/main', 'local/alice', kind, identifier,
        allow_secret_placeholder_replace=True)
    payload = kit.alice / 'data/asset_delivery/inbox' / sent['delivery_id'] / 'payload'
    files = [path.read_bytes() for path in payload.rglob('*') if path.is_file()]
    assert files and any(b'${REDACTED_SECRET}' in raw for raw in files)
    assert all(literal.encode() not in raw for raw in files)


# LLM: 独审给出的原始字节命中跨Unicode转义，必须在真实扫描器复现，替换后仍可解析JSON且无原字节秘密。
# 函数用途: 防止JSON原始文本先替换导致无效转义。
def test_round2_json_replacement_preserves_unicode_escape_validity(sanitize, tmp_path):
    import json

    raw = br'{"body":"\uabcd1234"}'
    assert json.loads(raw)['body']
    output = sanitize({'payload.json': raw}, owner_home=tmp_path, extra_secrets=('abcd1234',),
        allow_secret_placeholder_replace=True)['payload.json']
    assert json.loads(output)['body'] == '${REDACTED_SECRET}'
    assert b'abcd1234' not in output


# LLM: 非字符串值或JSON语法本身若命中秘密，不能用未引号占位符破坏JSON后返回成功。
# 函数用途: 验证无法安全替换的JSON原始位置失败关闭。
@pytest.mark.parametrize('raw,secret', [(b'{"n":123}', '123'), (b'{"field":"data"}', ':')])
def test_round2_json_replacement_rejects_nonstring_secret(sanitize, tmp_path, raw, secret):
    from agent_py_agent.agent.assets.delivery_contracts import AssetDeliveryError

    with pytest.raises(AssetDeliveryError) as failure:
        sanitize({'payload.json': raw}, owner_home=tmp_path, extra_secrets=(secret,),
            allow_secret_placeholder_replace=True)
    assert failure.value.error_code == 'ASSET_DELIVERY_SECRET_DETECTED'


# LLM: 六个可选参数继续接受旧位置及命名调用，值对象必须严格拒绝未知或重复赋值；不修改管理员可信调用边界。
# 函数用途: 验证发送选项归并保持公开调用行为和自动接收默认关闭。
def test_send_optional_argument_compatibility_and_admin_boundaries(kit):
    from agent_py_agent.agent.assets.delivery_contracts import AssetDeliveryError

    seed_delivery_bundle(kit)
    sent = kit.service.send_bundle('local/main', 'local/alice', 'script', 'collect',
        'fixture', include_dependencies=True, allow_secret_placeholder_replace=False,
        auto_accept=True, auto_enable=False, sender_role='admin_provision')
    assert sent['status'] == 'accepted' and ScriptStore(kit.alice).get_script('collect')['enabled'] is False
    with pytest.raises(TypeError):
        kit.service.send_bundle('local/main', 'local/bob', 'script', 'collect', 'note', note='duplicate')
    with pytest.raises(TypeError):
        kit.service.send_bundle('local/main', 'local/bob', 'script', 'collect', unknown_option=True)
    with pytest.raises(AssetDeliveryError) as error:
        kit.service.send_bundle('local/alice', 'local/bob', 'script', 'collect', auto_accept=True)
    assert error.value.error_code == 'DELIVERY_ADMIN_PROVISION_REQUIRED'


# LLM: 普通用户工具使用真实宿主Owner后缀，不受plugin开关控制；无绑定/伪绑定或子代理不能借cwd取得权限。
# 函数用途: 验证工具注册在本地普通用户与管理员都能准确绑定身份。
@pytest.mark.parametrize('owner_id,kind', [('local/main', 'main_agent'), ('local/alice', 'user')])
def test_delivery_tool_registration_uses_trusted_owner_binding(kit, owner_id, kind):
    from types import SimpleNamespace

    from agent_py_agent.agent.tooling.asset_delivery_tool import register_asset_delivery_tool

    tools = []
    registry = SimpleNamespace(owner_type=kind, register=tools.append)
    home = kit.main if kind == 'main_agent' else kit.alice
    params = SimpleNamespace(protected_persona_root=str(home), owner_scope_root='', operation_owner_id=owner_id)
    register_asset_delivery_tool(registry, params)
    assert len(tools) == 1 and tools[0].owner_id == owner_id
    params.operation_owner_id = 'local/bob'
    register_asset_delivery_tool(registry, params)
    assert len(tools) == 1


# LLM: 只保留重绑定需求ID，不复制可执行credential_refs或发送方凭据文件；预览应在清空引用前记下需求。
# 函数用途: 验证凭据引用移除后需求摘要仍准确提示接收方重新授权。
def test_required_credential_summary_survives_portable_descriptor_cleanup(kit):
    from agent_py_agent.agent.assets.descriptor import CredentialBindingRef

    descriptor = AssetDescriptor('needs_auth', 'script', '需授权脚本',
        credential_refs=(CredentialBindingRef('credential_fixture'),))
    ScriptStore(kit.main).save_script(descriptor, 'print("{}")')
    preview = kit.service.preview_bundle('local/main', 'script', 'needs_auth')
    assert preview['required_capabilities_summary']['required_grants'] == ['credential_fixture']
    assert preview['bundled_assets'][0]['descriptor']['credential_refs'] == []


# LLM: 内存目录模拟大小写敏感系统可存在的双ID，避免在当前文件系统伪造共存；遍历顺序不得改变精确目标。
# 函数用途: 验证overwrite/skip精确ID优先，不覆盖或复用另一个大小写等价资产。
@pytest.mark.parametrize('ids', [('collect', 'COLLECT'), ('COLLECT', 'collect')])
@pytest.mark.parametrize('strategy', ['overwrite', 'skip'])
def test_casefold_multiple_candidates_preserve_exact_target(kit, monkeypatch, ids, strategy):
    from agent_py_agent.agent.assets import script_store_support as support

    records = {'script:' + identifier: {'id': identifier} for identifier in ids}
    monkeypatch.setattr(support, '_raw_catalog', lambda _: records)
    envelope = {'sender_owner_id': 'local/main', 'delivery_id': 'dlv_fixture',
        'bundled_assets': [{'kind': 'script', 'id': 'collect'}]}
    mapping, _, _ = support.delivery_conflict_mapping(kit.alice, envelope, strategy)
    assert mapping['script:collect'] == 'collect'


# LLM: 多候选但没有精确ID时不能随遍历选择；错误发生在草稿/事务写入前。
# 函数用途: 验证歧义大小写目标失败关闭且提供领域错误码。
@pytest.mark.parametrize('strategy', ['overwrite', 'skip'])
def test_casefold_ambiguous_target_without_exact_match_is_rejected(kit, monkeypatch, strategy):
    from agent_py_agent.agent.assets import script_store_support as support
    from agent_py_agent.agent.assets.delivery_contracts import AssetDeliveryError

    records = {'script:' + identifier: {'id': identifier} for identifier in ('collect', 'COLLECT')}
    monkeypatch.setattr(support, '_raw_catalog', lambda _: records)
    envelope = {'sender_owner_id': 'local/main', 'delivery_id': 'dlv_fixture',
        'bundled_assets': [{'kind': 'script', 'id': 'Collect'}]}
    with pytest.raises(AssetDeliveryError) as error:
        support.delivery_conflict_mapping(kit.alice, envelope, strategy)
    assert error.value.error_code == 'ASSET_DELIVERY_CONFLICT_AMBIGUOUS'


@pytest.mark.parametrize('action', ['preview', 'send'])
def test_missing_dependency_service_and_tool_preserve_domain_details(kit, action):
    import json

    from agent_py_agent.agent.assets.workflow_store import WorkflowStepSpec
    from agent_py_agent.agent.tooling.asset_delivery_tool import AssetDeliveryTool

    definition = WorkflowDefinition(AssetDescriptor('incomplete', 'workflow', '缺依赖'),
        (WorkflowStepSpec('s', '缺失脚本', 'deterministic_script', script_id='absent'),))
    WorkflowStore(kit.main).save_workflow(definition)
    invoke = kit.service.preview_bundle if action == 'preview' else kit.service.send_bundle
    args = ('local/main', 'workflow', 'incomplete') if action == 'preview' else (
        'local/main', 'local/alice', 'workflow', 'incomplete')
    with pytest.raises(ValueError) as error:
        invoke(*args)
    assert error.value.error_code == 'ASSET_DELIVERY_DEPENDENCY_MISSING'
    assert error.value.details['missing_refs'] == ['script:absent']
    params = {'action': action, 'asset_kind': 'workflow', 'asset_id': 'incomplete'}
    if action == 'send':
        params['recipient_owner_id'] = 'local/alice'
    result = AssetDeliveryTool(kit.root, 'local/main').execute(params)
    assert result.ok is False and result.reported_error_code == 'ASSET_DELIVERY_DEPENDENCY_MISSING'
    payload = json.loads(result.output)
    assert payload['error_code'] == 'ASSET_DELIVERY_DEPENDENCY_MISSING'
    assert payload['missing_refs'] == ['script:absent']
    assert kit.service.list_inbox('local/alice')['deliveries'] == []


def test_tool_preserves_closure_error_from_downstream_adapter(kit, monkeypatch):
    import json

    from agent_py_agent.agent.tooling.asset_delivery_tool import AssetDeliveryTool

    tool = AssetDeliveryTool(kit.root, 'local/main')

    def missing(*args, **kwargs):
        raise topology.AssetClosureError(['script:absent'])

    monkeypatch.setattr(tool.service, 'preview_bundle', missing)
    result = tool.execute({'action': 'preview', 'asset_kind': 'workflow', 'asset_id': 'incomplete'})
    assert result.reported_error_code == 'ASSET_DELIVERY_DEPENDENCY_MISSING'
    payload = json.loads(result.output)
    assert payload['error_code'] == 'ASSET_DELIVERY_DEPENDENCY_MISSING'
    assert payload['missing_refs'] == ['script:absent']


@pytest.mark.parametrize('secret', ['CustomDbPass_998877', 'q9Z!', 'z'])
def test_datasource_secret_raw_scan_precedes_cleaning_and_never_echoes(kit, monkeypatch, secret):
    import json

    from agent_py_agent.agent.assets.delivery_contracts import AssetDeliveryError

    monkeypatch.delenv('FIXTURE_DELIVERY_TOKEN', raising=False)
    secret_file = kit.main / 'data/datasource_secrets/cred-1.json'
    secret_file.parent.mkdir(parents=True)
    secret_file.write_text(json.dumps({'schema_version': 'v1', 'credential_ref': 'cred-1',
        'fields': {'nested': ['ignore', {'custom_field': secret}]}}))
    ScriptStore(kit.main).save_script(AssetDescriptor('custom_secret', 'script', '合成脚本'),
        'value = ' + repr(secret) + '\nprint("{}")')
    with pytest.raises(AssetDeliveryError) as error:
        kit.service.send_bundle('local/main', 'local/alice', 'script', 'custom_secret')
    assert error.value.error_code == 'ASSET_DELIVERY_SECRET_DETECTED'
    assert secret not in str(error.value.to_dict())
    assert kit.service.list_inbox('local/alice')['deliveries'] == []
    assert not (kit.main / 'data/asset_delivery/outbox').exists()


def test_datasource_secret_metadata_does_not_block_safe_script(kit):
    import json

    secret_file = kit.main / 'data/datasource_secrets/cred-1.json'
    secret_file.parent.mkdir(parents=True)
    secret_file.write_text(json.dumps({'schema_version': 'metadata_marker', 'credential_ref': 'cred-1',
        'datasource_id': 'source-1', 'updated_at': '2026-10-26', 'created_at': 'fixture-created'}))
    ScriptStore(kit.main).save_script(AssetDescriptor('safe', 'script', '安全'),
        'value = "metadata_marker cred-1 source-1 2026-10-26 fixture-created"\nprint("{}")')
    assert kit.service.send_bundle('local/main', 'local/alice', 'script', 'safe')['ok'] is True


@pytest.mark.parametrize('recipient,home_name,channel', [
    ('local/alice', 'alice', 'local'), ('providers/feishu/users/ou_fixture', 'feishu', 'feishu')])
def test_notification_is_durable_queued_and_visible_to_inbox(kit, recipient, home_name, channel):
    import json

    from agent_py_agent.agent.assets.delivery_service import AssetDeliveryService

    seed_delivery_bundle(kit)
    sent = kit.service.send_bundle('local/main', recipient, 'workflow', 'flow')
    assert sent['notification_status'] == 'queued'
    path = getattr(kit, home_name) / 'data/asset_delivery/notifications' / (sent['delivery_id'] + '.json')
    event = json.loads(path.read_bytes())
    assert event == sent['notification_event']
    assert event['schema_version'] == 'asset_delivery_notification.v1'
    assert event['delivery_id'] == sent['delivery_id']
    assert event['sender_owner_id'] == 'local/main' and event['recipient_owner_id'] == recipient
    assert event['status'] == 'queued' and event['channel'] == channel
    assert event['created_at'] and event['idempotency_key']
    result = AssetDeliveryService(kit.root).list_inbox(recipient)
    assert result['deliveries'][0]['notification_status'] == 'queued'
    assert result['deliveries'][0]['notification_event'] == event


def test_notification_write_failure_does_not_report_send_success(kit, monkeypatch):
    seed_delivery_bundle(kit)
    original = kit.service._put_json

    def fail_notification(home, parts, value, admission):
        if tuple(parts[:3]) == ('data', 'asset_delivery', 'notifications'):
            raise OSError('synthetic notification fault')
        return original(home, parts, value, admission)

    monkeypatch.setattr(kit.service, '_put_json', fail_notification)
    with pytest.raises(OSError, match='synthetic notification fault'):
        kit.service.send_bundle('local/main', 'local/alice', 'workflow', 'flow')
    assert kit.service.list_inbox('local/alice')['deliveries'] == []
    assert not list((kit.main / 'data/asset_delivery/outbox').glob('*.json'))


def test_configured_encrypted_datasource_secret_is_also_scanned(kit):
    from agent_py_agent.agent.assets.datasource_store import DatasourceStore
    from agent_py_agent.agent.assets.delivery_contracts import AssetDeliveryError

    secret = 'FixtureVaultPrivateValue_77336'
    DatasourceStore(kit.main)._save_secret({'custom_field': secret})
    ScriptStore(kit.main).save_script(AssetDescriptor('vault', 'script', '合成密文凭据'),
        'value = ' + repr(secret) + '\nprint("{}")')
    with pytest.raises(AssetDeliveryError) as error:
        kit.service.preview_bundle('local/main', 'script', 'vault')
    assert error.value.error_code == 'ASSET_DELIVERY_SECRET_DETECTED'
    assert secret not in str(error.value.to_dict())


def test_configured_secret_explicit_replacement_cleans_payload_and_note(kit):
    import json

    secret = 'FixturePrivateDbValue_998877'
    path = kit.main / 'data/datasource_secrets/cred-1.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'fields': {'custom_field': secret}}))
    ScriptStore(kit.main).save_script(AssetDescriptor('s', 'script', '合成'),
        'value = ' + repr(secret) + '\nprint("{}")')
    sent = kit.service.send_bundle('local/main', 'local/alice', 'script', 's', note=secret,
        allow_secret_placeholder_replace=True)
    kit.service.accept_delivery('local/alice', sent['delivery_id'])
    assert secret not in ScriptStore(kit.alice).get_script('s')['code']
    envelope = kit.service.list_inbox('local/alice')['deliveries'][0]
    assert secret not in str(envelope) and envelope['note'] == '${REDACTED_SECRET}'


def test_datasource_secret_symlink_is_rejected_without_reading_target(kit):
    from agent_py_agent.agent.assets.delivery_contracts import AssetDeliveryError

    seed_delivery_bundle(kit)
    directory = kit.main / 'data/datasource_secrets'
    directory.mkdir(parents=True)
    (directory / 'bad.json').symlink_to(kit.alice / 'unreadable-fixture.json')
    with pytest.raises(AssetDeliveryError) as error:
        kit.service.send_bundle('local/main', 'local/alice', 'workflow', 'flow')
    assert error.value.error_code == 'ASSET_DELIVERY_SECRET_SCAN_FAILED'
    assert kit.service.list_inbox('local/alice')['deliveries'] == []


@pytest.mark.parametrize('key', ['api_key', 'secret', 'token', 'password', 'passwd',
    'private_key', 'access_key', 'client_secret'])
@pytest.mark.parametrize('literal', ['x', 'four', 'a1b2c3d', '[REDACTED]', '秘密'])
@pytest.mark.parametrize('json_key', [False, True])
def test_short_literal_secret_assignments_are_rejected(sanitize, tmp_path, key, literal, json_key):
    from agent_py_agent.agent.assets.delivery_contracts import AssetDeliveryError

    text = f'"{key}": "{literal}"' if json_key else f'{key} = "{literal}"'
    with pytest.raises(AssetDeliveryError) as error:
        sanitize({'script/s/main.py': text.encode()}, owner_home=tmp_path)
    assert error.value.error_code == 'ASSET_DELIVERY_SECRET_DETECTED'
    assert error.value.details['redacted_matches'][0]['masked_snippet'] == '<redacted>'
    assert 'text' not in error.value.to_dict() and 'value' not in error.value.to_dict()


@pytest.mark.parametrize('literal', ['', '${PASSWORD}', '<PASSWORD>', 'YOUR_PASSWORD', 'CHANGE_ME'])
def test_short_literal_secret_templates_remain_allowed(sanitize, tmp_path, literal):
    raw = f'password = "{literal}"'.encode()
    assert sanitize({'script/s/main.py': raw}, owner_home=tmp_path)['script/s/main.py'] == raw


def test_secret_after_many_templates_is_not_hidden_by_diagnostic_limit(sanitize, tmp_path):
    from agent_py_agent.agent.assets.delivery_contracts import AssetDeliveryError

    raw = ('password = "${PASSWORD}"\n' * 101 + 'password = "x"\n').encode()
    with pytest.raises(AssetDeliveryError) as error:
        sanitize({'script/s/main.py': raw}, owner_home=tmp_path)
    assert error.value.error_code == 'ASSET_DELIVERY_SECRET_DETECTED'
    assert error.value.details['redacted_matches'][0]['line_number'] == 102


def test_preparation_preserves_adapter_dependency_errors(kit, monkeypatch):
    from agent_py_agent.agent.assets import script_store_support as support
    from agent_py_agent.agent.assets.delivery_contracts import AssetDeliveryError

    def missing(*args, **kwargs):
        raise topology.AssetClosureError(['script:absent'])

    monkeypatch.setattr(support, 'delivery_conflict_mapping', missing)
    with pytest.raises(AssetDeliveryError) as error:
        support.prepare_delivery_items(kit.alice, {}, {}, 'skip', False, 'fixture')
    assert error.value.error_code == 'ASSET_DELIVERY_DEPENDENCY_MISSING'
    assert error.value.details['missing_refs'] == error.value.missing_refs == ['script:absent']


def test_inbox_rename_failure_removes_queued_notification(kit, monkeypatch):
    seed_delivery_bundle(kit)

    def fail_publish(*args):
        raise OSError('synthetic inbox publication failure')

    monkeypatch.setattr(kit.service, '_publish_stage', fail_publish)
    with pytest.raises(OSError, match='synthetic inbox publication failure'):
        kit.service.send_bundle('local/main', 'providers/feishu/users/ou_fixture', 'workflow', 'flow')
    assert kit.service.list_inbox('providers/feishu/users/ou_fixture')['deliveries'] == []
    assert not list((kit.feishu / 'data/asset_delivery/notifications').glob('*.json'))


def test_inbox_notification_tracks_consumed_status_without_writes(kit):
    import json

    seed_delivery_bundle(kit)
    sent = kit.service.send_bundle('local/main', 'local/alice', 'workflow', 'flow')
    path = kit.alice / 'data/asset_delivery/notifications' / (sent['delivery_id'] + '.json')
    event = json.loads(path.read_bytes())
    event['status'] = 'delivered'
    path.write_text(json.dumps(event))
    before = path.read_bytes()
    row = kit.service.list_inbox('local/alice')['deliveries'][0]
    assert row['notification_status'] == 'delivered'
    assert row['notification_event'] == event and path.read_bytes() == before


@pytest.mark.parametrize('replace_secrets', [False, True])
def test_known_secret_resource_filename_never_leaves_sender(kit, replace_secrets):
    import json

    from agent_py_agent.agent.assets.delivery_contracts import AssetDeliveryError

    secret = 'FixturePathPrivateValue_7753'
    vault = kit.main / 'data/datasource_secrets/cred.json'
    vault.parent.mkdir(parents=True)
    vault.write_text(json.dumps({'custom_field': secret}))
    ScriptStore(kit.main).save_script(AssetDescriptor('safe', 'script', '安全正文'), 'print("{}")')
    (kit.main / 'scripts/safe' / (secret + '.txt')).write_text('harmless fixture')
    with pytest.raises(AssetDeliveryError) as error:
        kit.service.send_bundle('local/main', 'local/alice', 'script', 'safe',
            allow_secret_placeholder_replace=replace_secrets)
    assert error.value.error_code == 'ASSET_DELIVERY_SECRET_DETECTED'
    assert secret not in str(error.value.to_dict())
    assert kit.service.list_inbox('local/alice')['deliveries'] == []


@pytest.mark.parametrize('after_inbox', [False, True])
def test_notification_crash_is_not_consumable_and_reconciles_from_inbox(kit, after_inbox):
    import json
    import subprocess
    import sys
    from pathlib import Path

    seed_delivery_bundle(kit)
    code = '''
import os, sys
from agent_py_agent.agent.assets.delivery_service import AssetDeliveryService
service = AssetDeliveryService(sys.argv[1])
if sys.argv[2] == 'before':
    service._publish_stage = lambda *args: os._exit(73)
else:
    put = service._put_json
    def interrupt_promotion(home, parts, value, admission):
        if tuple(parts[:3]) == ('data', 'asset_delivery', 'notifications') and value.get('status') == 'queued':
            os._exit(73)
        return put(home, parts, value, admission)
    service._put_json = interrupt_promotion
service.send_bundle('local/main', 'local/alice', 'workflow', 'flow')
'''
    result = subprocess.run([sys.executable, '-c', code, str(kit.root), 'after' if after_inbox else 'before'],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, timeout=30)
    assert result.returncode == 73
    queue = kit.alice / 'data/asset_delivery/notifications'
    orphan = next(queue.glob('*.json'))
    assert json.loads(orphan.read_bytes())['status'] == 'prepared'
    rows = kit.service.list_inbox('local/alice')['deliveries']
    if after_inbox:
        assert len(rows) == 1 and rows[0]['notification_status'] == 'prepared'
    else:
        assert rows == []
    sent = kit.service.send_bundle('local/main', 'local/alice', 'workflow', 'flow')
    assert sent['notification_status'] == 'queued'
    if after_inbox:
        assert json.loads(orphan.read_bytes())['status'] == 'queued'
        assert len(list(queue.glob('*.json'))) == 2
    else:
        assert not orphan.exists() and len(list(queue.glob('*.json'))) == 1


def test_publish_target_open_failure_closes_source_directory_fd(kit, monkeypatch):
    import errno
    import os

    from agent_py_agent.agent.assets import delivery_service

    original = delivery_service.open_directory_beneath
    opened = []

    def fail_target(*args, **kwargs):
        if opened:
            raise OSError('synthetic target open failure')
        descriptor = original(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(delivery_service, 'open_directory_beneath', fail_target)
    try:
        with pytest.raises(OSError, match='synthetic target open failure'):
            kit.service._publish_stage(kit.alice.relative_to(kit.root).parts, ('fixture-stage',), ('fixture-inbox',), 'fixture')
        with pytest.raises(OSError) as error:
            os.fstat(opened[0])
        assert error.value.errno == errno.EBADF
    finally:
        if opened:
            try:
                os.close(opened[0])
            except OSError:
                pass


@pytest.mark.parametrize(
    "sample",
    [
        "-----BEGIN PGP " + "PRIVATE KEY BLOCK-----\nPRIVATE_BODY_PAYLOAD_XYZ987\n-----END PGP PRIVATE KEY BLOCK-----",
        "-----BEGIN RSA " + "PRIVATE KEY-----\nPRIVATE_BODY_PAYLOAD_XYZ987\n-----END RSA PRIVATE KEY-----",
        "-----BEGIN OPENSSH " + "PRIVATE KEY-----\nPRIVATE_BODY_PAYLOAD_XYZ987\n-----END OPENSSH PRIVATE KEY-----",
        "ASIA" + "IOSFODNN7EXAMPLE",
        "sk_" + "live_4eC39HqLyjWDarjtT1zdp7dc",
        "rk_" + "live_4eC39HqLyjWDarjtT1zdp7dc",
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        + "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        + "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    ],
)
def test_h01_h02_secret_shapes_blocked_in_delivery_and_safe_projection(sample, tmp_path):
    from agent_py_agent.agent.assets.delivery_sanitizer import _shared_scan, sanitize_bundle_files
    from agent_py_agent.agent.audit_group.safe_projection import public_value

    changed, replaced = _shared_scan(f"note={sample}")
    assert changed is True
    assert sample not in replaced
    assert "PRIVATE_BODY_PAYLOAD_XYZ987" not in replaced
    cleaned = sanitize_bundle_files(
        {"note.txt": f"prefix\n{sample}\nsuffix".encode()},
        owner_home=tmp_path,
        allow_secret_placeholder_replace=True,
    )["note.txt"].decode()
    assert sample not in cleaned
    assert "PRIVATE_BODY_PAYLOAD_XYZ987" not in cleaned
    projected = public_value({"message": f"leak={sample}"})
    assert sample not in projected["message"]
    assert "PRIVATE_BODY_PAYLOAD_XYZ987" not in projected["message"]
