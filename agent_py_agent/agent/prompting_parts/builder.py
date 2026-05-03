from __future__ import annotations

"""主智能体使用的 prompt 拼装器。

这个模块的职责很纯粹：
把系统人格、相关记忆、动态规则、工具目录、候选工具详情、用户任务和工具执行记录
按固定顺序拼成模型真正看到的上下文。

顺序设计不是随便排的：
- 前面先放人格和长期规则，保证回答风格稳定
- 中间放记忆和工具信息，帮助模型理解“这次该怎么做”
- 没有工具记录时，最后放用户任务，让模型聚焦当前问题
- 已经有工具记录时，最后放工具记录和继续指令，让模型从最新工具结果往下走
"""

from pathlib import Path

from ..config import AgentConfig
from ..memory import MemoryRecord


class PromptBuilder:
    """负责构造每一轮发给模型的完整 prompt。"""

    def __init__(self, config: AgentConfig, root: Path):
        self.config = config
        self.root = root

    def read_prompt_files(self, extra_files: list[str] | None = None) -> list[str]:
        """读取动态 prompt 文件并拼接内容。

        大白话解释：
        这些文件相当于“额外行为规则”，只要被读进来，这一轮模型就真的能看到。
        """

        chunks: list[str] = []
        for name in [*self.config.prompt_files, *(extra_files or [])]:
            path = Path(name)
            if not path.is_absolute():
                path = self.root / path
            if path.exists():
                chunks.append(f"# Prompt File: {path}\n" + path.read_text(encoding="utf-8"))
        return chunks

    def build(
        self,
        user_prompt: str,
        memories: list[MemoryRecord],
        *,
        inject: list[str] | None = None,
        prompt_files: list[str] | None = None,
        tool_catalog_section: str = "",
        tool_recommendations_section: str = "",
        tool_context: list[str] | None = None,
    ) -> str:
        """拼出完整 prompt。

        这版和旧版最大的区别是把工具信息拆成了两层：
        - `tool_catalog_section`：常驻的工具目录，告诉模型“你手里有什么工具”
        - `tool_recommendations_section`：按当前任务筛出来的少量候选详情，告诉模型“这次大概率该用谁”
        """

        memory_text = "\n".join(
            f"- [{m.kind}] {m.role}: {m.content}" for m in memories
        ) or "（无相关记忆）"
        dynamic = "\n".join(self.read_prompt_files(prompt_files))
        injected = "\n".join(inject or [])
        tools_history = "\n\n".join(tool_context or [])
        if tools_history:
            task_and_transcript = (
                f"# User Task\n{user_prompt}\n\n"
                f"# Tool Transcript\n{tools_history}\n\n"
                "# Continue From Tool Transcript\n"
                "从最新的工具结果继续推进，不要重新开始任务。"
                "如果某个工具调用已经成功，不要重复调用同一个工具和同一组参数；"
                "直接使用已有结果进入下一步，或在证据足够时给出最终答案。"
            )
        else:
            task_and_transcript = (
                "# Tool Transcript\n（无）\n\n"
                f"# User Task\n{user_prompt}"
            )
        default_tools = "# Tools\n（当前未启用工具）"
        default_recommendations = "# Recommended Tools\n（当前无候选工具详情）"
        return (
            f"# System\n{self.config.system_prompt}\n\n"
            f"# Related Memory\n{memory_text}\n\n"
            f"# Dynamic Prompt Files\n{dynamic or '（无）'}\n\n"
            f"# Runtime Injection\n{injected or '（无）'}\n\n"
            f"{tool_catalog_section or default_tools}\n\n"
            f"{tool_recommendations_section or default_recommendations}\n\n"
            f"{task_and_transcript}\n"
        )
