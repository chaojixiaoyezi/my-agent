"""Wave S：自学 Skill 的两处用户可见缺陷。

1. 普通对话回合里用户要求归档自学 Skill 时，skill_feedback(action=remove) 常是本回合第一个工具；它此前不晋升回合任务，
   执行权核对因缺任务链接一律报 CLAIM_UNAVAILABLE（R-17 只修了 adopted/ineffective）。
2. /skills learned 与 CLI learned list 的“有效使用”从汇总顶层读，汇总把它放在 totals 里，因此永远显示 0 次。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from agent_py_agent.agent.capability import skill_feedback_tool
from agent_py_agent.agent.capability.skill_learning_report import learned_skills_report
from agent_py_agent.agent.capability.skill_usage_ledger import record_skill_usage_event
from agent_py_agent.cli.skill_learning_commands import _list_lines
from agent_py_agent.tests.test_skill_chat_control import _run
from agent_py_agent.tests.test_skill_learning import _output, _publish_first
from agent_py_agent.tests.test_skill_learning import _runtime as _learning_runtime
from agent_py_agent.tests.test_sm_w9_feedback_awareness import _make_real_agent, _publish_skill
from agent_py_agent.tests.test_w3_r17_skill_feedback_main_turn import _plain_foreground_turn


# 函数用途: 普通回合第一次调用 remove 也先晋升本回合任务，然后照常归档并封禁同名自动生成。
def test_remove_in_a_plain_conversation_turn_binds_the_turn_task_first(tmp_path):
    agent, _repo, store = _make_real_agent(tmp_path, enable_self_learning=True)
    _publish_skill(store, "wave-s-remove")
    thread = _plain_foreground_turn(agent, tmp_path)
    assert agent.conversation_store.tasks.load("gwreq-msg-r17") is None
    outcome = skill_feedback_tool.SkillFeedbackTool(agent).execute({"action": "remove",
                                                                    "skill_id": "owner:wave-s-remove"})
    assert outcome.ok, outcome.output
    assert json.loads(outcome.output)["code"] == "OK"
    link = agent.conversation_store.tasks.load("gwreq-msg-r17")
    assert link is not None and link.thread_id == thread.thread_id
    registry = store.load_registry()
    assert "wave-s-remove" not in registry.skills and "wave-s-remove" in registry.blocked_names


# 函数用途: 写入两次正文读取，列表行应显示生命周期累计的 2 次有效使用。
def _usage_ctx(tmp_path):
    ctx = _learning_runtime(tmp_path, _output())
    _publish_first(ctx)
    for task in ("task-a", "task-b"):
        record_skill_usage_event(ctx.store, event="manual_get", skill_id="owner:csv-merge-by-date", source="owner",
                                 request_id=f"request-{task}", thread_id="thread-wave-s", task_id=task, chars=40)
    return ctx


# 函数用途: TUI/IM 的 /skills learned 读汇总 totals 里的有效使用次数。
def test_chat_learned_listing_reports_lifetime_effective_uses(tmp_path, monkeypatch):
    ctx = _usage_ctx(tmp_path)
    listing = _run(monkeypatch, ctx.home, "/skills learned")
    assert listing.ok and "有效使用 2 次" in listing.message.splitlines()[0]


# 函数用途: 命令行 learned list 与聊天共用同一报告，也显示 totals 里的累计有效使用。
def test_cli_learned_listing_reports_lifetime_effective_uses(tmp_path):
    ctx = _usage_ctx(tmp_path)
    payload = {**learned_skills_report(ctx.store, SimpleNamespace(enable_self_learning=True)), "owner_id": "local/main"}
    assert "累计有效使用 2 次" in _list_lines(payload)[1]
