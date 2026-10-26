# LLM: WP1目录绑定回归只在临时目录构造身份和工作区，不触真实宿主权限或运行记录。
# 模块用途: 验证目录别名、认证Gateway路由、身份隔离、工具双根及监控跨入口事务。
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.tool_runtime_ledger import write_boundary_with_runtime_ledger
from agent_py_agent.agent.assets import skill_asset_id
from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.workflow_store import WorkflowDefinition, WorkflowStore
from agent_py_agent.agent.common.nofollow_fs import NoFollowPathError
from agent_py_agent.agent.conversation.authority import (
    CONVERSATION_EXECUTION_CWD_ATTR,
    CONVERSATION_RUNTIME_WORKSPACE_ROOTS_ATTR,
)
from agent_py_agent.agent.gateway_parts.workspace_scope import (
    GatewayWorkspaceScopeError,
    gateway_request_workspace_scope,
)
from agent_py_agent.agent.path_access_policy import granted_external_work_roots
from agent_py_agent.cli.parser import build_interactive_parser, build_parser
from agent_py_agent.cli.workspace_resolution import (
    explicit_workspace_roots,
    validate_requested_workspace_roots,
)


# LLM: 只构造结构化本机/远程身份；路径均在tmp_path，不模拟完整Agent或赋予隐式管理员权限。
# 函数用途: 给目录硬门与工具边界测试提供相同的用户和家目录事实。
def dir_agent(tmp_path, *, provider="local", kind="main", access="workspace-write"):
    root = tmp_path / "home"
    owner = root / "owners" / "local" / ("main" if kind == "main" else "bob")
    owner.mkdir(parents=True, exist_ok=True)
    config = SimpleNamespace(my_agent_home=str(root), my_agent_owner_provider=provider,
        my_agent_owner_kind=kind, my_agent_owner_id="bob", access_mode=access,
        workspace_root="")
    return SimpleNamespace(config=config,
        home_paths=SimpleNamespace(root=root, owner_home_dir=owner, owner_provider=provider,
            owner_kind=kind, owner_id="main" if kind == "main" else "bob"),
        tools=SimpleNamespace(owner_scope_root=str(owner) if access != "full-access" else "",
            workspace_root=owner), root=owner)


@pytest.mark.parametrize("argv", [
    ["--dir", "."], ["chat", "--dir", "."], ["chat", "--workspace-root", "."],
    ["run", "--dir", ".", "hello"], ["--workspace", ".", "chat"],
])
# LLM: 只解析命令参数，不构造Agent或启动Gateway；各入口须汇入同一显式根流水线。
# 函数用途: 核对顶层、chat和run的三个目录别名。
def test_dir_aliases_across_cli_routes(argv):
    args = build_parser().parse_args(argv)
    assert explicit_workspace_roots(args) == [Path.cwd().resolve()]


# LLM: 交互子解析器不能用默认值覆盖已给的顶层目录；不执行任何业务。
# 函数用途: 防止顶层目录参数被chat默认值清空。
def test_dir_top_level_survives_interactive_defaults():
    args = build_interactive_parser().parse_args(["--dir", ".", "chat"])
    assert explicit_workspace_roots(args) == [Path.cwd().resolve()]


@pytest.mark.parametrize("shape", ["missing", "file", "root"])
# LLM: 仅在临时目录构造反例；不得自动建目录迁就输入。
# 函数用途: 核对不存在路径、普通文件与系统根被拒绝。
def test_dir_invalid_explicit_paths_rejected(tmp_path, shape):
    target = tmp_path / shape
    if shape == "file":
        target.write_text("not a directory")
    if shape == "root":
        target = Path("/")
    with pytest.raises(ValueError):
        explicit_workspace_roots(SimpleNamespace(workspace_root=str(target)))


# LLM: 临时结构化管理员身份走真实CLI/Gateway目录校验，非执行授权模拟。
# 函数用途: 验证管理员普通模式允许安全外部项目。
def test_dir_admin_workspace_write_accepts_explicit_project(tmp_path):
    agent = dir_agent(tmp_path)
    work = tmp_path / "project"
    work.mkdir()
    validate_requested_workspace_roots(agent.config, [work])
    assert gateway_request_workspace_scope(agent, {"workspace": {"cwd": str(work), "roots": [str(work)]}}) == (str(work), (str(work),))


@pytest.mark.parametrize("provider,kind", [("local", "user"), ("feishu", "user")])
# LLM: 空工具墙不是管理员身份，远程与本机普通用户均走同一硬门。
# 函数用途: 验证非管理员不能声明家目录外的工作根。
def test_dir_nonadmin_cannot_override_even_without_tool_wall(tmp_path, provider, kind):
    agent = dir_agent(tmp_path, provider=provider, kind=kind)
    work = tmp_path / "project"
    work.mkdir()
    agent.tools.owner_scope_root = ""
    with pytest.raises(ValueError):
        validate_requested_workspace_roots(agent.config, [work])
    with pytest.raises(GatewayWorkspaceScopeError):
        gateway_request_workspace_scope(agent, {"workspace": {"cwd": str(work)}})


@pytest.mark.parametrize("part", ["owners/local/bob", "config", "admin_grants", "."])
# LLM: 管理员项目绑定不覆盖宿主和其它owner隔离；只操作tmp_path。
# 函数用途: 核对跨用户和宿主控制面工作根被拒绝。
def test_dir_admin_cannot_bind_other_owner_or_control_plane(tmp_path, part):
    agent = dir_agent(tmp_path)
    work = agent.home_paths.root / part
    work.mkdir(parents=True, exist_ok=True)
    with pytest.raises(ValueError):
        validate_requested_workspace_roots(agent.config, [work])
    with pytest.raises(GatewayWorkspaceScopeError):
        gateway_request_workspace_scope(agent, {"workspace": {"cwd": str(work)}})


# LLM: 边界由真实账本生成，只检查本轮typed目录与规范家目录，不以声明扩权。
# 函数用途: 核对管理员普通模式的项目加家目录双根。
def test_dir_admin_execution_boundary_keeps_project_and_home_only(tmp_path):
    agent = dir_agent(tmp_path)
    work = tmp_path / "project"
    work.mkdir()
    params = SimpleNamespace(context_scope="default", task_attributes={
        "conversation_thread_id": "thread-1", CONVERSATION_EXECUTION_CWD_ATTR: str(work),
        CONVERSATION_RUNTIME_WORKSPACE_ROOTS_ATTR: [str(work)]})
    boundary = write_boundary_with_runtime_ledger(agent, params)
    assert boundary["execution_cwd"] == str(work)
    assert set(boundary["execution_workspace_roots"]) == {str(work), str(agent.root)}
    assert set(boundary["allowed_write_roots"]) == {str(work), str(agent.root)}
    assert granted_external_work_roots(boundary, agent.root) == (work,)


# LLM: 缺少workspace时保持无显式声明，不从测试进程cwd推导授权。
# 函数用途: 防止Gateway把daemon启动目录当默认工作区。
def test_dir_gateway_no_payload_does_not_inherit_daemon_cwd(tmp_path):
    assert gateway_request_workspace_scope(dir_agent(tmp_path), {}) == ("", ())


# LLM: cli_run显式目录沿同一真实账本投影；不启动模型或外部命令。
# 函数用途: 验证非交互run保留配置中的项目根。
def test_dir_cli_run_uses_explicit_config_workspace(tmp_path):
    agent = dir_agent(tmp_path)
    work = tmp_path / "project"
    work.mkdir()
    agent.config.workspace_root = [str(work)]
    params = SimpleNamespace(context_scope="default", source="cli_run", task_attributes={})
    boundary = write_boundary_with_runtime_ledger(agent, params)
    assert boundary.get("execution_cwd") == str(work)
    assert set(boundary["allowed_write_roots"]) == {str(work), str(agent.root)}


@pytest.mark.parametrize("bad_root", ["/", "missing", "control"])
# LLM: Full Access不取消工作目录安全校验；测试不缩窄其独立文件权限。
# 函数用途: 核对全权限模式也不绑定危险或失效目录。
def test_dir_full_access_does_not_bind_invalid_runtime_roots(tmp_path, bad_root):
    agent = dir_agent(tmp_path, access="full-access")
    bad = "/" if bad_root == "/" else str(tmp_path / "missing")
    if bad_root == "control":
        bad = str(agent.home_paths.root / "config")
        Path(bad).mkdir()
    params = SimpleNamespace(context_scope="default", task_attributes={
        "conversation_thread_id": "thread-1", CONVERSATION_EXECUTION_CWD_ATTR: bad,
        CONVERSATION_RUNTIME_WORKSPACE_ROOTS_ATTR: [bad]})
    boundary = write_boundary_with_runtime_ledger(agent, params)
    assert boundary["execution_cwd"] == str(agent.root)
    assert bad not in boundary["execution_workspace_roots"]


# LLM: 实际调用正式工具网关在临时A/家目录读写，B目录必须真实拒绝且没有落盘。
# 函数用途: 验证目录账本确实驱动文件工具而非只生成展示字段。
def test_dir_generated_boundary_drives_real_file_tools_and_blocks_b(tmp_path, monkeypatch):
    from agent_py_agent.tests.test_tool_gateway_contract import _execute, _registry

    agent = dir_agent(tmp_path)
    monkeypatch.setenv("MY_AGENT_HOME", str(agent.home_paths.root))
    a, b = tmp_path / "A", tmp_path / "B"
    a.mkdir()
    b.mkdir()
    registry = _registry(agent.root)
    registry.owner_scope_root = str(agent.root)
    agent.tools = registry
    params = SimpleNamespace(context_scope="default", task_attributes={
        "conversation_thread_id": "thread-1", CONVERSATION_EXECUTION_CWD_ATTR: str(a),
        CONVERSATION_RUNTIME_WORKSPACE_ROOTS_ATTR: [str(a)]})
    boundary = write_boundary_with_runtime_ledger(agent, params)
    for target in (a / "out.txt", agent.root / "home.txt"):
        written = _execute(registry, "write_file", {"path": str(target), "content": "first"}, write_boundary=boundary)
        assert written.ok, written.output
        read = _execute(registry, "read_file", {"path": str(target)}, write_boundary=boundary)
        assert read.ok and "first" in read.output
    denied = _execute(registry, "write_file", {"path": str(b / "no.txt"), "content": "no"}, write_boundary=boundary)
    assert not denied.ok
    assert not (b / "no.txt").exists()


@pytest.mark.parametrize("raw,kind,value,title", [
    ("/dir", "dir", "", ""), ("/dir /tmp/project", "dir", "/tmp/project", ""),
    ('/dir "/tmp/with space"', "dir", "/tmp/with space", ""),
    ("/fork", "fork", "", ""), ('/fork msg-1 --title "方案 B"', "fork", "msg-1", "方案 B"),
])
# LLM: 只做真实命令解析，不把领域解析成功当Gateway路由接通。
# 函数用途: 核对目录、分叉参数与带空格标题的解析。
def test_dir_and_fork_control_parsing(raw, kind, value, title):
    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control

    command = parse_conversation_control(raw)
    assert command is not None
    assert command.valid and command.kind == kind
    assert command.value == value
    assert command.name == title


@pytest.mark.parametrize("raw", ["/fork one two", "/fork --bad", "/fork --title", '/dir "unclosed'])
# LLM: 无效斜线参数仍必须返回明确无效命令，不进入模型普通输入。
# 函数用途: 核对分叉多余参数和未闭引号被拒绝。
def test_dir_and_fork_bad_arguments_are_not_model_prompts(raw):
    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control

    command = parse_conversation_control(raw)
    assert command is not None and not command.valid


# LLM: 在临时真实ConversationStore调用领域执行口；不宣称CLI/Gateway公共路由已验证。
# 函数用途: 核对目录切换只改选定线程且非管理员外部切换失败。
def test_dir_domain_control_switches_only_selected_thread(tmp_path):
    from agent_py_agent.agent.conversation import workspace_context
    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
    from agent_py_agent.agent.conversation.models import ConversationThread
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.conversation.store_threads import create_thread_atomic

    handler = getattr(workspace_context, "execute_workspace_control", None)
    assert callable(handler)
    agent = dir_agent(tmp_path)
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    store = ConversationStore(agent.root / "conversations")
    for thread_id in ("thread-first", "thread-second"):
        create_thread_atomic(store.threads, ConversationThread(thread_id=thread_id,
            canonical_user_id="local-agent", cwd=str(a), owner_home=str(agent.root)))
    result = handler(store, parse_conversation_control(f"/dir {b}"), thread_id="thread-first",
        owner_home=agent.root, is_admin=True)
    assert result.ok, result
    assert result.details["workspace_root"] == str(b)
    assert store.threads.require("thread-second").cwd == str(a)
    rejected = handler(store, parse_conversation_control(f"/dir {b}"), thread_id="thread-second",
        owner_home=agent.root, is_admin=False)
    assert not rejected.ok and rejected.error_code == "WORKSPACE_DIR_INVALID"
    assert store.threads.require("thread-second").cwd == str(a)
    conflicted = handler(store, parse_conversation_control(f"/dir {a}"), thread_id="thread-first",
        owner_home=agent.root, is_admin=True, expected_revision=1)
    assert not conflicted.ok and conflicted.error_code == "WORKSPACE_REVISION_CONFLICT"
    assert store.threads.require("thread-first").cwd == str(b)


# LLM: 用读前后真实文件集合验证只读上下文，不以返回空列表代替零写入证据。
# 函数用途: 防止冷读创建项目目录、锁或存储记录。
def test_dir_context_cold_read_has_no_workspace_writes(tmp_path):
    from agent_py_agent.agent.conversation import workspace_context

    handler = getattr(workspace_context, "get_workspace_context", None)
    assert callable(handler)
    agent = dir_agent(tmp_path)
    work = tmp_path / "project"
    work.mkdir()
    before = sorted(str(path) for path in tmp_path.rglob("*"))
    result = handler(owner_home=agent.root, workspace_root=work, is_admin=True)
    assert result["workspace_root"] == str(work)
    assert result["assets"] == {"skills": [], "scripts": [], "workflows": [], "monitors": []}
    assert result["threads"] == []
    assert sorted(str(path) for path in tmp_path.rglob("*")) == before


# LLM: 先声明规范A再替换链接，真实运行边界必须回退家目录而不授权B。
# 函数用途: 防止已绑定项目经符号链接偷换到另一工作根。
def test_dir_persisted_project_cannot_retarget_through_replaced_symlink(tmp_path):
    agent = dir_agent(tmp_path)
    a, b = tmp_path / "A", tmp_path / "B"
    a.mkdir()
    b.mkdir()
    params = SimpleNamespace(context_scope="default", task_attributes={
        "conversation_thread_id": "thread-1", CONVERSATION_EXECUTION_CWD_ATTR: str(a),
        CONVERSATION_RUNTIME_WORKSPACE_ROOTS_ATTR: [str(a)]})
    a.rename(tmp_path / "original-A")
    a.symlink_to(b, target_is_directory=True)
    boundary = write_boundary_with_runtime_ledger(agent, params)
    assert boundary["execution_cwd"] == str(agent.root)
    assert str(b) not in boundary["allowed_write_roots"]


# LLM: 精简宿主缺少身份事实时失败关闭，不把字段缺失按管理员默认处理。
# 函数用途: 防止普通owner的精简Host投影获得外部项目授权。
def test_dir_thin_host_missing_owner_kind_cannot_gain_admin_binding(tmp_path):
    owner = tmp_path / "home" / "owners" / "local" / "bob"
    owner.mkdir(parents=True)
    work = tmp_path / "outside"
    work.mkdir()
    thin_host = SimpleNamespace(config=SimpleNamespace(my_agent_owner_provider="local"),
        tools=SimpleNamespace(owner_scope_root=str(owner)))
    with pytest.raises(GatewayWorkspaceScopeError):
        gateway_request_workspace_scope(thin_host, {"workspace": {"cwd": str(work)}})


# LLM: 使用真实配置解析，并在另一启动目录构造工具账本，避免只验证临时mock中的同一路径。
# 函数用途: 防止相对或波浪号工作区在cli_run中丢失已验证的规范目录。
@pytest.mark.parametrize("spelling", ["relative", "tilde"])
def test_dir_configured_roots_are_frozen_before_cli_runtime(tmp_path, monkeypatch, spelling):
    from agent_py_agent.cli.common import agent_configuration

    agent = dir_agent(tmp_path)
    work = tmp_path / "project"
    work.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    config_path = tmp_path / "config.yaml"
    raw = "project" if spelling == "relative" else "~/project"
    config_path.write_text(f'my_agent_home: {agent.home_paths.root}\nworkspace_root: "{raw}"\nmodel_backend: echo\n')
    launch = tmp_path / "launch"
    launch.mkdir()
    monkeypatch.chdir(launch)
    config, roots = agent_configuration(SimpleNamespace(config=str(config_path)))
    assert roots == [work]
    assert config.workspace_root == [str(work)]
    agent.config = config
    params = SimpleNamespace(context_scope="default", source="cli_run", task_attributes={})
    assert write_boundary_with_runtime_ledger(agent, params)["execution_cwd"] == str(work)


# LLM: 即使显式目录在owner墙内或处于Full Access，绑定失效也不能继承为执行根。
# 函数用途: 验证运行前重新检查墙内不存在、文件和宿主控制目录。
@pytest.mark.parametrize("access", ["workspace-write", "full-access"])
@pytest.mark.parametrize("shape", ["missing", "file", "retarget"])
def test_dir_invalid_inner_runtime_binding_falls_back_home(tmp_path, access, shape):
    agent = dir_agent(tmp_path, access=access)
    bad = agent.root / "project"
    if shape == "file":
        bad.write_text("not a directory")
    elif shape == "retarget":
        target = agent.root / "runs" / "private-state"
        target.mkdir(parents=True)
        bad.symlink_to(target, target_is_directory=True)
    params = SimpleNamespace(context_scope="default", task_attributes={
        "conversation_thread_id": "thread-1", CONVERSATION_EXECUTION_CWD_ATTR: str(bad),
        CONVERSATION_RUNTIME_WORKSPACE_ROOTS_ATTR: [str(bad)]})
    boundary = write_boundary_with_runtime_ledger(agent, params)
    assert boundary["execution_cwd"] == str(agent.root)
    assert str(bad.resolve()) not in boundary["execution_workspace_roots"]


# LLM: 目录名单来自真实权限权威，不以最终文件工具仍可能拒绝写入替代绑定检查。
# 函数用途: 防止未列入旧手写黑名单的宿主目录成为工作区。
@pytest.mark.parametrize("relative", ["temporary_grants", "capability_requests", "workspace/runtime", "memory/curator"])
def test_dir_all_authoritative_host_state_directories_rejected(tmp_path, relative):
    agent = dir_agent(tmp_path)
    target = agent.root / relative
    target.mkdir(parents=True)
    with pytest.raises(ValueError):
        validate_requested_workspace_roots(agent.config, [target])


# LLM: 资产测试仅在tmp_path运行真实存储，不访问真实用户家目录；目录测试由父代理另行合并。
# 模块用途: 验证项目覆盖、显式写入、晋升CAS和跨资产原子事务。




# LLM: 缺实现使用明确断言报红，不把意外ImportError当业务失败。
# 函数用途: 加载本副本的项目资产公共入口。
def asset_api():
    name = 'agent_py_agent.agent.assets.workspace_assets'
    assert importlib.util.find_spec(name), '尚未实现WorkspaceAssetOverlay与批量事务'
    return importlib.import_module(name)


# LLM: 测试草稿沿正式描述符传输，无真实外部地址或凭据。
# 函数用途: 构造四种资产的最小有效批量项。
def asset_item(kind='script', identifier='sample', **options):
    item = {'descriptor': AssetDescriptor(identifier, kind, identifier).to_dict()}
    item.update({'script': {'code': 'print("ok")\n'}, 'workflow': {'steps': []},
                 'skill': {'body_markdown': f'---\nname: {identifier}\ndescription: 测试技能\n---\n# 使用\n'},
                 'monitor': {'schedule_spec': {'kind': 'every', 'every_seconds': 60}}}[kind])
    return item | options


# LLM: 冷读必须走真实Overlay与两种Store，不允许以构造锁或目录充当缓存。
# 函数用途: 验证空项目没有任何.agent写入。
def test_asset_cold_overlay_and_stores_create_no_agent(tmp_path):
    api = asset_api()
    home, project = tmp_path / 'home', tmp_path / 'project'
    project.mkdir()
    overlay = api.WorkspaceAssetOverlay(project, owner_home=home)
    assert overlay.list_assets() == [] and overlay.list_agents() == []
    assert overlay.get_asset('script', 'missing') is None
    assert ScriptStore(home, workspace_root=project).get_script('missing') is None
    assert WorkflowStore(home, workspace_root=project).list_workflows() == []
    assert not (project / '.agent').exists()


# LLM: 来源字段故意伪造以验证展示值不能重定位写根或覆盖真实来源。
# 函数用途: 验证四类资产的同ID覆盖顺序与来源投影。
@pytest.mark.parametrize('kind', ['script', 'workflow', 'skill', 'monitor'])
def test_asset_precedence_and_scope_is_projection(tmp_path, kind):
    api = asset_api()
    home, project, builtin = (tmp_path / name for name in ('home', 'project', 'builtin'))
    home.mkdir()
    project.mkdir()
    builtin.mkdir()
    for root, scope in ((builtin, 'builtin'), (home, 'owner')):
        descriptor = AssetDescriptor('sample', kind, scope, scope_layer='workspace', workspace_root='/untrusted')
        api.commit_asset_batch([asset_item(kind, descriptor=descriptor.to_dict())], owner_home=root)
    overlay = api.WorkspaceAssetOverlay(project, owner_home=home, builtin_roots=(builtin,))
    assert overlay.get_asset(kind, 'sample')['name'] == 'owner'
    assert overlay.get_asset(kind, 'sample')['source_scope'] == 'owner'
    api.commit_asset_batch([asset_item(kind, descriptor=AssetDescriptor('sample', kind, 'project').to_dict())],
                           scope='workspace', workspace_root=project, owner_home=home)
    record = overlay.get_asset(kind, 'sample')
    assert record['name'] == 'project' and record['scope_layer'] == record['source_scope'] == 'workspace'
    assert record['workspace_root'] == str(project)
    assert len(overlay.list_assets(kind)) == 1


# LLM: 规则只按来源顺序提供正文，测试不执行或授予其中任何文本权限。
# 函数用途: 验证全局与两种项目规则均保留且读取无副作用。
def test_asset_agents_merge_source_order_and_cold_read(tmp_path):
    api = asset_api()
    home, project, builtin = (tmp_path / name for name in ('home', 'project', 'builtin'))
    for root, content in ((home, 'owner'), (project, 'project'), (builtin, 'builtin')):
        root.mkdir()
        (root / 'AGENTS.md').write_text(content)
    overlay = api.WorkspaceAssetOverlay(project, owner_home=home, builtin_roots=(builtin,))
    assert [row['content'] for row in overlay.list_agents()] == ['builtin', 'owner', 'project']
    assert not (project / '.agent').exists()
    (project / '.agent').mkdir()
    (project / '.agent/AGENTS.md').write_text('overlay')
    assert [row['content'] for row in overlay.list_agents()][-1] == 'overlay'


# LLM: 保存默认Owner，只有显式workspace目标才建立覆盖；旧CAS必须在写前失败。
# 函数用途: 验证两种Store的写入目标与版本冲突。
@pytest.mark.parametrize('kind', ['script', 'workflow'])
def test_asset_store_explicit_scope_and_cas(tmp_path, kind):
    asset_api()
    home, project = tmp_path / 'home', tmp_path / 'project'
    project.mkdir()
    store = (ScriptStore if kind == 'script' else WorkflowStore)(home, workspace_root=project)
    descriptor = AssetDescriptor('sample', kind, 'sample', scope_layer='workspace', workspace_root='/untrusted')
    if kind == 'script':
        store.save_script(descriptor, 'pass\n')
        saved = store.save_script(descriptor, 'print(1)\n', target_scope='workspace', expected_revision=0)
        assert saved['revision'] == 1 and store.get_script('sample')['code'] == 'print(1)\n'
        with pytest.raises(ValueError, match='ASSET_REVISION_CONFLICT'):
            store.save_script(descriptor, 'pass\n', target_scope='workspace', expected_revision=0)
    else:
        definition = WorkflowDefinition(descriptor, ())
        store.save_workflow(definition)
        saved = store.save_workflow(definition, target_scope='workspace', expected_revision=0)
        assert saved.descriptor.revision == 1 and store.get_workflow('sample').descriptor.source_scope == 'workspace'
        with pytest.raises(ValueError, match='ASSET_REVISION_CONFLICT'):
            store.save_workflow(definition, target_scope='workspace', expected_revision=0)
    assert (home / f'{kind}s/sample/{kind}.json').exists()
    assert (project / f'.agent/{kind}s/sample/{kind}.json').exists()


# LLM: 晋升不移动或覆盖项目源，目标版本与出处从正式Owner读口核验。
# 函数用途: 验证四类资产晋升写入原canonical路径并受CAS保护。
@pytest.mark.parametrize('kind', ['script', 'workflow', 'skill', 'monitor'])
def test_asset_promote_canonical_cas_and_provenance(tmp_path, kind):
    api = asset_api()
    home, project = tmp_path / 'home', tmp_path / 'project'
    home.mkdir()
    project.mkdir()
    api.commit_asset_batch([asset_item(kind)], scope='workspace', workspace_root=project, owner_home=home)
    result = api.promote_to_owner(kind, 'sample', workspace_root=project, owner_home=home, expected_revision=0)
    assert result['items'][0]['revision'] == 1
    assert api.WorkspaceAssetOverlay(project, owner_home=home).get_asset(kind, 'sample')['source_scope'] == 'workspace'
    record = api.WorkspaceAssetOverlay(None, owner_home=home).get_asset(kind, 'sample')
    assert record['metadata']['promoted_from_workspace'] == str(project)
    with pytest.raises(ValueError, match='ASSET_REVISION_CONFLICT'):
        api.promote_to_owner(kind, 'sample', workspace_root=project, owner_home=home, expected_revision=0)
    if kind == 'monitor':
        assert (home / 'data/monitors/sample.json').exists()
        assert not (home / 'monitors/sample.json').exists()


# LLM: 坏项后置，若按项边验边存会留下good资产，因此必须一次预检整包。
# 函数用途: 验证语法、schema和CAS失败均不留下部分资产。
@pytest.mark.parametrize('bad', [asset_item(code='def broken(:\n'), asset_item('workflow', steps=[{'mode': 'invalid'}]),
                                  asset_item('skill', body_markdown='---\nname: [broken\n---\n'),
                                  asset_item('monitor', schedule_spec={'kind': 'every', 'every_seconds': -1}),
                                  asset_item(expected_revision=9)])
def test_asset_batch_preflight_zero_partial_assets(tmp_path, bad):
    api = asset_api()
    home = tmp_path / 'home'
    home.mkdir()
    with pytest.raises((ValueError, TypeError, SyntaxError)):
        api.commit_asset_batch([asset_item(identifier='good'), bad], owner_home=home)
    assert not (home / 'scripts/good/script.json').exists()


# LLM: 第一项已经真实发表后才注入跨集合故障，旧资产必须无损恢复。
# 函数用途: 验证阶段二失败撤销全部四类正式资产。
def test_asset_batch_publish_failure_rolls_back_every_kind(tmp_path, monkeypatch):
    api = asset_api()
    home = tmp_path / 'home'
    home.mkdir()
    api.commit_asset_batch([asset_item(code='print("old")\n')], owner_home=home)
    original = api.write_text_atomic_beneath
    failed = False

    # LLM: 故障在第一个资产发表之后；回滚仍执行真实nofollow写入。
    # 函数用途: 模拟跨集合发表第二项失败。
    def asset_fail_once(root, parts, text, **options):
        nonlocal failed
        if parts == ('workflows', 'flow', 'workflow.json') and not failed:
            failed = True
            raise OSError('synthetic publish failure')
        return original(root, parts, text, **options)

    monkeypatch.setattr(api, 'write_text_atomic_beneath', asset_fail_once)
    with pytest.raises(OSError, match='synthetic publish failure'):
        api.commit_asset_batch([asset_item(code='print("new")\n'), asset_item('skill', 'guide'),
                                asset_item('monitor', 'watch'), asset_item('workflow', 'flow')], owner_home=home)
    assert ScriptStore(home).get_script('sample')['code'] == 'print("old")\n'
    assert not (home / 'skills/guide/SKILL.md').exists()
    assert not (home / 'data/monitors/watch.json').exists()
    assert not (home / 'workflows/flow/workflow.json').exists()
    assert not list(home.rglob('*.stage-*'))


# LLM: 原生硬链接若被平台明确拒绝，保留nlink=2元数据分支并记录区别，不删除拒绝断言。
# 函数用途: 验证.agent、代码、元数据及锁的链接攻击均被拒绝且目标不变。
@pytest.mark.parametrize('link,leaf', [(link, leaf) for link in ('symlink', 'hardlink')
                                    for leaf in ('metadata', 'code', 'agent', 'lock')
                                    if not (link == 'hardlink' and leaf == 'agent')])
def test_asset_nofollow_rejects_links_and_preserves_target(tmp_path, monkeypatch, record_property, link, leaf):
    api = asset_api()
    home, project = tmp_path / 'home', tmp_path / 'project'
    home.mkdir()
    project.mkdir()
    target = tmp_path / 'target'
    target.write_text('untouched')
    if leaf == 'agent':
        (project / '.agent').symlink_to(home, target_is_directory=True)
    else:
        api.commit_asset_batch([asset_item()], scope='workspace', workspace_root=project, owner_home=home)
        relative = {'metadata': '.agent/scripts/sample/script.json', 'code': '.agent/scripts/sample/main.py',
                    'lock': '.agent/scripts/.assets.lock'}[leaf]
        path = project / relative
        path.unlink()
        if link == 'symlink':
            path.symlink_to(target)
        else:
            try:
                os.link(target, path)
                record_property('hardlink_observation', 'native')
            except PermissionError as error:
                record_property('hardlink_observation', f'metadata_injection_after_errno_{error.errno}')
                path.write_text(target.read_text())
                original_stat = os.stat

                # LLM: 仅在原生硬链接被明确拒绝后注入目标叶nlink=2，不改nofollow产品拒绝逻辑或其它真实stat。
                # 函数用途: 保留同一硬链接拒绝分支的可运行证据，不冒称原生硬链接成功。
                def asset_linked_stat(name, *args, **options):
                    metadata = original_stat(name, *args, **options)
                    if name == path.name and options.get('follow_symlinks') is False:
                        values = list(metadata)
                        values[3] = 2
                        return os.stat_result(values)
                    return metadata

                monkeypatch.setattr(os, 'stat', asset_linked_stat)
                monkeypatch.setattr(os, 'supports_dir_fd', {*os.supports_dir_fd, asset_linked_stat})
    with pytest.raises(NoFollowPathError):
        api.commit_asset_batch([asset_item()], scope='workspace', workspace_root=project, owner_home=home)
    with pytest.raises(NoFollowPathError):
        api.WorkspaceAssetOverlay(project).get_asset('script', 'sample')
    assert target.read_text() == 'untouched'


# LLM: 两个真实Python进程共享同一目标版本，不以线程替代跨进程锁证据。
# 函数用途: 验证同一CAS版本最多只有一个批量提交成功。
def test_asset_batch_cas_is_inside_cross_process_lock(tmp_path):
    api = asset_api()
    home = tmp_path / 'home'
    home.mkdir()
    api.commit_asset_batch([asset_item()], owner_home=home)
    program = '''
import json,sys
from pathlib import Path
from agent_py_agent.agent.assets.workspace_assets import commit_asset_batch
item=json.loads(sys.argv[2])
try:
    commit_asset_batch([item], owner_home=Path(sys.argv[1]))
    print('committed')
except ValueError as exc:
    if 'ASSET_REVISION_CONFLICT' not in str(exc):
        raise
    print('conflict')
'''
    item = json.dumps(asset_item(expected_revision=1))
    workers = [subprocess.Popen([sys.executable, '-c', program, str(home), item], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True) for _ in range(2)]
    outcomes = [worker.communicate(timeout=20) for worker in workers]
    assert [worker.returncode for worker in workers] == [0, 0], outcomes
    assert sorted(out.strip() for out, _ in outcomes) == ['committed', 'conflict']
    assert ScriptStore(home).get_script('sample')['revision'] == 2


# LLM: 低优先级坏头部不能击穿已命中覆盖，项目技能根链接则须明确失败而非静默空列表。
# 函数用途: 验证Skill遮蔽与nofollow的组合边界。
def test_asset_skill_shadowed_corruption_and_symlink_rejection(tmp_path):
    api = asset_api()
    home, builtin, project = (tmp_path / name for name in ('home', 'builtin', 'project'))
    for root in (home, builtin, project):
        root.mkdir()
    for root in (home, builtin):
        api.commit_asset_batch([asset_item('skill')], owner_home=root)
    (builtin / 'skills/sample/SKILL.md').write_text('---\nname: [broken\n---\n')
    overlay = api.WorkspaceAssetOverlay(project, owner_home=home, builtin_roots=(builtin,))
    assert overlay.list_assets('skill')[0]['source_scope'] == 'owner'
    (project / '.agent').mkdir()
    (project / '.agent/skills').symlink_to(home / 'skills', target_is_directory=True)
    with pytest.raises(NoFollowPathError):
        overlay.list_assets('skill')


# LLM: 在暂存阶段注入IO故障，正式资产及全部暂存须清空。
# 函数用途: 验证第一阶段失败没有任何正式发表。
def test_asset_batch_failure_during_staging_has_no_formal_assets(tmp_path, monkeypatch):
    api = asset_api()
    home = tmp_path / 'home'
    home.mkdir()
    original = api.write_text_atomic_beneath
    failed = False

    # LLM: 只破坏第二阶段之前的暂存IO，不移除校验或绕过正式读写。
    # 函数用途: 模拟磁盘错误导致阶段一失败。
    def asset_stage_failure(root, parts, text, **options):
        nonlocal failed
        if parts[0] == 'workflows' and '.stage-' in parts[-1] and not failed:
            failed = True
            raise OSError('synthetic stage failure')
        return original(root, parts, text, **options)

    monkeypatch.setattr(api, 'write_text_atomic_beneath', asset_stage_failure)
    with pytest.raises(OSError, match='synthetic stage failure'):
        api.commit_asset_batch([asset_item(), asset_item('workflow', 'flow')], owner_home=home)
    assert not (home / 'scripts/sample/script.json').exists()
    assert not (home / 'workflows/flow/workflow.json').exists()
    assert not list(home.rglob('*.stage-*'))
    assert not (home / '.asset-batch.json').exists()


# LLM: 子进程直接退出不执行except，下一次原Store入口必须恢复整包而不是只修本集合。
# 函数用途: 验证持久化undo跨进程中断恢复及冷读拒绝混合版本。
def test_asset_batch_process_death_recovers_all_kinds(tmp_path):
    api = asset_api()
    home = tmp_path / 'home'
    home.mkdir()
    api.commit_asset_batch([asset_item()], owner_home=home)
    program = '''
import json,os,sys
from pathlib import Path
import agent_py_agent.agent.assets.workspace_assets as assets
original=assets._put_text
# LLM: 在真实第一项发表后退出子进程，故意不运行Python回滚钩子。
# 函数用途: 模拟主机进程中断留下批量undo。
def asset_exit_after_publish(root,parts,content):
    original(root,parts,content)
    if parts==('scripts','sample','script.json'):
        os._exit(23)
assets._put_text=asset_exit_after_publish
assets.commit_asset_batch(json.loads(sys.argv[2]),owner_home=Path(sys.argv[1]))
'''
    items = [asset_item(code='print("changed")\n'), asset_item('workflow', 'flow'),
             asset_item('skill', 'guide'), asset_item('monitor', 'watch')]
    result = subprocess.run([sys.executable, '-c', program, str(home), json.dumps(items)], capture_output=True, timeout=20)
    assert result.returncode == 23, result.stderr.decode()
    assert (home / '.asset-batch.json').exists()
    with pytest.raises(ValueError, match='批量事务未提交'):
        api.WorkspaceAssetOverlay(None, owner_home=home).list_assets()
    restored = ScriptStore(home).get_script('sample')
    assert restored['code'] == 'print("ok")\n' and restored['revision'] == 1
    assert not (home / 'workflows/flow/workflow.json').exists()
    assert not (home / 'skills/guide/SKILL.md').exists()
    assert not (home / 'data/monitors/watch.json').exists()
    assert not (home / '.asset-batch.json').exists()


# LLM: Scheduler deleted是原调度事实，旧扩展绑定不能把它重新列为有效监控。
# 函数用途: 验证Owner监控沿唯一Scheduler事实排除删除项。
def test_asset_monitor_deleted_job_is_not_resurrected_by_binding(tmp_path):
    api = asset_api()
    home = tmp_path / 'home'
    home.mkdir()
    api.commit_asset_batch([asset_item('monitor')], owner_home=home)
    scheduler = home / 'data/scheduler'
    scheduler.mkdir()
    (scheduler / 'store.json').write_text(json.dumps({'jobs': {'sample': {'job_id': 'sample', 'status': 'deleted'}}}))
    assert api.WorkspaceAssetOverlay(None, owner_home=home).list_assets('monitor') == []


# LLM: 重复身份的预检必须在.agent和锁创建前发生，兼顾大小写不敏感文件系统。
# 函数用途: 验证重复四类资产均被整包拒绝。
@pytest.mark.parametrize('kind', ['script', 'workflow', 'skill', 'monitor'])
def test_asset_duplicate_casefold_ids_rejected_before_workspace_creation(tmp_path, kind):
    api = asset_api()
    project = tmp_path / 'project'
    project.mkdir()
    with pytest.raises(ValueError, match='重复'):
        api.commit_asset_batch([asset_item(kind, 'same'), asset_item(kind, 'SAME')],
                               scope='workspace', workspace_root=project, owner_home=tmp_path / 'home')
    assert not (project / '.agent').exists()


# LLM: 字节级比较项目原资产与二进制资源，防止晋升丢失模板或改写源。
# 函数用途: 验证完整Skill资源复制，不把二进制当UTF8文本。
@pytest.mark.parametrize('skill_dir', ['sample', '中文技能'])
def test_asset_promote_skill_copies_binary_resources_without_rewriting_source(tmp_path, skill_dir):
    api = asset_api()
    home, project = tmp_path / 'home', tmp_path / 'project'
    home.mkdir()
    project.mkdir()
    api.commit_asset_batch([asset_item('skill')], scope='workspace', workspace_root=project, owner_home=home)
    if skill_dir != 'sample':
        (project / '.agent/skills/sample').rename(project / '.agent/skills' / skill_dir)
    identifier = skill_asset_id(skill_dir)
    templates = project / '.agent/skills' / skill_dir / 'templates'
    templates.mkdir()
    content = bytes(range(256))
    (templates / 'sample.bin').write_bytes(content)
    source = {str(path.relative_to(project)): path.read_bytes() for path in project.rglob('*') if path.is_file()}
    api.promote_to_owner('skill', identifier, workspace_root=project, owner_home=home, expected_revision=0)
    assert (home / 'skills' / identifier / 'templates/sample.bin').read_bytes() == content
    assert {str(path.relative_to(project)): path.read_bytes() for path in project.rglob('*') if path.is_file()} == source


# LLM: 回滚失败不能销毁undo，故障解除后仍由原Store恢复，不允许手工补数据。
# 函数用途: 验证二次故障保留恢复责任与最终一致旧版本。
def test_asset_batch_keeps_undo_if_rollback_fails_then_recovers(tmp_path, monkeypatch):
    api = asset_api()
    home = tmp_path / 'home'
    home.mkdir()
    api.commit_asset_batch([asset_item()], owner_home=home)
    original = api.write_text_atomic_beneath

    # LLM: 同时破坏后项发表和旧代码回滚，必须保留整包undo供下一次原Store入口恢复。
    # 函数用途: 构造不能靠当前except完成恢复的故障现场。
    def asset_fail_publish_and_undo(root, parts, text, **options):
        if parts == ('workflows', 'flow', 'workflow.json') or parts == ('scripts', 'sample', 'main.py') and text == 'print("ok")\n':
            raise OSError('synthetic rollback failure')
        return original(root, parts, text, **options)

    monkeypatch.setattr(api, 'write_text_atomic_beneath', asset_fail_publish_and_undo)
    with pytest.raises(OSError, match='synthetic rollback failure'):
        api.commit_asset_batch([asset_item(code='print("changed")\n'), asset_item('workflow', 'flow')], owner_home=home)
    assert (home / '.asset-batch.json').exists()
    with pytest.raises(ValueError, match='批量事务未提交'):
        api.WorkspaceAssetOverlay(None, owner_home=home).get_asset('script', 'sample')
    monkeypatch.setattr(api, 'write_text_atomic_beneath', original)
    assert ScriptStore(home).get_script('sample')['code'] == 'print("ok")\n'
    assert not (home / '.asset-batch.json').exists()
    assert not (home / 'workflows/flow/workflow.json').exists()


# LLM: 独立进程只打开原根锁和Skill锁，非阻塞探测后立即释放，不出生锁或改锁协议。
# 函数用途: 在资源冻结或目标提交边界取得实际OS锁观察。
def asset_source_lock_observation(root):
    program = '''
import json,os,sys
from pathlib import Path
from agent_py_agent.agent.common.json_io import _flock_descriptor, _funlock_descriptor
root=Path(sys.argv[1])
observed={}
for key,parts in [('root',('.asset-batch.lock',)),('skill',('skills','sample','skill_meta.json.lock'))]:
    fd=os.open(root.joinpath(*parts),os.O_RDONLY)
    try:
        try:
            _flock_descriptor(fd,blocking=False)
        except BlockingIOError:
            observed[key]='held'
        else:
            observed[key]='free'
            _funlock_descriptor(fd)
    finally:
        os.close(fd)
print(json.dumps(observed))
'''
    result = subprocess.run([sys.executable, '-c', program, str(root)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# LLM: 正文和附件只由正式批量入口写；探针在真实根锁尝试前通知，随后无条件调用原OS锁。
# 函数用途: 启动正常并发Skill写者，并有界等待它到达事务锁入口。
def asset_start_skill_writer(home, project, item):
    import queue
    import threading

    program = '''
import json,sys
from pathlib import Path
import agent_py_agent.agent.common.json_io as io
from agent_py_agent.agent.assets.workspace_assets import commit_asset_batch
original=io._flock_descriptor
announced=False
# LLM: 只观察首次锁尝试，不替代原事务锁或绕过正常批量入口。
# 函数用途: 通知测试写者已经到达根锁。
def announce(fd,**options):
    global announced
    if not announced:
        announced=True
        print('root-lock-attempt',flush=True)
    return original(fd,**options)
io._flock_descriptor=announce
result=commit_asset_batch([json.loads(sys.argv[3])],scope='workspace',workspace_root=Path(sys.argv[2]),owner_home=Path(sys.argv[1]))
print(json.dumps(result))
'''
    worker = subprocess.Popen([sys.executable, '-c', program, str(home), str(project), json.dumps(item)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    lines = queue.Queue()
    threading.Thread(target=lambda: lines.put(worker.stdout.readline()), daemon=True).start()
    try:
        assert lines.get(timeout=20).strip() == 'root-lock-attempt'
    except BaseException:
        worker.kill()
        worker.communicate(timeout=20)
        raise
    return worker


# LLM: 用实际锁状态安排并发，不靠sleep猜时序；真实写者和Owner提交均执行正常批量入口。
# 函数用途: 复现A2旧正文配新附件，并验证同源快照及先释放来源锁再提交Owner。
def test_asset_promote_skill_freezes_body_revision_and_resources_before_owner_lock(tmp_path, monkeypatch):
    api = asset_api()
    home, project = tmp_path / 'home', tmp_path / 'project'
    home.mkdir()
    project.mkdir()
    versions = {version: asset_item('skill', body_markdown=f'---\nname: sample\ndescription: 测试技能\n---\n{version}\n',
                files={'references/version.txt': version, 'templates/version.bin': {'data_base64': binary}})
                for version, binary in [('old', '/29sZA=='), ('new', '/25ldw==')]}
    api.commit_asset_batch([versions['old']], scope='workspace', workspace_root=project, owner_home=home)
    original_resources, original_commit = api._skill_resources, api.commit_asset_batch
    workers, observations, completed = [], [], []

    # LLM: 正常退出才记完成；锁序死锁或写入失败保留真实失败，不吞掉。
    # 函数用途: 等待写者完成并校验新的来源revision。
    def finish_writer():
        if not completed:
            stdout, stderr = workers[0].communicate(timeout=20)
            assert workers[0].returncode == 0, stderr
            assert json.loads(stdout)['items'][0]['revision'] == 2
            completed.append(True)

    # LLM: 旧实现的来源锁已释放时让正常写者完成，稳定复现混版；修复后保留锁内读取。
    # 函数用途: 在资源冻结前安排并发更新。
    def freeze_with_concurrent_writer(*args):
        observation = asset_source_lock_observation(project / '.agent')
        observations.append(observation)
        workers.append(asset_start_skill_writer(home, project, versions['new'] | {'expected_revision': 1}))
        if observation['root'] == 'free':
            finish_writer()
        return original_resources(*args)

    # LLM: 来源写者必须能在目标事务前完成，两把来源锁均释放，防止跨根持锁的ABBA修法。
    # 函数用途: 在真实Owner提交前验证来源锁释放。
    def commit_after_source_unlock(items, **options):
        if workers:
            finish_writer()
            assert asset_source_lock_observation(project / '.agent') == {'root': 'free', 'skill': 'free'}
        return original_commit(items, **options)

    monkeypatch.setattr(api, '_skill_resources', freeze_with_concurrent_writer)
    monkeypatch.setattr(api, 'commit_asset_batch', commit_after_source_unlock)
    try:
        api.promote_to_owner('skill', 'sample', workspace_root=project, owner_home=home, expected_revision=0)
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
                worker.communicate(timeout=20)
    record = api.WorkspaceAssetOverlay(None, owner_home=home).get_asset('skill', 'sample')
    observed = (record['body_markdown'], record['metadata']['promoted_from_revision'],
                (home / 'skills/sample/references/version.txt').read_text(),
                (home / 'skills/sample/templates/version.bin').read_bytes())
    print('A2 observed:', repr(observed), 'source locks during resource freeze:', observations)
    assert observed == (versions['old']['body_markdown'], 1, 'old', b'\xffold')
    assert observations == [{'root': 'held', 'skill': 'held'}]
    monkeypatch.setattr(api, '_skill_resources', original_resources)
    api.promote_to_owner('skill', 'sample', workspace_root=project, owner_home=home, expected_revision=1)
    record = api.WorkspaceAssetOverlay(None, owner_home=home).get_asset('skill', 'sample')
    assert (record['body_markdown'], record['metadata']['promoted_from_revision'], record['revision']) == (
        versions['new']['body_markdown'], 2, 2)
    assert (home / 'skills/sample/references/version.txt').read_text() == 'new'
    assert (home / 'skills/sample/templates/version.bin').read_bytes() == b'\xffnew'


# LLM: 复用正式资产草稿格式，只在临时根提交，不注册或启动Scheduler。
# 函数用途: 为监控跨入口反例构造同ID、不同名称的合法批量项。
def wp11_monitor_item(name):
    return asset_item('monitor', 'watch', descriptor=AssetDescriptor('watch', 'monitor', name).to_dict())


# LLM: 来源是c4独立A1红反例；先正常批量写到revision=2，再检查旧入口返回与磁盘版本共同递增。
# 函数用途: 防止监控旧写入口重置跨入口版本。
def test_a1_legacy_save_must_increment_existing_revision(tmp_path):
    from agent_py_agent.agent.assets import monitor_adapter

    api, home = asset_api(), tmp_path / 'home'
    home.mkdir()
    api.commit_asset_batch([wp11_monitor_item('first')], owner_home=home)
    api.commit_asset_batch([wp11_monitor_item('second') | {'expected_revision': 1}], owner_home=home)
    path = home / 'data/monitors/watch.json'
    assert json.loads(path.read_text())['revision'] == 2
    result = monitor_adapter.save_monitor_binding(home, 'watch', name='acknowledged',
        schedule_spec={'kind': 'every', 'every_seconds': 120})
    after = json.loads(path.read_text())
    assert after['name'] == result['name'] == 'acknowledged'
    assert after['revision'] == result['revision'] == 3
    assert monitor_adapter.get_monitor_descriptor(home, 'watch')['revision'] == 3


# LLM: 来源是c4独立A1红反例；真实子进程在发表后退出，不改undo或恢复协议，后续只走正常入口。
# 函数用途: 防止恢复旧批量事务回滚已经确认的监控写入。
def test_a1_recovery_must_preserve_acknowledged_monitor_write(tmp_path):
    from agent_py_agent.agent.assets import monitor_adapter

    api, home = asset_api(), tmp_path / 'home'
    home.mkdir()
    script = {'descriptor': AssetDescriptor('anchor', 'script', 'anchor').to_dict(), 'code': 'pass\n'}
    api.commit_asset_batch([wp11_monitor_item('baseline'), script], owner_home=home)
    program = '''
import json,os,sys
from pathlib import Path
import agent_py_agent.agent.assets.workspace_assets as assets
original=assets._put_text
# LLM: 只在真实监控叶发表后中断，原锁与undo仍由产品建立。
# 函数用途: 留下可由正常入口恢复的批量中断现场。
def crash_after_monitor(root,parts,content):
    original(root,parts,content)
    if parts==('data','monitors','watch.json'):
        os._exit(23)
assets._put_text=crash_after_monitor
assets.commit_asset_batch(json.loads(sys.argv[2]),owner_home=Path(sys.argv[1]))
'''
    items = [wp11_monitor_item('uncommitted') | {'expected_revision': 1}, script]
    worker = subprocess.run([sys.executable, '-c', program, str(home), json.dumps(items)],
                            capture_output=True, text=True, timeout=20)
    assert worker.returncode == 23, worker.stderr
    journal, path = home / '.asset-batch.json', home / 'data/monitors/watch.json'
    assert journal.exists()
    crashed = json.loads(path.read_text())
    assert (crashed['name'], crashed['revision']) == ('uncommitted', 2)
    result = monitor_adapter.save_monitor_binding(home, 'watch', name='acknowledged-after-crash',
        schedule_spec={'kind': 'every', 'every_seconds': 180})
    saved = json.loads(path.read_text())
    assert saved['name'] == result['name'] == 'acknowledged-after-crash'
    assert not journal.exists()
    assert ScriptStore(home).get_script('anchor')['code'] == 'pass\n'
    assert json.loads(path.read_text()) == saved


# LLM: CAS只作为控制参数，旧版本冲突必须在写前失败；以真实文件字节证明未覆盖已确认记录。
# 函数用途: 验证监控新建、显式版本更新及迟到写入拒绝。
def test_wp11_monitor_expected_revision_is_cas_not_metadata(tmp_path):
    from agent_py_agent.agent.assets import monitor_adapter

    first = monitor_adapter.save_monitor_binding(tmp_path, 'watch', name='first')
    assert first['revision'] == 1
    second = monitor_adapter.save_monitor_binding(tmp_path, 'watch', name='second', expected_revision=1)
    assert second['revision'] == 2 and 'expected_revision' not in second
    path = tmp_path / 'data/monitors/watch.json'
    before = path.read_bytes()
    with pytest.raises(ValueError, match='ASSET_REVISION_CONFLICT'):
        monitor_adapter.save_monitor_binding(tmp_path, 'watch', name='late', expected_revision=1)
    assert path.read_bytes() == before


# LLM: 本地管理员HomePaths不能覆盖远程config事实；即使目录在家内也应拒绝主机目录覆盖请求。
# 函数用途: 防止远程身份借本地管理员HomePaths绕过校验。
@pytest.mark.parametrize('provider', ['feishu', ' REMOTE '])
def test_wp11_gateway_workspace_rejects_remote_config_with_admin_home(tmp_path, provider):
    agent = dir_agent(tmp_path)
    agent.config.my_agent_owner_provider = provider
    with pytest.raises(GatewayWorkspaceScopeError, match='远程 owner'):
        gateway_request_workspace_scope(agent, {'workspace': {'cwd': str(agent.root)}})


# LLM: 显式非管理员config必须缩小HomePaths给出的管理员候选，不从工具墙或磁盘位置抬升权限。
# 函数用途: 核对普通用户和群配置与管理员HomePaths冲突时仍拒绝外部目录。
@pytest.mark.parametrize('kind', ['user', 'group', ' USER '])
def test_wp11_gateway_workspace_nonadmin_config_overrides_admin_home(tmp_path, kind):
    agent = dir_agent(tmp_path)
    agent.config.my_agent_owner_kind = kind
    work = tmp_path / 'project'
    work.mkdir()
    with pytest.raises(GatewayWorkspaceScopeError, match='普通用户'):
        gateway_request_workspace_scope(agent, {'workspace': {'cwd': str(work)}})


# LLM: 只隔离Agent物化和无关活动任务查询；鉴权、命令分派及真实ConversationStore/领域写入均不替换。
# 函数用途: 为公共Gateway目录与分叉路由准备认证scope和独立owner存储。
def wp11_gateway_case(tmp_path, monkeypatch, *, kind='main'):
    from unittest.mock import MagicMock

    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.gateway_parts import control_service
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    host = dir_agent(tmp_path, kind=kind)
    store = ConversationStore(host.root / 'conversations')
    owner_agent = SimpleNamespace(home_paths=host.home_paths, conversation_store=store)
    host.conversation_store = object()
    factory = MagicMock(return_value=owner_agent)
    monkeypatch.setattr(control_service, '_request_agent_for_scope', factory)
    monkeypatch.setattr(control_service, '_active_control_target', lambda *_args: None)
    identity = OwnerIdentity.local_main() if kind == 'main' else OwnerIdentity.local_user('bob')
    scope = control_service.GatewayControlScope('authenticated-sender', 'chat', 'session-one', resolved_owner=identity)
    return SimpleNamespace(host=host, owner=owner_agent, store=store, factory=factory, scope=scope)


# LLM: 按真实通道三元组绑定线程，cwd始终是临时owner里的合法项目；不调用Gateway待测分支建立夹具。
# 函数用途: 准备指定认证会话的目录及消息存储。
def wp11_bound_thread(case, *, conversation_id=None):
    work = case.owner.home_paths.owner_home_dir / 'project'
    work.mkdir(exist_ok=True)
    return case.store.threads.get_or_create({'canonical_user_id': case.scope.user_id,
        'channel': case.scope.channel, 'channel_conversation_id': conversation_id or case.scope.conversation_id,
        'channel_user_id': case.scope.user_id, 'owner_home': str(case.owner.home_paths.owner_home_dir), 'cwd': str(work)})


# LLM: 首次dir只允许在认证owner的Store创建准确通道线程；缺HomePaths时仍须沿Store根回退。
# 函数用途: 验证Gateway目录查询首次接线及家目录解析。
@pytest.mark.parametrize('fallback_home', [False, True])
def test_wp11_gateway_dir_creates_only_authenticated_thread(tmp_path, monkeypatch, fallback_home):
    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
    from agent_py_agent.agent.gateway_parts.control_service import (
        execute_gateway_conversation_control,
    )

    case = wp11_gateway_case(tmp_path, monkeypatch)
    home = case.owner.home_paths.owner_home_dir
    if fallback_home:
        del case.owner.home_paths
    result = execute_gateway_conversation_control(case.host, None, parse_conversation_control('/dir'), case.scope)
    assert result.ok, result
    thread = case.store.threads.resolve(channel=case.scope.channel, channel_conversation_id=case.scope.conversation_id,
                                       channel_user_id=case.scope.user_id)
    assert thread.thread_id == result.details['thread_id']
    assert thread.owner_home == str(home) and thread.canonical_user_id == case.scope.user_id
    assert len(case.store.threads.list()) == 1
    case.factory.assert_called_once_with(case.host, case.scope)


# LLM: 公共控制入口必须只切换认证通道绑定，不得按最新会话或基础Agent的Store误写另一个窗口。
# 函数用途: 核对Gateway目录切换与查询使用同一选定线程。
def test_wp11_gateway_dir_switches_only_bound_session(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
    from agent_py_agent.agent.gateway_parts.control_service import (
        execute_gateway_conversation_control,
    )

    case = wp11_gateway_case(tmp_path, monkeypatch)
    selected, other = wp11_bound_thread(case), wp11_bound_thread(case, conversation_id='session-two')
    work = tmp_path / 'next-project'
    work.mkdir()
    result = execute_gateway_conversation_control(case.host, None, parse_conversation_control(f'/dir {work}'), case.scope)
    assert result.ok and result.details['thread_id'] == selected.thread_id
    assert case.store.threads.require(selected.thread_id).cwd == str(work)
    assert case.store.threads.require(other.thread_id).cwd == other.cwd
    queried = execute_gateway_conversation_control(case.host, None, parse_conversation_control('/dir'), case.scope)
    assert queried.ok and queried.details['workspace_root'] == str(work)


# LLM: 原命令目录鉴权先于owner物化；普通用户dir不能通过新胶水分支绕过管理员要求。
# 函数用途: 证明拒绝发生在创建会话或访问owner Agent之前。
def test_wp11_gateway_dir_nonadmin_denied_before_materialization(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
    from agent_py_agent.agent.gateway_parts.control_service import (
        execute_gateway_conversation_control,
    )

    case = wp11_gateway_case(tmp_path, monkeypatch, kind='user')
    result = execute_gateway_conversation_control(case.host, None, parse_conversation_control('/dir'), case.scope)
    assert not result.ok and result.error_code == 'COMMAND_ADMIN_REQUIRED'
    case.factory.assert_not_called()
    assert case.store.threads.list() == []


# LLM: 管理员和普通身份均使用真实分叉服务；身份只由认证scope提供，源线程字节不得被分叉改写。
# 函数用途: 验证Gateway分叉路由保留消息与源会话，并使用当前owner Store。
@pytest.mark.parametrize('kind', ['main', 'user'])
def test_wp11_gateway_fork_routes_scoped_store_without_changing_source(tmp_path, monkeypatch, kind):
    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
    from agent_py_agent.agent.gateway_parts.control_service import (
        execute_gateway_conversation_control,
    )

    case = wp11_gateway_case(tmp_path, monkeypatch, kind=kind)
    thread = wp11_bound_thread(case)
    for role in ('user', 'assistant'):
        case.store.messages.append({'thread_id': thread.thread_id, 'role': role, 'content': role + ' original'})
    path = case.store.storage.thread_path(thread.thread_id)
    before = path.read_bytes()
    result = execute_gateway_conversation_control(case.host, None,
        parse_conversation_control('/fork --title "分叉标题"'), case.scope)
    assert result.ok, result
    branch = case.store.threads.require(result.details['new_thread_id'])
    assert branch.forked_from_thread_id == thread.thread_id and branch.title == '分叉标题'
    assert [row.content for row in case.store.messages.recent(branch.thread_id, limit=0)] == ['user original', 'assistant original']
    assert path.read_bytes() == before
    case.factory.assert_called_once_with(case.host, case.scope)


# LLM: 空会话fork必须直接返回结构化错误，不能像首次dir那样出生一个空线程再伪称已分叉。
# 函数用途: 验证没有源会话时不写会话绑定。
def test_wp11_gateway_fork_without_thread_never_creates_one(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
    from agent_py_agent.agent.gateway_parts.control_service import (
        execute_gateway_conversation_control,
    )

    case = wp11_gateway_case(tmp_path, monkeypatch)
    result = execute_gateway_conversation_control(case.host, None, parse_conversation_control('/fork'), case.scope)
    assert not result.ok and result.error_code == 'WORKSPACE_CONTROL_INVALID'
    assert result.message == '当前还没有可分叉的会话。'
    assert case.store.threads.list() == []
