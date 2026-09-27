"""聊天 `/skills`：在 TUI 与 IM 里查看、确认、拒绝技能提案，查看、回滚、删除自动总结的 Skill。

背景（2026-09-27）：用户几乎不用命令行，要求“所有都能 TUI 和 IM 来”。技能提案与自动总结 Skill 原来只有
`my-agent skills …` 命令行入口，自学习审核顺序点（skill_proposal_review）因此在 TUI/IM 里永远触发不了。
本测试锁定：解析拒绝式校验、命令目录走 Gateway、TUI 文本还原与本地模式拒绝（未列出的类型会落进停止分支）、
Gateway 分派不落入 steer/stop、提案确认必须带当前版本号、自动 Skill 只按登记表里的名字操作、
写操作在改动生效后才失败时回执不承诺“没有改动”（Codex 静态复核发现，2026-09-27）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore
from agent_py_agent.agent.capability.skill_proposals import PROPOSAL_COMMITTED, PROPOSAL_REJECTED
from agent_py_agent.agent.command_catalog import match_conversation_command
from agent_py_agent.agent.conversation.control_commands import (
    ConversationControlCommand,
    parse_conversation_control,
)
from agent_py_agent.agent.gateway_parts import skill_control_service as module
from agent_py_agent.agent.gateway_parts.skill_control_service import (
    SkillControlHost,
    execute_skill_control,
)
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.cli.chat_parts import control_runtime
from agent_py_agent.tests.test_skill_learning import NAME, _learned_file, _output, _publish_first
from agent_py_agent.tests.test_skill_learning import _runtime as _learning_runtime
from agent_py_agent.tests.test_skill_proposals import _proposal_ctx


# 函数用途: 解析一条聊天命令并断言它是 /skills 控制。
def _skills(text: str) -> ConversationControlCommand:
    command = parse_conversation_control(text, reject_unknown_slash=True)
    assert command is not None and command.kind == "skills"
    return command


# 函数用途: 在给定 owner 路径上执行 /skills（替换 Gateway 的 owner 解析，其余走真实服务与真实配置类）。
def _run(monkeypatch, home: object, text: str, *, self_learning: bool = True):
    host = SkillControlHost(home, AgentConfig(model_backend="echo", enable_self_learning=self_learning,
                                              self_learning_daily_limit=5))
    monkeypatch.setattr(module, "_scoped_host", lambda _agent, _scope: host)
    return execute_skill_control(None, _skills(text), None)


@pytest.mark.parametrize("text,operation,valid", [
    ("/skills", "overview", True), ("/skills help", "help", True), ("/skills proposals", "proposals", True),
    ("/skills Proposals ALL", "proposals", True), ("/skills proposals mine", "proposals", False),
    ("/skills show 0123ab", "show", True), ("/skills show 0123", "show", False), ("/skills show ../x", "show", False),
    ("/skills confirm 0123ab 2", "confirm", True), ("/skills confirm 0123ab 0", "confirm", False),
    ("/skills reject 0123ab", "reject", False), ("/skills learned", "learned", True),
    ("/skills learned show csv-merge-by-date", "learned_show", True),
    ("/skills learned remove ../../etc", "learned_remove", False), ("/skills learned drop x-y-z", "unknown", False),
    ("/skills whatever", "unknown", False),
])
def test_parser_validates_ids_revisions_and_names_before_any_service_call(text, operation, valid):
    command = _skills(text)
    assert (command.operation, command.valid) == (operation, valid)
    assert "/skills confirm" in command.usage


def test_catalog_routes_skills_through_the_gateway_and_tui_round_trips_the_text():
    # 带 conversation_suffix 的目录项会交给 Gateway，TUI 与 IM 共用；文本还原后重新解析得到同一命令。
    assert match_conversation_command("/skills confirm 0123ab 2") is not None
    command = _skills("/skills Confirm 0123AB 2")
    text = control_runtime._command_text(command)
    assert text == "/skills confirm 0123ab 2" and _skills(text) == command


def test_local_tui_mode_refuses_instead_of_falling_into_stop():
    execution = SimpleNamespace(state=SimpleNamespace(request_id="", running=True))
    result = control_runtime._execute_local_control(execution, _skills("/skills"))
    assert result.kind == "skills" and result.ok is False and "Gateway" in result.message


def test_gateway_dispatch_reaches_the_skill_service_not_steer_or_stop(monkeypatch):
    from agent_py_agent.agent.gateway_parts import control_service

    seen = []
    monkeypatch.setattr(module, "execute_skill_control",
                        lambda agent, command, scope: seen.append(command.operation) or "handled")
    assert control_service.execute_gateway_conversation_control(None, None, _skills("/skills"), None) == "handled"
    assert seen == ["overview"]
    invalid = control_service.execute_gateway_conversation_control(None, None, _skills("/skills show 01"), None)
    assert invalid.ok is False and "/skills confirm" in invalid.message and seen == ["overview"]


def test_pending_proposal_can_be_viewed_then_needs_the_current_revision_to_confirm(tmp_path, monkeypatch):
    ctx = _proposal_ctx(tmp_path)
    ref = ctx.proposal.proposal_id[:8]
    overview = _run(monkeypatch, ctx.home, "/skills")
    assert overview.ok and "待确认的技能提案：1 条" in overview.message and ctx.proposal.target.skill_name in overview.message
    assert f"/skills confirm {ref} 1" in overview.message and "自动总结的 Skill：0 个" in overview.message
    assert str(tmp_path) not in overview.message
    detail = _run(monkeypatch, ctx.home, f"/skills show {ref}")
    assert detail.ok and ctx.proposal.proposal_id in detail.message and "拟保存内容：" in detail.message
    stale = _run(monkeypatch, ctx.home, f"/skills confirm {ref} 7")
    assert stale.ok is False and "版本号对不上" in stale.message
    assert ctx.service.show(ctx.proposal.proposal_id).status != PROPOSAL_COMMITTED
    confirmed = _run(monkeypatch, ctx.home, f"/skills confirm {ref} 1")
    assert confirmed.ok and confirmed.message == f"已确认并安装 Skill：{ctx.proposal.target.skill_name}。"
    assert ctx.service.show(ctx.proposal.proposal_id).status == PROPOSAL_COMMITTED
    listing = _run(monkeypatch, ctx.home, "/skills proposals all")
    assert "已确认并安装" in listing.message and "/skills confirm" not in listing.message
    again = _run(monkeypatch, ctx.home, f"/skills reject {ref} 2")
    assert again.ok is False and "不是待确认" in again.message


def test_reject_and_unknown_or_ambiguous_references(tmp_path, monkeypatch):
    ctx = _proposal_ctx(tmp_path)
    ref = ctx.proposal.proposal_id[:8]
    missing = _run(monkeypatch, ctx.home, "/skills show ffffffff")
    assert missing.ok is False and "没有编号以 ffffffff 开头的提案" in missing.message
    rejected = _run(monkeypatch, ctx.home, f"/skills reject {ref} 1")
    assert rejected.ok and rejected.message.startswith(f"已拒绝提案 {ref}")
    assert ctx.service.show(ctx.proposal.proposal_id).status == PROPOSAL_REJECTED
    empty = _run(monkeypatch, ctx.home, "/skills", self_learning=False)
    assert "待确认的技能提案：0 条（自学习：关闭）" in empty.message and "没有需要你处理的提案" in empty.message


def test_a_prefix_shared_by_two_proposals_asks_for_more_digits():
    service = SimpleNamespace(list=lambda: [SimpleNamespace(proposal_id="abcdef" + "0" * 18),
                                            SimpleNamespace(proposal_id="abcdef" + "1" * 18)])
    with pytest.raises(module.SkillControlError, match="多输入几位"):
        module._proposal_by_ref(service, "abcdef")
    assert module._proposal_by_ref(service, "abcdef1").proposal_id.endswith("1" * 18)


def test_learned_skill_list_show_revert_and_remove_use_the_registry(tmp_path, monkeypatch):
    ctx = _learning_runtime(tmp_path, _output())
    _publish_first(ctx)
    listing = _run(monkeypatch, ctx.home, "/skills learned")
    assert listing.ok and f"1. {NAME} 第 1 版［生效中］" in listing.message
    assert f"/skills learned remove {NAME}" in listing.message and str(tmp_path) not in listing.message
    shown = _run(monkeypatch, ctx.home, f"/skills learned show {NAME}")
    assert shown.ok and shown.message.startswith(f"{NAME}：第 1 版，生效中") and "最近事件：" in shown.message
    absent = _run(monkeypatch, ctx.home, "/skills learned show not-learned-here")
    assert absent.ok is False and "SKILL_LEARNING_NOT_LEARNED" in absent.message
    removed = _run(monkeypatch, ctx.home, f"/skills learned remove {NAME}")
    assert removed.ok and "以后不再自动生成同名 Skill" in removed.message and not _learned_file(ctx).exists()
    assert "还没有自动总结的 Skill" in _run(monkeypatch, ctx.home, "/skills learned").message


def test_a_failure_after_the_change_does_not_claim_nothing_changed(tmp_path, monkeypatch):
    # 删除先归档目录、写登记表，再追加账本；账本追加失败时删除已经生效，回执不能说“没有改动”，要让用户先查当前状态。
    ctx = _learning_runtime(tmp_path, _output())
    _publish_first(ctx)

    def _ledger_unwritable(_store, _event):
        raise OSError("ledger unwritable")

    monkeypatch.setattr(SkillLearningStore, "append_event", _ledger_unwritable)
    failed = _run(monkeypatch, ctx.home, f"/skills learned remove {NAME}")
    assert failed.ok is False and not _learned_file(ctx).exists()
    assert "没能完整确认" in failed.message and "/skills learned" in failed.message and "没有改动" not in failed.message

    def _registry_unreadable(*_args):
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(module, "learned_skills_report", _registry_unreadable)
    unreadable = _run(monkeypatch, ctx.home, "/skills learned")
    assert unreadable.ok is False and unreadable.message == "技能提案或自动 Skill 暂时读不到，请稍后重试。"
