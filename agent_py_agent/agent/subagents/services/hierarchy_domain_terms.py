# LLM: Hierarchy domain vocabulary stays data-only so scope guards remain focused on decisions.
# 模块用途: 保存层级调度领域识别用的停用词和协调角色词。

from __future__ import annotations

DOMAIN_STOPWORDS = {
    "agent",
    "acceptor",
    "build",
    "child",
    "checker",
    "claude",
    "code",
    "coordinator",
    "deliverables",
    "demo",
    "depth",
    "grand",
    "grandchild",
    "html",
    "implementer",
    "js",
    "css",
    "lead",
    "leaf",
    "level",
    "layer",
    "my",
    "one",
    "page",
    "reporter",
    "reviewer",
    "runner",
    "shop",
    "static",
    "subagent",
    "task",
    "test",
    "tests",
    "tester",
    "three",
    "two",
    "users",
    "worker",
    "workspace",
    "xiaoyezi",
}

COORDINATION_ROLE_TOKENS = {
    "acceptor",
    "checker",
    "coordinator",
    "lead",
    "reporter",
    "reviewer",
    "tester",
}

FORBIDDEN_SCOPE_GENERIC_TERMS = {
    *DOMAIN_STOPWORDS,
    "depth",
    "layer",
    "level",
}
