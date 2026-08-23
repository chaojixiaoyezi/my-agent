"""Focused tests for create_subagents items batch mode."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock


def _agent(max_subagents: int = 10) -> MagicMock:
    mock_agent = MagicMock()
    mock_agent.config.enable_subagents = True
    mock_agent.config.max_subagents = max_subagents
    # 普通 helper 明确表示“当前没有 canonical 计划”；不能让 MagicMock 的
    # 动态 root/home_paths 偶然解析到工作区或 /tmp 中其它测试留下的账本。
    mock_agent.home_paths = None
    mock_agent.root = None
    mock_agent._current_run_params = None
    mock_agent._current_subagent_run_id = ""
    mock_agent.subagents.workspace = Path("/tmp/subs")
    mock_agent.subagents.list_runs.return_value = []
    return mock_agent


def _create_run_sequence():
    created_count = 0

    def create_run(*, params):
        nonlocal created_count
        created_count += 1
        task = MagicMock()
        task.id = f"run_{created_count}"
        task.goal = params.goal
        task.status = "PLANNING"
        task.verification_status = "UNVERIFIED"
        task.task_dir = f"/tmp/{task.id}"
        return task

    return create_run


def _planned_agent(tmp_path) -> MagicMock:
    workspace = tmp_path / "project"
    workspace.mkdir()
    mock_agent = _agent()
    mock_agent.home_paths = None
    mock_agent.root = tmp_path / "owner-home"
    mock_agent._current_run_params = SimpleNamespace(
        run_id="plan-root",
        task_id="plan-root",
        context_scope="default",
        task_attributes={
            "conversation_execution_cwd": str(workspace),
            "conversation_runtime_workspace_roots": [str(workspace)],
        },
    )
    mock_agent.subagents.workspace_root = workspace
    mock_agent.subagents.workspace_roots = [workspace]
    mock_agent.subagents.role_template_dirs = []
    mock_agent.subagents.list_runs.return_value = []
    mock_agent.subagents.create_run.side_effect = _create_run_sequence()
    return mock_agent


def _seed_plan(agent: MagicMock, items: list[dict[str, str]]) -> None:
    from agent_py_agent.agent.task_progress import write_task_progress

    write_task_progress(agent.root, "plan-root", {"items": items})


class TestCreateSubagentsItemsMode:
    """测试 create_subagents 的 items 结构化批量入口。"""

    def test_items_create_distinct_goals_with_batch_goal(self):
        """items[] 批量模式应创建不同目标，不能复制同一个 goal。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _agent()
        mock_agent.subagents.create_run.side_effect = _create_run_sequence()

        result = CreateSubagentsTool(mock_agent).execute({
            "goal": "并行研究市场并形成进入策略",
            "items": [
                {"goal": "研究越南市场环境", "description": "调研越南市场", "role": "worker", "agent_name": "小傻妞-市场"},
                {"goal": "研究竞争格局", "description": "分析竞争格局", "role": "worker", "agent_name": "小傻妞-竞争"},
                {"goal": "制定进入策略", "description": "制定进入策略", "role": "coordinator", "agent_name": "小傻妞-策略"},
            ],
            "acceptance_checks": ["必须有证据", "必须标注未确认信息"],
        })

        created_params = [
            call.kwargs["params"] for call in mock_agent.subagents.create_run.call_args_list
        ]
        payload = json.loads(result.output)

        assert result.ok is True
        assert [params.goal for params in created_params] == [
            "研究越南市场环境",
            "研究竞争格局",
            "制定进入策略",
        ]
        assert [params.agent_name for params in created_params] == [
            "小傻妞-市场-1",
            "小傻妞-竞争-2",
            "小傻妞-策略-3",
        ]
        assert [params.description for params in created_params] == [
            "调研越南市场",
            "分析竞争格局",
            "制定进入策略",
        ]
        assert payload["created"] == 3
        assert payload["auto_start"]["status"] == "started"
        assert payload["auto_start"]["run_ids"] == ["run_1", "run_2", "run_3"]
        assert payload["next_action"]["action"] == "await_lifecycle_event"

    def test_batch_description_does_not_replace_each_child_duty(self):
        """顶层批次说明不能扇出成所有 child 相同的 TUI 职责短标题。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _agent()
        mock_agent.subagents.create_run.side_effect = _create_run_sequence()

        result = CreateSubagentsTool(mock_agent).execute({
            "goal": "并行完成游戏模块",
            "description": "超级玛丽游戏并行开发",
            "items": [
                {"goal": "开发游戏核心引擎"},
                {"goal": "设计前三个关卡", "description": "设计前三个关卡"},
            ],
        })

        created_params = [
            call.kwargs["params"] for call in mock_agent.subagents.create_run.call_args_list
        ]
        assert result.ok is True
        assert [params.description for params in created_params] == [
            "",
            "设计前三个关卡",
        ]

    def test_items_are_capped_by_max_subagents(self):
        """items[] 超过容量时整批拒绝，避免无声丢失一部分任务。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _agent(max_subagents=2)
        mock_agent.subagents.create_run.side_effect = _create_run_sequence()

        result = CreateSubagentsTool(mock_agent).execute({
            "goal": "并行研究三个市场维度",
            "items": [
                {"goal": "市场"},
                {"goal": "竞争"},
                {"goal": "策略"},
            ],
        })

        assert result.ok is False
        assert result.reported_error_code == "SUBAGENT_CAPACITY_EXCEEDED"
        assert mock_agent.subagents.create_run.call_count == 0

    def test_duplicate_items_reject_whole_batch(self):
        """同一结构化任务不得在一个批次里复制给多个子代理。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _agent()
        mock_agent.subagents.create_run.side_effect = _create_run_sequence()

        result = CreateSubagentsTool(mock_agent).execute({
            "goal": "并行读取资料并形成证据报告",
            "items": [
                {
                    "goal": "读取 README.md 并写证据报告",
                    "role": "worker",
                    "tool_preset": "coding",
                },
                {
                    "goal": "读取 README.md 并写证据报告",
                    "role": "worker",
                    "tool_preset": "coding",
                },
            ],
        })

        assert result.ok is False
        assert result.reported_error_code == "TOOL_INVALID_ARGUMENTS"
        assert "重复" in result.output
        assert mock_agent.subagents.create_run.call_count == 0

    def test_same_goal_with_distinct_structured_output_boundaries_is_allowed(self):
        """相同文案不是唯一机器事实；不同结构化交付边界代表不同工作。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _agent()
        mock_agent.subagents.create_run.side_effect = _create_run_sequence()

        result = CreateSubagentsTool(mock_agent).execute({
            "goal": "并行实现两个模块",
            "items": [
                {"goal": "实现指定模块", "output_files": ["src/a.py"]},
                {"goal": "实现指定模块", "output_files": ["src/b.py"]},
            ],
        })

        assert result.ok is True
        assert mock_agent.subagents.create_run.call_count == 2

    def test_items_limit_uses_agent_config_default_when_config_field_missing(self):
        """轻量配置对象缺少 max_subagents 时，也使用 AgentConfig 默认容量。"""
        from types import SimpleNamespace

        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
        from agent_py_agent.agent.settings import AgentConfig

        mock_agent = MagicMock()
        mock_agent.config = SimpleNamespace(enable_subagents=True)
        result = CreateSubagentsTool(mock_agent).execute(
            {"goal": "并行执行一批任务", "items": [{"goal": f"任务 {index}"} for index in range(60)]}
        )

        assert result.ok is False
        assert result.reported_error_code == "SUBAGENT_CAPACITY_EXCEEDED"
        assert str(AgentConfig().max_subagents) in result.output
        assert mock_agent.subagents.create_run.call_count == 0

    def test_items_validate_before_creating_any_run(self, monkeypatch):
        """某个 item 失败时不应留下半创建的子代理记录。"""
        from agent_py_agent.agent.agent_core import orchestration_tools
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        def fake_target_error(request):
            return "second item invalid" if request.params.get("extra_write_roots") == ["/bad"] else ""

        monkeypatch.setattr(orchestration_tools, "external_write_target_error", fake_target_error)
        mock_agent = _agent()
        result = CreateSubagentsTool(mock_agent).execute({
            "goal": "并行研究市场与竞争",
            "items": [{"goal": "市场"}, {"goal": "竞争", "extra_write_roots": ["/bad"]}],
        })

        assert result.ok is False
        assert "second item invalid" in result.output
        assert mock_agent.subagents.create_run.call_count == 0

    def test_items_with_blank_goal_reject_whole_batch_with_repairable_error(self):
        """真机 0/22 根因#1 钉子:复杂指令下某个 item 的 goal 空/全空白,必须整批报可修
        错误让模型自纠,一个空壳子代理都不能落地(空壳会 Context Gate BLOCKED 拖死收尾)。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        for bad_goal in ("", "   ", None):
            mock_agent = _agent()
            result = CreateSubagentsTool(mock_agent).execute({
                "goal": "并行实现前后端",
                "items": [{"goal": "写后端"}, {"goal": bad_goal, "role": "frontend"}],
            })

            assert result.ok is False
            assert result.error_code == "TOOL_INVALID_ARGUMENTS"
            assert "items[1]" in result.output and "goal" in result.output
            assert mock_agent.subagents.create_run.call_count == 0

    def test_items_do_not_inherit_global_plan_but_keep_nested_children(self):
        """items[] 不应把顶层全局计划误当成每个 child 自己的计划。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _agent()
        mock_agent.subagents.create_run.side_effect = _create_run_sequence()

        result = CreateSubagentsTool(mock_agent).execute({
            "goal": "研究市场并保留子级计划",
            "plan": "主代理最后输出 final_report.md",
            "items": [
                {
                    "goal": "研究市场环境",
                    "children": [{"goal": "分析越南市场", "agent_name": "小小傻妞-越南"}],
                }
            ],
        })

        params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert result.ok is True
        assert not any("final_report.md" in item for item in params.plan)
        assert any("分析越南市场" in item for item in params.plan)

    def test_items_preserve_refs_first_context_manifest(self):
        """items[] 应把资料路径作为 refs 传给子代理，而不是要求 root 先读正文。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _agent()
        mock_agent.subagents.create_run.side_effect = _create_run_sequence()

        result = CreateSubagentsTool(mock_agent).execute({
            "goal": "分析市场资料",
            "items": [
                {
                    "goal": "分析市场资料",
                    "required_read_paths": ["data/market.md", "rubric.md"],
                    "context_manifest": {"required_read_paths": ["README.md"]},
                    "context_packs": [{"kind": "brief", "summary": "评分标准", "path": "rubric.md"}],
                }
            ],
        })

        params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert result.ok is True
        assert params.context_manifest["required_read_paths"] == [
            "README.md",
            "data/market.md",
            "rubric.md",
        ]
        assert params.context_packs == [{"kind": "brief", "summary": "评分标准", "path": "rubric.md"}]

    def test_single_goal_preserves_source_refs_and_context_pack_refs(self):
        """单任务模式也应保留 source_refs/context_pack_refs，方便小傻妞按路径读取资料。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _agent()
        mock_task = MagicMock()
        mock_task.id = "run_1"
        mock_task.goal = "研究资料"
        mock_task.status = "PLANNING"
        mock_task.verification_status = "UNVERIFIED"
        mock_task.task_dir = "/tmp/run_1"
        mock_agent.subagents.create_run.return_value = mock_task

        result = CreateSubagentsTool(mock_agent).execute({
            "goal": "研究资料并写摘要",
            "source_refs": ["docs/a.md"],
            "context_pack_refs": ["packs/brief.json"],
        })

        params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert result.ok is True
        assert params.context_manifest["required_read_paths"] == ["docs/a.md"]
        assert params.context_manifest["task_pack_refs"] == ["packs/brief.json"]
        assert params.context_packs == [{"kind": "context_ref", "path": "packs/brief.json"}]

    def test_tasks_alias_is_rejected(self):
        """create_subagents 不再接受 tasks 批量别名，避免模型看到两套入口。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _agent()
        result = CreateSubagentsTool(mock_agent).execute({
            "goal": "整合市场资料",
            "tasks": [{"goal": "整合市场", "role": "coordinator"}],
        })

        assert result.ok is False
        assert "只接受 items" in result.output
        mock_agent.subagents.create_run.assert_not_called()

    def test_create_subagents_keeps_grandchild_like_names_as_display_text(self):
        """名字里的小小傻妞/grandchild 只是展示文本，不应变成 create 阶段硬拒。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _agent()
        mock_agent.subagents.create_run.side_effect = _create_run_sequence()
        result = CreateSubagentsTool(mock_agent).execute({
            "goal": "分析印尼市场",
            "items": [{
                "goal": "分析印尼市场",
                "role": "grandworker",
                "agent_name": "小小傻妞-印尼市场",
            }],
        })

        params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert result.ok is True
        assert params.agent_name == "小小傻妞-印尼市场"

    def test_existing_plan_rejects_unbound_batch_before_any_child_is_created(self, tmp_path):
        """已有 Todo 时漏 covers 必须整批 not_started，不能先落四个孤立 child。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _planned_agent(tmp_path)
        _seed_plan(
            mock_agent,
            [
                {"id": "impl-core", "title": "实现核心", "status": "pending"},
                {"id": "impl-ui", "title": "实现界面", "status": "pending"},
            ],
        )

        result = CreateSubagentsTool(mock_agent).execute({
            "goal": "并行完成现有计划",
            "items": [
                {"goal": "实现核心", "output_files": ["port/core/"]},
                {"goal": "实现界面", "output_files": ["port/ui/"]},
            ],
        })
        payload = json.loads(result.output)

        assert result.ok is False
        assert result.error_code == "SUBAGENT_PLANNED_DELEGATION_INVALID"
        assert result.reported_error_code == "SUBAGENT_PLANNED_DELEGATION_INVALID"
        assert result.effect_outcome == "not_started"
        assert result.retryable is True
        assert result.recommended_action == "repair_tool_arguments"
        assert payload["planned_dispatch"]["missing_covers_indexes"] == [0, 1]
        assert mock_agent.subagents.create_run.call_count == 0

    def test_existing_plan_rejects_coding_item_without_output_write_set(self, tmp_path):
        """计划内直接编码 item 必须声明结构化 output_files，goal 里的目录不算。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _planned_agent(tmp_path)
        _seed_plan(
            mock_agent,
            [{"id": "impl-core", "title": "实现核心", "status": "pending"}],
        )

        result = CreateSubagentsTool(mock_agent).execute({
            "goal": "完成核心",
            "items": [{"goal": "在兄弟目录实现核心", "covers": ["impl-core"]}],
        })
        payload = json.loads(result.output)

        assert result.ok is False
        assert payload["missing_output_files_indexes"] == [0]
        assert mock_agent.subagents.create_run.call_count == 0

    def test_existing_plan_rejects_sibling_output_path_before_child_creation(self, tmp_path):
        """output_files 逃出当前 workspace 时整批拒绝，不能留给 child 再申请权限。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _planned_agent(tmp_path)
        _seed_plan(
            mock_agent,
            [{"id": "impl-core", "title": "实现核心", "status": "pending"}],
        )

        result = CreateSubagentsTool(mock_agent).execute({
            "goal": "完成核心",
            "items": [{
                "goal": "实现核心",
                "covers": ["impl-core"],
                "output_files": ["../sibling-port/core/"],
            }],
        })
        payload = json.loads(result.output)

        assert result.ok is False
        assert payload["invalid_output_files"][0]["issues"][0]["reason"] == "outside_parent_workspace"
        assert mock_agent.subagents.create_run.call_count == 0

    def test_existing_plan_accepts_exact_covers_and_disjoint_workspace_outputs(self, tmp_path):
        """exact covers 与 workspace 内互斥写入集合齐全时，保持原自动创建/启动主链。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _planned_agent(tmp_path)
        _seed_plan(
            mock_agent,
            [
                {"id": "impl-core", "title": "实现核心", "status": "pending"},
                {"id": "impl-ui", "title": "实现界面", "status": "pending"},
            ],
        )

        result = CreateSubagentsTool(mock_agent).execute({
            "goal": "并行完成现有计划",
            "items": [
                {
                    "goal": "实现核心",
                    "covers": ["impl-core"],
                    "output_files": ["port/core/"],
                },
                {
                    "goal": "实现界面",
                    "covers": ["impl-ui"],
                    "output_files": ["port/ui/"],
                },
            ],
        })

        assert result.ok is True
        assert mock_agent.subagents.create_run.call_count == 2
        created = [call.kwargs["params"] for call in mock_agent.subagents.create_run.call_args_list]
        assert created[0].attributes["covers"] == ["impl-core"]
        assert created[1].attributes["covers"] == ["impl-ui"]

    def test_existing_plan_rejects_unbound_single_before_child_creation(self, tmp_path):
        """单 child 入口也不能绕过计划绑定合同。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _planned_agent(tmp_path)
        _seed_plan(
            mock_agent,
            [{"id": "impl-core", "title": "实现核心", "status": "pending"}],
        )

        result = CreateSubagentsTool(mock_agent).execute({
            "goal": "实现核心",
            "output_files": ["port/core/"],
        })

        assert result.ok is False
        assert result.error_code == "SUBAGENT_PLANNED_DELEGATION_INVALID"
        assert result.reported_error_code == "SUBAGENT_PLANNED_DELEGATION_INVALID"
        assert json.loads(result.output)["planned_dispatch"]["missing_covers_indexes"] == [0]
        assert mock_agent.subagents.create_run.call_count == 0

    def test_existing_plan_accepts_bound_single_inside_workspace(self, tmp_path):
        """单 child 带 exact covers 与 workspace 写入集合时保持自动启动。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _planned_agent(tmp_path)
        _seed_plan(
            mock_agent,
            [{"id": "impl-core", "title": "实现核心", "status": "pending"}],
        )

        result = CreateSubagentsTool(mock_agent).execute({
            "goal": "实现核心",
            "covers": ["impl-core"],
            "output_files": ["port/core/"],
        })

        assert result.ok is True
        created = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert created.attributes["covers"] == ["impl-core"]

    def test_nested_create_uses_same_planned_delegation_preflight(self, tmp_path):
        """子代理派孙代理也必须先绑定主计划，不能从递归入口绕过原子合同。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import _nested_create_params
        from agent_py_agent.agent.tooling.models import ToolHandlerOutcome

        mock_agent = _planned_agent(tmp_path)
        mock_agent._current_subagent_run_id = "parent-child"
        _seed_plan(
            mock_agent,
            [{"id": "impl-core", "title": "实现核心", "status": "pending"}],
        )

        result = _nested_create_params(
            mock_agent,
            {
                "goal": "派孙代理实现核心",
                "items": [{"goal": "实现核心", "output_files": ["port/core/"]}],
            },
        )

        assert isinstance(result, ToolHandlerOutcome)
        assert result.reported_error_code == "SUBAGENT_PLANNED_DELEGATION_INVALID"
        assert json.loads(result.output)["planned_dispatch"]["ledger_run_id"] == "plan-root"


class TestCreateSubagentsToolGrantProtocol:
    """测试 create_subagents 对模型少填工具和协议漂移的兜底。"""

    def test_partial_explicit_allowed_tools_keep_baseline_write_tools(self):
        """模型只填 read_file/list_files 时，系统仍补齐基础读写工具，避免子代理变哑巴。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _agent()
        mock_task = MagicMock()
        mock_task.id = "worker_001"
        mock_task.goal = ""
        mock_task.status = "PLANNING"
        mock_task.verification_status = "UNVERIFIED"
        mock_task.task_dir = "/tmp/worker_001"
        mock_agent.subagents.create_run.return_value = mock_task

        result = CreateSubagentsTool(mock_agent).execute({
            "goal": "研究资料并写 output.json",
            "role": "worker",
            "allowed_tools": ["read_file", "list_files"],
        })

        params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert result.ok is True
        assert "read_file" in params.allowed_tools
        assert "write_file" in params.allowed_tools
        assert "apply_patch" in params.allowed_tools
        assert "apply_patch" in params.allowed_tools


def test_empty_items_list_falls_through_to_single_goal():
    """模型常反射性带一个空 items:[] 同时把规格放顶层 goal——应落单 goal 模式建出 1 个子代理,
    而不是 TOOL_INVALID_ARGUMENTS(B1 真机暴露:14 次 create_subagents 因此全失败、子代理一个没派出)。"""
    import json

    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    mock_agent = _agent()
    mock_agent.subagents.create_run.side_effect = _create_run_sequence()

    result = CreateSubagentsTool(mock_agent).execute({
        "goal": "创建企业级协作平台的基础目录结构与入口文件",
        "items": [],
        "output_files": ["platform/__init__.py", "platform/config.py"],
        "defer_start": False,
    })
    payload = json.loads(result.output)

    assert result.ok is True
    assert payload["created"] == 1
    assert len(payload["created_run_ids"]) == 1
