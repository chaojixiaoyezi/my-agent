"""task_progress coverage ledger tests."""

from __future__ import annotations

import json
from types import SimpleNamespace


class TestTaskProgressCoverageTool:
    """测试 task_progress 的通用覆盖账本。"""

    def test_updates_and_summarizes_open_coverage_ledger(self, tmp_path):
        """覆盖账本应复用 task_progress，不把“项目/论文/API”等对象写死。"""
        from agent_py_agent.agent.agent_core.task_progress_tool import TaskProgressTool
        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig

        agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
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
                "coverage": {
                    "targets": [
                        {
                            "id": "codex-main",
                            "title": "Codex",
                            "checks": {"读基础信息": "done", "分析结构": "pending"},
                            "evidence": ["codex-main/README.md"],
                        }
                    ]
                }
            },
        )
        write_task_progress(
            tmp_path,
            "run-main",
            {
                "coverage": {
                    "targets": [
                        {
                            "id": "codex-main",
                            "checks": {"分析结构": "done", "写进报告": "done"},
                            "evidence": ["codex-main/README.md", "codex-main/core"],
                        }
                    ]
                }
            },
        )

        payload = read_task_progress(tmp_path, "run-main")

        target = payload["coverage"]["targets"][0]
        assert target["checks"] == {"读基础信息": "done", "分析结构": "done", "写进报告": "done"}
        assert target["evidence"] == ["codex-main/README.md", "codex-main/core"]
        assert payload["coverage"]["counts"]["targets_done"] == 1

    def test_coverage_ledger_does_not_shrink_when_later_update_has_subset(self, tmp_path):
        """compact 后模型只记得一部分覆盖目标时，账本不能静默丢掉剩余目标。"""
        from agent_py_agent.agent.task_progress import read_task_progress, write_task_progress

        write_task_progress(
            tmp_path,
            "run-main",
            {
                "coverage": {
                    "targets": [
                        {"id": "fragment-001", "checks": {"读文件": "done"}, "evidence": ["fragment-001.txt"]},
                        {"id": "fragment-002", "checks": {"读文件": "pending"}},
                        {"id": "fragment-003", "checks": {"读文件": "pending"}},
                    ]
                }
            },
        )
        write_task_progress(
            tmp_path,
            "run-main",
            {
                "summary": "误以为只有一个文件。",
                "coverage": {
                    "targets": [
                        {"id": "fragment-001", "checks": {"读文件": "done"}, "evidence": ["fragment-001.txt"]}
                    ]
                },
                "next_action": "提交验收",
            },
        )

        payload = read_task_progress(tmp_path, "run-main")

        assert [target["id"] for target in payload["coverage"]["targets"]] == [
            "fragment-001",
            "fragment-002",
            "fragment-003",
        ]
        assert payload["coverage"]["counts"]["targets_total"] == 3
        assert payload["coverage"]["counts"]["targets_incomplete"] == 2
        assert payload["next_action"] == "提交验收"

    def test_done_coverage_checks_are_not_downgraded_by_later_update(self, tmp_path):
        """已完成覆盖检查不能被后续模糊状态降级。"""
        from agent_py_agent.agent.task_progress import read_task_progress, write_task_progress

        write_task_progress(
            tmp_path,
            "run-main",
            {
                "coverage": {
                    "targets": [
                        {
                            "id": "fragment-001",
                            "status": "done",
                            "checks": {"读文件": "done", "写报告": "done"},
                            "evidence": ["fragment-001.txt", "final_report.md"],
                        }
                    ]
                }
            },
        )
        write_task_progress(
            tmp_path,
            "run-main",
            {
                "coverage": {
                    "targets": [
                        {
                            "id": "fragment-001",
                            "status": "pending",
                            "checks": {"读文件": "pending"},
                        }
                    ]
                }
            },
        )

        target = read_task_progress(tmp_path, "run-main")["coverage"]["targets"][0]

        assert target["status"] == "done"
        assert target["checks"] == {"读文件": "done", "写报告": "done"}
        assert target["evidence"] == ["fragment-001.txt", "final_report.md"]

    def test_corrupt_progress_file_is_reported_not_silently_emptied(self, tmp_path):
        """坏进度账本不能被伪装成“没有进度”。"""
        from agent_py_agent.agent.task_progress import (
            progress_path,
            read_task_progress,
            read_task_progress_report,
            task_progress_summary,
        )

        path = progress_path(tmp_path, "run-main")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not-json", encoding="utf-8")

        progress, load_error = read_task_progress_report(tmp_path, "run-main")
        legacy_progress = read_task_progress(tmp_path, "run-main")
        summary = task_progress_summary({**legacy_progress, "ref": str(path)})

        assert progress["load_error"]["context"] == "task_progress.read"
        assert load_error == progress["load_error"]
        assert legacy_progress["load_error"] == load_error
        assert summary["load_error"] == load_error


class TestTaskProgressFactPreservation:
    """测试 compact/恢复场景下已确认事实不会被无意覆盖。"""

    def test_done_item_facts_are_not_overwritten_by_later_done_update(self, tmp_path):
        """compact 后模型重构旧进度时，不应把已确认事实覆盖成错误值。"""
        from agent_py_agent.agent.task_progress import read_task_progress, write_task_progress

        write_task_progress(
            tmp_path,
            "run-main",
            {
                "items": [
                    {
                        "id": "fragment-002",
                        "title": "fragment-002",
                        "status": "done",
                        "result": "CP-002-15975 / SECRET-002-12711 / REVIEW",
                        "decision": "REVIEW",
                        "evidence": ["fragment-002.txt:1-120"],
                    }
                ]
            },
        )
        write_task_progress(
            tmp_path,
            "run-main",
            {
                "items": [
                    {
                        "id": "fragment-002",
                        "title": "fragment-002",
                        "status": "done",
                        "result": "CP-002-71319 / SECRET-002-55247 / KEEP",
                        "decision": "KEEP",
                        "evidence": ["fragment-002.txt:compact-summary"],
                    }
                ]
            },
        )

        item = read_task_progress(tmp_path, "run-main")["items"][0]

        assert item["result"] == "CP-002-15975 / SECRET-002-12711 / REVIEW"
        assert item["decision"] == "REVIEW"
        assert item["evidence"] == ["fragment-002.txt:1-120", "fragment-002.txt:compact-summary"]

    def test_done_item_can_be_explicitly_corrected(self, tmp_path):
        """确实要纠错时可以显式声明 correction，而不是无意覆盖。"""
        from agent_py_agent.agent.task_progress import read_task_progress, write_task_progress

        write_task_progress(
            tmp_path,
            "run-main",
            {"items": [{"id": "a", "status": "done", "result": "old", "evidence": ["old.md"]}]},
        )
        write_task_progress(
            tmp_path,
            "run-main",
            {"items": [{"id": "a", "status": "done", "result": "new", "evidence": ["new.md"], "correction": True}]},
        )

        item = read_task_progress(tmp_path, "run-main")["items"][0]

        assert item["result"] == "new"
        assert item["evidence"] == ["old.md", "new.md"]

    def test_fragment_id_aliases_do_not_merge_without_explicit_same_id(self, tmp_path):
        """不同 id 不靠名字猜成同一项；需要合并时必须用同一个结构化 id。"""
        from agent_py_agent.agent.task_progress import read_task_progress, write_task_progress

        write_task_progress(
            tmp_path,
            "run-main",
            {"items": [{"id": "001", "status": "done", "notes": "CP-001/SECRET-001/KEEP"}]},
        )
        write_task_progress(
            tmp_path,
            "run-main",
            {"items": [{"id": "frag-001", "status": "pending", "next": "稍后再看"}]},
        )
        write_task_progress(
            tmp_path,
            "run-main",
            {"items": [{"id": "fragment-001", "status": "done", "notes": "WRONG"}]},
        )

        payload = read_task_progress(tmp_path, "run-main")

        assert payload["counts"] == {"total": 3, "done": 2, "pending": 1}
        assert [item["id"] for item in payload["items"]] == ["001", "frag-001", "fragment-001"]

    def test_open_range_item_is_not_removed_by_child_item_names(self, tmp_path):
        """范围待办不会靠标题/编号猜测自动删除。"""
        from agent_py_agent.agent.task_progress import read_task_progress, write_task_progress

        write_task_progress(
            tmp_path,
            "run-main",
            {"items": [{"id": "ch051-052", "title": "章节051-052待读", "status": "in_progress"}]},
        )
        write_task_progress(
            tmp_path,
            "run-main",
            {
                "items": [
                    {"id": "ch051", "title": "章节051", "status": "done", "notes": "地点=杭州"},
                    {"id": "ch052", "title": "章节052", "status": "done", "notes": "地点=成都"},
                ]
            },
        )

        payload = read_task_progress(tmp_path, "run-main")

        assert [item["id"] for item in payload["items"]] == ["ch051-052", "ch051", "ch052"]
        assert payload["counts"] == {"total": 3, "in_progress": 1, "done": 2}

    def test_open_range_item_stays_until_all_child_items_are_done(self, tmp_path):
        """范围里还有缺口时，账本不能因为部分完成就放行。"""
        from agent_py_agent.agent.task_progress import read_task_progress, write_task_progress

        write_task_progress(
            tmp_path,
            "run-main",
            {"items": [{"id": "chapter-051-053", "title": "章节051-053待读", "status": "in_progress"}]},
        )
        write_task_progress(
            tmp_path,
            "run-main",
            {
                "items": [
                    {"id": "ch051", "title": "章节051", "status": "done"},
                    {"id": "ch052", "title": "章节052", "status": "done"},
                ]
            },
        )

        payload = read_task_progress(tmp_path, "run-main")

        assert [item["id"] for item in payload["items"]] == ["chapter-051-053", "ch051", "ch052"]
        assert payload["counts"] == {"total": 3, "in_progress": 1, "done": 2}


class TestTaskProgressContinuationAndAliases:
    """测试续读游标和结构化字段处理。"""

    def test_old_open_continuation_item_is_not_replaced_by_newer_cursor_text(self, tmp_path):
        """续读游标不会靠自然语言标题自动替换。"""
        from agent_py_agent.agent.task_progress import read_task_progress, write_task_progress

        write_task_progress(
            tmp_path,
            "run-main",
            {
                "items": [
                    {"id": "ch011+", "title": "章节011及之后章节待读", "status": "in_progress"},
                    {"id": "ch011", "title": "章节011", "status": "done"},
                    {"id": "ch012", "title": "章节012", "status": "done"},
                ]
            },
        )
        write_task_progress(
            tmp_path,
            "run-main",
            {"items": [{"id": "ch013+", "title": "章节013及之后章节待读", "status": "in_progress"}]},
        )

        payload = read_task_progress(tmp_path, "run-main")

        assert [item["id"] for item in payload["items"]] == ["ch011+", "ch011", "ch012", "ch013+"]
        assert payload["counts"] == {"total": 4, "in_progress": 2, "done": 2}

    def test_old_estimated_open_range_is_not_replaced_by_newer_cursor_text(self, tmp_path):
        """旧估算范围不会靠后续标题自动删除。"""
        from agent_py_agent.agent.task_progress import read_task_progress, write_task_progress

        write_task_progress(
            tmp_path,
            "run-main",
            {"items": [{"id": "ch005-040", "title": "章节005-040待读", "status": "in_progress"}]},
        )
        write_task_progress(
            tmp_path,
            "run-main",
            {
                "items": [
                    {"id": f"ch{number:03d}", "title": f"章节{number:03d}", "status": "done"}
                    for number in range(5, 13)
                ]
                + [{"id": "ch013+", "title": "章节013及之后章节待读", "status": "in_progress"}]
            },
        )

        payload = read_task_progress(tmp_path, "run-main")

        assert "ch005-040" in [item["id"] for item in payload["items"]]
        assert payload["items"][-1]["id"] == "ch013+"
        assert payload["counts"] == {"total": 10, "in_progress": 2, "done": 8}

    def test_task_progress_keeps_non_schema_statuses_out_of_done_counts(self, tmp_path):
        """status 只接受固定机器值；其它标签留作普通文本，不替模型猜成 done。"""
        from agent_py_agent.agent.task_progress import read_task_progress, write_task_progress

        write_task_progress(
            tmp_path,
            "run-main",
            {
                "items": [
                    {"id": "a", "status": "completed"},
                    {"id": "b", "status": "read"},
                    {"id": "c", "status": "已读"},
                    {"id": "d", "status": "待处理"},
                    {"id": "e", "status": "in-progress"},
                ]
            },
        )

        payload = read_task_progress(tmp_path, "run-main")

        assert payload["counts"] == {"total": 5, "other": 5}
        assert [item["status"] for item in payload["items"]] == ["completed", "read", "已读", "待处理", "in-progress"]

    def test_task_progress_summary_carries_recent_done_facts(self, tmp_path):
        """compact 交接要带最近完成事实，而不是只带未完成项。"""
        from agent_py_agent.agent.task_progress import (
            read_task_progress,
            task_progress_summary,
            write_task_progress,
        )

        write_task_progress(
            tmp_path,
            "run-main",
            {
                "items": [
                    {
                        "id": "fragment-001",
                        "title": "fragment-001",
                        "status": "done",
                        "result": "CP-001-10001 / SECRET-001-20001 / KEEP",
                        "evidence": ["fragment-001.txt:1-120"],
                    },
                    {"id": "fragment-002", "title": "fragment-002", "status": "in_progress"},
                ]
            },
        )

        summary = task_progress_summary(read_task_progress(tmp_path, "run-main"))

        assert summary["active_items"][0]["id"] == "fragment-002"
        assert summary["recent_done_items"][0]["id"] == "fragment-001"
        assert summary["recent_done_items"][0]["result"] == "CP-001-10001 / SECRET-001-20001 / KEEP"
        assert summary["recent_done_items"][0]["evidence"] == ["fragment-001.txt:1-120"]

    def test_coverage_targets_use_explicit_checks(self, tmp_path):
        """覆盖账本只从显式 checks 建立检查项。"""
        from agent_py_agent.agent.task_progress import read_task_progress, write_task_progress

        write_task_progress(
            tmp_path,
            "run-main",
            {
                "coverage": {
                    "targets": [
                        {
                            "id": "codex-main",
                            "checks": {
                                "做什么的": "pending",
                                "主要模块": "pending",
                                "优点": "pending",
                                "缺点": "pending",
                                "值得借鉴的地方": "pending",
                            },
                        }
                    ]
                }
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

    def test_task_progress_accepts_explicit_coverage_checks_with_update_action(self, tmp_path):
        """coverage checks 是当前结构化覆盖输入；action 必须使用 update。"""
        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig

        agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
        agent._main_agent_run_id = "run-main"

        result = agent.tools.execute_call(
            {
                "tool": "task_progress",
                "action": "update",
                "summary": "开始覆盖五个项目。",
                "coverage": {
                    "targets": [
                        {
                            "id": "agentscope",
                            "title": "agentscope-main分析",
                            "status": "in_progress",
                            "checks": {
                                "功能": "pending",
                                "主要模块": "pending",
                                "优点": "pending",
                                "缺点": "pending",
                                "借鉴点": "pending",
                            },
                        }
                    ]
                },
            }
        )
        payload = json.loads(result.output)

        assert result.ok is True
        assert payload["summary"] == "开始覆盖五个项目。"
        assert payload["coverage"]["targets"][0]["checks"]["功能"] == "pending"

    def test_task_progress_accepts_checks_dict(self, tmp_path):
        """checks 必须显式写成字段到状态的字典。"""
        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig

        agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
        agent._main_agent_run_id = "run-main"

        result = agent.tools.execute_call(
            {
                "tool": "task_progress",
                "action": "update",
                "coverage": {
                    "targets": [
                        {
                            "id": "agentscope",
                            "checks": {"功能描述": "pending", "主要模块": "pending", "优点": "done"},
                        }
                    ]
                },
            }
        )
        payload = json.loads(result.output)

        assert result.ok is True
        assert payload["coverage"]["targets"][0]["checks"] == {
            "功能描述": "pending",
            "主要模块": "pending",
            "优点": "done",
        }


def test_task_progress_rejects_old_action_aliases(tmp_path):
    """工具入口不再把 create/init/begin 旧别名偷偷当成 update。"""
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    agent._main_agent_run_id = "run-main"

    result = agent.tools.execute_call(
        {
            "tool": "task_progress",
            "action": "create",
            "summary": "开始覆盖五个项目。",
        }
    )
    payload = json.loads(result.output)

    assert result.ok is False
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"
    assert payload["invalid_action"] == "create"
    assert payload["allowed_actions"] == ["read", "update"]


class TestTaskProgressCoverageRejectedAliases:
    """测试旧 coverage 输入不会被隐式归一成机器账本。"""

    def test_task_progress_rejects_string_coverage_targets(self, tmp_path):
        """字符串 coverage_targets 不再被解析成结构化 coverage，也不再静默吞掉。"""
        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig

        agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
        agent._main_agent_run_id = "run-main"

        result = agent.tools.execute_call(
            {
                "tool": "task_progress",
                "action": "update",
                "summary": "开始分析五个项目。",
                "coverage_targets": [
                    "agentscope-main:功能定位,主要模块,优点,缺点,借鉴点",
                    "codex-main:功能定位,主要模块,优点,缺点,借鉴点",
                ],
            }
        )
        payload = json.loads(result.output)

        assert result.ok is False
        assert payload["invalid_fields"] == ["coverage_targets"]

    def test_task_progress_rejects_fields_needed_and_chinese_target_text(self, tmp_path):
        """fields_needed 和中文冒号覆盖项不再生成 checks，也不再作为顶层旧字段通过。"""
        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig

        agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
        agent._main_agent_run_id = "run-main"

        result = agent.tools.execute_call(
            {
                "tool": "task_progress",
                "action": "update",
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

        assert result.ok is False
        assert payload["invalid_fields"] == ["coverage_targets"]

    def test_task_progress_notes_do_not_create_implicit_coverage_checks(self, tmp_path):
        """notes 是事实或备注；不要靠冒号文本猜 coverage checks。"""
        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig

        agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
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
                        "notes": "待分析：做什么、主要模块、优点、缺点、借鉴点",
                    }
                ],
            }
        )
        payload = json.loads(result.output)

        assert result.ok is True
        item = payload["items"][0]
        assert item["notes"] == "待分析：做什么、主要模块、优点、缺点、借鉴点"
        assert "coverage" not in payload

    def test_task_progress_note_alias_is_ignored(self, tmp_path):
        """note 不再被提升成 notes。"""
        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig

        agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
        agent._main_agent_run_id = "run-main"

        result = agent.tools.execute_call(
            {
                "tool": "task_progress",
                "action": "update",
                "items": [{"id": "agentscope-main", "status": "in_progress", "note": "旧字段"}],
            }
        )
        payload = json.loads(result.output)

        assert result.ok is True
        assert payload["items"][0]["notes"] == ""

    def test_task_progress_rejects_name_and_missing_fields_aliases(self, tmp_path):
        """name/missing_fields 和字符串 coverage 不再生成 target，也不再作为顶层旧字段通过。"""
        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig

        agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
        agent._main_agent_run_id = "run-main"

        result = agent.tools.execute_call(
            {
                "tool": "task_progress",
                "action": "update",
                "coverage": "0/2 项目已分析",
                "coverage_targets": [
                    {"name": "agentscope-main", "status": "pending", "missing_fields": ["主要模块", "借鉴点"]},
                    {"name": "codex-main", "status": "pending", "missing_fields": []},
                ],
            }
        )
        payload = json.loads(result.output)

        assert result.ok is False
        assert payload["invalid_fields"] == ["coverage_targets"]


class TestTaskProgressQualityHints:
    """测试 task_progress 的软提示，不让提示变成验收门。"""

    def test_result_like_progress_without_evidence_is_a_soft_hint(self, tmp_path):
        """模型写了结果类字段但没 evidence 时才软提醒，不判断 ok/pass/完成 的语义。"""
        from agent_py_agent.agent.task_progress import (
            read_task_progress,
            task_progress_summary,
            write_task_progress,
        )

        payload = write_task_progress(
            tmp_path,
            "run-main",
            {
                "summary": "已经写完两个对象的初稿。",
                "items": [
                    {"id": "a", "title": "对象 A", "status": "ok"},
                    {"id": "b", "title": "对象 B", "result": "已经分析完"},
                    {"id": "c", "title": "对象 C", "conclusion": "可作为工具层参考", "evidence": ["notes.md"]},
                    {"id": "d", "title": "对象 D"},
                ],
            },
        )
        readback = read_task_progress(tmp_path, "run-main")
        summary = task_progress_summary(readback)

        assert readback["quality_hints"]["severity"] == "soft"
        assert readback["quality_hints"]["result_without_evidence_count"] == 1
        assert "建议补上" in readback["quality_hints"]["messages"][0]
        assert summary["quality_hints"]["result_without_evidence_ids"] == ["b"]
        assert payload["items"][1]["result"] == "已经分析完"
        assert readback["quality_hints"]["next_suggestions"]
        assert "不要只打勾" in readback["quality_hints"]["soft_prompt"]

    def test_update_returns_immediate_soft_feedback_when_evidence_is_missing(self, tmp_path):
        """写入进度当场返回软提醒，避免模型到下一轮 read 才看到问题。"""
        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig

        agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
        agent._main_agent_run_id = "run-main"

        result = agent.tools.execute_call(
            {
                "tool": "task_progress",
                "action": "update",
                "items": [{"id": "a", "title": "对象 A", "status": "done"}],
            }
        )
        payload = json.loads(result.output)

        assert result.ok is True
        assert payload["soft_feedback"]["severity"] == "soft"
        assert payload["soft_feedback"]["blocking"] is False
        assert "不要只打勾" in payload["soft_feedback"]["message"]

    def test_task_progress_tool_rejects_natural_language_status_values(self, tmp_path):
        """工具入口不允许普通自然语言污染 status 机器字段。"""
        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig

        agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
        agent._main_agent_run_id = "run-main"

        result = agent.tools.execute_call(
            {
                "tool": "task_progress",
                "action": "update",
                "items": [{"id": "a", "title": "对象 A", "status": "已完成"}],
            }
        )
        payload = json.loads(result.output)

        assert result.ok is False
        assert result.error_code == "TOOL_INVALID_ARGUMENTS"
        assert payload["invalid_statuses"][0]["status"] == "已完成"
        assert "done" in payload["allowed_statuses"]

    def test_update_soft_feedback_marks_failed_or_unseen_evidence_refs(self, tmp_path):
        """已写进进度的本地证据应能对上本轮工具事实，但只给软提醒。"""
        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig

        (tmp_path / "notes.md").write_text("ok\n", encoding="utf-8")
        agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
        agent._main_agent_run_id = "run-main"
        agent._current_tool_loop_params = SimpleNamespace(
            archive_tool_calls=[
                {"tool": "read_file", "ok": True, "parameters": {"path": "notes.md"}},
                {
                    "tool": "read_file",
                    "ok": False,
                    "parameters": {"path": "missing/README.md"},
                    "error_code": "PATH_NOT_FOUND",
                },
            ]
        )

        result = agent.tools.execute_call(
            {
                "tool": "task_progress",
                "action": "update",
                "items": [
                    {
                        "id": "a",
                        "title": "对象 A",
                        "status": "done",
                        "result": "已经分析完",
                        "evidence": ["notes.md", "missing/README.md", "unread.md"],
                    }
                ],
            }
        )
        payload = json.loads(result.output)

        warnings = payload["soft_feedback"]["evidence_source_warnings"]
        assert payload["soft_feedback"]["blocking"] is False
        assert warnings["failed_refs"] == ["missing/README.md"]
        assert warnings["unseen_refs"] == ["unread.md"]
        assert payload["quality_hints"]["evidence_source_warning_count"] == 2

    def test_update_does_not_warn_for_successfully_seen_evidence_refs(self, tmp_path):
        """本轮工具已经成功确认的证据路径，不应产生来源软提醒。"""
        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig

        agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
        agent._main_agent_run_id = "run-main"
        agent._current_tool_loop_params = SimpleNamespace(
            archive_tool_calls=[
                {"tool": "read_file", "ok": True, "parameters": {"path": str(tmp_path / "notes.md")}},
            ]
        )

        result = agent.tools.execute_call(
            {
                "tool": "task_progress",
                "action": "update",
                "items": [
                    {
                        "id": "a",
                        "title": "对象 A",
                        "status": "done",
                        "result": "已经分析完",
                        "evidence": ["notes.md"],
                    }
                ],
            }
        )
        payload = json.loads(result.output)

        assert "evidence_source_warnings" not in payload.get("soft_feedback", {})
        assert "evidence_source_warning_count" not in payload.get("quality_hints", {})

    def test_plain_progress_item_without_result_signal_does_not_hint(self, tmp_path):
        """只写标题/待办项时不提醒，避免把普通进度表变成噪声。"""
        from agent_py_agent.agent.task_progress import read_task_progress, write_task_progress

        write_task_progress(
            tmp_path,
            "run-main",
            {"items": [{"id": "a", "title": "对象 A"}, {"id": "b", "title": "对象 B", "notes": "待分析"}]},
        )

        payload = read_task_progress(tmp_path, "run-main")

        assert "quality_hints" not in payload

    def test_incomplete_coverage_gets_natural_next_step_hints(self, tmp_path):
        """覆盖账本没逐项推进时，应给模型自然语言软提示而不是验收硬卡。"""
        from agent_py_agent.agent.task_progress import read_task_progress, write_task_progress

        write_task_progress(
            tmp_path,
            "run-main",
            {
                "summary": "开始分析多个项目。",
                "coverage": {
                    "goal": "每个项目都要读源码、分析模块、写进报告。",
                    "dimensions": ["读源码", "分析模块", "写进报告"],
                    "targets": [
                        {
                            "id": "agentscope-main",
                            "checks": {"读源码": "done", "分析模块": "pending", "写进报告": "pending"},
                            "evidence": ["agentscope-main/README.md"],
                        },
                        {
                            "id": "codex-main",
                            "checks": {"读源码": "pending", "分析模块": "pending", "写进报告": "pending"},
                        },
                    ],
                },
            },
        )

        payload = read_task_progress(tmp_path, "run-main")
        hints = payload["quality_hints"]

        assert hints["severity"] == "soft"
        assert hints["coverage_incomplete_count"] == 2
        assert hints["coverage_incomplete_ids"] == ["agentscope-main", "codex-main"]
        assert any("继续补未完成对象" in item for item in hints["next_suggestions"])
        assert "先选一个未完成对象" in hints["soft_prompt"]

    def test_closeout_next_action_is_not_rewritten_from_natural_language_text(self, tmp_path):
        """next_action 是模型写入内容，系统不靠自然语言关键词改写它。"""
        from agent_py_agent.agent.task_progress import read_task_progress, write_task_progress

        payload = write_task_progress(
            tmp_path,
            "run-main",
            {
                "summary": "误以为已经全部完成。",
                "next_action": "提交验收",
                "items": [{"id": "fragment-001", "status": "done", "evidence": ["fragment-001.txt:12"]}],
                "coverage": {
                    "targets": [
                        {
                            "id": "fragment-001",
                            "checks": {"读取": "done", "写报告": "done"},
                            "evidence": ["fragment-001.txt:12"],
                        },
                        {
                            "id": "fragment-002",
                            "checks": {"读取": "pending", "写报告": "pending"},
                        },
                    ]
                },
            },
        )
        readback = read_task_progress(tmp_path, "run-main")

        assert payload["next_action"] == "提交验收"
        assert readback["next_action"] == "提交验收"
        assert "soft_next_action_repair" not in payload
