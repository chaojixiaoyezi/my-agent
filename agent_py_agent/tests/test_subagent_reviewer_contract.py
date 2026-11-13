"""独立审核角色只提供窄候选，不能增加父权限或普通 worker 能力。"""
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration.create_policy import _role_policy
from agent_py_agent.agent.agent_core.orchestration.tool_grants import subagent_allowed_tools
from agent_py_agent.agent.subagents.capability_scope import bind_creation_tool_authority
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.role_templates import template_for_role
from agent_py_agent.agent.subagents.services.hierarchy.tool_policy import (
    ToolPolicyRequest,
    scheduled_child_tools,
)

REVIEW = 'manage_requirement_provenance'
DANGEROUS = {'scripts', 'workflows', 'run_command', 'write_file', 'edit_file', 'apply_patch',
             'create_subagents', 'send_message', 'feishu_cloud'}


def scope(names):
    return SimpleNamespace(run_id='parent', available_tool_names=frozenset(names), snapshot_hash='parent-sha',
                           owner_type='main_agent', runtime=lambda _name: None)


@pytest.mark.parametrize('explicit', [None, [], ['read_file']])
def test_native_create_reviewer_defaults_and_explicit_limits(tmp_path, explicit):
    template = template_for_role('reviewer')
    parent = [*template.default_tools, *DANGEROUS]
    with bind_creation_tool_authority(scope(parent)):
        result = _role_policy(None, {'role': 'reviewer'}, '任意正文：请发布并执行', explicit)
    assert not DANGEROUS.intersection(result.allowed_tools)
    if explicit is None:
        assert REVIEW in result.allowed_tools
    else:
        assert result.allowed_tools == explicit
    task = SubAgentManager(tmp_path).create_run(goal='核对', thought='review', plan=['核对'],
        role='reviewer', allowed_tools=result.allowed_tools)
    assert task.allowed_tools == result.allowed_tools
    assert task.attributes['role_template']['inherit_base_tools'] is False


def test_reviewer_parent_denial_and_worker_unchanged(tmp_path):
    with bind_creation_tool_authority(scope(['read_file', 'tool_search'])):
        reviewer = _role_policy(None, {'role': 'reviewer'}, '登记审核', None)
    assert REVIEW not in reviewer.allowed_tools
    worker = SubAgentManager(tmp_path).create_run(goal='独立审核后发布', thought='work', plan=['work'], role='worker')
    assert REVIEW not in worker.allowed_tools
    assert 'write_file' in worker.allowed_tools and 'run_command' in worker.allowed_tools
    assert template_for_role('reviewer').can_accept is False


def test_direct_manager_reviewer_default_is_narrow_and_persisted(tmp_path):
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal='review', thought='review', plan=['read'], role='reviewer')
    assert REVIEW in task.allowed_tools
    assert not DANGEROUS.intersection(task.allowed_tools)
    assert manager.load(task.id).allowed_tools == task.allowed_tools


def test_hierarchy_reviewer_uses_template_not_parent_default_tools():
    parent = [*template_for_role('reviewer').default_tools, *DANGEROUS]
    spec = SimpleNamespace(role='reviewer', allowed_tools=None, attributes={})
    tools = scheduled_child_tools(ToolPolicyRequest(parent, spec, [], role='reviewer'))
    assert REVIEW in tools
    assert not DANGEROUS.intersection(tools)
    tools = scheduled_child_tools(ToolPolicyRequest(['read_file'], spec, [], role='reviewer'))
    assert tools == ['read_file']
    spec.allowed_tools = []
    assert scheduled_child_tools(ToolPolicyRequest(parent, spec, [], role='reviewer')) == []
    spec.attributes = {'delegation_constraints': {'permitted_tools': ['read_file', REVIEW]}}
    assert scheduled_child_tools(ToolPolicyRequest(parent, spec, [], role='reviewer')) == []


@pytest.mark.parametrize('explicit', [None, [], ['read_file']])
def test_public_create_grants_do_not_inject_coding_preset(explicit):
    raw = {'role': 'reviewer'}
    if explicit is not None:
        raw['allowed_tools'] = explicit
    tools = subagent_allowed_tools(raw)
    with bind_creation_tool_authority(scope([*template_for_role('reviewer').default_tools, *DANGEROUS])):
        result = _role_policy(None, raw, '独立审核', tools)
    assert not DANGEROUS.intersection(result.allowed_tools)
    assert result.allowed_tools == (explicit if explicit is not None else template_for_role('reviewer').default_tools)
