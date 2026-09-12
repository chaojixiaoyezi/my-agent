"""composition root for SimpleAgent runtime, tools, memory, gateway, and subagents."""

# LLM: core.py 只装配 SimpleAgent 的当前主链；拆出的私有 helper 必须由所属
# 模块直接导入，不在这里保留无人消费的旧 re-export 兼容面。
# 模块用途: 组装模型后端、工具、记忆、会话和子代理，生成可运行的 SimpleAgent。

from __future__ import annotations

import logging
import os
from dataclasses import replace
from pathlib import Path

from .agent_core.capability_request_tool import CapabilityRequestTool
from .agent_core.models import AgentRunResult
from .agent_core.orchestration.dispatch.mixin import SimpleAgentDispatchMixin
from .agent_core.orchestration_tools import (
    CODING_SUBAGENT_TOOLS,
    READ_ONLY_SUBAGENT_TOOLS,
    CancelSubagentsTool,
    CreateSubagentsTool,
    ListAgentsTool,
    ResolveCapabilityRequestsTool,
    execute_cancel_subagents,
)
from .agent_core.parameters import (
    ONE_SHOT_TOOL_NAMES,
)
from .agent_core.planner_service import (
    PARENT_PLANNER_READ_TOOLS,
)
from .agent_core.runner.context import ThreadLocalAgentAttribute, current_subagent_run_id
from .agent_core.runtime.guidance_tool import SendGuidanceTool
from .agent_core.runtime.owner_roots import runtime_owner_root
from .agent_core.runtime_mixin import SimpleAgentRuntimeMixin
from .agent_core.subagent_mixin import SimpleAgentSubagentMixin
from .agent_core.task_progress_tool import TaskProgressTool
from .backends import get_backend
from .capability import CapabilityRouter, from_tool_model_spec
from .capability.channel_message_tool import SendMessageTool
from .capability.memory_tool import RememberTool
from .capability.persona_repository import PersonaRepository
from .capability.persona_tool import UpdatePersonaTool
from .capability.runtime_config_reload import default_capability_config_path
from .capability.session_search_tool import SessionSearchTool
from .capability.skill_search_tool import SkillSearchTool
from .capability.skill_service import SkillsService
from .collaboration import CollaborationStore
from .common.audit_activation import AUDIT_RUN_EPOCH_ATTR
from .contracts.model_call_ledger import ModelCallLedger
from .conversation import ConversationStore
from .conversation.audit_tools import PublishAuditUpdateTool
from .conversation.authority import CONVERSATION_REQUEST_ID_ATTR
from .conversation.goal_tools import CreateGoalTool, GetGoalTool, UpdateGoalTool
from .conversation.named_work import StopNamedWorkTool
from .delivery import (
    DeliveryContext,
    DeliveryService,
    RuntimeHealthProvider,
    build_default_channel_registry,
)
from .extensions import load_extension_registry
from .gateway_parts.channel_health import adapter_runtime_health
from .ingestion.watch_tool import WatchStreamTool
from .local_storage import LocalStore
from .memory_archive.control_plane import (
    MemoryControlPlaneQueryOptions,
    query_memory_control_plane,
)
from .memory_store import JsonlMemory
from .memory_store.candidates import CandidateService
from .memory_store.curator import (
    MemoryCuratorDependencies,
    MemoryCuratorIdentity,
    MemoryCuratorService,
)
from .memory_store.curator_formal import FormalMemorySource
from .memory_store.curator_inputs import CuratorToolReferenceSource
from .memory_store.curator_models import MemoryCuratorConfig
from .memory_store.curator_run_log import CuratorRunLog
from .memory_store.curator_state import MemoryCuratorStateStore
from .memory_store.daily import DailyMemoryStore
from .memory_store.lessons import HotRuleRepository, LessonRepository
from .memory_store.migration import MemoryMigrationService
from .memory_store.promotion import (
    ConversationMessageEvidenceVerifier,
    LocalStoreToolEvidenceVerifier,
    MemoryPromotionDependencies,
    MemoryPromotionPolicy,
    MemoryPromotionService,
)
from .prompting_parts import PromptBuilder
from .runtime_db.operation_store_selector import select_operation_store
from .scheduler import SchedulerDueIndex, SchedulerRepository, SchedulerService, ScheduleTool
from .settings import AgentConfig
from .settings.model_scope import ModelScopedAttribute
from .settings.runtime_guard_config import runtime_guard_policy
from .subagents.manager import SubAgentManager
from .tooling.computer_use_profile import computer_use_mcp_servers
from .tooling.gateway_status import GatewayStatusTool
from .tooling.registry import ToolRegistry, ToolRegistryParams
from .tooling.user_config_tool import UserConfigTool
from .user_space.home_indexes import register_owner_ref
from .user_space.home_layout import ensure_my_agent_home
from .user_space.home_root import configured_home_root
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


# LLM: Curator provider/model selection reuses the canonical backend factory and config credentials;
# it must not create a second agent loop or tool registry.
# 函数用途: 为无工具后台策展构造独立非流式模型适配器，并返回解析后的 provider/model 标识。
def _build_memory_curator_backend(
    config: AgentConfig,
    curator_config: MemoryCuratorConfig,
) -> tuple[object, str, str]:
    provider = (
        str(config.model_backend)
        if curator_config.provider in {"", "auto"}
        else curator_config.provider
    )
    model = curator_config.model or str(config.model_name)
    scoped_config = replace(
        config,
        model_backend=provider,
        model_name=model,
        stream_enabled=False,
    )
    return get_backend(provider, scoped_config), provider, model


# LLM: The composition root is the only adapter allowed to join Memory Store with the existing
# archive control plane; Curator receives bounded metadata and never imports the archive layer.
# 函数用途: 按精确 run_id 查询当前 owner 的工具产物索引，并保持内部层依赖单向。
def _query_curator_tool_references(
    owner_root: Path,
    run_id: str,
    limit: int,
) -> dict[str, object]:
    return query_memory_control_plane(
        owner_root,
        MemoryControlPlaneQueryOptions(run_id=run_id, limit=limit),
    )


# LLM: Formal Memory, Candidate and Persona authorities are constructed once from canonical owner paths before prompts/tools.
# 函数用途: 为 composition root 接通 LocalStore 派生索引、唯一候选账本、长期记忆和 Persona 仓库。
def _wire_memory_authorities(
    agent: object,
    config: AgentConfig,
    paths: dict[str, object],
) -> None:
    agent.local_store = LocalStore(
        paths["local_store_path"],
        files_dir=paths["local_store_files_dir"],
        events_path=paths["local_store_events_path"],
        enable_fts=config.local_store_fts_enabled,
    )
    agent.memory_candidates = CandidateService(
        agent.home_paths.owner_memory_candidates_jsonl,
        quota_enforcer=agent.owner_quota,
    )
    semantic_status: dict[str, str] = {}
    embedder = _build_memory_embedder(config, diagnostics=semantic_status)
    agent.memory = JsonlMemory(
        paths["memory_path"],
        local_store=agent.local_store,
        ops_path=getattr(agent.home_paths, "owner_memory_ops_jsonl", None),
        candidate_service=agent.memory_candidates,
        embedder=embedder,
        semantic_status=semantic_status,
        quota_enforcer=agent.owner_quota,
    )
    agent.persona_repository = PersonaRepository.from_home_paths(
        agent.home_paths,
        quota_enforcer=agent.owner_quota,
    )


# LLM: Composition is kept outside SimpleAgent.__init__; every repository is the existing owner
# authority and the Curator dependency bundle contains no model-facing tools.
# 函数用途: 为一个已建立 ConversationStore/Memory/Persona 的 agent 接通统一后台策展链。
def _wire_memory_curator(agent: object, config: AgentConfig) -> None:
    agent.memory_lessons = LessonRepository(
        agent.home_paths.owner_memory_lessons_dir,
        agent.home_paths.owner_memory_routing_index_md,
    )
    agent.memory_hot = HotRuleRepository(agent.home_paths.owner_memory_hot_md)
    agent.memory_promotion = MemoryPromotionService(
        dependencies=MemoryPromotionDependencies(
            candidates=agent.memory_candidates,
            long_term=agent.memory,
            persona=agent.persona_repository,
            lessons=agent.memory_lessons,
            hot=agent.memory_hot,
            message_verifier=ConversationMessageEvidenceVerifier(agent.conversation_store),
            tool_verifier=LocalStoreToolEvidenceVerifier(
                agent.local_store,
                owner_id=str(agent.home_paths.owner_id or "local/main"),
                owner_root=agent.home_paths.owner_home_dir,
            ),
        ),
        policy=MemoryPromotionPolicy(
            lesson_min_occurrences=config.memory_lesson_min_occurrences,
            hot_min_occurrences=config.memory_hot_min_occurrences,
        ),
    )
    curator_config = MemoryCuratorConfig.from_agent_config(config)
    backend, provider, model = _build_memory_curator_backend(config, curator_config)
    daily_store = DailyMemoryStore(
        agent.home_paths.owner_memory_daily_dir,
        quota_enforcer=agent.owner_quota,
    )
    state_store = MemoryCuratorStateStore(
        agent.home_paths.owner_memory_curator_state_json
    )
    run_log = CuratorRunLog(agent.home_paths.owner_memory_curator_runs_dir)
    agent.memory_curator = MemoryCuratorService(
        config=curator_config,
        dependencies=MemoryCuratorDependencies(
            backend=backend,
            conversation_store=agent.conversation_store,
            audit_dir=agent.home_paths.owner_audit_dir,
            state_store=state_store,
            daily_store=daily_store,
            candidate_service=agent.memory_candidates,
            run_log=run_log,
            identity=MemoryCuratorIdentity(
                provider=provider,
                model=model,
                owner_id=str(agent.home_paths.owner_id or "local/main"),
                timezone_name=str(getattr(config, "timezone", "") or ""),
            ),
            formal_memory_source=FormalMemorySource(
                agent.memory,
                agent.memory_lessons,
                agent.memory_hot,
            ),
            tool_reference_source=CuratorToolReferenceSource(
                agent.home_paths.owner_home_dir,
                query=_query_curator_tool_references,
            ),
            promotion_callback=lambda candidate_id: agent.memory_promotion.promote(
                candidate_id,
                automatic=True,
            ),
            # 升级自愈:curator 每次持 lease 执行前自动应用 v2 迁移(幂等),旧数据不再能瘫痪提炼。
            migration_service=MemoryMigrationService(
                home_paths=agent.home_paths,
                candidates=agent.memory_candidates,
                long_term=agent.memory,
                daily=daily_store,
                lessons=agent.memory_lessons,
                legacy_workspace_roots=tuple(
                    root
                    for root in (
                        getattr(agent, "effective_workspace_roots", ())
                        or getattr(agent, "workspace_roots", ())
                        or ()
                    )
                    if root
                ),
            ),
        ),
    )


# LLM: 模型与工具权限依赖按执行作用域覆盖；其它 owner/runtime/存储依赖保持原权威对象，不复制整套 Agent。
# 类用途: 组装主代理，让同用户切模型或权限不会热改正在工作的其它线程。
class SimpleAgent(
    SimpleAgentRuntimeMixin,
    SimpleAgentSubagentMixin,
    SimpleAgentDispatchMixin,
):
    """wires config, memory, prompts, backend, tools, and subagent manager into one agent runtime.

    这是用户和 CLI 看到的主代理对象。
    它自己只做依赖组装；具体怎么聊天、怎么跑子代理和怎么由系统调度，已经分别交给 mixin 文件。
    """

    # 同一 owner 的 agent 会被多个 Gateway/后台 worker 复用；这些运行中字段必须按线程隔离，
    # 否则普通聊天会读到后台任务的 task/workspace，或两个会话互相覆盖工具上下文。
    _current_user_prompt = ThreadLocalAgentAttribute("_current_user_prompt")
    _current_run_params = ThreadLocalAgentAttribute("_current_run_params")
    _current_run_task_workspace = ThreadLocalAgentAttribute("_current_run_task_workspace")
    _current_tool_loop_params = ThreadLocalAgentAttribute("_current_tool_loop_params")
    _current_skill_snapshot = ThreadLocalAgentAttribute("_current_skill_snapshot")
    config = ModelScopedAttribute("config")
    backend = ModelScopedAttribute("backend")
    prompts = ModelScopedAttribute("prompts")
    tools = ModelScopedAttribute("tools")

    def __init__(
        self,
        config: AgentConfig,
        root: str | Path,
        workspace_roots: list[str | Path] | None = None,
        *,
        channel_runtime_health_provider: RuntimeHealthProvider | None = None,
    ):
        """initialize all SimpleAgent collaborators and register orchestration tools.

        创建主代理时会准备本地账本、记忆、prompt 构造器、模型后端、子代理管理器和工具注册表。
        最后只注册创建、查看、发消息/纠偏、中断取消和能力裁决等用户级子代理操作。
        """
        self.config = config
        _export_model_endpoint_env(config)
        self.runtime_guard_policy = runtime_guard_policy()
        # 同一 owner 的 Gateway/后台 worker 会并发共用 agent；启动时只建一份线程安全
        # 模型调用账本，避免首次并发请求各建一份后互相覆盖。
        self._model_call_ledger = ModelCallLedger()
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.capability_config_path = default_capability_config_path(self.root)
        self._capability_config_runtime_snapshot = None
        self._orchestration_run_ids_seen: set[str] = set()
        self.workspace_roots = _normalized_workspace_roots(self.root, workspace_roots)

        self.home_paths = _resolve_home_paths(config)
        self.owner_policy = resolve_effective_owner_policy(self.home_paths)
        # LLM: prompt 和工具必须共享同一份结构化工作区事实。所有未获 Full Access 的
        # owner（含本地管理员）都以自己的 owner home 为唯一 workspace；进程启动 cwd
        # 不是权限来源。不要再从 self.root 各自推导。
        # 人类: 先算一次唯一工作区，避免从 /root 启动 TUI 就把 /root 误当任务目录。
        self.effective_workspace_root, self.effective_workspace_roots = _effective_workspace_scope(
            self, config
        )
        self.owner_quota = _build_owner_quota_enforcer(self)
        # LLM: Durable runtime paths must hash and resolve from the same effective workspace used
        # by prompts and tools. Using the constructor's process/root hint here creates a second
        # subagent/conversation home that CLI workers cannot find under WorkspaceOnly.
        # 人类: 子代理、会话和本地账本跟随已经裁决过的工作区，不能再偷偷使用启动目录。
        self.runtime_path_resolution = resolve_runtime_paths_for_agent(
            config, self.effective_workspace_root, self.home_paths
        )
        paths = self.runtime_path_resolution.paths
        apply_runtime_paths_to_config(config, self.runtime_path_resolution)
        _wire_memory_authorities(self, config, paths)
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
        from .settings.thread_model_selection import default_model_profile_id

        self.conversation_store = ConversationStore(
            paths["conversation_workspace"], model_default=lambda: default_model_profile_id(self),
        )
        _wire_memory_curator(self, config)
        self.collaboration_store = CollaborationStore(paths["collaboration_workspace"])
        self.scheduler_repository = SchedulerRepository(
            self.home_paths.owner_scheduler_dir,
            owner_provider=self.home_paths.owner_provider or "local",
            owner_kind=self.home_paths.owner_kind or "main",
            owner_id=self.home_paths.owner_id or "local/main",
            due_owner_id=str(getattr(config, "my_agent_owner_id", "") or "main"),
            default_timezone=str(getattr(config, "timezone", "") or ""),
            quota_enforcer=self.owner_quota,
            due_index=SchedulerDueIndex(
                self.home_paths.global_index_dir / "scheduler_due.sqlite3"
            ),
        )
        self.scheduler_service = SchedulerService(
            self.scheduler_repository,
            conversation_store=self.conversation_store,
            skill_snapshot_provider=self.current_skill_snapshot,
        )
        self.subagents = _build_subagent_manager(self, paths)
        # Adapter daemon health belongs to the shared gateway process, while channel binding,
        # conversations and credentials remain owner-scoped.  A scoped agent therefore inherits
        # only this read-only health provider from its composition root; it never shares a registry
        # or a DeliveryContext with another owner.
        self._channel_runtime_health_provider = channel_runtime_health_provider or (
            lambda: adapter_runtime_health(self)
        )
        self.channel_registry = build_default_channel_registry(
            config,
            runtime_health_provider=self._channel_runtime_health_provider,
        )
        self.delivery_service = DeliveryService(self.channel_registry)
        self.tools = _build_tool_registry(self, config)
        self.extensions = load_extension_registry(config.extension_plugins)
        self.extensions.activate_agent(self)
        _register_orchestration_tools(self)
        for spec in self.tools.specs():
            self.capability_router.register(from_tool_model_spec(spec))

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
        current_audit_epoch = _active_audit_run_epoch(self, request_key)
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
            if (
                task_id
                and (linked_request == request_key or bool(scope_ids.intersection(lineage)))
                and _task_matches_audit_run_epoch(task, current_audit_epoch)
            ):
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
        return execute_cancel_subagents(
            self,
            {
                "run_ids": targets,
                "status": ["PLANNING", "PENDING", "RUNNING", "BLOCKED", "PAUSED"],
                "reason": reason,
                "kill_process": True,
                # 调用方已经在唯一 creation_guard 内复读完整 request lineage；
                # 每个 exact run 只收口一次，避免非可重入 guard 嵌套。
                "cascade_descendants": False,
            }
        )


def _active_audit_run_epoch(agent: object, task_id: str) -> int | None:
    """Return the current epoch only when this id is one active named Audit."""

    store = getattr(agent, "conversation_store", None)
    loader = getattr(store, "load_task_link", None)
    if not callable(loader):
        return None
    try:
        link = loader(task_id)
    except Exception:
        # A lineage read is authorization/state selection.  On corruption do
        # not broaden the query to historical runs.
        return -1
    if link is None:
        return None
    if (
        str(getattr(link, "task_id", "") or "").strip() != task_id
        or str(getattr(link, "work_kind", "") or "").strip().lower() != "audit"
        or str(getattr(link, "status", "") or "").strip().lower() != "active"
    ):
        return None
    try:
        return max(0, int(getattr(link, "run_epoch", 0) or 0))
    except (TypeError, ValueError):
        return -1


def _task_matches_audit_run_epoch(task: object, expected: int | None) -> bool:
    if expected is None:
        return True
    if expected < 0:
        return False
    attrs = getattr(task, "attributes", None)
    if not isinstance(attrs, dict):
        return expected == 0
    try:
        observed = max(0, int(attrs.get(AUDIT_RUN_EPOCH_ATTR) or 0))
    except (TypeError, ValueError):
        return False
    return observed == expected


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


# LLM: 构建失败仅影响派生向量检索；诊断必须区分未开启、未配置和初始化失败，不记录密钥或异常正文。
# 函数用途: 创建可选记忆向量接口，向健康面板报告降级原因，不中断正式记忆读写。
def _build_memory_embedder(config: AgentConfig, *, diagnostics: dict[str, str] | None = None):
    """记忆语义召回的 embedder(检索拓宽 #1):默认关 / 没配 embedding 模型 → None(纯关键词,不变)。

    配了 memory_semantic_recall=true + memory_embedding_model 才建。
    失败保留纯关键词召回，同时发出结构化诊断和不含凭据的警告。
    """
    status = diagnostics if diagnostics is not None else {}
    status.update(state="disabled")
    if not getattr(config, "memory_semantic_recall", False):
        return None
    model = str(getattr(config, "memory_embedding_model", "") or "").strip()
    if not model:
        status.update(state="degraded", error_code="MEMORY_EMBEDDING_MODEL_MISSING")
        logging.getLogger(__name__).warning("语义记忆已开启，但未配置 embedding 模型；当前使用关键词召回")
        return None
    try:
        from .retrieval.embedding import MiniMaxEmbedder, OpenAICompatibleEmbedder

        api_key = _embedding_api_key(config)
        # embedding 端点常与聊天端点不同(MiniMax 聊天走 /anthropic、embedding 走 /v1);独立配置,缺省沿用主 api_base
        api_base = str(getattr(config, "memory_embedding_api_base", "") or "") or str(
            getattr(config, "api_base", "") or ""
        )
        factory = MiniMaxEmbedder if model.startswith("embo") else OpenAICompatibleEmbedder
        embedder = factory(api_base=api_base, model=model, api_key=api_key)
        status.clear()
        status.update(state="configured")
        return embedder
    except Exception as exc:
        status.update(state="degraded", error_code="MEMORY_EMBEDDING_INIT_FAILED", error_type=type(exc).__name__)
        logging.getLogger(__name__).warning("语义记忆初始化失败（%s）；当前使用关键词召回", type(exc).__name__)
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
    root = configured_home_root(config)
    paths = ensure_my_agent_home(root)
    owner = ensure_owner_home(paths.root, owner_identity_from_config(config))
    _register_owner_ref_if_possible(paths, owner)
    return home_paths_with_owner(paths, owner)


# LLM: Compatibility shim only; home_root.configured_home_root remains the sole implementation.
# 函数用途: 保留旧测试和内部调用名，并把解析统一转交轻量 home 根模块。
def _configured_home_root(config: AgentConfig) -> str | None:
    return configured_home_root(config)


def _register_owner_ref_if_possible(paths, owner) -> None:
    try:
        register_owner_ref(paths, owner)
    except OSError:
        return


# LLM: Child agents inherit the parent's effective project roots but never inherit an absent
# owner wall from an administrator's Full Access session. Child permissions are independently
# capped to WorkspaceWrite and stay inside the structured owner/task scope.
# 函数用途: 创建子代理管理器；共享任务工作区事实，但不把管理员的全盘权限递归传给下级。
def _build_subagent_manager(agent: SimpleAgent, paths: dict) -> SubAgentManager:
    workspace_root = agent.effective_workspace_root
    workspace_roots = agent.effective_workspace_roots
    child_owner_scope_root = str(getattr(agent.home_paths, "owner_home_dir", "") or "")
    manager = SubAgentManager(
        paths["subagent_workspace"],
        local_store=agent.local_store,
        collaboration_store=agent.collaboration_store,
        conversation_store=agent.conversation_store,
        workspace_root=workspace_root,
        workspace_roots=workspace_roots,
        role_template_dirs=agent.config.subagent_role_template_dirs,
        candidate_service=agent.memory_candidates,
        debug_trace_level=agent.config.subagent_debug_trace_level,
        takeover_chain_max_depth=agent.config.subagent_takeover_chain_max_depth,
        owner_id=str(getattr(agent.home_paths, "owner_id", "") or ""),
        owner_home_dir=str(getattr(agent.home_paths, "owner_home_dir", "") or ""),
        # B 切片：配置显式执行模式透传（空 = 挂载逻辑按 home 推断，兼容存量）。
        execution_mode=str(getattr(agent.config, "execution_mode", "") or ""),
        owner_scope_root=child_owner_scope_root,
        owner_policy_snapshot=agent.owner_policy.to_dict(),
    )
    manager.home_paths = agent.home_paths
    return manager


# F11④ 多用户隔离 / admin 降权：任何 owner 默认只看/写自己的 owner home 与 shared。
# 本地 local/main 是结构化管理员身份，但只有显式 full-access 配置才解除 owner 墙；
# 普通 owner 即使私自把配置写成 full-access 也会降回 workspace-write。旧的临时 grant
# 在长驻 Gateway 中会在构造时冻结、到期后仍保留权限，因此不再作为 Full Access 来源。


# LLM: Local/main is the only administrator identity. Full Access must be an explicit structured
# config value; remote owner configuration and natural-language claims cannot lift the owner wall.
# 函数用途: 统一裁决 owner 硬边界和命令权限档位，供工作区、文件工具、Shell 与 Gateway 共用。
def _resolve_owner_scope_and_access(agent: SimpleAgent, config: AgentConfig) -> tuple[str, str]:
    """返回 (owner_scope_root, access_mode)。

    owner home 是普通用户和默认管理员的硬边界。只有 local/main 管理员显式请求
    full-access 才同时解除 owner 墙和 shell 限制；其他 owner 不能仅靠自己的配置
    文本或 owner 目录内文件提权。
    """
    owner_scope_root = str(getattr(agent.home_paths, "owner_home_dir", "") or "")
    requested_access = str(getattr(config, "access_mode", "workspace-write") or "workspace-write")
    requested_access = requested_access.strip().lower().replace("_", "-")
    if requested_access not in {"restricted", "workspace-write", "full-access"}:
        requested_access = "workspace-write"
    if requested_access == "full-access":
        if _is_local_admin_owner(agent.home_paths):
            return "", "full-access"
        requested_access = "workspace-write"
    policy_access = str(
        getattr(getattr(agent, "owner_policy", None), "shell_access_mode", "") or ""
    ).strip().lower().replace("_", "-")
    requested_access = _narrower_access_mode(requested_access, policy_access)
    return owner_scope_root, requested_access


# LLM: Host-admin identity is a structured local/main fact, never a username parsed from chat
# text. Remote identities remain owner-scoped even if their display/user id says "admin".
# 函数用途: 判断当前 owner 是否为本机 TUI 的默认管理员身份。
def _is_local_admin_owner(home_paths: object) -> bool:
    provider = str(getattr(home_paths, "owner_provider", "") or "").strip().lower()
    owner_kind = str(getattr(home_paths, "owner_kind", "") or "").strip().lower()
    return provider in {"", "local"} and owner_kind in {"", "main"}


# LLM: Per-owner policy can only narrow a non-Full host configuration. Unknown policy values are
# ignored rather than treated as grants, and Full Access is decided before this helper.
# 函数用途: 从全局请求和 owner 策略中选出更严格的 Shell 权限档位。
def _narrower_access_mode(requested: str, owner_policy: str) -> str:
    rank = {"restricted": 0, "workspace-write": 1, "full-access": 2}
    if owner_policy not in rank:
        return requested
    if requested not in rank:
        return owner_policy
    return min((requested, owner_policy), key=rank.__getitem__)


# LLM: Process cwd never grants workspace authority. While the owner wall exists, both local and
# remote agents use owner home; only a local/main Full Access registry may retain an explicit root.
# 函数用途: 为 WorkspaceOnly 身份把默认工作区收回 owner home，避免从 /root 启动就污染系统目录。
def _owner_scoped_workspace_override(
    agent: SimpleAgent, config: AgentConfig
) -> tuple[Path, list[Path]] | None:
    """owner 墙有效时返回 (owner_home, [owner_home])；Full Access 时返回 None。"""
    owner_scope_root, _access_mode = _resolve_owner_scope_and_access(agent, config)
    if not owner_scope_root:
        return None
    owner_home = Path(owner_scope_root).expanduser().resolve(strict=False)
    return owner_home, [owner_home]


# LLM: Prompt, filesystem tools and process tools must consume this one normalized workspace view.
# 函数用途: 返回当前代理唯一有效的主工作区和根列表。
def _effective_workspace_scope(agent: SimpleAgent, config: AgentConfig) -> tuple[Path, list[Path]]:
    """返回 prompt、文件工具和 shell 共用的唯一有效工作区。"""
    workspace_root = agent.root.parent if (agent.root / "__main__.py").exists() else agent.root
    workspace_roots = [
        workspace_root,
        *[root for root in agent.workspace_roots if root != agent.root],
    ]
    if scoped_workspace := _owner_scoped_workspace_override(agent, config):
        return scoped_workspace
    return workspace_root, workspace_roots


# LLM: ToolRegistry 唯一装配入口；工作区与工具配置来自同一 AgentConfig，审批读取器只绑定该 Agent 的可信 home。
# 函数用途: 按当前 Agent 的工作区、权限和配置创建工具注册表，让工具执行与能力自我描述看到同一份运行事实。
def _build_tool_registry(agent: SimpleAgent, config: AgentConfig) -> ToolRegistry:
    from functools import partial

    from .user_space.approval_mode import read_approval_mode

    workspace_root = agent.effective_workspace_root
    workspace_roots = agent.effective_workspace_roots
    owner_scope_root, access_mode = _resolve_owner_scope_and_access(agent, config)
    effective_path_access_mode = (
        "full" if access_mode == "full-access" and not owner_scope_root else config.path_access_mode
    )
    mcp_servers = computer_use_mcp_servers(
        getattr(config, "mcp_servers", {}),
        enabled=bool(getattr(config, "computer_use_enabled", False)),
        is_local_admin=_is_local_admin_owner(agent.home_paths),
        access_mode=access_mode,
    )
    return ToolRegistry(
        ToolRegistryParams(
            workspace_root=workspace_root,
            workspace_roots=workspace_roots,
            owner_scope_root=owner_scope_root,
            owner_type=_tool_registry_owner_type(agent),
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
            path_access_mode=effective_path_access_mode,
            path_dangerous_roots=config.path_dangerous_roots,
            access_mode=access_mode,
            tool_write_inline_max_chars=config.tool_write_inline_max_chars,
            artifact_read_budget_window_seconds=config.tool_artifact_read_budget_window_seconds,
            artifact_read_budget_max_chars=config.tool_artifact_read_budget_max_chars,
            artifact_default_read_chars=config.memory_artifact_default_read_chars,
            disabled_tools=list(getattr(agent.owner_policy, "disabled_tools", ())),
            artifact_root=runtime_owner_root(agent),
            artifact_backup_root=agent.home_paths.owner_artifact_backups_dir,
            runtime_guard_policy=getattr(agent, "runtime_guard_policy", None),
            mcp_servers=mcp_servers,
            tool_embedder=_build_tool_embedder(config),
            operation_store=select_operation_store(agent),
            operation_store_required=True,
            operation_owner_id=str(
                getattr(agent.home_paths, "owner_id", "") or "local/main"
            ),
            approval_mode_reader=partial(read_approval_mode, agent.home_paths),
        )
    )


# LLM: manifest 的 owner 类型来自已解析 home identity，不从消息、prompt 或路径字符串猜测。
# 函数用途: 区分本地主代理与远程 user/group owner 的工具权限展示语义。
def _tool_registry_owner_type(agent: SimpleAgent) -> str:
    home_paths = getattr(agent, "home_paths", None)
    provider = str(getattr(home_paths, "owner_provider", "") or "").strip().lower()
    owner_kind = str(getattr(home_paths, "owner_kind", "") or "").strip().lower()
    if provider in {"", "local"} and owner_kind in {"", "main"}:
        return "main_agent"
    return owner_kind or "owner"


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
    # Gateway 状态包含宿主 PID、配置和日志路径，只向本机管理员主代理提供；普通 owner
    # 不注册这项能力，从工具快照源头避免跨用户泄露。
    if str(getattr(agent.tools, "owner_type", "") or "") == "main_agent":
        agent.tools.register(GatewayStatusTool(agent))
        # 用户级配置的受控入口：读生效值/来源，改白名单项；安全边界不可写。
        # 真机问题：用户问"能不能改 compact 阈值"，模型没有任何入口，只能凭空答"我没权限"。
        agent.tools.register(UserConfigTool(agent))
    agent.tools.register(CapabilityRequestTool(agent))
    agent.tools.register(TaskProgressTool(agent))
    agent.tools.register(GetGoalTool(agent))
    agent.tools.register(CreateGoalTool(agent))
    agent.tools.register(UpdateGoalTool(agent))
    agent.tools.register(StopNamedWorkTool(agent))
    agent.tools.register(PublishAuditUpdateTool(agent))
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
    # owner 持久计划使用会话唤醒链；它不是子代理轮询或推进工具。
    agent.tools.register(ScheduleTool(agent))
    # 内网主机出站授权:用户点名的内网监控目标(跨机数据源)经属主确认后进白名单,解除
    # NETWORK_PRIVATE_HOST_BLOCKED;不放松出站闸本身,只接通闸已内置的 allowed_private_hosts。
    # 高吞吐数据流盯守摄取层:代码层结构化预聚合/初筛/背压把 100+/s 压成候选批,主代理与
    # 所有 Agent 共用；游标和统计跨轮、跨重启持久，业务定性始终留给模型。
    agent.tools.register(WatchStreamTool(agent))
    # 增量结论账(收尾一公里):确认一条结论就持久化一条到 findings.jsonl,收尾崩/重派/
    # 被取消都不丢;整合/收口层从账合并,最终报告只是汇总视图。子代理与主代理长任务共用。
    if not agent.config.enable_subagents:
        return
    agent.tools.register(CreateSubagentsTool(agent))
    agent.tools.register(ListAgentsTool(agent))
    agent.tools.register(CancelSubagentsTool(agent))
    agent.tools.register(SendGuidanceTool(agent))
    agent.tools.register(ResolveCapabilityRequestsTool(agent))


__all__ = [
    "AgentRunResult",
    "CapabilityRequestTool",
    "CancelSubagentsTool",
    "ResolveCapabilityRequestsTool",
    "CODING_SUBAGENT_TOOLS",
    "CreateSubagentsTool",
    "ListAgentsTool",
    "ONE_SHOT_TOOL_NAMES",
    "PARENT_PLANNER_READ_TOOLS",
    "READ_ONLY_SUBAGENT_TOOLS",
    "SendGuidanceTool",
    "SimpleAgent",
    "TaskProgressTool",
]
