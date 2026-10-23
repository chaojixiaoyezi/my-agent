# LLM: Pin the 529f4dff input contracts; descriptions may shrink without changing ownership or validation.
# 模块用途: 核对上下文瘦身后的工具参数、审计口径、错误分类和温度校验。
import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration.tool_specs import (
    build_create_subagents_model_spec,
    build_task_progress_model_spec,
)
from agent_py_agent.agent.capability.memory_tool import build_remember_model_spec
from agent_py_agent.agent.capability.skill_search_tool import build_skill_search_model_spec
from agent_py_agent.agent.contracts.error_taxonomy import classify_error, error_contract
from agent_py_agent.agent.conversation.goal_tools import UpdateGoalTool
from agent_py_agent.agent.ingestion.watch_tool_spec import build_watch_stream_model_spec
from agent_py_agent.agent.settings.model_provider_schema import ModelProfileError, validate_model
from agent_py_agent.agent.settings.services import _normalize
from agent_py_agent.agent.tooling import audit_records_tool as audit
from agent_py_agent.agent.tooling.package_build_tool import PackageBuildTool
from agent_py_agent.agent.tooling.package_install_tool import PackageInstallTool
from agent_py_agent.agent.tooling.task_credential_tool import RequestCredentialsTool
from agent_py_agent.tests.ctxopt_c7_contract_snapshot import (
    DESCRIPTION_HASHES as _DESCRIPTION_HASHES,
)
from agent_py_agent.tests.ctxopt_c7_contract_snapshot import (
    SCHEMA_HASHES as _SCHEMA_HASHES,
)


# LLM: Only instantiate metadata; never execute tools or initialize an owner.
# 函数用途: 收集本次涉及的原生工具契约。
def _specs():
    specs = [build_create_subagents_model_spec(), build_task_progress_model_spec(),
             build_skill_search_model_spec(), build_remember_model_spec(),
             audit.AuditRecordsTool.model_spec, RequestCredentialsTool(None).model_spec,
             UpdateGoalTool.model_spec, PackageBuildTool.model_spec, PackageInstallTool.model_spec]
    result = {spec.name: spec for spec in specs}
    result['create_subagents_media'] = build_create_subagents_model_spec(input_media=True)
    for surface in ('ordinary', 'audit_prepare', 'audit_binding', 'audit_source', 'audit_coordinator'):
        result['watch_' + surface] = build_watch_stream_model_spec(surface=surface)
    return result


# LLM: Canonical JSON includes descriptions as well as nested validation fields.
# 函数用途: 计算可复核的契约快照摘要。
def _sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


# LLM: Only TOOL-10's two ordinary parameter descriptions are allowed to change.
# 函数用途: 排除明示可改文案，保留所有动作和嵌套参数约束作比较。
def _ordinary_schema(schema):
    schema = deepcopy(schema)
    for name in ('action', 'source_adapter'):
        schema['properties'][name].pop('description', None)
    return schema


# LLM: Constants below were captured before editing the pristine 529f4dff tree.
# 函数用途: 防止描述调整偷偷改动任何七项工具输入schema或Audit表面。
@pytest.mark.parametrize('name', [
    'create_subagents', 'task_progress', 'skill_search', 'remember', 'audit_records',
    'request_credentials', 'update_goal', 'package_build', 'package_install',
    'create_subagents_media', 'watch_ordinary', 'watch_audit_prepare',
    'watch_audit_binding', 'watch_audit_source', 'watch_audit_coordinator',
])
def test_input_contract_is_unchanged(name):
    schema = _specs()[name].input_schema
    if name == 'watch_ordinary':
        schema = _ordinary_schema(schema)
    assert _sha(schema) == _SCHEMA_HASHES[name]


# LLM: Check exact reviewed candidates without retaining the former long prose.
# 函数用途: 验证七个描述确实替换为任务给出的候选版本。
@pytest.mark.parametrize('name', ['create_subagents', 'task_progress', 'skill_search',
                                 'remember', 'audit_records', 'request_credentials', 'update_goal'])
def test_description_matches_candidate(name):
    assert _sha(_specs()[name].description) == _DESCRIPTION_HASHES[name]


# LLM: Both packaging metadata surfaces must route ordinary skills and native automation elsewhere.
# 函数用途: 验证打包安装说明及提示都保留用户明确要求和产品边界。
@pytest.mark.parametrize('tool', [PackageBuildTool, PackageInstallTool])
def test_packaging_is_explicit_and_not_a_skill_or_workflow(tool):
    spec = tool.model_spec
    for text in (spec.description, ' '.join(spec.hints.avoid_when)):
        assert '用户明确' in text
        for token in ('CAPABILITY.md', 'SKILL.md', 'workflows', 'scripts'):
            assert token in text


# LLM: Ordinary text stays compact; published actions remain unchanged even on the older baseline.
# 函数用途: 防止普通盯守继续携带整段Audit保证档说明，审计表面则保留原约束。
def test_watch_descriptions_are_surface_specific():
    ordinary = build_watch_stream_model_spec()
    audit_spec = build_watch_stream_model_spec(surface='audit_binding')
    adapter = ordinary.input_schema['properties']['source_adapter']['description']
    assert '命名 Audit' not in adapter and 'sha256' in adapter
    assert len(adapter) < 350
    assert len(ordinary.description) < 250
    assert '每条完整记录' in audit_spec.description
    assert 'verdict_token' in audit_spec.input_schema['properties']['verdicts']['description']
    assert '保证档' not in ordinary.description


# LLM: Topic collectors must supply definitions even when the source context is unavailable.
# 函数用途: 核对审计字段解释从描述迁到真实回执，而不是消失。
@pytest.mark.parametrize('topic', ['decision', 'requests'])
def test_audit_topics_return_field_glossary(monkeypatch, topic):
    monkeypatch.setattr(audit, '_thread_ids', lambda store: ([], 0))
    monkeypatch.setattr(audit, '_observations', lambda *args: {'available': False})
    monkeypatch.setattr(audit, '_decision_owner_report', lambda owner, *args: {'owner_id': owner})
    from agent_py_agent.agent.tooling import audit_requests_topic
    monkeypatch.setattr(audit_requests_topic, 'requests_topic', lambda *args, **kw: {'available': False})
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_id='alice'), conversation_store=None)
    query = audit.AuditQuery(topic, 'owner', '', 0, 10)
    result = audit._TOPIC_COLLECTORS[topic](agent, query)
    glossary = result.get('field_glossary', {})
    assert 'send_intent_uncertain' in glossary
    assert 'usage' in glossary['send_intent_uncertain']
    if topic == 'decision':
        for key in ('jev_failures', 'not_sent_calls', 'input_tokens_estimated_unfinished',
                    'points.not_sent', 'status=skipped', 'reason=privacy_url',
                    'point_diagnostics.covered', 'not_called.label'):
            assert key in glossary
        assert 'HTTP' in glossary['not_sent_calls']
        assert '不是供应商回报' in glossary['input_tokens_estimated_unfinished']
        assert '不要对比或相加' in glossary['points.not_sent']
        assert '不要说成 0 次' in glossary['point_diagnostics.covered']
    glossary['send_intent_uncertain'] = 'tampered'
    assert audit._TOPIC_COLLECTORS[topic](agent, query)['field_glossary']['send_intent_uncertain'] != 'tampered'


# LLM: These deterministic denials require user/host intervention, never blind retry.
# 函数用途: 保证四个真实错误码具有准确类别与非重试恢复提示。
@pytest.mark.parametrize('code,category,fragment', [
    ('AGENTS_WRITE_USER_ONLY', 'permission', '/agents'),
    ('AGENTS_SOURCE_UNAVAILABLE', 'state', '来源'),
    ('AUDIT_CONSUMER_CONTEXT_UNAVAILABLE', 'state', '来源'),
    ('NETWORK_DISABLED', 'permission', '网络'),
])
def test_error_codes_are_known_non_retryable(code, category, fragment):
    result = error_contract(code)
    assert result.code == code
    assert result.category == category
    assert result.retryable is False
    assert fragment in result.recovery_hint
    assert classify_error('error_code=' + code).code == code


# LLM: Config keeps warning+fallback while model profile rejects the same booleans.
# 函数用途: 复现原配置归一化把布尔值当成温度数字的缺陷。
@pytest.mark.parametrize('value', [True, False])
def test_temperature_booleans_are_rejected_consistently(value):
    out = {'temperature': value}
    warnings = _normalize._normalize_temperature(out, SimpleNamespace(temperature=None))
    assert out['temperature'] is None
    assert len(warnings) == 1 and 'temperature' in warnings[0]
    with pytest.raises(ModelProfileError, match='温度'):
        validate_model(_profile(value))


# LLM: Keep config string formatting and profile canonical float formatting separately.
# 函数用途: 验证共用校验后两种既有输出格式、空值和非法值仍各守原合同。
@pytest.mark.parametrize('value,config,profile', [(0, '0.0', '0.0'), (2, '2.0', '2.0'),
                                                (' 0.70 ', '0.70', '0.7')])
def test_valid_temperature_keeps_caller_format(value, config, profile):
    out = {'temperature': value}
    assert _normalize._normalize_temperature(out, SimpleNamespace(temperature=None)) == []
    assert out['temperature'] == config
    assert validate_model(_profile(value))['temperature'] == profile


# LLM: Invalid numerics must not leak into sampling or become config failures.
# 函数用途: 覆盖范围、非有限数、对象和字符串的失败路径。
@pytest.mark.parametrize('value', [-1, 2.01, 'nan', 'inf', 'oops', {}, [1]])
def test_invalid_temperature_warns_or_raises(value):
    out = {'temperature': value}
    assert _normalize._normalize_temperature(out, SimpleNamespace(temperature=None))
    assert out['temperature'] is None
    with pytest.raises(ModelProfileError):
        validate_model(_profile(value))


# LLM: The profile helper input has no secret and exercises the public validator.
# 函数用途: 为温度校验构造最小合法模型档案。
def _profile(temperature):
    return {'provider_id': 'demo', 'model_name': 'demo', 'model_backend': 'openai_compatible',
            'model_context_window_tokens': 4096, 'temperature': temperature}
