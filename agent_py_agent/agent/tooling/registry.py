# LLM: 原权限、Schema、搜索和执行共用快照；宿主展示投影只折叠本轮名称，不热改共享工具、注册身份或真实loaded状态。
# 模块用途: 装配工具并交给唯一执行器，沿原目录与搜索展示可找回的可选工具。
from __future__ import annotations

"""Bind tools into one immutable runtime snapshot and delegate canonical calls.

这个文件是工具系统的“前台服务台”：登记工具、冻结每轮可见工具集合、从同一快照渲染目录，
并把已经由 Provider adapter 形成的 ``ToolCall`` 交给统一 ``ToolExecutor``。它不解析模型正文，
也不在执行时重新发现工具或另做一套授权判断。
"""

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..common.cancellation import CancellationToken
from ..contracts.tool_manifest_contract import tool_manifest_payload
from ..settings.defaults import default_config_int
from .artifact import ReadArtifactTool
from .content_transport_policy import (
    MAX_INLINE_WRITE_CONTENT_CHARS,
    tool_content_transport_protocol,
)
from .executor import ToolExecution, ToolExecutor, ToolExecutorRequest, ToolOutputProjection
from .models import (
    TOOL_DISCOVERY_ENTRY_NAMES,
    BaseTool,
    ConcurrencyPolicy,
    EffectResolverPolicy,
    ResourceScopePolicy,
    ToolAvailability,
    ToolHandlerOutcome,
    ToolInvocationContext,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntime,
    ToolRuntimePolicy,
    ToolRuntimeSnapshot,
)
from .registry_auth import allowed_tool_set
from .registry_bootstrap import build_tool_retriever, register_base_tools
from .registry_workspace import effective_registry_cwd
from .runtime_contracts import ToolCall, ToolResult

if TYPE_CHECKING:
    from ..user_space.owner_resolver import OwnerHomeResult

_allowed_tool_set = allowed_tool_set
# LLM: 这些工具都只允许宿主显式下发给受控 runner；普通 root 会话既不能直接
# 调用，也不能经 tool_search 重新发现。capability_request 的 run_id 由 child
# runner 注入，root 没有上级可申请，暴露后只会制造缺 run_id 的未知副作用账；
# record_lesson 只写当前 child run 的经验账本，主线程没有 child run 可归属。
# 常量用途: 把内部执行器和子代理专属的能力申请、经验记录入口从普通主代理工具面隐藏。
_DEFAULT_HIDDEN_TOOL_NAMES = frozenset({"controlled_exec", "capability_request", "record_lesson"})


def _agent_config_int(key: str) -> int:
    return default_config_int(key)


@dataclass(frozen=True)
class CatalogRenderConfig:
    mode: str
    offset: int
    limit: int
    categories: list[str]
    include_examples: bool
    entry_max_chars: int
    show_truncated_notice: bool
    detail_max_chars: int
    # 渐进式披露:这些 category 的工具不进初始模型 schema，只在目录末尾留折叠清单。
    # 模型通过 tool_search 加载命中工具；list_tools 仍可查看完整注册表。默认空=全量直出。
    deferred_categories: list[str] = field(default_factory=list)


# LLM: ToolRegistry 的单一装配配置；审批读取器和可选插件 owner 由 core 绑定，不能从工作区或模型参数推导；联测 owner/worker。
# 类用途: 汇总工作区、权限、工具上限和可信插件身份，未启用插件时不读安装表或启动服务。
@dataclass(frozen=True)
class ToolRegistryParams:
    workspace_root: Path
    max_chars: int
    max_entries: int
    max_matches: int
    web_max_chars: int
    http_timeout: int
    catalog_limit: int
    retrieval_limit: int
    vector_search_enabled: bool
    workspace_roots: list[Path] | None = None
    path_access_mode: str = "normal"
    path_dangerous_roots: list[str] | None = None
    owner_scope_root: str = (
        ""  # 多用户隔离 0 层:per-user agent 的 owner home;空=不隔离(单租户/主代理)
    )
    owner_type: str = "main_agent"
    protected_persona_root: str = ""  # 三份 Persona 只走专用入口；admin bypass 也不清空
    owner_quota_max_bytes: int = 0
    owner_quota_policy_available: bool = True
    access_mode: str = "workspace-write"
    shell_tool_timeout: int = 30
    shell_tool_output_max_chars: int = 12_000
    background_process_listen_scope_enforce: bool = True
    catalog_mode: str = "compact"
    catalog_offset: int = 0
    catalog_categories: list[str] | None = None
    catalog_deferred_categories: list[str] | None = None
    catalog_include_examples: bool = False
    catalog_entry_max_chars: int = 700
    catalog_show_truncated_notice: bool = True
    tool_detail_max_chars: int = 0
    tool_write_inline_max_chars: int = MAX_INLINE_WRITE_CONTENT_CHARS
    artifact_read_budget_window_seconds: int = field(
        default_factory=lambda: _agent_config_int("tool_artifact_read_budget_window_seconds")
    )
    artifact_read_budget_max_chars: int = field(
        default_factory=lambda: _agent_config_int("tool_artifact_read_budget_max_chars")
    )
    artifact_default_read_chars: int = field(
        default_factory=lambda: _agent_config_int("memory_artifact_default_read_chars")
    )
    disabled_tools: list[str] = field(default_factory=list)
    artifact_root: Path | None = None
    artifact_backup_root: Path | None = None
    runtime_fact_roots: list[Path] | None = None
    runtime_guard_policy: object | None = None
    # MCP 客户端(短板6)：要连接的外部 MCP server 声明。默认空 = 不连、不起子进程(零开销)。
    mcp_servers: dict[str, Any] | None = None
    # 只有宿主显式启用插件时注入规范 owner；不从工作目录或权限墙反推身份，构造不启动插件。
    plugin_owner: OwnerHomeResult | None = None
    # owner 权威运行库（agent.subagents.runtime_db）的只读引用：插件动作按候选 ID 发送前复核观察新鲜度用；None 时复核一律不通过
    plugin_runtime_repo: object | None = None
    # 配置 plugin_process_sandbox：插件业务进程是否套平台沙箱（只写插件数据目录）；默认关，沙箱不可用时插件不接入
    plugin_process_sandbox: bool = False
    # 视觉理解(短板6)：辅助视觉模型配置(VisionModelConfig)。默认 None = 未配视觉模型,
    # analyze_image 注册但调用时返回 TOOL_UNAVAILABLE(可选加法,零默认影响)。
    # 真实语义工具检索的 embedding provider；未配置时 vector 通道明确显示 unconfigured。
    tool_embedder: Any | None = None
    # list_capabilities 只读运行时实际配置，不再把“代码里有适配器”虚报成“已经配置可用”。
    # 通道状态和当前绑定来自 composition root 注入的唯一 registry/ConversationStore 事实。
    # 生产 SimpleAgent 注入 owner 自己的权威 LocalStore；裸 registry/合同探针可不注入。
    operation_store: object | None = None
    # 副作用默认 fail-closed；只有明确的无副作用合同探针/单元测试可显式关闭。
    operation_store_required: bool = True
    operation_owner_id: str = ""
    approval_mode_reader: Callable[[], str] | None = None


# LLM: list_tools 只能描述调用它的请求快照，不能退回进程级注册表或猜测 owner 类型。
# 类用途: 把当前请求真正可见且可执行的工具清单渲染成机器可读 manifest。
class ListToolsTool(BaseTool):
    model_spec = ToolModelSpec(
        name="list_tools",
        description="列出当前执行上下文可见的工具清单。",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        hints=ToolModelHints(
            category="system",
            use_cases=(
                "不确定当前有哪些工具时，先查询机器可读工具清单",
                "需要确认 run_command、write_file、apply_patch 等工具是否可用",
            ),
            avoid_when=("已经知道要用哪个工具时，直接调用目标工具",),
            keywords=("list_tools", "tools", "工具清单", "tool manifest", "available tools"),
            examples=('{"tool": "list_tools"}',),
        ),
    )
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("read_only"),
        concurrency_policy=ConcurrencyPolicy("parallel_safe"),
        resource_scopes=ResourceScopePolicy(mode="declared", static_scopes=("tool_registry",)),
    )

    def __init__(self, registry: Any):
        self.registry = registry

    # LLM: 直接调用仅供本地兼容和单测；真实 Tool Gateway 会走 execute_scoped 复用请求快照。
    # 函数用途: 用当前 registry 的默认权限与可用性生成一次自洽工具清单。
    def execute(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        _ = params
        return self._execute_snapshot(self.registry.runtime_snapshot())

    # LLM: 请求快照已经完成 owner、allowlist 与 availability 交集，目录工具不得重新放宽。
    # 函数用途: 在统一执行入口中列出本轮实际可见工具。
    def execute_scoped(
        self,
        params: dict[str, Any],
        context: ToolInvocationContext,
    ) -> ToolHandlerOutcome:
        _ = params
        return self._execute_snapshot(context.runtime_snapshot)

    # LLM: 完整归档与紧凑 prompt 输出必须来自同一份 specs，防止两份清单漂移。
    # 函数用途: 把指定运行快照渲染为完整 manifest 和有界模型视图。
    def _execute_snapshot(self, snapshot: ToolRuntimeSnapshot) -> ToolHandlerOutcome:
        payload = tool_manifest_payload(snapshot)
        payload["tool_failure_taxonomy"] = payload["failure_taxonomy"]
        payload["tool_retrieval"] = self.registry.retriever.status()
        live_payload = _live_tool_manifest_payload(payload)
        return ToolHandlerOutcome(
            "list_tools",
            True,
            json.dumps(payload, ensure_ascii=False),
            result_envelope={
                "tool_output_policy": {
                    "preserve_prompt_output": True,
                    "live_prompt_output": json.dumps(live_payload, ensure_ascii=False),
                }
            },
        )


# LLM: tool_search 只能在请求快照中检索；一次命中即为下一模型回合展开 Schema，但永远不能扩大 allowed_tools 或恢复不可用工具。
# 类用途: 从本轮已授权且已就绪的 deferred 工具中搜索，并按 会话运行时 的单步协议为下一回合展开命中工具。
class ToolSearchTool(BaseTool):
    """会话运行时 discovery for tools that are registered but not initially exposed."""

    _DEFAULT_SEARCH_LIMIT = 5

    model_spec = ToolModelSpec(
        name="tool_search",
        description=(
            "按当前权限与实时可用性搜索尚未展开的工具。一次搜索即会在下一次模型调用中临时展开命中工具的 Schema，"
            "不需要再次调用或填写选择参数。"
            "当任务需要 /goal 生命周期、外部协作、web、vision 或 MCP，而当前工具列表里没有对应工具时使用。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "必填。描述需要的工具能力或工具名。"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "description": "可选。搜索时最多返回多少个精简候选，默认 5。",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="system",
            use_cases=(
                "需要一种当前未直接提供的工具能力",
                "需要创建/管理子代理、读取持续目标或发起跨代理协作",
            ),
            avoid_when=("目标工具已经出现在当前工具列表中时，直接调用目标工具",),
            keywords=("tool search", "工具搜索", "发现工具", "子代理", "goal", "协作"),
            examples=(
                '{"tool":"tool_search","query":"创建并管理子代理","limit":5}',
            ),
        ),
    )
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("read_only"),
        concurrency_policy=ConcurrencyPolicy("parallel_safe"),
        resource_scopes=ResourceScopePolicy(mode="declared", static_scopes=("tool_registry",)),
    )

    def __init__(self, registry: Any):
        self.registry = registry

    # LLM: 直接调用使用 registry 默认快照；真实调用由 execute_scoped 固定到本轮快照。
    # 函数用途: 为兼容调用者执行一次默认范围的 deferred 工具搜索。
    def execute(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        return self._execute_snapshot(params, self.registry.runtime_snapshot())

    # LLM: 受限子代理只能搜索父级下发的快照子集，不能看到未授权工具名或说明。
    # 函数用途: 使用统一请求快照完成单步工具搜索并返回下一回合会展开的名称。
    def execute_scoped(
        self,
        params: dict[str, Any],
        context: ToolInvocationContext,
    ) -> ToolHandlerOutcome:
        return self._execute_snapshot(params, context.runtime_snapshot)

    # LLM: 搜索算法仍复用唯一 retriever；命中结果作为 typed envelope 交给下一模型回合，不得另造选择状态。
    # 函数用途: 校验查询参数，并在指定快照中一次搜索、展开 deferred specs。
    def _execute_snapshot(
        self,
        params: dict[str, Any],
        snapshot: ToolRuntimeSnapshot,
    ) -> ToolHandlerOutcome:
        query = str(params.get("query") or "").strip()
        if not query:
            return _tool_search_invalid_arguments("query 不能为空")
        try:
            limit = int(params.get("limit") or self._DEFAULT_SEARCH_LIMIT)
        except (TypeError, ValueError):
            limit = 0
        if limit < 1 or limit > 20:
            return _tool_search_invalid_arguments("limit 必须在 1-20 之间")
        return _search_deferred_tool_result(
            self.registry,
            query,
            limit=limit,
            snapshot=snapshot,
        )


def _tool_search_invalid_arguments(message: str) -> ToolHandlerOutcome:
    return ToolHandlerOutcome(
        "tool_search",
        False,
        json.dumps({"error": message}, ensure_ascii=False),
        error_code="TOOL_INVALID_ARGUMENTS",
    )


# LLM: 搜索结果必须同时携带文本协议 Schema 与 typed loaded names，以便原生与文本 Provider 共享单步语义。
# 函数用途: 搜索最多 limit 个 deferred 工具，返回完整参数说明并标记为下一回合临时可见。
def _search_deferred_tool_result(
    registry: Any,
    query: str,
    *,
    limit: int,
    snapshot: ToolRuntimeSnapshot,
) -> ToolHandlerOutcome:
    specs = registry.search_deferred_specs(
        query,
        limit=limit,
        runtime_snapshot=snapshot,
    )
    loaded_names = [spec.name for spec in specs]
    payload = {
        "schema_name": "tool_search_output",
        "schema_version": 3,
        "mode": "search_and_load",
        "tools": [
            {
                "name": spec.name,
                "category": spec.category,
                "description": spec.description,
                "schema_hash": spec.schema_hash,
                "input_schema": spec.input_schema,
            }
            for spec in specs
        ],
        "loaded_for_next_model_call": loaded_names,
    }
    return ToolHandlerOutcome(
        "tool_search",
        True,
        json.dumps(payload, ensure_ascii=False),
        result_envelope={"tool_search": {"loaded_tool_names": loaded_names}},
    )


def _live_tool_manifest_payload(payload: dict[str, object]) -> dict[str, object]:
    """Keep the model-facing discovery page bounded while preserving the full manifest."""
    tools = payload.get("tools")
    compact_tools = []
    for item in tools if isinstance(tools, list) else []:
        if not isinstance(item, dict):
            continue
        compact_tools.append(
            {
                "name": str(item.get("name") or ""),
                "category": str(item.get("category") or ""),
                "schema_hash": str(item.get("schema_hash") or ""),
                "effect_resolver": dict(
                    (item.get("runtime_policy") or {}).get("effect_resolver") or {}
                ),
                "approval_policy": dict(
                    (item.get("runtime_policy") or {}).get("approval_policy") or {}
                ),
                "input_fields": [
                    str(field.get("name") or "")
                    for field in item.get("input_fields") or []
                    if isinstance(field, dict)
                ],
            }
        )
    return {
        "schema_name": "tool_manifest_live_prompt",
        "schema_version": 2,
        "visible_tool_count": len(compact_tools),
        "snapshot_hash": str(payload.get("snapshot_hash") or ""),
        "permission_mode": str(payload.get("permission_mode") or ""),
        "tools": compact_tools,
        "tool_retrieval": payload.get("tool_retrieval") or {},
        "full_manifest": "完整参数、示例和失败合同已保存在本次工具输出归档中。",
    }


# LLM: 注册、Schema、搜索和执行由同一原快照派生；展示投影不能重建注册表、改变handler或伪造真实加载。
# 类用途: 组合工具实现与原权限快照，在同一发现链中呈现本工作片可忽略的工具短名单。
class ToolRegistry:
    # LLM: 保存唯一构造配置供工作片派生权限视图；普通 MCP 保持初始连接，插件只在新运行准备时启动，视图共用客户端与关闭标记。
    # 函数用途: 初始化一个 owner 的工具表、检索和审批读取器，运行时按同一注册表完成授权与调用。
    def __init__(
        self,
        params: ToolRegistryParams,
    ):
        self._construction_params = params
        self.workspace_root = params.workspace_root.resolve()
        self.workspace_roots = params.workspace_roots or [self.workspace_root]
        self.path_access_mode = params.path_access_mode
        self.path_dangerous_roots = params.path_dangerous_roots or []
        self.owner_scope_root = params.owner_scope_root
        self.owner_type = str(params.owner_type or "main_agent").strip() or "main_agent"
        self.tools: dict[str, BaseTool] = {}
        self.default_hidden_tool_names = set(_DEFAULT_HIDDEN_TOOL_NAMES)
        self.disabled_tool_names = {
            str(item).strip() for item in params.disabled_tools if str(item).strip()
        }
        self.catalog_limit = params.catalog_limit
        self.catalog_mode = params.catalog_mode
        self.catalog_offset = max(0, params.catalog_offset)
        self.catalog_categories = [
            item.strip() for item in params.catalog_categories or [] if item.strip()
        ]
        self.catalog_deferred_categories = [
            item.strip() for item in params.catalog_deferred_categories or [] if item.strip()
        ]
        self.catalog_include_examples = params.catalog_include_examples
        self.catalog_entry_max_chars = max(0, params.catalog_entry_max_chars)
        self.catalog_show_truncated_notice = params.catalog_show_truncated_notice
        self.tool_detail_max_chars = max(0, params.tool_detail_max_chars)
        self.runtime_guard_policy = params.runtime_guard_policy
        self.operation_store = params.operation_store
        self.operation_store_required = params.operation_store_required
        self.operation_owner_id = str(params.operation_owner_id or "local/main").strip()
        self.approval_mode_reader = params.approval_mode_reader
        self.retrieval_limit = params.retrieval_limit
        self.retriever = build_tool_retriever(params)
        register_base_tools(self, params)
        self.register(ToolSearchTool(self))
        self.register(ListToolsTool(self))
        # MCP 客户端(短板6)：连接配置的外部 MCP server，把其工具动态注册成 mcp__* 前缀工具。
        # mcp_servers 为空时此调用零开销返回(不起任何子进程)；启动失败的配置仍保留 client，
        # 后续只在新 run 边界重试，不让一次瞬时故障永久删掉工具。
        self._mcp_clients = _connect_mcp_servers(self, params.mcp_servers)
        self._mcp_prepare_lock = threading.Lock()
        self._mcp_closed = threading.Event()
        initial_retry_at = time.monotonic() + 1.0
        self._mcp_retry_state: dict[int, tuple[int, float]] = {
            id(client): (1, initial_retry_at)
            for client in self._mcp_clients
            if not client.is_running()
        }

    # LLM: 权限视图只重建基础文件/进程工具；其他 handler、存储、MCP 连接仍共享原权威，不启动第二套服务。
    # 函数用途: 给一个工作片生成独立权限视图，避免切 Full Access 热改同用户其他正在执行的会话。
    def with_access_policy(self, *, access_mode: str, path_access_mode: str, owner_scope_root: str):
        from copy import copy
        from dataclasses import replace

        params = replace(self._construction_params, access_mode=access_mode,
                         path_access_mode=path_access_mode, owner_scope_root=owner_scope_root)
        if params == self._construction_params:
            return self
        view = copy(self)
        view._construction_params = params
        view.path_access_mode = path_access_mode
        view.owner_scope_root = owner_scope_root
        view.tools = {}
        register_base_tools(view, params)
        view.register(ToolSearchTool(view))
        view.register(ListToolsTool(view))
        for name, tool in self.tools.items():
            if name not in view.tools:
                view.register(tool)
        return view

    # LLM: 注册表只存进程级实现；是否能给某个请求使用由 runtime_snapshot 决定。
    # 函数用途: 按稳定工具名登记一个实现；重复名称必须在装配期失败，不能静默换 handler。
    def register(self, tool: BaseTool) -> None:
        model_spec = getattr(tool, "model_spec", None)
        runtime_policy = getattr(tool, "runtime_policy", None)
        if not isinstance(model_spec, ToolModelSpec) or not isinstance(
            runtime_policy, ToolRuntimePolicy
        ):
            raise TypeError("registered tool must declare ToolModelSpec and ToolRuntimePolicy")
        name = model_spec.name
        if name in self.tools:
            raise ValueError(f"duplicate tool registration: {name}")
        self.tools[name] = tool

    # LLM: 快照先做硬权限交集，再做无副作用可用性检查；检查异常按不可用 fail-closed。
    # 函数用途: 固定一次 Agent run 可见、可搜、可调用的工具事实，供全部工具表面复用。
    def runtime_snapshot(
        self,
        *,
        allowed_tools: list[str] | None = None,
        run_id: str = "",
    ) -> ToolRuntimeSnapshot:
        allowed = allowed_tool_set(allowed_tools)
        runtimes: list[ToolRuntime] = []
        unavailable: list[tuple[str, str, str]] = []
        for name, tool in self.tools.items():
            if name in self.disabled_tool_names:
                continue
            if allowed is None and name in self.default_hidden_tool_names:
                continue
            if allowed is not None and name not in allowed:
                continue
            availability = _safe_tool_availability(tool)
            if not availability.available:
                unavailable.append(
                    (
                        name,
                        availability.error_code or "TOOL_UNAVAILABLE",
                        availability.reason,
                    )
                )
                continue
            model_spec = getattr(tool, "model_spec", None)
            runtime_policy = getattr(tool, "runtime_policy", None)
            if not isinstance(model_spec, ToolModelSpec) or not isinstance(
                runtime_policy, ToolRuntimePolicy
            ):
                raise TypeError(
                    f"registered tool must declare ToolModelSpec and ToolRuntimePolicy: {name}"
                )
            runtimes.append(
                ToolRuntime(
                    model_spec=model_spec,
                    runtime_policy=runtime_policy,
                    handler=tool,
                    availability=availability,
                )
            )
        return ToolRuntimeSnapshot(
            run_id=str(run_id or ""),
            runtimes=tuple(runtimes),
            available_tool_names=frozenset(runtime.model_spec.name for runtime in runtimes),
            unavailable_tools=tuple(unavailable),
            allowed_tools=frozenset(allowed) if allowed is not None else None,
            owner_type=self.owner_type,
        )

    def close_mcp_clients(self) -> None:
        """关闭所有已连接的 MCP server 子进程(进程生命周期收尾)。幂等。"""
        _close_registry_clients(self)

    # LLM: 可用性检查始终无副作用；普通 MCP 恢复与插件按代接线只发生在新 run 冻结工具快照之前。
    # 函数用途: 为下一轮同步完整插件贡献和 MCP 连接，各权限视图分别生成自己的工具表。
    def prepare_for_run(self) -> None:
        _prepare_registry_clients_for_run(self)

    # LLM: 扩展目录只按注册对象的结构化类型判定（MCP/插件代理 MCPProxyTool），不看名称前缀；先做与新运行相同的
    #   连接准备，再剔除 owner 禁用表；激活失效/连接断开由 runtime_snapshot 的可用性复核 fail-closed，本方法不重复判定。
    #   调用方（后台续跑白名单）必须在同一 run 冻结快照之前调用；同步失败按原 prepare 规则记录，不在这里吞掉。
    # 函数用途: 列出当前 owner 已启用插件和普通 MCP 服务贡献的工具名，供只列核心工具的白名单并入。
    def extension_tool_names(self) -> list[str]:
        from .mcp_registration import MCPProxyTool

        self.prepare_for_run()
        return [
            name
            for name, tool in self.tools.items()
            if isinstance(tool, MCPProxyTool) and name not in self.disabled_tool_names
        ]

    # LLM: specs 只是 runtime_snapshot 的投影；它不再维护独立的授权或可用性判断。
    # 函数用途: 返回当前快照中的工具说明，并按调用者需要隐藏编排类工具。
    def specs(
        self,
        *,
        allowed_tools: list[str] | None = None,
        include_orchestration: bool = False,
        runtime_snapshot: ToolRuntimeSnapshot | None = None,
    ) -> list[ToolModelSpec]:
        snapshot = runtime_snapshot or self.runtime_snapshot(allowed_tools=allowed_tools)
        specs = list(snapshot.specs)
        allowed = allowed_tool_set(allowed_tools)
        if allowed is not None:
            specs = [spec for spec in specs if spec.name in allowed]
        if not include_orchestration:
            specs = [spec for spec in specs if spec.category != "orchestration"]
        return specs

    # LLM: 宿主额外折叠只能减当前快照；显式allowed及真实搜索loaded优先，短名单不生成loaded或改变注册身份。
    # 函数用途: 返回原直接工具、未被本轮收起的可选工具，以及真实搜索已加载的完整Schema。
    def model_visible_specs(
        self,
        *,
        allowed_tools: list[str] | None = None,
        loaded_tool_names: set[str] | None = None,
        runtime_snapshot: ToolRuntimeSnapshot | None = None,
    ) -> list[ToolModelSpec]:
        """Return direct tools plus deferred tools loaded by a real tool_search result.

        An explicit ``allowed_tools`` profile is already a structured runtime
        selection (background/runner turns), so it remains fully visible.  Only
        the unrestricted foreground surface uses progressive disclosure.
        """

        snapshot = runtime_snapshot or self.runtime_snapshot(allowed_tools=allowed_tools)
        specs = self.specs(
            allowed_tools=allowed_tools,
            include_orchestration=True,
            runtime_snapshot=snapshot,
        )
        if allowed_tools is not None or (snapshot.allowed_tools is not None and
                (snapshot.presentation_deferred_names is not None or snapshot.presentation_shortlist_names is not None)):
            return specs
        extra_deferred = _presentation_deferred_names(snapshot, self.catalog_deferred_categories, allowed_tools=allowed_tools)
        if not self.catalog_deferred_categories and not extra_deferred:
            return specs
        deferred = set(self.catalog_deferred_categories)
        loaded = {str(item).strip() for item in loaded_tool_names or set() if str(item).strip()}
        return [spec for spec in specs if (spec.category not in deferred and spec.name not in extra_deferred) or spec.name in loaded]

    # LLM: 原deferred类别与本轮额外收起名共同进入同一retriever；展示短名单不能裁掉搜索，命中不得扩展快照权限。
    # 函数用途: 从原授权快照找回被折叠工具的精确说明和Schema，仍由真实tool_search产生加载事实。
    def search_deferred_specs(
        self,
        query: str,
        *,
        limit: int = 8,
        allowed_tools: list[str] | None = None,
        runtime_snapshot: ToolRuntimeSnapshot | None = None,
    ) -> list[ToolModelSpec]:
        """Search only structurally deferred specs; searching never grants authority."""

        snapshot = runtime_snapshot or self.runtime_snapshot(allowed_tools=allowed_tools)
        specs = self.specs(
            allowed_tools=allowed_tools,
            include_orchestration=True,
            runtime_snapshot=snapshot,
        )
        deferred = set(self.catalog_deferred_categories)
        extra_deferred = _presentation_deferred_names(snapshot, self.catalog_deferred_categories, allowed_tools=allowed_tools)
        searchable = [spec for spec in specs if spec.category in deferred or spec.name in extra_deferred]
        if not searchable:
            return []
        hits = self.retriever.search(str(query or ""), searchable, max(1, min(20, int(limit))))
        by_name = {spec.name: spec for spec in searchable}
        return [by_name[hit.name] for hit in hits if hit.name in by_name]

    # LLM: 目录沿同一原快照展示；可选短名单仅缩名卡，显式allowed忽略自适应投影，原完整list_tools仍可发现全部授权工具。
    # 函数用途: 渲染本轮工具提示和可找回的折叠清单，不把展示选择变成权限或加载事实。
    def render_catalog_section(
        self,
        *,
        allowed_tools: list[str] | None = None,
        tool_protocol: str = "native",
        runtime_snapshot: ToolRuntimeSnapshot | None = None,
    ) -> str:
        snapshot = runtime_snapshot or self.runtime_snapshot(allowed_tools=allowed_tools)
        specs = self.specs(
            allowed_tools=allowed_tools,
            include_orchestration=True,
            runtime_snapshot=snapshot,
        )
        presentation = allowed_tools is None and snapshot.allowed_tools is None
        return _render_registry_catalog_section(
            specs,
            self._catalog_render_config(),
            write_inline_max_chars=self._write_inline_max_chars(),
            tool_protocol=tool_protocol,
            presentation_deferred_names=_presentation_deferred_names(snapshot, self.catalog_deferred_categories, allowed_tools=allowed_tools),
            presentation_shortlist_names=snapshot.presentation_shortlist_names if presentation else None,
        )

    def _catalog_render_config(self) -> CatalogRenderConfig:
        return CatalogRenderConfig(
            mode=self.catalog_mode,
            offset=self.catalog_offset,
            limit=self.catalog_limit,
            categories=self.catalog_categories,
            deferred_categories=self.catalog_deferred_categories,
            include_examples=self.catalog_include_examples,
            entry_max_chars=self.catalog_entry_max_chars,
            show_truncated_notice=self.catalog_show_truncated_notice,
            detail_max_chars=self.tool_detail_max_chars,
        )

    def _write_inline_max_chars(self) -> int:
        tool = self.tools.get("write_file")
        value = getattr(tool, "max_inline_content_chars", MAX_INLINE_WRITE_CONTENT_CHARS)
        try:
            return int(value)
        except (TypeError, ValueError):
            return MAX_INLINE_WRITE_CONTENT_CHARS

    # LLM: 推荐检索只能消费快照 specs，不能把进程级工具重新带回提示词。
    # 函数用途: 在本轮工具范围内查找与查询相关的工具说明。
    def find_relevant_specs(
        self,
        query: str,
        *,
        allowed_tools: list[str] | None = None,
        runtime_snapshot: ToolRuntimeSnapshot | None = None,
    ) -> list[ToolModelSpec]:

        specs = self.specs(
            allowed_tools=allowed_tools,
            include_orchestration=True,
            runtime_snapshot=runtime_snapshot,
        )
        hits = self.retriever.search(query, specs, self.retrieval_limit)
        if not hits:
            return []
        by_name = {spec.name: spec for spec in specs}
        return [by_name[hit.name] for hit in hits if hit.name in by_name]

    # LLM: 推荐名卡可按宿主短名单做减法，基础发现入口保留；未loaded的原deferred工具不能因此出现在原生Schema。
    # 函数用途: 在原快照内显示本轮相关工具名称，保持搜索可达与权限范围不变。
    def render_recommended_tools_section(
        self,
        query: str,
        *,
        allowed_tools: list[str] | None = None,
        tool_protocol: str = "native",
        runtime_snapshot: ToolRuntimeSnapshot | None = None,
    ) -> str:
        snapshot = runtime_snapshot or self.runtime_snapshot(allowed_tools=allowed_tools)
        specs = self.model_visible_specs(
            allowed_tools=allowed_tools,
            runtime_snapshot=snapshot,
        )
        presentation = allowed_tools is None and snapshot.allowed_tools is None
        return _render_recommended_tools(
            query,
            specs,
            retriever=self.retriever,
            retrieval_limit=self.retrieval_limit,
            detail_max_chars=self.tool_detail_max_chars,
            tool_protocol=tool_protocol,
            presentation_shortlist_names=snapshot.presentation_shortlist_names if presentation else None,
        )

    # LLM: Every gate and handler in one invocation must receive the same host-authored effective
    # cwd/root set. 审批模式在工具边界读取 owner 唯一策略；读取错误不得放行，也不能由模型参数覆盖。
    # 函数用途: 在本轮统一工作目录和授权根下执行工具，并返回可审计的标准执行结果。
    def execute_tool(
        self,
        call: ToolCall,
        *,
        write_boundary: dict[str, object] | None,
        runtime_snapshot: ToolRuntimeSnapshot,
        trusted_run_context: dict[str, object] | None = None,
        cancellation_token: CancellationToken | None = None,
        required_action: object | None = None,
        pre_handler_gate: Callable[[ToolCall], ToolHandlerOutcome | None] | None = None,
        output_archiver: Callable[[ToolCall, object], ToolOutputProjection] | None = None,
    ) -> ToolExecution:
        invocation_root = effective_registry_cwd(self.workspace_root, write_boundary)
        invocation_roots = _execution_workspace_roots(
            invocation_root,
            self.workspace_roots,
            write_boundary,
        )
        boundary = write_boundary if isinstance(write_boundary, dict) else {}
        effective_owner_scope = str(
            boundary.get("effective_owner_scope_root") or self.owner_scope_root or ""
        ).strip()
        try:
            approval_mode = self.approval_mode_reader() if self.approval_mode_reader else "ask"
        except (OSError, ValueError):
            approval_mode = "unavailable"
        return ToolExecutor().execute(
            ToolExecutorRequest(
                call=call,
                runtime_snapshot=runtime_snapshot,
                workspace_root=invocation_root,
                workspace_roots=invocation_roots,
                path_access_mode=self.path_access_mode,
                path_dangerous_roots=tuple(self.path_dangerous_roots),
                owner_scope_root=effective_owner_scope,
                write_boundary=write_boundary,
                runtime_guard_policy=self.runtime_guard_policy,
                approval_mode=approval_mode,
                operation_store=self.operation_store,
                operation_store_required=self.operation_store_required,
                operation_owner_id=self.operation_owner_id,
                owner_type=self.owner_type,
                trusted_run_context=trusted_run_context,
                required_action=required_action,
                cancellation_token=cancellation_token,
                pre_handler_gate=pre_handler_gate,
                output_archiver=output_archiver,
            )
        )


# LLM: Tool authorization, resource locks, filesystem aliases and shell handlers must receive the
# same per-turn cwd/root set. The boundary values are host-authored by the Gateway thread scope;
# model arguments cannot add roots here.
# 函数用途: 合并本轮 TUI 工作目录与注册表原始目录，供所有工具门和执行器共同使用。
def _execution_workspace_roots(
    invocation_root: Path,
    registry_roots: list[Path],
    write_boundary: dict[str, object] | None,
) -> tuple[Path, ...]:
    boundary = write_boundary if isinstance(write_boundary, dict) else {}
    raw = boundary.get("execution_workspace_roots")
    requested = raw if isinstance(raw, list) else []
    inherited = requested if requested else registry_roots
    roots: list[Path] = []
    for value in [invocation_root, *inherited]:
        try:
            path = Path(value).expanduser().resolve(strict=False)
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
        if path not in roots:
            roots.append(path)
    return tuple(roots or (invocation_root,))


# LLM: 先置共享关闭标记再冻结客户端并锁外 stop；迟到登记方复查标记，之后取得 prepare 锁清引用，不等发现后才撤销。
# 函数用途: 关闭本注册表及其权限视图共用的 MCP 连接，避免旧视图再次重连。
def _close_registry_clients(registry: ToolRegistry) -> None:
    registry._mcp_closed.set()
    for client in tuple(getattr(registry, "_mcp_clients", ()) or ()):
        try:
            client.stop()
        except Exception:  # 关闭尽力而为，单个失败不阻断其余清理。
            pass
    with registry._mcp_prepare_lock:
        for client in registry._mcp_clients:
            if not client.is_closed():
                try:
                    client.stop()
                except Exception:
                    pass
        registry._mcp_clients.clear()
        registry._mcp_retry_state.clear()


# LLM: 插件同步及各视图投影在原 prepare 锁内，安装权威只读；永久关闭不重连，旧快照保留原 handler。
# 函数用途: 为新运行接入当前插件并恢复普通 MCP，核心工具及其他视图不被原位改写。
def _prepare_registry_clients_for_run(registry: ToolRegistry) -> None:
    if registry._mcp_closed.is_set():
        return
    clients = list(getattr(registry, "_mcp_clients", ()) or ())
    plugin_owner = registry._construction_params.plugin_owner
    if plugin_owner is None and (not clients or all(client.is_running() and not getattr(client, "tools_changed", False) for client in clients)):
        return
    with registry._mcp_prepare_lock:
        if registry._mcp_closed.is_set():
            return
        if plugin_owner is not None:
            from .plugin_registration import project_plugin_tools, synchronize_plugin_clients

            synchronize_plugin_clients(registry)
        for client in list(getattr(registry, "_mcp_clients", ()) or ()):
            if client.is_closed():
                registry._mcp_retry_state.pop(id(client), None)
                continue
            if client.is_running() and not getattr(client, "tools_changed", False):
                registry._mcp_retry_state.pop(id(client), None)
                continue
            _reconnect_registry_client_if_due(registry, client)
        if plugin_owner is not None:
            project_plugin_tools(registry)


# LLM: 恢复与发布固定同一 transport；失败只能清理已收到的句柄，不能 stop 整个可恢复客户端或猜 current。
# 函数用途: 按退避连接暂时不可用的 MCP 服务，永久关闭时撤销恢复队列。
def _reconnect_registry_client_if_due(registry: ToolRegistry, client: object) -> None:
    if client.is_closed():
        registry._mcp_retry_state.pop(id(client), None)
        return
    attempts, retry_at = registry._mcp_retry_state.get(id(client), (0, 0.0))
    if time.monotonic() < retry_at:
        return
    transport = None
    try:
        from .mcp_registration import disconnect_failed_mcp_transport, refresh_registered_mcp_client

        transport = client.reconnect()
        from ..plugin_runtime import PluginMCPClient

        if isinstance(client, PluginMCPClient):
            from .plugin_registration import refresh_plugin_client

            refresh_plugin_client(registry, client, transport)
        else:
            refresh_registered_mcp_client(registry, client, transport=transport)
        registry._mcp_retry_state.pop(id(client), None)
    except Exception as exc:
        if transport is not None:
            disconnect_failed_mcp_transport(client, transport)
        if client.is_closed():
            registry._mcp_retry_state.pop(id(client), None)
            return
        attempts += 1
        delay = min(60.0, float(2 ** min(attempts - 1, 6)))
        registry._mcp_retry_state[id(client)] = (
            attempts,
            time.monotonic() + delay,
        )
        logging.getLogger(__name__).warning(
            "MCP server '%s' run-boundary reconnect failed; retry in %.0fs: %s",
            getattr(getattr(client, "config", None), "name", "unknown"),
            delay,
            exc,
        )


# LLM: 仅核对原显式allowed与原tool_search是否直接可见，不推断可选类别；宿主已有类别策略是名称集合的来源。
# 函数用途: 当原发现入口被既有目录设置收起时放弃额外折叠，避免把直接工具隐藏后无法找回。
def _presentation_deferred_names(snapshot: ToolRuntimeSnapshot, deferred_categories: list[str], *,
                                 allowed_tools: list[str] | None = None) -> frozenset[str]:
    names = snapshot.presentation_deferred_names
    if not names or allowed_tools is not None or snapshot.allowed_tools is not None:
        return frozenset()
    search = snapshot.runtime("tool_search")
    if search is None or search.model_spec.category in deferred_categories:
        return frozenset()
    return names


# LLM: 默认投影为None时保持原目录字节；额外折叠与短名单仅影响提示，不改变传入的完整授权specs。
# 函数用途: 组合原工具协议、传输说明和本工作片的折叠发现提示。
def _render_registry_catalog_section(
    specs: list[ToolModelSpec],
    render_config: CatalogRenderConfig,
    *,
    write_inline_max_chars: int,
    tool_protocol: str,
    presentation_deferred_names: frozenset[str] | None = None,
    presentation_shortlist_names: frozenset[str] | None = None,
) -> str:
    if str(tool_protocol or "").strip().lower() == "native":
        # Native providers already receive canonical schemas in their
        # structured tools field. Keep only transport rules and the deferred
        # discovery surface so the immutable prompt stays compactable.
        _, deferred = _split_deferred_specs(
            _filter_catalog_specs(specs, render_config.categories),
            render_config.deferred_categories,
            presentation_deferred_names=presentation_deferred_names,
        )
        entries = [
            "- 当前直接工具的名称、说明和参数 Schema 已通过原生工具通道提供；"
            "以该结构化 Schema 为准，不在提示词中重复展开。",
        ]
        deferred_notice = _render_deferred_notice(deferred, presentation_shortlist_names=presentation_shortlist_names)
        if deferred_notice:
            entries.append(deferred_notice)
    else:
        entries = render_catalog_entries(specs, render_config, presentation_deferred_names=presentation_deferred_names,
                                         presentation_shortlist_names=presentation_shortlist_names)
    transport_protocol = (
        tool_content_transport_protocol(write_inline_max_chars)
        if any(
            spec.name
            in {
                "write_file",
                "apply_patch",
                "run_command",
                "terminal_session",
                "lsp",
            }
            for spec in specs
        )
        else ""
    )
    return _render_tool_catalog_section(
        entries,
        transport_protocol,
        tool_protocol=tool_protocol,
    )


# LLM: 推荐只筛名卡候选，不覆盖权限或loaded；短名单为空不等于没有授权，默认None保持原检索与输出。
# 函数用途: 用原retriever渲染相关工具名字，省略未选名卡时仍保留原发现入口。
def _render_recommended_tools(
    query: str,
    specs: list[ToolModelSpec],
    *,
    retriever: Any,
    retrieval_limit: int,
    detail_max_chars: int,
    tool_protocol: str,
    presentation_shortlist_names: frozenset[str] | None = None,
) -> str:
    if not specs:
        return (
            "# Recommended Tools\n"
            "当前执行上下文没有授权工具。若缺少能力，请提交 capability_request。"
        )
    candidates = specs if presentation_shortlist_names is None else [
        spec for spec in specs if spec.name in presentation_shortlist_names or spec.name in TOOL_DISCOVERY_ENTRY_NAMES
    ]
    hits = retriever.search(query, candidates, retrieval_limit) if candidates else []
    if not hits:
        return (
            "# Recommended Tools\n"
            "当前没有明显高相关的工具命中。若要动手操作，请先根据 Tool Catalog 选最接近的工具。"
        )
    by_name = {spec.name: spec for spec in specs}
    native = str(tool_protocol or "").strip().lower() == "native"
    blocks: list[str] = []
    for hit in hits:
        spec = by_name[hit.name]
        reason_text = "；".join(hit.reasons) or "与当前任务相关"
        if native:
            blocks.append(f"- {spec.name}：{reason_text}")
        else:
            blocks.append(
                f"{spec.render_recommended_entry(max_chars=detail_max_chars)}\n"
                f"推荐理由：{reason_text}"
            )
    return "# Recommended Tools\n" + "\n\n".join(blocks)


# LLM: readiness check 发生在已授权候选集内；实现异常不得让工具进入 Schema 或中断整个 Agent。
# 函数用途: 安全调用工具的无副作用 availability 合同，并把异常归一为不可用。
def _safe_tool_availability(tool: BaseTool) -> ToolAvailability:
    try:
        return tool.availability()
    except Exception as exc:
        return ToolAvailability.unavailable(f"availability check failed: {type(exc).__name__}")


def _connect_mcp_servers(registry: ToolRegistry, mcp_servers: dict[str, Any] | None) -> list[Any]:
    """惰性连接 MCP server 并注册其工具；返回全部合法配置的 client(供重连和 close)。

    惰性 import ``mcp_registration``：mcp_servers 为空(默认)时连模块都不导入，零开销；
    且把 MCP 子系统与核心 registry 解耦。整个连接过程被 try 兜底——MCP 是可选加法，
    任何异常都不许阻断 registry 构造(主流程)。
    """
    if not mcp_servers:
        return []
    try:
        from .mcp_registration import register_mcp_servers

        return register_mcp_servers(registry, mcp_servers)
    except Exception:  # 兜底：连接子系统整体异常也不崩主流程。
        logging.getLogger(__name__).exception("MCP server 连接子系统初始化失败，已跳过")
        return []


def _render_tool_catalog_section(
    entries: list[str],
    content_transport_protocol: str,
    *,
    tool_protocol: str = "native",
) -> str:
    if not entries:
        entries = ["- none：当前执行上下文没有授权任何工具；缺能力时请上抛 capability_request。"]
    return (
        _tool_call_protocol(tool_protocol) + "\n\n" + content_transport_protocol + "\n\n"
        "# Tool Catalog\n" + "\n".join(entries)
    )


def _tool_call_protocol(tool_protocol: str = "native") -> str:
    protocol = str(tool_protocol or "").strip().lower()
    if protocol != "native":
        raise ValueError(f"invalid tool protocol: {protocol or '<empty>'}")
    return _native_tool_call_protocol()


def _native_tool_call_protocol() -> str:
    # native 协议下模型直接用结构化 tool_use 调工具，不需要教它写 [TOOL_CALL] 文本格式。
    # 明确禁止退回文本协议：弱模型有训练惯性，偶尔会在正文里写 [TOOL_CALL]{...} 文本而非发起
    # 结构化调用，导致解析失败(TOOL_CALL_JSON_INVALID/UNCLOSED)。这条正向约束是低风险缓解，
    # 治本需历史 messages 完全结构化(对标 长期助手，红线区大工程，单独立项)。
    return (
        "# Tools\n"
        "当你需要看文件、改代码、查网页或测接口时，可以调用工具。\n"
        "本会话已启用原生工具调用：直接发起结构化工具调用即可，参数按下方 Tool Catalog 中各工具的参数名填写。\n"
        "重要：必须用原生工具调用机制发起调用；不要把工具调用写成正文里的文本 JSON 块，那样不会被执行。\n"
        "拿到工具结果后再输出最终答案。"
    )


# LLM: 展示分页只消费原specs；宿主短名单不能写回注册表，默认None不改变既有目录语义。
# 函数用途: 在原目录模式和分页设置中渲染本轮名卡及折叠提示。
def render_catalog_entries(specs: list[ToolModelSpec], config: CatalogRenderConfig, *,
                           presentation_deferred_names: frozenset[str] | None = None,
                           presentation_shortlist_names: frozenset[str] | None = None) -> list[str]:
    filtered = _filter_catalog_specs(specs, config.categories)
    if config.mode == "off":
        return ["- disabled：tool_catalog_mode=off，当前 prompt 不注入工具目录。"]
    if config.mode == "retrieval_only":
        return [
            "- retrieval_only：工具目录精简隐藏，请依赖 Recommended Tools；缺少工具时用 tool_search 加载。"
        ]
    primary, deferred = _split_deferred_specs(filtered, config.deferred_categories,
                                             presentation_deferred_names=presentation_deferred_names)
    if presentation_shortlist_names is not None:
        primary = [spec for spec in primary if spec.name in presentation_shortlist_names or spec.name in TOOL_DISCOVERY_ENTRY_NAMES]
    page = primary[config.offset : config.offset + max(0, config.limit)]
    entries = [_render_catalog_spec(spec, config) for spec in page]
    if config.show_truncated_notice:
        notice = _catalog_page_notice(config, total=len(primary), returned=len(page))
        if notice:
            entries.append(notice)
    deferred_notice = _render_deferred_notice(deferred, presentation_shortlist_names=presentation_shortlist_names)
    if deferred_notice:
        entries.append(deferred_notice)
    return entries


# LLM: 原category折叠与宿主本轮名称集合取并集，不因短名单选中原deferred工具就伪造已加载。
# 函数用途: 把原授权工具分成直接名卡与可经原搜索找回的折叠名卡。
def _split_deferred_specs(
    specs: list[ToolModelSpec],
    deferred_categories: list[str],
    *,
    presentation_deferred_names: frozenset[str] | None = None,
) -> tuple[list[ToolModelSpec], list[ToolModelSpec]]:
    """渐进式披露:把 deferred category 的工具从主目录分出去(只在末尾留折叠清单)。"""
    if not deferred_categories and not presentation_deferred_names:
        return specs, []
    deferred_set = set(deferred_categories)
    names = presentation_deferred_names or frozenset()
    primary = [spec for spec in specs if spec.category not in deferred_set and spec.name not in names]
    deferred = [spec for spec in specs if spec.category in deferred_set or spec.name in names]
    return primary, deferred


# LLM: 短名单只缩名字，完整数目和原tool_search/list_tools入口保留；省略名称不能成为权限或loaded事实。
# 函数用途: 显示有界的本轮折叠名称提示，默认None逐字保留原完整折叠清单；被折叠的插件按插件 ID 与简介列出（仅展示）。
def _render_deferred_notice(specs: list[ToolModelSpec], *, presentation_shortlist_names: frozenset[str] | None = None) -> str:
    """折叠清单:只列被 defer 工具的名字，按 会话运行时 方式用 tool_search 再加载。"""
    if not specs:
        return ""
    shown = [spec.name for spec in specs if presentation_shortlist_names is None or spec.name in presentation_shortlist_names]
    names = ", ".join(sorted(shown))
    notice = (
        f"- ⊞ 另有 {len(specs)} 个工具已注册但未直接展开；需要时先用 tool_search 搜索并加载，"
        "也可用 list_tools 查看完整清单"
    )
    if presentation_shortlist_names is None:
        return notice + f"：{names}"
    notice += f"。本轮提示 {len(shown)} 个：{names}" if shown else "。本轮未列短名单，仍可按能力搜索。"
    # 被折叠的插件按插件列出（数量受已启用插件数约束），否则模型不知道用户点名的插件其实已安装
    plugins = sorted({spec.description.split("的 ", 1)[0] for spec in specs
                      if spec.category == "plugins" and spec.name not in shown and spec.description.startswith("插件 ")})
    if plugins:
        notice += "\n  已启用但本轮未展开的插件（可用 tool_search 按插件 ID 加载）：" + "；".join(plugins)
    return notice


def _filter_catalog_specs(specs: list[ToolModelSpec], categories: list[str]) -> list[ToolModelSpec]:
    if not categories:
        return specs
    allowed_categories = set(categories)
    return [spec for spec in specs if spec.category in allowed_categories]


def _render_catalog_spec(spec: ToolModelSpec, config: CatalogRenderConfig) -> str:
    if config.mode == "full":
        return spec.render_detail_entry(max_chars=config.detail_max_chars)
    return spec.render_catalog_entry(
        include_examples=config.include_examples,
        max_chars=config.entry_max_chars,
    )


def _catalog_page_notice(config: CatalogRenderConfig, *, total: int, returned: int) -> str:
    next_offset = config.offset + returned
    if total <= next_offset:
        return ""
    return (
        f"- more_tools：工具目录已分页，next_offset={next_offset} limit={config.limit} total={total}。"
        " 如需更多工具，请调大 tool_catalog_limit 或 tool_catalog_offset。"
    )
