
from __future__ import annotations

import os

"""composition root for SimpleAgent runtime, tools, memory, gateway, and subagents."""

from pathlib import Path

from .agent_core import (
    AgentRunResult,
    CancelSubagentsTool,
    CapabilityRequestTool,
    CreateSubagentsTool,
    DispatchSubagentsTool,
    InspectAgentTreeTool,
    RaiseEventTool,
    ResolveCapabilityRequestsTool,
    ScheduleChildSubagentsTool,
    SendGuidanceTool,
    SimpleAgentDispatchMixin,
    SimpleAgentRuntimeMixin,
    SimpleAgentSubagentMixin,
    TaskProgressTool,
    WaitTool,
)
from .agent_core.orchestration.dispatch.lock import _DispatchWatchLock
from .agent_core.orchestration_tools import CODING_SUBAGENT_TOOLS, READ_ONLY_SUBAGENT_TOOLS
from .agent_core.parameters import (
    ONE_SHOT_TOOL_NAMES,
    _bool_param,
    _non_negative_int,
    _one_shot_tool_call_key,
    _positive_int,
    _sleep_with_stop,
)
from .agent_core.planner_service import (
    PARENT_PLANNER_READ_TOOLS,
)
from .agent_core.planner_service import (
    build_parent_planner_prompt as _build_parent_planner_prompt,
)
from .agent_core.planner_service import (
    build_parent_planner_state as _build_parent_planner_state,
)
from .agent_core.planner_service import (
    combine_runner_instruction as _combine_runner_instruction,
)
from .agent_core.planner_service import (
    task_state_for_planner as _task_state_for_planner,
)
from .agent_core.runner.dispatch import (
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
from .agent_core.runner.prompts import (
    _append_runner_repair_failure,
    _append_runner_repair_prompt,
    _append_runner_repair_response,
    _build_subagent_runner_prompt,
    _build_subagent_runner_repair_prompt,
)
from .agent_core.runtime.owner_roots import runtime_owner_root
from .backends import get_backend
from .capability import CapabilityRouter
from .capability.create_skill_tool import CreateSkillTool, register_owner_skills
from .capability.memory_tool import RememberTool
from .capability.runtime_config_reload import default_capability_config_path
from .capability.session_search_tool import SessionSearchTool
from .capability.skill_search_tool import SkillSearchTool
from .collaboration import (
    CollaborationStore,
    InspectCollaborationTool,
    RaiseCollaborationTool,
    SubmitCollaborationResultTool,
    UpdateCollaborationTool,
)
from .conversation import ConversationStore
from .local_storage import LocalStore
from .memory_store import JsonlMemory
from .prompting_parts import PromptBuilder
from .settings import AgentConfig
from .settings.runtime_guard_config import runtime_guard_policy
from .subagents.manager import SubAgentManager
from .tooling.registry import ToolRegistry, ToolRegistryParams
from .tooling.registry_payload_normalize import tool_payload_limits_from_config
from .user_space.home_indexes import register_owner_ref
from .user_space.home_layout import ensure_my_agent_home, home_paths
from .user_space.owner_policy import resolve_effective_owner_policy
from .user_space.owner_resolver import (
    ensure_owner_home,
    home_paths_with_owner,
    owner_identity_from_config,
)
from .user_space.runtime_paths import (
    apply_runtime_paths_to_config,
    resolve_runtime_paths_for_agent,
)


def _normalized_workspace_roots(primary: Path, roots: list[str | Path] | None) -> list[Path]:
    resolved: list[Path] = []
    for raw in [primary, *(roots or [])]:
        path = Path(raw).resolve()
        if path not in resolved:
            resolved.append(path)
    return resolved


# LLM: 模型端点环境事实(R13c 实锤:任务里需要 LLM 子调用〔翻译/摘要〕时,
#   模型知道 AGENT_API_KEY 在环境里,却不知道配套端点——把多家第三方服务商
#   端点猜了个遍全 401,最后写了"声称产物存在"的清单提前交付。把本代理自用的
#   api_base/model_name 以 AGENT_API_BASE/AGENT_MODEL_NAME 暴露进进程环境,
#   run_command 子进程自然继承,脚本可用同一套凭据+端点完成 LLM 子调用。
#   setdefault:用户显式设置的同名变量优先;非敏感信息(key 本就在环境)。
# 函数用途: 让"帮手脚本也能调到模型"不再靠猜端点。
def _export_model_endpoint_env(config) -> None:
    api_base = str(getattr(config, "api_base", "") or "").strip()
    model_name = str(getattr(config, "model_name", "") or "").strip()
    if api_base:
        os.environ.setdefault("AGENT_API_BASE", api_base)
    if model_name:
        os.environ.setdefault("AGENT_MODEL_NAME", model_name)


class SimpleAgent(
    SimpleAgentRuntimeMixin,
    SimpleAgentSubagentMixin,
    SimpleAgentDispatchMixin,
):
    """wires config, memory, prompts, backend, tools, and subagent manager into one agent runtime.

    这是用户和 CLI 看到的主代理对象。
    它自己只做依赖组装；具体怎么聊天、怎么跑子代理、怎么 dispatch，已经分别交给 mixin 文件。
    """

    def __init__(self, config: AgentConfig, root: str | Path, workspace_roots: list[str | Path] | None = None):
        """initialize all SimpleAgent collaborators and register orchestration tools.

        创建主代理时会准备本地账本、记忆、prompt 构造器、模型后端、子代理管理器和工具注册表。
        最后把"创建子代理、看板、dispatch"这三个编排工具也注册进去。
        """
        self.config = config
        _export_model_endpoint_env(config)
        self.runtime_guard_policy = runtime_guard_policy()
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.capability_config_path = default_capability_config_path(self.root)
        self._capability_config_runtime_snapshot = None
        self._orchestration_run_ids_seen: set[str] = set()
        self.workspace_roots = _normalized_workspace_roots(self.root, workspace_roots)

        self.home_paths = _resolve_home_paths(config)
        self.owner_policy = resolve_effective_owner_policy(self.home_paths)
        self.runtime_path_resolution = resolve_runtime_paths_for_agent(config, self.root, self.home_paths)
        paths = self.runtime_path_resolution.paths
        apply_runtime_paths_to_config(config, self.runtime_path_resolution)
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
        # skill 树第一期:主代理常驻一个能力路由器(默认带 builtin skills),
        # prompt 层类目索引/命中卡与 skill_search 工具共用同一实例。
        self.capability_router = CapabilityRouter()
        register_owner_skills(self.capability_router, self)
        self.prompts.capability_router = self.capability_router
        self.backend = get_backend(config.model_backend, config)
        self.conversation_store = ConversationStore(paths["conversation_workspace"])
        self.collaboration_store = CollaborationStore(paths["collaboration_workspace"])
        self.subagents = _build_subagent_manager(self, paths)
        self.tools = _build_tool_registry(self, config)
        _register_orchestration_tools(self)


def _resolve_home_paths(config: AgentConfig):
    root = _configured_home_root(config)
    paths = ensure_my_agent_home(root)
    owner = ensure_owner_home(paths.root, owner_identity_from_config(config))
    _register_owner_ref_if_possible(paths, owner)
    return home_paths_with_owner(paths, owner)


def _configured_home_root(config: AgentConfig) -> str | None:
    raw = str(getattr(config, "my_agent_home", "") or "").strip()
    return raw or None


def _register_owner_ref_if_possible(paths, owner) -> None:
    try:
        register_owner_ref(paths, owner)
    except OSError:
        return


def _daily_memory_dir(config: AgentConfig, paths):
    if not bool(getattr(config, "daily_memory_mirror_enabled", True)):
        return None
    owner_daily = getattr(paths, "owner_memory_daily_dir", None)
    return (owner_daily,) if owner_daily else None


def _build_subagent_manager(agent: SimpleAgent, paths: dict) -> SubAgentManager:
    manager = SubAgentManager(
        paths["subagent_workspace"],
        local_store=agent.local_store,
        collaboration_store=agent.collaboration_store,
        conversation_store=agent.conversation_store,
        workspace_root=agent.root,
        workspace_roots=agent.workspace_roots,
        role_template_dirs=agent.config.subagent_role_template_dirs,
        enable_self_learning=agent.config.enable_self_learning,
        debug_trace_level=agent.config.subagent_debug_trace_level,
        takeover_chain_max_depth=agent.config.subagent_takeover_chain_max_depth,
        closeout_for_all_task_nodes=agent.config.closeout_for_all_task_nodes,
        owner_id=str(getattr(agent.home_paths, "owner_id", "") or ""),
        owner_home_dir=str(getattr(agent.home_paths, "owner_home_dir", "") or ""),
        owner_policy_snapshot=agent.owner_policy.to_dict(),
    )
    manager.home_paths = agent.home_paths
    return manager


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
            path_access_mode=config.path_access_mode,
            path_dangerous_roots=config.path_dangerous_roots,
            access_mode=config.access_mode,
            tool_write_inline_max_chars=config.tool_write_inline_max_chars,
            artifact_read_budget_window_seconds=config.tool_artifact_read_budget_window_seconds,
            artifact_read_budget_max_chars=config.tool_artifact_read_budget_max_chars,
            artifact_default_read_chars=config.memory_artifact_default_read_chars,
            payload_limits=tool_payload_limits_from_config(config),
            disabled_tools=list(getattr(agent.owner_policy, "disabled_tools", ())),
            artifact_root=runtime_owner_root(agent),
            runtime_guard_policy=getattr(agent, "runtime_guard_policy", None),
            mcp_servers=dict(getattr(config, "mcp_servers", {}) or {}),
        )
    )


def _register_orchestration_tools(agent: SimpleAgent) -> None:
    agent.tools.register(CapabilityRequestTool(agent))
    agent.tools.register(RaiseEventTool(agent))
    agent.tools.register(TaskProgressTool(agent))
    # skill 树第一期:skill_search 检索台(千级冷路,prompt 零索引成本)。
    agent.tools.register(SkillSearchTool(agent))
    # 历史检索台(对标 长期助手 session_search 三模式):封装 LocalStore 的 FTS5/最近列表/
    # 时间窗,让模型能查/翻本地历史记录(记忆、产物、归档),零 LLM 成本纯读。
    agent.tools.register(SessionSearchTool(agent))
    # 长期记忆写入:用户明确要求记住偏好/事实时落 owner memory(对标 长期助手 memory_tool)。
    agent.tools.register(RememberTool(agent))
    # 自学习 skill 草稿(对标 长期助手,但更保守):仅 enable_self_learning 时暴露——默认关闭=零打扰,
    # 且 agent 只产 data/skill_drafts 草稿、绝不直接改正式 skill 库(AGENTS.md 自学习约束)。
    if bool(getattr(getattr(agent, "config", None), "enable_self_learning", False)):
        agent.tools.register(CreateSkillTool(agent))
    agent.tools.register(RaiseCollaborationTool(agent))
    agent.tools.register(InspectCollaborationTool(agent))
    agent.tools.register(SubmitCollaborationResultTool(agent))
    agent.tools.register(UpdateCollaborationTool(agent))
    if not agent.config.enable_subagents:
        return
    agent.tools.register(CreateSubagentsTool(agent))
    agent.tools.register(CancelSubagentsTool(agent))
    agent.tools.register(InspectAgentTreeTool(agent))
    agent.tools.register(WaitTool(agent))
    agent.tools.register(SendGuidanceTool(agent))
    agent.tools.register(DispatchSubagentsTool(agent))
    agent.tools.register(ResolveCapabilityRequestsTool(agent))
    agent.tools.register(ScheduleChildSubagentsTool(agent))


__all__ = [
    "AgentRunResult",
    "CapabilityRequestTool",
    "CancelSubagentsTool",
    "ResolveCapabilityRequestsTool",
    "CODING_SUBAGENT_TOOLS",
    "CreateSubagentsTool",
    "DispatchSubagentsTool",
    "InspectAgentTreeTool",
    "InspectCollaborationTool",
    "RaiseCollaborationTool",
    "RaiseEventTool",
    "ONE_SHOT_TOOL_NAMES",
    "PARENT_PLANNER_READ_TOOLS",
    "READ_ONLY_SUBAGENT_TOOLS",
    "ScheduleChildSubagentsTool",
    "SendGuidanceTool",
    "SimpleAgent",
    "TaskProgressTool",
    "SubmitCollaborationResultTool",
    "UpdateCollaborationTool",
    "WaitTool",
]
