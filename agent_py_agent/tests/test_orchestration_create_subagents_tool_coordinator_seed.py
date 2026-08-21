"""LLM: coordinator seed tests stay separate so orchestration tool tests stay reviewable.

模块用途: 验证 root/coordinator 派工意图不会被模型的 role/workflow 参数写偏。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock


class TestCreateSubagentsToolCoordinatorSeed:
    """测试 coordinator/root seed 的边界继承和 workflow 保护。"""

    def test_slash_separated_deliverable_labels_do_not_trip_external_write_guard(self):
        """交付物标签里的斜杠不是绝对路径，不能误拦截 coordinator seed。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
        mock_agent.subagents.workspace_root = Path("/Users/example/my-claude-code")
        mock_agent.subagents.workspace_roots = [Path("/Users/example/my-claude-code")]

        mock_task = MagicMock()
        mock_task.id = "coordinator_001"
        mock_task.goal = ""
        mock_task.status = "PLANNING"
        mock_task.verification_status = "UNVERIFIED"
        mock_task.task_dir = "/tmp/coordinator_001"
        mock_agent.subagents.create_run.return_value = mock_task

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({
            "goal": (
                "Create deliverables named requirements/research-brief/implementation/"
                "README/bug-report/test-report/acceptance-verdict inside the approved root."
            ),
            "role": "coordinator",
            "extra_write_roots": ["/Users/example/my-claude-code/deliverables/role-template"],
        })

        assert result.ok is True
        mock_agent.subagents.create_run.assert_called_once()

    def test_explicit_coordinator_seed_creates_one_root(self):
        """显式 coordinator seed 只创建一个根节点。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10

        mock_task = MagicMock()
        mock_task.id = "coordinator_001"
        mock_task.goal = ""
        mock_task.status = "PLANNING"
        mock_task.verification_status = "UNVERIFIED"
        mock_task.task_dir = "/tmp/coordinator_001"
        mock_agent.subagents.create_run.return_value = mock_task

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({
            "goal": "Seed one root coordinator. Children must be created by that coordinator.",
            "role": "coordinator",
        })

        params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert result.ok is True
        assert params.role == "coordinator"

    def test_natural_coordinator_seed_intent_does_not_repair_model_worker_role(self):
        """模型把 root coordinator 误写成 worker 时，工具层不从自然语言目标纠偏。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
        mock_agent._current_user_prompt = (
            "请建立主代理 -> 小傻妞-root-coordinator -> 小小傻妞-child-coordinator "
            "-> 小小小傻妞-leaf-worker 的链路。第一层必须创建下一层，不能自己写最终产物。"
        )

        mock_task = MagicMock()
        mock_task.id = "root_001"
        mock_task.goal = ""
        mock_task.status = "PLANNING"
        mock_task.verification_status = "UNVERIFIED"
        mock_task.task_dir = "/tmp/root_001"
        mock_agent.subagents.create_run.return_value = mock_task

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({
            "goal": (
                "创建 小傻妞-root-coordinator，并让它使用 create_subagents "
                "继续创建 小小傻妞-child-coordinator；本节点不要写最终产物。"
            ),
            "role": "worker",
        })

        params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert result.ok is True
        assert params.role == "worker"

    def test_child_creation_tool_grant_repairs_worker_to_coordinator(self):
        """显式给统一 child 创建工具时，工具层按结构化能力纠偏为 coordinator。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10

        mock_task = MagicMock()
        mock_task.id = "root_001"
        mock_task.goal = ""
        mock_task.status = "PLANNING"
        mock_task.verification_status = "UNVERIFIED"
        mock_task.task_dir = "/tmp/root_001"
        mock_agent.subagents.create_run.return_value = mock_task

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({
            "goal": "创建第一层 root coordinator。",
            "role": "worker",
            "allowed_tools": ["read_file", "write_file", "create_subagents"],
        })

        params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert result.ok is True
        assert params.role == "coordinator"

    def test_user_style_delegate_to_next_layer_does_not_repair_without_structured_signal(self):
        """用户说小傻妞可再找小小傻妞时，代码层不靠自然语言把 worker 改成 coordinator。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
        mock_agent._current_user_prompt = (
            "请你派小傻妞来完成这个任务，不要你自己亲自写页面。"
            "如果任务比较多，可以让小傻妞再找小小傻妞帮忙。"
        )

        mock_task = MagicMock()
        mock_task.id = "root_001"
        mock_task.goal = ""
        mock_task.status = "PLANNING"
        mock_task.verification_status = "UNVERIFIED"
        mock_task.task_dir = "/tmp/root_001"
        mock_agent.subagents.create_run.return_value = mock_task

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({
            "goal": "做 3 个高端现代家具品牌网站首页 HTML 文件。",
            "role": "worker",
        })

        params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert result.ok is True
        assert params.role == "worker"

    def test_user_style_if_needed_delegate_does_not_repair_child_without_structured_signal(self):
        """用户说小傻妞必要时找小小傻妞时，role=child 不靠自然语言变 coordinator。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
        mock_agent._current_user_prompt = "请组织小傻妞协作完成；每个小傻妞如果需要，可以再找小小傻妞帮忙。"

        mock_task = MagicMock()
        mock_task.id = "market_001"
        mock_task.goal = ""
        mock_task.status = "PLANNING"
        mock_task.verification_status = "UNVERIFIED"
        mock_task.task_dir = "/tmp/market_001"
        mock_agent.subagents.create_run.return_value = mock_task

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({
            "goal": "研究东南亚市场环境，输出国家优先级和证据摘要。",
            "role": "child",
            "agent_name": "小傻妞-市场环境",
            "allowed_tools": ["read_file", "write_file"],
        })

        params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert result.ok is True
        assert params.role == "child"


class TestCreateSubagentsToolCoordinatorPlan:
    """测试 coordinator plan 和 lineage 名称处理。"""

    def test_items_count_and_nested_children_become_coordinator_plan(self):
        """items 内显式 children 会让当前 item 成为 coordinator 计划。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
        mock_agent._current_user_prompt = "请派小傻妞研究市场；每个小傻妞可以找小小傻妞帮忙。"

        mock_task = MagicMock()
        mock_task.id = "market_001"
        mock_task.goal = ""
        mock_task.status = "PLANNING"
        mock_task.verification_status = "UNVERIFIED"
        mock_task.task_dir = "/tmp/market_001"
        mock_agent.subagents.create_run.return_value = mock_task

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({
            "goal": "组织市场研究并汇总评分",
            "items": [{
                "goal": "研究印尼、泰国、越南市场环境，汇总评分。",
                "role": "worker",
                "agent_name": "小傻妞-市场",
                "children": [
                    {"goal": "分析印尼市场", "role": "grandchild", "agent_name": "印尼研究员"},
                    {"goal": "分析泰国和越南市场", "role": "grandchild", "agent_name": "泰越研究员"},
                ],
            }],
        })

        params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert result.ok is True
        assert params.role == "coordinator"
        assert any("分析印尼市场" in item for item in params.plan)
        assert any("泰越研究员" in item for item in params.plan)

    def test_explicit_child_creation_tool_repairs_worker_to_coordinator(self):
        """显式给统一 child 创建工具时，role=worker 按带队节点创建。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
        mock_agent._current_user_prompt = "请让小傻妞分别研究市场、竞争和策略。"
        mock_agent.subagents.workspace_root = Path("/tmp/project")
        mock_agent.subagents.workspace_roots = [Path("/tmp/project")]

        mock_task = MagicMock()
        mock_task.id = "market_001"
        mock_task.goal = ""
        mock_task.status = "PLANNING"
        mock_task.verification_status = "UNVERIFIED"
        mock_task.task_dir = "/tmp/market_001"
        mock_agent.subagents.create_run.return_value = mock_task

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({
            "goal": "组织代理研究市场环境并交付报告",
            "items": [{
                "goal": (
                    "研究市场环境。每个子代理需要用统一创建入口建立至少2个孙代理负责细分研究，"
                    "最终产出写到 /tmp/project/deliverables/market_env_report.md"
                ),
                "role": "worker",
                "agent_name": "小傻妞-市场环境",
                "allowed_tools": ["read_file", "write_file", "create_subagents"],
            }],
            "extra_write_roots": ["/tmp/project/deliverables"],
        })

        params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert result.ok is True
        assert params.role == "coordinator"

    def test_role_field_is_not_repaired_from_display_agent_name(self):
        """role 是结构化字段；display name 误填到 role 时不再靠中文名字兜底纠错。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10

        mock_task = MagicMock()
        mock_task.id = "root_001"
        mock_task.goal = ""
        mock_task.status = "PLANNING"
        mock_task.verification_status = "UNVERIFIED"
        mock_task.task_dir = "/tmp/root_001"
        mock_agent.subagents.create_run.return_value = mock_task

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({
            "goal": "创建第一层 root coordinator，并使用 create_subagents 创建下一层。",
            "role": "小傻妞-root-coordinator",
        })

        params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert result.ok is True
        assert params.role == "小傻妞-root-coordinator"

    def test_structured_role_and_agent_name_still_create_coordinator(self):
        """正确结构是 role 放模板 ID，agent_name 放展示名。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10

        mock_task = MagicMock()
        mock_task.id = "root_001"
        mock_task.goal = ""
        mock_task.status = "PLANNING"
        mock_task.verification_status = "UNVERIFIED"
        mock_task.task_dir = "/tmp/root_001"
        mock_agent.subagents.create_run.return_value = mock_task

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({
            "goal": "创建第一层 root coordinator，并使用 create_subagents 创建下一层。",
            "role": "coordinator",
            "agent_name": "小傻妞-root-coordinator",
        })

        params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert result.ok is True
        assert params.role == "coordinator"
        assert params.agent_name == "小傻妞-root-coordinator"


class TestCreateSubagentsLogicalRefScopes:
    """seq 253 锁层级自冲突回归：input_refs/replacement_for_run_ids 不是写根。

    修复前：这两个参数无 parameter_kinds → 未传时走 None 兜底锁
    workspace:{cwd}（父锁）→ 与 output_files 声明的工作区内子路径锁
    （子锁）同一事务父子重叠 → RuntimeConflictError →
    TOOL_OPERATION_STORE_UNAVAILABLE → create_subagents 从未运行 →
    子代理 0 创建（真机 natural-language e2e 实锤）。修复=标 logical
    （seq 248 #6 既有机制），只有 output_files 锁 workspace。
    """

    def test_output_files_child_path_does_not_conflict_with_workspace_root(self):
        """output_files 在工作区内（父/子关系）时，scope 只含子路径锁。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
        from agent_py_agent.agent.tooling.workspace_scopes import authoritative_workspace_scopes

        tool = CreateSubagentsTool(MagicMock())
        policy = tool.runtime_policy
        root = Path("/tmp/lock-hierarchy-root")
        scopes = authoritative_workspace_scopes(
            workspace_root=root,
            write_boundary=None,
            policy=policy,
            arguments={"output_files": [str(root / "site" / "index.html")]},
        )
        child = (root / "site" / "index.html").resolve()
        assert f"workspace:{root.resolve()}" not in scopes  # 无父锁
        assert f"workspace:{child}" in scopes  # 只有子路径锁

    def test_logical_kinds_do_not_lock_workspace(self):
        """input_refs/replacement_for_run_ids 缺省时不得投影 workspace 锁。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
        from agent_py_agent.agent.tooling.workspace_scopes import authoritative_workspace_scopes

        tool = CreateSubagentsTool(MagicMock())
        policy = tool.runtime_policy
        root = Path("/tmp/lock-logical-root")
        scopes = authoritative_workspace_scopes(
            workspace_root=root,
            write_boundary=None,
            policy=policy,
            arguments={"output_files": [str(root / "site" / "index.html")]},
        )
        child = (root / "site" / "index.html").resolve()
        assert not any(
            scope.startswith("workspace:") and scope != f"workspace:{child}"
            for scope in scopes
        )


class TestLogicalIdToolsSelfConflictRegression:
    """seq 258 复核反例：wait/guidance/record_finding 的逻辑 ID 参数被当 path 锁。

    修复前：run_id/task_id/thread_id、target_id/run_ids/root_id、finding_id/watch_id
    无 parameter_kinds → 默认按 path 解析。传一个 ID（如 wait run_id=child-1）→
    锁 workspace:{root}/child-1（子）；其余参数缺省 → None 兜底锁 workspace:{cwd}
    （父）→ 同一操作父子重叠 → claim 阶段自冲突（handler 前被拒）。
    修复=逐工具把逻辑 ID 标 logical，只让真实写路径进 workspace scope。
    """

    def test_guidance_target_id_is_logical_not_path(self):
        from agent_py_agent.agent.agent_core.runtime.guidance_tool import SendGuidanceTool
        from agent_py_agent.agent.tooling.workspace_scopes import authoritative_workspace_scopes

        tool = SendGuidanceTool(MagicMock())
        root = Path("/tmp/guidance-logical-id")
        scopes = authoritative_workspace_scopes(
            workspace_root=root,
            write_boundary=None,
            policy=tool.runtime_policy,
            arguments={"target_id": "sub-1"},
        )
        assert not any(s.startswith("workspace:") for s in scopes)
        assert "logical:agent_run:sub-1" in scopes

    def test_guidance_run_ids_list_is_logical(self):
        from agent_py_agent.agent.agent_core.runtime.guidance_tool import SendGuidanceTool
        from agent_py_agent.agent.tooling.workspace_scopes import authoritative_workspace_scopes

        tool = SendGuidanceTool(MagicMock())
        root = Path("/tmp/guidance-logical-ids")
        scopes = authoritative_workspace_scopes(
            workspace_root=root,
            write_boundary=None,
            policy=tool.runtime_policy,
            arguments={"run_ids": ["r-1", "r-2"]},
        )
        assert not any(s.startswith("workspace:") for s in scopes)
        # seq 261 #2：list 逐元素投影去重，绝无整体 json.dumps 串——[r-1,r-2]
        # 与 [r-2,r-3] 必须能在共同目标 r-2 上互斥（整体串交集为 0 是缺陷）。
        assert "logical:agent_run:r-1" in scopes
        assert "logical:agent_run:r-2" in scopes
        assert len(scopes) == 2
        assert not any("[" in s or "]" in s for s in scopes)

    def test_overlapping_logical_lists_conflict_on_shared_element(self):
        """重叠 logical list 必冲突：run_ids=[r-1,r-2] 与 [r-2,r-3] 在 r-2 互斥。"""
        from agent_py_agent.agent.agent_core.runtime.guidance_tool import SendGuidanceTool
        from agent_py_agent.agent.tooling.workspace_scopes import authoritative_workspace_scopes

        tool = SendGuidanceTool(MagicMock())
        root = Path("/tmp/guidance-overlap")
        a = authoritative_workspace_scopes(
            workspace_root=root, write_boundary=None,
            policy=tool.runtime_policy, arguments={"run_ids": ["r-1", "r-2"]},
        )
        b = authoritative_workspace_scopes(
            workspace_root=root, write_boundary=None,
            policy=tool.runtime_policy, arguments={"run_ids": ["r-2", "r-3"]},
        )
        shared = set(a) & set(b)
        assert shared == {"logical:agent_run:r-2"}

    def test_audit_publish_source_refs_are_logical_not_paths(self):
        """audit publish：source_probe_refs/remove_source_ids 不再锁假写根。

        修复前（无 parameter_kinds）：audit({source_probe_refs:[ws-1]}) →
        workspace:{cwd}/ws-1（子）+ 缺省 None 兜底 workspace:{cwd}（父）→
        父子自冲突，publish 永远调不通（真机实测互撞）。
        """
        from agent_py_agent.agent.conversation.audit_tools import PublishAuditUpdateTool
        from agent_py_agent.agent.tooling.workspace_scopes import authoritative_workspace_scopes

        tool = PublishAuditUpdateTool(MagicMock())
        root = Path("/tmp/audit-publish")
        scopes = authoritative_workspace_scopes(
            workspace_root=root, write_boundary=None,
            policy=tool.runtime_policy,
            arguments={"source_probe_refs": ["ws-1"], "remove_source_ids": ["src-2"]},
        )
        assert not any(s.startswith("workspace:") for s in scopes)
        assert "logical:source_probe:ws-1" in scopes
        assert "logical:source:src-2" in scopes
        assert len(scopes) == 2

    def test_watch_stream_source_ids_are_logical_not_paths(self):
        """watch_stream（audit_source 面）：watch_id/source_ref 不再锁假写根。"""
        from agent_py_agent.agent.ingestion.watch_tool_spec import (
            build_watch_stream_runtime_policy,
        )
        from agent_py_agent.agent.tooling.workspace_scopes import authoritative_workspace_scopes

        root = Path("/tmp/watch-audit-source")
        scopes = authoritative_workspace_scopes(
            workspace_root=root, write_boundary=None,
            policy=build_watch_stream_runtime_policy(surface="audit_source"),
            arguments={"action": "verdict", "watch_id": "w-1", "source_ref": "src-1"},
        )
        assert not any(s.startswith("workspace:") for s in scopes)
        assert "logical:watch:w-1" in scopes
        assert "logical:watch:src-1" in scopes

    def test_watch_stream_open_url_is_logical_not_path(self):
        """watch_stream（ordinary 面）：open 的 url 不再锁假写根。"""
        from agent_py_agent.agent.ingestion.watch_tool_spec import (
            build_watch_stream_runtime_policy,
        )
        from agent_py_agent.agent.tooling.workspace_scopes import authoritative_workspace_scopes

        root = Path("/tmp/watch-open")
        scopes = authoritative_workspace_scopes(
            workspace_root=root, write_boundary=None,
            policy=build_watch_stream_runtime_policy(surface="ordinary"),
            arguments={"action": "open", "url": "https://example.com/feed"},
        )
        assert not any(s.startswith("workspace:") for s in scopes)
        assert "logical:watch:https://example.com/feed" in scopes

    def test_watch_url_alias_adds_the_same_canonical_watch_scope_as_watch_id(self, tmp_path):
        """同一 watch 的 URL/open 与 watch_id 写入口必须在 claim 前互斥。"""
        from agent_py_agent.agent.agent_core.runtime.record_finding_tool import (
            execute_record_finding,
        )
        from agent_py_agent.agent.ingestion.watch_state import watch_id_for
        from agent_py_agent.agent.ingestion.watch_tool import WatchStreamTool
        from agent_py_agent.agent.tooling.executor import _workspace_operation_scopes

        owner_home = tmp_path / "owner"
        agent = SimpleNamespace(
            home_paths=SimpleNamespace(owner_home_dir=owner_home),
            _current_run_params=None,
        )
        watch_tool = WatchStreamTool(agent)
        url = "https://example.com/feed?cursor=<next>"
        watch_id = watch_id_for(owner_home, "https://example.com/feed")

        open_arguments = {"action": "open", "url": url}
        request = SimpleNamespace(workspace_root=tmp_path, write_boundary=None)
        open_scopes = _workspace_operation_scopes(
            request,
            SimpleNamespace(arguments=open_arguments),
            SimpleNamespace(runtime_policy=watch_tool.runtime_policy, handler=watch_tool),
        )
        finding_tool = watch_tool
        finding_scopes = _workspace_operation_scopes(
            request,
            SimpleNamespace(arguments={"watch_id": watch_id}),
            SimpleNamespace(
                runtime_policy=finding_tool.runtime_policy,
                handler=finding_tool,
            ),
        )

        canonical = f"logical:watch:{watch_id}"
        assert canonical in open_scopes
        assert canonical in finding_scopes
        assert set(open_scopes) & set(finding_scopes) == {canonical}

    def test_watch_url_alias_keeps_named_audit_identity_isolated(self, tmp_path):
        """同 URL 的普通 watch 与命名 Audit watch 不能误归成同一资源。"""
        from agent_py_agent.agent.common.audit_activation import (
            AUDIT_ATTR,
            AUDIT_SOURCE_BINDING_PENDING_ATTR,
        )
        from agent_py_agent.agent.conversation.authority import (
            CONVERSATION_REQUEST_ID_ATTR,
        )
        from agent_py_agent.agent.ingestion.watch_state import watch_id_for
        from agent_py_agent.agent.ingestion.watch_tool import WatchStreamTool

        owner_home = tmp_path / "owner"
        audit_id = "audit-1"
        agent = SimpleNamespace(
            home_paths=SimpleNamespace(owner_home_dir=owner_home),
            _current_run_params=SimpleNamespace(
                task_attributes={
                    AUDIT_ATTR: True,
                    AUDIT_SOURCE_BINDING_PENDING_ATTR: True,
                    CONVERSATION_REQUEST_ID_ATTR: audit_id,
                }
            ),
        )
        tool = WatchStreamTool(agent)
        arguments = {
            "action": "open",
            "url": "https://example.com/feed?cursor=<next>",
        }

        scopes = tool.effective_resource_scopes(arguments, None, tmp_path)
        canonical_url = "https://example.com/feed"
        audit_scope = f"logical:watch:{watch_id_for(owner_home, canonical_url, audit_id)}"
        ordinary_scope = f"logical:watch:{watch_id_for(owner_home, canonical_url)}"

        assert scopes == (audit_scope,)
        assert ordinary_scope not in scopes

    def test_watch_source_ref_alias_projects_its_embedded_watch_id(self, tmp_path):
        """Audit source_ref 必须复用引用内的 watch_id，不能锁整段引用。"""
        from agent_py_agent.agent.ingestion.watch_tool import WatchStreamTool

        agent = SimpleNamespace(
            home_paths=SimpleNamespace(owner_home_dir=tmp_path / "owner"),
            _current_run_params=None,
        )
        tool = WatchStreamTool(agent)
        watch_id = "ws-0123456789"

        scopes = tool.effective_resource_scopes(
            {
                "action": "inspect",
                "source_ref": f"audit://{watch_id}/candidate/12:0",
            },
            None,
            tmp_path,
        )

        assert scopes == (f"logical:watch:{watch_id}",)


class TestResourceDomainAliasingPerSeq266:
    """seq 266 复核反例：scope 必须是「同一底层资源」互斥，不是「参数名+原值」。

    修复前：cancel(run_id=r-1) 产 logical:run_id:r-1、cancel(run_ids=[r-1]) 产
    logical:run_ids:r-1，交集空——但 handler 的 _explicit_run_ids 把两者归一成
    同一目标；logical 值只 strip 检查不 strip 存储（" r-1 " 可绕过）；root_id/
    status 在 handler 内展开，claim 前 scope 不含真实目标；audit 锁的是引用
    不是 Audit 本体（无 refs 时 scopes=() 空锁）。
    修复=资源域别名映射（run_id/run_ids→agent_run 域，先 strip 再构造）+
    effective_resource_scopes hook（claim 前合并展开的真实目标，解析失败保守
    锁稳定域不空锁）+ audit 锁 audit:{task_id}。
    """

    def test_cancel_run_id_vs_run_ids_alias_must_conflict(self):
        # 反例 1：同一 run 的两种参数入口必须锁同一 scope（handler 会归一）。
        from agent_py_agent.agent.agent_core.orchestration.tools.cancel import (
            CancelSubagentsTool,
        )
        from agent_py_agent.agent.tooling.workspace_scopes import (
            authoritative_workspace_scopes,
        )

        tool = CancelSubagentsTool(MagicMock())
        root = Path("/tmp/cancel-alias")
        single = authoritative_workspace_scopes(
            workspace_root=root,
            write_boundary=None,
            policy=tool.runtime_policy,
            arguments={"run_id": "r-1"},
        )
        many = authoritative_workspace_scopes(
            workspace_root=root,
            write_boundary=None,
            policy=tool.runtime_policy,
            arguments={"run_ids": ["r-1"]},
        )
        assert set(single) & set(many), (
            f"run_id 与 run_ids 指向同一 run 必须互斥: {single} vs {many}"
        )

    def test_cancel_root_expansion_locks_child_runs(self):
        # 反例 2：cancel(root_id=X) 在 handler 内展开子树 → claim 前必须锁具体子 run。
        from types import SimpleNamespace

        from agent_py_agent.agent.agent_core.orchestration.tools.cancel import (
            CancelSubagentsTool,
        )
        from agent_py_agent.agent.tooling.workspace_scopes import (
            authoritative_workspace_scopes,
        )

        def _task(tid: str, parent: str = "") -> SimpleNamespace:
            return SimpleNamespace(id=tid, parent_id=parent)

        tasks = [_task("child-1", "root-X"), _task("root-X", "")]
        agent = SimpleNamespace(subagents=SimpleNamespace(list_runs=lambda: list(tasks)))
        tool = CancelSubagentsTool(agent)
        hook = getattr(tool, "effective_resource_scopes", None)
        assert hook is not None, "cancel 必须提供 effective_resource_scopes hook（seq 266 #3）"
        root = Path("/tmp/cancel-root")
        hook_scopes = hook({"root_id": "root-X"}, None, root)
        direct = authoritative_workspace_scopes(
            workspace_root=root,
            write_boundary=None,
            policy=tool.runtime_policy,
            arguments={"run_id": "child-1"},
        )
        assert set(hook_scopes) & set(direct), (
            "root 展开出的 child run 必须与直接 run_id 锁互斥"
        )

    def test_logical_value_stripped_before_scope(self):
        # 反例 4：首尾空格别名必须锁同一 scope（handler 会 strip 成同一目标）。
        from agent_py_agent.agent.agent_core.runtime.guidance_tool import (
            SendGuidanceTool,
        )
        from agent_py_agent.agent.tooling.workspace_scopes import (
            authoritative_workspace_scopes,
        )

        tool = SendGuidanceTool(MagicMock())
        root = Path("/tmp/guidance-strip")
        clean = authoritative_workspace_scopes(
            workspace_root=root,
            write_boundary=None,
            policy=tool.runtime_policy,
            arguments={"target_id": "r-1"},
        )
        padded = authoritative_workspace_scopes(
            workspace_root=root,
            write_boundary=None,
            policy=tool.runtime_policy,
            arguments={"target_id": "  r-1  "},
        )
        assert set(clean) & set(padded), (
            "首尾空格是同一目标（handler strip），scope 必须相同"
        )

    def test_audit_publish_locks_audit_domain_not_refs(self):
        # 反例 5：publish 锁 Audit 本体（audit:{task_id}），无 refs/不同 refs 也互斥。
        from types import SimpleNamespace

        from agent_py_agent.agent.conversation.audit_tools import PublishAuditUpdateTool

        agent = SimpleNamespace(
            _current_run_params=SimpleNamespace(
                task_attributes={"conversation_task_id": "task-9"}
            )
        )
        tool = PublishAuditUpdateTool(agent)
        hook = getattr(tool, "effective_resource_scopes", None)
        assert hook is not None, "audit publish 必须提供 effective_resource_scopes hook（seq 266 #4）"
        root = Path("/tmp/audit-domain")
        no_refs = hook({}, None, root)
        with_refs = hook({"source_probe_refs": ["ws-1"]}, None, root)
        assert set(no_refs) & set(with_refs), (
            "同一 Audit 的无 refs/不同 refs 发布必须互斥"
        )
        assert any(s.startswith("audit:") for s in no_refs), (
            "必须锁 audit 域，不能空锁"
        )


class TestResourceScopePolicyNormalizationPerSeq269:
    """seq 269 两个底层契约缺口：domains 规范化写回 + hook 失败 fail-closed。"""

    def test_resource_domains_normalized_and_written_back(self):
        # 反例 6（seq 269 #2）：__post_init__ 计算了规范化 domains 却漏写回——
        # 校验值和运行时使用值不是同一份。带空格的参数名键必须 strip 后写回，
        # 投影才查得到映射（否则退化成参数名域 logical:run_id:r-1）。
        from agent_py_agent.agent.tooling.models import ResourceScopePolicy

        policy = ResourceScopePolicy(
            parameter_names=("run_id",),
            parameter_kinds={"run_id": "logical"},
            resource_domains={" run_id ": " agent_run "},
        )
        assert policy.resource_domains == {"run_id": "agent_run"}, (
            "规范化后的 domains 必须写回（校验值 = 运行时使用值）"
        )
        from pathlib import Path

        from agent_py_agent.agent.tooling.models import (
            EffectResolverPolicy,
            ToolRuntimePolicy,
        )
        from agent_py_agent.agent.tooling.workspace_scopes import (
            authoritative_workspace_scopes,
        )

        scopes = authoritative_workspace_scopes(
            workspace_root=Path("/tmp/domain-writeback"),
            write_boundary=None,
            policy=ToolRuntimePolicy(
                effect_resolver=EffectResolverPolicy("mutating"),
                resource_scopes=ResourceScopePolicy(
                    parameter_names=("run_id",),
                    parameter_kinds={"run_id": "logical"},
                    resource_domains={" run_id ": " agent_run "},
                ),
            ),
            arguments={"run_id": "r-1"},
        )
        assert "logical:agent_run:r-1" in scopes, (
            "strip 后的域映射必须生效: %s" % (scopes,)
        )
        assert not any(s == "logical:run_id:r-1" for s in scopes), (
            "不能退化成未映射的参数名域"
        )

    def test_effective_resource_scopes_hook_failure_fails_closed(self):
        # 反例 7（seq 269 #3）：hook 抛错 = 实现缺陷，executor 必须 fail-closed
        # （ResourceScopeResolutionError 上抛拒绝执行），不能静默降级成无保护
        # 执行；BaseTool 有正式默认 hook（空元组），不做 getattr 半协议。
        from pathlib import Path
        from types import SimpleNamespace

        import pytest

        from agent_py_agent.agent.tooling.executor import _workspace_operation_scopes
        from agent_py_agent.agent.tooling.models import (
            BaseTool,
            EffectResolverPolicy,
            ResourceScopePolicy,
            ResourceScopeResolutionError,
            ToolRuntimePolicy,
        )

        class _PlainTool(BaseTool):
            runtime_policy = ToolRuntimePolicy(
                effect_resolver=EffectResolverPolicy("mutating")
            )

            def execute(self, params):
                raise AssertionError("not reached")

        assert _PlainTool().effective_resource_scopes({}, None, Path("/tmp/x")) == (), (
            "BaseTool 必须正式声明默认 effective_resource_scopes=()"
        )

        class _BrokenHookTool(BaseTool):
            runtime_policy = ToolRuntimePolicy(
                effect_resolver=EffectResolverPolicy("mutating"),
                resource_scopes=ResourceScopePolicy(
                    parameter_names=("run_id",),
                    parameter_kinds={"run_id": "logical"},
                ),
            )

            def execute(self, params):
                raise AssertionError("not reached")

            def effective_resource_scopes(self, arguments, write_boundary, workspace_root):
                raise RuntimeError("hook boom")

        policy = _BrokenHookTool().runtime_policy
        runtime = SimpleNamespace(runtime_policy=policy, handler=_BrokenHookTool())
        request = SimpleNamespace(workspace_root=Path("/tmp/scope-fail"), write_boundary=None)
        call = SimpleNamespace(arguments={"run_id": "r-1"})
        with pytest.raises(ResourceScopeResolutionError):
            _workspace_operation_scopes(request, call, runtime)

    def test_executor_entry_mutating_hook_failure_fails_closed(self):
        # 反例 7b（seq 274/276 补强，executor 入口级）：mutating 路径 hook 抛错
        # 必须产出 TOOL_RESOURCE_SCOPE_RESOLUTION_FAILED + handler_executed=False，
        # handler 零执行、零副作用——仅函数级断言（_workspace_operation_scopes
        # 抛异常）不能证明执行器输出与零执行，入口级才闭环。
        from pathlib import Path
        from types import SimpleNamespace

        from agent_py_agent.agent.tooling.action_policy import ActionDecision
        from agent_py_agent.agent.tooling.executor import _invoke_with_operation_policy
        from agent_py_agent.agent.tooling.models import (
            BaseTool,
            EffectResolverPolicy,
            ResourceScopePolicy,
            ToolFailureStage,
            ToolRuntimePolicy,
        )

        class _BrokenHookTool(BaseTool):
            runtime_policy = ToolRuntimePolicy(
                effect_resolver=EffectResolverPolicy("mutating"),
                resource_scopes=ResourceScopePolicy(
                    parameter_names=("run_id",),
                    parameter_kinds={"run_id": "logical"},
                ),
            )

            def __init__(self):
                self.hook_calls = 0
                self.handler_calls = 0

            def execute(self, params):
                self.handler_calls += 1
                raise AssertionError("handler must not run")

            def effective_resource_scopes(self, arguments, write_boundary, workspace_root):
                self.hook_calls += 1
                raise RuntimeError("hook boom")

        tool = _BrokenHookTool()
        request = SimpleNamespace(
            workspace_root=Path("/tmp/scope-fail-entry"),
            write_boundary=None,
            operation_store=SimpleNamespace(),  # 无 require_authority → 无权威门，直达 scope 解析
            operation_store_required=False,
            operation_owner_id="owner-1",
        )
        call = SimpleNamespace(
            tool_name="broken_hook",
            run_id="r-1",
            task_id="",
            operation_id="op-1",
            args_hash="h",
            idempotency_key="",
            attempt_id="a-1",
            arguments={"run_id": "r-1"},
        )
        outcome = _invoke_with_operation_policy(
            request,
            call,
            SimpleNamespace(runtime_policy=tool.runtime_policy, handler=tool),
            ActionDecision("allow", resolved_effect="mutating"),
        )
        assert outcome.ok is False
        assert outcome.error_code == "TOOL_RESOURCE_SCOPE_RESOLUTION_FAILED"
        assert outcome.handler_executed is False
        assert outcome.failure_stage == ToolFailureStage.RUNTIME_GATE.value
        assert tool.hook_calls == 1
        assert tool.handler_calls == 0, "fail-closed：handler 必须零执行"

    def test_executor_entry_read_only_bypasses_resource_scope_resolution(self):
        # 反例 7c（seq 274/276 补强）：同一坏 hook 在 read_only 路径不取资源锁
        # （_workspace_operation_scopes 不被调用 → hook 计数恒 0，若被调用必抛错
        # 测试即崩），handler 正常执行——read_only 无副作用不取锁，不被 hook
        # 实现缺陷牵连。
        from pathlib import Path
        from types import SimpleNamespace

        from agent_py_agent.agent.tooling.action_policy import ActionDecision
        from agent_py_agent.agent.tooling.executor import _invoke_with_operation_policy
        from agent_py_agent.agent.tooling.models import (
            BaseTool,
            EffectResolverPolicy,
            ResourceScopePolicy,
            ToolHandlerOutcome,
            ToolRuntimePolicy,
        )

        tool_name = "broken_hook_ro"

        class _BrokenHookReadOnlyTool(BaseTool):
            runtime_policy = ToolRuntimePolicy(
                effect_resolver=EffectResolverPolicy("read_only"),
                resource_scopes=ResourceScopePolicy(
                    parameter_names=("run_id",),
                    parameter_kinds={"run_id": "logical"},
                ),
            )

            def __init__(self):
                self.hook_calls = 0
                self.handler_calls = 0

            def execute(self, params):
                self.handler_calls += 1
                return ToolHandlerOutcome(tool_name, True, "ok")

            def effective_resource_scopes(self, arguments, write_boundary, workspace_root):
                self.hook_calls += 1
                raise RuntimeError("hook boom")

        tool = _BrokenHookReadOnlyTool()
        snapshot_runtime = SimpleNamespace(
            model_spec=SimpleNamespace(name=tool_name),
            runtime_policy=tool.runtime_policy,
            handler=tool,
        )
        snapshot = SimpleNamespace(
            runtimes=(snapshot_runtime,),
            runtime=lambda name: (
                snapshot_runtime if str(name or "").strip() == tool_name else None
            ),
            available_tool_names=frozenset({tool_name}),
        )
        request = SimpleNamespace(
            workspace_root=Path("/tmp/scope-ro-entry"),
            workspace_roots=[Path("/tmp/scope-ro-entry")],
            write_boundary=None,
            operation_store=SimpleNamespace(),
            operation_store_required=False,
            operation_owner_id="owner-1",
            runtime_snapshot=snapshot,
            path_access_mode="normal",
            path_dangerous_roots=(),
            owner_type="main_agent",
            cancellation_token=SimpleNamespace(cancelled=False),
        )
        call = SimpleNamespace(
            tool_name=tool_name,
            run_id="r-1",
            task_id="",
            operation_id="op-1",
            args_hash="h",
            idempotency_key="",
            attempt_id="a-1",
            call_id="c-1",
            source_protocol="test",
            arguments={"run_id": "r-1"},
        )
        outcome = _invoke_with_operation_policy(
            request,
            call,
            SimpleNamespace(runtime_policy=tool.runtime_policy, handler=tool),
            ActionDecision("allow", resolved_effect="read_only"),
        )
        assert outcome.ok is True
        assert outcome.handler_executed is True
        assert tool.hook_calls == 0, "read_only 不取资源锁：hook 必须零调用"
        assert tool.handler_calls == 1
