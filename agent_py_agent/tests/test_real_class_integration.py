"""真实类组装的端到端集成测试（不 mock 被测类）。

背景：现有 e2e 测试大多由 mock 驱动，mock 测试验证的是 mock 行为；
"调参不生效"这类 bug（参数被建议了、却没真正写回任务并被下一轮读取）
只有真实链路才能捕获。本文件全部用 tmp_path 实例化真实 SubAgentManager /
SimpleAgent，所有断言落在真实中间状态上：task.status、落盘文件、attributes。

约定：
- 不用 MagicMock/patch 模拟任何被测类；唯一允许的替身是模型 backend
  （fake backend 不是被测类），本文件用一个"调用即报错"的 backend 兜底，
  保证以下链路全程不需要模型、也绝不触发网络。
- 全部测试共计应在 30 秒内跑完。

覆盖的 6 条核心路径（每条一个测试函数）：
a. test_create_run_persists_real_files_and_load_roundtrip
   创建→落盘：canonical_state.json / 工单文件真实存在，load 回读一致。
b. test_failure_introspection_params_actually_take_effect（最关键）
   失败自省（FailureIntrospector 规则路径，无 LLM）建议的参数被
   _apply_introspection_params 真正应用：dynamic_timeout_seconds /
   max_tool_rounds 写入任务 attributes、落盘后由下一轮 runner 的真实读取方
   （get_task_timeout / _effective_max_tool_rounds）拿到新值。
c. test_runner_result_delivers_real_artifact_to_declared_location
   runner result 写回→产物搬运：子代理写锚定落点的真实文件，
   record_runner_result 把它搬到声明意图位置并记录 delivered 账本。
d. test_capability_request_grant_closes_loop_and_extends_write_boundary
   capability request→grant 闭环：请求 GRANTED、grant 落盘、
   runner 写边界即时包含新目录。
e. test_status_transitions_persist_and_cancel_is_terminal
   状态流转经 save/load 往返一致；DONE 需要证据；CANCELLED 是
   dispatch 不可再入的终态；非法状态被协议拒绝。
f. test_child_run_links_into_parent_tree_and_kernel_snapshot
   子代理树：create_run 带 parent_id 后父任务 child_ids 含子任务，
   kernel 树快照能看到层级。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core._tool_loop_service import _effective_max_tool_rounds
from agent_py_agent.agent.agent_core.runner.timeout_policy import get_task_timeout
from agent_py_agent.agent.backends import BaseBackend, ModelResponse
from agent_py_agent.agent.core import ResolveCapabilityRequestsTool, SimpleAgent
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.subagents.kernel import SubagentKernelQuery
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.manager_runner_result_payload import RecordRunnerResultParams
from agent_py_agent.agent.subagents.models import (
    FailureType,
    SubAgentParsedOutput,
    SubAgentRunnerResult,
    task_is_dispatch_ineligible,
)
from agent_py_agent.agent.subagents.services.base import CreateRunParams
from agent_py_agent.agent.subagents.services.lifecycle import (
    RecordCapabilityRequestParams,
    RecordEvidenceParams,
    SetStatusParams,
)
from agent_py_agent.agent.subagents.services.output_alignment import (
    anchored_output_refs,
    delivery_root,
)

pytestmark = pytest.mark.integration


class _NoModelCallBackend(BaseBackend):
    """fake backend：这些链路全程不该走模型，一旦被调用立即失败。

    注意这是替换模型 backend（外部依赖），不是 mock 被测类。
    """

    name = "fake_no_model_call_backend"

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        raise AssertionError("真实类集成测试不应触发任何模型调用")


def _build_agent(tmp_path: Path) -> SimpleAgent:
    """组装真实 SimpleAgent：echo backend 不碰网络，home 隔离进 tmp_path。"""
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=True,
            memory_path="memory.jsonl",
            subagent_workspace="subs",
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
        ),
        tmp_path,
    )
    agent.backend = _NoModelCallBackend()
    return agent


def _create_task(manager: SubAgentManager, **overrides):
    params = {
        "goal": "真实类集成测试目标",
        "thought": "先验证真实落盘，再验证回读",
        "plan": ["创建任务", "校验工单"],
        "role": "worker",
    }
    params.update(overrides)
    return manager.create_run(params=CreateRunParams(**params))


# ---------------------------------------------------------------------------
# a. 创建→dispatch 链路：真实文件落盘 + load 回读一致
# ---------------------------------------------------------------------------


def test_create_run_persists_real_files_and_load_roundtrip(tmp_path: Path) -> None:
    manager = SubAgentManager(workspace=tmp_path / "workspace")
    task = _create_task(manager, attributes={"output_files": ["proj/app.py"]})

    assert task.status == "PLANNING"

    # canonical_state.json 真实存在，且 attributes 里登记了它的引用
    canonical = Path(task.agent_run_workspace_dir) / "canonical_state.json"
    assert canonical.exists()
    assert task.attributes["canonical_state_ref"] == str(canonical)
    payload = json.loads(canonical.read_text(encoding="utf-8"))
    assert payload["id"] == task.id
    assert payload["goal"] == task.goal
    assert payload["status"] == "PLANNING"

    # 工单文件真实落盘（不是字段里写个路径就算数）
    for file_field in ("status_file", "work_log_file", "acceptance_file", "output_json", "checkpoint_json"):
        assert Path(getattr(task, file_field)).exists(), f"工单文件缺失: {file_field}"
    validation = manager.validate_work_order(task.id)
    assert validation.ok is True
    assert validation.missing == []

    # load 回读与创建时一致
    loaded = manager.load(task.id)
    assert loaded.id == task.id
    assert loaded.goal == task.goal
    assert loaded.plan == task.plan
    assert loaded.task_dir == task.task_dir
    assert loaded.attributes["output_files"] == ["proj/app.py"]


# ---------------------------------------------------------------------------
# b. 失败自省调参真正生效（最关键）
# ---------------------------------------------------------------------------


def test_failure_introspection_params_actually_take_effect(tmp_path: Path) -> None:
    agent = _build_agent(tmp_path)
    task = agent.subagents.create_run(
        goal="会超时的长任务", thought="t", plan=["step1", "step2"], role="worker"
    )

    # 制造一次真实的"runner 超时"失败现场并落盘
    task = agent.subagents.load(task.id)
    task.status = "TIMEOUT"
    task.failure_type = FailureType.RUNNER_TIMEOUT.value
    task.runner_attempts = 1
    agent.subagents.save(task)

    before = agent.subagents.load(task.id)
    assert "dynamic_timeout_seconds" not in before.attributes

    runner_result = SubAgentRunnerResult(
        run_id=task.id,
        dry_run=False,
        ok=False,
        status="TIMEOUT",
        verification_status="UNVERIFIED",
        message="runner timeout",
        runner_attempts=1,
        runner_last_error="timeout after 120s",
    )

    # 真实自省链路：SubAgentFailureAnalyzer（规则）→ FailureIntrospector（规则回退，
    # 无 LLM）→ _apply_introspection_params → manager.save。
    # 注意 _handle_failure_introspection 内部吞异常只打日志，所以下面对落盘
    # attributes 的硬断言正是这条测试存在的意义：链路里任何一环断了都会在这里暴露。
    agent._handle_failure_introspection(task.id, before, runner_result)

    reloaded = agent.subagents.load(task.id)
    introspection = reloaded.attributes["failure_introspection_data"]
    assert introspection["root_cause"] == "timeout"
    assert introspection["should_retry"] is True
    # 规则：当前超时 120s（默认）* 1.5 = 180s
    assert introspection["suggested_params"]["new_timeout_seconds"] == pytest.approx(180.0)

    # 建议的参数被 _apply_introspection_params 真正写进任务并落盘
    assert reloaded.attributes["dynamic_timeout_seconds"] == pytest.approx(180.0)

    # 下一轮 runner 的真实读取方拿到新值（get_task_timeout 是 dispatch 给 runner
    # 算超时的唯一入口；runner_timeout_seconds="auto" 表示不禁用、走动态超时）
    next_round_config = AgentConfig(runner_timeout_seconds="auto")
    assert get_task_timeout(reloaded, 0.0, next_round_config) == pytest.approx(180.0)

    # max_tool_rounds 分支：同一个真实应用入口 + 落盘 + 下一轮真实读取方
    assert _effective_max_tool_rounds(agent, _tool_loop_params(dict(reloaded.attributes), task.id)) == 0
    agent._apply_introspection_params(reloaded, {"max_tool_rounds": 7})
    agent.subagents.save(reloaded)
    after_rounds = agent.subagents.load(task.id)
    assert after_rounds.attributes["max_tool_rounds"] == 7
    assert _effective_max_tool_rounds(agent, _tool_loop_params(dict(after_rounds.attributes), task.id)) == 7


def _tool_loop_params(task_attributes: dict, run_id: str) -> ToolLoopExecuteParams:
    """构造真实 ToolLoopExecuteParams（纯数据 bundle），喂给真实读取方。"""
    return ToolLoopExecuteParams(
        user_prompt="",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        write_boundary=None,
        task_attributes=task_attributes,
        request_id="req-real-class-it",
        run_id=run_id,
        task_id=run_id,
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
    )


# ---------------------------------------------------------------------------
# b2. 失败自省 split 建议真正拆出子任务（生产→消费打通钉子）
# ---------------------------------------------------------------------------


def _write_capability_config_yaml(tmp_path: Path, *, auto_split: bool) -> Path:
    """写一份真实 capability_config.yaml（覆盖 yaml→dataclass 字段解析路径）。"""
    path = tmp_path / "capability_config.yaml"
    path.write_text(
        "enable_capability_routing: true\n"
        f"subagent_failure_auto_split_enabled: {'true' if auto_split else 'false'}\n"
        "subagent_failure_split_max_depth: 2\n",
        encoding="utf-8",
    )
    return path


def _timeout_exhausted_task(agent: SimpleAgent):
    """制造"重试次数耗尽的超时任务"现场：规则分析必产 should_split+建议。"""
    task = agent.subagents.create_run(
        goal="重试耗尽待拆分任务", thought="t", plan=["s1", "s2", "s3", "s4"], role="worker"
    )
    task = agent.subagents.load(task.id)
    task.status = "TIMEOUT"
    task.failure_type = FailureType.RUNNER_TIMEOUT.value
    task.runner_attempts = 3  # >= 默认 max_retry_attempts(3) → split_task 建议
    agent.subagents.save(task)
    return agent.subagents.load(task.id)


def _timeout_runner_result(run_id: str) -> SubAgentRunnerResult:
    return SubAgentRunnerResult(
        run_id=run_id,
        dry_run=False,
        ok=False,
        status="TIMEOUT",
        verification_status="UNVERIFIED",
        message="runner timeout",
        runner_attempts=3,
        runner_last_error="timeout after 120s",
    )


def test_failure_introspection_split_spawns_real_subtasks_when_enabled(tmp_path: Path) -> None:
    agent = _build_agent(tmp_path)
    # 真实配置链路：yaml → load_capability_config → dataclass → 消费方
    agent.capability_config_path = str(_write_capability_config_yaml(tmp_path, auto_split=True))
    before = _timeout_exhausted_task(agent)

    agent._handle_failure_introspection(before.id, before, _timeout_runner_result(before.id))

    # 原任务：磁盘回读确认转 TAKEN_OVER（dispatch 不可再入）+ split 账本落盘
    reloaded = agent.subagents.load(before.id)
    data = reloaded.attributes["failure_introspection_data"]
    assert data["should_split"] is True
    assert data["split_applied"] is True
    split_ids = data["split_into"]
    assert split_ids and split_ids == reloaded.attributes["split_into"]
    assert reloaded.status == "TAKEN_OVER"
    assert task_is_dispatch_ineligible(reloaded) is True
    assert reloaded.child_ids == split_ids

    # 子任务：真实落盘、可 load、PLANNING 可派工、goal/plan/谱系正确
    for index, sub_id in enumerate(split_ids, start=1):
        sub = agent.subagents.load(sub_id)
        assert sub.parent_id == reloaded.id
        assert sub.depth == reloaded.depth + 1
        assert sub.status == "PLANNING"
        assert task_is_dispatch_ineligible(sub) is False
        assert f"第{index}部分" in sub.goal
        canonical = Path(sub.agent_run_workspace_dir) / "canonical_state.json"
        assert canonical.exists(), f"子任务 {sub_id} 的 canonical_state.json 未落盘"


def test_failure_introspection_split_disabled_by_default_records_skip(tmp_path: Path) -> None:
    # 不写 capability_config.yaml：走"无配置文件→全部默认值→自动拆分关闭"路径
    agent = _build_agent(tmp_path)
    before = _timeout_exhausted_task(agent)

    agent._handle_failure_introspection(before.id, before, _timeout_runner_result(before.id))

    reloaded = agent.subagents.load(before.id)
    data = reloaded.attributes["failure_introspection_data"]
    assert data["should_split"] is True
    assert data["split_applied"] is False
    assert data["split_skipped_reason"] == "auto_split_disabled"
    # 默认关闭时行为与历史一致：不拆分、原任务状态不动、无 split 痕迹
    assert reloaded.status == "TIMEOUT"
    assert "split_into" not in reloaded.attributes
    assert reloaded.child_ids == []


# ---------------------------------------------------------------------------
# c. runner result 写回→产物搬运到声明位置
# ---------------------------------------------------------------------------


def test_runner_result_delivers_real_artifact_to_declared_location(tmp_path: Path) -> None:
    manager = SubAgentManager(workspace=tmp_path / "workspace")
    task = _create_task(manager, attributes={"output_files": ["proj/app.py"]})

    # 家目录方案：相对声明直接锚到任务交付区 tasks/<日期>/<任务>/output/，
    # 子代理直接写、用户拿走即可，无需"子代理家→搬运"两段式（delivery_map 空）。
    anchoring = anchored_output_refs(task)
    assert anchoring.delivery_map == []
    delivery = delivery_root(task)
    anchored_target = Path(anchoring.anchored_refs[0])
    assert str(anchored_target).startswith(delivery)
    assert anchored_target.as_posix().endswith("proj/app.py")

    # 子代理写真实文件到交付区落点
    anchored_target.parent.mkdir(parents=True, exist_ok=True)
    anchored_target.write_text("print('real artifact')\n", encoding="utf-8")

    result = manager.runner_result.record_runner_result(
        RecordRunnerResultParams(
            run_id=task.id,
            dry_run=False,
            ok=True,
            message="done",
            structured_output=SubAgentParsedOutput(
                found=True,
                ok=True,
                status="DONE",
                summary="产物已写入交付区",
                findings=[],
                next_actions=[],
            ),
        )
    )
    assert result.ok is True
    assert result.status == "DONE"

    # 产物真实落在任务交付区，用户拿走即可
    assert anchored_target.exists()
    assert anchored_target.read_text(encoding="utf-8") == "print('real artifact')\n"

    loaded = manager.load(task.id)
    assert loaded.status == "DONE"


# ---------------------------------------------------------------------------
# d. capability request→grant 闭环
# ---------------------------------------------------------------------------


def test_capability_request_grant_closes_loop_and_extends_write_boundary(tmp_path: Path) -> None:
    agent = _build_agent(tmp_path)
    task = agent.subagents.create_run(
        goal="写共享交付目录",
        thought="目标目录在写权限外，需要父级授权",
        plan=["申请权限", "写文件"],
        allowed_tools=["write_file"],
    )
    granted_dir = str(Path(task.task_workspace_dir) / "output" / "shared-deliverables")

    request = agent.subagents.lifecycle.record_capability_request(
        task.id,
        RecordCapabilityRequestParams(
            problem="目标路径不在 allowed_write_roots，写入会被拒",
            needed_capability="unlock_output_directory",
            capability_type="filesystem",
            path_scope=[granted_dir],
        ),
    )
    assert request.status == "OPEN"

    # 提交即落盘；grant 之前写边界不含目标目录
    persisted = agent.subagents.load(task.id)
    assert persisted.capability_requests[0].status == "OPEN"
    boundary_before = agent.subagents.runner_context._build_write_boundary(persisted)
    assert granted_dir not in boundary_before["allowed_write_roots"]

    result = ResolveCapabilityRequestsTool(agent).execute(
        {"run_id": task.id, "decision": "grant", "reason": "解锁共享交付目录"}
    )
    payload = json.loads(result.output)
    assert result.ok and payload["ok"]
    assert payload["resolved"][0]["status"] == "GRANTED"

    # 请求 GRANTED、grant 落盘（从磁盘重新 load 验证）
    reloaded = agent.subagents.load(task.id)
    assert reloaded.capability_requests[0].status == "GRANTED"
    assert reloaded.capability_grants, "grant 必须持久化进任务"
    assert reloaded.attributes["capability_resolution_wake"]["decision"] == "grant"

    # grant 即时生效：runner 写边界包含新目录
    boundary_after = agent.subagents.runner_context._build_write_boundary(reloaded)
    assert granted_dir in boundary_after["allowed_write_roots"]
    # P3-2:grant 的 path_scope 同时开放读取(读是写的最低权限子集)——
    # R5a"中途求读权限、grant 后读边界不扩"的针对修复。
    assert granted_dir in boundary_after["allowed_read_roots"]


# ---------------------------------------------------------------------------
# e. 任务状态流转持久化 + 取消终态
# ---------------------------------------------------------------------------


def test_status_transitions_persist_and_cancel_is_terminal(tmp_path: Path) -> None:
    manager = SubAgentManager(workspace=tmp_path / "workspace")
    task = _create_task(manager)
    assert task.status == "PLANNING"

    # RUNNING 经 save/load 往返一致
    manager.lifecycle.set_status(SetStatusParams(run_id=task.id, status="RUNNING"))
    assert manager.load(task.id).status == "RUNNING"

    # 无证据时不允许 DONE，且失败的变更不落盘
    with pytest.raises(ValueError):
        manager.lifecycle.set_status(
            SetStatusParams(run_id=task.id, status="DONE", require_evidence=True)
        )
    assert manager.load(task.id).status == "RUNNING"

    # 补真实证据后 DONE 成功，终态字段落盘
    manager.lifecycle.record_evidence(
        task.id,
        RecordEvidenceParams(kind="test", summary="pytest 通过", command="pytest -q", ok=True),
    )
    manager.lifecycle.set_status(
        SetStatusParams(run_id=task.id, status="DONE", require_evidence=True)
    )
    done = manager.load(task.id)
    assert done.status == "DONE"
    assert done.verification_status == "VERIFIED"
    assert done.ended_at > 0

    # cancel 后状态终态：不再进入 dispatch 候选
    cancelled_task = _create_task(manager, goal="将被取消的任务")
    manager.lifecycle.set_status(
        SetStatusParams(run_id=cancelled_task.id, status="CANCELLED", failure_type="cancelled")
    )
    cancelled = manager.load(cancelled_task.id)
    assert cancelled.status == "CANCELLED"
    assert cancelled.failure_type == "cancelled"
    assert task_is_dispatch_ineligible(cancelled) is True

    # 非法状态被协议层拒绝（fail closed）
    with pytest.raises(ValueError):
        manager.lifecycle.set_status(
            SetStatusParams(run_id=cancelled_task.id, status="not-a-status")
        )


# ---------------------------------------------------------------------------
# f. 子代理树：父子层级在持久化与 kernel 快照中可见
# ---------------------------------------------------------------------------


def test_child_run_links_into_parent_tree_and_kernel_snapshot(tmp_path: Path) -> None:
    manager = SubAgentManager(workspace=tmp_path / "workspace")
    parent = _create_task(manager, goal="父任务", role="lead")
    child = _create_task(
        manager,
        goal="子任务",
        parent_id=parent.id,
        root_id=parent.id,
        depth=1,
    )

    # 父任务 child_ids 持久化包含子任务；system_tree 投影同步
    parent_loaded = manager.load(parent.id)
    assert child.id in parent_loaded.child_ids
    assert child.id in parent_loaded.attributes["system_tree"]["child_ids"]

    # 子任务继承会话谱系（真实创建链路里生成的 session 链）
    child_loaded = manager.load(child.id)
    assert child_loaded.parent_id == parent.id
    assert child_loaded.root_subagent_session_id == parent_loaded.subagent_session_id

    # kernel 树快照能看到层级
    snapshot = manager.kernel_snapshot(SubagentKernelQuery(scope="subtree", run_id=parent.id))
    rows = {row.run_id: row for row in snapshot.runs}
    assert set(rows) == {parent.id, child.id}
    assert snapshot.root_id == parent.id
    assert rows[child.id].parent_id == parent.id
    assert rows[child.id].root_id == parent.id
    assert rows[child.id].depth == 1
    assert child.id in rows[parent.id].child_ids
