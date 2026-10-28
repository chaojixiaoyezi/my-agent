# LLM: Exercise real client/session/storage paths; replace only interactive, network and daemon boundaries, never owner authorization.
# 模块用途: 回归轻量resume、只读恢复、预检释放及CLI延迟MCP，不连接真实模型或通道。
from __future__ import annotations

import inspect
import subprocess
import sys
import weakref
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_py_agent.tests.support.ci_authenticated_gateway import seed_gateway_credential


# LLM: Each fixture owns config/home below pytest tmp; no external credentials or service state are consumed.
# 函数用途: 用真实配置与轻客户端创建独立用户测试环境。
def _client(tmp_path, monkeypatch, *, conversation="", access="full-access", workspace=False):
    from agent_py_agent.cli.chat_client_context import make_gateway_chat_client

    home = tmp_path / "home"
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    monkeypatch.delenv("MY_AGENT_RUNTIME_CONFIG", raising=False)
    monkeypatch.delenv("MY_AGENT_RUNTIME_CONFIG_LAYERS", raising=False)
    seed_gateway_credential(home)
    config = tmp_path / "agent.yaml"
    config.write_text(f'model_backend: echo\naccess_mode: {access}\nconversation_workspace: "{conversation}"\n', encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    args = Namespace(config=str(config), workspace_root=str(project) if workspace else "",
                     session_id="resume-light", gateway=True, plain=False)
    return args, make_gateway_chat_client(args)


@pytest.mark.parametrize("missing", [False, True])
# LLM: A fresh interpreter observes both registered/missing resume hot paths and its live caller frame, not merely a patched constructor flag.
# 函数用途: 确认正常Gateway恢复不导入core，且进入聊天前释放真实预检客户端和manager局部变量。
def test_gateway_resume_is_cold_lightweight_and_releases_precheck(tmp_path, monkeypatch, missing):
    args, client = _client(tmp_path, monkeypatch)
    if missing:
        from agent_py_agent.agent.conversation.store import ConversationStore

        ConversationStore(client.config.conversation_workspace).threads.get_or_create({
            "canonical_user_id": "local-agent", "channel": "chat", "channel_user_id": "local-agent",
            "channel_conversation_id": args.session_id, "owner_id": client.home_paths.owner_id,
            "owner_home": str(client.home_paths.owner_home_dir), "title": "missing registration",
        })
    script = r'''
import inspect, sys, weakref
from argparse import Namespace
from agent_py_agent.cli import chat
from agent_py_agent.cli.chat_client_context import GatewayChatClientAgent, make_gateway_chat_client
from agent_py_agent.agent.session.manager import SessionManager
args = Namespace(config=sys.argv[1], workspace_root="", gateway=True, plain=False)
client = make_gateway_chat_client(args)
manager = SessionManager(client.config)
args.session_id = "resume-light" if sys.argv[2] == "missing" else manager.create_session(channel="chat").session_id
del client, manager
assert "agent_py_agent.agent.core" not in sys.modules
original = chat.make_agent
references = []
# LLM: Delegate to the real route; retain only weak references to observe precheck lifetime.
# 函数用途: 记录真实构造结果而不替代路由或持有客户端。
def construct(selected):
    client = original(selected)
    assert isinstance(client, GatewayChatClientAgent), "resume constructed a full agent"
    assert selected._gateway_client_mode is True
    references.append(weakref.ref(client))
    return client
# LLM: Interactive boundary only; inspect the still-live resume frame and actual reference lifetime.
# 函数用途: 在进入聊天时验证预检对象已释放且没有加载core。
def enter_chat(selected):
    assert references and references[0]() is None, "precheck agent retained by resume frame"
    previous = inspect.currentframe().f_back.f_locals
    assert "agent" not in previous and "manager" not in previous
    assert "agent_py_agent.agent.core" not in sys.modules
    return 17
chat.make_agent = construct
chat._has_prompt_toolkit = lambda: True
chat.cmd_chat = enter_chat
assert chat.cmd_resume(args) == 17
'''
    result = subprocess.run([sys.executable, "-c", script, args.config, "missing" if missing else "registered"],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


# LLM: Snapshot immutable fixture contents/modes, excluding atime which legitimate reads may change.
# 函数用途: 记录会话库内容与权限，以发现恢复读路径的迁移或修复写入。
def _storage_snapshot(root):
    return {str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mode)
            for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize("conversation", ["", "relative-conversations", "absolute"])
@pytest.mark.parametrize("access,workspace", [("full-access", True), ("restricted", False)])
# LLM: Compare with the original SimpleAgent authority, including explicit project roots, relative overrides and WorkspaceOnly.
# 函数用途: 让真实薄客户端按完整Agent同一根只读恢复会话，禁止挂载conversation_store属性。
def test_missing_registration_reads_exact_formal_store_without_mutation(tmp_path, monkeypatch, conversation, access, workspace):
    from agent_py_agent.agent.conversation import store as store_module
    from agent_py_agent.agent.session.manager import SessionManager
    from agent_py_agent.cli import chat, common

    if conversation == "absolute":
        conversation = str(tmp_path / "selected-conversations")
    args, client = _client(tmp_path, monkeypatch, conversation=conversation, access=access, workspace=workspace)
    args._defer_mcp_connect = True
    host = common.make_agent(args)
    root = host.conversation_store.storage.root
    assert root == Path(client.config.conversation_workspace)
    host.conversation_store.threads.get_or_create({
        "canonical_user_id": "local-agent", "channel": "chat", "channel_user_id": "local-agent",
        "channel_conversation_id": args.session_id, "owner_id": client.home_paths.owner_id,
        "owner_home": str(client.home_paths.owner_home_dir), "title": "resume fixture",
    })
    before = _storage_snapshot(root)
    original = store_module.ConversationStore
    opened = []

    # LLM: Preserve the real store and resolver; only capture initialization options at the lazy import seam.
    # 函数用途: 观察恢复存储以initialize=False打开，不替换真实线程校验。
    def observe(*args, **kwargs):
        opened.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(store_module, "ConversationStore", observe)
    manager = SessionManager(client.config)
    try:
        assert not manager.session_exists(args.session_id)
        assert chat._restore_missing_session_registration(client, manager, args.session_id)
        assert manager.load_session(args.session_id).user_id == client.config.user_id
        assert len(opened) == 1 and opened[0][1].get("initialize") is False
        selected_root = opened[0][1].get("root", next(iter(opened[0][0]), None))
        assert Path(selected_root) == root
        assert _storage_snapshot(root) == before
        with pytest.raises(AttributeError):
            _ = client.conversation_store
    finally:
        host.tools.close_mcp_clients()


@pytest.mark.parametrize("fault", ["owner", "home", "binding", "missing"])
# LLM: Invalid owner/home/binding facts cannot authorize session registration; a missing store must stay absent.
# 函数用途: 确认只读恢复保留原跨用户失败关闭合同，不创建不存在的会话库。
def test_lightweight_recovery_denies_foreign_or_missing_threads(tmp_path, monkeypatch, fault):
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.session.manager import SessionManager
    from agent_py_agent.cli import chat

    args, client = _client(tmp_path, monkeypatch)
    root = Path(client.config.conversation_workspace)
    if fault != "missing":
        ConversationStore(root).threads.get_or_create({
            "canonical_user_id": "local-agent", "channel": "chat", "channel_user_id": "local-agent",
            "channel_conversation_id": "other-session" if fault == "binding" else args.session_id,
            "owner_id": "foreign/owner" if fault == "owner" else client.home_paths.owner_id,
            "owner_home": str(tmp_path / "foreign") if fault == "home" else str(client.home_paths.owner_home_dir),
            "title": "negative fixture",
        })
    before = _storage_snapshot(root)
    existed = root.exists()
    manager = SessionManager(client.config)
    assert not chat._restore_missing_session_registration(client, manager, args.session_id)
    assert not manager.session_exists(args.session_id)
    assert root.exists() == existed and _storage_snapshot(root) == before


@pytest.mark.parametrize("failure", ["", "missing", "owner", "manager"])
# LLM: Direct precheck may still construct a full agent; all exits close its MCP ownership and success releases locals before chat.
# 函数用途: 验证直连resume延迟连接并在成功、拒绝和初始化失败时收尾预检对象。
def test_direct_resume_defers_and_closes_precheck_on_all_exits(monkeypatch, failure):
    from agent_py_agent.agent.session import manager as manager_module
    from agent_py_agent.cli import chat

    close = Mock()
    references = []
    args = Namespace(session_id="known", gateway=False, plain=True)
    manager = SimpleNamespace(session_exists=lambda _: failure != "missing",
                              load_session=lambda _: SimpleNamespace(user_id="other" if failure == "owner" else "local"))

    # LLM: Constructor boundary only; verify the private flag before returning a weak-referenceable precheck object.
    # 函数用途: 为资源生命周期回归提供最小预检代理。
    def construct(selected):
        assert getattr(selected, "_defer_mcp_connect", False) is True
        agent = Namespace(config=SimpleNamespace(user_id="local"), tools=SimpleNamespace(close_mcp_clients=close))
        references.append(weakref.ref(agent))
        return agent

    # LLM: Manager failure occurs after agent construction and must not bypass cleanup.
    # 函数用途: 注入受控manager初始化失败，覆盖异常出口。
    def open_manager(_):
        if failure == "manager":
            raise ValueError("fixture manager failure")
        return manager

    # LLM: Entering the actual interactive loop is outside this unit test; its caller frame remains inspectable.
    # 函数用途: 在交接时核对预检关闭和局部引用释放。
    def enter_chat(_):
        assert close.call_count == 1 and references[0]() is None
        assert not {"agent", "manager"}.intersection(inspect.currentframe().f_back.f_locals)
        return 19

    monkeypatch.setattr(chat, "make_agent", construct)
    monkeypatch.setattr(chat, "_has_prompt_toolkit", lambda: False)
    monkeypatch.setattr(manager_module, "SessionManager", open_manager)
    monkeypatch.setattr(chat, "_restore_missing_session_registration", lambda *_: False)
    monkeypatch.setattr(chat, "cmd_chat", enter_chat)
    assert chat.cmd_resume(args) == (3 if failure else 19)
    assert close.call_count == 1


@pytest.mark.parametrize("initial", ["absent", None, False, True])
# LLM: A temporary precheck flag must not become a permanent direct-chat policy; preserve both absence and explicit caller values.
# 函数用途: 防止direct resume把预检延迟标记泄露到正式聊天构造。
def test_direct_resume_restores_caller_mcp_policy_before_chat(monkeypatch, initial):
    from agent_py_agent.agent.session import manager as manager_module
    from agent_py_agent.cli import chat

    args = Namespace(session_id="known", gateway=False, plain=True)
    if initial != "absent":
        args._defer_mcp_connect = initial
    close = Mock()
    agent = Namespace(config=SimpleNamespace(user_id="local"), tools=SimpleNamespace(close_mcp_clients=close))
    manager = SimpleNamespace(session_exists=lambda _: True, load_session=lambda _: SimpleNamespace(user_id="local"))

    # LLM: Observe precheck construction separately from formal chat; the original flag may intentionally request deferred MCP.
    # 函数用途: 确认预检总是延迟MCP。
    def construct(selected):
        assert selected._defer_mcp_connect is True
        return agent

    # LLM: This is the formal chat boundary, not a second mocked precheck; identity and exact original flag state must survive.
    # 函数用途: 确认正式聊天收到调用者原来的MCP构造策略。
    def enter_chat(selected):
        assert selected is args
        if initial == "absent":
            assert not hasattr(selected, "_defer_mcp_connect")
        else:
            assert selected._defer_mcp_connect is initial
        return 23

    monkeypatch.setattr(chat, "make_agent", construct)
    monkeypatch.setattr(chat, "_has_prompt_toolkit", lambda: False)
    monkeypatch.setattr(manager_module, "SessionManager", lambda _: manager)
    monkeypatch.setattr(chat, "cmd_chat", enter_chat)
    assert chat.cmd_resume(args) == 23
    close.assert_called_once_with()


@pytest.mark.parametrize("alive", [True, False])
# LLM: Reused paths retain object identity through both already-running and start/wait branches; the original agent factory must not run.
# 函数用途: 验证复用规范路径不构造第二个代理，仍走原启动及等待协议。
def test_ensure_gateway_reuses_selected_paths_without_agent(tmp_path, monkeypatch, alive):
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.cli import gateway_client as cli

    assert "paths" in inspect.signature(cli.ensure_gateway_started).parameters
    paths = gateway_paths_from_root(tmp_path / "gateway")
    factory = Mock(side_effect=AssertionError("must reuse paths, not reconstruct agent"))
    observed = []
    states = iter([alive, True])

    # LLM: Process observation is the only replaced dependency; assert the selected path is transported unchanged.
    # 函数用途: 模拟一次存活观察和启动后的真实协议复查。
    def running(selected):
        assert selected is paths
        observed.append(selected)
        return 123, next(states)

    start = Mock(return_value=0)
    monkeypatch.setattr(cli, "make_agent", factory)
    monkeypatch.setattr(cli, "gateway_running", running)
    monkeypatch.setattr(cli, "cmd_gateway_start", start)
    args = Namespace(config="selected.yaml")
    assert cli.ensure_gateway_started(args, paths=paths) == 0
    factory.assert_not_called()
    assert start.call_count == (0 if alive else 1)
    assert len(observed) == (1 if alive else 2)


@pytest.mark.parametrize("path_failure", [False, True])
# LLM: Legacy callers still construct their original host agent with defer enabled and release any touched MCP even on path failure.
# 函数用途: 验证未传路径的启动预检延迟MCP并收尾其临时代理。
def test_ensure_gateway_default_path_defers_and_closes(tmp_path, monkeypatch, path_failure):
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.cli import gateway_client as cli

    close = Mock()
    agent = SimpleNamespace(tools=SimpleNamespace(close_mcp_clients=close))
    paths = gateway_paths_from_root(tmp_path / "gateway")

    # LLM: Assert at construction time rather than accepting a flag written after eager initialization.
    # 函数用途: 在make_agent边界核对延迟标记。
    def construct(selected):
        assert getattr(selected, "_defer_mcp_connect", False) is True
        return agent

    # LLM: A failed resolver must still execute precheck cleanup.
    # 函数用途: 让路径解析的正常及异常出口都可观察。
    def resolve(selected):
        assert selected is agent
        if path_failure:
            raise ValueError("fixture path failure")
        return paths

    monkeypatch.setattr(cli, "make_agent", construct)
    monkeypatch.setattr(cli, "gateway_paths", resolve)
    monkeypatch.setattr(cli, "gateway_running", lambda _: (123, True))
    if path_failure:
        with pytest.raises(ValueError, match="fixture path failure"):
            cli.ensure_gateway_started(Namespace(config="selected.yaml"))
    else:
        assert cli.ensure_gateway_started(Namespace(config="selected.yaml")) == 0
    assert close.call_count == 1


# LLM: BaseException escapes CLI presentation handlers so tests stop exactly at construction without starting hosts or adapters.
# 类用途: 在观察到构造参数后停止控制入口，避免执行真实进程副作用。
class _ConstructionObserved(BaseException):
    pass


@pytest.mark.parametrize("module,command", [
    ("gateway_client", "cmd_gateway_ask"), ("gateway_client", "cmd_gateway_result"),
    ("adapter", "cmd_adapter_start"), ("adapter", "cmd_adapter_file"),
    ("adapter", "cmd_adapter_status"), ("adapter", "cmd_adapter_stop"),
    ("gateway_process", "cmd_gateway_status"), ("gateway_process", "cmd_gateway_stop"),
    ("gateway_process", "cmd_gateway_logs"), ("gateway_process", "cmd_gateway_restart"),
    ("supervisor", "cmd_supervisor_run"), ("supervisor", "cmd_supervisor_start"),
    ("supervisor", "cmd_supervisor_status"), ("supervisor", "cmd_supervisor_stop"),
    ("supervisor", "cmd_start_all"), ("supervisor", "cmd_supervisor_install_service"),
    ("supervisor", "cmd_supervisor_uninstall_service"),
    ("supervisor", "cmd_maintenance_lock"), ("supervisor", "cmd_maintenance_unlock"),
])
# LLM: Preserve each public entrypoint's original factory; only observe whether defer precedes construction, without replacing host composition.
# 函数用途: 逐个检查管理、supervisor及adapter入口不会在构造时急连MCP。
def test_management_entrypoints_defer_before_original_factory(tmp_path, monkeypatch, module, command):
    import importlib

    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.cli import common, gateway_coordinated_run

    cli = importlib.import_module(f"agent_py_agent.cli.{module}")
    args = Namespace(config="selected.yaml", force=False)
    monkeypatch.setattr(common, "frozen_gateway_arguments", lambda selected: selected)
    monkeypatch.setattr(gateway_coordinated_run, "_cold_gateway_paths", lambda _: gateway_paths_from_root(tmp_path))
    if module == "gateway_process":
        monkeypatch.setattr(cli, "get_running_pid", lambda _: None)

    # LLM: The sentinel aborts before any daemon/transport action; missing markers fail as assertions, not incidental AttributeError.
    # 函数用途: 验证同一个args已带延迟标记，随后停止入口。
    def observe(selected):
        assert selected is args
        assert getattr(selected, "_defer_mcp_connect", False) is True
        raise _ConstructionObserved

    monkeypatch.setattr(cli, "make_agent", observe)
    with pytest.raises(_ConstructionObserved):
        getattr(cli, command)(args)


# LLM: Plain Gateway chat retains the original full-agent path but must defer before calling it.
# 函数用途: 防止--plain --gateway遗漏MCP延迟标记。
def test_plain_gateway_chat_defers_before_construction(monkeypatch):
    from agent_py_agent.cli import chat
    from agent_py_agent.cli.chat_parts import tui_upgrade_follow

    monkeypatch.setattr(chat, "_has_prompt_toolkit", lambda: True)
    monkeypatch.setattr(tui_upgrade_follow, "adopt_handoff", lambda: SimpleNamespace(child=False))

    # LLM: Do not enter the actual terminal; validate both route and defer at the unchanged public construction boundary.
    # 函数用途: 观察plain仍走完整代理接口，但不急连MCP。
    def observe(args):
        assert args._gateway_client_mode is False
        assert getattr(args, "_defer_mcp_connect", False) is True
        raise _ConstructionObserved

    monkeypatch.setattr(chat, "make_agent", observe)
    with pytest.raises(_ConstructionObserved):
        chat.cmd_chat(Namespace(gateway=True, plain=True))


@pytest.mark.parametrize("tui", [False, True])
# LLM: P0 changes prechecks and Gateway clients only; a fresh direct chat retains its original construction semantics.
# 函数用途: 防止共享模式判定顺手改变普通direct聊天的MCP连接策略。
def test_fresh_direct_chat_keeps_original_mcp_construction_policy(monkeypatch, tui):
    from agent_py_agent.cli import chat
    from agent_py_agent.cli.chat_parts import tui_upgrade_follow

    monkeypatch.setattr(chat, "_has_prompt_toolkit", lambda: tui)
    monkeypatch.setattr(tui_upgrade_follow, "adopt_handoff", lambda: SimpleNamespace(child=False))

    # LLM: Observe the original factory boundary only; never enter the actual UI or construct a backend here.
    # 函数用途: 确认新启动direct聊天未被强制设置延迟标记。
    def observe(args):
        assert args._gateway_client_mode is False
        assert not getattr(args, "_defer_mcp_connect", False)
        raise _ConstructionObserved

    monkeypatch.setattr(chat, "make_agent", observe)
    with pytest.raises(_ConstructionObserved):
        chat.cmd_chat(Namespace(gateway=False, plain=not tui))


# LLM: File adapter must transport its already-resolved paths to readiness, not call a second factory or derive another inbox.
# 函数用途: 用真实入口和选项解析验证文件adapter只构造一次并复用原gpaths。
def test_adapter_file_passes_original_paths_to_gateway_start(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.cli import adapter

    paths = gateway_paths_from_root(tmp_path / "gateway")
    agent = SimpleNamespace(config=SimpleNamespace(gateway_request_timeout=1.0))
    factory = Mock(return_value=agent)
    started = Mock(return_value=0)
    monkeypatch.setattr(adapter, "make_agent", factory)
    monkeypatch.setattr(adapter, "gateway_paths", lambda _: paths)
    monkeypatch.setattr(adapter, "_resolve_adapter_paths", lambda *_: object())
    monkeypatch.setattr(adapter, "ensure_gateway_started", started)
    monkeypatch.setattr(adapter, "_process_file_adapter_loop", lambda _: 3)
    monkeypatch.setattr(adapter, "_print_file_adapter_summary", lambda *_: None)
    args = Namespace(config="selected.yaml", once=True)
    assert adapter.cmd_adapter_file(args) == 0
    factory.assert_called_once_with(args)
    started.assert_called_once_with(args, paths=paths)


# LLM: 冷解释器以工作树根运行，避免已加载core或系统editable路径掩盖传递导入；不构造服务或调用网络。
# 函数用途: 检查六个CLI模块及common在全新进程中都不加载core。
def test_all_management_modules_remain_cold_without_core():
    script = '''
import sys
import agent_py_agent.cli.chat
import agent_py_agent.cli.chat_client_context
import agent_py_agent.cli.adapter
import agent_py_agent.cli.gateway_client
import agent_py_agent.cli.gateway_process
import agent_py_agent.cli.supervisor
import agent_py_agent.cli.common
assert 'agent_py_agent.agent.core' not in sys.modules
'''
    result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[2],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


# LLM: 仅准备显式临时home/config，不创建Home、客户端凭据或业务存储；管理上下文不得靠完整Agent初始化补齐路径。
# 函数用途: 为未替换工厂回归准备最小真实配置。
def _management_args(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "cold-home"))
    monkeypatch.delenv("MY_AGENT_RUNTIME_CONFIG", raising=False)
    monkeypatch.delenv("MY_AGENT_RUNTIME_CONFIG_LAYERS", raising=False)
    config = tmp_path / "management.yaml"
    config.write_text("model_backend: echo\nenable_plugins: false\nenable_subagents: false\n", encoding="utf-8")
    return Namespace(config=str(config), workspace_root="", force=False)


@pytest.mark.parametrize("module,command", [
    ("gateway_client", "cmd_gateway_ask"), ("gateway_client", "cmd_gateway_result"),
    ("adapter", "cmd_adapter_start"), ("adapter", "cmd_adapter_file"),
    ("adapter", "cmd_adapter_status"), ("adapter", "cmd_adapter_stop"),
    ("gateway_process", "cmd_gateway_start"), ("gateway_process", "cmd_gateway_status"),
    ("gateway_process", "cmd_gateway_stop"), ("gateway_process", "cmd_gateway_logs"),
    ("gateway_process", "cmd_gateway_restart"),
    ("supervisor", "cmd_supervisor_start"), ("supervisor", "cmd_supervisor_status"),
    ("supervisor", "cmd_supervisor_stop"), ("supervisor", "cmd_start_all"),
    ("supervisor", "cmd_supervisor_install_service"), ("supervisor", "cmd_supervisor_uninstall_service"),
    ("supervisor", "cmd_maintenance_lock"), ("supervisor", "cmd_maintenance_unlock"),
])
# LLM: 原make_agent保持未替换，只禁止真实SimpleAgent初始化；路径边界核对实际上下文后停止，绝不启动或停止真实宿主。
# 函数用途: 逐个证明管理入口能在完整Agent构造被禁止时完成真实配置和路径解析。
def test_unpatched_management_factories_never_construct_simple_agent(tmp_path, monkeypatch, module, command):
    import importlib

    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths
    from agent_py_agent.cli import common

    cli = importlib.import_module(f"agent_py_agent.cli.{module}")
    args = _management_args(tmp_path, monkeypatch)
    original = common.make_agent
    constructor = Mock(side_effect=AssertionError("SimpleAgent must not be constructed"))
    monkeypatch.setattr(SimpleAgent, "__init__", constructor)
    if module == "gateway_process":
        monkeypatch.setattr(cli, "get_running_pid", lambda _: None)

    # LLM: 使用真正的路径函数确认同一临时home下的服务地址，然后在进程/网络副作用之前终止。
    # 函数用途: 观察真实轻量上下文，既不替代配置解析也不伪造Agent。
    def observe_context(context):
        assert not isinstance(context, SimpleAgent)
        paths = gateway_paths(context)
        assert paths.root.is_relative_to(tmp_path / "cold-home")
        assert context.config.config_path == args.config
        raise _ConstructionObserved

    monkeypatch.setattr(cli, "gateway_paths", observe_context)
    with pytest.raises(_ConstructionObserved):
        getattr(cli, command)(args)
    assert common.make_agent is original
    constructor.assert_not_called()


@pytest.mark.parametrize("default_chat", [False, True])
@pytest.mark.parametrize("already_running", [False, True])
# LLM: 完整执行原ensure/default分支，保留未替换make_agent；只替换真实进程状态、启动和交互聊天边界。
# 函数用途: 验证缺省路径的存活及启动分支和裸CLI入口均不构造SimpleAgent。
def test_unpatched_default_gateway_paths_never_construct_simple_agent(tmp_path, monkeypatch, default_chat, already_running):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.cli import common, gateway_client

    args = _management_args(tmp_path, monkeypatch)
    original = common.make_agent
    constructor = Mock(side_effect=AssertionError("SimpleAgent must not be constructed"))
    monkeypatch.setattr(SimpleAgent, "__init__", constructor)
    states = iter([already_running, True])
    observed = []

    # LLM: 观察管理解析出的真实服务路径，存活状态由测试控制，不接触真实PID。
    # 函数用途: 模拟已运行或原启动完成后的就绪观察。
    def running(paths):
        assert paths.root.is_relative_to(tmp_path / "cold-home")
        observed.append(paths.root)
        return 123, next(states)

    monkeypatch.setattr(gateway_client, "gateway_running", running)
    start = Mock(return_value=0)
    chat = Mock(return_value=17)
    monkeypatch.setattr(gateway_client, "cmd_gateway_start", start)
    monkeypatch.setattr(gateway_client, "cmd_chat", chat)
    result = gateway_client.cmd_default(args) if default_chat else gateway_client.ensure_gateway_started(args)
    assert result == (17 if default_chat else 0)
    assert len(observed) == (1 if already_running else 2)
    assert start.call_count == (0 if already_running else 1)
    assert chat.call_count == int(default_chat)
    if default_chat:
        assert args.gateway is True
    assert common.make_agent is original
    constructor.assert_not_called()


@pytest.mark.parametrize("access,project_mode", [
    ("full-access", "project"), ("workspace-write", "project"),
    ("restricted", "owner"), ("full-access", "package"),
])
# LLM: 与真实原Agent对照，不复制路径公式；构造轻上下文前后不得创建home/存储，且相对服务及审计路径不漂移。
# 函数用途: 核对Full Access、owner墙和包入口下的原路径、配置与轻量管理解析一致。
def test_management_context_matches_real_agent_paths_without_initializing_home(tmp_path, monkeypatch, access, project_mode):
    from agent_py_agent.agent.gateway_parts.paths import adapter_paths, gateway_paths
    from agent_py_agent.cli import common

    args = _management_args(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    if project_mode == "package":
        (project / "__main__.py").write_text("# package-entry fixture\n", encoding="utf-8")
    args.workspace_root = str(project) if project_mode != "owner" else ""
    with Path(args.config).open("a") as config:
        config.write(f"access_mode: {access}\ngateway_workspace: gateway-relative\nadapter_workspace: adapter-relative\n"
                     "audit_log_path: audit-relative\nconversation_workspace: conversations-relative\n")
    builder = getattr(common, "make_lightweight_cli_agent_context", None)
    assert callable(builder), "a dependency-light management context resolver is required"
    home = tmp_path / "cold-home"
    assert not home.exists()
    context = builder(args)
    assert not home.exists(), "path resolution must not initialize owner services"
    args._defer_mcp_connect = True
    host = common.make_agent(args)
    try:
        assert context.root == host.root and context.workspace_roots == host.workspace_roots
        assert context.home_paths == host.home_paths
        assert context.workspace == host.effective_workspace_root
        assert context.paths == gateway_paths(context) == gateway_paths(host)
        assert adapter_paths(context) == adapter_paths(host)
        for field in ("gateway_workspace", "adapter_workspace", "audit_log_path", "conversation_workspace", "session_workspace"):
            assert getattr(context.config, field) == getattr(host.config, field), field
    finally:
        host.tools.close_mcp_clients()


@pytest.mark.parametrize("kind,mirror", [("payload", True), ("event", True), ("event", False)])
# LLM: 管理上下文必须保留原CLI超时/控制事件的真实索引，不可把缺local_store吞成side-effect错误；只在首次日志写入初始化原Store。
# 函数用途: 验证轻上下文不构造Agent，按需写原日志库且关闭镜像时不创建数据库。
def test_management_context_preserves_lazy_gateway_logging(tmp_path, monkeypatch, capsys, kind, mirror):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts.logging import log_gateway_event, log_gateway_payload
    from agent_py_agent.cli import common

    args = _management_args(tmp_path, monkeypatch)
    constructor = Mock(side_effect=AssertionError("SimpleAgent must not be constructed"))
    monkeypatch.setattr(SimpleAgent, "__init__", constructor)
    context = common.make_lightweight_cli_agent_context(args)
    context.config.store_event_mirror_enabled = mirror
    database = Path(context.config.local_store_path)
    assert not database.exists() and not (tmp_path / "cold-home").exists()
    payload = {"id": "native-client-timeout", "kind": "ask", "status": "timeout", "ok": False}
    if kind == "payload":
        log_gateway_payload(context, payload, event_type="gateway_request_timeout")
    else:
        log_gateway_event(context, "gateway_stop_requested", payload)
    assert "gateway-side-effect-error" not in capsys.readouterr().err
    constructor.assert_not_called()
    if kind == "event" and not mirror:
        assert not database.exists() and not (tmp_path / "cold-home").exists()
        return
    assert database.is_file()
    store = context.local_store
    assert context.local_store is store
    records = store.list_recent(source_type="gateway_request" if kind == "payload" else "gateway_event")
    assert len(records) == 1
    assert records[0].metadata["status"] == "timeout"
