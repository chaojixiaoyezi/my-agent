"""task_progress coverage ledger tests."""

from __future__ import annotations

import json


class TestTaskProgressCoverageTool:
    """测试 task_progress 的通用覆盖账本。"""

    def test_updates_and_summarizes_open_coverage_ledger(self, tmp_path):
        """覆盖账本应复用 task_progress，不把“项目/论文/API”等对象写死。"""
        from agent_py_agent.agent.agent_core.task_progress_tool import TaskProgressTool
        from agent_py_agent.agent.config import AgentConfig
        from agent_py_agent.agent.core import SimpleAgent

        agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
        agent._main_agent_run_id = "run-main"
        tool = TaskProgressTool(agent)

        tool.execute(
            {
                "action": "update",
                "summary": "正在覆盖多个对象。",
                "coverage": {
                    "goal": "每个对象都要读基础信息、分析结构、写进报告。",
                    "dimensions": ["读基础信息", "分析结构", "写进报告"],
                    "targets": [
                        {
                            "id": "agentscope-main",
                            "title": "AgentScope",
                            "status": "done",
                            "checks": {"读基础信息": "done", "分析结构": "done", "写进报告": "done"},
                            "evidence": ["agentscope-main/README.md"],
                        },
                        {
                            "id": "codex-main",
                            "title": "Codex",
                            "status": "in_progress",
                            "checks": {"读基础信息": "done", "分析结构": "pending", "写进报告": "pending"},
                            "next": "继续看核心目录",
                        },
                    ],
                },
            }
        )
        payload = json.loads(tool.execute({"action": "read"}).output)

        assert payload["coverage"]["goal"] == "每个对象都要读基础信息、分析结构、写进报告。"
        assert payload["coverage"]["counts"]["targets_total"] == 2
        assert payload["coverage"]["counts"]["targets_done"] == 1
        assert payload["coverage"]["counts"]["checks_done"] == 4
        assert payload["coverage"]["targets"][1]["checks"]["分析结构"] == "pending"
        assert payload["coverage"]["targets"][1]["next"] == "继续看核心目录"

    def test_coverage_ledger_merges_by_target_and_check(self, tmp_path):
        """覆盖账本跨轮更新同一对象时，只补新状态，不丢已有证据。"""
        from agent_py_agent.agent.task_progress import read_task_progress, write_task_progress

        write_task_progress(
            tmp_path,
            "run-main",
            {
                "coverage_targets": [
                    {
                        "id": "codex-main",
                        "title": "Codex",
                        "checks": {"读基础信息": "done", "分析结构": "pending"},
                        "evidence": ["codex-main/README.md"],
                    }
                ]
            },
        )
        write_task_progress(
            tmp_path,
            "run-main",
            {
                "coverage_targets": [
                    {
                        "id": "codex-main",
                        "checks": {"分析结构": "done", "写进报告": "done"},
                        "evidence": ["codex-main/README.md", "codex-main/core"],
                    }
                ]
            },
        )

        payload = read_task_progress(tmp_path, "run-main")

        target = payload["coverage"]["targets"][0]
        assert target["checks"] == {"读基础信息": "done", "分析结构": "done", "写进报告": "done"}
        assert target["evidence"] == ["codex-main/README.md", "codex-main/core"]
        assert payload["coverage"]["counts"]["targets_done"] == 1

    def test_coverage_targets_accept_expected_fields_as_pending_checks(self, tmp_path):
        """模型用 expected_fields 表达覆盖项时，也应归一成 checks。"""
        from agent_py_agent.agent.task_progress import read_task_progress, write_task_progress

        write_task_progress(
            tmp_path,
            "run-main",
            {
                "coverage_targets": [
                    {
                        "id": "codex-main",
                        "expected_fields": ["做什么的", "主要模块", "优点", "缺点", "值得借鉴的地方"],
                    }
                ]
            },
        )

        target = read_task_progress(tmp_path, "run-main")["coverage"]["targets"][0]

        assert target["checks"] == {
            "做什么的": "pending",
            "主要模块": "pending",
            "优点": "pending",
            "缺点": "pending",
            "值得借鉴的地方": "pending",
        }

    def test_task_progress_accepts_create_action_and_fields_alias(self, tmp_path):
        """真实模型常写 action=create 和 fields，工具应宽容成 update + checks。"""
        from agent_py_agent.agent.config import AgentConfig
        from agent_py_agent.agent.core import SimpleAgent

        agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
        agent._main_agent_run_id = "run-main"

        result = agent.tools.execute_call(
            {
                "tool": "task_progress",
                "action": "create",
                "summary": "开始覆盖五个项目。",
                "items": [
                    {
                        "id": "agentscope",
                        "title": "agentscope-main分析",
                        "status": "in_progress",
                        "fields": ["功能", "主要模块", "优点", "缺点", "借鉴点"],
                    }
                ],
            }
        )
        payload = json.loads(result.output)

        assert result.ok is True
        assert payload["summary"] == "开始覆盖五个项目。"
        assert payload["coverage"]["targets"][0]["checks"]["功能"] == "pending"

    def test_task_progress_accepts_fields_dict_as_checks(self, tmp_path):
        """模型把 fields 写成字段到状态的字典时，应直接当 checks。"""
        from agent_py_agent.agent.config import AgentConfig
        from agent_py_agent.agent.core import SimpleAgent

        agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
        agent._main_agent_run_id = "run-main"

        result = agent.tools.execute_call(
            {
                "tool": "task_progress",
                "action": "update",
                "items": [
                    {
                        "id": "agentscope",
                        "fields": {"功能描述": "pending", "主要模块": "pending", "优点": "done"},
                    }
                ],
            }
        )
        payload = json.loads(result.output)

        assert result.ok is True
        assert payload["coverage"]["targets"][0]["checks"] == {
            "功能描述": "pending",
            "主要模块": "pending",
            "优点": "done",
        }


class TestTaskProgressCoverageAliases:
    """测试模型常见的 coverage 自然写法。"""

    def test_task_progress_accepts_init_and_string_coverage_targets(self, tmp_path):
        """模型用 init 和字符串覆盖清单时，也应写成结构化 coverage。"""
        from agent_py_agent.agent.config import AgentConfig
        from agent_py_agent.agent.core import SimpleAgent

        agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
        agent._main_agent_run_id = "run-main"

        result = agent.tools.execute_call(
            {
                "tool": "task_progress",
                "action": "init",
                "summary": "开始分析五个项目。",
                "coverage_targets": [
                    "agentscope-main:功能定位,主要模块,优点,缺点,借鉴点",
                    "codex-main:功能定位,主要模块,优点,缺点,借鉴点",
                ],
            }
        )
        payload = json.loads(result.output)

        assert result.ok is True
        assert payload["summary"] == "开始分析五个项目。"
        assert payload["coverage"]["targets"][0]["id"] == "agentscope-main"
        assert payload["coverage"]["targets"][0]["checks"]["主要模块"] == "pending"
        assert payload["coverage"]["counts"]["targets_total"] == 2

    def test_task_progress_accepts_fields_needed_and_chinese_target_text(self, tmp_path):
        """模型用 fields_needed 或中文冒号写覆盖项时，也应归一成 checks。"""
        from agent_py_agent.agent.config import AgentConfig
        from agent_py_agent.agent.core import SimpleAgent

        agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
        agent._main_agent_run_id = "run-main"

        result = agent.tools.execute_call(
            {
                "tool": "task_progress",
                "action": "begin",
                "summary": "开始覆盖多个对象。",
                "items": [
                    {
                        "id": "free-code-main",
                        "title": "free-code-main",
                        "fields_needed": ["功能定位", "主要模块", "借鉴点"],
                    }
                ],
                "coverage_targets": [
                    "hermes-agent-main：功能定位，主要模块，优点，缺点，借鉴点",
                ],
            }
        )
        payload = json.loads(result.output)

        assert result.ok is True
        by_id = {target["id"]: target for target in payload["coverage"]["targets"]}
        assert by_id["free-code-main"]["checks"]["借鉴点"] == "pending"
        assert by_id["hermes-agent-main"]["checks"]["主要模块"] == "pending"
        assert payload["coverage"]["counts"]["targets_total"] == 2

    def test_task_progress_derives_checks_from_note_text(self, tmp_path):
        """模型把覆盖要求写在 note/notes 里时，也应保留下来做覆盖清单。"""
        from agent_py_agent.agent.config import AgentConfig
        from agent_py_agent.agent.core import SimpleAgent

        agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
        agent._main_agent_run_id = "run-main"

        result = agent.tools.execute_call(
            {
                "tool": "task_progress",
                "action": "update",
                "items": [
                    {
                        "id": "agentscope-main",
                        "title": "分析 agentscope-main",
                        "status": "in_progress",
                        "note": "待分析：做什么、主要模块、优点、缺点、借鉴点",
                    }
                ],
            }
        )
        payload = json.loads(result.output)

        assert result.ok is True
        item = payload["items"][0]
        assert item["notes"] == "待分析：做什么、主要模块、优点、缺点、借鉴点"
        target = payload["coverage"]["targets"][0]
        assert target["id"] == "agentscope-main"
        assert target["checks"]["主要模块"] == "pending"

    def test_task_progress_accepts_name_and_missing_fields_aliases(self, tmp_path):
        """模型用 name/missing_fields 和字符串 coverage 时，不应把多个对象合成一个 target。"""
        from agent_py_agent.agent.config import AgentConfig
        from agent_py_agent.agent.core import SimpleAgent

        agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
        agent._main_agent_run_id = "run-main"

        result = agent.tools.execute_call(
            {
                "tool": "task_progress",
                "action": "create",
                "coverage": "0/2 项目已分析",
                "coverage_targets": [
                    {"name": "agentscope-main", "status": "pending", "missing_fields": ["主要模块", "借鉴点"]},
                    {"name": "codex-main", "status": "pending", "missing_fields": []},
                ],
            }
        )
        payload = json.loads(result.output)

        assert result.ok is True
        assert payload["coverage"]["goal"] == "0/2 项目已分析"
        assert [target["id"] for target in payload["coverage"]["targets"]] == [
            "agentscope-main",
            "codex-main",
        ]
        assert payload["coverage"]["targets"][0]["checks"]["借鉴点"] == "pending"
