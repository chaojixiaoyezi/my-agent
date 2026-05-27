"""Tests for parent-visible subagent status observability."""

from __future__ import annotations

from agent_py_agent.agent.agent_core.agent_tree_status import agent_tree_status_payload
from agent_py_agent.agent.subagents.kernel import SubagentKernelRun, SubagentKernelSnapshot


def test_agent_tree_node_exposes_liveness_progress_and_evidence_layers():
    """父代理看树时，应直接看到存活、进展、工具/产物/阻塞三层状态。"""

    class _Manager:
        def kernel_snapshot(self, query):
            return SubagentKernelSnapshot(
                schema_version="subagent_kernel_snapshot.v1",
                scope=query.scope,
                runs=[
                    SubagentKernelRun(
                        run_id="child-1",
                        task_id="child-1",
                        parent_run_id="main",
                        status="RUNNING",
                        progress=0.4,
                        current_tool="web_search",
                        heartbeat_at=100.0,
                        updated_at=110.0,
                        last_progress_at=105.0,
                        last_progress_summary="查到一批候选资料",
                        artifact_refs=["/tmp/out.xlsx"],
                        evidence_refs=["trace:web_search:1"],
                        blockers=["需要执行脚本能力"],
                        tool_contract={"open_request_count": 1, "gap_count": 1},
                        reserved={
                            "recent_tool_trace": [
                                {"tool": "web_search", "ok": True, "summary": "查到候选资料"}
                            ],
                        },
                    )
                ],
            )

    class _Agent:
        subagents = _Manager()

    payload = agent_tree_status_payload(_Agent())
    node = payload["nodes"][0]

    assert node["liveness"]["heartbeat_at"] == 100.0
    assert node["progress_layer"]["last_progress_summary"] == "查到一批候选资料"
    assert node["evidence_layer"]["artifact_refs"] == ["/tmp/out.xlsx"]
    assert node["needs_capability"] == ["capability_request", "capability_gap"]
    assert node["recent_tool_trace"] == [{"tool": "web_search", "ok": True, "summary": "查到候选资料"}]


def test_subagent_runner_can_only_inspect_own_subtree_even_with_root_params():
    """子代理只读查树时，即使传 root_id，也应被限制到自己的子树。"""

    class _Manager:
        seen_query = None

        def kernel_snapshot(self, query):
            self.seen_query = query
            return SubagentKernelSnapshot(
                schema_version="subagent_kernel_snapshot.v1",
                root_id="root-1",
                scope=query.scope,
                runs=[SubagentKernelRun(run_id="child-1", parent_run_id="root-1")],
            )

    class _Agent:
        _current_subagent_run_id = "child-1"
        subagents = _Manager()

    payload = agent_tree_status_payload(_Agent(), {"root_id": "root-1", "scope": "root_tree"})

    query = _Agent.subagents.seen_query
    assert query.run_id == "child-1"
    assert query.root_id == ""
    assert query.scope == "own_subtree"
    assert payload["scope"] == "own_subtree"


def test_agent_tree_reports_scope_conflict_when_runner_context_wins():
    """当前 runner 上下文覆盖显式外部 scope 时，不能静默吞掉冲突。"""

    class _Manager:
        seen_query = None

        def kernel_snapshot(self, query):
            self.seen_query = query
            return SubagentKernelSnapshot(
                schema_version="subagent_kernel_snapshot.v1",
                root_id="child-1",
                scope=query.scope,
                runs=[SubagentKernelRun(run_id="child-1")],
            )

    class _Agent:
        _current_subagent_run_id = "child-1"
        subagents = _Manager()

    payload = agent_tree_status_payload(
        _Agent(),
        {"root_id": "foreign-root", "run_id": "foreign-child", "scope": "root_tree"},
    )

    assert _Agent.subagents.seen_query.run_id == "child-1"
    assert "explicit_scope_overridden_by_current_runner" in payload["warnings"]
    assert payload["policy"]["scope_resolution"]["source"] == "current_runner_context"
    assert payload["policy"]["scope_resolution"]["effective"]["run_id"] == "child-1"
    assert payload["policy"]["scope_resolution"]["ignored_explicit"]["run_id"] == "foreign-child"
