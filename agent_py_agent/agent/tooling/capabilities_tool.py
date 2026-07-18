# LLM: 本模块是模型自我能力描述的事实投影；新增通道或工具时必须保持“安装、配置、健康、绑定”分层，并同步注册表与测试。
# 模块用途: 为 list_capabilities 生成真实能力清单，让模型知道现在能做什么、还缺什么配置，避免凭代码支持列表误报已经接通。
"""让模型按当前安装、配置和注册事实发现能力，避免虚报已连接能力。

模型只能把明确可用的 runtime 工具和已配置通道当成能力；adapter 已安装、凭据已配置、健康检查
通过、当前会话已绑定是四层不同事实，不能相互替代。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from typing import Any

from ..delivery import ChannelAdapterRegistry, DeliveryContext
from .models import BaseTool, ToolExecutionResult, ToolSpec


# LLM: 通道清单只读取 composition root 注入的 registry snapshot；缺 registry 返回空，不能回扫模块造第二条链。
# 函数用途: 取得安装、配置、健康和当前绑定四层状态，并隐藏真实收件人 ID。
def _channel_catalog(
    registry: ChannelAdapterRegistry | None,
    binding_provider: Callable[[], DeliveryContext | None] | None,
) -> list[dict[str, object]]:
    if registry is None:
        return []
    binding = None
    if binding_provider is not None:
        try:
            binding = binding_provider()
        except Exception:
            binding = None
    return [item.to_dict() for item in registry.runtime_snapshot(binding)]


# LLM: 工具名来自当前 registry 的惰性 provider；读取失败必须返回空集合，不能把静态清单当成已注册事实。
# 函数用途: 取得这个 Agent 当前真正注册的工具名，用于决定哪些产品能力可以告诉模型。
def _tool_names(provider: Callable[[], Iterable[str]] | None) -> set[str]:
    if provider is None:
        return set()
    try:
        return {str(name) for name in provider() if str(name).strip()}
    except Exception:
        return set()


_OPTIONAL_CAPABILITY_SPECS = (
    (
        frozenset(("remember", "session_search")),
        "持久记忆",
        "available",
        "owner-scoped 长期记忆与历史会话检索",
        "remember 写入明确记忆；session_search 按需查旧对话",
    ),
    (
        frozenset(("update_persona",)),
        "人格定制",
        "available_with_confirmation",
        "owner-scoped SOUL/USER/AGENTS 人格与偏好更新",
        "update_persona；SOUL/AGENTS 修改需用户确认，USER 偏好可由 agent 维护",
    ),
    (
        frozenset(("create_subagents", "dispatch_subagents")),
        "子代理编排",
        "available",
        "按真实独立工作项创建、派发、跟踪和取消子代理",
        "create_subagents / dispatch_subagents / inspect_agent_tree",
    ),
    (
        frozenset(("wait",)),
        "当前任务非阻塞等待",
        "available",
        "让出当前回合并在同一任务稍后被唤醒；不是日历、cron 或持久提醒服务",
        "wait",
    ),
    (
        frozenset(("schedule",)),
        "持久定时与提醒",
        "available",
        "owner-scoped 定时任务在同一 thread 到点继续，支持 at/every/cron、暂停恢复和重启补跑",
        "schedule；与当前任务内部等待用的 wait 是两种能力",
    ),
    (
        frozenset(("browser",)),
        "浏览器自动化",
        "available",
        "导航、页面交互和可访问性快照",
        "browser",
    ),
    (
        frozenset(("analyze_image",)),
        "视觉理解",
        "registered_optional",
        "图片分析工具已注册；视觉模型未配置时会明确返回 TOOL_UNAVAILABLE",
        "analyze_image",
    ),
    (
        frozenset(("send_message",)),
        "当前通道主动发送",
        "available_in_bound_channel",
        "只向当前 owner 已绑定且可投递的通道发送消息或已登记附件",
        "send_message；不得把“adapter 已安装”当成“通道已连接”",
    ),
)


# LLM: owner scoping 只认显式配置；配置对象缺失与明确关闭必须保留成两个不同状态。
# 函数用途: 把多用户 owner 隔离配置转换成模型能准确描述的状态。
def _owner_scope_state(config: object | None) -> str:
    if config is None:
        return "configuration_unknown"
    return (
        "enabled" if bool(getattr(config, "gateway_per_user_owner_scoping", False)) else "disabled"
    )


# LLM: 基础能力来自当前产品主链和结构化配置，不依赖可选工具；新增项要避免把安装事实写成健康事实。
# 函数用途: 生成每个 Agent 都应看到的网关、隔离和会话基础能力说明。
def _base_runtime_capabilities(
    config: object | None,
    channel_catalog: list[dict[str, object]],
) -> list[dict[str, object]]:
    channel_names = [str(item.get("name") or "") for item in channel_catalog]
    return [
        {
            "area": "网关服务",
            "state": "available",
            "what": "统一接收通道/CLI 请求并路由到 owner-scoped 主代理",
            "how": "my-agent gateway start",
        },
        {
            "area": "多通道网关",
            "state": "available",
            "what": f"当前 registry 已安装通道：{', '.join(channel_names) or 'none'}；是否可用以 channel_catalog 四层状态为准",
            "how": "未配置通道先由管理员提供凭据并启动 adapter；未注册通道不能口头宣称可用",
        },
        {
            "area": "多用户隔离",
            "state": _owner_scope_state(config),
            "what": "按结构化 channel + user/group owner 隔离 home、任务、记忆、人格、skill、审计和成本",
            "how": "由 gateway owner scope 自动执行；不得读取其他 owner 私有目录",
        },
        {
            "area": "会话上下文与 compact",
            "state": "available",
            "what": "同一 thread 持续累计，达到配置阈值后压缩旧段并在同一 thread 继续",
            "how": "底座自动处理；不要另建 chat/task 历史",
        },
    ]


# LLM: 能力只由结构化配置和当前工具注册事实生成；不要加入基于用户自然语言或 prompt 猜测的分支。
# 函数用途: 按当前运行时工具和配置生成能力说明，例如记忆、人格、子代理和等待是否真的可调用。
def _runtime_capabilities(
    config: object | None,
    tool_names: set[str],
    channel_catalog: list[dict[str, object]],
    skill_catalog: dict[str, object],
    memory_catalog: dict[str, object],
    persona_catalog: dict[str, object],
) -> list[dict[str, object]]:
    capabilities = _base_runtime_capabilities(config, channel_catalog)
    for required, area, state, what, how in _OPTIONAL_CAPABILITY_SPECS:
        if required.issubset(tool_names):
            if area == "持久记忆":
                state = str(memory_catalog.get("state") or "unavailable")
                what += f"；当前有效 {int(memory_catalog.get('active_total') or 0)} 条"
            elif area == "人格定制":
                state = str(persona_catalog.get("state") or "unavailable")
            capabilities.append({"area": area, "state": state, "what": what, "how": how})
    if "skill_search" in tool_names:
        counts = skill_catalog.get("enabled_by_source")
        rendered_counts = ", ".join(
            f"{source}={count}"
            for source, count in (counts.items() if isinstance(counts, dict) else [])
        )
        capabilities.append(
            {
                "area": "Skill 检索",
                "state": str(skill_catalog.get("state") or "unavailable"),
                "what": "当前 owner 逐轮不可变 Skill 快照" + (
                    f"（{rendered_counts}）" if rendered_counts else ""
                ),
                "how": "skill_search 先 search 获取 stable_id，再用 get 读取同一轮正文",
            }
        )
    return capabilities


def _skill_catalog(provider: Callable[[], Any] | None) -> dict[str, object]:
    if provider is None:
        return {
            "state": "unavailable",
            "health": "not_probed",
            "enabled_total": 0,
            "enabled_by_source": {},
        }
    try:
        snapshot = provider()
        entries = tuple(snapshot.enabled_entries())
        errors = tuple(getattr(snapshot, "errors", ()) or ())
    except Exception as exc:
        return {
            "state": "unavailable",
            "health": "unavailable",
            "enabled_total": 0,
            "enabled_by_source": {},
            "load_error_codes": [type(exc).__name__],
        }
    counts: dict[str, int] = {}
    for entry in entries:
        source = str(getattr(entry, "source", "") or "unknown")
        counts[source] = counts.get(source, 0) + 1
    error_codes = sorted(
        {
            str(getattr(error, "code", "") or "SKILL_LOAD_ERROR")
            for error in errors
        }
    )
    return {
        # A malformed sibling does not revoke the immutable entries that were loaded
        # successfully.  Availability and load health are intentionally separate facts.
        "state": "available",
        "health": "degraded" if error_codes else "healthy",
        "enabled_total": len(entries),
        "enabled_by_source": counts,
        "load_error_count": len(errors),
        "load_error_codes": error_codes,
        "snapshot_fingerprint": str(getattr(snapshot, "fingerprint", "") or ""),
    }


def _memory_catalog(provider: Callable[[], dict[str, object]] | None) -> dict[str, object]:
    default: dict[str, object] = {
        "state": "unavailable",
        "health": "not_probed",
        "active_total": 0,
        "active_by_kind": {},
        "load_error_count": 0,
        "load_error_codes": [],
        "text_index": "unknown",
        "semantic_index": "unknown",
    }
    if provider is None:
        return default
    try:
        source = provider()
        if not isinstance(source, dict):
            raise TypeError("memory snapshot must be an object")
    except Exception as exc:
        return {
            **default,
            "health": "unavailable",
            "load_error_count": 1,
            "load_error_codes": [type(exc).__name__],
        }
    by_kind = source.get("active_by_kind")
    return {
        "state": str(source.get("state") or "unavailable"),
        "health": str(source.get("health") or "unavailable"),
        "active_total": int(source.get("active_total") or 0),
        "active_by_kind": {
            str(key): int(value)
            for key, value in (by_kind.items() if isinstance(by_kind, dict) else [])
        },
        "event_total": int(source.get("event_total") or 0),
        "load_error_count": int(source.get("load_error_count") or 0),
        "load_error_codes": sorted(
            {str(value) for value in (source.get("load_error_codes") or []) if str(value)}
        ),
        "text_index": str(source.get("text_index") or "unknown"),
        "semantic_index": str(source.get("semantic_index") or "unknown"),
    }


def _persona_catalog(provider: Callable[[], dict[str, object]] | None) -> dict[str, object]:
    default: dict[str, object] = {
        "state": "unavailable",
        "health": "not_probed",
        "targets": [],
        "confirmation_required": ["soul", "agents"],
        "user_autonomous": True,
        "versioned": True,
        "load_error_codes": [],
    }
    if provider is None:
        return default
    try:
        source = provider()
        if not isinstance(source, dict):
            raise TypeError("persona snapshot must be an object")
    except Exception as exc:
        return {
            **default,
            "health": "unavailable",
            "load_error_codes": [type(exc).__name__],
        }
    targets: list[dict[str, object]] = []
    raw_targets = source.get("targets")
    if isinstance(raw_targets, list):
        for row in raw_targets:
            if not isinstance(row, dict):
                continue
            target = str(row.get("target") or "")
            if target not in {"soul", "user", "agents"}:
                continue
            targets.append(
                {
                    "target": target,
                    "state": str(row.get("state") or "unknown"),
                    "truncated": bool(row.get("truncated")),
                    "blocked_lines": int(row.get("blocked_lines") or 0),
                    "version": int(row.get("version") or 0),
                }
            )
    return {
        "state": str(source.get("state") or "unavailable"),
        "health": str(source.get("health") or "unavailable"),
        "targets": targets,
        "confirmation_required": ["soul", "agents"],
        "user_autonomous": True,
        "versioned": True,
        "load_error_codes": sorted(
            {str(value) for value in (source.get("load_error_codes") or []) if str(value)}
        ),
    }


def _scheduler_catalog(provider: Callable[[], dict[str, object]] | None) -> dict[str, object]:
    default: dict[str, object] = {
        "state": "unavailable",
        "health": "not_probed",
        "active_jobs": 0,
        "paused_jobs": 0,
        "active_runs": 0,
        "schedule_kinds": [],
        "load_error_codes": [],
    }
    if provider is None:
        return default
    try:
        source = provider()
        if not isinstance(source, dict):
            raise TypeError("scheduler snapshot must be an object")
    except Exception as exc:
        return {
            **default,
            "health": "unavailable",
            "load_error_codes": [type(exc).__name__],
        }
    return {
        "state": str(source.get("state") or "unavailable"),
        "health": str(source.get("health") or "unavailable"),
        "active_jobs": int(source.get("active_jobs") or 0),
        "paused_jobs": int(source.get("paused_jobs") or 0),
        "active_runs": int(source.get("active_runs") or 0),
        "schedule_kinds": sorted(
            {str(value) for value in (source.get("schedule_kinds") or []) if str(value)}
        ),
        "load_error_codes": sorted(
            {str(value) for value in (source.get("load_error_codes") or []) if str(value)}
        ),
    }


# LLM: 这是 list_capabilities 的唯一 payload 构造入口；schema 变更要同步工具说明、注册调用方和 test_capabilities_tool。
# 函数用途: 汇总通道、工具能力和明确不可用项，返回一份可审计的模型自我描述。
def build_capability_inventory(
    *,
    config: object | None = None,
    tool_names_provider: Callable[[], Iterable[str]] | None = None,
    channel_registry: ChannelAdapterRegistry | None = None,
    channel_binding_provider: Callable[[], DeliveryContext | None] | None = None,
    skill_snapshot_provider: Callable[[], Any] | None = None,
    memory_snapshot_provider: Callable[[], dict[str, object]] | None = None,
    persona_snapshot_provider: Callable[[], dict[str, object]] | None = None,
    scheduler_snapshot_provider: Callable[[], dict[str, object]] | None = None,
) -> dict[str, Any]:
    channel_catalog = _channel_catalog(channel_registry, channel_binding_provider)
    configured_channels = [
        str(item["name"]) for item in channel_catalog if item["configured"] is True
    ]
    tool_names = _tool_names(tool_names_provider)
    skill_catalog = _skill_catalog(skill_snapshot_provider)
    memory_catalog = _memory_catalog(memory_snapshot_provider)
    persona_catalog = _persona_catalog(persona_snapshot_provider)
    scheduler_catalog = _scheduler_catalog(scheduler_snapshot_provider)
    return {
        "schema_name": "my_agent_capability_inventory",
        "schema_version": 5,
        "channels": [str(item["name"]) for item in channel_catalog],
        "configured_channels": configured_channels,
        "channel_catalog": channel_catalog,
        "capabilities": _runtime_capabilities(
            config,
            tool_names,
            channel_catalog,
            skill_catalog,
            memory_catalog,
            persona_catalog,
        ),
        "skill_catalog": skill_catalog,
        "memory_catalog": memory_catalog,
        "persona_catalog": persona_catalog,
        "scheduler_catalog": scheduler_catalog,
        "not_available": (
            []
            if "schedule" in tool_names and scheduler_catalog["state"] == "available"
            else [
                {
                    "area": "持久定时/日历提醒",
                    "state": "unavailable",
                    "reason": "当前 owner 的 schedule 工具或持久 scheduler repository 未装配。",
                }
            ]
        ),
        "discover_more": "更细命令用 `my-agent --help`；当前模型可调用工具以 list_tools 为准。",
        "principle": (
            "只能把 state=available/enabled 且满足当前 owner/channel 绑定的能力当成可用；"
            "setup_required、not_implemented、health=not_probed 或 current_bound=false 都不能被口头升级成已连接。"
        ),
    }


# LLM: 该只读工具只投影真实 registry/config 状态，不执行安装、探活或通道发送；变更时保持无副作用。
# 类用途: 给模型一个统一入口查看自己当前能做什么，以及哪些能力只是安装了但尚未配置。
class ListCapabilitiesTool(BaseTool):
    spec = ToolSpec(
        name="list_capabilities",
        category="meta",
        description=(
            "列出 my-agent 当前已安装、已配置和模型可调用的产品能力，并明确未配置/未实现项。"
            "遇到接通道、记忆、人格、调度或多用户需求时先查它，不能把支持列表虚报成已连接。"
        ),
        use_cases=[
            "要接入飞书/QQ或其他通道前，先区分 adapter 是否安装、配置和可投递",
            "要做定时任务/多用户隔离前,先查有没有内置能力",
            "不确定自己能做什么、有什么内置命令时",
        ],
        avoid_when=["只是要低层文件/命令/网络工具时,用 list_tools 即可"],
        keywords=[
            "capabilities",
            "能力",
            "内置",
            "adapter",
            "通道",
            "channel",
            "gateway",
            "网关",
            "list_capabilities",
            "造轮子",
        ],
        parameters={},
        parameter_schema={},
        examples=['{"tool": "list_capabilities"}'],
        effect="read_only",
        default_mode="real",
        requires_idempotency=False,
        requires_approval=False,
    )

    # LLM: config 与 tool_names_provider 均由 registry bootstrap 注入；不要在这里再创建第二份 registry。
    # 函数用途: 保存当前配置和工具名读取器，执行时再取最新的注册状态。
    def __init__(
        self,
        *,
        config: object | None = None,
        tool_names_provider: Callable[[], Iterable[str]] | None = None,
        channel_registry: ChannelAdapterRegistry | None = None,
        channel_binding_provider: Callable[[], DeliveryContext | None] | None = None,
        skill_snapshot_provider: Callable[[], Any] | None = None,
        memory_snapshot_provider: Callable[[], dict[str, object]] | None = None,
        persona_snapshot_provider: Callable[[], dict[str, object]] | None = None,
        scheduler_snapshot_provider: Callable[[], dict[str, object]] | None = None,
    ) -> None:
        self.config = config
        self.tool_names_provider = tool_names_provider
        self.channel_registry = channel_registry
        self.channel_binding_provider = channel_binding_provider
        self.skill_snapshot_provider = skill_snapshot_provider
        self.memory_snapshot_provider = memory_snapshot_provider
        self.persona_snapshot_provider = persona_snapshot_provider
        self.scheduler_snapshot_provider = scheduler_snapshot_provider

    # LLM: execute 只序列化同一 inventory 到 text 和 result_envelope；不得在这里探活或修改配置。
    # 函数用途: 执行 list_capabilities，把结构化能力清单同时返回给模型和运行时。
    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        payload = build_capability_inventory(
            config=self.config,
            tool_names_provider=self.tool_names_provider,
            channel_registry=self.channel_registry,
            channel_binding_provider=self.channel_binding_provider,
            skill_snapshot_provider=self.skill_snapshot_provider,
            memory_snapshot_provider=self.memory_snapshot_provider,
            persona_snapshot_provider=self.persona_snapshot_provider,
            scheduler_snapshot_provider=self.scheduler_snapshot_provider,
        )
        return ToolExecutionResult(
            self.spec.name,
            True,
            json.dumps(payload, ensure_ascii=False, indent=2),
            result_envelope=payload,
        )


__all__ = ["ListCapabilitiesTool", "build_capability_inventory"]
