# Conversation runtime utilities
from __future__ import annotations

import json
import time
from typing import Any

from .models import ConversationThread, ObservationEvent, WakeSignal
from .store import ConversationStore


# LLM: 定时/自设提醒唤醒轮的自驱续任务提示词(P1 持续监控 0/8 命中的提示词侧根因修复)。
#   旧版把这轮框成"定时汇报",模型醒来不知道该继续干自己的活,反过来问没人会回答的问题。
#   心法=自驱蹲守循环:醒来→自己重读数据源→有命中才上报→需要就再等→到终点才收口,
#   全程不请示。通用机制,不做任务类型判断——盯文件/盯接口/阶段性长活都是同一个循环。
#   注意:文案不点名具体工具(部署可用 background_main_agent_allowed_tools 收窄工具集,
#   prompt 不得引用可能不可用的工具名;可用工具清单见 Available Control Actions)。
def _scheduled_continuation_prompt(reason: str) -> str:
    return (
        "定时唤醒:多半是你自己登记的等待提醒到点了(Active Wake Signal 里的 wait_reason 是你当时"
        "写下的原因)。这是你手上任务的【续跑轮】,不是新对话:没有新的用户消息,提问不会有人回答——"
        "别提问、别等指示,按任务已有的授权自主决策。先看 Recent Messages / Bound Tasks / Active Wake "
        "Signal 回忆任务目标和上次进度,然后用 Available Control Actions 里列出的工具接着干:\n"
        "1) 该重读的数据源/文件就自己再读一遍,该推进的活就推进。增量读要【从上次记下的游标"
        "(行号/偏移)接续读到当前末尾】,别用固定行数的尾部窗口凑——窗口对不齐会漏掉中间的行"
        "(真机实锤漏过目标行);每轮读完把新游标记进账本或 wait 原因里。\n"
        "2) 只在有真命中/关键进展时详细上报(哪一条、证据、为什么算命中);拿不准的迷惑项不要报;"
        "没有新情况就一句话说明,别硬凑汇报。判读纪律:采集/拉取数据可以写脚本代劳,但【每条候选"
        "是否命中必须你自己按判据判断】(通常要同时看触发端和结果/响应端才能定性),关键字/正则"
        "匹配不算判断;脚本报 0 命中≠真没有——先抽样读几条原始数据核实,再下结论。\n"
        "3) 任务还没到终点 → 本轮的活处理完就结束本轮(循环提醒会按间隔再叫你;间隔不合适就重新"
        "登记等待提醒);不要在一轮里原地反复轮询。\n"
        "4) 任务到终点了(时长/条件已满足或活干完了)→ 把结果汇总写进任务交付目录并提交验收收口;"
        "收口通过后系统会自动停掉这个任务的循环提醒。\n"
        "5) 有子代理还在跑就先别整合,等完成事件;发现挂了的用调度工具重拉;重拉/给提示都救不回的"
        "就了结取消掉,别让一个卡死的子代理拖住任务、也别因此丢掉你自己的判断改用死板脚本顶替。"
        "接管子代理的活=你必须自己【真取到数据、逐条判完】;取数被出站闸拦(内网地址,"
        "NETWORK_PRIVATE_HOST_BLOCKED)就先走授权(用户点名过的目标用 authorize_network_host 落白名单)"
        "再取——拿'够不到/没权限'的报告当交付收口不算完成。\n"
        f"唤醒原因:{reason}"
    )


def background_prompt(reason: str) -> str:
    if str(reason or "").strip().lower() in _SUBAGENT_LIFECYCLE_WAKE_REASONS:
        # 子代理有新进展把你叫回来了。这是「由客观信号驱动的编排收尾循环」(提炼自 会话运行时/长期助手/通道运行时/
        #   ralph 等业界成熟做法):①子代理产出=待你验证的材料,不是"已完成";②未全终态先等、全终态才整合;
        #   ③整合是你不可外包的活,自己动手拼+跑起来验(退出码=完成判据,非自述);④别过早收手也别撒谎说完成;
        #   ⑤连续修不过就熔断——交结构化诊断,绝不无限重派 verifier/recovery 空转。
        return (
            "你派出的子代理有新进展把你唤醒了(完成 / 汇报 / 卡住 / 申请能力)。先看 Active Wake Signal、"
            "Recent Observations、Agent Tree Snapshot 看清【整体】局面。核心心法:子代理交回来的产出是"
            "【待你验证的材料】,不是'已经完成'——你的职责是把它们收成一个【真能跑】的交付物,亲手验证过才算数。按下面走:\n"
            "1) 子代理的常规能力申请(shell / 写自己任务沙箱)系统已【机制层自动批并自动续派】,不用你管;"
            "resolve_capability_requests 只处理剩下的特殊申请(网络 / MCP / skill / 越界路径 / 高风险)——"
            "看到这类未决申请立刻批或拒,别晾着让它 BLOCKED(网络类=用户点名过的内网主机,先用 "
            "authorize_network_host 落白名单再批,只批工具解不了出站拦截)。没有未决申请却卡着的子代理,"
            "用 dispatch_subagents 重派或 send_guidance 补提示;确实救不回来的用 cancel_subagents 了结"
            "(其遗留申请会一并了结),别让一个空壳拖住整个任务。\n"
            "2) 还有子代理在 RUNNING / PENDING(没全部终态)→ 现在【别整合、别派新子代理】:处理完能力/阻塞后调 "
            "wait 结束本轮,等它们全部完成再一次性整合(别对半成品反复整合、反复唤醒空转)。\n"
            "3) 子代理【全部终态】了 → 收尾是你自己的活,别派子代理:用 read_file 读齐所有子代理产物"
            "(通常在 work/child_outputs),用 write_file/edit_file 把它们【拼成一个能跑的完整项目】放进本任务交付目录"
            "(成型、可运行,不是散落碎片)。\n"
            "4) 【客观验证才算完成】:自己 run_command 真跑一遍(装依赖 / 跑导入 / 跑测试 / build),看退出码——"
            "通过了才 submit_for_acceptance 交付;没通过就接着修再跑。每整合验收完一块,用 task_progress 把对应"
            "待办标 done(派工时已自动登记进账本);待办没清空别收尾。别自称完成、别为了收尾撒谎说做好了;"
            "也别过早收手:只要再干点活能让成品更完整更对,就干完再交。\n"
            "5) 【连续修不过就熔断,别空转】:同一处连续修 2-3 次还过不了,就【停止死磕】——把'卡在哪、"
            "试过什么、建议怎么办'写成结构化诊断,连同已完成的部分一起交付。这也是合格交付,远比停在碎片或无限空转强。\n"
            "6) 【盯守类编队】:某路盯守子代理终态但盯守窗口没走完时,系统会机制层自动补岗"
            "(建接管 run 从游标续盯,观察流里有 watch_lane_respawned 记录);你用 watch_stream(action=list) "
            "核对每路 window_complete——没走完的路必须有人在岗,补岗没生效就自己 dispatch_subagents 重派,"
            "别把'有一路提前收工'当成整个盯守任务可以收尾。\n"
            "铁律:这是【整合收尾轮】,你手上是读 + 写 + 跑命令 + 交付的工具(这轮【没有派子代理的工具】)——"
            "读取、整合、验证、收尾全是你自己动手;缺哪块就自己补上,确实补不了的就如实标注这块缺失,别停在半成品。"
            f"\n唤醒原因:{reason}"
        )
    if str(reason or "").strip().lower() in _SCHEDULED_WAKE_REASONS:
        return _scheduled_continuation_prompt(reason)
    return (
        "后台主代理被唤醒。请基于持久会话、任务绑定和代理树状态判断下一步："
        "如果只是定时汇报，就给出清楚的阶段进展；如果发现子代理阻塞或需要推进，可以调用调度工具。"
        f"\n唤醒原因：{reason}"
    )


def default_route_target(thread: ConversationThread, route_channel: str) -> str:
    for binding in thread.channel_bindings:
        if binding.channel == route_channel:
            return binding.channel_conversation_id
    return thread.channel_bindings[-1].channel_conversation_id if thread.channel_bindings else thread.thread_id


def json_block(value: Any) -> str:
    return "```json\n" + json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n```"


def pending_wake_payload(store: ConversationStore, thread_id: str, *, limit: int) -> list[dict[str, Any]]:
    return [item.to_dict() for item in store.pending_wake_signals(limit=limit) if item.thread_id == thread_id]


def wake_signal_payload(signal: WakeSignal | dict[str, Any] | None) -> dict[str, Any] | None:
    if isinstance(signal, WakeSignal):
        return signal.to_dict()
    return dict(signal) if isinstance(signal, dict) else None


def observations_by_thread(observations: list[ObservationEvent]) -> dict[str, list[ObservationEvent]]:
    grouped: dict[str, list[ObservationEvent]] = {}
    for observation in observations:
        grouped.setdefault(observation.thread_id, []).append(observation)
    return grouped


def first_root_task_id(observations: list[ObservationEvent]) -> str:
    return next((item.root_task_id for item in observations if item.root_task_id), "")


def claim_heartbeat_interval_seconds(*, ttl_seconds: int, configured_interval_seconds: float | None) -> float:
    ttl = max(1.0, float(ttl_seconds or 1))
    if configured_interval_seconds is not None:
        interval = max(0.05, float(configured_interval_seconds))
    elif ttl >= 90.0:
        interval = max(30.0, ttl / 3.0)
    else:
        interval = max(0.05, ttl / 3.0)
    return min(interval, max(0.05, ttl * 0.8))


def now(value: float | None = None) -> float:
    return float(time.time() if value is None else value)

# Conversation runtime channel snapshots
from .models import ConversationThread
from .store import ConversationStore


class ChannelMessageRuntime:
    def __init__(self, *, runtime: BackgroundMainAgentRuntime, store: ConversationStore):
        self.runtime = runtime
        self.store = store

    def receive(self, request: dict) -> ConversationThread:
        current = now(request.get("now"))
        thread = self.store.get_or_create_thread(
            {
                "canonical_user_id": request.get("canonical_user_id", ""),
                "channel": request.get("channel", ""),
                "channel_conversation_id": request.get("channel_conversation_id", ""),
                "channel_user_id": request.get("channel_user_id", ""),
                "reuse_latest_for_user": True,
                "owner_id": _agent_owner_id(self.runtime.agent),
                "owner_home": _agent_owner_home(self.runtime.agent),
                "now": current,
            }
        )
        self.store.append_message({"thread_id": thread.thread_id, "role": "user", "content": request.get("content", ""), "channel": request.get("channel", ""), "now": current})
        if request.get("run_background", True):
            self.runtime.run_once({"thread_id": thread.thread_id, "reason": "incoming_channel_message", "route_channel": request.get("channel", ""), "route_target": request.get("channel_conversation_id", ""), "now": current})
        latest = self.store.load_thread(thread.thread_id)
        if latest is None:
            raise KeyError(f"unknown conversation thread: {thread.thread_id}")
        return latest


def _agent_owner_id(agent: object) -> str:
    home_paths = getattr(agent, "home_paths", None)
    return str(getattr(home_paths, "owner_id", "") or "")


def _agent_owner_home(agent: object) -> str:
    home_paths = getattr(agent, "home_paths", None)
    return str(getattr(home_paths, "owner_home_dir", "") or "")

# Conversation runtime tool policy
"""Background main-agent tool policy helpers."""


from dataclasses import dataclass
from typing import Any

# 唤醒续作的工作工具集:后台唤醒轮不是"只能看和调度"的旁观轮——被叫回的主代理是同一个
#   任务循环的续跑,必须能真干活(读产物/盯数据源/写交付/跑验证/记账/收尾)。历史断点实锤
#   (P1 持续监控 0/8 命中):scheduled/default 轮只有控制类工具、连 read_file 都没有,定时
#   唤醒回来"字面上读不了文件"→ 声称读不到、反过来问没人会回答的问题、任务空转到死。
_BACKGROUND_WORK_TOOLS = (
    "read_file",
    "list_files",
    "search_text",
    "write_file",
    "edit_file",
    "run_command",
    # watch_stream 必须在唤醒轮可用:主代理 solo 盯守时每轮醒来继续 pull 候选批;
    # 整合轮用它 list/status 查各路盯守窗口走没走完(补岗判断的事实来源)。
    "watch_stream",
    "task_progress",
    "submit_for_acceptance",
    "resolve_capability_requests",
    # cancel_subagents 必须在唤醒轮可用:收尾门(SUBAGENTS_UNRESOLVED)明确指引"取消/接管/
    # 重跑",但真机 0/22 实锤唤醒轮里根本没有取消工具——救不回来的 BLOCKED 子代理既解不了
    # 阻也了结不掉,整条编队被一个空壳拖死。
    "cancel_subagents",
    # authorize_network_host 同理要在唤醒轮可用:子代理撞私网出站闸(NETWORK_PRIVATE_HOST_BLOCKED)
    # 提能力申请后,叫回的主代理得能当场把用户点名的内网监控目标落白名单(真机回归② N1:
    # 5 个盯源子代理全卡该闸放弃,整条编队随之崩)。
    "authorize_network_host",
)

DEFAULT_BACKGROUND_ALLOWED_TOOLS = (
    "wait",
    "inspect_agent_tree",
    "raise_event",
    "raise_collaboration",
    "inspect_collaboration",
    "submit_collaboration_result",
    "update_collaboration",
    "dispatch_subagents",
    "send_guidance",
    "create_subagents",
    *_BACKGROUND_WORK_TOOLS,
)

# 定时/自设提醒唤醒轮:续跑自己的持续任务(盯数据源/周期自查/推进未完事项)。带全部工作
#   工具;不含 create_subagents——定时轮不该开新拆解(防"整合轮反复派子代理空转"同款回归),
#   重拉已有失败子代理用 dispatch_subagents。
SCHEDULED_BACKGROUND_ALLOWED_TOOLS = (
    "wait",
    "inspect_agent_tree",
    "inspect_collaboration",
    "dispatch_subagents",
    "send_guidance",
    *_BACKGROUND_WORK_TOOLS,
)

# 子代理生命周期唤醒(完成/要汇报/卡住/申请能力)叫回主代理时,它要真干活——读子代理产物、
# 写最终交付、自检、提交验收、批准能力——所以工具集必须含整合工具,而不是只能再 inspect/wait。
# 这是"叫回来了却干不了活"那处最关键断点的修复(对齐 终端应用:同对话续跑用全套工具收口)。
# 唤醒后整合工具集:给读+整合+交付的工具,但【去掉 create_subagents】——唤醒回来是自己
#   read_file 读子代理产物、整合成交付,不是再派新孙代理去"读"(实测会派读取孙代理绕圈)。
#   保留 dispatch_subagents(重派已有失败子代理,非创建新的)+ send_guidance(给卡住的补提示)。
SUBAGENT_INTEGRATION_ALLOWED_TOOLS = tuple(
    t for t in DEFAULT_BACKGROUND_ALLOWED_TOOLS if t != "create_subagents"
)

CONTROL_ACTION_DESCRIPTIONS = {
    "wait": "登记到点自动唤醒你的非阻塞提醒；等子代理进度、盯持续变化的数据/文件都用它，不要原地轮询。",
    "inspect_agent_tree": "只读查看主/子/孙代理状态树。",
    "raise_event": "记录普通进展、阻塞或需要主代理处理的事件。",
    "raise_collaboration": "发起协作；没有 case_id 时开 case，有 question/target 时同步发 request。",
    "inspect_collaboration": "只读查看协作 case 或待处理协作请求。",
    "submit_collaboration_result": "提交协作命中、未命中、证据引用和限制说明。",
    "update_collaboration": "更新协作 case 或 request；带 target_agent_ids 可改派请求。",
    "dispatch_subagents": "只有需要推进、恢复或调度时才调用。",
    "send_guidance": "给正在运行的代理追加软提示。",
    "create_subagents": "创建并启动新的下级代理。",
    "read_file": "读取子代理产出的文件/产物,用于整合与验收。",
    "list_files": "查看子代理在工作区写了哪些产物。",
    "search_text": "在子代理产物里检索内容。",
    "write_file": "写最终交付物,或把子代理产物整合成成品。",
    "edit_file": "修订/整合已有交付文件。",
    "run_command": "运行 import/测试做交付前自检。",
    "watch_stream": "高频数据流盯守摄取:pull 持续消费流并只把结构化稀有候选批给你判;list/status 查各路盯守覆盖与窗口进度。",
    "task_progress": "更新任务清单进展。",
    "submit_for_acceptance": "子代理产物整合完、自检过后,提交系统验收收口。",
    "resolve_capability_requests": "批准或拒绝子代理的能力申请,让它能继续干。",
    "cancel_subagents": "了结救不回来的子代理(重派/给提示都无效时),别让空壳拖住整个任务收尾。",
}


@dataclass(frozen=True)
class BackgroundToolPolicyRequest:
    """Facts used to choose the background main-agent control tool profile."""

    reason: str = ""
    wake_signal: dict[str, Any] | None = None
    config: object | None = None
    owner_policy: object | None = None
    policy_snapshot: dict[str, Any] | None = None


@dataclass(frozen=True)
class BackgroundToolPolicyDecision:
    """Final background tool list plus where each restriction came from."""

    allowed_tools: tuple[str, ...]
    profile: str
    sources: tuple[str, ...]
    removed_tools: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "background-tool-policy.v1",
            "profile": self.profile,
            "allowed_tools": list(self.allowed_tools),
            "sources": list(self.sources),
            "removed_tools": list(self.removed_tools),
        }


def background_allowed_tools(
    config: object | None = None,
    request: BackgroundToolPolicyRequest | None = None,
) -> list[str]:
    return list(background_tool_policy_decision(config, request=request).allowed_tools)


def background_tool_policy_decision(
    config: object | None = None,
    request: BackgroundToolPolicyRequest | None = None,
) -> BackgroundToolPolicyDecision:
    if request is None:
        request = BackgroundToolPolicyRequest(config=config)
    config = request.config if request.config is not None else config
    configured = tool_names(getattr(config, "background_main_agent_allowed_tools", None))
    if configured:
        tools = configured
        profile = "configured"
        sources = ["agent_config.background_main_agent_allowed_tools"]
    else:
        profile, default_tools = _default_profile_for_request(request)
        tools = list(default_tools)
        sources = [f"default_profile:{profile}"]
    tools, removed = _apply_policy_limits(tools, request)
    if removed:
        sources.append("owner_or_task_policy")
    return BackgroundToolPolicyDecision(
        allowed_tools=tuple(tools),
        profile=profile,
        sources=tuple(sources),
        removed_tools=tuple(removed),
    )


def background_control_action_lines(
    config: object | None = None,
    request: BackgroundToolPolicyRequest | None = None,
) -> list[str]:
    lines: list[str] = []
    for name in background_allowed_tools(config, request=request):
        description = CONTROL_ACTION_DESCRIPTIONS.get(name)
        if description:
            lines.append(f"- {name}: {description}")
    return lines


def tool_names(value: object) -> list[str]:
    if isinstance(value, str):
        raw_items = value.split(",")
    elif isinstance(value, (list, tuple)):
        raw_items = value
    else:
        return []
    return list(dict.fromkeys(str(item).strip() for item in raw_items if str(item).strip()))


def _default_profile_for_request(request: BackgroundToolPolicyRequest) -> tuple[str, tuple[str, ...]]:
    # 子代理生命周期唤醒(完成/汇报/卡住/能力申请)叫回主代理时要真整合收口,优先给整合工具集。
    if _is_subagent_lifecycle_wake(request):
        return "subagent_integration", SUBAGENT_INTEGRATION_ALLOWED_TOOLS
    if _is_urgent_wake(request):
        return "urgent", DEFAULT_BACKGROUND_ALLOWED_TOOLS
    if _is_scheduled_progress(request):
        return "scheduled_progress", SCHEDULED_BACKGROUND_ALLOWED_TOOLS
    return "default", DEFAULT_BACKGROUND_ALLOWED_TOOLS


def _apply_policy_limits(
    tools: list[str],
    request: BackgroundToolPolicyRequest,
) -> tuple[list[str], list[str]]:
    allowed = list(dict.fromkeys(tools))
    task_policy = request.policy_snapshot if isinstance(request.policy_snapshot, dict) else {}
    task_allowed = tool_names(task_policy.get("allowed_tools"))
    if task_allowed:
        allowed = [tool for tool in allowed if tool in set(task_allowed)]
    disabled = set(_disabled_tools_from_owner(request.owner_policy))
    disabled.update(tool_names(task_policy.get("disabled_tools")))
    filtered = [tool for tool in allowed if tool not in disabled]
    removed = [tool for tool in allowed if tool not in filtered]
    return filtered, removed


def _disabled_tools_from_owner(owner_policy: object | None) -> list[str]:
    return tool_names(getattr(owner_policy, "disabled_tools", ()))


def _is_urgent_wake(request: BackgroundToolPolicyRequest) -> bool:
    wake = request.wake_signal if isinstance(request.wake_signal, dict) else {}
    urgency = str(wake.get("urgency") or "").strip().lower()
    reason = str(request.reason or wake.get("reason") or "").strip().lower()
    return urgency == "urgent" or reason in {"urgent_wake_signal", "wake_signal"}


# 定时类唤醒 reason 的权威名单:工具策略(_is_scheduled_progress)与提示词分支
#   (background_prompt → _scheduled_continuation_prompt)共用,防两处漂移。
_SCHEDULED_WAKE_REASONS = frozenset(
    {"scheduled_progress_report", "progress_policy_due", "due_progress_policy"}
)


def _is_scheduled_progress(request: BackgroundToolPolicyRequest) -> bool:
    return str(request.reason or "").strip().lower() in _SCHEDULED_WAKE_REASONS


# 子代理→主代理的"生命周期"推送:完成/卡住/失败(subagent_runner_finished)、申请能力
# (subagent_capability_request_open)、能力获批可续跑(subagent_capability_granted)。
# 这些唤醒叫回主代理是为了真整合收口/批能力,所以要给整合工具集(见 SUBAGENT_INTEGRATION_ALLOWED_TOOLS)。
_SUBAGENT_LIFECYCLE_WAKE_REASONS = {
    "subagent_runner_finished",
    "subagent_capability_request_open",
    "subagent_capability_granted",
}


def _is_subagent_lifecycle_wake(request: BackgroundToolPolicyRequest) -> bool:
    wake = request.wake_signal if isinstance(request.wake_signal, dict) else {}
    reason = str(request.reason or wake.get("reason") or "").strip().lower()
    return reason in _SUBAGENT_LIFECYCLE_WAKE_REASONS

# Conversation runtime worker
from dataclasses import dataclass
from typing import Any

from ..agent_core.runtime.loop_models import RunParams
from ..runtime_errors import DataCorruptionError
from .channels import PROACTIVE_PUSH_CHANNELS, ChannelSendRequest, FakeChannelHub
from .models import BackgroundMainAgentReport, WakeSignal
from .store import ConversationStore


@dataclass(frozen=True)
class BackgroundRunRequest:
    thread_id: str
    task_id: str = ""
    reason: str = "scheduled_progress_report"
    route_channel: str = "internal"
    route_target: str = ""
    now: float = 0.0
    wake_signal: dict[str, Any] | None = None


class BackgroundMainAgentRuntime:
    def __init__(self, *, agent: object, store: ConversationStore, channels: FakeChannelHub | None = None):
        self.agent = agent
        self.store = store
        self.channels = channels or FakeChannelHub()

    def run_once(self, params: dict) -> BackgroundMainAgentReport:
        request = _run_request(params)
        if callable(getattr(self.store, "load_thread_report", None)):
            thread, load_error = self.store.load_thread_report(request.thread_id)
            if load_error is not None:
                raise DataCorruptionError(str(load_error))
        else:
            thread = self.store.load_thread(request.thread_id)
        if thread is None:
            raise KeyError(f"unknown conversation thread: {request.thread_id}")
        response, tool_call_count, tool_success_count = self._run_agent(thread, request)
        channel, target = _resolve_delivery_route(thread, request)
        send_request = ChannelSendRequest(channel=channel, target=target, content=response, thread_id=request.thread_id, task_id=request.task_id)
        self._record_response(request, send_request)
        return BackgroundMainAgentReport(
            thread_id=request.thread_id,
            task_id=request.task_id,
            reason=request.reason,
            response=response,
            route_channel=channel,
            route_target=target,
            created_at=request.now,
            tool_call_count=tool_call_count,
            tool_success_count=tool_success_count,
        )

    def _run_agent(self, thread, request: BackgroundRunRequest) -> tuple[str, int, int]:
        result = self.agent.run(
            background_prompt(request.reason),
            params=_run_params(thread.thread_id, request, self.agent),
            inject=[context_markdown(agent=self.agent, store=self.store, thread=thread, request=request)],
        )
        calls = [item for item in (getattr(result, "archive_tool_calls", None) or []) if isinstance(item, dict)]
        successes = sum(1 for item in calls if item.get("ok") is True)
        return str(getattr(result, "response", "") or ""), len(calls), successes

    def _record_response(self, request: BackgroundRunRequest, send_request: ChannelSendRequest) -> None:
        self.store.append_message({"thread_id": request.thread_id, "role": "assistant", "content": send_request.content, "channel": send_request.channel, "now": request.now, "metadata": {"reason": request.reason, "task_id": request.task_id}})
        self.channels.send(send_request)


# 后台主代理产出的投递路由。只有"内部/无真实外部路由"(子代理事件叫回、定时巡检默认走 internal)才
# 尝试升级成主动外呼:若该会话绑过可主动外呼的通道(飞书)就投到那个通道,这样"叫回来产出的汇总"才发
# 得到用户所在真渠道、不进内部黑洞。显式外部路由(feishu/wechat/qq/chat…进度策略或入站消息带来的)一律
# 原样尊重,不改既有语义;没有可外呼绑定则保持原路由(internal → 单机/CLI 行为不变)。
_INTERNAL_ROUTE_CHANNELS = frozenset({"internal", ""})


def _resolve_delivery_route(thread: object, request: BackgroundRunRequest) -> tuple[str, str]:
    channel = str(getattr(request, "route_channel", "") or "")
    route_target = str(getattr(request, "route_target", "") or "")
    if channel in _INTERNAL_ROUTE_CHANNELS:
        binding = _latest_proactive_binding(thread)
        if binding is not None:
            # 飞书 send_message 用 receive_id_type=open_id,需要用户 open_id(=binding.channel_user_id);
            # 缺失才回落 channel_conversation_id。
            return binding.channel, (binding.channel_user_id or binding.channel_conversation_id)
    return channel, route_target or default_route_target(thread, channel)


def _latest_proactive_binding(thread: object):
    candidates = [
        binding
        for binding in getattr(thread, "channel_bindings", ()) or ()
        if getattr(binding, "channel", "") in PROACTIVE_PUSH_CHANNELS
        and (getattr(binding, "channel_user_id", "") or getattr(binding, "channel_conversation_id", ""))
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda binding: float(getattr(binding, "last_active_at", 0.0) or 0.0))


def _run_request(kwargs: dict[str, Any]) -> BackgroundRunRequest:
    current = now(kwargs.get("now"))
    return BackgroundRunRequest(
        thread_id=str(kwargs.get("thread_id") or ""),
        task_id=str(kwargs.get("task_id") or ""),
        reason=str(kwargs.get("reason") or "scheduled_progress_report"),
        route_channel=str(kwargs.get("route_channel") or "internal"),
        route_target=str(kwargs.get("route_target") or ""),
        now=current,
        wake_signal=wake_signal_payload(kwargs.get("wake_signal")),
    )


def _run_params(thread_id: str, request: BackgroundRunRequest, agent: object | None = None) -> RunParams:
    config = getattr(agent, "config", None)
    return RunParams(
        save=False,
        source="background_main_agent",
        run_id=f"bg-main-{thread_id}",
        task_id=request.task_id or thread_id,
        allowed_tools=background_allowed_tools(
            config,
            request=BackgroundToolPolicyRequest(
                reason=request.reason,
                wake_signal=request.wake_signal,
                config=config,
                owner_policy=getattr(agent, "owner_policy", None),
                policy_snapshot=_policy_snapshot_from_request(request),
            ),
        ),
    )


def _policy_snapshot_from_request(request: BackgroundRunRequest) -> dict[str, Any]:
    wake = request.wake_signal if isinstance(request.wake_signal, dict) else {}
    snapshot = wake.get("policy_snapshot")
    return dict(snapshot) if isinstance(snapshot, dict) else {}

# Conversation runtime context
from dataclasses import dataclass
from typing import Any

from ..agent_core.agent_tree.status import agent_tree_status_payload
from ..artifacts.registry import latest_artifact_records
from ..runtime_errors import runtime_error_report
from ..settings.defaults import default_config_value
from .context_budget import (
    BackgroundContextPayloadRequest,
    background_context_budget_from_config,
    bounded_background_context_payload,
)
from .models import ConversationThread
from .store import ConversationStore


@dataclass(frozen=True)
class _BackgroundContextLoad:
    agent: object
    store: ConversationStore
    thread: ConversationThread
    config: object | None
    policy_request: BackgroundToolPolicyRequest
    load_errors: list[dict[str, Any]]


def context_markdown(*, agent: object, store: ConversationStore, thread: ConversationThread, request) -> str:
    policy_request = _tool_policy_request(agent, request)
    bounded = _bounded_context(agent, store, thread, policy_request)
    policy_decision = background_tool_policy_decision(getattr(agent, "config", None), request=policy_request)
    sections = [
        ("Active Wake Signal", request.wake_signal or {}),
        ("Conversation Thread", bounded["thread"]),
        ("Runtime Load Errors", bounded.get("load_errors") or []),
        ("Recent Messages", bounded["messages"]),
        ("Bound Tasks", bounded["tasks"]),
        ("Channel Bindings", bounded["channel_bindings"]),
        ("Recent Observations", bounded["observations"]),
        ("Pending Guidance", bounded["guidance"]),
        ("Pending Wake Signals", bounded["pending_wake_signals"]),
        ("Recovery Snapshot", bounded["recovery_snapshot"]),
        ("Agent Tree Snapshot", bounded["agent_tree"]),
        ("Control Action Policy", policy_decision.to_dict()),
    ]
    lines = _context_header(request, thread)
    for title, payload in sections:
        lines.extend(["", f"## {title}", json_block(payload)])
    lines.extend([
        "",
        "## Available Control Actions",
        *background_control_action_lines(getattr(agent, "config", None), request=policy_request),
        "[/background-main-agent-context]",
    ])
    return "\n".join(lines)


def _bounded_context(
    agent: object,
    store: ConversationStore,
    thread: ConversationThread,
    policy_request: BackgroundToolPolicyRequest,
) -> dict[str, Any]:
    config = getattr(agent, "config", None)
    load_errors: list[dict[str, Any]] = []
    state = _BackgroundContextLoad(agent, store, thread, config, policy_request, load_errors)
    visible_run_ids = _thread_active_task_ids(state)
    agent_tree = _agent_tree_payload(state, visible_run_ids)
    bundle = _context_bundle(state)
    pending_wake_signals = _pending_wake_signals(state)
    recovery_snapshot = _safe_recovery_snapshot(state, visible_run_ids)
    return bounded_background_context_payload(
        BackgroundContextPayloadRequest(
            bundle=bundle,
            pending_wake_signals=pending_wake_signals,
            agent_tree=agent_tree,
            recovery_snapshot=recovery_snapshot,
            load_errors=load_errors,
            budget=background_context_budget_from_config(config),
        )
    )


def _context_bundle(state: _BackgroundContextLoad) -> dict[str, Any]:
    try:
        if callable(getattr(state.store, "context_bundle_report", None)):
            bundle, load_errors = state.store.context_bundle_report(
                state.thread.thread_id,
                recent_limit=_config_int(state.config, "conversation_context_recent_limit"),
            )
            state.load_errors.extend(load_errors)
            return bundle
        return state.store.context_bundle(
            state.thread.thread_id,
            recent_limit=_config_int(state.config, "conversation_context_recent_limit"),
        )
    except Exception as exc:
        state.load_errors.append(runtime_error_report(exc, context="background_context.context_bundle"))
        return _minimal_context_bundle(state.thread)


def _pending_wake_signals(state: _BackgroundContextLoad) -> list[dict[str, Any]]:
    try:
        if callable(getattr(state.store, "pending_wake_signals_report", None)):
            signals, load_errors = state.store.pending_wake_signals_report(
                limit=_config_int(state.config, "background_pending_wake_prompt_limit"),
            )
            state.load_errors.extend(load_errors)
            return [item.to_dict() for item in signals if item.thread_id == state.thread.thread_id]
        return pending_wake_payload(
            state.store,
            state.thread.thread_id,
            limit=_config_int(state.config, "background_pending_wake_prompt_limit"),
        )
    except Exception as exc:
        state.load_errors.append(runtime_error_report(exc, context="background_context.pending_wake_signals"))
        return []


def _agent_tree_payload(
    state: _BackgroundContextLoad,
    visible_run_ids: list[str],
) -> dict[str, Any]:
    try:
        return agent_tree_status_payload(
            state.agent,
            {
                "visible_run_ids": visible_run_ids,
                "allowed_tools": background_allowed_tools(state.config, request=state.policy_request),
            },
        )
    except Exception as exc:
        report = runtime_error_report(exc, context="background_context.agent_tree")
        state.load_errors.append(report)
        return {
            "schema_version": "agent_tree_status.v1",
            "effect": "read_only",
            "nodes": [],
            "edges": [],
            "warnings": ["agent_tree_load_error"],
            "load_error": report,
        }


def _safe_recovery_snapshot(
    state: _BackgroundContextLoad,
    visible_run_ids: list[str],
) -> dict[str, Any]:
    try:
        return _recovery_snapshot(state.agent, state.store, state.thread.thread_id, visible_run_ids)
    except Exception as exc:
        report = runtime_error_report(exc, context="background_context.recovery_snapshot")
        state.load_errors.append(report)
        return {
            "schema_version": "background_recovery_snapshot.v1",
            "effect": "read_only",
            "does_not_block": True,
            "load_error": report,
        }


def _tool_policy_request(agent: object, request: object) -> BackgroundToolPolicyRequest:
    return BackgroundToolPolicyRequest(
        reason=str(getattr(request, "reason", "") or ""),
        wake_signal=getattr(request, "wake_signal", None),
        config=getattr(agent, "config", None),
        owner_policy=getattr(agent, "owner_policy", None),
        policy_snapshot=_policy_snapshot_from_request(request),
    )


def _policy_snapshot_from_request(request: object) -> dict[str, Any]:
    wake = getattr(request, "wake_signal", None)
    if isinstance(wake, dict) and isinstance(wake.get("policy_snapshot"), dict):
        return dict(wake["policy_snapshot"])
    return {}


def _config_int(config: object | None, key: str) -> int:
    if config is None:
        return max(0, int(default_config_value(key)))
    try:
        return max(0, int(getattr(config, key)))
    except (TypeError, ValueError):
        return max(0, int(default_config_value(key)))


def _context_header(request, thread: ConversationThread) -> list[str]:
    return [
        "[background-main-agent-context]",
        f"reason: {request.reason}",
        f"thread_id: {thread.thread_id}",
        f"task_id: {request.task_id or ''}",
    ]


def _thread_active_task_ids(state: _BackgroundContextLoad) -> list[str]:
    latest = _latest_thread_with_load_error(state)
    source = latest or state.thread
    return [str(item or "").strip() for item in source.active_task_ids if str(item or "").strip()]


def _latest_thread_with_load_error(state: _BackgroundContextLoad) -> ConversationThread | None:
    try:
        if not callable(getattr(state.store, "load_thread_report", None)):
            return state.store.load_thread(state.thread.thread_id)
        latest, load_error = state.store.load_thread_report(state.thread.thread_id)
        if load_error is not None:
            load_error["consumer_context"] = "background_context.thread_active_tasks"
            state.load_errors.append(load_error)
        return latest
    except Exception as exc:
        state.load_errors.append(runtime_error_report(exc, context="background_context.thread_active_tasks"))
        return None


def _minimal_context_bundle(thread: ConversationThread) -> dict[str, Any]:
    return {
        "thread": thread.to_dict(),
        "messages": [],
        "tasks": [],
        "channel_bindings": [item.to_dict() for item in thread.channel_bindings],
        "observations": [],
        "guidance": [],
    }


def _recovery_snapshot(agent: object, store: ConversationStore, thread_id: str, visible_run_ids: list[str]) -> dict[str, Any]:
    claim = store.load_background_run_claim(thread_id)
    previous = claim.get("previous_claim") if isinstance(claim.get("previous_claim"), dict) else {}
    tree = agent_tree_status_payload(agent, {"visible_run_ids": visible_run_ids})
    records = latest_artifact_records(getattr(agent, "root", "."))
    payload = {
        "schema_version": "background_recovery_snapshot.v1",
        "effect": "read_only",
        "does_not_block": True,
        "current_claim_status": str(claim.get("status") or ""),
        "current_claim_id": str(claim.get("claim_id") or ""),
        "current_claim_reason": str(claim.get("reason") or ""),
        "previous_claim_status": str(previous.get("status") or ""),
        "previous_claim_error": previous.get("last_error") if isinstance(previous.get("last_error"), dict) else {},
        "takeover": claim.get("takeover") if isinstance(claim.get("takeover"), dict) else {},
        "tree_status_buckets": tree.get("status_buckets") if isinstance(tree.get("status_buckets"), dict) else {},
        "artifact_registry_count": len(records),
        "artifact_registry_status_counts": _artifact_status_counts(records),
        "takeover_advice": "接手前先核对 claim、任务树和产物登记；不要把模型文本里的完成声明当成事实。",
    }
    if isinstance(claim.get("load_error"), dict):
        payload["claim_load_error"] = claim["load_error"]
    return payload


def _artifact_status_counts(records: dict[str, object]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records.values():
        status = str(getattr(record, "status", "") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts

# Conversation runtime scheduler
import logging
import threading
from typing import TYPE_CHECKING

from ..agent_core.agent_tree.status import agent_tree_status_payload
from ..settings.defaults import default_config_int
from .models import BackgroundMainAgentReport, ObservationEvent, ProgressPolicy, WakeSignal

# 后台 claim 心跳是 daemon 线程，其异常必须结构化落日志而非裸崩 stderr 杀线程。
_HEARTBEAT_LOGGER = logging.getLogger("agent.conversation.background_claim_heartbeat")

_TASK_LINK_TERMINAL_STATUSES = frozenset({
    "ABANDONED",
    "CANCELLED",
    "CHANNEL_ERROR",
    "DONE",
    "FAILED",
    "TAKEN_OVER",
    "TIMEOUT",
})
_MIN_PROGRESS_POLICY_CATCHUP_SECONDS = 7200
_MAX_PROGRESS_POLICY_CATCHUP_INTERVALS = 4
from .store import ConversationStore

if TYPE_CHECKING:
    from ..collaboration import CollaborationStore


class BackgroundMainAgentScheduler:
    def __init__(self, config: dict):
        runtime = config["runtime"]
        store = config["store"]
        collaboration_store = config.get("collaboration_store")
        self.runtime = runtime
        self.store = store
        self.collaboration_store = collaboration_store or getattr(self.runtime.agent, "collaboration_store", None)
        agent_config = getattr(getattr(self.runtime, "agent", None), "config", None)
        claim_ttl_seconds = config.get("claim_ttl_seconds", _agent_config_int(agent_config, "background_claim_ttl_seconds"))
        configured_claim_heartbeat_interval_seconds = config.get(
            "claim_heartbeat_interval_seconds",
            _agent_config_int(agent_config, "background_claim_heartbeat_interval_seconds"),
        )
        self.claim_ttl_seconds = max(1, int(claim_ttl_seconds or 1))
        self.claim_heartbeat_interval_seconds = claim_heartbeat_interval_seconds(
            ttl_seconds=self.claim_ttl_seconds,
            configured_interval_seconds=configured_claim_heartbeat_interval_seconds,
        )
        self.last_progress_policy_load_errors: list[dict[str, object]] = []
        self.last_progress_policy_suppressed: list[dict[str, object]] = []

    def tick(self, *, now: float | None = None) -> list[BackgroundMainAgentReport]:
        current = now if now is not None else __import__("time").time()
        self._process_collaboration_cases(now=current)
        reports: list[BackgroundMainAgentReport] = []
        reported = self._run_wake_signals(reports, current)
        self._run_observation_batches(reports, reported, current)
        self._run_due_policies(reports, reported, current)
        return reports

    def _process_collaboration_cases(self, *, now: float) -> None:
        if self.collaboration_store is None:
            return
        from ..collaboration import CollaborationCoordinator
        CollaborationCoordinator(store=self.collaboration_store, conversation_store=self.store).tick(now=now)

    def _run_wake_signals(self, reports: list[BackgroundMainAgentReport], current: float) -> set[str]:
        reported: set[str] = set()
        handled: set[str] = set()
        wake_signals = self.store.pending_wake_signals(limit=self._config_limit("conversation_pending_wake_limit"))
        for signal in wake_signals:
            if signal.wake_signal_id in handled:
                continue
            if signal.thread_id in reported:
                self._mark_signal(signal, current, handled)
                continue
            report = self._run_wake_signal(signal, now=current)
            if report is not None:
                reports.append(report)
                reported.add(report.thread_id)
                self._mark_sibling_signals(wake_signals, signal.thread_id, current, handled)
        return reported

    def _run_observation_batches(self, reports: list[BackgroundMainAgentReport], reported: set[str], current: float) -> None:
        pending_observations = self.store.unhandled_observations_requiring_main(
            limit=self._config_limit("conversation_unhandled_observation_limit")
        )
        for thread_id, thread_observations in observations_by_thread(pending_observations).items():
            if thread_id in reported:
                continue
            report = self._run_observation_batch(thread_id, thread_observations, now=current)
            if report is not None:
                reports.append(report)
                reported.add(report.thread_id)

    def _run_due_policies(self, reports: list[BackgroundMainAgentReport], reported: set[str], current: float) -> None:
        policies, load_errors = self.store.due_progress_policies_report(now=current)
        self.last_progress_policy_load_errors = load_errors
        self.last_progress_policy_suppressed = []
        runnable, suppressed = _runnable_due_policies(self.store, policies, now=current)
        self.last_progress_policy_suppressed = _snooze_suppressed_policies(self.store, suppressed, now=current)
        for policy in runnable:
            if policy.thread_id in reported:
                continue
            if report := self._run_due_policy(policy, now=current):
                reports.append(report)

    def _run_wake_signal(self, signal: WakeSignal, *, now: float) -> BackgroundMainAgentReport | None:
        # 关键:透传 signal 的【真实 reason】(subagent_runner_finished / capability_request_open 等),
        #   不要用泛泛的 "wake_signal" 盖掉它——否则 background_prompt 掉进泛泛提示词(拿不到整合收敛引导)、
        #   且 _is_subagent_lifecycle_wake 判 False → 拿到含 create_subagents 的默认工具集(主代理能派
        #   verifier/recovery 子代理空转)。透传后:子代理生命周期唤醒 → 整合工具集(无 create_subagents,
        #   结构级逼主代理自己整合)+ 整合收敛提示词。非生命周期唤醒(无 reason)回落原 urgent/wake_signal。
        lifecycle_reason = str(getattr(signal, "reason", "") or "").strip()
        reason = lifecycle_reason or ("urgent_wake_signal" if signal.urgency == "urgent" else "wake_signal")
        self._pre_wake_capability_sweep(lifecycle_reason, signal)
        report = self._run_claimed({"thread_id": signal.thread_id, "task_id": signal.root_task_id, "reason": reason, "now": now, "wake_signal": signal})
        if report is not None:
            self.store.mark_wake_signal_handled(signal.wake_signal_id, now=now)
        return report

    # LLM: 子代理生命周期唤醒进 LLM 整合轮之前的机制层预处理(§5.1 头号靶的 wake 端半边):
    #   常规能力申请自动批 + BLOCKED/孤儿候选全量续派,全部确定性动作,不依赖模型调
    #   resolve_capability_requests / dispatch_subagents。失败静默记日志,唤醒轮照常进行。
    def _pre_wake_capability_sweep(self, lifecycle_reason: str, signal: WakeSignal) -> None:
        from ..agent_core.orchestration.dispatch.capability_auto_sweep import (
            auto_capability_sweep,
            sweep_applies_to_reason,
        )

        if not sweep_applies_to_reason(lifecycle_reason):
            return
        try:
            auto_capability_sweep(self.runtime.agent, signal)
        except Exception:
            _HEARTBEAT_LOGGER.warning("pre-wake capability sweep failed", exc_info=True)

    def _watch_lane_sweep_quietly(self) -> None:
        _scheduled_watch_lane_sweep(self.runtime.agent)

    def _run_observation_batch(self, thread_id: str, observations: list[ObservationEvent], *, now: float) -> BackgroundMainAgentReport | None:
        report = self._run_claimed({"thread_id": thread_id, "task_id": first_root_task_id(observations), "reason": "observation_requires_main_agent", "now": now})
        if report is not None:
            self.store.mark_observations_handled([item.observation_id for item in observations], now=now)
        return report

    def _run_due_policy(self, policy: ProgressPolicy, *, now: float) -> BackgroundMainAgentReport | None:
        self._watch_lane_sweep_quietly()
        report = self._run_claimed(
            {
                "thread_id": policy.thread_id,
                "task_id": policy.task_id,
                "reason": "scheduled_progress_report",
                "route_channel": policy.route_channel,
                "route_target": policy.route_target,
                "now": now,
                "wake_signal": _progress_policy_wake_payload(policy),
            }
        )
        if report is not None:
            self.store.mark_progress_reported(
                policy.policy_id, now=now, no_progress_streak=_next_no_progress_streak(policy, report)
            )
        return report

    def _run_claimed(self, kwargs: dict) -> BackgroundMainAgentReport | None:
        claim = self.store.claim_background_run({
            "thread_id": kwargs.get("thread_id", ""),
            "task_id": kwargs.get("task_id", ""),
            "reason": kwargs.get("reason", ""),
            "lease_seconds": self.claim_ttl_seconds,
            "now": kwargs.get("now"),
        })
        if claim is None:
            return None
        return self._run_with_heartbeat(str(claim.get("claim_id") or ""), kwargs)

    def _run_with_heartbeat(self, claim_id: str, kwargs: dict) -> BackgroundMainAgentReport | None:
        heartbeat = self._start_heartbeat(claim_id, kwargs["thread_id"])
        status = "finished"
        error: BaseException | None = None
        try:
            return self.runtime.run_once(kwargs)
        except BaseException as exc:
            status = "failed"
            error = exc
            raise
        finally:
            heartbeat.stop()
            self.store.finish_background_run({
                "thread_id": kwargs["thread_id"],
                "claim_id": claim_id,
                "task_id": kwargs.get("task_id", ""),
                "status": status,
                "error": error,
                "runtime_facts": self._runtime_facts(),
                "now": now(),
            })

    def _start_heartbeat(self, claim_id: str, thread_id: str) -> _BackgroundClaimHeartbeat:
        heartbeat = _BackgroundClaimHeartbeat({"store": self.store, "thread_id": thread_id, "claim_id": claim_id, "lease_seconds": self.claim_ttl_seconds, "interval_seconds": self.claim_heartbeat_interval_seconds})
        heartbeat.start()
        return heartbeat

    def _mark_signal(self, signal: WakeSignal, current: float, handled: set[str]) -> None:
        self.store.mark_wake_signal_handled(signal.wake_signal_id, now=current)
        handled.add(signal.wake_signal_id)

    def _mark_sibling_signals(self, signals: list[WakeSignal], thread_id: str, current: float, handled: set[str]) -> None:
        for signal in signals:
            if signal.thread_id == thread_id and signal.wake_signal_id not in handled:
                self._mark_signal(signal, current, handled)

    def _config_limit(self, key: str) -> int:
        return _agent_config_int(getattr(getattr(self.runtime, "agent", None), "config", None), key)

    def _runtime_facts(self) -> dict[str, object]:
        agent = getattr(self.runtime, "agent", None)
        tree = agent_tree_status_payload(agent, {}) if agent is not None else {}
        return {
            "current_tool": str(getattr(agent, "_current_tool", "") or ""),
            "last_progress_at": float(getattr(agent, "_last_progress_at", 0.0) or 0.0),
            "last_progress_summary": str(getattr(agent, "_last_progress_summary", "") or ""),
            "tree_status_buckets": tree.get("status_buckets") if isinstance(tree.get("status_buckets"), dict) else {},
            "progress_policy_load_errors": list(self.last_progress_policy_load_errors),
            "progress_policy_suppressed": list(self.last_progress_policy_suppressed),
        }


# 函数用途: 把到点的 progress policy 摊开成 Active Wake Signal 载荷——被唤醒的模型要能看到
#   "这是我自己登记的提醒 + 当时写下的原因(wait_reason)",而不是一个没头没尾的定时汇报。
# §6-B4 无进展退避判据(纯结构化信号,不做任何文本判断):本唤醒轮一次成功的工具调用都
# 没有(全失败或零调用)=无进展轮,streak+1;有任一成功调用(健康守望每轮至少读一次数据源)
# =有进展,streak 归零。streak 由 store 落进 policy.metadata 并按 2^streak 拉长间隔(封顶),
# 让卡死任务自动让出调度资源(真机:BLOCKED 子代理让主代理每分钟醒来空转解阻,饿死建站用户)。
def _next_no_progress_streak(policy: ProgressPolicy, report: BackgroundMainAgentReport) -> int:
    if report.tool_success_count > 0:
        return 0
    metadata = policy.metadata if isinstance(policy.metadata, dict) else {}
    try:
        previous = int(metadata.get("no_progress_streak") or 0)
    except (TypeError, ValueError):
        previous = 0
    return max(0, previous) + 1


def _progress_policy_wake_payload(policy: ProgressPolicy) -> dict[str, object]:
    metadata = policy.metadata if isinstance(policy.metadata, dict) else {}
    return {
        "kind": "progress_policy_due",
        "reason": "scheduled_progress_report",
        "policy_id": policy.policy_id,
        "task_id": policy.task_id,
        "interval_seconds": policy.interval_seconds,
        "wait_reason": str(metadata.get("reason") or ""),
        "registered_by_tool": str(metadata.get("tool") or ""),
        "watch_run_id": str(metadata.get("watch_run_id") or ""),
    }


# 函数用途: 定时提醒唤醒路上的盯守补岗兜底——被 cancel/没触发 wake 信号的死岗也能在
#   下一个到点提醒被机制层补上(建接管 run 后立即续派拉起);无盯守状态时近零开销。
def _scheduled_watch_lane_sweep(agent: object) -> None:
    try:
        from ..agent_core.orchestration.dispatch.capability_auto_sweep import (
            _redispatch_stalled_subagents,
        )
        from ..agent_core.orchestration.dispatch.watch_lane_sweep import (
            respawn_dead_watch_lanes,
        )

        if respawn_dead_watch_lanes(agent):
            _redispatch_stalled_subagents(agent)
    except Exception:
        _HEARTBEAT_LOGGER.debug("scheduled watch lane sweep failed", exc_info=True)


def _prefer_progress_policy(first: ProgressPolicy, second: ProgressPolicy) -> ProgressPolicy:
    first_score = (first.last_report_at, first.next_due_at, first.policy_id)
    second_score = (second.last_report_at, second.next_due_at, second.policy_id)
    return second if second_score > first_score else first


def _runnable_due_policies(
    store,
    policies: list[ProgressPolicy],
    *,
    now: float,
) -> tuple[list[ProgressPolicy], list[tuple[ProgressPolicy, str]]]:
    selected_by_key: dict[tuple[str, str, str, str], ProgressPolicy] = {}
    suppressed: list[tuple[ProgressPolicy, str]] = []
    for policy in policies:
        reason = _progress_policy_suppression_reason(store, policy, now=now)
        if reason:
            suppressed.append((policy, reason))
            continue
        key = (policy.thread_id, policy.task_id, policy.route_channel, policy.route_target)
        existing = selected_by_key.get(key)
        if existing is None:
            selected_by_key[key] = policy
            continue
        selected = _prefer_progress_policy(existing, policy)
        skipped = existing if selected is policy else policy
        selected_by_key[key] = selected
        suppressed.append((skipped, "duplicate_policy"))
    return list(selected_by_key.values()), suppressed


def _progress_policy_suppression_reason(store, policy: ProgressPolicy, *, now: float) -> str:
    if _policy_task_link_is_terminal(store, policy):
        return "terminal_task_link"
    if _progress_policy_is_stale(policy, now=now):
        return "stale_missed_interval"
    return ""


def _policy_task_link_is_terminal(store, policy: ProgressPolicy) -> bool:
    if not policy.task_id:
        return False
    try:
        links = store.task_links(policy.thread_id)
    except Exception:
        return False
    for link in links:
        if link.task_id == policy.task_id and str(link.status or "").upper() in _TASK_LINK_TERMINAL_STATUSES:
            return True
    return False


# 被抑制后应"退休"(disable)而非"续命"的原因:被观察任务已终态,或策略早已 stale(错过整个
# 追赶窗口=任务多半已死/无可挽回)。这两类若继续 mark_progress_reported 续命,会被无限复活、
# 每个间隔唤醒后台主代理发一次 LLM 进度汇报,占满 gateway worker(churn 根因)。
# duplicate_policy 不退休(只是本轮去重,真身仍活),继续续命留作后备。
_RETIRE_SUPPRESSION_REASONS = frozenset({"terminal_task_link", "stale_missed_interval"})


def _apply_suppressed_policy(store, policy: ProgressPolicy, reason: str, *, now: float) -> None:
    try:
        if reason in _RETIRE_SUPPRESSION_REASONS:
            store.disable_progress_policy(policy.policy_id, now=now)
        else:
            store.mark_progress_reported(policy.policy_id, now=now)
    except Exception:
        pass


def _snooze_suppressed_policies(
    store,
    suppressed: list[tuple[ProgressPolicy, str]],
    *,
    now: float,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for policy, reason in suppressed:
        rows.append(
            {
                "policy_id": policy.policy_id,
                "thread_id": policy.thread_id,
                "task_id": policy.task_id,
                "reason": reason,
            }
        )
        _apply_suppressed_policy(store, policy, reason, now=now)
    return rows


def _progress_policy_is_stale(policy: ProgressPolicy, *, now: float) -> bool:
    if policy.next_due_at <= 0:
        return False
    interval = max(1, int(policy.interval_seconds or 1))
    catchup_window = max(_MIN_PROGRESS_POLICY_CATCHUP_SECONDS, interval * _MAX_PROGRESS_POLICY_CATCHUP_INTERVALS)
    return now - policy.next_due_at > catchup_window


def _agent_config_int(config: object | None, key: str) -> int:
    if config is None:
        return default_config_int(key, minimum=0)
    try:
        return max(0, int(getattr(config, key)))
    except (TypeError, ValueError):
        return default_config_int(key, minimum=0)


class _BackgroundClaimHeartbeat(threading.Thread):
    def __init__(self, config: dict):
        thread_id = str(config.get("thread_id") or "")
        super().__init__(name=f"bg-claim-heartbeat-{thread_id}", daemon=True)
        self.store = config["store"]
        self.thread_id = thread_id
        self.claim_id = str(config.get("claim_id") or "")
        self.lease_seconds = max(1, int(config.get("lease_seconds") or 1))
        self.interval_seconds = max(0.05, float(config.get("interval_seconds") or 0.05))
        self.stop_event = threading.Event()

    def stop(self) -> None:
        self.stop_event.set()
        self.join(timeout=self.interval_seconds + 5.0)

    def run(self) -> None:
        while not self.stop_event.wait(self.interval_seconds):
            try:
                renewed = self.store.renew_background_run_claim({"thread_id": self.thread_id, "claim_id": self.claim_id, "lease_seconds": self.lease_seconds, "now": now()})
            except BaseException as exc:  # noqa: BLE001 - daemon 心跳绝不裸崩
                # 兜底：renew 遇任何异常（未知线程 KeyError、IO 错、极端下 store 根竞态）都不能让
                # 未捕获异常杀死这条 daemon 心跳线程、连累被叫回的 run。记结构化账后优雅停机；
                # 根因（线程缺失）已在 renew 层软化为返回 None，这里是防御纵深的最后一层。
                _HEARTBEAT_LOGGER.warning(
                    "background claim heartbeat stopped early thread=%s claim=%s: %s",
                    self.thread_id,
                    self.claim_id,
                    runtime_error_report(exc, context="background_claim_heartbeat.renew"),
                )
                return
            if renewed is None:
                return
