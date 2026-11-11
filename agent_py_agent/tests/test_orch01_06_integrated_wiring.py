# LLM: ORCH-01..06 集成贯通测试：
# 1. 真实 Agent 注册检验：manage_handoff_board 与 manage_requirement_provenance 为默认延迟收起工具（default_deferred=True），
#    首轮保持 29 项未延迟工具面不变，tool_search 可发现并按需加载；
# 2. PRO-MSG-031(2) 身份证明门禁检验：execute_scoped 未证明身份时失败关闭（REQUESTER_IDENTITY_UNPROVEN, not_started），
#    证明身份后放行；
# 3. CreateSubagentsTool 结构化委托约束（ORCH-06）：max_cumulative_children=1 跨生命周期不可逆（子代理终止后不释放配额），
#    allow_descendants=False 立即拦截，exact_write_set 单调收窄上界校验；
# 4. ORCH-01..06 完整端到端贯通烟测。
# 模块用途: 验证 Wave 2 跨模块集成贯通与不可变契约。
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_py_agent.agent.agent_core.orchestration.capacity import (
    available_creation_slots,
    cumulative_created_child_count,
    subagent_quota_result,
)
from agent_py_agent.agent.agent_core.orchestration.delegation_constraints import (
    StructuredDelegationConstraints,
)
from agent_py_agent.agent.agent_core.orchestration.tools.handoff_board_tool import (
    ManageHandoffBoardTool,
)
from agent_py_agent.agent.agent_core.orchestration.tools.requirement_provenance_tool import (
    RequirementProvenanceTool,
)
from agent_py_agent.agent.agent_core.orchestration_tools import (
    CreateSubagentsTool,
    ListAgentsTool,
    ResolveCapabilityRequestsTool,
)
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.agent_core.runtime.owner_roots import runtime_owner_root
from agent_py_agent.agent.collaboration.handoff_board import HandoffBoardService
from agent_py_agent.agent.collaboration.store import CollaborationStore
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.runtime_context import set_current_subagent_context
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.tooling.models import ToolInvocationContext
from agent_py_agent.agent.verification.repository import (
    VerificationContext,
    VerificationEvidence,
    VerificationEvidenceRepository,
)


# LLM: 构造真实的隔离主代理环境，使用临时目录与 echo 后端。
# 函数用途: 提供集成测试的基础 Agent fixture。
@pytest.fixture
def integrated_agent(tmp_path: Path) -> SimpleAgent:
    config = AgentConfig(
        model_backend="echo",
        my_agent_home=str(tmp_path / "home"),
        execution_mode="local_unmanaged",
    )
    return SimpleAgent(config, tmp_path / "workspace")


# -----------------------------------------------------------------------------
# 1. 真实 Agent 注册与渐进式暴露（Progressive Disclosure）
# -----------------------------------------------------------------------------


def test_real_agent_registered_deferred_tools(integrated_agent: SimpleAgent) -> None:
    """ORCH-04/05 tools registered with default_deferred=True; surface is 29 and tool_search discovers them."""
    tools = integrated_agent.tools

    assert "manage_handoff_board" in tools.tools
    assert "manage_requirement_provenance" in tools.tools

    board_tool = tools.tools["manage_handoff_board"]
    prov_tool = tools.tools["manage_requirement_provenance"]

    assert board_tool.model_spec.hints.default_deferred is True
    assert prov_tool.model_spec.hints.default_deferred is True

    # 首轮未延迟模型工具数量严格保持 29 项（含 list_pending_requests）
    visible = {spec.name for spec in tools.model_visible_specs()}
    assert len(visible) == 29, sorted(visible)
    assert "manage_handoff_board" not in visible
    assert "manage_requirement_provenance" not in visible

    # 通过 tool_search 发现并动态加载
    search_tool = tools.tools["tool_search"]
    res = search_tool.execute({
        "action": "search",
        "names": ["manage_handoff_board", "manage_requirement_provenance"],
    })
    assert res.ok is True
    loaded = res.result_envelope.get("tool_search", {}).get("loaded_tool_names", [])
    assert "manage_handoff_board" in loaded
    assert "manage_requirement_provenance" in loaded


# -----------------------------------------------------------------------------
# 2. PRO-MSG-031(2) 证明身份执行门（execute_scoped）
# -----------------------------------------------------------------------------


def test_execute_scoped_requester_identity_unproven_gate(integrated_agent: SimpleAgent) -> None:
    """Missing or unproven requester context fails closed with REQUESTER_IDENTITY_UNPROVEN / not_started."""
    board_tool = integrated_agent.tools.tools["manage_handoff_board"]
    prov_tool = integrated_agent.tools.tools["manage_requirement_provenance"]

    # 1. 未带有效上下文 -> fail-closed
    unproven_ctx = ToolInvocationContext(runtime_snapshot=None)
    board_outcome = board_tool.execute_scoped({"action": "list_items"}, unproven_ctx)
    assert board_outcome.ok is False
    assert board_outcome.error_code == "TOOL_PERMISSION_DENIED"
    assert board_outcome.effect_outcome == "not_started"
    assert json.loads(board_outcome.output)["reason"] == "REQUESTER_IDENTITY_UNPROVEN"

    prov_outcome = prov_tool.execute_scoped({"action": "query_provenance"}, unproven_ctx)
    assert prov_outcome.ok is False
    assert prov_outcome.error_code == "TOOL_PERMISSION_DENIED"
    assert prov_outcome.effect_outcome == "not_started"
    assert json.loads(prov_outcome.output)["reason"] == "REQUESTER_IDENTITY_UNPROVEN"

    # 2. 注入证明身份上下文 -> 放行
    integrated_agent._current_run_params = SimpleNamespace(
        run_id="run-proven-001",
        task_id="task-proven-001",
        request_id="req-proven-001",
    )
    dummy_snapshot = SimpleNamespace(runtimes=())
    proven_ctx = ToolInvocationContext(runtime_snapshot=dummy_snapshot)

    board_res = board_tool.execute_scoped({"action": "list_items"}, proven_ctx)
    assert board_res.ok is True, board_res.output

    prov_res = prov_tool.execute_scoped({"action": "query_provenance"}, proven_ctx)
    assert prov_res.ok is True, prov_res.output


# -----------------------------------------------------------------------------
# 3. CreateSubagentsTool 结构化委托约束（ORCH-06）
# -----------------------------------------------------------------------------


def test_create_subagents_cumulative_limit_enforced_across_lifecycles(tmp_path: Path) -> None:
    """max_cumulative_children=1 strictly enforces cumulative count; child termination does NOT free quota."""
    manager = SubAgentManager(tmp_path / "subagents")
    db_path = tmp_path / "runtime.db"
    repo = RuntimeRepository(db_path)
    manager.runtime_db = repo

    parent = manager.create_run(goal="parent coordinator", thought="p", plan=["p"], owner="test-owner")
    parent.attributes["delegation_constraints"] = StructuredDelegationConstraints(
        max_cumulative_children=1,
    ).to_dict()
    manager.save(parent)

    agent = SimpleNamespace(
        subagents=manager,
        runtime_db=repo,
        config=SimpleNamespace(
            enable_subagents=True,
            max_subagents=10,
            subagent_hierarchy_max_children_per_tool_call=0,
        ),
        _current_run_params=RunParams(task_attributes=parent.attributes, host_channel_actor=None),
        _current_subagent_run_id=parent.id,
    )
    set_current_subagent_context(agent, run_id=parent.id, task_attributes=parent.attributes)

    create_tool = CreateSubagentsTool(agent)

    # 第一次派单：创建 1 个子代理 -> 成功
    out1 = create_tool.execute({
        "items": [{"goal": "first child task", "agent_name": "worker-1", "role": "worker"}],
    })
    assert out1.ok is True, out1.output
    payload1 = json.loads(out1.output)
    created_id = payload1["created_run_ids"][0]
    assert created_id

    # 模拟子代理完成退出（终止终态）
    child = manager.load(created_id)
    assert child is not None
    child.status = "COMPLETED"
    manager.save(child)

    # 终止后累计创建数仍为 1，再次创建第 2 个子代理必须被原子拒绝
    out2 = create_tool.execute({
        "items": [{"goal": "second child task", "agent_name": "worker-2", "role": "worker"}],
    })
    assert out2.ok is False
    assert out2.reported_error_code == "SUBAGENT_CUMULATIVE_LIMIT_EXCEEDED"
    assert out2.effect_outcome == "not_started"
    payload2 = json.loads(out2.output)
    assert payload2["error_code"] == "SUBAGENT_CUMULATIVE_LIMIT_EXCEEDED"


def test_create_subagents_allow_descendants_forbidden(tmp_path: Path) -> None:
    """When allow_descendants=False, CreateSubagentsTool rejects with SUBAGENT_DESCENDANTS_FORBIDDEN immediately."""
    manager = SubAgentManager(tmp_path / "subagents")
    parent = manager.create_run(goal="leaf worker", thought="p", plan=["p"], owner="test-owner")
    parent.attributes["delegation_constraints"] = StructuredDelegationConstraints(
        allow_descendants=False,
    ).to_dict()
    manager.save(parent)

    agent = SimpleNamespace(
        subagents=manager,
        runtime_db=None,
        config=SimpleNamespace(enable_subagents=True, max_subagents=5),
        _current_run_params=RunParams(task_attributes=parent.attributes, host_channel_actor=None),
        _current_subagent_run_id=parent.id,
    )
    set_current_subagent_context(agent, run_id=parent.id, task_attributes=parent.attributes)

    create_tool = CreateSubagentsTool(agent)
    outcome = create_tool.execute({
        "items": [{"goal": "prohibited descendant", "role": "worker"}],
    })
    assert outcome.ok is False
    assert outcome.reported_error_code == "SUBAGENT_DESCENDANTS_FORBIDDEN"
    assert outcome.effect_outcome == "not_started"
    assert json.loads(outcome.output)["error_code"] == "SUBAGENT_DESCENDANTS_FORBIDDEN"


def test_create_subagents_exact_write_set_upper_bound_narrowing(tmp_path: Path) -> None:
    """Child exact_write_set must be within parent write roots; broadening outside fails closed."""
    manager = SubAgentManager(tmp_path / "subagents")
    db_path = tmp_path / "runtime.db"
    repo = RuntimeRepository(db_path)
    manager.runtime_db = repo

    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    allowed_file = project_dir / "allowed_file.py"
    allowed_file.touch()

    parent = manager.create_run(goal="scoped parent", thought="p", plan=["p"], owner="test-owner")
    parent.allowed_write_roots = [str(project_dir)]
    parent.attributes["delegation_constraints"] = StructuredDelegationConstraints(
        exact_write_set=(str(allowed_file),),
    ).to_dict()
    manager.save(parent)

    agent = SimpleNamespace(
        subagents=manager,
        runtime_db=repo,
        config=SimpleNamespace(enable_subagents=True, max_subagents=5, subagent_hierarchy_max_children_per_tool_call=0),
        _current_run_params=RunParams(task_attributes=parent.attributes, host_channel_actor=None),
        _current_subagent_run_id=parent.id,
    )
    set_current_subagent_context(agent, run_id=parent.id, task_attributes=parent.attributes)

    create_tool = CreateSubagentsTool(agent)

    # 1. 扩大写集至父级 write roots 外部 -> 失败关闭（DELEGATION_SCOPE_EXCEEDS_PARENT，not_started）
    out_broad = create_tool.execute({
        "items": [
            {
                "goal": "exceeds scope",
                "role": "worker",
                "delegation_constraints": {
                    "exact_write_set": [str(tmp_path / "forbidden_outside.py")],
                },
            }
        ],
    })
    assert out_broad.ok is False
    assert out_broad.effect_outcome == "not_started"
    assert "DELEGATION_SCOPE_EXCEEDS_PARENT" in (out_broad.reported_error_code or out_broad.output)

    # 2. 单调收窄（在父级 write roots 内部） -> 成功
    out_valid = create_tool.execute({
        "items": [
            {
                "goal": "narrowed scope",
                "role": "worker",
                "delegation_constraints": {
                    "exact_write_set": [str(allowed_file)],
                },
            }
        ],
    })
    assert out_valid.ok is True
    child_id = json.loads(out_valid.output)["created_run_ids"][0]
    child_task = manager.load(child_id)
    assert child_task is not None
    assert child_task.attributes["delegation_constraints"]["exact_write_set"] == [str(allowed_file)]


# -----------------------------------------------------------------------------
# 4. ORCH-01..06 完整贯通烟测
# -----------------------------------------------------------------------------


def test_orch01_06_composed_pipeline_smoke(integrated_agent: SimpleAgent, tmp_path: Path) -> None:
    """Full composed smoke flow covering ORCH-01..06 together without state leaks or crashes."""
    agent = integrated_agent
    tools = agent.tools
    module_a = Path(agent.home_paths.owner_home_dir) / "module_a.py"
    module_a.parent.mkdir(parents=True, exist_ok=True)
    module_a.touch()

    # 1. ORCH-01/06: 主代理派发带有结构化委托约束的协调子代理
    create_tool = tools.tools["create_subagents"]
    spawn_res = create_tool.execute({
        "items": [
            {
                "goal": "Coordinate subsystem integration",
                "agent_name": "coord-subsystem",
                "role": "coordinator",
                "delegation_constraints": {
                    "max_cumulative_children": 2,
                    "allow_descendants": True,
                    "exact_write_set": [str(module_a)],
                },
            }
        ],
    })
    assert spawn_res.ok is True, spawn_res.output
    spawn_payload = json.loads(spawn_res.output)
    child_run_id = spawn_payload["created_run_ids"][0]
    assert child_run_id

    # 2. ORCH-03: inspect_agent_progress / list_agents 查看进展分类与状态契约
    list_tool = tools.tools["list_agents"]
    list_res = list_tool.execute({"run_id": child_run_id})
    assert list_res.ok is True
    list_payload = json.loads(list_res.output)
    assert "nodes" in list_payload
    node = list_payload["nodes"][0]
    assert node["run_id"] == child_run_id
    assert "progress" in node
    assert node["progress"]["schema_version"] == "progress_watch.v1"

    # 3. ORCH-02: 提申请 -> 决裁 -> 因果续跑链接（continuation_id -> resumed_attempt_id）
    from agent_py_agent.agent.subagents.services.lifecycle import RecordCapabilityRequestParams
    child_task = agent.subagents.load(child_run_id)
    assert child_task is not None
    extra_dir = Path(child_task.task_workspace_dir) / "extra"
    extra_dir.mkdir(parents=True, exist_ok=True)

    cap_req = agent.subagents.lifecycle.record_capability_request(
        child_run_id,
        RecordCapabilityRequestParams(
            problem="需要写入 extra 目录进行集成",
            needed_capability="unlock_extra_directory",
            capability_type="filesystem",
            path_scope=[str(extra_dir)],
        ),
    )
    req_id = cap_req.id

    # 协调者决裁授权
    resolve_tool = tools.tools["resolve_capability_requests"]
    resolve_res = resolve_tool.execute({
        "run_id": child_run_id,
        "request_id": req_id,
        "decision": "grant",
        "reason": "Approved integration access",
    })
    assert resolve_res.ok is True, resolve_res.output
    resolve_payload = json.loads(resolve_res.output)
    assert resolve_payload.get("decision") == "grant"

    # 4. ORCH-04: ManageHandoffBoardTool 原生交接板状态机流转
    agent._current_run_params = SimpleNamespace(
        run_id="main-run-coord",
        task_id="task-main-coord",
        request_id="req-main-coord",
    )
    dummy_snapshot = SimpleNamespace(runtimes=())
    proven_ctx = ToolInvocationContext(runtime_snapshot=dummy_snapshot)

    board_tool = tools.tools["manage_handoff_board"]
    thread = agent.conversation_store.threads.create(title="thread-orch-int")
    case = agent.collaboration_store.open_case({"thread_id": thread.thread_id})
    case_id = case.case_id

    # 4.1 publish_item
    pub_res = board_tool.execute_scoped({
        "action": "publish_item",
        "case_id": case_id,
        "request_id": "req-flow-1",
        "responsible_actor": "worker-1",
        "frozen_input_version": "git-sha-1234",
        "exclusive_write_set": [str(module_a)],
    }, proven_ctx)
    assert pub_res.ok is True, pub_res.output
    pub_payload = json.loads(pub_res.output)
    item_id = pub_payload.get("result", {}).get("item", {}).get("item_id")
    assert item_id

    # 4.2 record_ack
    ack_res = board_tool.execute_scoped({
        "action": "record_ack",
        "case_id": case_id,
        "item_id": item_id,
        "frozen_input_version": "git-sha-1234",
    }, proven_ctx)
    assert ack_res.ok is True, ack_res.output

    # 4.3 submit_handoff
    sub_res = board_tool.execute_scoped({
        "action": "submit_handoff",
        "case_id": case_id,
        "item_id": item_id,
        "evidence_ids": ["ev-int-1"],
    }, proven_ctx)
    assert sub_res.ok is True, sub_res.output

    # 4.4 decide_acceptance (accepted)
    dec_res = board_tool.execute_scoped({
        "action": "decide_acceptance",
        "case_id": case_id,
        "item_id": item_id,
        "verdict": "accepted",
        "reason": "Integration verified cleanly",
    }, proven_ctx)
    assert dec_res.ok is True, dec_res.output
    assert json.loads(dec_res.output).get("result", {}).get("item", {}).get("state") == "accepted"

    # 5. ORCH-05: RequirementProvenanceTool 需求溯源全闭环
    prov_tool = tools.tools["manage_requirement_provenance"]

    # 5.1 bind_requirement
    bind_res = prov_tool.execute_scoped({
        "action": "bind_requirement",
        "requirement_id": "ORCH-E2E-PIPE",
        "input_version": "git-sha-1234",
        "watched_paths": [str(module_a)],
    }, proven_ctx)
    assert bind_res.ok is True, bind_res.output

    # 5.2 link_verification
    evidence_repo = VerificationEvidenceRepository(runtime_owner_root(integrated_agent))
    evidence_ctx = prov_tool._resolve_context({})
    ev_passed = evidence_repo.record(
        evidence_ctx,
        VerificationEvidence(
            command="pytest module_a",
            canonical_command="pytest",
            kind="test",
            scope="targeted",
            status="passed",
            exit_code=0,
            cwd=str(tmp_path),
            root=str(tmp_path),
            output_summary="1 passed",
        ),
    )
    link_res = prov_tool.execute_scoped({
        "action": "link_verification",
        "requirement_id": "ORCH-E2E-PIPE",
        "verification_event_ids": [ev_passed["id"]],
        "observation_scope": "module_only",
    }, proven_ctx)
    assert link_res.ok is True, link_res.output

    # 5.3 record_independent_review
    rev_res = prov_tool.execute_scoped({
        "action": "record_independent_review",
        "requirement_id": "ORCH-E2E-PIPE",
        "reviewer_principal_id": "principal-reviewer-charlie",
        "author_principal_id": "principal-author-alice",
        "reviewed_input_version": "git-sha-1234",
        "review_verdict": "pass",
        "review_scope": "integration_verification",
    }, proven_ctx)
    assert rev_res.ok is True, rev_res.output

    # 5.4 record_acceptance
    acc_res = prov_tool.execute_scoped({
        "action": "record_acceptance",
        "requirement_id": "ORCH-E2E-PIPE",
        "accepted_by_principal_id": "principal-coord",
        "accepted_input_version": "git-sha-1234",
    }, proven_ctx)
    assert acc_res.ok is True, acc_res.output

    # 5.5 query_provenance
    query_res = prov_tool.execute_scoped({
        "action": "query_provenance",
        "requirement_id": "ORCH-E2E-PIPE",
    }, proven_ctx)
    assert query_res.ok is True, query_res.output
    query_payload = json.loads(query_res.output)
    assert len(query_payload.get("records", [])) >= 1
    record = query_payload["records"][0]
    assert record["requirement_id"] == "ORCH-E2E-PIPE"
    assert record["review_status"] == "approved"
    assert record["acceptance_status"] == "accepted"
