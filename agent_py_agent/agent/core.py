# LLM: 这是主代理兼容门面，保持初始化依赖顺序和公开导出稳定。
# 模块用途: SimpleAgent 组装入口，连接配置、记忆、后端、工具和子代理管理器。

from __future__ import annotations

"""composition root for SimpleAgent after splitting runtime, subagents, dispatch, and tools.

给人看的解释：
以前这个文件把主循环、子代理 runner、父代理 dispatch、工具定义、prompt 模板都堆在一起。
现在真实逻辑按职责拆到 `agent_core/`，这里只负责组装 SimpleAgent，并保留旧公开导入路径。
"""

from pathlib import Path

from .agent_core import (
    AgentRunResult,
    CapabilityConfigPatchTool,
    CapabilityRequestTool,
    CreateSubagentsTool,
    DispatchSubagentsTool,
    InspectAgentTreeTool,
    RaiseMainEventTool,
    RaiseObservationTool,
    ScheduleChildSubagentsTool,
    SimpleAgentDispatchMixin,
    SimpleAgentRuntimeMixin,
    SimpleAgentSubagentMixin,
    SubagentBoardTool,
    SubagentMessageTool,
)
from .agent_core.dispatch_lock import _DispatchWatchLock
from .agent_core.orchestration_tools import CODING_SUBAGENT_TOOLS, READ_ONLY_SUBAGENT_TOOLS
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
from .backend import get_backend
from .capability.runtime_config import default_capability_config_path
from .collaboration import (
    CaseStatusTool,
    CollaborationStore,
    ListCollaborationRequestsTool,
    OpenCaseTool,
    RaiseCollaborationEventTool,
    RequestCollaborationTool,
    RerouteCollaborationRequestTool,
    SubmitEvidenceTool,
    UpdateCaseStatusTool,
    UpdateCollaborationRequestTool,
)
from .config import AgentConfig
from .conversation import ConversationStore
from .local_store import LocalStore
from .memory import JsonlMemory
from .prompting import PromptBuilder
from .subagent import SubAgentManager
from .tooling.registry import ToolRegistry, ToolRegistryParams
from .tooling.registry_payload_normalize import tool_payload_limits_from_config
from .user_space.home_layout import ensure_my_agent_home, home_paths
from .user_space.paths import get_user_paths


# LLM: _resolve_paths 属于 兼容入口 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 根据配置和用户空间选择 LocalStore、memory、subagent 与 gateway 的落盘路径。
def _resolve_paths(config, root: Path):
    """解析所有存储路径（支持用户空间隔离）。"""
    user_id = getattr(config, "user_id", "admin") or "admin"
    user_data_root = getattr(config, "user_data_root", "data/users") or "data/users"
    root = Path(root)

    if user_id != "admin":
        user_paths = get_user_paths(user_id, root / user_data_root)
        return {
            "local_store_path": user_paths.local_store_path,
            "local_store_files_dir": user_paths.local_store_files_dir,
            "local_store_events_path": user_paths.local_store_events_path,
            "memory_path": user_paths.memory_path,
            "subagent_workspace": user_paths.subagent_workspace,
            "gateway_workspace": root / config.gateway_workspace,
            "conversation_workspace": root / config.conversation_workspace,
            "collaboration_workspace": root / config.collaboration_workspace,
        }

    return {
        "local_store_path": root / config.local_store_path,
        "local_store_files_dir": root / config.local_store_files_dir,
        "local_store_events_path": root / config.local_store_events_path,
        "memory_path": root / config.memory_path,
        "subagent_workspace": root / config.subagent_workspace,
        "gateway_workspace": root / config.gateway_workspace,
        "conversation_workspace": root / config.conversation_workspace,
        "collaboration_workspace": root / config.collaboration_workspace,
    }


# LLM: _normalized_workspace_roots 属于 兼容入口 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 解析并去重工作区根目录，保留第一个主工作区。
def _normalized_workspace_roots(primary: Path, roots: list[str | Path] | None) -> list[Path]:
    resolved: list[Path] = []
    for raw in [primary, *(roots or [])]:
        path = Path(raw).resolve()
        if path not in resolved:
            resolved.append(path)
    return resolved


# LLM: SimpleAgent 属于 兼容入口 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: 主代理门面，持有配置、记忆、后端、工具注册表和子代理管理器。
class SimpleAgent(
    SimpleAgentRuntimeMixin,
    SimpleAgentSubagentMixin,
    SimpleAgentDispatchMixin,
):
    """wires config, memory, prompts, backend, tools, and subagent manager into one agent facade.

    给人看的解释：
    这是用户和 CLI 看到的主代理对象。
    它自己只做依赖组装；具体怎么聊天、怎么跑子代理、怎么 dispatch，已经分别交给 mixin 文件。
    """

    # LLM: SimpleAgent.__init__ 属于 兼容入口 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 初始化 SimpleAgent 的依赖、配置和运行期字段。
    def __init__(self, config: AgentConfig, root: str | Path, workspace_roots: list[str | Path] | None = None):
        """initialize all SimpleAgent collaborators and register orchestration tools.

        给人看的解释：
        创建主代理时会准备本地账本、记忆、prompt 构造器、模型后端、子代理管理器和工具注册表。
        最后把"创建子代理、看板、dispatch"这三个编排工具也注册进去。
        """
        self.config = config
        self.root = Path(root)
        self.capability_config_path = default_capability_config_path(self.root)
        self._capability_config_runtime_snapshot = None
        self._orchestration_run_ids_seen: set[str] = set()
        self.workspace_roots = _normalized_workspace_roots(self.root, workspace_roots)

        self.home_paths = _resolve_home_paths(config)
        paths = _resolve_paths(config, self.root)
        self.local_store = LocalStore(
            paths["local_store_path"],
            files_dir=paths["local_store_files_dir"],
            events_path=paths["local_store_events_path"],
            enable_fts=config.local_store_fts_enabled,
        )
        self.memory = JsonlMemory(
            paths["memory_path"],
            local_store=self.local_store,
            daily_mirror_dir=_daily_memory_dir(config, self.home_paths),
        )
        self.prompts = PromptBuilder(config, self.root, home_paths=self.home_paths)
        self.backend = get_backend(config.model_backend, config)
        self.conversation_store = ConversationStore(paths["conversation_workspace"])
        self.collaboration_store = CollaborationStore(paths["collaboration_workspace"])
        self.subagents = _build_subagent_manager(self, paths)
        self.tools = _build_tool_registry(self, config)
        _register_orchestration_tools(self)


# LLM: _resolve_home_paths is the single owner-home bootstrap point for SimpleAgent startup.
# 函数用途: 根据配置初始化或解析 my-agent 家目录，并把路径对象交给运行时复用。
def _resolve_home_paths(config: AgentConfig):
    if bool(getattr(config, "home_runtime_bootstrap_enabled", True)):
        return ensure_my_agent_home(getattr(config, "my_agent_home", None))
    return home_paths(getattr(config, "my_agent_home", None))


# LLM: _daily_memory_dir keeps daily mirroring opt-in/out through config while preserving legacy memory_path.
# 函数用途: 返回 JsonlMemory 的按天镜像目录；关闭配置时返回 None。
def _daily_memory_dir(config: AgentConfig, paths):
    if not bool(getattr(config, "daily_memory_mirror_enabled", True)):
        return None
    return paths.memory_daily_dir


# LLM: _build_subagent_manager 属于 兼容入口 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 用代理依赖和路径配置创建 SubAgentManager。
def _build_subagent_manager(agent: SimpleAgent, paths: dict) -> SubAgentManager:
    return SubAgentManager(
        paths["subagent_workspace"],
        local_store=agent.local_store,
        collaboration_store=agent.collaboration_store,
        workspace_root=agent.root,
        workspace_roots=agent.workspace_roots,
        role_template_dirs=agent.config.subagent_role_template_dirs,
        enable_self_learning=agent.config.enable_self_learning,
        debug_trace_level=agent.config.subagent_debug_trace_level,
        takeover_chain_max_depth=agent.config.subagent_takeover_chain_max_depth,
        closeout_for_all_task_nodes=agent.config.closeout_for_all_task_nodes,
    )


# LLM: _build_tool_registry 属于 兼容入口 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 按工具配置创建 ToolRegistry 并注入工作区边界。
def _build_tool_registry(agent: SimpleAgent, config: AgentConfig) -> ToolRegistry:
    workspace_root = agent.root.parent if (agent.root / "__main__.py").exists() else agent.root
    workspace_roots = [workspace_root, *[root for root in agent.workspace_roots if root != agent.root]]
    return ToolRegistry(
        ToolRegistryParams(
            workspace_root=workspace_root,
            workspace_roots=workspace_roots,
            max_chars=config.tool_read_max_chars,
            max_entries=config.tool_list_max_entries,
            max_matches=config.tool_search_max_matches,
            web_max_chars=config.tool_web_max_chars,
            http_timeout=config.tool_http_timeout,
            catalog_limit=config.tool_catalog_limit,
            catalog_mode=config.tool_catalog_mode,
            catalog_offset=config.tool_catalog_offset,
            catalog_categories=config.tool_catalog_categories,
            catalog_include_examples=config.tool_catalog_include_examples,
            catalog_entry_max_chars=config.tool_catalog_entry_max_chars,
            catalog_show_truncated_notice=config.tool_catalog_show_truncated_notice,
            tool_detail_max_chars=config.tool_detail_max_chars,
            retrieval_limit=config.tool_retrieval_limit,
            vector_search_enabled=config.tool_vector_search_enabled,
            shell_tool_timeout=config.tool_shell_timeout,
            shell_tool_output_max_chars=config.tool_shell_output_max_chars,
            access_mode=config.access_mode,
            tool_write_inline_max_chars=config.tool_write_inline_max_chars,
            artifact_read_budget_window_seconds=config.tool_artifact_read_budget_window_seconds,
            artifact_read_budget_max_chars=config.tool_artifact_read_budget_max_chars,
            artifact_default_read_chars=config.memory_artifact_default_read_chars,
            payload_limits=tool_payload_limits_from_config(config),
        )
    )


# LLM: _register_orchestration_tools 属于 兼容入口 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 把创建子代理、看板和 dispatch 编排工具注册到主代理工具表。
def _register_orchestration_tools(agent: SimpleAgent) -> None:
    agent.tools.register(CreateSubagentsTool(agent))
    agent.tools.register(CapabilityRequestTool(agent))
    agent.tools.register(CapabilityConfigPatchTool(agent))
    agent.tools.register(SubagentBoardTool(agent))
    agent.tools.register(InspectAgentTreeTool(agent))
    agent.tools.register(RaiseObservationTool(agent))
    agent.tools.register(RaiseMainEventTool(agent))
    agent.tools.register(RaiseCollaborationEventTool(agent))
    agent.tools.register(OpenCaseTool(agent))
    agent.tools.register(RequestCollaborationTool(agent))
    agent.tools.register(SubmitEvidenceTool(agent))
    agent.tools.register(UpdateCollaborationRequestTool(agent))
    agent.tools.register(RerouteCollaborationRequestTool(agent))
    agent.tools.register(UpdateCaseStatusTool(agent))
    agent.tools.register(CaseStatusTool(agent))
    agent.tools.register(ListCollaborationRequestsTool(agent))
    agent.tools.register(SubagentMessageTool(agent))
    agent.tools.register(DispatchSubagentsTool(agent))
    agent.tools.register(ScheduleChildSubagentsTool(agent))


__all__ = [
    "AgentRunResult",
    "CapabilityRequestTool",
    "CapabilityConfigPatchTool",
    "CaseStatusTool",
    "CODING_SUBAGENT_TOOLS",
    "CreateSubagentsTool",
    "DispatchSubagentsTool",
    "InspectAgentTreeTool",
    "ListCollaborationRequestsTool",
    "OpenCaseTool",
    "RaiseCollaborationEventTool",
    "RaiseMainEventTool",
    "RaiseObservationTool",
    "RequestCollaborationTool",
    "ONE_SHOT_TOOL_NAMES",
    "PARENT_PLANNER_READ_TOOLS",
    "READ_ONLY_SUBAGENT_TOOLS",
    "ScheduleChildSubagentsTool",
    "SimpleAgent",
    "SubmitEvidenceTool",
    "UpdateCaseStatusTool",
    "UpdateCollaborationRequestTool",
    "SubagentMessageTool",
    "SubagentBoardTool",
]
