"""订阅账号模型勾选：只列未添加的、勾几个加几个、以后再说不写入、目录读取失败给出可重试提示。"""
import asyncio

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from agent_py_agent.cli.chat_parts import tui_subscription_models
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
from agent_py_agent.cli.chat_parts.tui_ui_setup import make_tui_app
from agent_py_agent.tests.test_tui_decision_menu import wait_app, wait_dialog_ready
from agent_py_agent.tests.test_tui_prompt_toolkit_pipe import _app_params

CATALOG = [
    {"model_name": "gpt-a", "display_name": "GPT-A", "model_context_window_tokens": 272000, "model_backend": "openai_responses"},
    {"model_name": "gpt-b", "display_name": "GPT-B", "model_context_window_tokens": 272000, "model_backend": "openai_responses"},
    {"model_name": "gpt-c", "display_name": "GPT-C", "model_context_window_tokens": 128000, "model_backend": "openai_responses"},
]


def fake_gateway(monkeypatch, calls, *, catalog_ok=True):
    async def request(app, agent, session, operation, payload):
        calls.append((operation, dict(payload)))
        if not catalog_ok:
            return {"ok": False, "message": "订阅接口暂时不可用。"}
        return {"ok": True, "models": [dict(row) for row in CATALOG]}

    async def data(agent, session, operation, payload):
        calls.append((operation, dict(payload)))
        if operation == "list":
            return {"ok": True, "profiles": [{"id": "old", "provider_id": "account", "model_name": "gpt-a"},
                                             {"id": "other", "provider_id": "elsewhere", "model_name": "gpt-b"}]}
        return {"ok": True}

    monkeypatch.setattr(tui_subscription_models, "_request", request)
    monkeypatch.setattr(tui_subscription_models, "_request_data", data)


def run_picker(tmp_path, keys):
    async def main():
        runtime = TuiRuntime("pick-models")
        params = _app_params(tmp_path, runtime)
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            app = make_tui_app(params)
            runner = asyncio.create_task(app.run_async())
            try:
                await wait_app(app, lambda: app.is_running, "TUI 启动")
                picker = asyncio.create_task(tui_subscription_models.pick_subscription_models(
                    app, params.agent, "pick-models", "account"))
                if keys:
                    await wait_dialog_ready(app, None, "模型勾选框出现")
                    for chunk in keys:
                        pipe.send_bytes(chunk)
                        await asyncio.sleep(0.05)
                result = await asyncio.wait_for(picker, 3)
                assert not app._my_agent_model_float_container.floats
                return result
            finally:
                app.exit()
                await runner

    return asyncio.run(main())


def saved_names(calls):
    return [payload["profile"]["model_name"] for operation, payload in calls if operation == "save_model"]


def test_checked_models_are_added_with_catalog_defaults_and_existing_ones_are_not_offered(tmp_path, monkeypatch):
    calls = []
    fake_gateway(monkeypatch, calls)
    result = run_picker(tmp_path, [b" ", b"\x1b[B", b"\x1b[B", b" ", b"\t", b"\r"])
    assert saved_names(calls) == ["gpt-b", "gpt-c"]
    profile = next(payload for operation, payload in calls if operation == "save_model")["profile"]
    assert profile == {"provider_id": "account", "model_name": "gpt-b", "model_backend": "openai_responses",
                       "model_context_window_tokens": 272000, "capability": "agentic", "enabled": True}
    assert "已添加 2 个模型：GPT-B、GPT-C" in result and "选择已有模型" in result


def test_later_adds_nothing(tmp_path, monkeypatch):
    calls = []
    fake_gateway(monkeypatch, calls)
    result = run_picker(tmp_path, [b" ", b"\x1b"])
    assert saved_names(calls) == [] and "暂未添加模型" in result


def test_catalog_failure_gives_a_retry_hint_without_a_dialog(tmp_path, monkeypatch):
    calls = []
    fake_gateway(monkeypatch, calls, catalog_ok=False)
    result = run_picker(tmp_path, [])
    assert "订阅接口暂时不可用" in result and "选择模型" in result
    assert [operation for operation, _ in calls] == ["discover"]
