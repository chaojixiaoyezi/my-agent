"""/model「新增模型」真实按键：拉不到列表可手动填、OpenCode 模板与「高级」请求头、请求头格式错不发请求、决策类型、
登录账号分支；密钥不进输入历史和聊天记录。"""
import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from agent_py_agent.agent.settings import model_provider_network as network
from agent_py_agent.agent.settings.model_profiles import model_profiles_path, read_model_profiles
from agent_py_agent.cli.chat_parts import tui_model_add
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
from agent_py_agent.cli.chat_parts.tui_ui_setup import make_tui_app
from agent_py_agent.tests.test_thread_model_selection import host_with_store
from agent_py_agent.tests.test_tui_decision_menu import visible, wait_app, wait_dialog_ready
from agent_py_agent.tests.test_tui_prompt_toolkit_pipe import _app_params

SECRET = "only-private-secret"


@asynccontextmanager
async def running_add(tmp_path):
    runtime = TuiRuntime("add-models")
    params = _app_params(tmp_path, runtime)
    host = host_with_store(tmp_path)
    params.agent.home_paths, params.agent.config = host.home_paths, host.config
    params.agent.conversation_store = host.conversation_store
    host.config.session_workspace = str(tmp_path / "sessions")
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        app = make_tui_app(params)
        runner = asyncio.create_task(app.run_async())
        try:
            await wait_app(app, lambda: app.is_running, "TUI 启动")
            app._my_agent_model_menu_active = True
            flow = asyncio.create_task(tui_model_add.add_models(app, params.agent, "add-models"))
            yield app, pipe, host, flow, runtime
        finally:
            app.exit()
            await runner


# 函数用途: 同一个表单关掉又重新弹出时，控件对象是同一批，wait_dialog_ready 的"已登记父级"会提前成立；
# 再强制重绘一次，保证新浮层的按键绑定已生效后才发下一个键。
async def wait_redrawn(app, text, what):
    await wait_dialog_ready(app, text, what)
    seen = app.render_counter
    app.invalidate()
    await wait_app(app, lambda: app.render_counter > seen, what + "后重绘")


def saved(host):
    return read_model_profiles(model_profiles_path(host.home_paths))


async def choose_type(app, pipe, downs, purpose_downs=0):
    await wait_dialog_ready(app, "新增模型 · 选择类型", "类型列表")
    pipe.send_bytes(b"\x1b[B" * downs + b"\r")
    if downs == 0:
        await wait_dialog_ready(app, "新增模型 · 选择用途", "用途列表")
        assert "对话（默认）" in visible(app) and "Embedding" in visible(app)
        pipe.send_bytes(b"\x1b[B" * purpose_downs + b"\r")


def test_catalog_failure_offers_manual_entry_and_saves_one_model(tmp_path, monkeypatch):
    class Failure(RuntimeError):
        details = {"provider_error": {"error": {"message": f"no model list for {SECRET}"}}}

    add_payloads = []
    original_request = tui_model_add._request

    async def record_add_payload(*args, **kwargs):
        if args[3] == "add_models":
            add_payloads.append(args[4])
        return await original_request(*args, **kwargs)

    def failing(request):
        raise Failure()

    monkeypatch.setattr(network, "get_json", failing)
    monkeypatch.setattr(tui_model_add, "_request", record_add_payload)

    async def scenario():
        async with running_add(tmp_path) as (app, pipe, host, flow, runtime):
            await choose_type(app, pipe, 0)  # OpenAI Chat
            await wait_dialog_ready(app, "新增模型 · 填写连接", "连接表单")
            pipe.send_text(f"https://api.example.test\t{SECRET}\t\r")
            await wait_dialog_ready(app, "新增模型 · 手动填写", "拉不到列表时的手动填写")
            assert "no model list for [已隐藏]" in visible(app) and SECRET not in visible(app)
            pipe.send_text("deepseek-v4-flash\t\x01\x0b1000000\t\r")
            result = await asyncio.wait_for(flow, 3)
            assert result.startswith("已添加 1 个模型：deepseek-v4-flash")
            data = saved(host)
            row = next(iter(data["profiles"].values()))
            assert (row["model_name"], row["model_context_window_tokens"], row["model_backend"]) == (
                "deepseek-v4-flash", 1000000, "openai_compatible")
            assert row["capability"] == "agentic"
            assert data["providers"][row["provider_id"]]["capabilities"] == ["agentic"]
            assert len(add_payloads) == 1 and set(add_payloads[0]) == {"connection", "models"}
            assert data["providers"][row["provider_id"]]["api_key"] == SECRET
            assert all(SECRET not in path.read_text() for path in tmp_path.rglob("input_history"))
            assert all(SECRET not in block.text for block in runtime.store.snapshot().stable_blocks)

    asyncio.run(scenario())


def test_opencode_template_and_advanced_headers_reach_the_catalog_request(tmp_path, monkeypatch):
    seen = []

    def catalog(request):
        seen.append(request)
        return {"data": [{"id": "minimax-m2.7", "context_length": 204800}, {"id": "glm-5"}]}

    monkeypatch.setattr(network, "get_json", catalog)

    async def scenario():
        async with running_add(tmp_path) as (app, pipe, host, flow, _runtime):
            await choose_type(app, pipe, 0)
            await wait_dialog_ready(app, "新增模型 · 填写连接", "连接表单")
            pipe.send_text(f"\t{SECRET}\t\t\r")  # 地址先空着；Tab 到「OpenCode Go 模板」
            await wait_redrawn(app, "已填入 OpenCode Go 的地址和会话头", "模板填入地址")
            pipe.send_text("\t\t\t\t\r")  # 地址 → 密钥 →「拉取」→「模板」→「高级」
            await wait_dialog_ready(app, "新增模型 · 高级", "高级页")
            pipe.send_text('{"X-Title": "my-agent"}')
            pipe.send_bytes(b"\x1b")  # 关高级页，回到表单
            await wait_redrawn(app, "新增模型 · 填写连接", "回到连接表单")
            pipe.send_text("\t\t\r")  # 地址 → 密钥 →「拉取模型列表」
            await wait_dialog_ready(app, "选择要添加的模型（可多选）", "勾选框")
            assert "上下文未写明" in visible(app)
            pipe.send_bytes(b" ")
            await asyncio.sleep(0.05)
            pipe.send_bytes(b"\t\t\r")  # 只勾第一个（有上下文），跳过统一上下文栏到「添加」
            result = await asyncio.wait_for(flow, 3)
            assert result.startswith("已添加 1 个模型：minimax-m2.7")
            request = seen[0]
            assert request.api_base == "https://opencode.ai/zen/go/v1" and request.path == "/models"
            assert request.headers["X-Title"] == "my-agent" and request.headers.get("x-opencode-session")
            data = saved(host)
            provider = next(iter(data["providers"].values()))
            assert provider["display_name"] == "OpenCode Go" and provider["session_header"] == "x-opencode-session"
            assert provider["custom_headers"] == {"X-Title": "my-agent"}
            row = next(iter(data["profiles"].values()))
            assert row["model_context_window_tokens"] == 204800

    asyncio.run(scenario())


def test_bad_header_json_stays_on_the_form_without_any_request(tmp_path, monkeypatch):
    monkeypatch.setattr(network, "get_json", lambda request: pytest.fail("请求头格式错时不能发请求"))

    async def scenario():
        async with running_add(tmp_path) as (app, pipe, host, flow, _runtime):
            await choose_type(app, pipe, 0)
            await wait_dialog_ready(app, "新增模型 · 填写连接", "连接表单")
            pipe.send_text(f"https://api.example.test\t{SECRET}\t\t\t\r")  # 到「高级」
            await wait_dialog_ready(app, "新增模型 · 高级", "高级页")
            pipe.send_text("not json")
            pipe.send_bytes(b"\x1b")
            await wait_redrawn(app, "新增模型 · 填写连接", "回到连接表单")
            pipe.send_text("\t\t\r")  # 「拉取模型列表」
            await wait_redrawn(app, "请求头不是有效的 JSON 对象", "表单提示请求头格式错")
            pipe.send_bytes(b"\x1b")
            assert await asyncio.wait_for(flow, 3) == ""
            assert not model_profiles_path(host.home_paths).exists()

    asyncio.run(scenario())


def test_decision_type_has_no_template_and_saves_a_decision_model(tmp_path, monkeypatch):
    monkeypatch.setattr(network, "get_json", lambda request: {"data": [{"id": "jev-small", "context_length": 32768}]})

    async def scenario():
        async with running_add(tmp_path) as (app, pipe, host, flow, _runtime):
            await choose_type(app, pipe, 4)  # Jev 决策
            await wait_dialog_ready(app, "新增模型 · 填写连接", "连接表单")
            assert "OpenCode Go 模板" not in visible(app)
            pipe.send_text(f"https://jev.example.test\t{SECRET}\t\r")
            await wait_dialog_ready(app, "选择要添加的模型（可多选）", "勾选框")
            pipe.send_bytes(b" ")
            await asyncio.sleep(0.05)
            pipe.send_bytes(b"\t\r")
            result = await asyncio.wait_for(flow, 3)
            assert result.startswith("已添加 1 个模型：jev-small")
            data = saved(host)
            row = next(iter(data["profiles"].values()))
            assert (row["model_backend"], row["capability"]) == ("typesafe_decision", "decision")
            assert next(iter(data["providers"].values()))["capabilities"] == ["decision"]

    asyncio.run(scenario())


def test_login_account_type_goes_to_the_account_flow(tmp_path, monkeypatch):
    from agent_py_agent.cli.chat_parts import tui_model_auth

    async def add_account(app, agent, session):
        return "登录成功。已添加 2 个模型"

    monkeypatch.setattr(tui_model_auth, "add_account", add_account)

    async def scenario():
        async with running_add(tmp_path) as (app, pipe, _host, flow, _runtime):
            await choose_type(app, pipe, 3)
            assert await asyncio.wait_for(flow, 3) == "登录成功。已添加 2 个模型"

    asyncio.run(scenario())


def test_escape_on_the_type_list_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(network, "get_json", lambda request: pytest.fail("没进表单不能发请求"))

    async def scenario():
        async with running_add(tmp_path) as (app, pipe, host, flow, _runtime):
            await wait_dialog_ready(app, "新增模型 · 选择类型", "类型列表")
            pipe.send_bytes(b"\x1b")
            assert await asyncio.wait_for(flow, 3) == ""
            assert not app._my_agent_model_float_container.floats
            assert not model_profiles_path(host.home_paths).exists()

    asyncio.run(scenario())


def test_connection_form_clears_the_key_when_it_closes(monkeypatch):
    form = tui_model_add._ConnectionForm(None, None, "s", "openai_compatible")
    form.key.text, form.headers.text = SECRET, json.dumps({"Authorization": SECRET})

    async def close(*args, **kwargs):
        return None

    monkeypatch.setattr(tui_model_add, "_dialog", close)
    assert asyncio.run(form.run()) == ""
    assert form.key.text == "" and form.headers.text == ""


def test_embedding_usage_is_saved_like_manage_models_one_step_add(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability.model_profile_tool import ManageModelsTool
    from agent_py_agent.tests.test_model_profiles import Host

    monkeypatch.setattr(network, "get_json", lambda request: {"data": [{"id": "embed-v1", "context_length": 8192}]})

    async def scenario():
        async with running_add(tmp_path) as (app, pipe, host, flow, _runtime):
            await choose_type(app, pipe, 0, purpose_downs=1)
            await wait_dialog_ready(app, "新增模型 · 填写连接", "连接表单")
            pipe.send_text(f"https://api.example.test/v1\t{SECRET}\t\r")
            await wait_dialog_ready(app, "选择要添加的模型（可多选）", "勾选框")
            pipe.send_bytes(b" \t\r")
            result = await asyncio.wait_for(flow, 3)
            assert result == "已添加 1 个模型：embed-v1。去‘选择模型’→‘向量模型’里选用；重启 Gateway 后生效"

        tui_data = saved(host)
        tui_row = next(iter(tui_data["profiles"].values()))
        tui_provider = tui_data["providers"][tui_row["provider_id"]]

        quick_host = Host(tmp_path / "manage-config")
        quick = ManageModelsTool(quick_host).execute({"action": "add", "profile": {
            "model_name": "embed-v1", "model_backend": "openai_compatible",
            "api_base": "https://api.example.test/v1", "api_key": SECRET,
            "model_context_window_tokens": 8192, "capability": "embedding",
        }})
        assert quick.ok
        quick_data = read_model_profiles(model_profiles_path(quick_host.home_paths))
        quick_row = next(iter(quick_data["profiles"].values()))
        quick_provider = quick_data["providers"][quick_row["provider_id"]]
        assert {key: value for key, value in tui_row.items() if key != "provider_id"} == {
            key: value for key, value in quick_row.items() if key != "provider_id"
        }
        assert tui_provider["capabilities"] == quick_provider["capabilities"] == ["embedding"]
        assert {key: value for key, value in tui_provider.items() if key != "display_name"} == {
            key: value for key, value in quick_provider.items() if key != "display_name"
        }

    asyncio.run(scenario())
