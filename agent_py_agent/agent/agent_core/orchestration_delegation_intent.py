# LLM: Delegation intent helpers combine protocol flags with broad user delegation wording.
# 模块用途: 集中读取 refs_only/subagent_delegation 机器字段，并识别“派小傻妞/子代理协作”这类普通用户说法。

from __future__ import annotations


# LLM: prompt_requests_refs_only_delegation distinguishes explicit delegation from ordinary acceptance wording.
# 函数用途: 只接受 refs_only=true 或 delegate_only=true 机器字段，启用顶层 root 读正文保护。
def prompt_requests_refs_only_delegation(prompt: str) -> bool:
    compact = " ".join(str(prompt or "").lower().split())
    if not compact:
        return False
    return (
        "refs_only=true" in compact
        or "delegate_only=true" in compact
        or (_mentions_delegate_actor(compact) and _mentions_parent_should_not_do_body(compact))
    )


# LLM: prompt_requests_subagent_delegation catches normal user wording for "let agents help".
# 函数用途: 识别机器字段和普通“组织小傻妞/子代理协作完成”意图，用于派工前少读正文策略。
def prompt_requests_subagent_delegation(prompt: str) -> bool:
    compact = " ".join(str(prompt or "").lower().split())
    if not compact:
        return False
    return (
        "subagent_delegation=true" in compact
        or "delegate_only=true" in compact
        or (_mentions_delegate_actor(compact) and _mentions_delegation_action(compact))
    )


# LLM: user_authorized_parent_body_read recognizes explicit current-run user override phrases.
# 函数用途: 只接受 parent_body_read=allow 机器字段，临时允许父级读正文。
def user_authorized_parent_body_read(prompt: str) -> bool:
    compact = " ".join(str(prompt or "").lower().split())
    if not compact:
        return False
    return "parent_body_read=allow" in compact


# LLM: _mentions_delegate_actor is intentionally broad and task-agnostic.
# 函数用途: 只判断用户是否在谈子代理/小傻妞这类执行主体，不解析具体业务内容。
def _mentions_delegate_actor(text: str) -> bool:
    return any(marker in text for marker in ("小傻妞", "子代理", "subagent", "child agent"))


# LLM: _mentions_delegation_action separates active delegation from casual discussion.
# 函数用途: 判断用户是在要求安排/组织/派工，而不是单纯询问子代理概念。
def _mentions_delegation_action(text: str) -> bool:
    return any(
        marker in text
        for marker in (
            "派",
            "安排",
            "组织",
            "协作",
            "帮",
            "做",
            "完成",
            "处理",
            "执行",
            "delegate",
            "dispatch",
            "work on",
        )
    )


# LLM: _mentions_parent_should_not_do_body catches user refs-only wording without internal flags.
# 函数用途: 用户说“你不要亲自写/只看报告”时，父级验收前继续保持 refs-only。
def _mentions_parent_should_not_do_body(text: str) -> bool:
    return any(marker in text for marker in ("不要亲自", "不要你自己", "你自己不要", "只根据", "只看报告"))
