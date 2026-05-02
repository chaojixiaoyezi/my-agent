from __future__ import annotations

"""LLM: composition root for SimpleAgent after splitting runtime, subagents, dispatch, and tools.

给人看的解释：
以前这个文件把主循环、子代理 runner、父代理 dispatch、工具定义、prompt 模板都堆在一起。
现在真实逻辑按职责拆到 `agent_core/`，这里只负责组装 SimpleAgent，并保留旧公开导入路径。
"""

from pathlib import Path

from .agent_core import (
    AgentRunResult,
    CreateSubagentsTool,
    DispatchSubagentsTool,
    SimpleAgentDispatchMixin,
    SimpleAgentRuntimeMixin,
    SimpleAgentSubagentMixin,
    SubagentBoardTool,
)
from .agent_core.dispatch_lock import _DispatchWatchLock
from .agent_core.parameters import (
    ONE_SHOT_TOOL_NAMES,
    _bool_param,
    _non_negative_int,
    _one_shot_tool_call_key,
    _positive_int,
    _sleep_with_stop,
    _string_list,
)
from .agent_core.planner import (
    PARENT_PLANNER_READ_TOOLS,
    _build_parent_planner_prompt,
    _build_parent_planner_state,
    _combine_runner_instruction,
    _task_state_for_planner,
)
from .agent_core.runner_dispatch import (
    RETRYABLE_RUNNER_FAILURE_TYPES,
    _dispatch_patch_review_run_ids,
    _dispatch_runner_candidates,
    _is_dispatch_runner_candidate,
    _limit_items,
    _resolve_runner_concurrency,
    _run_subagent_worker,
    _runner_dispatch_record,
    _runner_failure_type,
    _runner_max_attempts,
    _runner_retry_reason,
    _task_has_runner_patches,
)
from .agent_core.runner_prompts import (
    _append_runner_repair_failure,
    _append_runner_repair_prompt,
    _append_runner_repair_response,
    _build_subagent_runner_prompt,
    _build_subagent_runner_repair_prompt,
)
from .agent_core.orchestration_tools import CODING_SUBAGENT_TOOLS, READ_ONLY_SUBAGENT_TOOLS
from .backend import get_backend
from .config import AgentConfig
from .local_store import LocalStore
from .memory import JsonlMemory
from .prompting import PromptBuilder
from .subagent import SubAgentManager
from .tools import ToolRegistry
from .user_space.paths import get_user_paths


class SimpleAgent(
    SimpleAgentRuntimeMixin,
    SimpleAgentSubagentMixin,
    SimpleAgentDispatchMixin,
):
    """LLM: wires config, memory, prompts, backend, tools, and subagent manager into one agent facade.

    给人看的解释：
    这是用户和 CLI 看到的主代理对象。
    它自己只做依赖组装；具体怎么聊天、怎么跑子代理、怎么 dispatch，已经分别交给 mixin 文件。
    """

    def __init__(self, config: AgentConfig, root: str | Path):
        """LLM: initialize all SimpleAgent collaborators and register orchestration tools.

        给人看的解释：
        创建主代理时会准备本地账本、记忆、prompt 构造器、模型后端、子代理管理器和工具注册表。
        最后把"创建子代理、看板、dispatch"这三个编排工具也注册进去。
        """

        self.config = config
        self.root = Path(root)

        # 检查是否使用用户空间隔离
        user_id = getattr(config, "user_id", "admin") or "admin"
        user_data_root = getattr(config, "user_data_root", "data/users") or "data/users"

        if user_id != "admin":
            # 使用用户空间路径
            user_paths = get_user_paths(user_id, self.root / user_data_root)
            local_store_path = user_paths.local_store_path
            local_store_files_dir = user_paths.local_store_files_dir
            local_store_events_path = user_paths.local_store_events_path
            memory_path = user_paths.memory_path
            subagent_workspace = user_paths.subagent_workspace
            # gateway 路径保持原配置，不隔离
            gateway_workspace = self.root / config.gateway_workspace
        else:
            # 默认行为：使用配置中的路径
            local_store_path = self.root / config.local_store_path
            local_store_files_dir = self.root / config.local_store_files_dir
            local_store_events_path = self.root / config.local_store_events_path
            memory_path = self.root / config.memory_path
            subagent_workspace = self.root / config.subagent_workspace
            gateway_workspace = self.root / config.gateway_workspace

        self.local_store = LocalStore(
            local_store_path,
            files_dir=local_store_files_dir,
            events_path=local_store_events_path,
            enable_fts=config.local_store_fts_enabled,
        )
        self.memory = JsonlMemory(memory_path, local_store=self.local_store)
        self.prompts = PromptBuilder(config, self.root)
        self.backend = get_backend(config.model_backend, config)
        self.subagents = SubAgentManager(
            subagent_workspace,
            local_store=self.local_store,
            workspace_root=self.root,
            enable_self_learning=config.enable_self_learning,
        )

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
        self.tools.register(CreateSubagentsTool(self))
        self.tools.register(SubagentBoardTool(self))
        self.tools.register(DispatchSubagentsTool(self))


__all__ = [
    "AgentRunResult",
    "CODING_SUBAGENT_TOOLS",
    "CreateSubagentsTool",
    "DispatchSubagentsTool",
    "ONE_SHOT_TOOL_NAMES",
    "PARENT_PLANNER_READ_TOOLS",
    "READ_ONLY_SUBAGENT_TOOLS",
    "SimpleAgent",
    "SubagentBoardTool",
]
