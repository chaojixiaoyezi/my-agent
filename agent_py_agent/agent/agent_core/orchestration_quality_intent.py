# LLM: Quality intent helpers convert explicit user QA wording into stable role ids.
# 模块用途: 用户要求“测试子代理/验收子代理/找茬子代理”时，收口层能识别缺少的质量角色。

from __future__ import annotations

from ..subagents.services.qa_role_contract import qa_roles_from_text


# LLM: required_quality_roles_from_prompt is only for top-level user intent, not child task contracts.
# 函数用途: 合并机器字段和普通中英文质量角色说法；只返回 tester/bug_finder/acceptor 这些模板 id。
def required_quality_roles_from_prompt(prompt: str) -> list[str]:
    roles = set(qa_roles_from_text(prompt))
    text = " ".join(str(prompt or "").lower().replace("-", "_").split())
    roles.update(_natural_quality_roles(text))
    return [role for role in ("tester", "bug_finder", "acceptor") if role in roles]


# LLM: _natural_quality_roles recognizes broad QA-role requests without parsing product requirements.
# 函数用途: 只识别“测试/找错/验收 + 子代理/小傻妞”等流程意图，避免把普通任务名当成验收合同。
def _natural_quality_roles(text: str) -> set[str]:
    roles: set[str] = set()
    if _mentions_agent_role(text, ("测试子代理", "测试小傻妞", "tester", "qa agent", "test agent")):
        roles.add("tester")
    if _mentions_agent_role(
        text,
        (
            "找茬子代理",
            "找错子代理",
            "查错子代理",
            "挑错子代理",
            "找茬小傻妞",
            "bug_finder",
            "bug finder",
            "critic agent",
        ),
    ):
        roles.add("bug_finder")
    if _mentions_agent_role(
        text,
        (
            "验收子代理",
            "验收小傻妞",
            "acceptor",
            "acceptance agent",
            "verification agent",
        ),
    ):
        roles.add("acceptor")
    if _mentions_agent_word(text) and "测试" in text and "验收" in text:
        roles.update({"tester", "acceptor"})
    return roles


# LLM: _mentions_agent_role keeps natural matching tied to role-agent phrases.
# 函数用途: 判断是否出现明确质量角色短语；不用业务词、文件名或页面内容做判断。
def _mentions_agent_role(text: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase in text for phrase in phrases)


# LLM: _mentions_agent_word distinguishes role workflow intent from generic QA words.
# 函数用途: 只有同时提到子代理/小傻妞时，才把“测试和验收”提升为缺失角色要求。
def _mentions_agent_word(text: str) -> bool:
    return any(marker in text for marker in ("子代理", "小傻妞", "agent"))
