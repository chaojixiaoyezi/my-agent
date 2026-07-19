# Conversation runtime utilities
from __future__ import annotations

import json
import time
from functools import partial
from typing import Any

from ..backends.errors import is_provider_transient_error, is_provider_usage_limit_error
from ..concurrency.interrupt import register_interruptible
from ..runtime_errors import compact_error_message
from ..settings.runtime_guard_config import runtime_guard_int
from .control_commands import conversation_request_interrupt_name
from .models import (
    SUBAGENT_LIFECYCLE_WAKE_REASONS,
    ConversationThread,
    ObservationEvent,
    WakeSignal,
)
from .store import ConversationStore
from .task_runtime_state import task_runtime_state


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
        "2) 【一条命中=一条结论,逐条入账】:每确认一条真命中,先用 record_finding 入账一条"
        "(claim=该条唯一 ID+结果端依据)。结论账是主代理内部续跑和收口依据,由你读取、核验后"
        "整合成自然的用户回复;不要把账本标题、记录 ID 或内部记录块原样发给用户。面向用户说明"
        "真实新发现时,仍要交代哪一条、证据和为什么算命中,禁止只说'计数在涨/又有 N 条'。"
        "拿不准的迷惑项不要报;没有新情况就一句话说明,别硬凑汇报。判读纪律:"
        "盯守/持续消费高频数据流必须用 watch_stream(pull 拉候选批),【禁止自写轮询脚本替代】"
        "——自写脚本没有游标持久/覆盖账目/结构化宽筛,真机实锤自定判据错、误报数万;只有没有"
        "专用工具的数据面(如本地文件/日志)才写脚本采集代劳。无论哪种,【每条候选是否命中必须"
        "你自己独立判断】(通常要同时看触发端和结果/响应端才能定性)——关键字/正则匹配不算判断,"
        "命中你自己配的判据也不算判断(判据可能配错,高频取值多半是常态);脚本报 0 命中≠真没有——"
        "先抽样读几条原始数据核实,再下结论。\n"
        "2b) 【任务清单还有 open 项 → 活没做完,汇报完接着干】:先读任务清单(coverage),还有未 done/"
        "未 skipped 的项就继续推进——工具集里有【派新子代理】的工具时把剩余项续派出去(每个 item 的 "
        'covers 带对应清单项 id,如 covers:["req-07"]),否则自己动手做;确认不适用的项标 skipped '
        "写原因。别把定时唤醒当成只交一句进展汇报。\n"
        "3) 任务还没到终点 → 本轮的活处理完就结束本轮(循环提醒会按间隔再叫你;间隔不合适就重新"
        "登记等待提醒);不要在一轮里原地反复轮询。\n"
        "4) 任务到终点了(时长/条件已满足或活干完了)→ 把结果汇总写进任务交付目录，跑完针对性验证，"
        "然后给最终回复；运行时会据本轮终态停止这个任务的循环提醒。\n"
        "5) 有子代理还在跑就先别整合,等完成事件;发现挂了的用调度工具重拉;重拉/给提示都救不回的"
        "就了结取消掉,别让一个卡死的子代理拖住任务、也别因此丢掉你自己的判断改用死板脚本顶替。"
        "接管子代理的活=你必须自己【真取到数据、逐条判完】;取数被出站闸拦(内网地址,"
        "NETWORK_PRIVATE_HOST_BLOCKED)就先走授权(用户点名过的目标用 authorize_network_host 落白名单)"
        "再取——拿'够不到/没权限'的报告冒充完成不算完成。\n"
        f"唤醒原因:{reason}"
    )


# 子代理有新进展把主代理叫回来的整合收敛提示词。这是「由客观信号驱动的编排收尾循环」
#   (提炼自 会话运行时/长期助手/通道运行时/ralph 等业界成熟做法):①子代理产出=待你验证的材料,
#   不是"已完成";②未全终态先等、全终态才整合;③整合是你不可外包的活,自己动手拼+跑起来验
#   (退出码=完成判据,非自述);④别过早收手也别撒谎说完成;⑤连续修不过就熔断——交结构化
#   诊断,绝不无限重派 verifier/recovery 空转。
_SUBAGENT_INTEGRATION_WAKE_PROMPT = (
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
    "3) 子代理【全部终态】但任务清单还有 open 项 → 【先续推,别收口】:用 task_progress(action=read) "
    "看 coverage 清单,还有未 done/未 skipped 的项就说明活没做完——这轮的首要职责是把剩下的项推下去:"
    "工具集里有 create_subagents 时,把剩余 open 项续派出去(每个 item 的 covers 带对应清单项 id,如 "
    'covers:["req-07"],goal 写明做哪项);项少或收尾性质的就自己动手做完。清单没清空以前,'
    "【看一眼状态就收口是错误行为】——打完勾的项不用管,没打勾的项必须有人接着做。\n"
    "3b) 子代理【全部终态】且清单已清空(或本来没有清单)→ 收尾是你自己的活,别派子代理:用 read_file 读齐所有子代理产物"
    "(通常在 work/child_outputs),用 write_file/edit_file 把它们【拼成一个能跑的完整项目】放进本任务交付目录"
    "(成型、可运行,不是散落碎片)。【先对账再整合】:每条 run 的增量结论账在其 findings_ledger"
    "(Agent Tree Snapshot 节点的 workspace_refs.findings_ledger,findings_recorded>0 的必读)——"
    "被取消/收尾崩的路,已确认结论都在账里,合并进最终报告,别跟着 run 一起扔掉。\n"
    "4) 【客观验证才算完成】:自己 run_command 真跑一遍(装依赖 / 跑导入 / 跑测试 / build),看退出码——"
    "通过后才给最终回复;没通过就接着修再跑。每整合验证完一块,用 task_progress 把对应"
    "待办标 done(派工时已自动登记进账本);待办没清空别收尾。别自称完成、别为了收尾撒谎说做好了;"
    "也别过早收手:只要再干点活能让成品更完整更对,就干完再交。\n"
    "4b) 【逐模块对照拆解清单,别让交付缩水】:每个子代理节点的 goal_digest 就是派工时的计划——"
    "逐条核对'计划要的 vs 实际交付的':哪个子代理崩了/被取消了,它负责的模块不能就地消失,"
    "你要么按它的 goal 自己补建到同等完成度(读它的账本和半成品当底子),要么在最终报告里"
    "如实标注'该模块缺失及原因'。整合不是把收到的碎片拼一下——是把【计划承诺的完整交付】补齐。\n"
    "5) 【连续修不过就熔断,别空转】:同一处连续修 2-3 次还过不了,就【停止死磕】——把'卡在哪、"
    "试过什么、建议怎么办'写成结构化诊断,连同已完成的部分一起交付。这也是合格交付,远比停在碎片或无限空转强。\n"
    "6) 【盯守类编队】:某路盯守子代理终态但盯守窗口没走完时,系统会机制层自动补岗"
    "(建接管 run 从游标续盯,观察流里有 watch_lane_respawned 记录);你用 watch_stream(action=list) "
    "核对每路 window_complete——没走完的路必须有人在岗,补岗没生效就自己 dispatch_subagents 重派,"
    "别把'有一路提前收工'当成整个盯守任务可以收尾。\n"
    "6b) 【持续型任务窗口未走完】:Active Wake Signal / Recent Observations 里带 "
    "service_window_incomplete=true 的子代理,承担的是声明过值守窗口的持续型任务,窗口没走完就退了"
    "——它的岗位现在空着。先用 dispatch_subagents 重派(或自己接管把值守续上),把它已产出的部分"
    "当中间成果收好;别把这条提前退出当成任务完成去整合收尾。\n"
    "铁律:这是【整合收尾轮】,工具集按账本状态配发——任务清单还有 open 项时你有 create_subagents"
    "(专用于续派清单剩余项,不是拿来派'读产物/验证'类你自己该干的活);清单全闭后【没有派子代理的工具】,"
    "读取、整合、验证、收尾全是你自己动手;缺哪块就自己补上,确实补不了的就如实标注这块缺失,别停在半成品。"
)


_GOAL_SUBAGENTS_ACTIVE_PROMPT = (
    "\n\nStructured runtime fact: one or more subagents related to this exact goal task are still "
    "nonterminal. Their lifecycle events will wake this same goal again. Do not poll them and do "
    "not call wait merely to schedule another check. Continue only parent-owned work that is "
    "currently possible; otherwise end this internal turn. Do not integrate incomplete child "
    "outputs and do not mark the goal complete."
)


def background_prompt(
    reason: str,
    *,
    goal: object | None = None,
    goal_subagent_phase: str = "",
    wake_signal: dict[str, Any] | None = None,
) -> str:
    normalized_reason = str(reason or "").strip().lower()
    if normalized_reason == "scheduled_job_due":
        wake = wake_signal if isinstance(wake_signal, dict) else {}
        metadata = wake.get("metadata") if isinstance(wake.get("metadata"), dict) else {}
        prompt = str(metadata.get("scheduler_prompt") or "").strip()
        return (
            "这是当前用户先前在同一会话中登记的持久计划，现在已经到点。"
            "把下面的内容当作该用户本轮的真实要求，结合这条会话已有上下文直接执行；"
            "普通回复仍由你根据真实执行结果自然撰写。\n\n"
            + prompt
        )
    if goal is not None and normalized_reason in {
        "thread_goal_continue",
        *SUBAGENT_LIFECYCLE_WAKE_REASONS,
    }:
        from .goal_prompting import continuation_prompt

        prompt = continuation_prompt(goal)
        if goal_subagent_phase == "subagents_active":
            return prompt + _GOAL_SUBAGENTS_ACTIVE_PROMPT
        if goal_subagent_phase == "subagents_terminal":
            return prompt + "\n\n" + _SUBAGENT_INTEGRATION_WAKE_PROMPT + f"\n唤醒原因:{reason}"
        return prompt
    if normalized_reason == "thread_goal_continue":
        return "Continue working toward the active thread goal. Call get_goal first."
    if normalized_reason in SUBAGENT_LIFECYCLE_WAKE_REASONS:
        return _SUBAGENT_INTEGRATION_WAKE_PROMPT + f"\n唤醒原因:{reason}"
    if normalized_reason in _SCHEDULED_WAKE_REASONS:
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
    # LLM: 0 是配置层约定的“自动”，不是 50ms；自动值必须显著小于 TTL，持续续租同时避免热写。
    # 函数用途: 把显式心跳或 0/None 自动配置归一成安全的实际秒数。
    ttl = max(1.0, float(ttl_seconds or 1))
    configured = float(configured_interval_seconds or 0.0)
    if configured > 0:
        interval = max(0.05, configured)
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
    # record_finding 必须在唤醒轮可用(§2 逐条结论根治的机制半边):盯守唤醒轮确认一条
    # 命中就入账一条;此前唤醒轮工具集里根本没有它,模型字面上记不了逐条账,只能出聚合概述。
    "record_finding",
    "task_progress",
    "resolve_capability_requests",
    # cancel_subagents 必须在唤醒轮可用：救不回来的 BLOCKED 子代理需要能被明确取消或接管，
    # 否则父代理无法结束其协作生命周期。
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

GOAL_BACKGROUND_ALLOWED_TOOLS = (
    *DEFAULT_BACKGROUND_ALLOWED_TOOLS,
    "get_goal",
    "update_goal",
)

# 子代理生命周期唤醒(完成/要汇报/卡住/申请能力)叫回主代理时,它要真干活——读子代理产物、
# 写最终交付、运行自检、批准能力——所以工具集必须含整合工具,而不是只能再 inspect/wait。
# 这是"叫回来了却干不了活"那处最关键断点的修复(对齐 终端应用:同对话续跑用全套工具收口)。
# 唤醒后整合工具集:给读+整合+交付的工具,但【去掉 create_subagents】——唤醒回来是自己
#   read_file 读子代理产物、整合成交付,不是再派新孙代理去"读"(实测会派读取孙代理绕圈)。
#   保留 dispatch_subagents(重派已有失败子代理,非创建新的)+ send_guidance(给卡住的补提示)。
SUBAGENT_INTEGRATION_ALLOWED_TOOLS = tuple(
    t for t in DEFAULT_BACKGROUND_ALLOWED_TOOLS if t != "create_subagents"
)

# 续推变体(不足3·派工叫回后不续):任务主账本 coverage 清单还有未闭环项=活没做完,唤醒/定时轮
#   必须有"把剩下的项续派出去"的通道,否则唤醒链只剩收敛动作、大工程如实停在半截(真机
#   u-fixtest2 停 8/24)。判据是纯结构信号(清单计数>0),清单全闭后仍用上面的无派工集合——
#   "整合轮派读取孙代理绕圈"的原防护只在没活可派时才该生效。
SUBAGENT_INTEGRATION_CONTINUE_ALLOWED_TOOLS = (*SUBAGENT_INTEGRATION_ALLOWED_TOOLS, "create_subagents")
SCHEDULED_CONTINUE_ALLOWED_TOOLS = (*SCHEDULED_BACKGROUND_ALLOWED_TOOLS, "create_subagents")
GOAL_SUBAGENTS_ACTIVE_ALLOWED_TOOLS = tuple(
    tool for tool in GOAL_BACKGROUND_ALLOWED_TOOLS if tool not in {"create_subagents", "wait"}
)
GOAL_SUBAGENTS_TERMINAL_ALLOWED_TOOLS = tuple(
    dict.fromkeys((*SUBAGENT_INTEGRATION_ALLOWED_TOOLS, "get_goal", "update_goal"))
)
GOAL_SUBAGENTS_TERMINAL_CONTINUE_ALLOWED_TOOLS = (
    *GOAL_SUBAGENTS_TERMINAL_ALLOWED_TOOLS,
    "create_subagents",
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
    "record_finding": "确认一条结论立刻入账一条(claim=条目唯一 ID+依据),收尾崩/重派不丢;账本只供主代理续跑和整合,用户只接收核验后的自然回复。",
    "task_progress": "更新任务清单进展。",
    "resolve_capability_requests": "批准或拒绝子代理的能力申请,让它能继续干。",
    "cancel_subagents": "了结救不回来的子代理(重派/给提示都无效时),别让空壳拖住整个任务收尾。",
    "get_goal": "读取当前 /goal 持续目标及其权威状态。",
    "update_goal": "仅在持续目标真正完成或确实阻塞时写入 complete/blocked 终态。",
}


@dataclass(frozen=True)
class BackgroundToolPolicyRequest:
    """Facts used to choose the background main-agent control tool profile."""

    reason: str = ""
    wake_signal: dict[str, Any] | None = None
    config: object | None = None
    owner_policy: object | None = None
    policy_snapshot: dict[str, Any] | None = None
    # 任务主账本 coverage 清单未闭环项计数(不足3续推开路的结构判据;调用方读账填充,失败=0)。
    open_coverage_targets: int = 0
    # These fields come only from the persisted goal/task/subagent records. They
    # never depend on model prose or natural-language intent classification.
    active_goal: bool = False
    goal_subagent_phase: str = ""


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
    # 主账本清单还有未闭环项(open_coverage_targets>0,纯结构信号)时给续推变体(含
    # create_subagents):活没做完的唤醒/定时轮必须派得动,否则叫回后只剩收敛动作(不足3)。
    reason = str(request.reason or "").strip().lower()
    if request.active_goal and (
        reason == "thread_goal_continue" or reason in SUBAGENT_LIFECYCLE_WAKE_REASONS
    ):
        if request.goal_subagent_phase == "subagents_active":
            return "thread_goal_subagents_active", GOAL_SUBAGENTS_ACTIVE_ALLOWED_TOOLS
        if request.goal_subagent_phase == "subagents_terminal":
            if request.open_coverage_targets > 0:
                return (
                    "thread_goal_subagents_terminal_continue",
                    GOAL_SUBAGENTS_TERMINAL_CONTINUE_ALLOWED_TOOLS,
                )
            return "thread_goal_subagents_terminal", GOAL_SUBAGENTS_TERMINAL_ALLOWED_TOOLS
    if reason == "thread_goal_continue":
        return "thread_goal", GOAL_BACKGROUND_ALLOWED_TOOLS
    if _is_subagent_lifecycle_wake(request):
        if request.open_coverage_targets > 0:
            return "subagent_integration_continue", SUBAGENT_INTEGRATION_CONTINUE_ALLOWED_TOOLS
        return "subagent_integration", SUBAGENT_INTEGRATION_ALLOWED_TOOLS
    if _is_urgent_wake(request):
        return "urgent", DEFAULT_BACKGROUND_ALLOWED_TOOLS
    if _is_scheduled_progress(request):
        if request.open_coverage_targets > 0:
            return "scheduled_progress_continue", SCHEDULED_CONTINUE_ALLOWED_TOOLS
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
    # observation_requires_main_agent = 子代理 raise_event(urgent) 的真事件被观察批叫回主代理。
    # 真机根 bug:它原来不在此集 → 落 default 分支拿到含 create_subagents 的工具集 → 主代理不上报、
    # 反去重派工(真事件永远不发用户)。它语义就是"紧急事件待上报",归入 urgent → 走上报提示词分支。
    return urgency == "urgent" or reason in {"urgent_wake_signal", "wake_signal", "observation_requires_main_agent"}


# 定时类唤醒 reason 的权威名单:工具策略(_is_scheduled_progress)与提示词分支
#   (background_prompt → _scheduled_continuation_prompt)共用,防两处漂移。
_SCHEDULED_WAKE_REASONS = frozenset(
    {"scheduled_progress_report", "progress_policy_due", "due_progress_policy", "scheduled_job_due"}
)


def _is_scheduled_progress(request: BackgroundToolPolicyRequest) -> bool:
    return str(request.reason or "").strip().lower() in _SCHEDULED_WAKE_REASONS


# 子代理→主代理的"生命周期"推送:完成/卡住/失败(subagent_runner_finished)、申请能力
# (subagent_capability_request_open)、能力获批可续跑(subagent_capability_granted)。
# 这些唤醒叫回主代理是为了真整合收口/批能力,所以要给整合工具集(见 SUBAGENT_INTEGRATION_ALLOWED_TOOLS)。
def _is_subagent_lifecycle_wake(request: BackgroundToolPolicyRequest) -> bool:
    wake = request.wake_signal if isinstance(request.wake_signal, dict) else {}
    reason = str(request.reason or wake.get("reason") or "").strip().lower()
    return reason in SUBAGENT_LIFECYCLE_WAKE_REASONS

# Conversation runtime worker
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..agent_core.runtime.loop_models import RunParams
from ..runtime_errors import DataCorruptionError
from .channels import (
    PROACTIVE_PUSH_CHANNELS,
    ChannelAttachment,
    DeliveryContext,
    DeliveryServiceProtocol,
    FakeDeliveryService,
    ReplyEnvelope,
    project_user_reply,
)
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


@dataclass(frozen=True)
class GoalRuntimeContext:
    goal: object | None = None
    subagent_phase: str = ""
    state_error: str = ""


def _goal_runtime_context(
    agent: object,
    store: ConversationStore,
    request: BackgroundRunRequest,
) -> GoalRuntimeContext:
    """Resolve one exact active goal and its child phase from durable state only."""
    task_id = str(request.task_id or "").strip()
    if not task_id:
        return GoalRuntimeContext()
    try:
        goal = store.load_goal(request.thread_id)
    except Exception:
        return GoalRuntimeContext(state_error="goal_state_load_error")
    if (
        goal is None
        or str(getattr(goal, "task_id", "") or "").strip() != task_id
        or str(getattr(goal, "status", "") or "").strip().lower() != "active"
    ):
        return GoalRuntimeContext()
    phase, state_error = _goal_subagent_phase(agent, task_id)
    return GoalRuntimeContext(goal=goal, subagent_phase=phase, state_error=state_error)


class BackgroundMainAgentRuntime:
    def __init__(self, *, agent: object, store: ConversationStore, channels: DeliveryServiceProtocol | None = None):
        self.agent = agent
        self.store = store
        # The runtime store is the one durable thread authority. Finalization,
        # goal tools, transcript writes, and scheduler reconciliation must not
        # silently use a second ConversationStore instance.
        if getattr(agent, "conversation_store", None) is not store:
            agent.conversation_store = store
        self.channels = channels or FakeDeliveryService()

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
        response, tool_call_count, tool_success_count, delivery_artifacts = self._run_agent(
            thread, request
        )
        channel, target = _resolve_delivery_route(
            thread,
            request,
            supports_proactive=getattr(self.channels, "supports_proactive", None),
        )
        delivery_context = DeliveryContext(
            channel=channel,
            target=target,
            mode="proactive",
            thread_id=request.thread_id,
            task_id=request.task_id,
        )
        deliver, delivery_reason = _background_delivery_decision(
            self.agent,
            request,
            store=self.store,
        )
        reported_content, delivery_status = self._record_response(
            request,
            delivery_context,
            response,
            delivery_artifacts=delivery_artifacts,
            deliver=deliver,
            delivery_reason=delivery_reason,
        )
        return BackgroundMainAgentReport(
            thread_id=request.thread_id,
            task_id=request.task_id,
            reason=request.reason,
            response=reported_content,
            route_channel=channel,
            route_target=target,
            created_at=request.now,
            tool_call_count=tool_call_count,
            tool_success_count=tool_success_count,
            delivery_status=delivery_status,
            delivery_reason=delivery_reason,
        )

    def _run_agent(
        self, thread, request: BackgroundRunRequest
    ) -> tuple[str, int, int, tuple[dict[str, object], ...]]:
        goal_context = _goal_runtime_context(self.agent, self.store, request)
        result = self.agent.run(
            background_prompt(
                request.reason,
                goal=goal_context.goal,
                goal_subagent_phase=goal_context.subagent_phase,
                wake_signal=request.wake_signal,
            ),
            params=_run_params(
                thread.thread_id,
                request,
                self.agent,
                goal_context=goal_context,
            ),
            inject=[context_markdown(agent=self.agent, store=self.store, thread=thread, request=request)],
        )
        calls = [item for item in (getattr(result, "archive_tool_calls", None) or []) if isinstance(item, dict)]
        successes = sum(1 for item in calls if item.get("ok") is True)
        artifacts = tuple(
            dict(item)
            for item in (getattr(result, "delivery_artifacts", None) or [])
            if isinstance(item, dict)
        )
        return str(getattr(result, "response", "") or ""), len(calls), successes, artifacts

    def _record_response(
        self,
        request: BackgroundRunRequest,
        delivery_context: DeliveryContext,
        internal_content: str,
        *,
        delivery_artifacts: tuple[dict[str, object], ...] = (),
        deliver: bool,
        delivery_reason: str,
    ) -> tuple[str, str]:
        # 内部协议仍交给真实 DeliveryService 做主动消息抑制，但普通 transcript/report
        # 只能保存用户投影，否则下一轮 compact 和 owner-local 搜索会被机器协议污染。
        projection = project_user_reply(internal_content)
        if not deliver:
            return projection.content, "suppressed"
        terminal_status = _background_task_link_status(self.agent, request, store=self.store)
        goal_terminal_delivery = delivery_reason in {
            "thread_goal_blocked",
            "thread_goal_budget_limited",
            "thread_goal_usage_limited",
        }
        if (
            terminal_status in {"abandoned", "cancelled", "interrupted", "superseded"}
            and not goal_terminal_delivery
        ):
            return projection.content, "suppressed"
        # ReplyEnvelope is a user-content envelope, not an internal protocol carrier.
        # Sending the already projected text also keeps the real DeliveryService from
        # having to distinguish a valid completion signal from other internal signals.
        attachments = _channel_attachments(delivery_artifacts)
        if not projection.content.strip() and not attachments:
            return "", "suppressed"
        envelope = ReplyEnvelope(content=projection.content, attachments=attachments)
        self.store.append_message(
            {
                "thread_id": request.thread_id,
                "role": "assistant",
                "content": projection.content,
                "channel": delivery_context.channel,
                "metadata": {
                    "reason": request.reason,
                    "task_id": request.task_id,
                    "delivery_artifacts": [dict(item) for item in delivery_artifacts],
                    "projection_status": projection.projection_status,
                    "background_delivery_reason": delivery_reason,
                },
            }
        )
        receipt = self.channels.deliver(delivery_context, envelope)
        return projection.content, str(getattr(receipt, "delivery_status", "sent") or "sent")


def _channel_attachments(
    artifacts: tuple[dict[str, object], ...],
) -> tuple[ChannelAttachment, ...]:
    attachments: list[ChannelAttachment] = []
    for item in artifacts:
        path = str(item.get("path") or "").strip()
        if not path or item.get("ok") is not True:
            continue
        try:
            size_bytes = max(0, int(item.get("size_bytes") or 0))
        except (TypeError, ValueError):
            size_bytes = 0
        attachments.append(
            ChannelAttachment(
                artifact_id=str(item.get("artifact_id") or ""),
                path=path,
                name=str(item.get("name") or Path(path).name),
                kind=str(item.get("kind") or "file"),
                sha256=str(item.get("sha256") or ""),
                size_bytes=size_bytes,
            )
        )
    return tuple(attachments)


def _background_delivery_decision(
    agent: object,
    request: BackgroundRunRequest,
    *,
    store: ConversationStore | None = None,
) -> tuple[bool, str]:
    """Keep partial child integration internal until durable state proves completion."""
    task_status = _background_task_link_status(agent, request, store=store)
    reason = str(request.reason or "").strip().lower()
    goal_status = _matching_goal_status(store, request)
    if goal_status == "complete":
        return True, "thread_goal_completion"
    if goal_status in {"blocked", "budget_limited", "usage_limited"}:
        return True, f"thread_goal_{goal_status}"
    if task_status in {"abandoned", "cancelled", "interrupted", "superseded"}:
        return False, f"task_{task_status}"
    task_completed = task_status == "completed"
    if reason == "thread_goal_continue":
        if task_completed:
            return True, "thread_goal_completion"
        return False, "thread_goal_continuation_internal"
    if goal_status == "active" and reason in SUBAGENT_LIFECYCLE_WAKE_REASONS:
        return False, "thread_goal_lifecycle_internal"
    if reason == "user_guidance" and not task_completed:
        # /btw already has a deterministic control acknowledgement.  Applying the
        # guidance is an internal continuation; a second model status paragraph is
        # noisy and can be stale before the next task checkpoint.
        return False, "user_guidance_applied_internal"
    if reason in _SCHEDULED_WAKE_REASONS and _internal_subagent_continuation(request):
        # wait/自动巡场只是内部续推面，不是用户通知面。即使最后一个 child 恰好在本轮
        # 结束前转为终态，也不能把模型的调度碎碎念送进普通聊天；runner completion
        # 的 wake（或 observation fallback）才是首选整合入口。只有本轮 runtime 已把
        # 精确任务链接转为 completed，才发送模型的自然最终回复。
        if task_completed:
            return True, "internal_scheduled_completion"
        return False, "internal_scheduled_continuation"
    if reason != "subagent_runner_finished":
        return True, "non_subagent_completion"
    wake = request.wake_signal if isinstance(request.wake_signal, dict) else {}
    metadata = wake.get("metadata") if isinstance(wake.get("metadata"), dict) else {}
    status = str(metadata.get("status") or "").strip().upper()
    if status != "DONE":
        return True, "subagent_non_success_terminal"
    root_task_id = str(wake.get("root_task_id") or request.task_id or "").strip()
    if not root_task_id:
        return True, "subagent_root_unknown"
    related, state_error = _related_subagent_runs(agent, root_task_id)
    if state_error:
        # A broken or missing child record cannot prove that the current task tree
        # has settled.  Keep the model's integration turn internal unless the exact
        # durable root-task link already reached completed.  This mirrors the same
        # terminal-state gate used when all child rows are readable and prevents an
        # unrelated historical parse error from leaking partial child chatter.
        if task_completed:
            return True, f"root_task_completed_with_{state_error}"
        return False, state_error
    from ..subagents.models import SUBAGENT_ENDED_STATUSES, task_status_in

    if any(not task_status_in(getattr(task, "status", ""), SUBAGENT_ENDED_STATUSES) for task in related):
        return False, "partial_subagent_success"
    if not task_completed:
        return False, "root_task_still_active"
    return True, "root_subagents_terminal"


def _matching_goal_status(
    store: ConversationStore | None,
    request: BackgroundRunRequest,
) -> str:
    if store is None:
        return ""
    try:
        goal = store.load_goal(request.thread_id)
    except Exception:
        return ""
    if goal is None:
        return ""
    if str(getattr(goal, "task_id", "") or "").strip() != str(request.task_id or "").strip():
        return ""
    return str(getattr(goal, "status", "") or "").strip().lower()


def _background_task_link_status(
    agent: object,
    request: BackgroundRunRequest,
    *,
    store: ConversationStore | None = None,
) -> str:
    """Read the durable root-task state without inferring cancellation from model text."""
    task_id = str(request.task_id or "").strip()
    selected_store = store or getattr(agent, "conversation_store", None)
    if not task_id or selected_store is None:
        return ""
    try:
        links, load_errors = selected_store.task_links_report(request.thread_id)
    except Exception:
        return ""
    if load_errors:
        return ""
    for link in links:
        if str(getattr(link, "task_id", "") or "").strip() == task_id:
            return str(getattr(link, "status", "") or "").strip().lower()
    return ""


def _internal_subagent_continuation(request: BackgroundRunRequest) -> bool:
    wake = request.wake_signal if isinstance(request.wake_signal, dict) else {}
    return str(wake.get("registered_by_tool") or "").strip() in {
        "coverage_open_continuation",
        "dispatch_supervision_auto",
        "wait",
    }


def _goal_subagent_phase(agent: object, root_task_id: str) -> tuple[str, str]:
    """Return a structured child phase for one exact goal task."""
    related, state_error = _related_subagent_runs(agent, root_task_id)
    if state_error == "subagent_root_not_found":
        return "no_subagents", ""
    if state_error:
        return "subagent_state_unknown", state_error
    from ..subagents.models import SUBAGENT_ENDED_STATUSES, task_status_in

    active_count = sum(
        1
        for task in related
        if not task_status_in(getattr(task, "status", ""), SUBAGENT_ENDED_STATUSES)
    )
    if active_count:
        return "subagents_active", ""
    return "subagents_terminal", ""


def _wake_signal_is_stale(
    agent: object,
    store: ConversationStore,
    signal: WakeSignal,
    lifecycle_reason: str,
) -> bool:
    """Drop only wakes whose exact durable task/goal is already terminal."""
    reason = str(lifecycle_reason or "").strip().lower()
    task_id = str(getattr(signal, "root_task_id", "") or "").strip()
    if reason == "thread_goal_continue":
        metadata = signal.metadata if isinstance(signal.metadata, dict) else {}
        try:
            goal = store.load_goal(signal.thread_id)
        except Exception:
            return False
        return (
            goal is None
            or str(getattr(goal, "goal_id", "") or "").strip()
            != str(metadata.get("goal_id") or "").strip()
            or str(getattr(goal, "task_id", "") or "").strip() != task_id
            or str(getattr(goal, "status", "") or "").strip().lower() != "active"
        )
    if not task_id:
        return False
    if reason in _SCHEDULED_WAKE_REASONS:
        return _signal_task_link_is_terminal(agent, store, signal, reason)
    if reason not in SUBAGENT_LIFECYCLE_WAKE_REASONS:
        return False
    return _signal_task_link_is_terminal(agent, store, signal, reason)


def _signal_task_link_is_terminal(
    agent: object,
    store: ConversationStore,
    signal: WakeSignal,
    reason: str,
) -> bool:
    task_id = str(getattr(signal, "root_task_id", "") or "").strip()
    if not task_id:
        return False
    status = _background_task_link_status(
        agent,
        BackgroundRunRequest(
            thread_id=signal.thread_id,
            task_id=task_id,
            reason=reason,
        ),
        store=store,
    )
    return status.upper() in _TASK_LINK_TERMINAL_STATUSES


def _goal_wake_waits_for_child_event(
    agent: object,
    store: ConversationStore,
    signal: WakeSignal,
    lifecycle_reason: str,
) -> bool:
    """A plain goal continuation does not poll while exact related children are active."""
    if str(lifecycle_reason or "").strip().lower() != "thread_goal_continue":
        return False
    metadata = signal.metadata if isinstance(signal.metadata, dict) else {}
    if str(metadata.get("guidance_id") or "").strip():
        return False
    task_id = str(getattr(signal, "root_task_id", "") or "").strip()
    try:
        if store.pending_guidance("task", task_id, limit=1):
            return False
    except Exception:
        return False
    phase, state_error = _goal_subagent_phase(agent, task_id)
    return not state_error and phase == "subagents_active"


def _wake_signal_should_skip(
    agent: object,
    store: ConversationStore,
    signal: WakeSignal,
    lifecycle_reason: str,
) -> bool:
    return _wake_signal_is_stale(
        agent,
        store,
        signal,
        lifecycle_reason,
    ) or _goal_wake_waits_for_child_event(agent, store, signal, lifecycle_reason)


def _related_subagent_runs(agent: object, root_task_id: str) -> tuple[list[object], str]:
    if not root_task_id:
        return [], "subagent_root_unknown"
    manager = getattr(agent, "subagents", None)
    if manager is None:
        return [], "subagent_state_unavailable"
    try:
        if callable(getattr(manager, "list_runs_report", None)):
            report = manager.list_runs_report()
            if list(getattr(report, "load_errors", []) or []):
                return [], "subagent_state_load_error"
            tasks = list(getattr(report, "runs", []) or [])
        else:
            tasks = list(manager.list_runs())
    except Exception:
        return [], "subagent_state_load_error"
    related = [
        task
        for task in tasks
        if str(getattr(task, "id", "") or "") == root_task_id
        or str(getattr(task, "root_id", "") or "") == root_task_id
    ]
    return (related, "") if related else ([], "subagent_root_not_found")



# 后台主代理产出的投递路由。只有"内部/无真实外部路由"(子代理事件叫回、定时巡检默认走 internal)才
# 尝试升级成主动外呼:若该会话绑过可主动外呼的通道(飞书)就投到那个通道,这样"叫回来产出的汇总"才发
# 得到用户所在真渠道、不进内部黑洞。显式外部路由(feishu/wechat/qq/chat…进度策略或入站消息带来的)一律
# 原样尊重,不改既有语义;没有可外呼绑定则保持原路由(internal → 单机/CLI 行为不变)。
_INTERNAL_ROUTE_CHANNELS = frozenset({"internal", ""})


# LLM: internal 路由只能按 delivery registry 的 proactive capability 升级；显式外部路由保持原样。
# 函数用途: 为后台回复选择结构化通道和目标。
def _resolve_delivery_route(
    thread: object,
    request: BackgroundRunRequest,
    *,
    supports_proactive: Callable[[str], bool] | None = None,
) -> tuple[str, str]:
    channel = str(getattr(request, "route_channel", "") or "")
    route_target = str(getattr(request, "route_target", "") or "")
    if channel in _INTERNAL_ROUTE_CHANNELS:
        binding = _latest_proactive_binding(thread, supports_proactive=supports_proactive)
        if binding is not None:
            # 飞书 send_message 用 receive_id_type=open_id,需要用户 open_id(=binding.channel_user_id);
            # 缺失才回落 channel_conversation_id。
            return binding.channel, (binding.channel_user_id or binding.channel_conversation_id)
    return channel, route_target or default_route_target(thread, channel)


# LLM: 候选绑定按结构化 capability 和目标存在性过滤，再以 last_active_at 选最近通道。
# 函数用途: 返回会话中最近可主动外呼的通道绑定。
def _latest_proactive_binding(
    thread: object,
    *,
    supports_proactive: Callable[[str], bool] | None = None,
):
    capability_check = supports_proactive or _supports_builtin_proactive
    candidates = [
        binding
        for binding in getattr(thread, "channel_bindings", ()) or ()
        if capability_check(str(getattr(binding, "channel", "") or ""))
        and (getattr(binding, "channel_user_id", "") or getattr(binding, "channel_conversation_id", ""))
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda binding: float(getattr(binding, "last_active_at", 0.0) or 0.0))


# LLM: 只在没有注入真实 DeliveryService 时使用内置默认，生产运行优先读取 registry 能力。
# 函数用途: 判断内置默认通道是否支持主动外呼。
def _supports_builtin_proactive(channel: str) -> bool:
    return str(channel or "").strip().lower() in PROACTIVE_PUSH_CHANNELS


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


def _run_params(
    thread_id: str,
    request: BackgroundRunRequest,
    agent: object | None = None,
    *,
    goal_context: GoalRuntimeContext | None = None,
) -> RunParams:
    config = getattr(agent, "config", None)
    conversation_store = getattr(agent, "conversation_store", None)
    resolved_goal_context = goal_context or (
        _goal_runtime_context(agent, conversation_store, request)
        if agent is not None and conversation_store is not None
        else GoalRuntimeContext()
    )
    scheduler_run_id = _scheduler_run_id(request)
    return RunParams(
        save=False,
        source="background_main_agent",
        request_id=scheduler_run_id,
        run_id=scheduler_run_id or f"bg-main-{thread_id}",
        task_id=request.task_id or scheduler_run_id or thread_id,
        # /audit 保证档跨后台轮延续:后台唤醒轮的 prompt 是机器拼的、不带 /audit 词元,若主代理
        # 在后台轮里新派判读子代理,继承需从结构化标志读——从 owner 已有的保证档 watch(持久棘轮)
        # 反推本任务树在保证档,盖回 task_attributes,让新派子代理照样继承(治残留边界:委派发生在
        # 后台轮时词元/前台 task_attributes 都不在)。owner 无保证档 watch 则不动(默认档不误开)。
        # 后台轮与前台轮必须携带同一份结构化会话任务引用。否则 FinalizationService
        # 不知道该关闭哪条 active task link，完成
        # 的任务会被定时 policy 反复叫醒。只在 request 有明确 task_id 时绑定，
        # 普通无任务后台消息不会被误升格成任务。
        task_attributes=_background_task_attributes(thread_id, request, agent),
        allowed_tools=_background_run_allowed_tools(
            config,
            BackgroundToolPolicyRequest(
                reason=request.reason,
                wake_signal=request.wake_signal,
                config=config,
                owner_policy=getattr(agent, "owner_policy", None),
                policy_snapshot=_policy_snapshot_from_request(request),
                open_coverage_targets=ledger_open_coverage_target_count(agent, request.task_id or thread_id),
                active_goal=resolved_goal_context.goal is not None,
                goal_subagent_phase=resolved_goal_context.subagent_phase,
            ),
        ),
    )


def _scheduler_run_id(request: BackgroundRunRequest) -> str:
    if str(request.reason or "").strip().lower() != "scheduled_job_due":
        return ""
    wake = request.wake_signal if isinstance(request.wake_signal, dict) else {}
    metadata = wake.get("metadata") if isinstance(wake.get("metadata"), dict) else {}
    return str(metadata.get("scheduler_run_id") or "").strip()


def _background_run_allowed_tools(
    config: object | None,
    request: BackgroundToolPolicyRequest,
) -> list[str] | None:
    # A durable user schedule is a fresh turn of the same owner agent, not the
    # internal wait/progress lane.  通道运行时 follows the same full-agent-turn
    # model; registry owner policy still removes disabled tools fail-closed.
    if str(request.reason or "").strip().lower() == "scheduled_job_due":
        return None
    return background_allowed_tools(config, request=request)


def _background_task_attributes(
    thread_id: str,
    request: BackgroundRunRequest,
    agent: object | None,
) -> dict[str, object] | None:
    attributes: dict[str, object] = dict(_background_audit_attributes(agent) or {})
    wake = request.wake_signal if isinstance(request.wake_signal, dict) else {}
    metadata = wake.get("metadata") if isinstance(wake.get("metadata"), dict) else {}
    scheduler_run_id = str(metadata.get("scheduler_run_id") or "").strip()
    task_id = str(request.task_id or "").strip()
    if thread_id and (task_id or scheduler_run_id):
        attributes["conversation_thread_id"] = str(thread_id).strip()
    wake_signal_id = str(wake.get("wake_signal_id") or "").strip()
    if wake_signal_id:
        # The scheduler acknowledges the event that started this turn. The
        # active-turn inbox only consumes newer events arriving mid-turn.
        attributes["background_wake_signal_id"] = wake_signal_id
    if task_id:
        attributes.update(
            {
                "conversation_task_id": task_id,
            }
        )
        link = _background_conversation_task_link(agent, str(thread_id or "").strip(), task_id)
        if link is not None:
            goal = str(getattr(link, "goal", "") or "").strip()
            if goal:
                attributes["task_title"] = goal
            task_path = str(getattr(link, "task_path", "") or "").strip()
            if task_path:
                root = Path(task_path).expanduser().resolve(strict=False)
                if root.exists():
                    attributes["run_workspace"] = {
                        "task_root": str(root),
                        "output_dir": str(root / "output"),
                        "work_dir": str(root / "work"),
                    }
    if scheduler_run_id:
        attributes.update(
            {
                "scheduler_job_id": str(metadata.get("scheduler_job_id") or "").strip(),
                "scheduler_run_id": scheduler_run_id,
                "scheduler_trigger": str(metadata.get("scheduler_trigger") or "").strip(),
            }
        )
        skill_refs = metadata.get("scheduler_skill_refs")
        if isinstance(skill_refs, list) and skill_refs:
            attributes["skill_snapshot_refs"] = skill_refs
    return attributes or None


def _background_conversation_task_link(agent: object | None, thread_id: str, task_id: str):
    store = getattr(agent, "conversation_store", None) if agent is not None else None
    if store is None or not thread_id or not task_id:
        return None
    try:
        links, errors = store.task_links_report(thread_id)
    except Exception:
        return None
    if errors:
        return None
    return next((item for item in links if str(getattr(item, "task_id", "") or "") == task_id), None)


def _background_audit_attributes(agent: object | None) -> dict | None:
    """后台轮 task_attributes 里延续 /audit 保证档标志:owner 名下任一 watch 已在保证档(持久
    棘轮)→ 本任务树处于保证档,盖标志让后台轮新派的判读子代理结构化继承。纯盘上结构判据,
    失败保守不盖(默认档不误开)。"""
    home = getattr(getattr(agent, "home_paths", None), "owner_home_dir", "") if agent is not None else ""
    if not str(home or "").strip():
        return None
    try:
        from pathlib import Path

        from ..ingestion.watch_state import owner_home_has_audit_watch

        if owner_home_has_audit_watch(Path(str(home))):
            from ..common.audit_activation import AUDIT_ATTR

            return {AUDIT_ATTR: True}
    except Exception:
        return None
    return None


def ledger_open_coverage_target_count(agent: object | None, task_id: str) -> int:
    """任务主账本(task_id 键,与 task_progress_gate._run_id 的后台轮账本键同语义)里
    coverage 清单未闭环项计数——不足3续推开路的结构判据。失败保守 0(不开路,行为回落旧集合)。"""
    if agent is None or not str(task_id or "").strip():
        return 0
    try:
        from ..agent_core.runtime.owner_roots import runtime_owner_root
        from ..task_progress import read_task_progress

        progress = read_task_progress(runtime_owner_root(agent), str(task_id).strip())
        coverage = progress.get("coverage") if isinstance(progress, dict) else None
        counts = coverage.get("counts") if isinstance(coverage, dict) else None
        if not isinstance(counts, dict):
            return 0
        return max(0, int(counts.get("targets_incomplete") or 0))
    except Exception:
        return 0


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
    task_id: str
    config: object | None
    policy_request: BackgroundToolPolicyRequest
    load_errors: list[dict[str, Any]]


def context_markdown(*, agent: object, store: ConversationStore, thread: ConversationThread, request) -> str:
    policy_request = _tool_policy_request(agent, request)
    task_id = str(getattr(request, "task_id", "") or "").strip()
    bounded = _bounded_context(agent, store, thread, task_id, policy_request)
    policy_decision = background_tool_policy_decision(getattr(agent, "config", None), request=policy_request)
    sections = [
        ("Active Wake Signal", request.wake_signal or {}),
        ("Conversation Thread", bounded["thread"]),
        ("Runtime Load Errors", bounded.get("load_errors") or []),
        ("Recent Messages", bounded["messages"]),
        ("Bound Tasks", bounded["tasks"]),
        ("Channel Bindings", bounded["channel_bindings"]),
        ("Recent Observations", bounded["observations"]),
        ("Guidance", bounded["guidance"]),
        ("Pending Wake Signals", bounded["pending_wake_signals"]),
        ("Recovery Snapshot", bounded["recovery_snapshot"]),
        ("Task Runtime State", bounded["task_runtime_state"]),
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
    task_id: str,
    policy_request: BackgroundToolPolicyRequest,
) -> dict[str, Any]:
    config = getattr(agent, "config", None)
    load_errors: list[dict[str, Any]] = []
    state = _BackgroundContextLoad(agent, store, thread, task_id, config, policy_request, load_errors)
    visible_run_ids = _thread_active_task_ids(state)
    agent_tree = _agent_tree_payload(state, visible_run_ids)
    bundle = _context_bundle(state)
    pending_wake_signals = _pending_wake_signals(state)
    recovery_snapshot = _safe_recovery_snapshot(state, visible_run_ids)
    task_state = task_runtime_state(
        agent=agent,
        store=store,
        thread_id=thread.thread_id,
        task_id=task_id,
        load_errors=load_errors,
    )
    return bounded_background_context_payload(
        BackgroundContextPayloadRequest(
            bundle=bundle,
            pending_wake_signals=pending_wake_signals,
            task_runtime_state=task_state,
            agent_tree=agent_tree,
            recovery_snapshot=recovery_snapshot,
            load_errors=load_errors,
            budget=background_context_budget_from_config(config),
        )
    )


def _context_bundle(state: _BackgroundContextLoad) -> dict[str, Any]:
    """Load the one authoritative thread history for every continuation.

    Task ids restrict operational ledgers, child trees, and wake signals.  They
    never filter or replace the model-visible thread transcript.
    """
    try:
        if callable(getattr(state.store, "context_bundle_report", None)):
            bundle, load_errors = state.store.context_bundle_report(
                state.thread.thread_id,
                recent_limit=_config_int(state.config, "conversation_context_recent_limit"),
            )
            state.load_errors.extend(load_errors)
        else:
            bundle = state.store.context_bundle(
                state.thread.thread_id,
                recent_limit=_config_int(state.config, "conversation_context_recent_limit"),
            )
        return bundle
    except Exception as exc:
        state.load_errors.append(runtime_error_report(exc, context="background_context.context_bundle"))
        return _minimal_context_bundle(state.thread)


_TASK_CONTEXT_ID_KEYS = (
    "task_id",
    "root_task_id",
    "conversation_task_id",
    "gateway_request_id",
)


def _context_row_matches_task_ids(row: dict[str, Any], task_ids: set[str]) -> bool:
    if any(str(row.get(key) or "").strip() in task_ids for key in _TASK_CONTEXT_ID_KEYS):
        return True
    metadata = row.get("metadata")
    if not isinstance(metadata, dict):
        return False
    if any(str(metadata.get(key) or "").strip() in task_ids for key in _TASK_CONTEXT_ID_KEYS):
        return True
    attributes = metadata.get("task_attributes")
    return isinstance(attributes, dict) and any(
        str(attributes.get(key) or "").strip() in task_ids for key in _TASK_CONTEXT_ID_KEYS
    )


def _task_context_ids(state: _BackgroundContextLoad) -> set[str]:
    """Resolve one task's persisted subagent lineage without using prompt text."""
    task_id = str(state.task_id or "").strip()
    if not task_id:
        return set()
    task_ids = {task_id}
    manager = getattr(state.agent, "subagents", None)
    if manager is None:
        return task_ids
    try:
        current = manager.load(task_id)
    except Exception:
        current = None
    root_id = str(getattr(current, "root_id", "") or "").strip() or task_id
    task_ids.add(root_id)
    try:
        runs = manager.list_runs()
    except Exception:
        return task_ids
    for run in runs:
        run_id = str(getattr(run, "id", "") or "").strip()
        run_root_id = str(getattr(run, "root_id", "") or "").strip()
        if run_id and (run_id == root_id or run_root_id == root_id):
            task_ids.add(run_id)
    return task_ids


def _pending_wake_signals(state: _BackgroundContextLoad) -> list[dict[str, Any]]:
    try:
        if callable(getattr(state.store, "pending_wake_signals_report", None)):
            signals, load_errors = state.store.pending_wake_signals_report(
                limit=_config_int(state.config, "background_pending_wake_prompt_limit"),
            )
            state.load_errors.extend(load_errors)
            payload = [item.to_dict() for item in signals if item.thread_id == state.thread.thread_id]
        else:
            payload = pending_wake_payload(
                state.store,
                state.thread.thread_id,
                limit=_config_int(state.config, "background_pending_wake_prompt_limit"),
            )
        if not state.task_id:
            return payload
        task_ids = _task_context_ids(state)
        return [row for row in payload if _context_row_matches_task_ids(row, task_ids)]
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
    task_id = str(getattr(request, "task_id", "") or "") or str(getattr(request, "thread_id", "") or "")
    return BackgroundToolPolicyRequest(
        reason=str(getattr(request, "reason", "") or ""),
        wake_signal=getattr(request, "wake_signal", None),
        config=getattr(agent, "config", None),
        owner_policy=getattr(agent, "owner_policy", None),
        policy_snapshot=_policy_snapshot_from_request(request),
        open_coverage_targets=ledger_open_coverage_target_count(agent, task_id),
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
    if state.task_id:
        return sorted(_task_context_ids(state))
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
    "COMPLETED",
    "DONE",
    "FAILED",
    "INTERRUPTED",
    "SUPERSEDED",
    "TAKEN_OVER",
    "TIMEOUT",
})
_MIN_PROGRESS_POLICY_CATCHUP_SECONDS = 7200
_MAX_PROGRESS_POLICY_CATCHUP_INTERVALS = 4
from .store import ConversationStore

if TYPE_CHECKING:
    from ..collaboration import CollaborationStore


# 供应断供的 tick 层长退避(治真机"429 限流断供把后台消费永久冻死"):模型额度限流时,
# turn 内 auto_resume 短链(约 6 分钟)用尽会把 ProviderTransientError 抛回消费循环。旧行为
# 两宗罪:①异常中断整个 tick——一个撞限流的会话把同 tick 的其他唤醒/观察/判读全部队头阻塞;
# ②下一 poll(秒级)立刻重打已限流的模型,额度按分钟/小时刷新,秒级猛打只会加重限流。
# 这里按 thread 记内存态指数退避:第 n 次失败等 base*2^(n-1) 秒(封顶 max),到点自动重试;
# 成功即清零。信号/policy 不被标记消费,退避只是"本轮跳过",供应恢复后自动续跑,无需人肉。
# 进程重启态丢失=重启后立刻重试一次,无害。判据只认 typed ProviderTransientError,不做文本匹配。
class _ProviderSupplyBackoff:
    def __init__(self, *, base_seconds: float = 30.0, max_seconds: float = 900.0):
        self._base = max(1.0, float(base_seconds))
        self._cap = max(self._base, float(max_seconds))
        self._streaks: dict[str, int] = {}
        self._next_attempt_at: dict[str, float] = {}

    def should_attempt(self, thread_id: str, now: float) -> bool:
        return now >= self._next_attempt_at.get(str(thread_id), 0.0)

    def record_failure(self, thread_id: str, now: float) -> dict[str, object]:
        key = str(thread_id)
        streak = self._streaks.get(key, 0) + 1
        self._streaks[key] = streak
        delay = min(self._base * (2 ** (streak - 1)), self._cap)
        self._next_attempt_at[key] = now + delay
        return {
            "thread_id": key,
            "consecutive_failures": streak,
            "retry_delay_seconds": delay,
            "next_attempt_at": now + delay,
        }

    def record_success(self, thread_id: str) -> int:
        key = str(thread_id)
        self._next_attempt_at.pop(key, None)
        return self._streaks.pop(key, 0)


def _consume_with_supply_guard(backoff: _ProviderSupplyBackoff, thread_id: str, now: float, run):
    """带供应退避护栏跑一个后台消费 turn(唤醒/观察/盯守判读共用)。
    冷却中 → 不消费返回 None(信号/policy 留 pending,到点自动重试);
    供应错 → 吸收进退避返回 None,放行其余会话;非供应异常原样上抛;
    成功拿到 report → 清退避计数(供应恢复)。"""
    if not backoff.should_attempt(thread_id, now):
        return None
    started = time.monotonic()
    try:
        report = run()
    except Exception as exc:
        # 退避锚点=失败真实时刻(tick 逻辑时刻 + turn 实际耗时)。turn 内 auto_resume 短链
        # 本身要跑几分钟,若锚在 tick 起点,next_attempt_at 在 turn 结束时早已过期 → 退避
        # 形同虚设、下一 poll 立刻猛打(隔离演练请求账实锤)。monotonic 差不受注入时钟影响。
        failed_at = now + (time.monotonic() - started)
        if not _absorb_provider_supply_failure(backoff, thread_id, failed_at, exc):
            raise
        return None
    if report is not None:
        _note_supply_recovery(backoff, thread_id)
    return report


def _absorb_provider_supply_failure(
    backoff: _ProviderSupplyBackoff, thread_id: str, now: float, exc: BaseException
) -> bool:
    """临时供应错(429/限流/断供)专属吸收:记长退避+打点,放行本 tick 其余会话的消费
    (治队头阻塞);其他异常一律不吸、照旧上抛走 [gateway-loop-error] 兜底(真 bug 不掩盖)。
    判据只认 typed ProviderTransientError——auto_resume 短链用尽后上抛的就是它。"""
    if not is_provider_transient_error(exc):
        return False
    payload = backoff.record_failure(thread_id, now)
    payload["error_type"] = exc.__class__.__name__
    payload["error"] = compact_error_message(exc)
    _print_supply_event("provider_supply_backoff", payload)
    return True


def _note_supply_recovery(backoff: _ProviderSupplyBackoff, thread_id: str) -> None:
    failed_attempts = backoff.record_success(thread_id)
    if failed_attempts:
        _print_supply_event(
            "provider_supply_resumed",
            {"thread_id": thread_id, "failed_attempts": failed_attempts},
        )


def _print_supply_event(event: str, payload: dict[str, object]) -> None:
    body = json.dumps({"event": event, **payload}, ensure_ascii=False, sort_keys=True)
    print(f"[gateway-supply-backoff] {body}", flush=True)


def _supply_backoff_from_agent(agent: object) -> _ProviderSupplyBackoff:
    guard_policy = getattr(agent, "runtime_guard_policy", None)
    return _ProviderSupplyBackoff(
        base_seconds=runtime_guard_int("provider_supply_backoff_base_seconds", 30, policy=guard_policy),
        max_seconds=runtime_guard_int("provider_supply_backoff_max_seconds", 900, policy=guard_policy),
    )


# 三条后台消费车道(唤醒信号/观察批/到点 policy)。都过 _consume_with_supply_guard:
# 供应断供时按会话退避而不是中断整个 tick,恢复后自动续跑。
def _is_scheduler_wake_signal(signal: WakeSignal) -> bool:
    return str(getattr(signal, "reason", "") or "").strip().lower() == "scheduled_job_due"


def _consume_pending_wake_signals(
    scheduler: BackgroundMainAgentScheduler, reports: list[BackgroundMainAgentReport], current: float
) -> set[str]:
    reported: set[str] = set()
    handled: set[str] = set()
    wake_signals = scheduler.store.pending_wake_signals(limit=scheduler._config_limit("conversation_pending_wake_limit"))
    for signal in wake_signals:
        scheduler_signal = _is_scheduler_wake_signal(signal)
        if signal.wake_signal_id in handled:
            continue
        if _successful_completion_waiting_for_batch(scheduler, signal, current):
            continue
        # 子任务可能在父任务被 supersede/complete 后才迟到结束。此时信号仍是合法持久记录，
        # 但不得再唤醒旧父任务并向普通会话写回过期工作；确认根链接已非 active 后直接归档。
        if _wake_signal_root_is_inactive(scheduler.store, signal):
            scheduler._mark_signal(signal, current, handled)
            continue
        if signal.thread_id in reported and not scheduler_signal:
            scheduler._mark_signal(signal, current, handled)
            continue
        report = _consume_with_supply_guard(
            scheduler._supply_backoff, signal.thread_id, current,
            partial(scheduler._run_wake_signal, signal, now=current),
        )
        if report is not None:
            reports.append(report)
            if not scheduler_signal:
                reported.add(report.thread_id)
                scheduler._mark_sibling_signals(wake_signals, signal.thread_id, current, handled)
    return reported


def _successful_completion_waiting_for_batch(
    scheduler: BackgroundMainAgentScheduler,
    signal: WakeSignal,
    current: float,
) -> bool:
    """Briefly debounce successful sibling completions; failures and blockers stay immediate."""
    if str(signal.reason or "").strip().lower() != "subagent_runner_finished":
        return False
    metadata = signal.metadata if isinstance(signal.metadata, dict) else {}
    if str(metadata.get("status") or "").strip().upper() != "DONE":
        return False
    delay = scheduler._config_limit("background_completion_coalesce_seconds")
    created_at = float(signal.created_at or 0.0)
    return delay > 0 and created_at > 0 and current < created_at + delay


def _observation_batch_semantics(
    observations: list[ObservationEvent],
) -> tuple[str, dict[str, object] | None]:
    event_types = {str(item.event_type or "").strip() for item in observations}
    if len(event_types) != 1:
        return "observation_requires_main_agent", None
    reason = next(iter(event_types))
    if reason not in SUBAGENT_LIFECYCLE_WAKE_REASONS:
        return "observation_requires_main_agent", None
    statuses = [
        str((item.metadata or {}).get("status") or "").strip().upper()
        for item in observations
    ]
    status = "DONE" if statuses and all(item == "DONE" for item in statuses) else next(
        (item for item in statuses if item and item != "DONE"),
        "",
    )
    return reason, {
        "kind": "observation_fallback",
        "reason": reason,
        "root_task_id": first_root_task_id(observations),
        "source_agent_ids": [item.source_agent_id for item in observations if item.source_agent_id],
        "observation_ids": [item.observation_id for item in observations],
        "metadata": {
            "status": status,
            "task_ids": [
                str((item.metadata or {}).get("task_id") or "")
                for item in observations
                if str((item.metadata or {}).get("task_id") or "")
            ],
        },
    }


def _wake_signal_root_is_inactive(store: object, signal: WakeSignal) -> bool:
    root_task_id = str(getattr(signal, "root_task_id", "") or "").strip()
    if not root_task_id:
        return False
    try:
        links, _load_errors = store.task_links_report(signal.thread_id)
    except Exception:
        return False
    return any(
        str(getattr(link, "task_id", "") or "") == root_task_id
        and str(getattr(link, "status", "") or "").strip().lower() != "active"
        for link in links
    )


def _consume_observation_batches(
    scheduler: BackgroundMainAgentScheduler,
    reports: list[BackgroundMainAgentReport],
    reported: set[str],
    current: float,
) -> None:
    pending_observations = scheduler.store.unhandled_observations_requiring_main(
        limit=scheduler._config_limit("conversation_unhandled_observation_limit")
    )
    for thread_id, thread_observations in observations_by_thread(pending_observations).items():
        if thread_id in reported:
            continue
        report = _consume_with_supply_guard(
            scheduler._supply_backoff, thread_id, current,
            partial(scheduler._run_observation_batch, thread_id, thread_observations, now=current),
        )
        if report is not None:
            reports.append(report)
            reported.add(report.thread_id)


def _consume_due_policies(
    scheduler: BackgroundMainAgentScheduler,
    reports: list[BackgroundMainAgentReport],
    reported: set[str],
    current: float,
) -> None:
    enabled, load_errors = scheduler.store.list_progress_policies_report(enabled_only=True)
    scheduler.last_progress_policy_load_errors = load_errors
    scheduler.last_progress_policy_suppressed = []
    # §8.3 盯守自唤醒兜底:owner 有未判读 backlog 时,把"睡过头"的盯守 policy 排期钳到
    # 响应上限(纯结构信号;常态零盘 IO)。钳完 next_due_at 仍在未来,本轮 due 口径不变。
    _expedite_watch_backlog_quietly(scheduler.runtime.agent, scheduler.store, enabled, now=current)
    policies = [policy for policy in enabled if policy.next_due_at <= current]
    agent = getattr(scheduler.runtime, "agent", None)
    runnable, suppressed = _runnable_due_policies(scheduler.store, policies, now=current, agent=agent)
    scheduler.last_progress_policy_suppressed = _snooze_suppressed_policies(
        scheduler.store, suppressed, now=current, agent=agent
    )
    for policy in runnable:
        if policy.thread_id in reported:
            continue
        report = _consume_with_supply_guard(
            scheduler._supply_backoff, policy.thread_id, current,
            partial(scheduler._run_due_policy, policy, now=current),
        )
        if report:
            reports.append(report)


class _BackgroundSchedulerTickMixin:
    """Tick orchestration and one durable wake-signal execution."""

    def tick(self, *, now: float | None = None) -> list[BackgroundMainAgentReport]:
        current = now if now is not None else __import__("time").time()
        self._process_collaboration_cases(now=current)
        _maybe_supervise_orphans(self, current)
        self._enqueue_scheduler_runs(now=current)
        reports: list[BackgroundMainAgentReport] = []
        reported = _consume_pending_wake_signals(self, reports, current)
        _consume_observation_batches(self, reports, reported, current)
        _consume_due_policies(self, reports, reported, current)
        return reports

    def _process_collaboration_cases(self, *, now: float) -> None:
        if self.collaboration_store is None:
            return
        from ..collaboration import CollaborationCoordinator
        CollaborationCoordinator(store=self.collaboration_store, conversation_store=self.store).tick(now=now)

    def _enqueue_scheduler_runs(self, *, now: float) -> None:
        if self.scheduler_service is None:
            return
        try:
            self.scheduler_service.enqueue_ready_runs(
                now=now,
                limit=self._config_limit("conversation_pending_wake_limit"),
            )
        except Exception:
            _HEARTBEAT_LOGGER.warning("owner scheduler enqueue failed", exc_info=True)

    def _run_wake_signal(self, signal: WakeSignal, *, now: float) -> BackgroundMainAgentReport | None:
        # 关键:透传 signal 的【真实 reason】(subagent_runner_finished / capability_request_open 等),
        #   不要用泛泛的 "wake_signal" 盖掉它——否则 background_prompt 掉进泛泛提示词(拿不到整合收敛引导)、
        #   且 _is_subagent_lifecycle_wake 判 False → 拿到含 create_subagents 的默认工具集(主代理能派
        #   verifier/recovery 子代理空转)。透传后:子代理生命周期唤醒 → 整合工具集(无 create_subagents,
        #   结构级逼主代理自己整合)+ 整合收敛提示词。非生命周期唤醒(无 reason)回落原 urgent/wake_signal。
        lifecycle_reason = str(getattr(signal, "reason", "") or "").strip()
        reason = lifecycle_reason or ("urgent_wake_signal" if signal.urgency == "urgent" else "wake_signal")
        scheduler_claim = None
        scheduler_heartbeat = None
        if _is_scheduler_wake_signal(signal):
            if self.scheduler_service is None:
                return None
            claim_result = self.scheduler_service.claim_wake(
                signal,
                lease_seconds=self.claim_ttl_seconds,
                now=now,
            )
            if claim_result.status == "stale":
                self.store.mark_wake_signal_handled(signal.wake_signal_id, now=now)
                return None
            if claim_result.status != "claimed" or claim_result.claim is None:
                return None
            scheduler_claim = claim_result.claim
            scheduler_heartbeat = self.scheduler_service.heartbeat(
                scheduler_claim,
                lease_seconds=self.claim_ttl_seconds,
            )
            scheduler_heartbeat.start()
        try:
            if _wake_signal_should_skip(self.runtime.agent, self.store, signal, lifecycle_reason):
                if scheduler_claim is not None:
                    self.scheduler_service.release(scheduler_claim, now=now)
                self.store.mark_wake_signal_handled(signal.wake_signal_id, now=now)
                return None
            self._pre_wake_capability_sweep(lifecycle_reason, signal)
            if lifecycle_reason == "subagent_runner_finished":
                # A4:盯守子代理终态的第一时间就机制层补岗(原先只挂定时 policy 轮:若该轮
                # 不再触发,窗口未走完的岗位会一直空着,整合轮只能靠模型自救)。幂等,静默失败。
                self._watch_lane_sweep_quietly()
            # 真机第5层根 bug(确诊):wake signal 路径**先于**观察批消费(tick 里 _consume_pending_wake_signals
            # 在 _consume_observation_batches 之前),且 mark_wake_signal_handled 会连带把关联 observation 标
            # handled → 带路由的观察批(_run_observation_batch)对同一真事件**永不运行**。但此处调 _run_claimed
            # 从不传 route_channel/route_target → 回落 route_channel="internal" → 主代理被真事件叫回后即便上报,
            # 也只发 internal、到不了用户通道。修:与观察批同源,按 owner 身份取真实投递
            # 路由(飞书/open_id),让原生"叫回→主代理自然汇报"直达 owner 通道。取不到 owner 身份回落
            # internal(单机不变)。内部 findings 账仍只作为主代理整合输入,不直接拼入用户正文。
            route_channel, route_target = self._observation_route(signal.thread_id)
            _HEARTBEAT_LOGGER.info(
                "WAKE_SIGNAL_RUN thread=%s reason=%s route_channel=%s route_target=%s",
                signal.thread_id, reason, route_channel, route_target,
            )
            report = self._run_claimed({"thread_id": signal.thread_id, "task_id": signal.root_task_id, "reason": reason, "route_channel": route_channel, "route_target": route_target, "now": now, "wake_signal": signal})
        except Exception as exc:
            if scheduler_claim is not None:
                if is_provider_transient_error(exc):
                    self.scheduler_service.release(scheduler_claim, now=time.time())
                else:
                    terminal = self.scheduler_service.finish(
                        scheduler_claim,
                        status="failed",
                        error_code=type(exc).__name__.upper(),
                        error_message=compact_error_message(exc),
                        now=time.time(),
                    )
                    if terminal is not None:
                        self.store.mark_wake_signal_handled(
                            signal.wake_signal_id,
                            now=time.time(),
                        )
            if lifecycle_reason == "thread_goal_continue":
                status = "usage_limited" if is_provider_usage_limit_error(exc) else "blocked"
                self._stop_thread_goal_after_error(signal, status=status)
                self.store.mark_wake_signal_handled(signal.wake_signal_id, now=time.time())
            raise
        finally:
            if scheduler_heartbeat is not None:
                scheduler_heartbeat.stop()
        if scheduler_claim is not None:
            if report is None:
                self.scheduler_service.release(scheduler_claim, now=time.time())
                return None
            terminal = self.scheduler_service.finish(
                scheduler_claim,
                status="done",
                response=report.response,
                delivery_status=report.delivery_status,
                delivery_reason=report.delivery_reason,
                now=time.time(),
            )
            if terminal is None:
                return None
        if report is not None:
            self.store.mark_wake_signal_handled(signal.wake_signal_id, now=now)
            if lifecycle_reason == "thread_goal_continue":
                self._continue_thread_goal(signal, report=report, now=now)
            if lifecycle_reason == "subagent_runner_finished":
                _ensure_open_coverage_wake_chain(self, signal, now=now)
        return report

class _BackgroundSchedulerGoalMixin:
    """Goal continuation, lifecycle preprocessing, and owner delivery routing."""

    # LLM: Turn errors and typed provider usage limits are system-owned goal stops, matching 会话运行时.
    # 函数用途: 目标后台轮异常时原子停住同一目标与任务，等待用户恢复。
    def _stop_thread_goal_after_error(self, signal: WakeSignal, *, status: str) -> None:
        metadata = signal.metadata if isinstance(signal.metadata, dict) else {}
        with self.store.goal_transition_guard(signal.thread_id):
            goal = self.store.load_goal(signal.thread_id)
            if (
                goal is None
                or goal.status != "active"
                or goal.goal_id != str(metadata.get("goal_id") or "")
                or goal.task_id != str(signal.root_task_id or "")
            ):
                return
            elapsed = self.store.take_goal_elapsed_seconds(goal)
            if elapsed:
                goal = self.store.account_goal_usage(
                    {
                        "thread_id": goal.thread_id,
                        "goal_id": goal.goal_id,
                        "time_delta_seconds": elapsed,
                        "mode": "active_only",
                    }
                ) or goal
            updated = self.store.update_goal(
                {
                    "thread_id": goal.thread_id,
                    "goal_id": goal.goal_id,
                    "status": status,
                    "expected_status": "active",
                }
            )
            if updated is None:
                return
            self.store.update_task_status({"task_id": goal.task_id, "status": "interrupted"})
            registry = getattr(
                getattr(getattr(self.runtime, "agent", None), "local_store", None),
                "task_registry",
                None,
            )
            if registry is not None:
                registry.register_task(goal.task_id, status="blocked", goal=updated.objective)

    # LLM: Reconcile exact goal/task identity after a turn, then publish at most one deduplicated next wake.
    # 函数用途: 持续目标一轮结束后同步终态，仍 active 则继续推进同一目标。
    def _continue_thread_goal(
        self,
        signal: WakeSignal,
        *,
        report: BackgroundMainAgentReport,
        now: float,
    ) -> None:
        """Reconcile one goal turn and enqueue exactly one next turn while active."""
        try:
            goal = self.store.load_goal(signal.thread_id)
            metadata = signal.metadata if isinstance(signal.metadata, dict) else {}
            if (
                goal is None
                or goal.goal_id != str(metadata.get("goal_id") or "")
                or goal.task_id != str(signal.root_task_id or "")
            ):
                return
            task_status = _background_task_link_status(
                self.runtime.agent,
                BackgroundRunRequest(
                    thread_id=signal.thread_id,
                    task_id=goal.task_id,
                    reason="thread_goal_continue",
                ),
                store=self.store,
            )
            if goal.status != "active":
                if goal.status in {"blocked", "usage_limited", "budget_limited"}:
                    self.store.update_task_status({"task_id": goal.task_id, "status": "interrupted"})
                return
            if task_status != "active" or report.tool_call_count == 0:
                return
            subagent_phase, state_error = _goal_subagent_phase(
                self.runtime.agent,
                goal.task_id,
            )
            if subagent_phase == "subagents_active" or state_error:
                # Child lifecycle events are the continuation authority while
                # related work is active. Do not create a polling wake loop.
                return
            from .goal_runtime import raise_goal_continuation_wake

            raise_goal_continuation_wake(
                self.store,
                goal,
                channel=str(metadata.get("channel") or ""),
                conversation_id=str(metadata.get("conversation_id") or ""),
                now=now,
            )
        except Exception:
            _HEARTBEAT_LOGGER.warning("thread goal continuation failed", exc_info=True)

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
        # 真机根 bug:此路原来不传 route_channel/route_target → BackgroundRunRequest 默认
        # route_channel="internal" → 主代理被真事件叫回后即便自然汇报,也只发到 internal、到不了用户
        # 通道。对比 _run_due_policy 是带 route 的。修:从线程 channel binding 取真实投递路由传进去,
        # 让原生"叫回→主代理自然汇报"直达 owner 通道；内部 findings 账不直接出站。
        route_channel, route_target = self._observation_route(thread_id)
        # 结构化探针:真机确认原生"叫回→上报"是否触发 + 路由落在哪个通道(只读日志即可核实,
        # 不必反复重启验证)。route_channel!=internal 即证明第3/5层路由修复生效、报告直达 owner。
        _HEARTBEAT_LOGGER.info(
            "OBSERVATION_BATCH_RUN thread=%s obs=%d route_channel=%s route_target=%s",
            thread_id, len(observations), route_channel, route_target,
        )
        reason, wake_signal = _observation_batch_semantics(observations)
        report = self._run_claimed({
            "thread_id": thread_id,
            "task_id": first_root_task_id(observations),
            "reason": reason,
            "route_channel": route_channel,
            "route_target": route_target,
            "now": now,
            "wake_signal": wake_signal,
        })
        if report is not None:
            self.store.mark_observations_handled([item.observation_id for item in observations], now=now)
        return report

    def _observation_route(self, thread_id: str) -> tuple[str, str]:
        """真事件上报要直达 owner 通道。取路由三档优先级(由最具体到最兜底):
        ① 观察所在【线程自带的真实外呼 binding】(PROACTIVE_PUSH_CHANNELS,如 feishu)——最具体,
           直取其 channel_user_id(飞书=open_id,适配器 receive_id_type=open_id)。
        ② 无外呼 binding 时(真机第3层实锤:urgent 观察常挂【子代理线程】bg-main-thread,无
           channel binding,按线程取会回落 internal 发不出去)→ 按 owner-scoped agent 的 owner 身份取
           (飞书/open_id):owner 身份就是那个飞书用户。open_id 优先从 owner home 路径解析(不依赖属性
           是否设置),再退 home_paths 属性。**owner 身份必须是真外呼通道(PROACTIVE_PUSH_CHANNELS)才用**
           ——单租户 owner_provider="local" 不是外呼通道,不能拿它当路由(否则绕过 internal 投递、发不出)。
        ③ 都取不到 → 回落线程 binding(单机 internal 绑定)或 (internal, "")。
        ⚠️ ①在②之前:owner_id 是 provider 的 user_id,生产环境恰等于 open_id,但概念上不等于线程
           binding 的 channel_user_id;②排前面会让已绑定线程错发到 owner_id 而非 open_id(实锤)。"""
        try:
            thread = self.store.load_thread(thread_id)
        except Exception:
            thread = None
        bindings = list(getattr(thread, "channel_bindings", ()) or ())
        # ① 线程自带真实外呼 binding(最具体)→ 直取 channel_user_id(飞书 open_id)
        for binding in reversed(bindings):
            channel = str(getattr(binding, "channel", "") or "")
            target = str(getattr(binding, "channel_user_id", "") or getattr(binding, "channel_conversation_id", "") or "")
            if channel in PROACTIVE_PUSH_CHANNELS and target:
                return channel, target
        # ② 无外呼 binding(子代理线程)→ owner 身份(仅真外呼通道;"local"/"internal" 不算)
        provider, open_id = self._owner_from_home_path()
        if provider in PROACTIVE_PUSH_CHANNELS and open_id:
            return provider, open_id
        home = getattr(getattr(getattr(self, "runtime", None), "agent", None), "home_paths", None)
        owner_channel = str(getattr(home, "owner_provider", "") or "").strip()
        owner_id = str(getattr(home, "owner_id", "") or "").strip()
        if owner_channel in PROACTIVE_PUSH_CHANNELS and owner_id:
            return owner_channel, owner_id
        # ③ 回落线程 binding(单机 internal 绑定)或 internal
        if bindings:
            binding = bindings[-1]
            channel = str(getattr(binding, "channel", "") or "internal")
            target = str(getattr(binding, "channel_user_id", "") or getattr(binding, "channel_conversation_id", "") or "")
            return channel, target or default_route_target(thread, channel)
        return "internal", ""

    def _owner_from_home_path(self) -> tuple[str, str]:
        """从 owner home 路径解析 (provider, open_id):.my-agent/owners/providers/<provider>/users/<id>。
        scoped scheduler 的 store/agent 根落在 owner home 子树,据此取投递路由最稳(不依赖属性是否设置)。"""
        import re

        sources = [
            getattr(getattr(getattr(self, "runtime", None), "agent", None), "home_paths", None),
            getattr(self, "store", None),
        ]
        for src in sources:
            for attr in ("owner_home", "owner_home_dir", "root"):
                text = str(getattr(src, attr, "") or "")
                match = re.search(r"owners/providers/([^/]+)/(?:users|groups)/([^/]+)", text)
                if match:
                    return match.group(1), match.group(2)
        return "", ""

class _BackgroundSchedulerExecutionMixin:
    """Claimed progress-policy execution, heartbeats, and runtime facts."""

    def _run_due_policy(self, policy: ProgressPolicy, *, now: float) -> BackgroundMainAgentReport | None:
        self._watch_lane_sweep_quietly()
        signature = _automatic_supervision_signature(self.runtime.agent, policy)
        previous_signature = str((policy.metadata or {}).get("material_signature") or "")
        if signature and previous_signature and signature == previous_signature:
            self.store.mark_progress_checked(
                policy.policy_id,
                now=now,
                metadata_updates={"material_signature": signature},
            )
            return None
        report = self._run_claimed(
            {
                "thread_id": policy.thread_id,
                "task_id": policy.task_id,
                "reason": _progress_policy_run_reason(policy),
                "route_channel": policy.route_channel,
                "route_target": policy.route_target,
                "now": now,
                "wake_signal": _progress_policy_wake_payload(policy),
            }
        )
        if report is not None:
            latest_signature = _automatic_supervision_signature(self.runtime.agent, policy) or signature
            self.store.mark_progress_reported(
                policy.policy_id,
                now=now,
                no_progress_streak=_next_no_progress_streak(policy, report),
                metadata_updates=(
                    {"material_signature": latest_signature}
                    if latest_signature
                    else None
                ),
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
            task_id = str(kwargs.get("task_id") or "").strip()
            if task_id:
                with register_interruptible(conversation_request_interrupt_name(task_id)):
                    return self.runtime.run_once(kwargs)
            return self.runtime.run_once(kwargs)
        except InterruptedError:
            # `/stop` is an expected user control transition.  The durable task
            # link is already marked interrupted by the control service, so the
            # background lease must close quietly instead of becoming a failed
            # run that recovery code may try to take over.
            status = "cancelled"
            return None
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
            if (
                signal.thread_id == thread_id
                and signal.wake_signal_id not in handled
                and not _is_scheduler_wake_signal(signal)
            ):
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


class BackgroundMainAgentScheduler(
    _BackgroundSchedulerTickMixin,
    _BackgroundSchedulerGoalMixin,
    _BackgroundSchedulerExecutionMixin,
):
    """Single facade over background tick, goal routing, and claimed execution."""

    def __init__(self, config: dict):
        self.runtime = config["runtime"]
        self.store = config["store"]
        self.collaboration_store = config.get("collaboration_store") or getattr(
            self.runtime.agent,
            "collaboration_store",
            None,
        )
        self.scheduler_service = config.get("scheduler_service") or getattr(
            self.runtime.agent,
            "scheduler_service",
            None,
        )
        agent_config = getattr(getattr(self.runtime, "agent", None), "config", None)
        claim_ttl = config.get(
            "claim_ttl_seconds",
            _agent_config_int(agent_config, "background_claim_ttl_seconds"),
        )
        heartbeat_interval = config.get(
            "claim_heartbeat_interval_seconds",
            _agent_config_int(agent_config, "background_claim_heartbeat_interval_seconds"),
        )
        self.claim_ttl_seconds = max(1, int(claim_ttl or 1))
        self.claim_heartbeat_interval_seconds = claim_heartbeat_interval_seconds(
            ttl_seconds=self.claim_ttl_seconds,
            configured_interval_seconds=heartbeat_interval,
        )
        self.last_progress_policy_load_errors: list[dict[str, object]] = []
        self.last_progress_policy_suppressed: list[dict[str, object]] = []
        self._last_supervision_at = 0.0
        self._supply_backoff = _supply_backoff_from_agent(self.runtime.agent)


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


def _automatic_supervision_signature(agent: object, policy: ProgressPolicy) -> str:
    """Only automatic subagent supervision may skip an unchanged LLM turn."""
    metadata = policy.metadata if isinstance(policy.metadata, dict) else {}
    if str(metadata.get("tool") or "") != "dispatch_supervision_auto":
        return ""
    from .progress_fingerprint import subagent_material_signature

    return subagent_material_signature(
        agent,
        task_id=policy.task_id,
        watched_run_ids=metadata.get("watched_run_ids"),
    )


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


def _progress_policy_run_reason(policy: ProgressPolicy) -> str:
    return "scheduled_progress_report"


# 函数用途: 唤醒链保底(不足3·派工叫回后不续):唤醒轮消费完 subagent-finished 并收口后,
#   任务清单还有未闭环项、该任务名下却没有任何 enabled 循环提醒(收口把监督提醒退休了/一直
#   没登记过)时,机制层补登一个——账没对完,唤醒链不许走空,否则任务如实停在半截再没有
#   未来轮次推它(真机 u-fixtest2 停 8/24)。判据全结构化(清单计数/policy 存在性);登记后的
#   生命周期完全复用既有机制:无进展退避(2^streak 封顶)、清单全闭后收口自动退休。
#   best-effort:任何失败只记日志,绝不影响唤醒轮本身。
def _ensure_open_coverage_wake_chain(scheduler: BackgroundMainAgentScheduler, signal: WakeSignal, *, now: float) -> None:
    try:
        task_id = str(getattr(signal, "root_task_id", "") or "").strip()
        thread_id = str(getattr(signal, "thread_id", "") or "").strip()
        if not task_id or not thread_id:
            return
        agent = getattr(scheduler.runtime, "agent", None)
        if ledger_open_coverage_target_count(agent, task_id) <= 0:
            return
        if any(policy.task_id == task_id for policy in scheduler.store.list_progress_policies(enabled_only=True)):
            return
        interval = int(getattr(getattr(agent, "config", None), "dispatch_supervision_reminder_seconds", 0) or 0)
        scheduler.store.set_progress_policy(
            {
                "thread_id": thread_id,
                "task_id": task_id,
                "interval_seconds": max(60, interval) if interval > 0 else 180,
                "route_channel": "internal",
                "route_target": "",
                "metadata": {
                    "kind": "subagent_progress_watch",
                    "tool": "coverage_open_continuation",
                    "scope": "own_task_tree",
                    "reason": "机制层续推保底:任务清单还有未闭环项,到点继续推进剩余项(续派或自己做),全部闭环并收口后自动停止",
                    "watch_run_id": task_id,
                },
            }
        )
    except Exception:
        _HEARTBEAT_LOGGER.warning("open-coverage wake chain ensure failed", exc_info=True)


# LLM: 周期性孤儿 supervision(worker-pool self-healing 的 reconcile 环,零 LLM 成本):
#   事件唤醒只覆盖"有人发信号"的死亡;宿主进程被 SIGKILL/断电类静默死亡不发任何 wake,
#   而定时提醒策略只有模型调过 wait 才存在——这里按 orphan_supervision_interval_seconds
#   (默认 60s,0=关)在调度器 tick 里兜底巡查:盯守死岗补建接管 + durable 复活可派孤儿。
#   无候选即 no-op(list_runs 有 mtime 缓存,近零开销);绝不外抛。
def _maybe_supervise_orphans(scheduler: BackgroundMainAgentScheduler, now: float) -> None:
    interval = scheduler._config_limit("orphan_supervision_interval_seconds")
    if interval <= 0 or (now - scheduler._last_supervision_at) < interval:
        return
    scheduler._last_supervision_at = now
    try:
        from ..agent_core.orchestration.dispatch.capability_auto_sweep import (
            supervise_stalled_orphans,
        )

        supervise_stalled_orphans(scheduler.runtime.agent)
    except Exception:
        _HEARTBEAT_LOGGER.debug("orphan supervision sweep failed", exc_info=True)


# 函数用途: §8.3 盯守自唤醒兜底①的调度器挂点——把"睡过头"(next_due_at 距今超过响应
#   上限)的盯守 policy 按 owner 的 spool backlog 结构信号钳到上限;判断与写回全在
#   ingestion.wake_backstop,这里只保证唤醒轮绝不被它拖垮(任何异常静默记日志)。
def _expedite_watch_backlog_quietly(agent: object, store, policies: list[ProgressPolicy], *, now: float) -> None:
    try:
        from ..ingestion.wake_backstop import expedite_watch_policies_for_backlog

        expedite_watch_policies_for_backlog(agent, store, policies, now=now)
    except Exception:
        _HEARTBEAT_LOGGER.debug("watch backlog expedite hook failed", exc_info=True)


# 函数用途: 定时提醒唤醒路上的盯守补岗兜底——被 cancel/没触发 wake 信号的死岗、以及
#   PENDING/PLANNING 停滞孤儿,都在到点提醒时被机制层补上(supervision 同款:补建接管
#   + durable 复活;原实现只在"新建了接管"时才续派,PENDING 孤儿岗恒漏)。近零开销。
def _scheduled_watch_lane_sweep(agent: object) -> None:
    try:
        from ..agent_core.orchestration.dispatch.capability_auto_sweep import (
            supervise_stalled_orphans,
        )

        supervise_stalled_orphans(agent)
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
    agent: object | None = None,
) -> tuple[list[ProgressPolicy], list[tuple[ProgressPolicy, str]]]:
    selected_by_key: dict[tuple[str, str, str, str], ProgressPolicy] = {}
    suppressed: list[tuple[ProgressPolicy, str]] = []
    for policy in policies:
        reason = _progress_policy_suppression_reason(store, policy, now=now, agent=agent)
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


def _progress_policy_suppression_reason(store, policy: ProgressPolicy, *, now: float, agent: object | None = None) -> str:
    if _policy_task_link_is_terminal(store, policy):
        # P1 消费吞吐豁免:盯守子代理的常态形态就是「turn 结束进 DONE、唤醒续驱下一轮
        # 判读」,而 DONE 会把它自己的 task link 置终态——若按终态一刀切抑制,盯守 policy
        # 在下一次 due 时就被退休、永不 fire,唤醒链每次 DONE 都自埋(真机实锤:窗口末尾
        # 岗停后 spool 积压无人消费)。owner 还有未清账的盯守路(窗口未满或积压>0)时,
        # 盯守类 policy 照常 runnable:fire 路径自带补岗扫描+唤醒轮消费。终点:积压清零
        # 或 close 后豁免消失,下一拍照常按终态退休。
        if _watch_policy_exempt_from_terminal(agent, policy, now=now):
            return ""
        return "terminal_task_link"
    if _progress_policy_is_stale(policy, now=now):
        return "stale_missed_interval"
    return ""


def _watch_policy_exempt_from_terminal(agent: object | None, policy: ProgressPolicy, *, now: float) -> bool:
    if agent is None:
        return False
    try:
        from ..ingestion.wake_backstop import is_watch_progress_policy, owner_has_incomplete_watch

        return is_watch_progress_policy(policy) and owner_has_incomplete_watch(agent, now=now)
    except Exception:
        return False


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


def _suppressed_policy_action(agent: object | None, policy: ProgressPolicy, reason: str) -> str:
    """被抑制 policy 的处置裁决(纯结构信号):retire=退休 / renew=续命推进排期。
    g8 问题B·stale 不杀活任务:错过追赶窗常见于唤醒轮长期领不到 claim/网关中断,任务本身
    可能还活着——清单还有未闭环项时不 disable,只把排期推到 now+interval 继续追(账没对完
    唤醒链不许死,与收口退休守卫同一原则)。churn 有界:每 interval 至多一轮 + 无进展退避
    8× 封顶;任务终态走 terminal_task_link 照常退休,清单全闭后收口自动退休——终点都在。
    无清单/读账失败按 0,行为与旧版完全一致。"""
    if reason not in _RETIRE_SUPPRESSION_REASONS:
        return "renew"
    if reason == "stale_missed_interval" and ledger_open_coverage_target_count(agent, policy.task_id) > 0:
        return "renew"
    return "retire"


def _apply_suppressed_policy(store, policy: ProgressPolicy, action: str, *, now: float) -> None:
    try:
        if action == "retire":
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
    agent: object | None = None,
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
        _apply_suppressed_policy(store, policy, _suppressed_policy_action(agent, policy, reason), now=now)
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
