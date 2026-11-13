# LLM: W1 运行态工具契约、作用域门禁与协作交接板回归单测：
#   覆盖：
#   1. ToolHandlerOutcome 针对 result_envelope=None / 非 dict / output=None 的自愈归一化；
#   2. RequirementProvenanceTool 在 bind / link / review / accept / query 全生命周期 ok=True 时的稳定执行；
#   3. execute_authorized_tool 对非 ToolHandlerOutcome 返回值与 apply_tool_execution_facts 异常的内部错误降级；
#   4. ApplyPatchTool 越界路径的有效写根安全解析与 fail-closed 权限拒绝；
#   5. HandoffBoardTool 的开单、查单、发单、领单、交付全生命周期，及不存在单号零锁文件创建保证与非法参数拦截。
# 模块用途: W1 工具执行契约与协作交接板专用行为单测。
"""Tests for Workstream W1 tool execution contract, coordinator guards, and handoff board."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from agent_py_agent.agent.agent_core.orchestration.tools.handoff_board_tool import (
    HandoffBoardTool,
    ManageHandoffBoardTool,
)
from agent_py_agent.agent.agent_core.orchestration.tools.requirement_provenance_tool import (
    RequirementProvenanceTool,
)
from agent_py_agent.agent.collaboration.handoff_board import (
    HandoffBoard,
    HandoffBoardService,
)
from agent_py_agent.agent.collaboration.store import CollaborationStore
from agent_py_agent.agent.tooling._filesystem_patch import ApplyPatchTool
from agent_py_agent.agent.tooling.executor import ResourceScopeResolutionError
from agent_py_agent.agent.tooling.models import (
    BaseTool,
    EffectResolverPolicy,
    ToolFailureStage,
    ToolHandlerOutcome,
    ToolInvocationContext,
    ToolRuntimePolicy,
)
from agent_py_agent.agent.tooling.registry_invoke import (
    AuthorizedToolDispatchRequest,
    execute_authorized_tool,
)
from agent_py_agent.agent.verification.repository import (
    VerificationEvidence,
    VerificationEvidenceRepository,
)


def test_tool_handler_outcome_normalization() -> None:
    """1. ToolHandlerOutcome normalizes result_envelope=None and output=None safely."""
    outcome_none = ToolHandlerOutcome(
        tool="test_tool",
        ok=True,
        output=None,  # type: ignore[arg-type]
        result_envelope=None,  # type: ignore[arg-type]
        error_code=None,  # type: ignore[arg-type]
        effect_outcome=None,  # type: ignore[arg-type]
    )
    assert outcome_none.ok is True
    assert outcome_none.output == ""
    assert outcome_none.result_envelope == {}
    assert outcome_none.error_code == ""
    assert outcome_none.effect_outcome == ""

    outcome_non_dict = ToolHandlerOutcome(
        tool="test_tool",
        ok=False,
        output=12345,  # type: ignore[arg-type]
        result_envelope=["not", "a", "dict"],  # type: ignore[arg-type]
        error_code="TEST_ERROR",
        effect_outcome="not_started",
    )
    assert outcome_non_dict.ok is False
    assert outcome_non_dict.output == "12345"
    assert outcome_non_dict.result_envelope == {}
    assert outcome_non_dict.effect_outcome == "not_started"

    outcome_dict = ToolHandlerOutcome(
        tool="test_tool",
        ok=True,
        output="success",
        result_envelope={"details": {"code": 0}},
    )
    assert outcome_dict.result_envelope == {"details": {"code": 0}}


def test_requirement_provenance_tool_lifecycle(tmp_path: Path) -> None:
    """2. RequirementProvenanceTool succeeds on read, bind, and verify with result_envelope == {}."""
    owner_root = tmp_path / "owner_home"
    owner_root.mkdir(parents=True, exist_ok=True)
    repo = VerificationEvidenceRepository(owner_root)

    agent = SimpleNamespace(
        owner_root=owner_root,
        root=owner_root,
        workspace_root=owner_root,
        _current_run_params=SimpleNamespace(run_id="run-prov-001"),
    )
    tool = RequirementProvenanceTool(agent=agent)
    proven_ctx = ToolInvocationContext(runtime_snapshot=MagicMock())

    # 2.1 bind_requirement
    bind_res = tool.execute_scoped(
        {
            "action": "bind_requirement",
            "requirement_id": "REQ-W1-001",
            "input_version": "v1.0.0",
            "watched_paths": [str(owner_root / "test_module.py")],
        },
        proven_ctx,
    )
    assert bind_res.ok is True
    assert bind_res.error_code == ""
    assert bind_res.result_envelope == {}

    # 2.2 link_verification
    ev_context = tool._resolve_context({})
    ev_record = repo.record(
        ev_context,
        VerificationEvidence(
            command="pytest test_module.py",
            canonical_command="pytest",
            kind="test",
            scope="targeted",
            status="passed",
            exit_code=0,
            cwd=str(owner_root),
            root=str(owner_root),
            output_summary="1 passed",
        ),
    )
    link_res = tool.execute_scoped(
        {
            "action": "link_verification",
            "requirement_id": "REQ-W1-001",
            "verification_event_ids": [ev_record["id"]],
            "observation_scope": "module_only",
        },
        proven_ctx,
    )
    assert link_res.ok is True
    assert link_res.error_code == ""
    assert link_res.result_envelope == {}

    agent._current_run_params = SimpleNamespace(run_id="reviewer-alice")
    # 2.3 record_independent_review
    rev_res = tool.execute_scoped(
        {
            "action": "record_independent_review",
            "requirement_id": "REQ-W1-001",
            "reviewer_principal_id": "reviewer-alice",
            "author_principal_id": "run-prov-001",
            "reviewed_input_version": "v1.0.0",
            "review_verdict": "pass",
            "review_scope": "integration_verification",
        },
        proven_ctx,
    )
    assert rev_res.ok is True
    assert rev_res.error_code == ""
    assert rev_res.result_envelope == {}

    # 2.4 record_acceptance
    acc_res = tool.execute_scoped(
        {
            "action": "record_acceptance",
            "requirement_id": "REQ-W1-001",
            "accepted_by_principal_id": "reviewer-alice",
            "accepted_input_version": "v1.0.0",
            "acceptance_verdict": "accepted",
        },
        proven_ctx,
    )
    assert acc_res.ok is True
    assert acc_res.error_code == ""
    assert acc_res.result_envelope == {}

    # 2.5 query_provenance
    query_res = tool.execute_scoped(
        {
            "action": "query_provenance",
            "requirement_id": "REQ-W1-001",
        },
        proven_ctx,
    )
    assert query_res.ok is True
    assert query_res.error_code == ""
    assert query_res.result_envelope == {}


def test_execute_authorized_tool_non_outcome_and_facts_guard() -> None:
    """3. execute_authorized_tool catches invalid return types and facts errors."""
    class BadTool(BaseTool):
        model_spec = MagicMock()
        runtime_policy = ToolRuntimePolicy(effect_resolver=EffectResolverPolicy("read_only"))

        def execute_scoped(self, params: dict[str, Any], context: ToolInvocationContext) -> Any:
            return "not-a-ToolHandlerOutcome"

    bad_tool = BadTool()
    ctx = ToolInvocationContext(runtime_snapshot=MagicMock())
    req = AuthorizedToolDispatchRequest(
        tool_name="bad_tool",
        tool=bad_tool,
        tool_params={},
        invocation_context=ctx,
        workspace_root=Path("/tmp"),
        write_boundary=None,
    )

    outcome = execute_authorized_tool(req)
    assert outcome.ok is False
    assert outcome.error_code == "INTERNAL_ERROR"
    assert outcome.effect_outcome == "unknown"
    assert "invalid handler return type" in outcome.output

    # Test facts guard exception conversion
    class OkTool(BaseTool):
        model_spec = MagicMock()
        runtime_policy = ToolRuntimePolicy(effect_resolver=EffectResolverPolicy("read_only"))

        def execute_scoped(self, params: dict[str, Any], context: ToolInvocationContext) -> ToolHandlerOutcome:
            return ToolHandlerOutcome("ok_tool", True, "ok")

    ok_req = AuthorizedToolDispatchRequest(
        tool_name="ok_tool",
        tool=OkTool(),
        tool_params={},
        invocation_context=ctx,
        workspace_root=Path("/tmp"),
        write_boundary=None,
    )

    from unittest.mock import patch
    with patch(
        "agent_py_agent.agent.tooling.registry_invoke.apply_tool_execution_facts",
        side_effect=RuntimeError("simulated bookkeeping drift"),
    ):
        fact_fail_outcome = execute_authorized_tool(ok_req)
        assert fact_fail_outcome.ok is False
        assert fact_fail_outcome.error_code == "INTERNAL_ERROR"
        assert fact_fail_outcome.effect_outcome == "unknown"


def test_apply_patch_tool_scope_resolution_safety(tmp_path: Path) -> None:
    """4. ApplyPatchTool effective_write_roots never raises unhandled PermissionError."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    tool = ApplyPatchTool(workspace_root=workspace)

    # Patch targeting an absolute path outside workspace
    outside_target = "/etc/simulated_outside_file"
    patch_text = f"*** {outside_target}\n--- {outside_target}\n@@ -1 +1 @@\n-old\n+new\n"

    # effective_write_roots should resolve safely without raising PermissionError or OSError
    roots = tool.effective_write_roots(
        arguments={"patch": patch_text},
        write_boundary=None,
        workspace_root=workspace,
    )
    assert isinstance(roots, tuple)

    # Execution should fail-closed with structured permission/path error, never unhandled exception
    outcome = tool.execute({"patch": patch_text})
    assert outcome.ok is False
    assert outcome.error_code in {"TOOL_PERMISSION_DENIED", "PERMISSION_DENIED", "TOOL_INVALID_ARGUMENTS", "WRITE_SCOPE_BLOCKED"}


def test_handoff_board_tool_lifecycle_and_zero_lock_files(tmp_path: Path) -> None:
    """5. HandoffBoardTool: roundtrip lifecycle and zero orphan .lock files on unknown case_id."""
    collab_dir = tmp_path / "collaboration"
    collab_store = CollaborationStore(collab_dir)
    handoff_service = HandoffBoardService(collab_store, workspace_root=tmp_path)

    agent = SimpleNamespace(
        collaboration_store=collab_store,
        workspace_root=tmp_path,
        _current_run_params=SimpleNamespace(run_id="run-w1-coord"),
    )
    tool = ManageHandoffBoardTool(agent=agent, service=handoff_service)
    proven_ctx = ToolInvocationContext(runtime_snapshot=MagicMock())

    # 5.1 Call publish / claim / read with nonexistent case_id -> RESOURCE_NOT_FOUND, zero locks
    for action in ("publish", "claim", "read"):
        res = tool.execute_scoped(
            {
                "action": action,
                "case_id": "nonexistent_case_999",
                "item_id": "item_999",
            },
            proven_ctx,
        )
        assert res.ok is False
        assert res.error_code == "RESOURCE_NOT_FOUND"
        assert res.effect_outcome == "not_started"

    # Verify zero .lock files created for nonexistent case
    lock_files = list(collab_store.handoff_items_dir.glob("nonexistent_case_999*.lock"))
    assert len(lock_files) == 0

    # 5.2 Call read / list_cases with limit="abc" -> TOOL_INVALID_ARGUMENTS
    bad_limit_res = tool.execute_scoped(
        {"action": "read", "limit": "abc"},
        proven_ctx,
    )
    assert bad_limit_res.ok is False
    assert bad_limit_res.error_code == "TOOL_INVALID_ARGUMENTS"

    bad_cases_limit = tool.execute_scoped(
        {"action": "list_cases", "limit": "abc"},
        proven_ctx,
    )
    assert bad_cases_limit.ok is False
    assert bad_cases_limit.error_code == "TOOL_INVALID_ARGUMENTS"

    # 5.3 Full roundtrip: open_case -> list_cases -> publish -> claim -> complete -> read
    open_res = tool.execute_scoped(
        {
            "action": "open_case",
            "title": "Cross-agent delivery pipeline",
            "summary": "Delivery verification case for W1",
            "priority": "normal",
        },
        proven_ctx,
    )
    assert open_res.ok is True
    case_info = open_res.result_envelope.get("case") or {}
    case_id = case_info.get("case_id")
    assert case_id

    list_cases_res = tool.execute_scoped({"action": "list_cases"}, proven_ctx)
    assert list_cases_res.ok is True
    cases_list = list_cases_res.result_envelope.get("cases") or []
    assert any(c.get("case_id") == case_id for c in cases_list)

    pub_res = tool.execute_scoped(
        {
            "action": "publish",
            "case_id": case_id,
            "item_id": "item-pipe-1",
            "responsible_actor": "worker-w1",
            "frozen_input_version": "v1.0.0",
            "exclusive_write_set": ["module_a.py"],
        },
        proven_ctx,
    )
    assert pub_res.ok is True
    assert pub_res.result_envelope["item"]["state"] == "published"

    agent._current_run_params = SimpleNamespace(run_id="worker-w1")
    claim_res = tool.execute_scoped(
        {
            "action": "claim",
            "case_id": case_id,
            "item_id": "item-pipe-1",
            "frozen_input_version": "v1.0.0",
            "recipient_ack": {"status": "claimed"},
        },
        proven_ctx,
    )
    assert claim_res.ok is True
    assert claim_res.result_envelope["item"]["state"] == "acked"

    complete_res = tool.execute_scoped(
        {
            "action": "complete",
            "case_id": case_id,
            "item_id": "item-pipe-1",
            "frozen_input_version": "v1.0.0",
            "evidence_ids": ["ev-pipe-100"],
        },
        proven_ctx,
    )
    assert complete_res.ok is True
    assert complete_res.result_envelope["item"]["state"] == "submitted"

    read_res = tool.execute_scoped(
        {
            "action": "read",
            "case_id": case_id,
        },
        proven_ctx,
    )
    assert read_res.ok is True
    items = read_res.result_envelope.get("items") or []
    assert len(items) == 1
    assert items[0]["item_id"] == "item-pipe-1"
    assert items[0]["state"] == "submitted"

    # Verify HandoffBoardTool alias
    assert HandoffBoardTool is ManageHandoffBoardTool
    assert HandoffBoard is HandoffBoardService
