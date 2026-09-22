"""真实 prompt_toolkit pipe 驱动决策菜单，使用原临时配置与 Gateway stub，不访问收费模型。"""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.formatted_text import to_formatted_text
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from agent_py_agent.agent.settings.decision_settings import (
    execute_decision_settings_operation as settings,
)
from agent_py_agent.agent.settings.model_profiles import (
    ModelProfileError,
    execute_model_profile_operation,
)
from agent_py_agent.cli.chat_parts.tui_decision_menu import _fields, _seconds
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
from agent_py_agent.cli.chat_parts.tui_ui_setup import make_tui_app
from agent_py_agent.tests.test_decision_model_profiles import decision
from agent_py_agent.tests.test_decision_settings import host_at, patch
from agent_py_agent.tests.test_tui_prompt_toolkit_pipe import _app_params


# LLM: stub 只模拟认证运输，read/patch/reset仍执行原 owner/thread 服务；probe 使用固定脱敏回执，不发网络。
# 类用途: 记录真实菜单动作并允许精确注入一次 CAS 冲突。
class Gateway:
    def __init__(self, tmp_path, *, unavailable=False):
        self.host = host_at(tmp_path)
        self.key, _ = decision(self.host, enabled=not unavailable)
        self.thread = self.host.conversation_store.threads.get_or_create({"canonical_user_id": "alice", "owner_id": "alice"})
        self.calls = []
        self.conflict = False

    def request_models(self, *, session_id, operation, payload):
        assert session_id == "pipe-session"
        self.calls.append((operation, payload))
        if operation == "decision_probe":
            return {"ok": False, "model_name": "jev-test", "elapsed_seconds": 0.2, "usage": {"input_tokens": None, "output_tokens": 999},
                    "error_type": "ProviderQuotaExhaustedError", "message": "测试额度不足，设置仍可修改。", "price": 888, "api_key": "private-secret"}
        if operation.startswith("decision_"):
            values = payload["decision"]
            if self.conflict and operation == "decision_patch":
                self.conflict = False
                patch(self.host, {"timeout_seconds": 8.0})
            try:
                return settings(self.host, operation.removeprefix("decision_"), values,
                                thread_id=self.thread.thread_id if values["scope"] == "thread" else "")
            except ModelProfileError as exc:
                return {"ok": False, "message": str(exc), "error_code": getattr(exc, "code", "")}
        return execute_model_profile_operation(self.host, operation, payload)


# LLM: 读取真实控件文本用于断言，既不替换对话框也不模拟其按钮回调。
# 函数用途: 收集当前浮层的标签与文本框显示内容。
def visible(app):
    parts = []
    for control in app.layout.find_all_controls():
        if hasattr(control, "buffer"):
            parts.append(control.buffer.text)
        elif hasattr(control, "text"):
            try:
                parts.extend(fragment[1] for fragment in to_formatted_text(control.text))
            except TypeError:
                pass
    return "\n".join(parts)


# LLM: 按键进入实际 Application 的输入管道；短等待只让原事件循环和线程配置请求完成。
# 函数用途: 发送一次真实终端输入并等待界面稳定。
async def press(ui, keys):
    if isinstance(keys, bytes):
        ui.pipe.send_bytes(keys)
    else:
        ui.pipe.send_text(keys)
    await asyncio.sleep(0.3 if keys == b"\x1b" else 0.12)


# LLM: 现有 make_tui_app / model 浮层负责全部键盘处理；只替换 Gateway 的传输对象，退出必回收 Application。
# 函数用途: 启动真实 TUI 测试应用，保留原聊天任务队列和模型显示用于隔离断言。
@asynccontextmanager
async def running(tmp_path, gateway):
    runtime = TuiRuntime("decision-menu")
    runtime.publish_session(version="test", model="ordinary-main", workspace=str(tmp_path))
    params = _app_params(tmp_path, runtime)
    params.agent.gateway_client_only = True
    params.agent.request_models = gateway.request_models
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        app = make_tui_app(params)
        task = asyncio.create_task(app.run_async(set_exception_handler=False))
        ui = SimpleNamespace(app=app, pipe=pipe, runtime=runtime, params=params)
        try:
            await asyncio.sleep(0.12)
            yield ui
        finally:
            app.exit()
            await asyncio.wait_for(task, timeout=3)


async def open_scope(ui, *, thread=False):
    await press(ui, "/model\r")
    await press(ui, b"\x1b[B" * 8 + b"\r")
    assert "决策模型 · 设置范围" in visible(ui.app)
    await press(ui, (b"\x1b[B" if thread else b"") + b"\r")


async def choose(ui, index):
    await press(ui, b"\x1b[B" * index + b"\r")


def test_pipe_owner_fields_model_mode_seconds_reset_and_provider_reuse(tmp_path):
    async def scenario():
        gateway = Gateway(tmp_path)
        async with running(tmp_path, gateway) as ui:
            await open_scope(ui)
            assert "应用默认配置" in visible(ui.app)
            await choose(ui, 0)
            await press(ui, b"\x1b[B\r")  # 总开关开启
            assert settings(gateway.host, "read", {})["effective"]["enabled"] is True
            await choose(ui, 1)
            await press(ui, b"\x1b[B\r")  # 绑定已有 Decision，普通模型不在选项中
            assert settings(gateway.host, "read", {})["effective"]["profile_id"] == gateway.key
            for index, number in ((2, "2.5"), (3, "5.5"), (4, "6.5")):
                await choose(ui, index)
                await press(ui, b"\x01\x0b")
                await press(ui, number + "\t\r")
            view = settings(gateway.host, "read", {})
            assert [view["effective"][key] for key in ("timeout_seconds", "stage_timeout_seconds", "background_timeout_seconds")] == [2.5, 5.5, 6.5]
            await choose(ui, 5)  # 接入点
            await choose(ui, 3)  # recall
            await choose(ui, 0)  # mode
            await press(ui, b"\x1b[B\x1b[B\r")
            assert settings(gateway.host, "read", {})["effective"]["points"]["recall"]["mode"] == "apply"
            await choose(ui, 6)  # reset 列表
            await choose(ui, 0)  # enabled
            assert "enabled" not in settings(gateway.host, "read", {})["overrides"]["owner"]
            await choose(ui, 7)  # 原服务商管理入口
            assert "服务商列表" in visible(ui.app)
            await press(ui, b"\x1b")
            assert ui.runtime.store.snapshot().selected_model_name == "ordinary-main"
            assert ui.params.jobs.empty() and not ui.params.stop_event.is_set()
            assert not any(operation in {"probe", "decision_probe", "select", "set_default"} for operation, _ in gateway.calls)
            assert all("only-private-secret" not in path.read_text() for path in tmp_path.rglob("input_history"))
    asyncio.run(scenario())


def test_pipe_thread_scope_hides_owner_background_and_changes_only_current_thread(tmp_path):
    async def scenario():
        gateway = Gateway(tmp_path)
        patch(gateway.host, {"profile_id": gateway.key, "enabled": True})
        async with running(tmp_path, gateway) as ui:
            await open_scope(ui, thread=True)
            assert "只在用户长期设置生效" in visible(ui.app)
            assert "后台阶段上限（秒）" not in visible(ui.app)
            await choose(ui, 0)
            await press(ui, b"\x1b[A\r")  # 本会话关闭
            owner = settings(gateway.host, "read", {})
            current = settings(gateway.host, "read", {"scope": "thread"}, thread_id=gateway.thread.thread_id)
            assert owner["effective"]["enabled"] is True and current["effective"]["enabled"] is False
            assert current["sources"]["enabled"] == "thread"
            await choose(ui, 4)  # thread 接入点菜单
            assert "后台记忆整理（用户长期）" not in visible(ui.app)
            await choose(ui, 3)  # recall
            await choose(ui, 2)  # 单次时间
            await press(ui, b"\x01\x0b")
            await press(ui, "1.25\t\r")
            current = settings(gateway.host, "read", {"scope": "thread"}, thread_id=gateway.thread.thread_id)
            assert current["overrides"]["thread"]["points.recall.timeout_seconds"] == 1.25
            writes = [payload["decision"] for operation, payload in gateway.calls if operation in {"decision_patch", "decision_reset"}]
            assert all(row["scope"] == "thread" and set(row["expected_revision"]) == {"owner", "thread"} for row in writes)
            assert all("thread_id" not in row and "owner_id" not in row for row in writes)
    asyncio.run(scenario())


def test_pipe_cas_conflict_rereads_without_replay_and_unavailable_service_can_close(tmp_path):
    async def scenario():
        gateway = Gateway(tmp_path, unavailable=True)
        patch(gateway.host, {"enabled": True, "profile_id": gateway.key})
        gateway.conflict = True
        async with running(tmp_path, gateway) as ui:
            await open_scope(ui)
            await choose(ui, 0)
            await press(ui, b"\x1b[A\r")
            view = settings(gateway.host, "read", {})
            assert view["effective"]["enabled"] is True and view["effective"]["timeout_seconds"] == 8
            assert "未自动重放修改" in visible(ui.app)
            assert sum(operation == "decision_patch" for operation, _ in gateway.calls) == 1
            await choose(ui, 0)
            await press(ui, b"\x1b[A\r")
            assert settings(gateway.host, "read", {})["effective"]["enabled"] is False
            assert sum(operation == "decision_patch" for operation, _ in gateway.calls) == 2
    asyncio.run(scenario())


def test_pipe_probe_requires_explicit_start_and_hides_price_output_and_secret(tmp_path):
    async def scenario():
        gateway = Gateway(tmp_path)
        async with running(tmp_path, gateway) as ui:
            await open_scope(ui)
            await choose(ui, 9)
            await press(ui, b"\x1b[B\r")  # 只选择模型
            assert not any(operation == "decision_probe" for operation, _ in gateway.calls)
            assert "开始测试" in visible(ui.app)
            await press(ui, b"\x01\x0b")
            await press(ui, "3.25\t\r")
            calls = [payload for operation, payload in gateway.calls if operation == "decision_probe"]
            assert calls == [{"profile_id": gateway.key, "timeout_seconds": 3.25}]
            shown = visible(ui.app)
            assert "测试额度不足" in shown and "决策输入：未知" in shown and "决策输出：\n" in shown
            assert all(secret not in shown for secret in ("999", "888", "private-secret"))
            assert settings(gateway.host, "read", {})["effective"]["enabled"] is False
            assert ui.runtime.store.snapshot().selected_model_name == "ordinary-main"
    asyncio.run(scenario())


@pytest.mark.parametrize("text", ["nan", "inf", "-inf", "0", "-1", "true", "1e9999"])
def test_seconds_reject_nonpositive_or_nonfinite(text):
    with pytest.raises(ValueError):
        _seconds(text)


def test_scope_fields_are_finite_and_thread_has_no_unused_curator_controls(tmp_path):
    gateway = Gateway(tmp_path)
    owner = settings(gateway.host, "read", {})
    thread = settings(gateway.host, "read", {"scope": "thread"}, thread_id=gateway.thread.thread_id)
    assert _seconds("0.25") == 0.25
    assert "background_timeout_seconds" not in _fields(thread)
    assert all(not field.startswith("points.curator.") for field in _fields(thread))
    assert "points.curator.mode" in _fields(owner)


def test_pipe_invalid_seconds_cancel_and_explicit_model_clear_are_distinct(tmp_path):
    async def scenario():
        gateway = Gateway(tmp_path)
        patch(gateway.host, {"profile_id": gateway.key})
        async with running(tmp_path, gateway) as ui:
            await open_scope(ui)
            await choose(ui, 2)
            await press(ui, b"\x01\x0b")
            await press(ui, "nan\t\r")
            assert "修改尚未保存" in visible(ui.app)
            assert not any(op == "decision_patch" for op, _ in gateway.calls)
            await press(ui, b"\x1b")
            await choose(ui, 1)
            await press(ui, b"\x1b[A\r")  # 明确清空保留一个空覆盖
            assert settings(gateway.host, "read", {})["overrides"]["owner"]["profile_id"] == ""
            await choose(ui, 1)
            await press(ui, "\t\t\r")  # 恢复继承按钮删除覆盖
            assert "profile_id" not in settings(gateway.host, "read", {})["overrides"]["owner"]
            await choose(ui, 9)
            await press(ui, b"\x1b[B\r")
            await press(ui, b"\x1b")  # 取消探测确认
            assert not any(op == "decision_probe" for op, _ in gateway.calls)
            assert ui.params.jobs.empty() and not ui.params.stop_event.is_set()
    asyncio.run(scenario())


def test_pipe_thread_can_clean_legacy_background_override_without_offering_new_patch(tmp_path):
    from dataclasses import replace

    async def scenario():
        gateway = Gateway(tmp_path)
        original = gateway.thread.decision_settings
        gateway.host.conversation_store.threads.update_atomic(
            gateway.thread.thread_id,
            lambda row: replace(row, decision_settings={**original, "revision": 1, "overrides": {"points.curator.mode": "apply"}}),
        )
        async with running(tmp_path, gateway) as ui:
            await open_scope(ui, thread=True)
            await choose(ui, 5)  # 恢复继承仅清理旧存储，不重新授权新覆盖
            assert "此范围不消费，仅可清理" in visible(ui.app)
            await press(ui, "\r")
            view = settings(gateway.host, "read", {"scope": "thread"}, thread_id=gateway.thread.thread_id)
            assert view["overrides"]["thread"] == {}
            assert [(op, body["decision"]["fields"]) for op, body in gateway.calls if op == "decision_reset"] == [
                ("decision_reset", ["points.curator.mode"])]
            assert not any(op == "decision_patch" for op, _ in gateway.calls)
    asyncio.run(scenario())
