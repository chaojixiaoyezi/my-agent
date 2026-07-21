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
        "可保存你明确要求长期记住的事实和偏好，也可按需查找较早的对话",
        "直接说明要记住或查找的内容",
    ),
    (
        frozenset(("update_persona",)),
        "人格定制",
        "available_with_confirmation",
        "可维护你的称呼和偏好；影响核心人格或行为规则的修改需要你确认",
        "直接告诉我你的偏好或希望怎样调整",
    ),
    (
        frozenset(("create_subagents", "dispatch_subagents")),
        "子代理编排",
        "available",
        "较大的任务可拆成互不依赖的部分并行完成，再由主代理统一汇总",
        "直接描述完整任务，无需指定内部执行方式",
    ),
    (
        frozenset(("wait",)),
        "当前任务非阻塞等待",
        "available",
        "当前任务可以暂时让出执行资源，稍后仍在原任务继续；它不是定时提醒",
        "由任务运行过程按需使用",
    ),
    (
        frozenset(("schedule",)),
        "持久定时与提醒",
        "available",
        "可设置一次性或周期提醒，到点后在原会话继续，也可暂停和恢复",
        "直接说明时间、时区、提醒内容和是否重复",
    ),
    (
        frozenset(("browser",)),
        "浏览器自动化",
        "available",
        "导航、页面交互和可访问性快照",
        "直接说明要访问的页面和要完成的操作",
    ),
    (
        frozenset(("analyze_image",)),
        "视觉理解",
        "registered_optional",
        "可在视觉模型已接通时分析图片；未接通时会明确说明不可用",
        "发送图片并说明想了解什么",
    ),
    (
        frozenset(("send_message",)),
        "当前通道主动发送",
        "available_in_bound_channel",
        "只向当前账号已经接通的会话通道主动发送消息或附件",
        "直接说明要发送的内容；安装了通道并不等于已经接通",
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
    del channel_catalog
    return [
        {
            "area": "网关服务",
            "state": "available",
            "what": "从当前聊天入口或本地终端接收请求，并交给同一个代理底座处理",
            "how": "直接聊天或布置任务",
        },
        {
            "area": "聊天入口管理",
            "state": "available",
            "what": "底座可以登记多个聊天入口，但安装、配置、运行正常和当前会话绑定是不同事实；各入口的实际状态见下方",
            "how": "只有明确显示已经接通的入口才能当成当前可用，其他入口需要管理员完成配置或启动",
        },
        {
            "area": "多用户隔离",
            "state": _owner_scope_state(config),
            "what": "不同用户和群聊的文件、任务、记忆、人格、技能记录和用量彼此隔离",
            "how": "系统自动执行；当前用户不能读取其他用户或群聊的私有信息",
        },
        {
            "area": "会话上下文与 compact",
            "state": "available",
            "what": "同一会话会持续记住前文；接近上下文上限时压缩较早内容并继续累计",
            "how": "系统自动处理，聊天和任务共用同一份会话历史",
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
    scheduler_catalog: dict[str, object],
) -> list[dict[str, object]]:
    capabilities = _base_runtime_capabilities(config, channel_catalog)
    for spec in _OPTIONAL_CAPABILITY_SPECS:
        projected = _optional_capability(
            spec,
            config=config,
            tool_names=tool_names,
            channel_catalog=channel_catalog,
            memory_catalog=memory_catalog,
            persona_catalog=persona_catalog,
            scheduler_catalog=scheduler_catalog,
        )
        if projected is not None:
            capabilities.append(projected)
    if "skill_search" in tool_names:
        capabilities.append(_skill_capability(skill_catalog))
    return capabilities


def _optional_capability(
    spec: tuple[frozenset[str], str, str, str, str],
    *,
    config: object | None,
    tool_names: set[str],
    channel_catalog: list[dict[str, object]],
    memory_catalog: dict[str, object],
    persona_catalog: dict[str, object],
    scheduler_catalog: dict[str, object],
) -> dict[str, object] | None:
    required, area, state, what, how = spec
    if not required.issubset(tool_names):
        return None
    match area:
        case "持久记忆":
            state = str(memory_catalog.get("state") or "unavailable")
            what += f"；当前有效 {int(memory_catalog.get('active_total') or 0)} 条"
        case "人格定制":
            state = str(persona_catalog.get("state") or "unavailable")
        case "持久定时与提醒":
            state = str(scheduler_catalog.get("state") or "unavailable")
        case "视觉理解":
            state, what = _vision_capability(config)
        case "当前通道主动发送":
            state, what = _bound_channel_capability(channel_catalog, what)
    return {"area": area, "state": state, "what": what, "how": how}


# LLM: analyze_image 始终注册只是为了给未配置环境返回稳定错误；能力自述必须另外读取
#   vision_api_base + vision_model_name，不能把“工具存在”升级成“视觉模型已接通”。
# 函数用途: 从结构化配置投影视觉能力状态，不做自然语言判断，也不在只读清单里触发外部探活。
def _vision_capability(config: object | None) -> tuple[str, str]:
    if config is None:
        return "configuration_unknown", "当前无法确认是否配置了视觉模型"
    api_base = str(getattr(config, "vision_api_base", "") or "").strip()
    model_name = str(getattr(config, "vision_model_name", "") or "").strip()
    if not api_base or not model_name:
        return "unavailable", "当前没有配置视觉模型，不能分析图片内容"
    return (
        "configured_not_probed",
        "视觉模型已经配置；实际连接会在首次分析图片时验证，尚未验证前不能宣称可用",
    )


def _bound_channel_capability(
    channel_catalog: list[dict[str, object]],
    what: str,
) -> tuple[str, str]:
    bound = next(
        (item for item in channel_catalog if item.get("current_bound") is True),
        None,
    )
    if bound is None:
        return "unavailable", what + "；当前没有已经接通且可主动发送的会话通道"
    if bound.get("ready") is True:
        return "available", what + f"；当前 {bound.get('name')} 已接通，可主动发送"
    return (
        "unavailable",
        what + f"；当前请求来自 {bound.get('name')}，但主动发送尚未接通",
    )


def _skill_capability(skill_catalog: dict[str, object]) -> dict[str, object]:
    enabled_total = int(skill_catalog.get("enabled_total") or 0)
    what = f"当前会话可按需选择 {enabled_total} 个已经启用的 Skill（技能）"
    return {
        "area": "Skill 检索",
        "state": str(skill_catalog.get("state") or "unavailable"),
        "what": what,
        "how": "直接描述任务，代理会先选择匹配技能，再读取并遵循其完整说明",
    }


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
            scheduler_catalog,
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


# LLM: 用户可见能力说明只能消费这个投影；详细 state/health/binding/tool 字段留在 result_envelope 供运行时审计。
# 函数用途: 把完整能力事实转换成不暴露工具名、内部状态码、owner、路径或目标 ID 的模型答复视图。
def _capability_model_view(inventory: dict[str, Any]) -> dict[str, object]:
    capabilities: list[dict[str, str]] = []
    for row in inventory.get("capabilities", []):
        if not isinstance(row, dict):
            continue
        capabilities.append(
            {
                "能力": str(row.get("area") or ""),
                "可用情况": _public_availability(str(row.get("state") or "")),
                "说明": str(row.get("what") or ""),
                "使用方式": str(row.get("how") or ""),
            }
        )

    channels: list[dict[str, str]] = []
    for row in inventory.get("channel_catalog", []):
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "")
        capabilities_row = row.get("capabilities")
        supports_proactive = bool(
            isinstance(capabilities_row, dict) and capabilities_row.get("proactive")
        )
        health = row.get("health") if isinstance(row.get("health"), dict) else {}
        health_state = str(health.get("state") or "not_probed")
        configured = row.get("configured")
        connected = configured is True and health_state == "healthy"
        channels.append(
            {
                "通道": name,
                "接通状态": _public_channel_connection(
                    configured=configured,
                    health_state=health_state,
                ),
                "与当前会话的关系": (
                    "当前请求按这个通道的会话身份处理"
                    if row.get("current_bound") is True
                    else "不是当前会话通道"
                ),
                "主动发送": (
                    "可用"
                    if connected and row.get("current_bound") is True and supports_proactive
                    else "尚未接通"
                ),
            }
        )

    unavailable: list[dict[str, str]] = []
    for row in inventory.get("not_available", []):
        if not isinstance(row, dict):
            continue
        unavailable.append(
            {
                "能力": str(row.get("area") or ""),
                "说明": _public_unavailable_reason(str(row.get("reason") or "")),
            }
        )

    return {
        "答复规则": [
            "只用普通用户能理解的自然语言说明，不复述工具名、内部实现名词、状态码、健康码、路径或任何内部 ID。",
            "多用户隔离是已启用的安全能力，不要因为当前用户不能读取别人数据而把它列为不可用。",
            "当前请求来自某个聊天通道，只表示这次会话身份已识别；是否支持离开当前回复流程主动发送，要看该通道的主动发送状态，两者不能混为一谈。",
            "代码中支持或注册了某个聊天入口，不等于管理员已经配置并启动它；只有接通状态明确为已接通时，才能说当前能通过该入口聊天。",
            "没有明确可用事实的能力必须说尚未接通，不得根据代码中存在某个模块或工具自行升级。",
            "视觉分析工具存在不代表视觉模型已经配置；只有当前能力明确显示可用时，才能说能看图。",
        ],
        "当前能力": capabilities,
        "聊天通道": channels,
        "尚未接通": unavailable,
    }


def _public_availability(state: str) -> str:
    if state in {"available", "enabled"}:
        return "可用"
    if state == "available_with_confirmation":
        return "可用，部分修改需要用户确认"
    if state == "registered_optional":
        return "已包含，但是否可用取决于当前配置"
    if state == "configured_not_probed":
        return "已配置，但连接尚未验证"
    if state == "configuration_unknown":
        return "当前状态无法确认"
    return "尚未接通"


def _public_unavailable_reason(reason: str) -> str:
    if "schedule" in reason or "scheduler" in reason:
        return "当前账号的持久定时服务尚未接通"
    return "当前运行环境尚未接通这项能力"


def _public_channel_connection(*, configured: object, health_state: str) -> str:
    if configured is not True:
        return "尚未配置" if configured is False else "配置状态无法确认"
    if health_state == "healthy":
        return "已接通"
    if health_state in {"unhealthy", "failed", "stopped"}:
        return "已配置，但当前未正常运行"
    return "已配置，但运行状态尚未确认"


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
    # 函数用途: 执行 list_capabilities，把用户安全视图返回给模型，完整结构化事实留给运行时审计。
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
            json.dumps(_capability_model_view(payload), ensure_ascii=False, indent=2),
            result_envelope=payload,
        )


__all__ = ["ListCapabilitiesTool", "build_capability_inventory"]
