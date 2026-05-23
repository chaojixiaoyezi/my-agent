# LLM: Hierarchy domain vocabulary stays data-only so scope guards remain focused on decisions.
# 模块用途: 保存层级调度领域识别用的停用词和协调角色词。

from __future__ import annotations

# LLM: structural run/ref/qa words are stopwords so generated checker children do not collide by parent refs.
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
    "id",
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
    "ref",
    "refs",
    "reviewer",
    "run",
    "runner",
    "qa",
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
