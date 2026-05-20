from __future__ import annotations


# LLM: Parent tasks must not complete while required children are failed or blocked.
# 函数用途: 验证父任务 required_child_run_ids 中有失败 child 时，父任务不能通过收口合同。
def test_parent_cannot_complete_if_required_child_failed() -> None:
    from agent_py_agent.agent.contracts.offline_subagent_contract import validate_subagent_contract

    result = validate_subagent_contract(
        {
            "parent": {"run_id": "parent", "status": "VERIFYING", "required_child_run_ids": ["child-a"]},
            "children": [
                {
                    "run_id": "child-a",
                    "parent_run_id": "parent",
                    "status": "FAILED",
                    "artifact_refs": ["artifact://child-a/report"],
                    "acceptance": {"ok": False},
                }
            ],
        }
    )

    assert result.ok is False
    assert result.error_codes == ("CHILD_NOT_SUCCESSFUL",)


# LLM: Child completion needs structured artifact refs and acceptance, not a text claim.
# 函数用途: 验证 child 状态为 SUCCEEDED 但缺产物或验收结构时会失败。
def test_child_success_requires_artifacts_and_acceptance() -> None:
    from agent_py_agent.agent.contracts.offline_subagent_contract import validate_subagent_contract

    result = validate_subagent_contract(
        {
            "parent": {"run_id": "parent", "status": "VERIFYING", "required_child_run_ids": ["child-a"]},
            "children": [
                {
                    "run_id": "child-a",
                    "parent_run_id": "parent",
                    "status": "SUCCEEDED",
                    "artifact_refs": [],
                    "acceptance": {"ok": False, "evidence_refs": []},
                }
            ],
        }
    )

    assert result.ok is False
    assert result.error_codes == ("CHILD_ARTIFACT_MISSING", "CHILD_ACCEPTANCE_MISSING")


# LLM: Child timeout should be a distinct parent-closeout finding, not hidden as generic failure.
# 函数用途: 验证 required child 超时会返回 CHILD_TIMEOUT，方便后续 repair/takeover 分流。
def test_child_timeout_blocks_parent_with_specific_code() -> None:
    from agent_py_agent.agent.contracts.offline_subagent_contract import validate_subagent_contract

    result = validate_subagent_contract(
        {
            "parent": {"run_id": "parent", "status": "VERIFYING", "required_child_run_ids": ["child-timeout"]},
            "children": [
                {
                    "run_id": "child-timeout",
                    "parent_run_id": "parent",
                    "status": "TIMEOUT",
                    "artifact_refs": [],
                    "acceptance": {"ok": False, "evidence_refs": []},
                }
            ],
        }
    )

    assert result.ok is False
    assert result.error_codes == ("CHILD_TIMEOUT",)


# LLM: Explicit subagent depth and child-count limits are contract fields, not prompt advice.
# 函数用途: 验证显式 max_depth/max_children 被离线合同执行；默认不测试任何固定层级限制。
def test_explicit_subagent_depth_and_child_limits_are_enforced() -> None:
    from agent_py_agent.agent.contracts.offline_subagent_contract import validate_subagent_contract

    result = validate_subagent_contract(
        {
            "limits": {"max_depth": 1, "max_children": 1},
            "parent": {"run_id": "parent", "status": "RUNNING", "required_child_run_ids": ["child-a", "child-b"]},
            "children": [
                {"run_id": "child-a", "parent_run_id": "parent", "depth": 1, "status": "RUNNING"},
                {"run_id": "child-b", "parent_run_id": "parent", "depth": 2, "status": "RUNNING"},
            ],
        }
    )

    assert result.ok is False
    assert result.error_codes == ("SUBAGENT_DEPTH_EXCEEDED", "SUBAGENT_CHILD_LIMIT_EXCEEDED")


# LLM: Parent context should receive refs and summaries, not the full child transcript or private context.
# 函数用途: 验证 child 输出到父级的 export_kind 不是 refs_only 时会被合同拒绝。
def test_child_result_export_must_be_refs_only() -> None:
    from agent_py_agent.agent.contracts.offline_subagent_contract import validate_subagent_contract

    result = validate_subagent_contract(
        {
            "parent": {"run_id": "parent", "status": "RUNNING", "required_child_run_ids": ["child-a"]},
            "children": [
                {
                    "run_id": "child-a",
                    "parent_run_id": "parent",
                    "status": "SUCCEEDED",
                    "artifact_refs": ["artifact://child-a/report"],
                    "acceptance": {"ok": True, "evidence_refs": ["evidence://child-a"]},
                    "export": {"kind": "full_context", "refs": []},
                }
            ],
        }
    )

    assert result.ok is False
    assert result.error_codes == ("CHILD_CONTEXT_EXPORT_NOT_REFS_ONLY",)


# LLM: A parent can close only after every required child has success, artifacts, acceptance, and refs-only export.
# 函数用途: 验证满足父子状态、产物、验收和隔离字段的最小正例。
def test_valid_subagent_parent_closeout_passes() -> None:
    from agent_py_agent.agent.contracts.offline_subagent_contract import validate_subagent_contract

    result = validate_subagent_contract(
        {
            "limits": {"max_depth": 2, "max_children": 2},
            "parent": {"run_id": "parent", "status": "VERIFYING", "required_child_run_ids": ["child-a"]},
            "children": [
                {
                    "run_id": "child-a",
                    "parent_run_id": "parent",
                    "depth": 1,
                    "status": "VERIFIED",
                    "artifact_refs": ["artifact://child-a/report"],
                    "acceptance": {"ok": True, "evidence_refs": ["evidence://child-a"]},
                    "export": {"kind": "refs_only", "refs": ["artifact://child-a/report"]},
                }
            ],
        }
    )

    assert result.ok is True
    assert result.error_codes == ()
