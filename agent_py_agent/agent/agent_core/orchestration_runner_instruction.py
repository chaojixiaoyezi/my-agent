# LLM: Runner instruction normalization keeps dispatch tool thin and prevents placeholder path leaks.
# 模块用途: 处理 dispatch_subagents 传给 runner 的补充说明，替换工作区占位符。

from __future__ import annotations

from pathlib import Path


# LLM: resolved_runner_instruction removes unresolved natural-language placeholders before runners see them.
# 函数用途: 把 {workspace_root} 这类模板变量替换成真实任务工作区，避免子代理把占位符当路径。
def resolved_runner_instruction(agent: object, value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    workspace_root = _agent_workspace_root(agent)
    if workspace_root:
        text = text.replace("{workspace_root}", workspace_root).replace("{{workspace_root}}", workspace_root)
    return text


# LLM: _agent_workspace_root returns a concrete workspace path without leaking MagicMock string values.
# 函数用途: 安全读取 agent.subagents.workspace_root；测试替身或缺失字段返回空字符串。
def _agent_workspace_root(agent: object) -> str:
    raw = getattr(getattr(agent, "subagents", None), "workspace_root", None)
    if not isinstance(raw, str | Path):
        return ""
    return str(Path(raw).expanduser().resolve(strict=False))
