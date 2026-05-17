# LLM: Delegation intent helpers read protocol flags, not natural-language intent.
# 模块用途: 集中读取 refs_only/subagent_delegation/parent_body_read 机器字段，供委托期 guard 使用。

from __future__ import annotations


# LLM: prompt_requests_refs_only_delegation distinguishes explicit delegation from ordinary acceptance wording.
# 函数用途: 只接受 refs_only=true 或 delegate_only=true 机器字段，启用顶层 root 读正文保护。
def prompt_requests_refs_only_delegation(prompt: str) -> bool:
    compact = " ".join(str(prompt or "").lower().split())
    if not compact:
        return False
    return "refs_only=true" in compact or "delegate_only=true" in compact


# LLM: prompt_requests_subagent_delegation catches normal user wording for "let agents help".
# 函数用途: 只接受 subagent_delegation=true / delegate_only=true 机器字段，用于派工前少读正文策略。
def prompt_requests_subagent_delegation(prompt: str) -> bool:
    compact = " ".join(str(prompt or "").lower().split())
    if not compact:
        return False
    return "subagent_delegation=true" in compact or "delegate_only=true" in compact


# LLM: user_authorized_parent_body_read recognizes explicit current-run user override phrases.
# 函数用途: 只接受 parent_body_read=allow 机器字段，临时允许父级读正文。
def user_authorized_parent_body_read(prompt: str) -> bool:
    compact = " ".join(str(prompt or "").lower().split())
    if not compact:
        return False
    return "parent_body_read=allow" in compact
