# LLM: 离线 echo 宿主与真实 run/工具循环装配函数核对前台管理员事实；存储只落 pytest 临时目录，不发网络请求。
# 模块用途: 锁定 W6 前台事实与渠道 actor 从 RunParams 进入 ToolLoopExecuteParams；后台、调度、子代理、宿主只读片和模型属性仍不能打开。
from __future__ import annotations

from dataclasses import fields, replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.runtime import loop_support
from agent_py_agent.agent.agent_core.runtime.loop_models import (
    PreparedRuntimeContext,
    RunParams,
    RuntimeLoopParams,
    RuntimeToolLoopSeed,
)
from agent_py_agent.agent.agent_core.tool_runtime_ledger import write_boundary_with_runtime_ledger
from agent_py_agent.agent.backends.base import EchoBackend
from agent_py_agent.agent.conversation.host_actor import HostChannelActor
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests._tool_runtime_harness import (
    make_test_protocol_snapshot,
    runtime_snapshot_for_model_specs,
)

_HOST_FACTS = ("host_foreground_interactive", "host_channel_actor")


# LLM: 本机 local/main 宿主只用 echo 模型；home 与工作区都在 pytest 临时目录。
# 函数用途: 构造能走原 Agent.run 和工具账本的离线管理员宿主。
@pytest.fixture
def host(tmp_path):
    agent = SimpleAgent(AgentConfig(my_agent_home=str(tmp_path / "home")), tmp_path / "workspace")
    agent.backend = EchoBackend()
    return agent


# LLM: 依次经过 loop_support 的两段真实装配函数，不替换上下文准备或权限逻辑。
# 函数用途: 把一次宿主 RunParams 转成工具循环实际收到的参数。
def _tool_loop_params(run_params: RunParams) -> tuple[RuntimeLoopParams, ToolLoopExecuteParams]:
    snapshot = runtime_snapshot_for_model_specs((), run_id="w6-run")
    protocol = make_test_protocol_snapshot(run_id="w6-run")
    prepared = PreparedRuntimeContext([], [], None, None, "", tool_runtime_snapshot=snapshot,
                                      tool_protocol_snapshot=protocol)
    runtime = loop_support._runtime_loop_params("前台核对", prepared, run_params)
    seed = RuntimeToolLoopSeed(params=runtime, memories=[], tool_catalog_section="", tool_recommendations_section="",
                               tool_runtime_snapshot=snapshot, tool_protocol_snapshot=protocol)
    return runtime, loop_support._tool_loop_execute_params(SimpleNamespace(), seed)


# LLM: 宿主冻结的渠道身份；测试 home 未登记该管理员渠道身份。
# 函数用途: 构造一个合法但未核验为管理员的 IM 发起人。
def _actor() -> HostChannelActor:
    return HostChannelActor(owner_id="local/main", source_thread_id="thread-w6", entry_request_id="request-w6",
                            channel="feishu", conversation_id="", actor_id="im-actor-w6")


# LLM: 两段装配都只复制宿主 typed 字段；同一调用内 replace 续轮保留同一 actor 对象。
# 函数用途: 验证 RunParams 的前台事实与 actor 原样进入工具循环参数。
def test_run_params_carry_foreground_fact_and_actor_into_tool_loop_params():
    actor = _actor()
    runtime, loop = _tool_loop_params(RunParams(host_foreground_interactive=True, host_channel_actor=actor,
                                                source="gateway", context_scope="conversation"))
    assert type(runtime) is RuntimeLoopParams and type(loop) is ToolLoopExecuteParams
    assert runtime.host_foreground_interactive is True and runtime.host_channel_actor is actor
    assert loop.host_foreground_interactive is True and loop.host_channel_actor is actor
    continued = replace(loop, tool_rounds=loop.tool_rounds + 1)
    assert continued.host_foreground_interactive is True and continued.host_channel_actor is actor


# LLM: 只有严格 True 才是宿主签发的前台事实，真值类输入不能被放大。
# 函数用途: 验证非布尔或 False 的宿主值进入工具循环后保持关闭。
@pytest.mark.parametrize("value", [False, None, 1, "true"])
def test_non_boolean_foreground_value_is_not_carried(value):
    runtime, loop = _tool_loop_params(RunParams(host_foreground_interactive=value))
    assert runtime.host_foreground_interactive is False
    assert loop.host_foreground_interactive is False


# LLM: 两个字段只属本次宿主调用：不进 repr、不参与相等比较，默认关闭。
# 函数用途: 锁定新字段的 host-only 形状与默认值。
def test_host_facts_are_host_only_fields_with_closed_defaults():
    for cls in (RuntimeLoopParams, ToolLoopExecuteParams):
        specs = {item.name: item for item in fields(cls)}
        for name in _HOST_FACTS:
            assert specs[name].repr is False and specs[name].compare is False
    _, loop = _tool_loop_params(RunParams())
    assert loop.host_foreground_interactive is False and loop.host_channel_actor is None


# LLM: 账本按每次工具调用的参数重新核验；Full Access 仍由注册表逐调用裁决，这里保持 False。
# 函数用途: 验证本机 local/main 前台会话轮的工具循环参数在账本里得到前台事实。
def test_ledger_reports_foreground_for_local_admin_conversation_turn(host):
    _, loop = _tool_loop_params(RunParams(host_foreground_interactive=True, source="gateway",
                                          context_scope="conversation"))
    boundary = write_boundary_with_runtime_ledger(host, loop)
    assert boundary["host_foreground_interactive"] is True
    assert boundary["foreground_admin_full_access"] is False
    assert boundary["host_authorization_identity"]["actor"] == "local"


# LLM: 渠道 actor 只作审批诊断身份；未登记为管理员渠道身份的 actor 不能打开前台事实。
# 函数用途: 验证 IM 轮的审批身份取原 actor，且未核验 actor 保持关闭。
def test_ledger_identity_uses_channel_actor_and_unverified_actor_stays_closed(host):
    _, loop = _tool_loop_params(RunParams(host_foreground_interactive=True, host_channel_actor=_actor(),
                                          source="gateway", context_scope="conversation"))
    boundary = write_boundary_with_runtime_ledger(host, loop)
    assert boundary["host_authorization_identity"]["actor"] == "im-actor-w6"
    assert boundary["host_foreground_interactive"] is False


# LLM: 调度/子代理/后台的 RunParams 不带前台事实；即使误带，来源或范围也必须让账本关闭。
# 函数用途: 锁定非前台来源经工具循环参数后仍不打开宿主前台事实。
@pytest.mark.parametrize("changes", [
    {"host_foreground_interactive": False, "source": "gateway", "context_scope": "conversation"},
    {"source": "scheduler"},
    {"source": "subagent", "context_scope": "task_local"},
    {"host_foreground_interactive": True, "source": "scheduler"},
    {"host_foreground_interactive": True, "source": "subagent"},
    {"host_foreground_interactive": True, "source": "background_main_agent"},
    {"host_foreground_interactive": True, "context_scope": "task_local"},
])
def test_non_foreground_paths_stay_closed_through_tool_loop_params(host, changes):
    _, loop = _tool_loop_params(RunParams(**changes))
    assert write_boundary_with_runtime_ledger(host, loop)["host_foreground_interactive"] is False


# LLM: 模型可写 task_attributes 的同名键不是宿主事实来源。
# 函数用途: 验证属性里的同名值不会变成工具循环的前台事实或审批身份。
def test_task_attributes_cannot_supply_host_facts(host):
    attrs = {"host_foreground_interactive": True, "host_channel_actor": {"actor_id": "attr-actor"}}
    _, loop = _tool_loop_params(RunParams(task_attributes=attrs, source="gateway", context_scope="conversation"))
    boundary = write_boundary_with_runtime_ledger(host, loop)
    assert boundary["host_foreground_interactive"] is False
    assert boundary["host_authorization_identity"]["actor"] == "local"


# LLM: 宿主只读工具片按设计不携带前台事实，即使原绑定参数带 True 也保持关闭。
# 函数用途: 锁定 tool_only_execution 装配的工具循环参数仍 fail-closed。
def test_host_tool_only_slice_stays_closed(host):
    from agent_py_agent.agent.agent_core.tool_only_execution import _tool_only_params
    from agent_py_agent.agent.common.cancellation import CancellationToken

    bound = RunParams(host_foreground_interactive=True, host_channel_actor=_actor(), source="gateway",
                      context_scope="conversation")
    loop = _tool_only_params(bound, None, CancellationToken(), "read_file")
    assert getattr(loop, "host_channel_actor", None) is None
    assert write_boundary_with_runtime_ledger(host, loop)["host_foreground_interactive"] is False


# LLM: 经原 Agent.run 生命周期与可信循环驱动接缝，驱动内即工具执行所用参数；echo 模型不发网络请求。
# 函数用途: 验证真实本机前台回合进入工具循环时账本得到前台事实，普通回合保持关闭。
@pytest.mark.parametrize("foreground", [True, False])
def test_real_run_turn_reaches_tool_loop_with_foreground_fact(host, foreground):
    seen = []

    # LLM: 只记录工具循环收到的参数类型与账本结果，随后交回真实循环。
    # 函数用途: 在工具执行入口观察本轮宿主前台事实。
    def driver(agent, params):
        seen.append((type(params), write_boundary_with_runtime_ledger(agent, params)["host_foreground_interactive"]))
        return loop_support.execute_tool_loop(agent, params)

    host._loop_driver = driver
    result = host.run("前台核对", params=RunParams(host_foreground_interactive=foreground), source="chat",
                      save=False, resume_context=False, allowed_tools=[])
    assert seen == [(ToolLoopExecuteParams, foreground)]
    assert result.runtime_status == "ok"
