from __future__ import annotations

"""智能体核心循环。"""

from dataclasses import dataclass
from pathlib import Path

from .backend import get_backend
from .config import AgentConfig
from .memory import JsonlMemory
from .prompting import PromptBuilder
from .subagent import SubAgentManager, SubAgentTask
from .tools import ToolRegistry


@dataclass
class AgentRunResult:
    """一次 `run()` 调用的结果。"""

    prompt: str
    response: str
    backend: str
    used_memories: int
    tool_rounds: int = 0


class SimpleAgent:
    """CLI 智能体的主调度器。

    它做的事情可以概括成一句话：
    “把记忆、prompt、模型后端和工具循环串成一条能跑的主链路。”
    """

    def __init__(self, config: AgentConfig, root: str | Path):
        self.config = config
        self.root = Path(root)
        self.memory = JsonlMemory(self.root / config.memory_path)
        self.prompts = PromptBuilder(config, self.root)
        self.backend = get_backend(config.model_backend, config)
        self.subagents = SubAgentManager(self.root / config.subagent_workspace)

        # CLI 入口传进来的 root 通常是包目录 `agent_py_agent`。
        # 但工具更适合看到整个项目根目录，不然它只能读到包内部文件。
        workspace_root = self.root.parent if (self.root / "__main__.py").exists() else self.root
        self.tools = ToolRegistry(
            workspace_root,
            max_chars=config.tool_read_max_chars,
            max_entries=config.tool_list_max_entries,
            max_matches=config.tool_search_max_matches,
            web_max_chars=config.tool_web_max_chars,
            http_timeout=config.tool_http_timeout,
            catalog_limit=config.tool_catalog_limit,
            retrieval_limit=config.tool_retrieval_limit,
            vector_search_enabled=config.tool_vector_search_enabled,
        )

    def run(
        self,
        user_prompt: str,
        *,
        inject: list[str] | None = None,
        prompt_files: list[str] | None = None,
        save: bool | None = None,
    ) -> AgentRunResult:
        """执行一轮智能体请求。"""

        memories = self.memory.search(user_prompt, self.config.memory_top_k)
        tool_catalog_section = self.tools.render_catalog_section() if self.config.enable_tools else ""
        tool_recommendations_section = (
            self.tools.render_recommended_tools_section(user_prompt)
            if self.config.enable_tools
            else ""
        )
        tool_context: list[str] = []
        tool_rounds = 0
        final_prompt = ""
        final_response = None

        while True:
            final_prompt = self.prompts.build(
                user_prompt,
                memories,
                inject=inject,
                prompt_files=prompt_files,
                tool_catalog_section=tool_catalog_section,
                tool_recommendations_section=tool_recommendations_section,
                tool_context=tool_context,
            )
            response = self.backend.generate(final_prompt)
            final_response = response

            if not self.config.enable_tools:
                break

            calls = self.tools.parse_tool_calls(response.text)
            if not calls:
                break

            if tool_rounds >= self.config.max_tool_rounds:
                tool_context.append("[tool-system]\n已达到最大工具轮数限制，停止继续调用工具。")
                break

            tool_rounds += 1
            tool_context.append(f"[assistant-tool-round-{tool_rounds}]\n{response.text}")
            for idx, payload in enumerate(calls, start=1):
                result = self.tools.execute_call(payload)
                tool_context.append(
                    f"[tool-call-{tool_rounds}-{idx}]\n{payload}\n"
                    f"[tool-result-{tool_rounds}-{idx}]\n{result.render_for_prompt()}"
                )

        assert final_response is not None
        do_save = self.config.auto_save_memory if save is None else save
        if do_save:
            self.memory.add("user", user_prompt)
            self.memory.add("agent", final_response.text, tags=[final_response.backend])

        return AgentRunResult(
            prompt=final_prompt,
            response=final_response.text,
            backend=final_response.backend,
            used_memories=len(memories),
            tool_rounds=tool_rounds,
        )

    def remember(self, content: str, *, kind: str = "note"):
        """手动写入一条记忆。"""

        return self.memory.add("user", content, kind=kind)

    def recall(self, query: str, top_k: int | None = None):
        """召回相关记忆。"""

        return self.memory.search(query, top_k or self.config.memory_top_k)

    def spawn_subagents(self, goal: str, count: int | None = None) -> list[SubAgentTask]:
        """生成子任务记录。"""

        if not self.config.enable_subagents:
            raise RuntimeError("配置已禁用 subagent。")
        n = min(count or self.config.max_subagents, self.config.max_subagents)
        return self.subagents.split(goal, n)
