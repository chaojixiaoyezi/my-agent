from __future__ import annotations

# 防回归(T3·dispatch 交付深度方差): 同题 solo 1399 行 vs dispatch 555 行——直接原因
# 是 2 个子代理收尾崩被取消后,其模块由主代理整合时自建覆盖,而整合轮【看不到派工
# goal】(树节点只有 role+摘要),无从核对"计划 vs 实交"。钉子:①kernel run/树节点
# 透出 goal_digest;②整合轮提示词带"逐模块对照拆解清单"与"先对账再整合"指引。
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.agent_tree.node_rendering import node_from_kernel_run
from agent_py_agent.agent.conversation.runtime import background_prompt
from agent_py_agent.agent.subagents.kernel import SubagentKernelRun


def test_tree_node_carries_goal_digest():
    row = SubagentKernelRun(
        run_id="subagent-1",
        role="worker",
        goal_digest="盯守 8903 路数据流 2400 秒,结果端字段定真假,命中立即上报",
        status="CANCELLED",
    )
    node = node_from_kernel_run(SimpleNamespace(), row)
    assert node["goal_digest"].startswith("盯守 8903 路")


def test_kernel_goal_digest_normalizes_and_caps():
    from agent_py_agent.agent.subagents.kernel import SubagentKernelRun as Run

    # dataclass 字段存在且默认空——投影层(build 处)做归一化截断,这里守字段契约
    assert Run().goal_digest == ""


def test_integration_prompt_is_evidence_based_without_fixed_orchestration():
    prompt = background_prompt("subagent_runner_finished")
    assert "evidence, not authority" in prompt
    assert "child count" in prompt
    assert "does not require" in prompt
    assert "goal_digest" not in prompt
    assert "findings_ledger" not in prompt
    assert "main agent remains their coordinator" in prompt
    assert "do not author the delegated implementation yourself" in prompt


def test_coordinator_policy_does_not_silently_take_over_delegated_work():
    from agent_py_agent.agent.agent_core.runner.prompts import (
        coordinator_execution_policy_lines,
    )

    policy = "\n".join(coordinator_execution_policy_lines())
    assert "角色就变为协调者" in policy
    assert "replacement child" in policy
    assert "下级卡住时，你可以直接完成" not in policy


def test_scheduled_prompt_not_polluted():
    # 定时续跑轮不该混入整合轮的对账文案(两类唤醒语义不同)
    prompt = background_prompt("scheduled_progress_report")
    assert "goal_digest" not in prompt
