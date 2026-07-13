"""task_progress coverage ledger tests."""

from __future__ import annotations

import json
from pathlib import Path
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

    def test_partial_update_by_id_keeps_original_title(self, tmp_path):
        """瑕疵A回归(merge 底座):按 id 部分更新(对账 credit / 模型只发 id+status)
        不得把 title 覆盖成 id——normalize 若把缺失 title 回填成 id,merge overlay 会拿
        这个非空合成值覆盖原功能名(真机:done 项 title 全变 req-NN)。"""
        from agent_py_agent.agent.task_progress import (
            read_task_progress,
            task_progress_summary,
            write_task_progress,
        )

        write_task_progress(
            tmp_path,
            "run-main",
            {"coverage": {"targets": [{"id": "req-07", "title": "文件附件上传下载", "status": "pending"}]}},
        )
        write_task_progress(
            tmp_path,
            "run-main",
            {
                "coverage": {
                    "targets": [
                        {
                            "id": "req-07",
                            "status": "done",
                            "evidence": ["output/upload.py"],
                            "source_ref": "auto:dispatch-covers-binding",
                        }
                    ]
                }
            },
        )

        target = read_task_progress(tmp_path, "run-main")["coverage"]["targets"][0]
        assert target["title"] == "文件附件上传下载"
        assert target["status"] == "done"
        assert target["source_ref"] == "auto:dispatch-covers-binding"

        # 从未有过 title 的项(模型只给 id 自立):存储如实留空,展示侧按 id 兜底不摆烂。
        write_task_progress(
            tmp_path,
            "run-main",
            {"coverage": {"targets": [{"id": "extra-target", "status": "pending"}]}},
        )
        progress = read_task_progress(tmp_path, "run-main")
        extra = next(t for t in progress["coverage"]["targets"] if t["id"] == "extra-target")
        assert extra["title"] == ""
        summary = task_progress_summary(progress)
        shown = next(t for t in summary["coverage"]["active_targets"] if t["id"] == "extra-target")
        assert shown["title"] == "extra-target"

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
                    {"id": "f", "status": "DONE"},
                    {"id": "g", "status": "Done"},
                    {"id": "h", "status": "IN_PROGRESS"},
                ]
            },
        )

        payload = read_task_progress(tmp_path, "run-main")

        assert payload["counts"] == {"total": 8, "unknown": 8}
        assert [item["status"] for item in payload["items"]] == [
            "unknown",
            "unknown",
            "unknown",
            "unknown",
            "unknown",
            "unknown",
            "unknown",
            "unknown",
        ]
        assert [item["raw_status"] for item in payload["items"]] == [
            "completed",
            "read",
            "已读",
            "待处理",
            "in-progress",
            "DONE",
            "Done",
            "IN_PROGRESS",
        ]
        assert {item["status_protocol_error"] for item in payload["items"]} == {"TASK_PROGRESS_STATUS_INVALID"}

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
    assert payload["allowed_actions"] == ["read", "update", "select"]


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
                "items": [
                    {"id": "a", "title": "对象 A", "status": "已完成"},
                    {"id": "b", "title": "对象 B", "status": "DONE"},
                ],
            }
        )
        payload = json.loads(result.output)

        assert result.ok is False
        assert result.error_code == "TOOL_INVALID_ARGUMENTS"
        assert payload["invalid_statuses"][0]["status"] == "已完成"
        assert payload["invalid_statuses"][1]["status"] == "DONE"
        assert "done" in payload["allowed_statuses"]

    def test_task_progress_tool_rejects_coverage_status_aliases(self, tmp_path):
        """coverage 里的机器状态也只接受当前 schema 值，不吃自然语言或别名。"""
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
                            "id": "paper-1",
                            "status": "已完成",
                            "checks": {"读取": "DONE", "翻译": "done"},
                        }
                    ]
                },
            }
        )
        payload = json.loads(result.output)

        assert result.ok is False
        assert result.error_code == "TOOL_INVALID_ARGUMENTS"
        invalid = payload["invalid_coverage_statuses"]
        assert [item["status"] for item in invalid] == ["已完成", "DONE"]
        assert invalid[0]["field"] == "coverage.targets[0].status"
        assert invalid[1]["field"] == "coverage.targets[0].checks.读取"

    def test_task_progress_legacy_coverage_status_aliases_become_unknown(self, tmp_path):
        """旧账本里的 coverage 状态别名只进 unknown，不补猜成完成。"""
        from agent_py_agent.agent.task_progress import read_task_progress

        progress_path = tmp_path / "memory_archive" / "task_progress" / "run-main" / "progress.json"
        progress_path.parent.mkdir(parents=True)
        progress_path.write_text(
            json.dumps(
                {
                    "run_id": "run-main",
                    "coverage": {
                        "targets": [
                            {
                                "id": "paper-1",
                                "status": "DONE",
                                "checks": {"读取": "已完成", "翻译": "done"},
                            }
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )

        payload = read_task_progress(tmp_path, "run-main")
        target = payload["coverage"]["targets"][0]

        assert target["status"] == "unknown"
        assert target["raw_status"] == "DONE"
        assert target["status_protocol_error"] == "TASK_PROGRESS_STATUS_INVALID"
        assert target["checks"] == {"读取": "unknown", "翻译": "done"}
        assert payload["coverage"]["counts"] == {
            "targets_total": 1,
            "targets_done": 0,
            "targets_incomplete": 1,
            "checks_total": 2,
            "checks_done": 1,
            "checks_incomplete": 1,
        }

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


class TestTaskProgressItemTitleFidelity:
    """items 侧与 coverage 同根的 title 如实度(瑕疵A补全)。"""

    def test_marking_seeded_item_done_by_id_keeps_title(self, tmp_path):
        """派工种的待办(title=子代理目标)被模型按 id 标 done 时,title 不得被覆盖成 id。"""
        from agent_py_agent.agent.task_progress import read_task_progress, write_task_progress

        write_task_progress(
            tmp_path,
            "run-main",
            {"items": [{"id": "subagent-abc12345", "title": "子代理[abc12345]:建后端 API", "status": "in_progress"}]},
        )
        write_task_progress(
            tmp_path,
            "run-main",
            {"items": [{"id": "subagent-abc12345", "status": "done"}]},
        )

        item = read_task_progress(tmp_path, "run-main")["items"][0]
        assert item["title"] == "子代理[abc12345]:建后端 API"
        assert item["status"] == "done"


class TestRequirementDoneEvidenceGate:
    """不足1·需求项 done 证据闸:自动种的需求枚举项标 done 必须带证据——把"顺手 done"
    (真机 u-fc1:'别用占位'被当约束满足了标 done)逼向两条稳定出口:补证据 / skipped+reason。"""

    @staticmethod
    def _seeded_ledger(tmp_path, run_id="run-req"):
        from agent_py_agent.agent.task_progress import write_task_progress

        write_task_progress(
            tmp_path,
            run_id,
            {
                "coverage": {
                    "targets": [
                        {"id": "req-21", "title": "别用占位", "status": "pending",
                         "coverage_kind": "requirement_item", "source_ref": "auto:requirement-enumeration"},
                        {"id": "req-01", "title": "用户注册登录", "status": "pending",
                         "coverage_kind": "requirement_item", "source_ref": "auto:requirement-enumeration"},
                    ]
                }
            },
        )

    def test_requirement_done_without_evidence_detected(self, tmp_path):
        from agent_py_agent.agent.task_progress import (
            read_task_progress,
            requirement_done_without_evidence,
        )

        self._seeded_ledger(tmp_path)
        existing = read_task_progress(tmp_path, "run-req")
        update = {"coverage": {"targets": [{"id": "req-21", "status": "done"}]}}

        violations = requirement_done_without_evidence(existing, update)

        assert violations == [{"id": "req-21", "title": "别用占位", "reason": "no_evidence"}]

    def test_done_with_evidence_or_skipped_passes(self, tmp_path):
        """artifact_roots=None(纯函数无盘上下文)保留旧"非空即过"语义。"""
        from agent_py_agent.agent.task_progress import (
            read_task_progress,
            requirement_done_without_evidence,
        )

        self._seeded_ledger(tmp_path)
        existing = read_task_progress(tmp_path, "run-req")
        with_evidence = {"coverage": {"targets": [{"id": "req-01", "status": "done", "evidence": ["output/auth/"]}]}}
        skipped = {"coverage": {"targets": [{"id": "req-21", "status": "skipped", "notes": "指令碎片"}]}}

        assert requirement_done_without_evidence(existing, with_evidence) == []
        assert requirement_done_without_evidence(existing, skipped) == []

    def test_existing_evidence_counts_and_non_requirement_untouched(self, tmp_path):
        from agent_py_agent.agent.task_progress import (
            read_task_progress,
            requirement_done_without_evidence,
            write_task_progress,
        )

        self._seeded_ledger(tmp_path)
        # 账本里已有证据(此前对账/中途附过)→ 本次只发 id+status 也放行(非空语义)。
        write_task_progress(
            tmp_path, "run-req",
            {"coverage": {"targets": [{"id": "req-01", "evidence": ["output/auth/api.py"]}]}},
        )
        existing = read_task_progress(tmp_path, "run-req")
        assert requirement_done_without_evidence(existing, {"coverage": {"targets": [{"id": "req-01", "status": "done"}]}}) == []
        # 模型自立项(非 auto 种)不受此闸约束。
        model_own = {"coverage": {"targets": [{"id": "my-own", "title": "自立项", "status": "done"}]}}
        assert requirement_done_without_evidence(existing, model_own) == []

    # ------------------------------------------------ g8 升级:产物存在性判据(正反)

    def test_junk_evidence_rejected_when_roots_given(self, tmp_path):
        """填充话术("已确保无占位")evidence 非空但指不出实存产物 → 拒(g8 u-gc2 实锤形态)。"""
        from agent_py_agent.agent.task_progress import (
            read_task_progress,
            requirement_done_without_evidence,
        )

        self._seeded_ledger(tmp_path)
        existing = read_task_progress(tmp_path, "run-req")
        update = {"coverage": {"targets": [{"id": "req-21", "status": "done", "evidence": ["已确保无占位"]}]}}

        violations = requirement_done_without_evidence(existing, update, artifact_roots=[tmp_path])

        assert violations == [{"id": "req-21", "title": "别用占位", "reason": "evidence_not_artifact"}]

    def test_real_artifact_file_dir_and_prose_path_pass(self, tmp_path):
        """真产物三形态放行:实存文件 / 非空目录 / 散文里嵌的路径 token;path:line 也认。"""
        from agent_py_agent.agent.task_progress import (
            read_task_progress,
            requirement_done_without_evidence,
        )

        self._seeded_ledger(tmp_path)
        module_dir = tmp_path / "output" / "auth"
        module_dir.mkdir(parents=True)
        (module_dir / "api.py").write_text("def register(): ...\n", encoding="utf-8")
        existing = read_task_progress(tmp_path, "run-req")

        for evidence in (
            ["output/auth/api.py"],
            ["output/auth/"],
            ["实现见 output/auth/api.py 与测试"],
            ["output/auth/api.py:12"],
            [str(module_dir / "api.py")],
        ):
            update = {"coverage": {"targets": [{"id": "req-01", "status": "done", "evidence": evidence}]}}
            assert requirement_done_without_evidence(existing, update, artifact_roots=[tmp_path]) == [], evidence

    def test_placeholder_or_empty_artifacts_rejected(self, tmp_path):
        """占位空壳不算产物:空文件 / 系统兜底占位文本 / 空目录 / 仅含占位空壳的目录 / 不存在路径全拒。"""
        from agent_py_agent.agent.task_progress import (
            read_task_progress,
            requirement_done_without_evidence,
        )

        self._seeded_ledger(tmp_path)
        (tmp_path / "output").mkdir()
        (tmp_path / "output" / "empty.py").write_text("", encoding="utf-8")
        (tmp_path / "output" / "shell.md").write_text("# Subagent Result\n- run: x\n", encoding="utf-8")
        (tmp_path / "output" / "hollow").mkdir()
        existing = read_task_progress(tmp_path, "run-req")

        # ["output/"] = 目录里只有空文件+占位空壳+空子目录:指整个目录也不能蒙混过闸
        # (旧代码目录证据只查"非空",占位壳 size>0 就放行 → 回归此格)。
        for evidence in (["output/empty.py"], ["output/shell.md"], ["output/hollow/"], ["output/"], ["output/nowhere.py"]):
            update = {"coverage": {"targets": [{"id": "req-01", "status": "done", "evidence": evidence}]}}
            violations = requirement_done_without_evidence(existing, update, artifact_roots=[tmp_path])
            assert violations == [{"id": "req-01", "title": "用户注册登录", "reason": "evidence_not_artifact"}], evidence

    def test_binary_artifact_passes_and_closed_target_skipped(self, tmp_path):
        """二进制产物不误伤;账本已 closed 的项重复标 done 是 no-op 不再验(对账已 credit 的项)。"""
        from agent_py_agent.agent.task_progress import (
            read_task_progress,
            requirement_done_without_evidence,
            write_task_progress,
        )

        self._seeded_ledger(tmp_path)
        (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
        existing = read_task_progress(tmp_path, "run-req")
        binary = {"coverage": {"targets": [{"id": "req-01", "status": "done", "evidence": ["logo.png"]}]}}
        assert requirement_done_without_evidence(existing, binary, artifact_roots=[tmp_path]) == []

        # 系统对账已把 req-01 credit 成 done(机器证据不是文件路径);模型重复报 done 不该被拒。
        write_task_progress(
            tmp_path, "run-req",
            {"coverage": {"targets": [{"id": "req-01", "status": "done", "evidence": ["subagent-done:req_x"]}]}},
        )
        existing = read_task_progress(tmp_path, "run-req")
        remark = {"coverage": {"targets": [{"id": "req-01", "status": "done"}]}}
        assert requirement_done_without_evidence(existing, remark, artifact_roots=[tmp_path]) == []

    def test_tool_rejects_requirement_done_without_evidence(self, tmp_path):
        """工具入口整体拒绝该次写入:账本不落 done,错误信息教两条出口;真产物放行。"""
        from agent_py_agent.agent.agent_core.task_progress_tool import TaskProgressTool
        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig
        from agent_py_agent.agent.task_progress import read_task_progress

        agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
        agent._main_agent_run_id = "run-req"
        root = agent.home_paths.owner_home_dir
        self._seeded_ledger(root, "run-req")
        tool = TaskProgressTool(agent)

        result = tool.execute(
            {"action": "update", "coverage": {"targets": [{"id": "req-21", "status": "done"}]}}
        )

        assert result.ok is False
        payload = json.loads(result.output)
        assert payload["targets_missing_evidence"] == [{"id": "req-21", "title": "别用占位", "reason": "no_evidence"}]
        assert "skipped" in payload["how_to_fix"]
        assert payload["artifact_roots_checked"]
        statuses = {
            t["id"]: t["status"]
            for t in read_task_progress(root, "run-req")["coverage"]["targets"]
        }
        assert statuses["req-21"] == "pending"  # 拒写生效,账本没被"顺手 done"

        # 场面话 evidence(非空但无产物)同样拒——这是 g8 u-gc2 糊弄过闸的原形态。
        junk = tool.execute(
            {"action": "update", "coverage": {"targets": [{"id": "req-21", "status": "done", "evidence": ["已确保无占位"]}]}}
        )
        assert junk.ok is False
        assert json.loads(junk.output)["targets_missing_evidence"][0]["reason"] == "evidence_not_artifact"

        # 出口一:改标 skipped+reason → 放行。
        ok_skip = tool.execute(
            {"action": "update", "coverage": {"targets": [{"id": "req-21", "status": "skipped", "notes": "约束指令,非功能"}]}}
        )
        assert ok_skip.ok is True
        # 出口二:done+真实存在的产物路径 → 放行(owner home 根解析)。
        module_dir = Path(root) / "output" / "auth"
        module_dir.mkdir(parents=True)
        (module_dir / "api.py").write_text("def register(): ...\n", encoding="utf-8")
        ok_done = tool.execute(
            {"action": "update", "coverage": {"targets": [{"id": "req-01", "status": "done", "evidence": ["output/auth/"]}]}}
        )
        assert ok_done.ok is True

    def test_tool_resolves_evidence_against_task_workspace_roots(self, tmp_path):
        """evidence 相对【任务工作区】写(真机主形态:output/server/...)也解析得到。"""
        from types import SimpleNamespace

        from agent_py_agent.agent.agent_core.task_progress_tool import TaskProgressTool
        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig

        agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
        agent._main_agent_run_id = "run-req"
        root = agent.home_paths.owner_home_dir
        self._seeded_ledger(root, "run-req")
        workspace = Path(root) / "tasks" / "2026-07-06" / "req_demo"
        (workspace / "output" / "server").mkdir(parents=True)
        (workspace / "output" / "server" / "notification.py").write_text("class Notification: ...\n", encoding="utf-8")
        agent._current_run_params = SimpleNamespace(
            source="",
            run_id="run-req",
            task_id="run-req",
            task_attributes={
                "run_workspace": {
                    "task_root": str(workspace),
                    "output_dir": str(workspace / "output"),
                    "work_dir": str(workspace / "work"),
                }
            },
        )
        tool = TaskProgressTool(agent)

        ok_done = tool.execute(
            {
                "action": "update",
                "coverage": {"targets": [{"id": "req-01", "status": "done", "evidence": ["output/server/notification.py"]}]},
            }
        )
        assert ok_done.ok is True
