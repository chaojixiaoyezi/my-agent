"""Tests for parent-visible subagent status observability."""

from __future__ import annotations

from agent_py_agent.agent.agent_core.agent_tree.status import agent_tree_status_payload
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
    assert node["liveness"]["not_done_reason"] == "blocked:需要执行脚本能力"
    assert node["running_seconds"] > 0
    assert node["seconds_since_progress"] > 0
    assert node["progress_layer"]["last_progress_summary"] == "查到一批候选资料"
    assert node["progress_layer"]["seconds_since_progress"] > 0
    assert node["evidence_layer"]["artifact_refs"] == ["/tmp/out.xlsx"]
    assert node["needs_capability"] == ["capability_request", "capability_gap"]
    assert node["recent_tool_trace"] == [{"tool": "web_search", "ok": True, "summary": "查到候选资料"}]


def test_agent_tree_workspace_refs_hide_legacy_task_dir():
    class _Manager:
        def kernel_snapshot(self, query):
            return SubagentKernelSnapshot(
                schema_version="subagent_kernel_snapshot.v1",
                scope=query.scope,
                runs=[
                    SubagentKernelRun(
                        run_id="child-1",
                        task_id="child-1",
                        status="RUNNING",
                        workspace_refs={
                            "task_dir": "/tmp/home/owners/local/main/tasks/2026-06-01/root-1",
                            "task_workspace": "/tmp/home/owners/local/main/tasks/2026-06-01/root-1",
                            "agent_run_workspace": "/tmp/home/owners/local/main/tasks/2026-06-01/root-1/work/agents/child-1",
                            "legacy_task_dir": "/tmp/workspace/data/subagents/child-1",
                        },
                    )
                ],
                source_refs={
                    "root_task_dir": "/tmp/workspace/data/subagents/tasks/root-1",
                    "root_task_workspace": "/tmp/home/owners/local/main/tasks/2026-06-01/root-1",
                },
            )

    class _Agent:
        subagents = _Manager()

    payload = agent_tree_status_payload(_Agent())
    node = payload["nodes"][0]

    assert node["workspace_refs"]["agent_work_dir"].endswith("/root-1/work/agents/child-1")
    assert node["workspace_refs"]["task_root"].endswith("/root-1")
    assert "legacy_task_dir" not in node["workspace_refs"]
    assert "/data/subagents/" not in str(payload)


def test_agent_tree_redacts_legacy_subagent_paths_everywhere():
    class _Manager:
        def kernel_snapshot(self, query):
            return SubagentKernelSnapshot(
                schema_version="subagent_kernel_snapshot.v1",
                scope=query.scope,
                runs=[
                    SubagentKernelRun(
                        run_id="child-1",
                        task_id="child-1",
                        status="RUNNING",
                        workspace_refs={
                            "task_workspace": "/tmp/project/data/subagents/tasks/root-1",
                            "agent_run_workspace": "/tmp/project/data/subagents/tasks/root-1/agents/child-1",
                            "final_report": "/tmp/project/data/subagents/tasks/root-1/agents/child-1/final_report.md",
                        },
                    )
                ],
                source_refs={
                    "root_agent_run_workspace": "/tmp/project/data/subagents/tasks/root-1/agents/child-1",
                },
            )

    class _Agent:
        subagents = _Manager()

    payload = agent_tree_status_payload(_Agent())

    assert "/data/subagents/" not in str(payload)
    assert "[internal_legacy_subagent_path_hidden]" not in str(payload)


def test_agent_tree_exposes_artifact_registry_refs():
    registry_record = {
        "artifact_id": "artifact-report-1",
        "path": "/tmp/run/current/report.xlsx",
        "kind": "xlsx",
        "status": "ready",
    }

    class _Manager:
        def kernel_snapshot(self, query):
            return SubagentKernelSnapshot(
                schema_version="subagent_kernel_snapshot.v1",
                scope=query.scope,
                runs=[
                    SubagentKernelRun(
                        run_id="child-1",
                        task_id="child-1",
                        status="DONE",
                        artifact_refs=["/tmp/run/old/report.xlsx"],
                        artifact_registry_refs=[registry_record],
                    )
                ],
            )

    class _Agent:
        subagents = _Manager()

    payload = agent_tree_status_payload(_Agent())
    node = payload["nodes"][0]

    assert node["artifact_registry_refs"] == [registry_record]
    assert node["evidence_layer"]["artifact_registry_refs"] == [registry_record]


def test_agent_tree_exposes_pending_guidance_layer(tmp_path):
    from agent_py_agent.agent.config import AgentConfig
    from agent_py_agent.agent.core import SimpleAgent

    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    child = agent.subagents.create_run(goal="child", thought="", plan=["child"])
    agent.conversation_store.append_guidance(
        {
            "target_type": "agent_run",
            "target_id": child.id,
            "message": "补读核心源码后再写结论。",
            "priority": "high",
        }
    )

    payload = agent_tree_status_payload(agent, {"root_id": child.id})
    node = payload["nodes"][0]

    assert node["guidance_layer"]["pending_count"] == 1
    assert node["guidance_layer"]["recent_pending"][0]["message"] == "补读核心源码后再写结论。"
    assert node["guidance_layer"]["recent_pending"][0]["priority"] == "high"


def test_agent_tree_reports_pending_guidance_load_error():
    class _ConversationStore:
        def pending_guidance(self, *_args, **_kwargs):
            raise OSError("guidance ledger unreadable")

    class _Manager:
        def kernel_snapshot(self, query):
            return SubagentKernelSnapshot(
                schema_version="subagent_kernel_snapshot.v1",
                scope=query.scope,
                runs=[SubagentKernelRun(run_id="child-1", task_id="child-1", status="RUNNING")],
            )

    class _Agent:
        subagents = _Manager()
        conversation_store = _ConversationStore()

    payload = agent_tree_status_payload(_Agent(), {"root_id": "child-1"})
    guidance_layer = payload["nodes"][0]["guidance_layer"]

    assert guidance_layer["pending_count"] == 0
    assert guidance_layer["warnings"] == ["guidance_unavailable"]
    assert guidance_layer["guidance_load_error"]["context"] == "agent_tree.guidance.pending"
    assert guidance_layer["guidance_load_error"]["category"] == "io"
    assert "不要把它当成" in guidance_layer["guidance_load_error"]["model_message"]


def test_agent_tree_visible_run_ids_filters_prompt_copy_only():
    """后台唤醒只应把当前 thread 相关的 run 放进 prompt 副本。"""

    class _Manager:
        def kernel_snapshot(self, query):
            return SubagentKernelSnapshot(
                schema_version="subagent_kernel_snapshot.v1",
                scope=query.scope,
                runs=[
                    SubagentKernelRun(run_id="child-current", status="DONE"),
                    SubagentKernelRun(run_id="child-old", status="BLOCKED"),
                ],
            )

    class _Agent:
        subagents = _Manager()

    payload = agent_tree_status_payload(_Agent(), {"visible_run_ids": ["child-current"]})

    assert [node["run_id"] for node in payload["nodes"]] == ["child-current"]
    assert payload["main"]["child_run_ids"] == ["child-current"]
    assert payload["status_buckets"]["blocked"] == []


def test_agent_tree_source_refs_prefer_latest_visible_workspace():
    """同 owner 下有旧任务时，source_refs 应指向当前最新可见 workspace。"""
    from agent_py_agent.agent.subagents.kernel import SubagentKernel
    from agent_py_agent.agent.subagents.models import SubAgentTask

    old = SubAgentTask(id="old-child", goal="old", thought="", plan=["old"])
    old.task_workspace_dir = "/tmp/home/tasks/same-slug"
    old.agent_run_workspace_dir = "/tmp/home/tasks/same-slug/work/agents/old-child"
    old.updated_at = 100.0
    new = SubAgentTask(id="new-child", goal="new", thought="", plan=["new"])
    new.task_workspace_dir = "/tmp/home/tasks/same-slug-run-new"
    new.agent_run_workspace_dir = "/tmp/home/tasks/same-slug-run-new/work/agents/new-child"
    new.updated_at = 200.0

    class _Manager:
        def list_runs(self):
            return [old, new]

        def kernel_snapshot(self, query):
            return SubagentKernel(self).snapshot(query)

    class _Agent:
        subagents = _Manager()

    payload = agent_tree_status_payload(_Agent())

    assert payload["source_refs"]["root_task"] == "/tmp/home/tasks/same-slug-run-new"


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


def test_main_agent_tree_defaults_to_visible_children_instead_of_first_remembered_run():
    """主代理查树时不应只收窄到第一个 remembered child，避免看板空树/漏子代理。"""

    class _Manager:
        seen_query = None

        def kernel_snapshot(self, query):
            self.seen_query = query
            return SubagentKernelSnapshot(
                schema_version="subagent_kernel_snapshot.v1",
                scope=query.scope,
                runs=[
                    SubagentKernelRun(run_id="child-1", status="RUNNING", artifact_refs=["a.md"]),
                    SubagentKernelRun(run_id="child-2", status="DONE", artifact_refs=["b.md"]),
                ],
            )

    class _Agent:
        _remembered_orchestration_run_ids = ["child-1"]
        subagents = _Manager()

    payload = agent_tree_status_payload(_Agent())

    assert _Agent.subagents.seen_query.run_id == ""
    assert [node["run_id"] for node in payload["nodes"]] == ["child-1", "child-2"]
    assert payload["child_result_index"][1]["primary_artifact_refs"] == ["b.md"]


def test_main_run_root_query_falls_back_to_visible_tree_when_no_subagent_root_matches():
    """显式传主代理 run_id 时，如果它不是子代理 root，也要返回可见子代理树。"""

    class _Manager:
        queries = []

        def kernel_snapshot(self, query):
            self.queries.append(query)
            if query.root_id == "main-run-1":
                return SubagentKernelSnapshot(schema_version="subagent_kernel_snapshot.v1", scope=query.scope, runs=[])
            return SubagentKernelSnapshot(
                schema_version="subagent_kernel_snapshot.v1",
                scope=query.scope,
                runs=[SubagentKernelRun(run_id="child-1", status="DONE", artifact_refs=["done.md"])],
            )

    class _Agent:
        _main_agent_run_id = "main-run-1"
        subagents = _Manager()

    payload = agent_tree_status_payload(_Agent(), {"root_id": "main-run-1"})

    assert len(_Agent.subagents.queries) == 2
    assert payload["nodes"][0]["run_id"] == "child-1"
    assert "main_run_scope_had_no_subagent_rows_returned_visible_tree" in payload["warnings"]


def test_cli_style_main_run_id_falls_back_to_visible_tree_without_agent_attribute():
    """CLI run_id 是 run-*，即使 agent 没挂 _main_agent_run_id，也应能查到子代理树。"""

    class _Manager:
        queries = []

        def kernel_snapshot(self, query):
            self.queries.append(query)
            if query.root_id == "run-1780171761499764000":
                return SubagentKernelSnapshot(schema_version="subagent_kernel_snapshot.v1", scope=query.scope, runs=[])
            return SubagentKernelSnapshot(
                schema_version="subagent_kernel_snapshot.v1",
                scope=query.scope,
                runs=[SubagentKernelRun(run_id="subagent-1", status="DONE", artifact_refs=["result.md"])],
            )

    class _Agent:
        subagents = _Manager()

    payload = agent_tree_status_payload(_Agent(), {"root_id": "run-1780171761499764000"})

    assert len(_Agent.subagents.queries) == 2
    assert payload["child_result_index"][0]["run_id"] == "subagent-1"
    assert payload["child_result_index"][0]["primary_artifact_refs"] == ["result.md"]


def test_cli_main_run_fallback_is_limited_to_current_orchestration_ids():
    """主代理 run-* 回退不能把旧任务树混进当前 run。"""

    class _Manager:
        def kernel_snapshot(self, query):
            if query.root_id == "run-1780172303447861000":
                return SubagentKernelSnapshot(schema_version="subagent_kernel_snapshot.v1", scope=query.scope, runs=[])
            return SubagentKernelSnapshot(
                schema_version="subagent_kernel_snapshot.v1",
                scope=query.scope,
                runs=[
                    SubagentKernelRun(run_id="old-child", status="DONE", artifact_refs=["old.md"]),
                    SubagentKernelRun(run_id="current-child", status="RUNNING", artifact_refs=["current.md"]),
                    SubagentKernelRun(run_id="current-grandchild", parent_run_id="current-child", status="DONE", artifact_refs=["leaf.md"]),
                ],
            )

    class _Agent:
        _orchestration_run_ids_seen = {"current-child"}
        subagents = _Manager()

    payload = agent_tree_status_payload(_Agent(), {"root_id": "run-1780172303447861000"})
    run_ids = [row["run_id"] for row in payload["child_result_index"]]

    assert run_ids == ["current-child", "current-grandchild"]
    assert payload["status_buckets"]["completed"] == ["current-grandchild"]
    assert "old-child" not in str(payload)


def test_agent_tree_soft_advice_does_not_treat_running_children_as_failed_outputs():
    """父代理查树时，应得到软提示：运行中的子代理缺产物不是失败。"""

    class _Manager:
        def kernel_snapshot(self, query):
            return SubagentKernelSnapshot(
                schema_version="subagent_kernel_snapshot.v1",
                scope=query.scope,
                runs=[
                    SubagentKernelRun(run_id="child-running", status="RUNNING"),
                    SubagentKernelRun(run_id="child-planning", status="PLANNING"),
                    SubagentKernelRun(run_id="child-done", status="DONE", artifact_refs=["done.md"]),
                ],
            )

    class _Agent:
        subagents = _Manager()

    payload = agent_tree_status_payload(_Agent())
    advice = payload["coordination_advice"]

    assert advice["soft_only"] is True
    assert advice["pending_child_run_ids"] == ["child-running", "child-planning"]
    assert advice["completed_child_run_ids"] == ["child-done"]
    assert advice["missing_outputs_while_running_is_failure"] is False
    assert advice["should_take_over_running_children"] is False
    assert "不要把目标目录暂时为空或占位报告当失败" in payload["policy"]["next_step"]


def test_child_result_index_keeps_progress_refs_for_running_child_without_artifacts():
    """运行中的子代理即使还没有 artifact，也要给父代理可读的进度 refs。"""

    class _Manager:
        def kernel_snapshot(self, query):
            return SubagentKernelSnapshot(
                schema_version="subagent_kernel_snapshot.v1",
                scope=query.scope,
                runs=[
                    SubagentKernelRun(
                        run_id="child-running",
                        status="RUNNING",
                        progress=0.2,
                        current_tool="list_files",
                        last_progress_summary="正在读项目目录",
                        latest_summary="最近成功调用工具: list_files",
                        workspace_refs={
                            "task_workspace": "/tmp/home/owners/local/main/tasks/2026-06-03/all-agent-架构分析",
                            "agent_run_workspace": "/tmp/home/owners/local/main/tasks/2026-06-03/all-agent-架构分析/work/agents/child-running",
                            "final_report": "/tmp/home/owners/local/main/tasks/2026-06-03/all-agent-架构分析/work/agents/child-running/final_report.md",
                        },
                        recovery_refs={
                            "summary": "/tmp/home/owners/local/main/tasks/2026-06-03/all-agent-架构分析/work/agents/child-running/summary.md",
                            "checkpoint": "/tmp/home/owners/local/main/tasks/2026-06-03/all-agent-架构分析/work/agents/child-running/checkpoint.json",
                        },
                        reserved={
                            "recent_tool_trace": [
                                {"tool": "list_files", "ok": True, "summary": "最近成功调用工具: list_files"}
                            ],
                        },
                    )
                ],
            )

    class _Agent:
        subagents = _Manager()

    payload = agent_tree_status_payload(_Agent())
    row = payload["child_result_index"][0]

    assert row["primary_artifact_refs"] == []
    assert row["final_report_ref"].endswith("/work/agents/child-running/final_report.md")
    assert row["summary_ref"].endswith("/work/agents/child-running/summary.md")
    assert row["checkpoint_ref"].endswith("/work/agents/child-running/checkpoint.json")
    assert row["agent_work_dir"].endswith("/work/agents/child-running")
    assert row["readiness"] == "progress_refs_available"
    assert row["recent_tool_trace"] == [{"tool": "list_files", "ok": True, "summary": "最近成功调用工具: list_files"}]
