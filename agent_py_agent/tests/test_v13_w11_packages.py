"""W11 能力包与合规治理：真实 Gateway HTTP、原安装表与 v13 证据集合的端到端回归。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agent_py_agent.agent.business_spaces.v13_store import V13Store
from agent_py_agent.agent.capability.package_build import build_capability_pack
from agent_py_agent.agent.gateway_parts.business_api_v13 import v13_action_specs
from agent_py_agent.agent.plugin_host.business_action_specs import business_action_specs
from agent_py_agent.agent.plugin_management import PluginManagement, plugin_management_context
from agent_py_agent.agent.user_space.owner_access import is_complete_local_admin_owner
from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
from agent_py_agent.cli.chat_client_context import owner_identity_from_config
from agent_py_agent.tests.test_mega_business_api_actions import native_request

pytest_plugins = ('agent_py_agent.tests.test_business_v12_f9_e2e',)

_ENVELOPE_KEYS = {'request_id', 'operation_id', 'state', 'data', 'error', 'allowed_actions',
                  'warnings', 'effect_outcome'}
_PACKAGE_ID = 'soc-core-detections-pack'


# LLM: 合成包只走宿主打包器生成合法字节，不手写 ZIP；语言/内容均为中性测试数据。
# 函数用途: 造一个带规则、流程、脚本、技能与合规声明的能力包。
def _package(package_id, version, *, controls=None):
    contents = {'README.md': b'# synthetic pack\n',
                'rules/ssh-brute.json': b'{"rule": "ssh-brute"}\n',
                'workflows/contain-host.json': b'{"workflow": "contain"}\n',
                'scripts/collect-evidence.sh': b'#!/bin/sh\nexit 0\n',
                'skills/triage/SKILL.md': b'# triage\n'}
    for control_id, assets in (controls or {'CC6.1': [('rule', 'rule-1')]}).items():
        contents['compliance/soc2/' + control_id + '.json'] = json.dumps(
            {'title': 'control ' + control_id, 'family': 'CC6',
             'assets': [{'asset_kind': kind, 'asset_ref': ref} for kind, ref in assets]}).encode()
    declaration = {'plugin_id': package_id, 'version': version, 'summary': 'synthetic package',
        'capability': {'description': 'synthetic detection pack', 'keywords': ['soc'],
                       'entry_document': 'README.md'},
        'files': [{'path': path} for path in contents], 'settings_schema': {}}
    return build_capability_pack(declaration, contents).payload


# LLM: 本地安装直接走 PluginManagement.install_package_bytes，沿用真实 owner 安装表。
# 函数用途: 通过宿主本地包安装端口安装合成能力包。
def _install_local(agent, raw, *, operation_id, expected_revision=0):
    owner = resolve_owner_home(agent.home_paths.root, owner_identity_from_config(agent.config))
    context = plugin_management_context(
        owner,
        agent.home_paths,
        agent.config,
        agent.conversation_store.threads,
        actor_id='w11-test',
        channel='http',
        conversation_id=operation_id,
        is_admin=is_complete_local_admin_owner(agent.home_paths),
    )
    return PluginManagement(context).install_package_bytes(
        raw, operation_id=operation_id, expected_revision=expected_revision
    )


# LLM: 使用隔离真实 Gateway，不绑定远程包源，仅测试本地已安装包查验、卸载与合规。
# 函数用途: 提供启用本地业务网关的测试夹具。
@pytest.fixture
def packages(native_http):
    server, agent = native_http
    return server, agent


# LLM: 调用只经原 HTTP 与宿主凭据，身份不从 JSON 携带；base 通道用于本机管理员宿主。
# 函数用途: 发一笔 /client/business 请求并返回状态与标准信封。
def _call(server, action, params=None, *, key=None, revision=None):
    request = {'action': action, 'request_id': 'w11-' + action, 'params': params or {}}
    if key is not None:
        request['idempotency_key'] = key
    if revision is not None:
        request['expected_revision'] = revision
    status, _headers, raw = native_request((server, None), 'POST', '/client/business', request,
        headers={'X-Channel': 'http'})
    return status, json.loads(raw)


# LLM: 结果凡声明 schema 就按同一规范校验；冻结动作没有 v13 登记时只读标准信封。
# 函数用途: 断言完成信封并返回结果。
def _completed(server, action, params=None, *, key=None, revision=None):
    status, body = _call(server, action, params, key=key, revision=revision)
    assert status == 200, json.dumps(body, ensure_ascii=False)
    assert body['state'] == 'completed' and body['error'] is None, json.dumps(body, ensure_ascii=False)
    specs = v13_action_specs()
    if action in specs:
        return specs[action].validate_result(body['data'])
    return body['data']


# LLM: 失败响应只在同一标准信封里报告，data 为空对象；不据 HTTP 文本猜原因。
# 函数用途: 断言失败信封的准确状态与错误码。
def _failed(server, action, params, code, status_code, *, key=None, revision=None):
    status, body = _call(server, action, params, key=key, revision=revision)
    assert status == status_code, json.dumps(body, ensure_ascii=False)
    assert body['state'] == 'failed' and body['data'] == {}, json.dumps(body, ensure_ascii=False)
    assert body['error']['code'] == code, json.dumps(body, ensure_ascii=False)
    return body


# LLM: 空间由真实 W1 入口创建，不把虚构 ID 塞给安装或导出动作。
# 函数用途: 创建一个本机业务空间。
def _space(server, key):
    data = _completed(server, 'spaces.create', {'name': 'w11 空间'}, key=key)
    return data['space_id'], data['revision']


# LLM: 安装表是产品文件，测试只读原文并检查包是否仍在账上，不手写安装状态。
# 函数用途: 读取当前 owner 的安装表原文。
def _installations_text(agent):
    path = Path(agent.home_paths.owner_home_dir) / 'data' / 'plugins' / 'installations.json'
    assert path.is_file(), path
    return path.read_text(encoding='utf-8')


# LLM: 证据集合写入走宿主 Store，不手写状态文件；记录形状使用冻结集合定义。
# 函数用途: 在 v13 证据集合中种入一条合规证据。
def _seed_evidence(agent, rows):
    store = V13Store(agent.home_paths.owner_home_dir, owner_id=agent.home_paths.owner_id)

    # LLM: 一次事务写入全部合成证据。
    # 函数用途: 合并给定证据记录并返回数量。
    def mutate(current):
        current.update(rows)
        return {'seeded': len(rows)}

    return store.transaction('compliance_evidence', mutate,
        idempotency_key='seed-evidence-' + str(len(rows)), params_digest='seed')


# LLM: 规则事实来自 W7 集合，测试只种入冻结字段形状。
# 函数用途: 种入一条监控规则记录。
def _seed_rule(agent, rule_id, *, enabled):
    store = V13Store(agent.home_paths.owner_home_dir, owner_id=agent.home_paths.owner_id)

    # LLM: 规则只保留测试关心的结构化字段。
    # 函数用途: 写入合成规则。
    def mutate(current):
        current[rule_id] = {'rule_id': rule_id, 'space_id': None, 'name': rule_id,
                            'description': '', 'enabled': enabled, 'severity': 'medium',
                            'query': {'language': 'text', 'text': 'x'}, 'schedule': None,
                            'script_id': None, 'skill_ids': [], 'datasource_ids': [],
                            'mitre_techniques': [], 'tags': [], 'exclusions': [],
                            'origin': 'manual', 'scheduler_job_id': None, 'last_run': None,
                            'revision': 0, 'created_at': '2026-11-02T00:00:00+00:00',
                            'updated_at': '2026-11-02T00:00:00+00:00',
                            'owner_id': agent.home_paths.owner_id}
        return {'seeded': 1}

    return store.transaction('monitor_rules', mutate,
        idempotency_key='seed-rule-' + rule_id, params_digest='seed')


# LLM: 已安装包的内容清单来自原安装表字节的静态检查，不执行任何随包文件；未安装包返回 404。
# 函数用途: 验证 packages.inspect 返回规则/流程/脚本/技能与合规映射。
def test_inspect_lists_rules_workflows_scripts_skills(packages):
    server, agent = packages
    _install_local(agent, _package(_PACKAGE_ID, '1.0.0'), operation_id='w11-inspect-install')
    data = _completed(server, 'packages.inspect', {'package_id': _PACKAGE_ID,
                                                   'version': '1.0.0'})
    assert data['installed'] is True and data['version'] == '1.0.0'
    assert data['manifest']['plugin_id'] == _PACKAGE_ID
    contents = data['contents']
    assert [row['ref'] for row in contents['detection_rules']] == ['rules/ssh-brute.json']
    assert [row['ref'] for row in contents['workflows']] == ['workflows/contain-host.json']
    assert [row['ref'] for row in contents['scripts']] == ['scripts/collect-evidence.sh']
    assert [row['name'] for row in contents['skills']] == ['triage']
    assert contents['compliance_mappings'] == [{'framework': 'soc2', 'control_id': 'CC6.1',
        'asset_kind': 'rule', 'asset_ref': 'rule-1'}]
    _failed(server, 'packages.inspect', {'package_id': 'unknown-pack', 'version': '1.0.0'},
            'RESOURCE_NOT_FOUND', 404)
    _failed(server, 'packages.inspect', {'package_id': _PACKAGE_ID, 'version': '9.9.9'},
            'RESOURCE_NOT_FOUND', 404)


# LLM: 卸载顺序必须是先摘挂载再走原卸载命令，卸载后同包再请求要 404。
# 函数用途: 验证 packages.uninstall 的摘除与删除事实。
def test_uninstall_detaches_then_removes(packages):
    server, agent = packages
    _install_local(agent, _package(_PACKAGE_ID, '1.0.0'), operation_id='w11-uninstall-install')
    data = _completed(server, 'packages.uninstall', {'package_id': _PACKAGE_ID},
                      key='w11-uninstall-remove')
    assert data['uninstalled'] is True and data['detached_spaces'] == []
    assert _PACKAGE_ID not in _installations_text(agent)
    _failed(server, 'packages.uninstall', {'package_id': _PACKAGE_ID},
            'RESOURCE_NOT_FOUND', 404, key='w11-uninstall-again')


# LLM: 控制项目录只来自已安装包声明，状态与证据计数来自证据集合，不返回猜测状态。
# 函数用途: 验证 compliance.status 的控制项、证据计数与未知框架 404。
def test_compliance_status_from_installed_mappings_and_evidence(packages):
    server, agent = packages
    _install_local(agent, _package(_PACKAGE_ID, '1.0.0'), operation_id='w11-status-install')
    _seed_rule(agent, 'rule-1', enabled=True)
    _completed(server, 'compliance.collect', {'framework': 'soc2'}, key='w11-status-collect')
    _seed_evidence(agent, {'evidence-seeded': {'evidence_id': 'evidence-seeded',
        'owner_id': agent.home_paths.owner_id, 'space_id': None, 'framework': 'soc2',
        'control_id': 'CC6.1', 'status': 'unmet', 'facts': {'assets': []},
        'collected_at': '2026-11-01T00:00:00+00:00', 'revision': 0,
        'created_at': '2026-11-01T00:00:00+00:00', 'updated_at': '2026-11-01T00:00:00+00:00'}})
    data = _completed(server, 'compliance.status', {'framework': 'soc2'})
    controls = {row['control_id']: row for row in data['controls']}
    assert controls['CC6.1']['status'] == 'met' and controls['CC6.1']['evidence_count'] == 2
    assert controls['CC6.1']['last_collected_at'] > '2026-11-01T00:00:00+00:00'
    assert controls['CC6.1']['mapped_assets'] == [{'framework': 'soc2', 'control_id': 'CC6.1',
        'asset_kind': 'rule', 'asset_ref': 'rule-1'}]
    assert data['summary'] == {'met': 1, 'partial': 0, 'unmet': 0, 'unknown': 0}
    _failed(server, 'compliance.status', {'framework': 'iso27001'}, 'RESOURCE_NOT_FOUND', 404)


# LLM: 采集是同一框架全部控制项的一次有界快照；同状态再采集不产生新版本，重放返回原回执。
# 函数用途: 验证 compliance.collect 的状态判定、证据落盘与确定性。
def test_compliance_collect_records_evidence_deterministically(packages):
    server, agent = packages
    source_package = _package(_PACKAGE_ID, '1.0.0',
        controls={'CC6.1': [('rule', 'rule-1')], 'CC6.2': [('rule', 'rule-2')],
                  'CC6.3': [('script', 'collect-evidence.sh')]})
    _install_local(agent, source_package, operation_id='w11-collect-install')
    _seed_rule(agent, 'rule-1', enabled=True)
    _seed_rule(agent, 'rule-2', enabled=False)
    first = _completed(server, 'compliance.collect', {'framework': 'soc2'},
                       key='w11-collect-first')
    assert first['collected'] == 3 and first['controls_updated'] == 3
    assert first['coverage'] == 'complete'
    replay = _completed(server, 'compliance.collect', {'framework': 'soc2'},
                        key='w11-collect-first')
    assert replay == first
    again = _completed(server, 'compliance.collect', {'framework': 'soc2'},
                       key='w11-collect-second')
    assert again['controls_updated'] == 0
    store = V13Store(agent.home_paths.owner_home_dir, owner_id=agent.home_paths.owner_id)
    rows = {row['control_id']: row for row in store.list('compliance_evidence')}
    assert rows['CC6.1']['status'] == 'met'
    assert rows['CC6.2']['status'] == 'unmet'
    assert rows['CC6.3']['status'] == 'unknown'


# LLM: 导出必须真的登记到原产物账本并通过摘要核对；三种格式互不冒充。
# 函数用途: 验证 compliance.export 的产物登记与内容摘要。
def test_compliance_export_artifact(packages):
    server, agent = packages
    space_id, _revision = _space(server, 'w11-export-space-key')
    _install_local(agent, _package(_PACKAGE_ID, '1.0.0'), operation_id='w11-export-install')
    _seed_rule(agent, 'rule-1', enabled=True)
    _completed(server, 'compliance.collect', {'framework': 'soc2'}, key='w11-export-collect')
    data = _completed(server, 'compliance.export', {'framework': 'soc2', 'format': 'json',
        'space_id': space_id}, key='w11-export-json-01')
    export = data['export']
    assert export['format'] == 'json' and export['name'].endswith('.json')
    root = Path(agent.home_paths.owner_home_dir) / 'artifacts' / 'business'
    blob = next(path for path in root.iterdir() if path.name.endswith('.blob'))
    payload = blob.read_bytes()
    assert len(payload) == export['size_bytes']
    assert hashlib.sha256(payload).hexdigest() == export['content_sha256']
    assert json.loads(payload)['framework'] == 'soc2'
    csv_export = _completed(server, 'compliance.export', {'framework': 'soc2', 'format': 'csv',
        'space_id': space_id}, key='w11-export-csv-01')['export']
    assert csv_export['format'] == 'csv' and csv_export['content_sha256'] != export['content_sha256']


# LLM: #74-9 只冻结映射，不改原动作名；v13 不能覆盖已有 spaces.packages.*。
# 函数用途: 验证冻结包动作名仍存在且未被扩展覆盖。
def test_frozen_package_names_exist():
    specs = business_action_specs()
    for name in ('spaces.packages.catalog', 'spaces.packages.list', 'spaces.packages.attach',
                 'spaces.packages.detach', 'spaces.packages.set', 'spaces.packages.reorder'):
        assert name in specs, name
    v13_specs = v13_action_specs()
    assert not [name for name in v13_specs if name.startswith('spaces.packages.')]
    for retired in ('packages.index', 'packages.install', 'packages.upgrade'):
        assert retired not in v13_specs
    for kept in ('packages.inspect', 'packages.uninstall'):
        assert kept in v13_specs


# LLM: 200/202 只用于完成或受理，error 必须为空；信封字段集合固定，不许夹带私有路径。
# 函数用途: 验证能力包与合规动作的标准信封形状。
def test_envelopes(packages):
    server, agent = packages
    _install_local(agent, _package(_PACKAGE_ID, '1.0.0'), operation_id='w11-envelope-install')
    for action, params in (('packages.inspect', {'package_id': _PACKAGE_ID}),
                           ('compliance.status', {'framework': 'soc2'})):
        status, body = _call(server, action, params)
        assert status == 200, json.dumps(body, ensure_ascii=False)
        assert set(body) == _ENVELOPE_KEYS, json.dumps(body, ensure_ascii=False)
        assert body['state'] == 'completed' and body['error'] is None
        assert body['effect_outcome'] == 'none'
        assert isinstance(body['allowed_actions'], list)


# LLM: 重放必须先消费耐久回执，不可在卸载后 404 处分叉。
# 函数用途: 验证卸载的原键原参数成功重放。
def test_package_uninstall_replay_original_receipts(packages):
    server, agent = packages
    _install_local(agent, _package(_PACKAGE_ID, '1.0.0'), operation_id='w11-replay-install')
    removed = _completed(server, 'packages.uninstall', {'package_id': _PACKAGE_ID},
                         key='w11-replay-uninstall')
    assert _completed(server, 'packages.uninstall', {'package_id': _PACKAGE_ID},
                      key='w11-replay-uninstall') == removed
