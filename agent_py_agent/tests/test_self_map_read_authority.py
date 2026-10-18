# LLM: 用真实Registry与内置read_file对照F5只读投影；仅访问pytest临时目录，不替产品读门发授权。
# 模块用途: 防止插件cwd范围冒充内置文件读取权限，并守住owner、凭据和精确空根。
# ruff: noqa: F811
import json

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.runtime_context import (
    restore_current_subagent_context,
    set_current_subagent_context,
)
from agent_py_agent.agent.tooling.self_map_registry import SectionDescriptor, SectionProjection
from agent_py_agent.agent.user_space.self_map_paths import _directory_item
from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call
from agent_py_agent.tests.test_self_map_runtime import make_agent  # noqa: F401


# LLM: 测试通过正式执行器冻结本次boundary；故意不把它存入Agent，防止投影误读可变当前参数而假绿。
# 函数用途: 在同一真实Registry上分别读取目录元信息和文件正文，返回原结果供独立断言。
def _pair(agent, target, boundary):
    tool = agent.tools.tools['self_map']
    if 'read_probe' not in {section.id for section in tool.sections.descriptors()}:
        tool.sections.register(SectionDescriptor('read_probe', '读取裁决测试', 999,
            lambda context: SectionProjection((_directory_item('target', target, context),))))
    projected = execute_registry_test_call(agent.tools, 'self_map', {'section': 'read_probe', 'item': 'target'},
        allowed_tools=['self_map', 'read_file'], write_boundary=boundary, call_id='map-read-authority')
    assert projected.ok, projected.output
    actual = execute_registry_test_call(agent.tools, 'read_file', {'path': str(target)},
        allowed_tools=['self_map', 'read_file'], write_boundary=boundary, call_id='actual-read-authority')
    return json.loads(projected.output)['item'], actual


# LLM: 输入与cwd分离且都在合成owner；normal读取不应误用插件cwd上界，写根不参与生成读取授权。
# 函数用途: 准备真实文件门可以访问的本人普通文件，避免只mock同一个错误判断。
def _case(agent):
    owner = agent.home_paths.owner_home_dir
    cwd = owner / 'business' / 'current'
    cwd.mkdir(parents=True, exist_ok=True)
    target = owner / 'artifacts' / 'prior.txt'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('isolated-owner-business', encoding='utf-8')
    return target, {'execution_cwd': str(cwd), 'allowed_write_roots': [str(owner)]}


@pytest.mark.parametrize('kind', ['main', 'user', 'group'])
def test_normal_owner_builtin_read_outside_cwd_matches_projection(make_agent, kind):
    agent = make_agent(kind=kind)
    target, boundary = _case(agent)
    # 当前可变参数与真正执行边界矛盾；投影只能用本次执行器冻结事实。
    agent._current_run_params = RunParams(write_boundary={'read_scope_mode': 'exact', 'allowed_read_roots': []})
    row, actual = _pair(agent, target, boundary)
    assert actual.ok and 'isolated-owner-business' in actual.output
    assert row['read_access'] is True
    assert row['read_access_scope'] == 'builtin_file_tools'
    assert row['workspace_read_access'] is False


@pytest.mark.parametrize('roots,expected', [([], False), (['target'], True), (['cwd'], False)])
def test_exact_builtin_read_preserves_original_empty_and_outside_cwd_roots(make_agent, roots, expected):
    agent = make_agent(kind='user')
    target, boundary = _case(agent)
    values = {'target': str(target), 'cwd': boundary['execution_cwd']}
    boundary |= {'read_scope_mode': 'exact', 'allowed_read_roots': [values[key] for key in roots]}
    row, actual = _pair(agent, target, boundary)
    assert actual.ok is expected
    assert row['read_access'] is expected
    assert row['workspace_read_access'] is False
    if not expected:
        assert actual.error_code == 'TOOL_PERMISSION_DENIED'


def test_child_keeps_same_owner_builtin_read_without_parent_inventory(make_agent):
    agent = make_agent(kind='user')
    target, boundary = _case(agent)
    agent._current_run_params = RunParams(context_scope='task_local', task_attributes={'parent_run_id': 'parent'})
    previous = set_current_subagent_context(agent, run_id='child')
    try:
        row, actual = _pair(agent, target, boundary)
        assert actual.ok and row['read_access'] is True
        listing = execute_registry_test_call(agent.tools, 'self_map', {'section': 'directories', 'limit': 100},
            allowed_tools=['self_map', 'read_file'], write_boundary=boundary)
        assert listing.ok, listing.output
        encoded = json.loads(listing.output)
        assert not {'owner_home', 'artifacts', 'tasks', 'builtin_skills'} & {item['id'] for item in encoded['items']}
    finally:
        restore_current_subagent_context(agent, previous)


def test_read_projection_uses_frozen_external_grant_without_expanding_plugin_cwd(make_agent, tmp_path):
    agent = make_agent(kind='user')
    _, boundary = _case(agent)
    target = tmp_path / 'declared-project' / 'note.txt'
    target.parent.mkdir()
    target.write_text('declared-external-business', encoding='utf-8')
    boundary['allowed_write_roots'].append(str(target.parent))
    boundary['execution_workspace_roots'] = [boundary['execution_cwd'], str(target.parent)]
    row, actual = _pair(agent, target, boundary)
    assert actual.ok, actual.output
    assert 'declared-external-business' in actual.output
    assert row['read_access'] is True and row['workspace_read_access'] is False


@pytest.mark.parametrize('kind', ['other_owner', 'credential', 'managed'])
def test_projection_and_builtin_read_keep_original_denials(make_agent, kind):
    agent = make_agent(kind='user')
    _, boundary = _case(agent)
    home = agent.home_paths.root
    targets = {'other_owner': home / 'owners' / 'feishu' / 'other' / 'note.txt',
               'credential': home / 'config' / 'model-profiles' / 'fake.json',
               'managed': agent.home_paths.owner_home_dir / 'data' / 'plugins' / 'fake.txt'}
    target = targets[kind]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('FAKE-PROTECTED-NOT-REAL', encoding='utf-8')
    # 墙外授权也不得穿透别owner、宿主凭据与托管库原门。
    boundary['allowed_write_roots'].append(str(target.parent))
    row, actual = _pair(agent, target, boundary)
    assert not actual.ok
    assert 'FAKE-PROTECTED-NOT-REAL' not in actual.output
    assert row['read_access'] is False


def test_concurrent_invocations_do_not_share_builtin_read_boundaries(make_agent):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    agent = make_agent(kind='user')
    target, boundary = _case(agent)
    barrier = Barrier(2)

    def project(context):
        barrier.wait(timeout=10)
        return SectionProjection((_directory_item('target', target, context),))

    agent.tools.tools['self_map'].sections.register(SectionDescriptor('parallel_probe', '并发读边界', 998, project))

    def invoke(exact):
        current = dict(boundary)
        if exact:
            current |= {'read_scope_mode': 'exact', 'allowed_read_roots': []}
        result = execute_registry_test_call(agent.tools, 'self_map', {'section': 'parallel_probe', 'item': 'target'},
            allowed_tools=['self_map', 'read_file'], write_boundary=current, call_id='parallel-' + str(exact))
        assert result.ok, result.output
        return json.loads(result.output)['item']['read_access']

    with ThreadPoolExecutor(max_workers=2) as executor:
        allowed, denied = list(executor.map(invoke, [False, True]))
    assert allowed is True and denied is False


def test_plugin_context_without_builtin_read_view_does_not_invent_denial(make_agent):
    from agent_py_agent.agent.tooling.models import ToolInvocationContext
    from agent_py_agent.agent.tooling.self_map_registry import SelfMapReadContext
    from agent_py_agent.agent.tooling.workspace_read_scope import build_workspace_read_context
    agent = make_agent(kind='user')
    target, boundary = _case(agent)
    policy = agent.tools.tools['read_file'].path_access_policy
    plugin = build_workspace_read_context(cwd=target.parent / 'other-cwd', write_boundary=None,
        path_policy=policy, granted_external_roots=())
    snapshot = agent.tools.runtime_snapshot(allowed_tools=['self_map', 'read_file'])
    context = SelfMapReadContext.for_call(agent, ToolInvocationContext(snapshot, workspace_read_context=plugin))
    row = _directory_item('target', target, context)
    actual = execute_registry_test_call(agent.tools, 'read_file', {'path': str(target)},
        allowed_tools=['self_map', 'read_file'], write_boundary=boundary)
    assert actual.ok
    assert row['read_access'] is None and row['workspace_read_access'] is False


def test_declared_external_write_root_without_preflight_workspace_does_not_claim_read(make_agent, tmp_path):
    agent = make_agent(kind='user')
    _, boundary = _case(agent)
    target = tmp_path / 'not-execution-workspace' / 'note.txt'
    target.parent.mkdir()
    target.write_text('not-authorized-by-preflight', encoding='utf-8')
    boundary['allowed_write_roots'].append(str(target.parent))
    row, actual = _pair(agent, target, boundary)
    assert not actual.ok and 'PATH_OWNER_SCOPE_BLOCKED' in actual.output
    assert row['read_access'] is False


def test_full_admin_keeps_host_credential_denial_in_projection_and_real_read(make_agent):
    agent = make_agent(path_access_mode='full', access_mode='full-access')
    assert agent.tools.tools['read_file'].path_access_policy.owner_scope_root is None
    _, boundary = _case(agent)
    target = agent.home_paths.root / 'config' / 'model-profiles' / 'fake.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('FAKE-CREDENTIAL-NEVER-EXPOSE', encoding='utf-8')
    row, actual = _pair(agent, target, boundary)
    assert not actual.ok and 'PATH_HOST_CREDENTIAL_BLOCKED' in actual.output
    assert 'FAKE-CREDENTIAL-NEVER-EXPOSE' not in actual.output
    assert row['read_access'] is False and row['read_denial_code'] == 'PATH_HOST_CREDENTIAL_BLOCKED'


def test_builtin_exact_read_view_is_immutable_and_not_reloaded_after_freeze(make_agent):
    from types import MappingProxyType
    agent = make_agent(kind='user')
    target, boundary = _case(agent)
    boundary |= {'read_scope_mode': 'exact', 'allowed_read_roots': [str(target)]}
    actual = execute_registry_test_call(agent.tools, 'read_file', {'path': str(target)},
        allowed_tools=['self_map', 'read_file'], write_boundary=boundary)
    assert actual.ok
    seen = []

    def project(context):
        frozen = context.invocation.execution_permission_context
        assert isinstance(frozen, MappingProxyType)
        assert isinstance(frozen['builtin_read_roots'], tuple)
        assert isinstance(frozen['builtin_read_external_roots'], tuple)
        with pytest.raises(TypeError):
            frozen['builtin_read_roots'] = ()
        seen.append(frozen['builtin_read_roots'])
        boundary['allowed_read_roots'].clear()
        return SectionProjection((_directory_item('target', target, context),))

    agent.tools.tools['self_map'].sections.register(SectionDescriptor('frozen_probe', '冻结范围', 997, project))
    result = execute_registry_test_call(agent.tools, 'self_map', {'section': 'frozen_probe', 'item': 'target'},
        allowed_tools=['self_map', 'read_file'], write_boundary=boundary)
    assert result.ok, result.output
    assert json.loads(result.output)['item']['read_access'] is True
    assert len(seen) == 1 and seen[0] == (target.resolve(),)
    assert boundary['allowed_read_roots'] == []


def test_workspace_label_uses_frozen_execution_cwd_instead_of_agent_global_root(make_agent):
    agent = make_agent(kind='user')
    _, boundary = _case(agent)
    result = execute_registry_test_call(agent.tools, 'self_map', {'section': 'directories', 'item': 'workspace'},
        allowed_tools=['self_map', 'read_file'], write_boundary=boundary)
    assert result.ok, result.output
    assert json.loads(result.output)['item']['path'] == boundary['execution_cwd']


def test_workspace_skills_follow_original_frozen_snapshot_not_execution_cwd(make_agent):
    from agent_py_agent.agent.capability.skill_snapshot import SkillSnapshot
    agent = make_agent(kind='user')
    _, boundary = _case(agent)
    source_root = agent.home_paths.owner_home_dir / 'original-skill-project'
    agent._current_skill_snapshot = SkillSnapshot((), (), 'synthetic-frozen-skill-source',
        agent.home_paths.owner_id, str(source_root))
    result = execute_registry_test_call(agent.tools, 'self_map', {'section': 'directories', 'item': 'workspace_skills'},
        allowed_tools=['self_map', 'read_file'], write_boundary=boundary)
    assert result.ok, result.output
    assert json.loads(result.output)['item']['path'] == str(source_root / '.agents' / 'skills')
    agent._current_skill_snapshot = None
    unbound = execute_registry_test_call(agent.tools, 'self_map', {'section': 'directories', 'item': 'workspace_skills'},
        allowed_tools=['self_map', 'read_file'], write_boundary=boundary)
    assert unbound.ok, unbound.output
    row = json.loads(unbound.output)['item']
    assert row['path'] is None and row['state'] == 'not_observed'


def test_workspace_skills_never_expose_foreign_owner_snapshot_location(make_agent):
    from agent_py_agent.agent.capability.skill_snapshot import SkillSnapshot
    agent = make_agent(kind='user')
    _, boundary = _case(agent)
    other = agent.home_paths.root / 'owners' / 'providers' / 'feishu' / 'users' / 'other'
    agent._current_skill_snapshot = SkillSnapshot((), (), 'synthetic-foreign-location', agent.home_paths.owner_id, str(other))
    result = execute_registry_test_call(agent.tools, 'self_map', {'section': 'directories', 'item': 'workspace_skills'},
        allowed_tools=['self_map', 'read_file'], write_boundary=boundary)
    assert result.ok, result.output
    row = json.loads(result.output)['item']
    assert row['path'] is None and row['state'] == 'not_observed'
    assert str(other) not in result.output


@pytest.mark.parametrize('exact', [False, True])
def test_list_tools_and_map_permissions_label_plugin_and_builtin_read_scopes(make_agent, exact):
    agent = make_agent(kind='user')
    target, boundary = _case(agent)
    if exact:
        boundary |= {'read_scope_mode': 'exact', 'allowed_read_roots': [str(target)]}
    allowed = ['self_map', 'read_file', 'list_tools']
    listing = execute_registry_test_call(agent.tools, 'list_tools', {}, allowed_tools=allowed, write_boundary=boundary)
    assert listing.ok, listing.output
    original = json.loads(listing.output)['self_context']['filesystem']
    mapped = execute_registry_test_call(agent.tools, 'self_map', {'section': 'permissions', 'item': 'current'},
        allowed_tools=allowed, write_boundary=boundary)
    assert mapped.ok, mapped.output
    current = json.loads(mapped.output)['item']['filesystem']
    assert current == original
    assert current['read_roots_scope'] == 'plugin_workspace_context'
    assert current['builtin_read_scope'] == ('exact' if exact else 'policy')
    assert current['builtin_read_scope_source'] == 'frozen_executor_context'
    assert current['builtin_exact_read_root_count'] == (1 if exact else None)
    # exact的cwd外交件不在插件范围，但内置文件工具确实获准；不把两种根混成一份。
    assert current['read_roots'] == ([] if exact else [boundary['execution_cwd']])
    actual = execute_registry_test_call(agent.tools, 'read_file', {'path': str(target)},
        allowed_tools=allowed, write_boundary=boundary)
    assert actual.ok
