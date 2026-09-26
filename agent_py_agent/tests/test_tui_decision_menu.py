# LLM: 真实 pipe 和原配置服务验证设置合同；按界面就绪事实有界等待，不用固定延迟代替异步确认。
# 模块用途: 在本地 Gateway stub 上测试决策菜单，不发模型请求，不改产品事件循环或设置行为。
"""真实 prompt_toolkit pipe 驱动决策菜单，使用原临时配置与 Gateway stub。"""
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
from agent_py_agent.cli.chat_parts.tui_decision_menu import (
    _POINTS,
    _field_text_value,
    _fields,
    _mode_control,
    _seconds,
)
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


# LLM: 只观察原浮层和焦点；_request 的只读等待框不是可交互菜单，不能因浮层出现就认定请求完成。
# 函数用途: 取得可交互界面的身份和内容，避免 Enter 后误把临时等待浮层当成操作完成。
def ready_state(app):
    floats = tuple(app._my_agent_model_float_container.floats)
    focused_buffer = getattr(app.layout.current_control, "buffer", None)
    if floats and focused_buffer is not None and focused_buffer.read_only():
        return None
    return floats, visible(app)


# LLM: 只轮询测试谓词，固定总截止时间；不重复发键、不改原 Future/handler，不将超时当成功。
# 函数用途: 等待真实 UI 出现目标状态，超时直接附当前界面帮助定位。
async def wait_ui(ui, predicate):
    deadline = asyncio.get_running_loop().time() + 3
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("等待 TUI 状态超时：\n" + visible(ui.app))
        await asyncio.sleep(0.01)


# LLM: Enter/Esc 等新浮层已真实绘制再继续，避免布局已变但按键缓存未刷新；编辑键也等待对应控件绘制。
# 函数用途: 发送一次真实输入，按原界面状态变化等待，不重放键盘操作或替代设置请求。
async def press(ui, keys):
    before = ready_state(ui.app)
    assert before is not None, "按键前仍在等待配置操作"
    encoded = keys if isinstance(keys, bytes) else keys.encode()
    transition = b"\r" in encoded or encoded == b"\x1b"
    if isinstance(keys, bytes):
        ui.pipe.send_bytes(keys)
    else:
        ui.pipe.send_text(keys)
    await wait_ui(ui, lambda: (after := ui.rendered_state) is not None and after == ready_state(ui.app)
                  and (after[0] != before[0] if transition else after != before))


# LLM: 原 make_tui_app / model 浮层处理按键，after_render 只观察已绘制状态；只替换 Gateway 运输，退出必回收 Application。
# 函数用途: 启动真实 TUI 并保留绘制事实、聊天任务队列和模型显示，供操作等待与隔离断言。
@asynccontextmanager
async def running(tmp_path, gateway):
    runtime = TuiRuntime("decision-menu")
    runtime.publish_session(version="test", model="ordinary-main", workspace=str(tmp_path))
    params = _app_params(tmp_path, runtime)
    params.agent.gateway_client_only = True
    params.agent.request_models = gateway.request_models
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        app = make_tui_app(params)
        ui = SimpleNamespace(app=app, pipe=pipe, runtime=runtime, params=params, rendered_state=None)

        # LLM: 仅记录已绘制的原浮层，不触发输入或变更产品状态。
        # 函数用途: 给后续按键提供明确的控件就绪证据。
        def rendered(current):
            ui.rendered_state = ready_state(current)

        app.after_render += rendered
        task = asyncio.create_task(app.run_async(set_exception_handler=False))
        try:
            await wait_ui(ui, lambda: app.is_running and ui.rendered_state is not None)
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


# LLM: 位置只由菜单 _POINTS 顺序与原服务 field_scopes 推出，和 _edit_point 的列表口径一致；不写死下标。
# 函数用途: 按原菜单的接入点顺序算出某接入点在当前作用域菜单里的位置，新增接入点时测试不必写死下标。
def point_index(gateway, point, *, thread=False):
    view = (settings(gateway.host, "read", {"scope": "thread"}, thread_id=gateway.thread.thread_id)
            if thread else settings(gateway.host, "read", {}))
    return [key for key in _POINTS if f"points.{key}.mode" in _fields(view)].index(point)


@pytest.mark.parametrize("stored,toggles,expected", [
    ("off", (), "off"), ("off", ("enabled",), "observe"), ("off", ("enabled", "observe"), "apply"),
    ("observe", (), "observe"), ("observe", ("observe",), "apply"), ("observe", ("enabled",), "off"),
    ("apply", (), "apply"), ("apply", ("observe",), "observe"), ("apply", ("enabled",), "off"),
])
def test_mode_checkboxes_map_on_off_and_observe_to_the_three_stored_modes(stored, toggles, expected):
    control, mode_value = _mode_control(stored)
    for key in toggles:
        values = control.current_values
        control.current_values = [item for item in values if item != key] if key in values else [*values, key]
    assert mode_value() == expected


def test_candidate_profile_ids_form_uses_structured_json(tmp_path):
    gateway = Gateway(tmp_path)
    field = "points.subagent_model.candidate_profile_ids"
    view = settings(gateway.host, "read", {})
    assert field in _fields(view)
    assert _field_text_value(field, '["' + gateway.key + '"]') == [gateway.key]
    with pytest.raises(ValueError):
        _field_text_value(field, '"' + gateway.key + '"')


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
            await choose(ui, 6)  # 接入点；前面有独立实验能力开关
            await choose(ui, point_index(gateway, "recall"))
            await choose(ui, 0)  # mode：两个勾选项"开启 / 观察模式"
            assert "观察模式" in visible(ui.app)
            await press(ui, b" ")  # 勾选开启；新开启默认勾观察
            await press(ui, b"\t\r")  # 保存 → 开 + 观察 = observe
            assert settings(gateway.host, "read", {})["effective"]["points"]["recall"]["mode"] == "observe"
            await choose(ui, 6)
            await choose(ui, point_index(gateway, "recall"))
            await choose(ui, 0)
            await press(ui, b"\x1b[B ")  # 下移到观察模式并取消 → 正式使用
            await press(ui, b"\t\r")
            assert settings(gateway.host, "read", {})["effective"]["points"]["recall"]["mode"] == "apply"
            await choose(ui, 7)  # reset 列表
            await choose(ui, 0)  # enabled
            assert "enabled" not in settings(gateway.host, "read", {})["overrides"]["owner"]
            await choose(ui, 8)  # 原服务商管理入口
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
            await choose(ui, 5)  # thread 接入点菜单
            assert "后台记忆整理（用户长期）" not in visible(ui.app)
            await choose(ui, point_index(gateway, "recall", thread=True))
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
            await choose(ui, 10)
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
            await choose(ui, 10)
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
            await choose(ui, 6)  # 恢复继承仅清理旧存储，不重新授权新覆盖
            assert "此范围不消费，仅可清理" in visible(ui.app)
            await press(ui, "\r")
            view = settings(gateway.host, "read", {"scope": "thread"}, thread_id=gateway.thread.thread_id)
            assert view["overrides"]["thread"] == {}
            assert [(op, body["decision"]["fields"]) for op, body in gateway.calls if op == "decision_reset"] == [
                ("decision_reset", ["points.curator.mode"])]
            assert not any(op == "decision_patch" for op, _ in gateway.calls)
    asyncio.run(scenario())


def test_pipe_experiment_boolean_can_save_and_reset_without_creating_authorization(tmp_path):
    async def scenario():
        gateway = Gateway(tmp_path)
        async with running(tmp_path, gateway) as ui:
            await open_scope(ui, thread=True)
            assert "实验能力（仍需独立授权）：关闭" in visible(ui.app)
            await choose(ui, 4)
            assert "不建立实验许可" in visible(ui.app) and "当前联网实验不可用" in visible(ui.app)
            await press(ui, b"\x1b[B\r")
            view = settings(gateway.host, "read", {"scope": "thread"}, thread_id=gateway.thread.thread_id)
            assert view["effective"]["experiment_enabled"] is True
            assert view["experiment_authorization"] is None
            await choose(ui, 6)  # 原恢复列表也必须能显示新布尔字段
            assert "实验能力（仍需独立授权）：开启" in visible(ui.app)
            await press(ui, "\r")
            view = settings(gateway.host, "read", {"scope": "thread"}, thread_id=gateway.thread.thread_id)
            assert "experiment_enabled" not in view["overrides"]["thread"]
            assert view["effective"]["experiment_enabled"] is False and view["experiment_authorization"] is None
            assert not hasattr(gateway.host, "_model_call_ledger")
            assert not any(op in {"probe", "decision_probe", "decision_experiment_authorize"} for op, _ in gateway.calls)
    asyncio.run(scenario())


def test_menu_points_follow_the_schema_registry_and_reset_can_list_pre_recall(tmp_path):
    from agent_py_agent.agent.settings.decision_settings_schema import POINTS
    from agent_py_agent.cli.chat_parts.tui_decision_menu import _reset_label

    gateway = Gateway(tmp_path)
    assert list(_POINTS) == list(POINTS), "菜单接入点必须与 schema 登记一致，不能另维护一份会漏项的清单"
    patch(gateway.host, {"points.pre_recall.mode": "apply"})
    view = settings(gateway.host, "read", {})
    assert "points.pre_recall.mode" in _fields(view)
    assert "记忆召回前补充查询" in _reset_label(view, "points.pre_recall.mode"), "已有覆盖的恢复列表不能因缺显示名崩溃"
