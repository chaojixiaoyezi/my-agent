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
from .agent_core.runner.context import ThreadLocalAgentAttribute, current_subagent_run_id
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
from .agent_core.runtime.record_finding_tool import RecordFindingTool
from .backends import get_backend
from .capability import CapabilityRouter, from_tool_spec
from .capability.channel_message_tool import SendMessageTool
from .capability.create_skill_tool import CreateSkillTool
from .capability.memory_tool import RememberTool
from .capability.network_authorization_tool import AuthorizeNetworkHostTool
from .capability.persona_repository import PersonaRepository
from .capability.persona_tool import UpdatePersonaTool
from .capability.runtime_config_reload import default_capability_config_path
from .capability.session_search_tool import SessionSearchTool
from .capability.skill_search_tool import SkillSearchTool
from .capability.skill_service import SkillsService
from .collaboration import (
    CollaborationStore,
    InspectCollaborationTool,
    RaiseCollaborationTool,
    SubmitCollaborationResultTool,
    UpdateCollaborationTool,
)
from .conversation import ConversationStore
from .conversation.authority import CONVERSATION_REQUEST_ID_ATTR
from .conversation.goal_tools import CreateGoalTool, GetGoalTool, UpdateGoalTool
from .delivery import DeliveryContext, DeliveryService, build_default_channel_registry
from .extensions import load_extension_registry
from .gateway_parts.channel_health import adapter_runtime_health
from .ingestion.watch_tool import WatchStreamTool
from .local_storage import LocalStore
from .memory_store import JsonlMemory
from .prompting_parts import PromptBuilder
from .scheduler import SchedulerRepository, SchedulerService, ScheduleTool
from .settings import AgentConfig
from .settings.runtime_guard_config import runtime_guard_policy
from .subagents.manager import SubAgentManager
from .tooling.registry import ToolRegistry, ToolRegistryParams
from .tooling.registry_payload_normalize import tool_payload_limits_from_config
from .tooling.vision_tools import vision_config_from_agent_config
from .user_space.home_indexes import register_owner_ref
from .user_space.home_layout import ensure_my_agent_home, home_paths
from .user_space.owner_policy import resolve_effective_owner_policy
from .user_space.owner_quota import OwnerQuotaEnforcer
from .user_space.owner_resolver import (
    ensure_owner_home,
    home_paths_with_owner,
    owner_identity_from_config,
)
from .user_space.runtime_paths import (
    apply_runtime_paths_to_config,
    resolve_runtime_paths_for_agent,
)
from .user_space.temporary_grants import has_active_capability_grant


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

    # 同一 owner 的 agent 会被多个 Gateway/后台 worker 复用；这些运行中字段必须按线程隔离，
    # 否则普通聊天会读到后台任务的 task/workspace，或两个会话互相覆盖工具上下文。
    _current_user_prompt = ThreadLocalAgentAttribute("_current_user_prompt")
    _current_run_params = ThreadLocalAgentAttribute("_current_run_params")
    _current_run_task_workspace = ThreadLocalAgentAttribute("_current_run_task_workspace")
    _current_tool_loop_params = ThreadLocalAgentAttribute("_current_tool_loop_params")
    _current_skill_snapshot = ThreadLocalAgentAttribute("_current_skill_snapshot")

    def __init__(
        self, config: AgentConfig, root: str | Path, workspace_roots: list[str | Path] | None = None
    ):
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
        # LLM: prompt 和工具必须共享同一份结构化工作区事实。远程 owner 只能看到自己的
        #   owner home；本地管理员仍使用启动项目目录。不要再从 self.root 各自推导。
        # 人类: 先算一次唯一工作区，避免提示词说 service-cwd、工具实际写 owner home。
        self.effective_workspace_root, self.effective_workspace_roots = _effective_workspace_scope(
            self, config
        )
        self.owner_quota = _build_owner_quota_enforcer(self)
        self.runtime_path_resolution = resolve_runtime_paths_for_agent(
            config, self.root, self.home_paths
        )
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
            embedder=_build_memory_embedder(config),  # 记忆语义召回(检索拓宽 #1);默认关返 None
            quota_enforcer=self.owner_quota,
        )
        self.persona_repository = PersonaRepository.from_home_paths(
            self.home_paths,
            quota_enforcer=self.owner_quota,
        )
        self.prompts = PromptBuilder(
            config,
            self.root,
            home_paths=self.home_paths,
            workspace_root=self.effective_workspace_root,
            persona_repository=self.persona_repository,
        )
        self.skills_service = SkillsService(
            home_paths=self.home_paths,
            workspace_root=self.effective_workspace_root,
            policy_provider=lambda: resolve_effective_owner_policy(self.home_paths),
        )
        self.capability_router = CapabilityRouter(
            config=config,
            skill_snapshot_provider=self.current_skill_snapshot,
        )
        self.prompts.capability_router = self.capability_router
        self.backend = get_backend(config.model_backend, config)
        self.conversation_store = ConversationStore(paths["conversation_workspace"])
        self.collaboration_store = CollaborationStore(paths["collaboration_workspace"])
        self.scheduler_repository = SchedulerRepository(
            self.home_paths.owner_scheduler_dir,
            owner_provider=self.home_paths.owner_provider or "local",
            owner_kind=self.home_paths.owner_kind or "main",
            owner_id=self.home_paths.owner_id or "local/main",
            default_timezone=str(getattr(config, "timezone", "") or ""),
            quota_enforcer=self.owner_quota,
        )
        self.scheduler_service = SchedulerService(
            self.scheduler_repository,
            conversation_store=self.conversation_store,
            skill_snapshot_provider=self.current_skill_snapshot,
        )
        self.subagents = _build_subagent_manager(self, paths)
        self.channel_registry = build_default_channel_registry(
            config,
            runtime_health_provider=lambda: adapter_runtime_health(self),
        )
        self.delivery_service = DeliveryService(self.channel_registry)
        self.tools = _build_tool_registry(self, config)
        self.extensions = load_extension_registry(config.extension_plugins)
        self.extensions.activate_agent(self)
        _register_orchestration_tools(self)
        for spec in self.tools.specs():
            self.capability_router.register(from_tool_spec(spec))

    def current_skill_snapshot(self):
        """Return the immutable Skill catalog bound to this worker turn."""

        current = getattr(self, "_current_skill_snapshot", None)
        if current is not None:
            return current
        workspace = getattr(self, "_current_run_task_workspace", "") or self.effective_workspace_root
        return self.skill_snapshot_for_run_scope(workspace)

    def skill_snapshot_for_run_scope(self, workspace_root):
        """Bind the owner snapshot, then narrow delegated runners to explicit grants."""

        snapshot = self.skills_service.snapshot_for(workspace_root)
        current = getattr(self, "_current_run_params", None)
        attrs = getattr(current, "task_attributes", None) if current is not None else None
        refs = attrs.get("skill_snapshot_refs") if isinstance(attrs, dict) else None
        if isinstance(refs, list) and refs:
            expected: dict[str, str] = {}
            references: list[str] = []
            _add_skill_snapshot_hashes(expected, refs)
            for row in refs:
                if isinstance(row, dict) and str(row.get("stable_id") or "").strip():
                    references.append(str(row["stable_id"]).strip())
            snapshot = snapshot.restricted(references, expected_sha256=expected)
        run_id = current_subagent_run_id(self)
        if not run_id:
            return snapshot
        task = self.subagents.load(run_id)
        references = list(getattr(task, "allowed_skills", None) or [])
        expected = _task_skill_snapshot_hashes(task)
        return snapshot.restricted(references, expected_sha256=expected)

    # LLM: This is the composition-root query used by status and stop; request linkage comes
    # from typed task attributes, with current runtime roots only as a compatibility bridge.
    # 函数用途：列出某个会话请求真正派生的子代理编号。
    def subagent_run_ids_for_request(self, request_id: str) -> list[str]:
        request_key = str(request_id or "").strip()
        if not request_key:
            return []
        scope_ids = {request_key}
        current = getattr(self, "_current_run_params", None)
        if str(getattr(current, "request_id", "") or "").strip() == request_key:
            attrs = getattr(current, "task_attributes", None)
            if isinstance(attrs, dict):
                scope_ids.add(str(attrs.get("conversation_task_id") or "").strip())
            scope_ids.add(str(getattr(current, "run_id", "") or "").strip())
            scope_ids.add(str(getattr(current, "task_id", "") or "").strip())
        scope_ids.discard("")
        run_ids: list[str] = []
        for task in self.subagents.list_runs():
            task_id = str(getattr(task, "id", "") or "").strip()
            attrs = getattr(task, "attributes", None)
            linked_request = (
                str(attrs.get(CONVERSATION_REQUEST_ID_ATTR) or "").strip()
                if isinstance(attrs, dict)
                else ""
            )
            lineage = {
                task_id,
                str(getattr(task, "root_id", "") or "").strip(),
                str(getattr(task, "parent_id", "") or "").strip(),
            }
            if task_id and (linked_request == request_key or bool(scope_ids.intersection(lineage))):
                run_ids.append(task_id)
        return list(dict.fromkeys(run_ids))

    # LLM: Gateway and CLI cancel exact run ids supplied by the typed request-lineage query;
    # they never import orchestration tools across their enforced layer boundaries.
    # 函数用途：取消某个会话请求派生的活跃子代理树。
    def cancel_request_subagents(
        self,
        request_id: str,
        *,
        reason: str,
        run_ids: list[str] | None = None,
    ) -> object:
        targets = (
            list(run_ids) if run_ids is not None else self.subagent_run_ids_for_request(request_id)
        )
        if not targets:
            return None
        return CancelSubagentsTool(self).execute(
            {
                "run_ids": targets,
                "status": ["PLANNING", "PENDING", "RUNNING", "BLOCKED", "PAUSED"],
                "reason": reason,
                "kill_process": True,
            }
        )


def _embedding_api_key(config: AgentConfig) -> str:
    """embedding 端点的 key:独立直配 > 独立 env 变量 > 回退聊天后端 key。

    独立字段让 embedding 用与聊天不同厂的 key(如聊天 MiniMax、embedding 另一家);都不配则零配置
    沿用聊天 key(同厂/本地无 key 场景)。_env 路径让生产把密钥放环境变量而非 yaml(免明文入库)。
    """
    direct = str(getattr(config, "memory_embedding_api_key", "") or "")
    if direct:
        return direct
    env_name = str(getattr(config, "memory_embedding_api_key_env", "") or "")
    if env_name and os.environ.get(env_name):
        return os.environ[env_name]
    return str(getattr(config, "api_key", "") or "") or os.environ.get(
        str(getattr(config, "api_key_env", "AGENT_API_KEY") or "AGENT_API_KEY"), ""
    )


def _build_memory_embedder(config: AgentConfig):
    """记忆语义召回的 embedder(检索拓宽 #1):默认关 / 没配 embedding 模型 → None(纯关键词,不变)。

    配了 memory_semantic_recall=true + memory_embedding_model 才建,走 agent 同款 api_base/key
    (OpenAI 兼容 /embeddings)。建失败/未配一律 None,记忆召回降级纯关键词不崩。
    """
    if not getattr(config, "memory_semantic_recall", False):
        return None
    model = str(getattr(config, "memory_embedding_model", "") or "").strip()
    if not model:
        return None
    try:
        from .retrieval.embedding import MiniMaxEmbedder, OpenAICompatibleEmbedder

        api_key = _embedding_api_key(config)
        # embedding 端点常与聊天端点不同(MiniMax 聊天走 /anthropic、embedding 走 /v1);独立配置,缺省沿用主 api_base
        api_base = str(getattr(config, "memory_embedding_api_base", "") or "") or str(
            getattr(config, "api_base", "") or ""
        )
        if model.startswith("embo"):  # MiniMax 原生 embedding(非 OpenAI 兼容,单独适配器)
            return MiniMaxEmbedder(api_base=api_base, model=model, api_key=api_key)
        return OpenAICompatibleEmbedder(api_base=api_base, model=model, api_key=api_key)
    except Exception:
        return None


def _build_tool_embedder(config: AgentConfig):
    """Build the real semantic provider for tool retrieval when explicitly configured."""

    if not getattr(config, "tool_vector_search_enabled", False):
        return None
    model = str(
        getattr(config, "tool_embedding_model", "")
        or getattr(config, "memory_embedding_model", "")
        or ""
    ).strip()
    if not model:
        return None
    from .retrieval.embedding import MiniMaxEmbedder, OpenAICompatibleEmbedder

    api_base = str(
        getattr(config, "tool_embedding_api_base", "")
        or getattr(config, "memory_embedding_api_base", "")
        or getattr(config, "api_base", "")
        or ""
    )
    direct_key = str(getattr(config, "tool_embedding_api_key", "") or "")
    env_name = str(getattr(config, "tool_embedding_api_key_env", "") or "")
    api_key = (
        direct_key
        or (os.environ.get(env_name, "") if env_name else "")
        or _embedding_api_key(config)
    )
    if model.startswith("embo"):
        return MiniMaxEmbedder(api_base=api_base, model=model, api_key=api_key)
    return OpenAICompatibleEmbedder(api_base=api_base, model=model, api_key=api_key)


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
    # 远程 scoped owner 的子代理与主代理同规:工作区=owner home(见 _remote_owner_workspace_override)。
    scoped_workspace = _remote_owner_workspace_override(agent, agent.config)
    workspace_root, workspace_roots = scoped_workspace or (agent.root, agent.workspace_roots)
    manager = SubAgentManager(
        paths["subagent_workspace"],
        local_store=agent.local_store,
        collaboration_store=agent.collaboration_store,
        conversation_store=agent.conversation_store,
        workspace_root=workspace_root,
        workspace_roots=workspace_roots,
        role_template_dirs=agent.config.subagent_role_template_dirs,
        enable_self_learning=agent.config.enable_self_learning,
        debug_trace_level=agent.config.subagent_debug_trace_level,
        takeover_chain_max_depth=agent.config.subagent_takeover_chain_max_depth,
        owner_id=str(getattr(agent.home_paths, "owner_id", "") or ""),
        owner_home_dir=str(getattr(agent.home_paths, "owner_home_dir", "") or ""),
        owner_policy_snapshot=agent.owner_policy.to_dict(),
    )
    manager.home_paths = agent.home_paths
    return manager


# F11①④ 多用户隔离 / admin 降权:main/admin(终端·主代理)也默认 owner-scoped(只看/写自己
#   owner home 子树 + .my-agent 顶层公共区;别人 owner home 由 owner 墙拦,写飞绝对路径由 ①归一
#   重定向)。普通 owner(owner_id 非 main)本就 owner-scoped,行为不变。owner_home_dir 已按 owner
#   解析(main→owners/local/main),空(无 home 上下文)= 不隔离=向后兼容。源码/工作区在
#   workspace_roots 内、不在 .my-agent home 下,owner 墙不碰它们,降权不误伤合法操作。
_ADMIN_BYPASS_CAPABILITY = "owner.full_access"


def _resolve_owner_scope_and_access(agent: SimpleAgent, config: AgentConfig) -> tuple[str, str]:
    """返回 (owner_scope_root, access_mode)。默认降权到自己 owner home;持有有效 admin bypass
    临时授权(owner.full_access,active 未过期)时解除降权——owner_scope_root="" 恢复看所有 owner,
    shell access_mode 提到 full-access 恢复全权运维。无 home 上下文(owner_home_dir 空)保持不隔离。"""
    owner_scope_root = str(getattr(agent.home_paths, "owner_home_dir", "") or "")
    if owner_scope_root and _has_admin_bypass_grant(agent.home_paths):
        return "", "full-access"
    return owner_scope_root, config.access_mode


def _has_admin_bypass_grant(home_paths: object) -> bool:
    # bypass 授权读 my-agent home 根下的 admin_grants 目录(owner-scoped agent 写不到的上级目录),
    # 不读 owner 自己的 temporary_grants——否则降权后 owner"自己家随便造"就能自写一张 full_access
    # 自提权(自授权漏洞)。require_expiry=True:bypass 高权限必须临时,无有效过期时间一律无效。
    directory = getattr(home_paths, "admin_grants_dir", None)
    if directory is None:
        return False
    try:
        return has_active_capability_grant(
            Path(directory), _ADMIN_BYPASS_CAPABILITY, require_expiry=True
        )
    except Exception:  # noqa: BLE001 - 授权读取失败按「无 bypass」处理(保持降权,安全侧默认)
        return False


# 真机逃逸实锤(2026-07-02,飞书用户建站):owner 池把网关的 root(部署机上=源码树)原样传给
#   每个远程用户的 scoped agent → 源码树成了该用户的 workspace_root;而上面 F11 的 owner 墙对
#   workspace_roots 内的路径【有意豁免】(CLI 主代理要在项目目录里干活),两者叠加=飞书用户的
#   子代理把 35+ 个建站文件直接写进源码树(/root/my-agent-src/frontend)。修:远程 provider 的
#   scoped agent 工作区收缩为 owner home 本身(其任务工作区/交付/记忆全在其下,源码树不再是它
#   的合法工作区);main/admin(local provider,终端在项目目录干活)不受影响;admin bypass
#   (owner_scope_root 已解除)保持全权语义。
def _remote_owner_workspace_override(
    agent: SimpleAgent, config: AgentConfig
) -> tuple[Path, list[Path]] | None:
    """远程通道 scoped owner(provider≠local)时返回 (owner_home, [owner_home]);否则 None。"""
    provider = str(getattr(config, "my_agent_owner_provider", "") or "").strip().lower()
    if provider in ("", "local"):
        return None
    owner_scope_root, _access_mode = _resolve_owner_scope_and_access(agent, config)
    if not owner_scope_root:
        return None
    owner_home = Path(owner_scope_root).expanduser().resolve(strict=False)
    return owner_home, [owner_home]


def _effective_workspace_scope(agent: SimpleAgent, config: AgentConfig) -> tuple[Path, list[Path]]:
    """返回 prompt、文件工具和 shell 共用的唯一有效工作区。"""
    workspace_root = agent.root.parent if (agent.root / "__main__.py").exists() else agent.root
    workspace_roots = [
        workspace_root,
        *[root for root in agent.workspace_roots if root != agent.root],
    ]
    if scoped_workspace := _remote_owner_workspace_override(agent, config):
        return scoped_workspace
    return workspace_root, workspace_roots


# LLM: 这是 SimpleAgent 构造 ToolRegistry 的唯一装配入口；workspace、owner policy、工具配置和能力事实必须从同一 AgentConfig 注入。
# 函数用途: 按当前 Agent 的工作区、权限和配置创建工具注册表，让工具执行与能力自我描述看到同一份运行事实。
def _build_tool_registry(agent: SimpleAgent, config: AgentConfig) -> ToolRegistry:
    workspace_root = agent.effective_workspace_root
    workspace_roots = agent.effective_workspace_roots
    owner_scope_root, access_mode = _resolve_owner_scope_and_access(agent, config)
    return ToolRegistry(
        ToolRegistryParams(
            workspace_root=workspace_root,
            workspace_roots=workspace_roots,
            owner_scope_root=owner_scope_root,
            protected_persona_root=_protected_persona_root(agent),
            owner_quota_max_bytes=(
                max(0, int(getattr(agent.owner_policy, "max_disk_mb", 0))) * 1024 * 1024
                if owner_scope_root
                else 0
            ),
            owner_quota_policy_available=not any(
                str(error.get("context") or "") == "owner_policy.quota"
                for error in getattr(agent.owner_policy, "load_errors", ())
                if isinstance(error, dict)
            ),
            max_chars=config.tool_read_max_chars,
            max_entries=config.tool_list_max_entries,
            max_matches=config.tool_search_max_matches,
            web_max_chars=config.tool_web_max_chars,
            http_timeout=config.tool_http_timeout,
            catalog_limit=config.tool_catalog_limit,
            catalog_mode=config.tool_catalog_mode,
            catalog_offset=config.tool_catalog_offset,
            catalog_categories=config.tool_catalog_categories,
            catalog_deferred_categories=config.tool_catalog_deferred_categories,
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
            access_mode=access_mode,
            tool_write_inline_max_chars=config.tool_write_inline_max_chars,
            artifact_read_budget_window_seconds=config.tool_artifact_read_budget_window_seconds,
            artifact_read_budget_max_chars=config.tool_artifact_read_budget_max_chars,
            artifact_default_read_chars=config.memory_artifact_default_read_chars,
            payload_limits=tool_payload_limits_from_config(config),
            disabled_tools=list(getattr(agent.owner_policy, "disabled_tools", ())),
            artifact_root=runtime_owner_root(agent),
            runtime_guard_policy=getattr(agent, "runtime_guard_policy", None),
            mcp_servers=dict(getattr(config, "mcp_servers", {}) or {}),
            lsp_servers=dict(getattr(config, "lsp_servers", {}) or {}),
            vision_config=vision_config_from_agent_config(config),
            tool_embedder=_build_tool_embedder(config),
            capability_config=config,
            channel_registry=agent.channel_registry,
            channel_binding_provider=lambda: _current_delivery_context(agent),
            skill_snapshot_provider=agent.current_skill_snapshot,
            memory_snapshot_provider=agent.memory.runtime_snapshot,
            persona_snapshot_provider=agent.persona_repository.runtime_snapshot,
            scheduler_snapshot_provider=agent.scheduler_service.runtime_snapshot,
        )
    )


def _build_owner_quota_enforcer(
    agent: SimpleAgent,
) -> OwnerQuotaEnforcer | None:
    """Create the one owner admission gate shared by tools and repositories."""

    owner_root = str(getattr(agent.home_paths, "owner_home_dir", "") or "").strip()
    if not owner_root:
        return None
    return OwnerQuotaEnforcer(
        owner_root,
        max_bytes=max(0, int(getattr(agent.owner_policy, "max_disk_mb", 0))) * 1024 * 1024,
        policy_available=not any(
            str(error.get("context") or "") == "owner_policy.quota"
            for error in getattr(agent.owner_policy, "load_errors", ())
            if isinstance(error, dict)
        ),
    )


# LLM: 当前绑定只从 owner-scoped ConversationStore 的结构化 thread binding 读取；不得从 prompt 或配置猜目标。
# 函数用途: 为能力自述提供本轮真实通道类型，目标只留在可信上下文且不会被清单输出。
def _current_delivery_context(agent: SimpleAgent) -> DeliveryContext | None:
    current = getattr(agent, "_current_run_params", None)
    attrs = getattr(current, "task_attributes", None) if current is not None else None
    thread_id = (
        str(attrs.get("conversation_thread_id") or "").strip()
        if isinstance(attrs, dict)
        else ""
    )
    if not thread_id:
        return None
    try:
        thread, load_error = agent.conversation_store.load_thread_report(thread_id)
    except Exception:
        return None
    if load_error is not None or thread is None:
        return None
    bindings = list(getattr(thread, "channel_bindings", ()) or ())
    if not bindings:
        return None
    binding = max(
        bindings,
        key=lambda item: float(getattr(item, "last_active_at", 0.0) or 0.0),
    )
    channel = str(getattr(binding, "channel", "") or "").strip()
    target = str(
        getattr(binding, "channel_user_id", "")
        or getattr(binding, "channel_conversation_id", "")
        or ""
    ).strip()
    if not channel or not target:
        return None
    return DeliveryContext(channel=channel, target=target, mode="proactive", thread_id=thread_id)


def _protected_persona_root(agent: SimpleAgent) -> str:
    """人格确认根不随临时 admin 文件访问豁免消失。"""
    return str(getattr(getattr(agent, "home_paths", None), "owner_home_dir", "") or "")


def _task_skill_snapshot_hashes(task: object) -> dict[str, str]:
    hashes: dict[str, str] = {}
    attrs = getattr(task, "attributes", None)
    if isinstance(attrs, dict):
        _add_skill_snapshot_hashes(hashes, attrs.get("skill_snapshot_refs"))
    for grant in getattr(task, "capability_grants", None) or []:
        _add_skill_snapshot_hashes(hashes, getattr(grant, "capability_cards", None))
    return hashes


def _add_skill_snapshot_hashes(target: dict[str, str], value: object) -> None:
    if not isinstance(value, list):
        return
    for item in value:
        if not isinstance(item, dict):
            continue
        stable_id = str(item.get("stable_id") or "").strip()
        content_sha256 = str(item.get("content_sha256") or "").strip()
        if stable_id and content_sha256:
            target[stable_id] = content_sha256


# LLM: 主代理通用工作工具在这里统一注册；send_message 是唯一通道发送入口，不增加平台专用旁路。
# 函数用途: 把编排、检索、记忆、消息和协作工具装入主代理 registry。
def _register_orchestration_tools(agent: SimpleAgent) -> None:
    agent.tools.register(CapabilityRequestTool(agent))
    agent.tools.register(RaiseEventTool(agent))
    agent.tools.register(TaskProgressTool(agent))
    agent.tools.register(GetGoalTool(agent))
    agent.tools.register(CreateGoalTool(agent))
    agent.tools.register(UpdateGoalTool(agent))
    # skill 树第一期:skill_search 检索台(千级冷路,prompt 零索引成本)。
    agent.tools.register(SkillSearchTool(agent))
    # 历史检索台(对标 长期助手 session_search 三模式):封装 LocalStore 的 FTS5/最近列表/
    # 时间窗,让模型能查/翻本地历史记录(记忆、产物、归档),零 LLM 成本纯读。
    agent.tools.register(SessionSearchTool(agent))
    # 已连接消息通道的原生外发能力:目标固定为当前 owner,附件只接受 owner registry 已登记文件。
    # 对标 长期助手 send_message 与 通道运行时 ReplyPayload(mediaUrls),不再让模型误以为“只能写文件不能发”。
    agent.tools.register(SendMessageTool(agent))
    # 长期记忆写入:记"需要时才想起"的具体事实/事件到 owner memory(对标 长期助手 memory_tool)。
    agent.tools.register(RememberTool(agent))
    # 人格文件写入:用户表达长期人设/画像/称呼/工作约定时,直接落 SOUL/USER/AGENTS.md(每轮注入,
    # 真正塑造每次交互)。区别于 remember——人设走这个,不进 memory(否则模型惯性把称呼/偏好塞进记忆)。
    agent.tools.register(UpdatePersonaTool(agent))
    # owner 持久计划使用同一条会话唤醒链；wait 仍只负责当前运行任务的非阻塞等待。
    agent.tools.register(ScheduleTool(agent))
    # 内网主机出站授权:用户点名的内网监控目标(跨机数据源)经属主确认后进白名单,解除
    # NETWORK_PRIVATE_HOST_BLOCKED;不放松出站闸本身,只接通闸已内置的 allowed_private_hosts。
    agent.tools.register(AuthorizeNetworkHostTool(agent))
    # 高吞吐数据流盯守摄取层:代码层结构化预聚合/初筛/背压把 100+/s 压成候选批,主代理与
    # 盯守子代理共用;游标+统计跨轮/跨补岗持久。初筛只做结构化降维,定性永远留给模型。
    agent.tools.register(WatchStreamTool(agent))
    # 增量结论账(收尾一公里):确认一条结论就持久化一条到 findings.jsonl,收尾崩/重派/
    # 被取消都不丢;整合/收口层从账合并,最终报告只是汇总视图。子代理与主代理长任务共用。
    agent.tools.register(RecordFindingTool(agent))
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
