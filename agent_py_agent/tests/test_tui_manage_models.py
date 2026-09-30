"""/model「管理已有模型」「连接测试」「默认模型与共享」真实按键：从已有连接再勾选添加、修改连接（高级里的会话头）、
删除模型、管理员指定其他用户的初始模型、普通用户看到的默认行、决策模型的连接测试走限时决策测试。"""
import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from agent_py_agent.agent.settings import model_provider_network as network
from agent_py_agent.agent.settings.model_profiles import (
    execute_model_profile_operation,
    model_profiles_path,
    read_model_profiles,
)
from agent_py_agent.agent.settings.shared_model_catalog import initial_profile_key
from agent_py_agent.agent.settings.thread_model_selection import execute_local_model_operation
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
from agent_py_agent.cli.chat_parts.tui_ui_setup import make_tui_app
from agent_py_agent.tests.test_thread_model_selection import host_with_store
from agent_py_agent.tests.test_tui_decision_menu import visible, wait_app, wait_dialog_ready
from agent_py_agent.tests.test_tui_model_add import wait_redrawn
from agent_py_agent.tests.test_tui_prompt_toolkit_pipe import _app_params

SECRET = "only-private-secret"


def connection_host(tmp_path, owner="alice"):
    host = host_with_store(tmp_path, owner)
    if owner == "local/main":
        host.home_paths.owner_kind = "main"
    host.config.session_workspace = str(tmp_path / "sessions" / owner.replace("/", "-"))
    execute_model_profile_operation(host, "add_models", {"connection": {
        "model_backend": "anthropic_compatible", "api_base": "https://api.example.test/anthropic", "api_key": SECRET,
        "custom_headers": {}, "session_header": ""}, "models": [
        {"profile_id": str(uuid4()), "model_name": "m-one", "model_context_window_tokens": 96000}]})
    return host


@asynccontextmanager
async def running_menu(tmp_path, host):
    from agent_py_agent.cli.chat_parts.tui_model_menu import run_model_menu

    runtime = TuiRuntime("manage-models")
    runtime.publish_session(version="0.3.0", model="deployment-model", workspace=str(tmp_path))
    params = _app_params(tmp_path, runtime)
    params.agent.home_paths, params.agent.config = host.home_paths, host.config
    params.agent.conversation_store = host.conversation_store
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        app = make_tui_app(params)
        runner = asyncio.create_task(app.run_async())
        try:
            await wait_app(app, lambda: app.is_running, "TUI 启动")
            menu = asyncio.create_task(run_model_menu(app, params.agent, "manage-models", runtime))
            await wait_dialog_ready(app, "模型配置 /model", "模型菜单")
            yield app, pipe, menu
        finally:
            app.exit()
            await runner


def saved(host):
    return read_model_profiles(model_profiles_path(host.home_paths))


async def open_connection(app, pipe):
    pipe.send_bytes(b"\x1b[B\x1b[B\r")  # 顶层第 3 项「管理已有模型」
    await wait_dialog_ready(app, "管理已有模型 · 选择连接或账号", "连接列表")
    assert "api.example.test · https://api.example.test/anthropic · 1 个模型" in visible(app)
    pipe.send_bytes(b"\r")
    await wait_dialog_ready(app, "连接操作 · api.example.test", "连接操作")


def test_add_more_models_from_a_saved_connection(tmp_path, monkeypatch):
    monkeypatch.setattr(network, "get_json", lambda request: {"data": [
        {"id": "m-one", "context_length": 96000}, {"id": "m-two", "context_length": 200000}]})
    host = connection_host(tmp_path)

    async def scenario():
        async with running_menu(tmp_path, host) as (app, pipe, menu):
            await open_connection(app, pipe)
            pipe.send_bytes(b"\x1b[B\r")  # 「从这个连接再添加模型」
            await wait_dialog_ready(app, "新增模型 · 选择接口", "接口选择（默认取已有模型的接口）")
            pipe.send_bytes(b"\r")
            await wait_dialog_ready(app, "选择要添加的模型（可多选）", "勾选框")
            pipe.send_bytes(b" \x1b[B ")  # 两个都勾；m-one 已有，服务端跳过
            await asyncio.sleep(0.1)
            pipe.send_bytes(b"\t\r")
            await wait_dialog_ready(app, "已添加 1 个模型：m-two。另有 1 个之前已添加", "添加结果")
            data = saved(host)
            assert len(data["providers"]) == 1
            assert sorted(row["model_name"] for row in data["profiles"].values()) == ["m-one", "m-two"]
            assert {row["model_backend"] for row in data["profiles"].values()} == {"anthropic_compatible"}
            pipe.send_bytes(b"\x1b")
            await asyncio.wait_for(menu, 3)

    asyncio.run(scenario())


def test_edit_connection_keeps_the_key_and_sets_a_session_header(tmp_path):
    host = connection_host(tmp_path)

    async def scenario():
        async with running_menu(tmp_path, host) as (app, pipe, menu):
            await open_connection(app, pipe)
            pipe.send_bytes(b"\x1b[B\x1b[B\r")  # 「修改地址 / 密钥 / 请求头」
            await wait_dialog_ready(app, "修改连接", "连接表单")
            pipe.send_bytes(b"\x01\x0b")
            pipe.send_text("Example 服务")
            for _ in range(4):  # 名称 → 地址 → 密钥 → 启用 →「保存」
                pipe.send_bytes(b"\t")
                await asyncio.sleep(0.05)
            pipe.send_bytes(b"\t\r")  # 「高级」
            await wait_dialog_ready(app, "修改连接 · 高级", "高级页")
            pipe.send_text("x-session-id")
            pipe.send_bytes(b"\x1b")
            await wait_redrawn(app, "连接编号（不可改）", "回到连接表单")
            for _ in range(4):
                pipe.send_bytes(b"\t")
                await asyncio.sleep(0.05)
            pipe.send_bytes(b"\r")  # 「保存」
            await wait_dialog_ready(app, "连接已保存", "保存结果")
            provider = next(iter(saved(host)["providers"].values()))
            assert provider["display_name"] == "Example 服务" and provider["session_header"] == "x-session-id"
            assert provider["api_key"] == SECRET  # 密钥留空 = 保留原值
            pipe.send_bytes(b"\x1b")
            await asyncio.wait_for(menu, 3)

    asyncio.run(scenario())


def test_delete_a_model_under_a_connection(tmp_path):
    host = connection_host(tmp_path)

    async def scenario():
        async with running_menu(tmp_path, host) as (app, pipe, menu):
            await open_connection(app, pipe)
            pipe.send_bytes(b"\r")  # 「编辑或删除这个连接下的模型」
            await wait_dialog_ready(app, "m-one · anthropic_compatible", "模型列表")
            pipe.send_bytes(b"\r")
            await wait_dialog_ready(app, "删除模型", "模型操作")
            pipe.send_bytes(b"\x1b[B\r")
            await wait_dialog_ready(app, "确定删除？", "删除确认")
            pipe.send_bytes(b"\r")
            await wait_dialog_ready(app, "配置已删除。", "删除结果")
            assert saved(host)["profiles"] == {}
            pipe.send_bytes(b"\x1b")
            await asyncio.wait_for(menu, 3)

    asyncio.run(scenario())


def test_delete_models_from_the_manage_list_with_one_confirmation(tmp_path):
    host = connection_host(tmp_path)

    async def scenario():
        async with running_menu(tmp_path, host) as (app, pipe, menu):
            pipe.send_bytes(b"\x1b[B\x1b[B\r")  # 「管理已有模型」
            await wait_dialog_ready(app, "删除模型（勾选一个或多个）", "连接列表最后一行是删除模型")
            pipe.send_bytes(b"\x1b[B\r")
            await wait_dialog_ready(app, "空格勾选要删的模型", "删除勾选框")
            pipe.send_bytes(b" ")
            await asyncio.sleep(0.05)
            pipe.send_bytes(b"\t\r")
            await wait_dialog_ready(app, "将删除 1 个模型", "删除确认")
            pipe.send_bytes(b"\r")
            await wait_dialog_ready(app, "已删除 1 个模型。", "删除结果")
            assert saved(host)["profiles"] == {}
            pipe.send_bytes(b"\x1b")
            await asyncio.wait_for(menu, 3)

    asyncio.run(scenario())


def test_admin_sets_the_initial_model_for_other_users(tmp_path):
    admin = connection_host(tmp_path, "local/main")
    alice = host_with_store(tmp_path)

    async def scenario():
        async with running_menu(tmp_path, admin) as (app, pipe, menu):
            pipe.send_bytes(b"\x1b[B" * 4 + b"\r")  # 顶层第 5 项「默认模型与共享」
            await wait_dialog_ready(app, "其他用户的初始模型（管理员", "默认与共享二级菜单")
            pipe.send_bytes(b"\x1b[B\x1b[B\r")
            await wait_dialog_ready(app, "只影响没自己选过模型的其他用户", "初始模型列表")
            assert "会同时开放共享" in visible(app)
            pipe.send_bytes(b"\x1b[B\r")
            await wait_dialog_ready(app, "其他用户的初始模型已设为 m-one", "设置结果")
            pipe.send_bytes(b"\x1b")
            await asyncio.wait_for(menu, 3)

    asyncio.run(scenario())
    key = next(iter(saved(admin)["profiles"]))
    assert initial_profile_key(admin.home_paths) == "shared:" + key
    listing = execute_local_model_operation(alice, "im", "list", {})
    assert listing["profiles"][0]["model_name"] == "m-one" and listing["profiles"][0]["default_source"] == "admin_initial"


def test_admin_manage_list_ignores_shared_aliases(tmp_path):
    admin = connection_host(tmp_path, "local/main")
    key = next(iter(saved(admin)["profiles"]))
    execute_model_profile_operation(admin, "set_shared", {"profile_id": key, "enabled": True})

    async def scenario():
        async with running_menu(tmp_path, admin) as (app, pipe, menu):
            await open_connection(app, pipe)  # 标签断言「1 个模型」：共享别名不重复计数
            pipe.send_bytes(b"\r")
            await wait_dialog_ready(app, "m-one · anthropic_compatible", "模型列表")
            assert visible(app).count("m-one") == 1  # 别名不进可编辑列表
            pipe.send_bytes(b"\x1b")
            await wait_dialog_ready(app, "模型配置 /model", "回到主菜单")
            pipe.send_bytes(b"\x1b")
            await asyncio.wait_for(menu, 3)

    asyncio.run(scenario())


def test_non_admin_is_told_only_admins_set_initial_models(tmp_path):
    host = connection_host(tmp_path)

    async def scenario():
        async with running_menu(tmp_path, host) as (app, pipe, menu):
            pipe.send_bytes(b"\x1b[B" * 4 + b"\r")
            await wait_dialog_ready(app, "其他用户的初始模型（管理员", "默认与共享二级菜单")
            pipe.send_bytes(b"\x1b[B\x1b[B\r")
            await wait_dialog_ready(app, "只有管理员可以设置", "无权限提示回到主菜单")
            pipe.send_bytes(b"\x1b")
            await asyncio.wait_for(menu, 3)

    asyncio.run(scenario())
    assert initial_profile_key(host.home_paths) == ""


def test_connection_test_routes_decision_models_to_the_timed_decision_probe(tmp_path, monkeypatch):
    from agent_py_agent.cli.chat_parts import tui_decision_menu

    host = connection_host(tmp_path)
    execute_model_profile_operation(host, "add_models", {"connection": {
        "model_backend": "typesafe_decision", "api_base": "https://jev.example.test", "api_key": SECRET,
        "custom_headers": {}, "session_header": ""}, "models": [
        {"profile_id": str(uuid4()), "model_name": "jev-small", "model_context_window_tokens": 32768}]})
    probed = []

    async def probe_selected(app, agent, session, selected):
        probed.append(selected)
        return "决策测试已完成"

    monkeypatch.setattr(tui_decision_menu, "probe_selected", probe_selected)

    async def scenario():
        async with running_menu(tmp_path, host) as (app, pipe, menu):
            pipe.send_bytes(b"\x1b[B" * 3 + b"\r")  # 顶层第 4 项「连接测试」
            await wait_dialog_ready(app, "jev-small · 决策", "连接测试列表含决策模型")
            pipe.send_bytes(b"\x1b[B\r")
            await wait_dialog_ready(app, "决策测试已完成", "决策模型走限时决策测试")
            pipe.send_bytes(b"\x1b")
            await asyncio.wait_for(menu, 3)

    asyncio.run(scenario())
    decision = next(key for key, row in saved(host)["profiles"].items() if row["model_name"] == "jev-small")
    assert probed == [decision]
