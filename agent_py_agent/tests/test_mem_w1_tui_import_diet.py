from __future__ import annotations

# LLM: MEM-W1 只把 Gateway 瘦客户端 TUI 启动链上三条"为一个常量/DTO 加载整套服务端模块"的边改成按需导入；
#   这里在全新解释器里观察真实导入集合，并核对三处行为（运行上下文默认值、后台转录 schema 常量、模型菜单错误映射）不变。
# 模块用途: 回归保护 TUI 进程不再常驻 runtime_db/scheduler/capability/memory_archive/model_profiles 链（实测每进程约 17 MB）。
import subprocess
import sys

from agent_py_agent.cli.chat_parts import tui_agent_navigation, tui_model_menu

# LLM: tui_background_agents 由 tui.py 在 TUI 启动时按需导入（后台观察），真实 TUI 进程一定会加载，所以也列入入口。
_TUI_ENTRY_MODULES = (
    "agent_py_agent.cli.chat",
    "agent_py_agent.cli.chat_client_context",
    "agent_py_agent.cli.chat_parts.tui",
    "agent_py_agent.cli.chat_parts.tui_ui_setup",
    "agent_py_agent.cli.chat_parts.tui_agent_navigation",
    "agent_py_agent.cli.chat_parts.tui_model_menu",
    "agent_py_agent.cli.chat_parts.tui_background_agents",
)
_SERVER_ONLY_MODULES = (
    "agent_py_agent.agent.conversation.background_shutdown",
    "agent_py_agent.agent.conversation.background_transcript",
    "agent_py_agent.agent.runtime_db.repository",
    "agent_py_agent.agent.scheduler",
    "agent_py_agent.agent.capability",
    "agent_py_agent.agent.memory_archive",
    "agent_py_agent.agent.settings.model_profiles",
    "agent_py_agent.agent.settings.thread_model_selection",
)


# LLM: 必须用新解释器：同一 pytest 进程里别的用例早已导入服务端模块，sys.modules 观察会失真。
# 函数用途: 确认导入 TUI 启动链后，服务端专用模块一个都没有被加载。
def test_fresh_tui_import_chain_does_not_load_server_only_modules(tmp_path):
    script = (
        "import importlib, sys\n"
        f"for name in {_TUI_ENTRY_MODULES!r}:\n"
        "    importlib.import_module(name)\n"
        f"loaded = [name for name in {_SERVER_ONLY_MODULES!r} if name in sys.modules]\n"
        "print('LOADED=' + ','.join(loaded))\n"
    )
    env = {"HOME": str(tmp_path), "MY_AGENT_HOME": str(tmp_path / ".my-agent"), "PATH": "/usr/bin:/bin"}
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, timeout=120, check=False)
    assert result.returncode == 0, result.stderr[-2000:]
    assert "LOADED=\n" in result.stdout or result.stdout.strip() == "LOADED=", result.stdout


# LLM: 默认值改为按需构造后，Gateway 运行上下文仍必须各自拿到一个真实且独立的 BackgroundShutdown。
# 函数用途: 核对 GatewayRunContext 的 background_shutdown 默认值类型与实例独立性不变。
def test_gateway_run_context_default_background_shutdown_is_real_and_per_instance():
    from dataclasses import fields

    from agent_py_agent.agent.conversation.background_shutdown import BackgroundShutdown
    from agent_py_agent.cli.models import GatewayRunContext

    field = next(item for item in fields(GatewayRunContext) if item.name == "background_shutdown")
    first, second = field.default_factory(), field.default_factory()
    assert type(first) is BackgroundShutdown and type(second) is BackgroundShutdown
    assert first is not second


# LLM: 导航器改用 tui_runtime 已有的同值常量过滤后台转录事件；与服务端常量不一致会静默丢掉全部后台转录。
# 函数用途: 核对 TUI 侧转录 schema 常量与服务端 background_transcript 的权威常量相同。
def test_tui_transcript_schema_matches_server_constant():
    from agent_py_agent.agent.conversation.background_transcript import BACKGROUND_TRANSCRIPT_SCHEMA

    assert tui_agent_navigation.BACKGROUND_TRANSCRIPT_SCHEMA == BACKGROUND_TRANSCRIPT_SCHEMA


# LLM: Gateway 瘦客户端只发认证请求；本地模式抛出的 ModelProfileError 仍要映射成固定 ok=False 文案。
# 函数用途: 核对模型菜单在两种模式下的返回值与错误映射不变。
def test_model_menu_request_keeps_gateway_and_local_error_semantics(monkeypatch):
    from agent_py_agent.agent.settings import thread_model_selection
    from agent_py_agent.agent.settings.model_provider_schema import ModelProfileError

    class _GatewayClient:
        gateway_client_only = True

        def request_models(self, *, session_id, operation, payload):
            return {"ok": True, "session_id": session_id, "operation": operation, "payload": payload}

    assert tui_model_menu._request_data_sync(_GatewayClient(), "s1", "list", {"a": 1}) == {
        "ok": True, "session_id": "s1", "operation": "list",
        "payload": {"a": 1, "metadata": {"client_capabilities": {"tui_streaming": True}}}}

    def _raise(*_args, **_kwargs):
        raise ModelProfileError("模型配置结构无效。")

    monkeypatch.setattr(thread_model_selection, "execute_local_model_operation", _raise)
    assert tui_model_menu._request_data_sync(object(), "s1", "list", {}) == {"ok": False, "message": "模型配置结构无效。"}


def test_renderer_and_subagent_models_share_canonical_ended_protocol():
    from agent_py_agent.agent.contracts.protocol_status import SUBAGENT_ENDED_STATUSES
    from agent_py_agent.agent.subagents.models import SUBAGENT_ENDED_STATUSES as server_statuses
    from agent_py_agent.agent.subagents.models import TaskStatus
    from agent_py_agent.cli.chat_parts.tui_block_renderer import (
        SUBAGENT_ENDED_STATUSES as renderer_statuses,
    )

    assert renderer_statuses is server_statuses is SUBAGENT_ENDED_STATUSES
    assert all(TaskStatus(value).value == value for value in SUBAGENT_ENDED_STATUSES)
