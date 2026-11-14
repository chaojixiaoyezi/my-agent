"""冻结读取合同的权限与协议回归；不计实际 TUI 或真实模型验收。"""

from dataclasses import replace
from pathlib import Path

import pytest

from agent_py_agent.agent.path_access_policy import PathAccessPolicy
from agent_py_agent.agent.tooling.workspace_read_scope import build_workspace_read_context
from agent_py_agent.agent.workspace_read_context import WorkspaceReadContext


# LLM: 只提供显式临时路径，不从真实用户目录补测试权限。
# 函数用途: 创建与 registry 相同的宿主读取快照。
def read_context(cwd, *, boundary=None, owner="", grants=(), mode="normal", dangerous=()):
    return build_workspace_read_context(
        cwd=cwd, write_boundary=boundary, granted_external_roots=grants,
        path_policy=PathAccessPolicy.from_values(mode=mode, dangerous_roots=dangerous, owner_scope_root=owner),
    )


@pytest.mark.parametrize("roots,expected", [([], ()), (["elsewhere"], ()), (["work/input.txt"], ("input.txt",)),
                                          (["work"], (".",)), (["."], (".",))])
def test_exact_scope_intersects_cwd_without_parent_or_empty_fallback(tmp_path, roots, expected):
    cwd = tmp_path / "work"
    context = read_context(cwd, boundary={"read_scope_mode": "exact", "allowed_read_roots": [str(tmp_path / p) for p in roots]})
    assert context.read_roots == tuple((cwd / item).resolve() for item in expected)
    restored = WorkspaceReadContext.from_payload(context.to_payload())
    assert restored == context
    if expected == ("input.txt",):
        assert restored.check(Path("input.txt")).allowed
        assert not restored.check(Path(".")).allowed
    if not expected:
        assert not restored.check(Path(".")).allowed


def test_normal_supplementary_read_roots_cannot_expand_workspace(tmp_path):
    context = read_context(tmp_path / "work", boundary={"allowed_read_roots": [str(tmp_path)]})
    assert context.read_roots == ((tmp_path / "work").resolve(),)
    assert context.check(Path("../outside.txt")).code == "PATH_READ_SCOPE_BLOCKED"


def test_default_and_aliased_dangerous_roots_round_trip(tmp_path):
    target = tmp_path / "restricted"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    for dangerous in ((), (str(target), str(alias), str(target))):
        context = read_context(tmp_path, dangerous=dangerous)
        assert len(context.path_policy.dangerous_roots) == len(set(context.path_policy.dangerous_roots))
        assert WorkspaceReadContext.from_payload(context.to_payload()) == context


def test_each_child_still_obeys_original_credential_and_dangerous_policy(tmp_path):
    context = read_context(tmp_path, dangerous=[str(tmp_path / "restricted")])
    assert context.check(Path(".")).allowed
    assert context.check(Path(".env")).code == "PATH_CREDENTIAL_FILE_BLOCKED"
    assert context.check(Path("restricted/child.txt")).code == "PATH_DANGEROUS_ROOT_BLOCKED"
    assert context.check(Path(".env.example")).allowed


def test_symlink_targets_must_satisfy_scope_and_policy(tmp_path):
    cwd = tmp_path / "work"
    cwd.mkdir()
    (cwd / "outside").symlink_to(tmp_path / "secret")
    (cwd / "alias").symlink_to(cwd / ".env")
    context = read_context(cwd)
    assert context.check(Path("outside")).code == "PATH_READ_SCOPE_BLOCKED"
    assert context.check(Path("alias")).code == "PATH_CREDENTIAL_FILE_BLOCKED"


def test_owner_wall_full_mode_and_exact_file_are_independent(tmp_path):
    owner = tmp_path / "home/owners/local/first"
    other = tmp_path / "home/owners/local/second"
    own = read_context(owner, owner=str(owner), mode="full")
    assert own.check(Path(".env")).allowed
    forbidden = read_context(other, owner=str(owner), mode="full", grants=(other,))
    assert forbidden.check(Path("data.txt")).code == "PATH_CROSS_OWNER_BLOCKED"
    control = read_context(tmp_path / "home/admin_grants", owner=str(owner), mode="full",
                           grants=(tmp_path / "home/admin_grants",))
    assert control.check(Path("control.json")).code == "PATH_ADMIN_GRANTS_BLOCKED"


def test_explicit_external_grant_is_intersected_and_does_not_override_other_denials(tmp_path):
    owner, cwd = tmp_path / "home/owners/local/main", tmp_path / "work"
    without = read_context(cwd, owner=str(owner))
    assert without.check(Path("note.txt")).code == "PATH_OWNER_SCOPE_BLOCKED"
    context = read_context(cwd, owner=str(owner), grants=(tmp_path,), dangerous=[str(cwd / "restricted")])
    assert context.granted_external_roots == (cwd.resolve(),)
    assert context.check(Path("note.txt")).allowed
    assert context.check(Path(".env")).code == "PATH_CREDENTIAL_FILE_BLOCKED"
    assert context.check(Path("restricted/child.txt")).code == "PATH_DANGEROUS_ROOT_BLOCKED"
    narrowed = read_context(cwd, owner=str(owner), grants=(cwd,),
                            boundary={"read_scope_mode": "exact", "allowed_read_roots": ["one.txt"]})
    assert narrowed.granted_external_roots == ((cwd / "one.txt").resolve(),)
    assert not narrowed.check(Path("two.txt")).allowed


def test_host_home_exemption_is_frozen_before_plugin_environment_changes(tmp_path, monkeypatch):
    home = tmp_path / "data"
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    context = read_context(home, dangerous=[str(tmp_path)])
    payload = context.to_payload()
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "other"))
    monkeypatch.setenv("HOME", str(tmp_path / "plugin-home"))
    restored = WorkspaceReadContext.from_payload(payload)
    assert context.path_policy.check(home / "note.txt").allowed
    assert restored.check(Path("note.txt")).allowed
    assert not restored.path_policy.check(tmp_path / "other/note.txt").allowed
    assert restored.path_policy.agent_home_root == home.resolve()


def test_owner_layout_and_unscoped_external_policy_share_the_host_data_root(tmp_path, monkeypatch):
    # H3 二审必须改 1：数据根只有一个权威来源。墙外复核策略沿用 owner 策略的宿主数据根，不再退回环境变量；载荷往返也不重读环境。
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "environment-home"))
    owner = tmp_path / "configured-home/owners/local/main"
    context = read_context(tmp_path / "work", owner=str(owner), grants=(tmp_path / "work",))
    configured = (tmp_path / "configured-home").resolve()
    assert context.path_policy.agent_home_root == context.external_policy.agent_home_root == configured
    assert WorkspaceReadContext.from_payload(context.to_payload()) == context


@pytest.mark.parametrize("field,value", [
    ("version", "2"), ("cwd", "."), ("cwd", "/tmp/../private"), ("cwd", "/tmp/\x00bad"),
    ("read_roots", "not-a-list"), ("read_roots", ["relative"]), ("read_roots", ["/"]),
    ("granted_external_roots", ["/"]), ("path_policy", {}),
])
def test_invalid_context_never_guesses_missing_or_wider_paths(tmp_path, field, value):
    payload = read_context(tmp_path).to_payload()
    payload[field] = value
    with pytest.raises(ValueError):
        WorkspaceReadContext.from_payload(payload)


def test_payload_is_independent_and_policy_values_are_not_normalized_again(tmp_path):
    context = read_context(tmp_path)
    payload = context.to_payload()
    payload["read_roots"].clear()
    assert context.read_roots == (tmp_path.resolve(),)
    denied = WorkspaceReadContext.from_payload(payload)
    assert denied.check(Path(".")).code == "PATH_READ_SCOPE_BLOCKED"
    payload = context.to_payload()
    payload["path_policy"]["mode"] = "FULL"
    with pytest.raises(ValueError):
        WorkspaceReadContext.from_payload(payload)
    invalid = replace(context, external_policy=replace(context.external_policy, owner_scope_root=tmp_path))
    with pytest.raises(ValueError):
        WorkspaceReadContext.from_payload(invalid.to_payload())


def test_business_aggregate_policy_transport_keeps_workspace_memory_and_maintenance(tmp_path):
    from agent_py_agent.agent.path_access_policy import business_aggregate_paths
    owner = (tmp_path / 'home/owners/local/main').resolve()
    roots = business_aggregate_paths(owner)
    policy = PathAccessPolicy.from_values(mode='full', owner_scope_root=owner,
                                         business_aggregate_roots=roots)
    context = build_workspace_read_context(cwd=owner, write_boundary=None, path_policy=policy,
                                           granted_external_roots=())
    restored = WorkspaceReadContext.from_payload(context.to_payload())
    for relative in ('memory/long_term/memory.jsonl', 'memory.md', 'data/telemetry/tool_calls.jsonl',
                     'workspace/runtime/workspaces/a/conversations/messages/a.jsonl', 'sessions/s.json', 'runtime.db-wal',
                     'artifacts/business/artifact-private.blob', 'artifacts/business/data/artifacts/registry.jsonl',
                     'data/business/catalog.json'):
        assert restored.check(Path(relative)).code == 'PATH_HOST_MANAGED_STORE_BLOCKED'
    for relative in ('workspace/project/memory/notes.txt', 'data/uploads/input.txt', 'workspace/input.md'):
        assert restored.check(Path(relative)).allowed
    maintenance = PathAccessPolicy.from_values(mode='full', owner_scope_root=owner)
    assert maintenance.check(owner / 'memory/long_term/memory.jsonl').allowed


@pytest.mark.parametrize('backend', ['rg', 'python'])
def test_canonical_business_file_reads_and_search_do_not_open_raw_aggregate(tmp_path, monkeypatch, backend):
    from agent_py_agent.agent.tooling._filesystem_list import ListFilesTool
    from agent_py_agent.agent.tooling._filesystem_read import FileSystemAccessOptions, ReadFileTool
    from agent_py_agent.agent.tooling._filesystem_search import SearchTextTool
    from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call
    owner = (tmp_path / 'home/owners/local/main').resolve()
    raw = owner / 'memory/long_term/memory.jsonl'
    raw.parent.mkdir(parents=True)
    raw.write_text('RAW_A731\nRAW_B842')
    blob = owner / 'artifacts/business/private.blob'
    blob.parent.mkdir(parents=True)
    blob.write_text('RAW_B842')
    public = owner / 'workspace/project/memory/notes.txt'
    public.parent.mkdir(parents=True)
    public.write_text('PUBLIC_ONLY_914')
    upload = owner / 'data/uploads/input.txt'
    upload.parent.mkdir(parents=True)
    upload.write_text('LEGAL_UPLOAD_916')
    options = FileSystemAccessOptions(owner_scope_root=str(owner))
    tools = {'search_text': SearchTextTool(owner, 20, access_options=options),
             'read_file': ReadFileTool(owner, 10000, access_options=options),
             'list_files': ListFilesTool(owner, 100, access_options=options)}
    boundary = {'canonical_owner_home_root': str(owner), 'business_aggregate_owner_root': str(owner)}
    if backend == 'python':
        monkeypatch.setattr('agent_py_agent.agent.tooling._filesystem_search.shutil.which', lambda *args: None)
    def invoke(name, args, *, scope=boundary):
        return execute_canonical_test_call(owner, tools=tools, tool_name=name, arguments=args,
            write_boundary=scope, owner_scope_root=str(owner)).result
    for name, denied_path in ((name, path) for name in ('read_file', 'list_files', 'search_text') for path in (raw, blob)):
        args = {'path': str(denied_path)}
        if name == 'search_text':
            args['query'] = 'RAW_'
        denied = invoke(name, args)
        assert denied.status != 'succeeded', repr(denied)
        assert 'PATH_HOST_MANAGED_STORE_BLOCKED' in repr(denied)
        assert 'RAW_A731' not in repr(denied) and 'RAW_B842' not in repr(denied)
    searched = invoke('search_text', {'path': str(owner), 'query': 'RAW_|PUBLIC_ONLY|LEGAL_UPLOAD', 'include_ignored': True})
    assert searched.status == 'succeeded', repr(searched)
    assert 'RAW_A731' not in repr(searched) and 'RAW_B842' not in repr(searched)
    assert 'PUBLIC_ONLY_914' in repr(searched) and 'LEGAL_UPLOAD_916' in repr(searched)
    assert invoke('read_file', {'path': str(public)}).status == 'succeeded'
    assert 'RAW_A731' in repr(invoke('read_file', {'path': str(raw)}, scope=None))
    # Exact输入只是路径上界，不能被模型自报用来绕过宿主聚合保护。
    exact = {**boundary, 'read_scope_mode': 'exact', 'allowed_read_roots': [str(raw)]}
    assert invoke('read_file', {'path': str(raw)}, scope=exact).status != 'succeeded'


def test_host_freeze_covers_foreground_space_and_child_without_widening(tmp_path):
    from types import SimpleNamespace as S

    from agent_py_agent.agent.agent_core.orchestration.create_policy import create_task_attributes
    from agent_py_agent.agent.agent_core.tool_runtime_ledger import (
        business_aggregate_owner_root,
        write_boundary_with_runtime_ledger,
    )
    from agent_py_agent.agent.memory_store.recall import MemoryRecallScope
    owner = (tmp_path / 'home/owners/local/main').resolve()
    agent = S(home_paths=S(owner_home_dir=owner), root=owner, config=S(), tools=S(owner_scope_root=str(owner)))
    params = S(memory_recall_scope=MemoryRecallScope((('global', 'global'), ('project', 'project:a'))),
               host_session_space={'space_id': 'space-a'}, host_channel_actor=None, request_id='req-a',
               business_context=None, task_attributes={}, write_boundary=None, context_scope='default', run_id='')
    assert business_aggregate_owner_root(agent, params) == owner
    assert write_boundary_with_runtime_ledger(agent, params)['business_aggregate_owner_root'] == str(owner)
    # 继承只取宿主当前scope，模型提供同名空值不能关闭；无绑定也不能凭模型字符串扩大。
    agent._current_run_params = params
    attrs = create_task_attributes({'attributes': {'host_business_aggregate_owner_root': ''}}, agent)
    assert attrs['host_business_aggregate_owner_root'] == str(owner)
    child = S(task_attributes=attrs)
    assert business_aggregate_owner_root(agent, child) == owner
    agent._current_run_params = S()
    attrs = create_task_attributes({'attributes': {'host_business_aggregate_owner_root': str(owner)}}, agent)
    assert 'host_business_aggregate_owner_root' not in attrs
    assert business_aggregate_owner_root(agent, S()) is None


@pytest.mark.sandbox_capability('nested_sandbox_exec' if __import__('sys').platform == 'darwin' else 'linux_bwrap')
@pytest.mark.parametrize('raw_kind', ['memory', 'upload'])
def test_business_raw_aggregate_is_hidden_by_original_shell_sandbox(tmp_path, raw_kind):
    from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions
    from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call
    owner = (tmp_path / 'home/owners/local/main').resolve()
    raw = owner / ('memory/long_term/memory.jsonl' if raw_kind == 'memory' else 'artifacts/business/private.blob')
    raw.parent.mkdir(parents=True)
    raw.write_text('RAW_B842')
    public = owner / 'workspace/project/memory/input.txt'
    public.parent.mkdir(parents=True)
    public.write_text('ORDINARY_MEMORY_913')
    shell = ShellTool(owner, options=ShellToolOptions(owner_scope_root=str(owner)))
    args = {'command': f'cat {raw}; cat {public}', 'timeout': 10}
    boundary = {'canonical_owner_home_root': str(owner), 'business_aggregate_owner_root': str(owner),
                'allowed_write_roots': [str(public.parent)]}
    def invoke(bound):
        return execute_canonical_test_call(owner, tools={'run_command': shell}, tool_name='run_command',
            arguments=args, write_boundary=bound, owner_scope_root=str(owner), run_id='shell-business').result
    result = invoke(boundary)
    if result.status == 'approval_required':
        binding = {**result.metadata['action_decision']['approval_request'], 'status': 'APPROVED', 'approval_id': 'qa-approved'}
        result = invoke({**boundary, 'approved_actions': [binding]})
    assert 'RAW_B842' not in repr(result), repr(result)
    assert 'ORDINARY_MEMORY_913' in repr(result), repr(result)
    assert not shell.host_private_roots  # 本次副本不能污染下一个无空间维护调用。


def test_business_aggregate_case_alias_follows_real_volume_identity(tmp_path):
    from agent_py_agent.agent.path_access_policy import business_aggregate_paths
    owner = (tmp_path / 'home/owners/local/main').resolve()
    source = owner / 'memory'
    source.mkdir(parents=True)
    (source / 'input.txt').write_text('PRIVATE_B842')
    alias = owner / 'MeMoRy'
    policy = PathAccessPolicy.from_values(owner_scope_root=owner,
                                         business_aggregate_roots=business_aggregate_paths(owner))
    if alias.exists() and alias.samefile(source):
        assert not policy.check(alias / 'input.txt').allowed
        assert policy.host_managed_store(alias / 'input.txt') == source
    else:
        alias.mkdir()
        (alias / 'input.txt').write_text('ORDINARY_CASE_DISTINCT')
        assert policy.check(alias / 'input.txt').allowed
        assert policy.host_managed_store(alias / 'input.txt') is None
    assert policy.check(source / 'input.txt').code == 'PATH_HOST_MANAGED_STORE_BLOCKED'
