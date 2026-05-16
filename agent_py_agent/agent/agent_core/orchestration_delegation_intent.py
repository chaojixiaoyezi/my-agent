# LLM: Delegation intent helpers detect when a parent should stay refs-only or may inspect bodies.
# 模块用途: 集中解析用户自然语言里的“只看报告”和“你亲自验收”意图，供委托期 guard 使用。

from __future__ import annotations

import re


# LLM: prompt_requests_refs_only_delegation distinguishes explicit delegation from ordinary acceptance wording.
# 函数用途: 用户要求 root/父级只调度下级、只读 refs/报告时，启用顶层 root 读正文保护。
def prompt_requests_refs_only_delegation(prompt: str) -> bool:
    compact = " ".join(str(prompt or "").lower().split())
    if not compact:
        return False
    markers = (
        "root 只能创建和调度下级",
        "root只能创建和调度下级",
        "主代理只能创建和调度下级",
        "父级只能创建和调度下级",
        "只根据报告",
        "只根据小傻妞的报告",
        "只根据子代理的报告",
        "只根据下级报告",
        "根据报告做收口",
        "根据小傻妞的报告做收口",
        "根据子代理报告做收口",
        "不要亲自写页面",
        "不要亲自写产物",
        "不要自己写页面",
        "不要自己写产物",
        "只读 refs/报告",
        "只读refs/报告",
        "refs-only",
        "refs only",
        "不要直接读取业务产物正文",
        "不要主动读取产物正文",
        "不要读正文",
    )
    return any(marker in compact for marker in markers)


# LLM: prompt_requests_subagent_delegation catches normal user wording for "let agents help".
# 函数用途: 识别用户自然语言里的派工/小傻妞/分层团队意图，用于派工前少读正文策略。
def prompt_requests_subagent_delegation(prompt: str) -> bool:
    compact = " ".join(str(prompt or "").lower().split())
    if not compact:
        return False
    markers = (
        "小傻妞",
        "子代理",
        "孙代理",
        "派工",
        "分层团队",
        "分层小队",
        "协作完成",
        "组织多层",
        "多层",
        "delegate",
        "subagent",
        "spawn agents",
    )
    return any(marker in compact for marker in markers)


# LLM: user_authorized_parent_body_read recognizes explicit current-run user override phrases.
# 函数用途: 用户明确要求主代理亲自验收/检查时，临时允许父级读正文；普通“验收标准”不会触发。
def user_authorized_parent_body_read(prompt: str) -> bool:
    compact = " ".join(str(prompt or "").lower().split())
    if not compact:
        return False
    explicit_patterns = (
        r"你自己.{0,12}(验收|检查|核查|看一下)",
        r"你亲自.{0,12}(验收|检查|核查|看一下)",
        r"你.{0,8}做一下.{0,8}(验收|检查|核查)",
        r"(主代理|父级).{0,12}(亲自|自己).{0,12}(验收|检查|核查|看一下)",
        r"(parent agent|root agent|you yourself|personally).{0,40}(acceptance|inspect|review|verify)",
    )
    return any(re.search(pattern, compact) for pattern in explicit_patterns)
