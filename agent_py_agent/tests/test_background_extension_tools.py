"""后台续跑白名单按注册表的结构化类型事实并入插件/MCP 代理工具；显式配置或任务白名单不并入。"""
from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.conversation.background_tool_policy import (
    EXTENSION_TOOLS_INHERIT,
    EXTENSION_TOOLS_NONE,
    BackgroundToolPolicyRequest,
    background_tool_policy_decision,
)
from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest, _run_params
from agent_py_agent.agent.plugin_runtime import plugin_tool_name
from agent_py_agent.tests.plugin_activation_fixtures import (
    installed_runtime_plugin,
    plugin_registry,
)

PLUGIN_TOOL = "plugin__genui_lite_a39ac1a1__export_d46aee08"


# 类用途: 假注册表，只记录扩展目录被读取的次数，不连接任何进程。
class _Registry:
    def __init__(self, names):
        self.names = list(names)
        self.prepared = 0

    def extension_tool_names(self):
        self.prepared += 1
        return list(self.names)


# 函数用途: 组一个只带 config/tools/owner_policy 的最小 agent，模拟 owner Agent 进入后台续跑。
def _agent(registry, **config):
    values = {"enable_tools": True, "background_main_agent_allowed_tools": []}
    values.update(config)
    return SimpleNamespace(config=SimpleNamespace(**values), tools=registry, owner_policy=None)


def _lifecycle_request(reason: str = "subagent_runner_finished") -> BackgroundRunRequest:
    return BackgroundRunRequest(thread_id="thread-1", task_id="task-1", reason=reason)


def test_default_profiles_inherit_extension_tools_and_explicit_lists_do_not():
    default = background_tool_policy_decision(request=BackgroundToolPolicyRequest(reason="subagent_runner_finished"))
    assert default.profile == "subagent_integration"
    assert default.extension_tools == EXTENSION_TOOLS_INHERIT
    assert not any(name.startswith("plugin__") for name in default.allowed_tools), "决策本身不读注册表"
    projected = default.to_dict()
    assert projected["schema_version"] == "background-tool-policy.v2"
    assert projected["extension_tools"] == EXTENSION_TOOLS_INHERIT

    configured = background_tool_policy_decision(
        SimpleNamespace(background_main_agent_allowed_tools=["read_file"]),
        request=BackgroundToolPolicyRequest(reason="subagent_runner_finished"),
    )
    assert configured.profile == "configured" and configured.extension_tools == EXTENSION_TOOLS_NONE

    task_scoped = background_tool_policy_decision(
        request=BackgroundToolPolicyRequest(
            reason="subagent_runner_finished",
            policy_snapshot={"allowed_tools": ["read_file", "write_file"]},
        )
    )
    assert task_scoped.allowed_tools == ("read_file", "write_file")
    assert task_scoped.extension_tools == EXTENSION_TOOLS_NONE


def test_lifecycle_wake_run_params_include_registry_extension_tools():
    registry = _Registry([PLUGIN_TOOL, "read_file"])
    params = _run_params("thread-1", _lifecycle_request(), _agent(registry))
    assert params.allowed_tools is not None
    assert PLUGIN_TOOL in params.allowed_tools
    assert params.allowed_tools.count("read_file") == 1, "已在核心目录的名称不重复"
    assert {"create_subagents", "write_file", "run_command"}.issubset(params.allowed_tools)
    assert registry.prepared == 1


def test_explicit_configuration_keeps_extension_tools_out_and_scheduled_jobs_keep_full_catalog():
    registry = _Registry([PLUGIN_TOOL])
    configured = _run_params(
        "thread-1", _lifecycle_request(),
        _agent(registry, background_main_agent_allowed_tools=["read_file", "write_file"]),
    )
    assert configured.allowed_tools == ["read_file", "write_file"]
    scheduled = _run_params("thread-1", _lifecycle_request("scheduled_job_due"), _agent(registry))
    assert scheduled.allowed_tools is None, "定时任务是完整代理回合，注册表自己决定目录"
    assert registry.prepared == 0


def test_registry_failure_or_tools_disabled_falls_back_to_core_catalog(caplog):
    class _Broken:
        def extension_tool_names(self):
            raise OSError("mcp down")

    with caplog.at_level("WARNING", logger="agent.conversation.runtime"):
        params = _run_params("thread-1", _lifecycle_request(), _agent(_Broken()))
    assert params.allowed_tools and not any(name.startswith("plugin__") for name in params.allowed_tools)
    assert any("OSError" in record.getMessage() for record in caplog.records)
    assert not any("mcp down" in record.getMessage() for record in caplog.records), "只记类型，不记异常正文"

    registry = _Registry([PLUGIN_TOOL])
    params = _run_params("thread-1", _lifecycle_request(), _agent(registry, enable_tools=False))
    assert PLUGIN_TOOL not in params.allowed_tools and registry.prepared == 0


def test_registry_extension_tool_names_follow_plugin_activation(tmp_path):
    service = installed_runtime_plugin(tmp_path)
    registry = plugin_registry(service)
    name = plugin_tool_name("sample-peek", "read")
    try:
        assert registry.extension_tool_names() == [], "已安装未启用：没有扩展目录"
        assert "read_file" in registry.tools
        enabled = service.command("/plugins enable sample-peek", revision=service.catalog().revision, request_id="enable")
        assert enabled["state"] == "succeeded", enabled
        names = registry.extension_tool_names()
        assert name in names and "read_file" not in names, "按代理类型判定，核心工具永远不算扩展目录"
        disabled = service.command("/plugins disable sample-peek", revision=service.catalog().revision, request_id="disable")
        assert disabled["state"] == "succeeded", disabled
        assert name not in registry.extension_tool_names(), "停用后目录贡献移除，不靠名称前缀残留"
    finally:
        registry.close_mcp_clients()
