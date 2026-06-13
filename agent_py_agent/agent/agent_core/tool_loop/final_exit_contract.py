# LLM: run 模式主代理的"最终出口合同"(任务完成力底座 P2-1+P1-1,实锤来源
#   docs/audits/R5-three-tasks-20260611.md)。契约:模型给出无工具调用的最终回复
#   (主循环 break 分支)时,若存在未收口任务态(产物可验 / 未终态子代理 / open
#   capability_request),必须先走 delivery closeout;closeout 失败(rework 已由
#   uncontracted 链注入 tool_context)时在"续航预算 + 进展 delta"双闸内继续工具
#   循环让模型修复,闸断才放行退出(finalize 兜底 REWORK+resume)。触发条件全部
#   是结构化事实,绝不解析自然语言判断"模型是否在放弃";纯问答 run(无任务态)
#   零影响。只作用于主代理 default scope;task_local(子代理)/control_plane
#   (planner)不适用。改动时同步检查 tests/test_final_exit_contract.py、
#   _tool_loop_service 的 break 分支、_finalization_service(出口兜底)。
# 模块用途: 修两类真实失血:R5b/R5c"口头放弃绕过交付门"(零 closeout 零留档、
#   僵尸子代理),R5a"closeout 阻断后 run 直接退出、REPAIRING 悬空"。让 run 要么
#   把任务收口,要么在预算内继续修,要么诚实退出且留下可恢复痕迹。
from __future__ import annotations

from dataclasses import dataclass, field

from ..delivery_closeout.closeout import (
    MainAgentDeliveryCloseoutRequest,
    main_agent_delivery_closeout_response,
)
from ..delivery_closeout.subagent_aggregation import open_task_state_summary


@dataclass
class FinalExitState:
    """出口续航的循环内状态(每个 run 一份,由主循环持有)。"""

    continuations: int = 0
    last_open_signature: tuple = field(default_factory=tuple)
    question_guard_fired: bool = False


@dataclass(frozen=True)
class FinalExitDecision:
    """出口裁决:continue=回到工具循环修复;response 非空=以 closeout 结果为最终回复。"""

    should_continue: bool
    response: object | None = None


@dataclass(frozen=True)
class FinalExitRequest:
    """出口裁决入参包(agent/本轮 params/候选最终回复/循环续航状态)。"""

    agent: object
    params: object
    final_response: object
    state: FinalExitState


# LLM: 出口裁决唯一入口。决策树(全结构化事实):
#   ①非 default scope / 已带 closeout 标记的回复 → 放行;
#   ②无任务态(无产物候选、无未收口子代理)→ 放行(纯问答零影响);
#   ③跑 closeout:成功 → 用 closeout 回复替换 final;失败(None,rework 已注入)
#     → 双闸判定:预算 run_repair_max_continuations(0=关闭续航)内且未收口签名
#     有变化(或首次)→ 续航;否则放行(finalize 兜底 REWORK+resume)。
#   副作用:跑 closeout 会写 .agent_delivery/closeout.json 与注入 tool_context。
# 函数用途: 模型想结束这轮时,决定"放行 / 用验收结果收口 / 打回去继续修"。
def final_exit_closeout_decision(request: FinalExitRequest) -> FinalExitDecision:
    agent, params = request.agent, request.params
    if not _final_exit_applies(agent, params, request.final_response):
        return FinalExitDecision(should_continue=False)
    open_summary = open_task_state_summary(_task_root(agent, params))
    if not _closeout_candidate(agent, params, open_summary):
        if _question_exit_guard_applies(params, request.final_response, request.state):
            request.state.question_guard_fired = True
            _append_question_guard_instruction(params)
            return FinalExitDecision(should_continue=True)
        return FinalExitDecision(should_continue=False)
    response = main_agent_delivery_closeout_response(
        MainAgentDeliveryCloseoutRequest(
            agent=agent,
            params=params,
            backend=str(getattr(request.final_response, "backend", "") or ""),
        )
    )
    if response is not None:
        return FinalExitDecision(should_continue=False, response=response)
    decision = _continuation_decision(agent, params, request.state)
    if decision.should_continue:
        # 兜底注入(R6a 实锤:contract 路径的 closeout 失败不注入任何 rework 指令,
        # 模型被打回却不知道为什么/该做什么,白跑一轮后签名不变即放行)。
        # 无论 closeout 哪条路失败,出口合同保证打回必有结构化指令。
        _ensure_exit_rework_hint(params, open_summary)
        return decision
    # 闸断放行但任务未收口:出口合同自己完成余留合同(P1-2)——不依赖 finalize 的
    # REWORK 路径(contract 产物缺失会把它短路),在模型原文后追加结构化未完成声明。
    unfinished = _unfinished_exit_response(request, open_summary)
    if unfinished is not None:
        return FinalExitDecision(should_continue=False, response=unfinished)
    return decision


# LLM: 非 break 出口(工具轮数耗尽等系统截停)的余留合同直通口:这些出口没有
#   续航语义(模型已不能再用工具,打回毫无意义),但孤儿回收 + RUN_UNFINISHED_EXIT
#   声明同样必须发生(R5a 形态:轮数耗尽退出,未收口子代理无人处置)。复用
#   _unfinished_exit_response 全部逻辑;无未收口事实时原样放行。
# 函数用途: 系统强制收尾的 run 出口,同样要清后台进程、留可恢复声明。
def unfinished_exit_passthrough(agent, params, final_response):
    if not _final_exit_applies(agent, params, final_response):
        return final_response
    open_summary = open_task_state_summary(_task_root(agent, params))
    unfinished = _unfinished_exit_response(
        FinalExitRequest(agent=agent, params=params, final_response=final_response, state=FinalExitState()),
        open_summary,
    )
    return unfinished if unfinished is not None else final_response


# 函数用途: 出口合同的适用面判定(只管主代理 default scope、未收口过的回复)。
def _final_exit_applies(agent, params, final_response) -> bool:
    scope = str(getattr(params, "context_scope", "") or "default").strip().lower()
    if scope not in {"", "default"}:
        return False
    text = str(getattr(final_response, "text", "") or "")
    if "[MAIN_AGENT_DELIVERY_COMPLETE]" in text or "[MAIN_AGENT_DELIVERY_REWORK_REQUIRED]" in text:
        return False
    return True


# LLM: closeout candidate = 产物可验(复用 finalize 的判定)OR 未收口任务态
#   (P2-1 扩展:非终态子代理 / open capreq——R5b/R5c 绕门缺口)OR 派过子代理
#   (P5-1:哪怕全部终态,零产物也必须走门交结果文件,空交付门在 closeout 内拦)。
# 函数用途: 判断这轮 run 有没有"必须走验收门"的客观事实。
def _closeout_candidate(agent, params, open_summary: dict) -> bool:
    if open_summary.get("open_children") or open_summary.get("open_capability_requests"):
        return True
    if open_summary.get("children_total"):
        return True
    from .._finalization_service import _has_final_closeout_candidate

    return _has_final_closeout_candidate(params, agent)


# LLM: 续航双闸:①预算 run_repair_max_continuations(主配置,0=关闭续航保持旧
#   行为);②进展签名(open 子代理数/open capreq 数/progress open 项数)与上次
#   完全相同 → 视为无进展,停止续航防死循环。首次失败总是允许续一次。
# 函数用途: closeout 失败后决定"再给模型一轮修复机会"还是"诚实放行退出"。
def _continuation_decision(agent, params, state: FinalExitState) -> FinalExitDecision:
    budget = _max_continuations(agent)
    if budget <= 0:
        return FinalExitDecision(should_continue=False)
    signature = _open_state_signature(agent, params)
    if state.continuations >= budget:
        return FinalExitDecision(should_continue=False)
    if state.continuations > 0 and signature == state.last_open_signature:
        return FinalExitDecision(should_continue=False)
    state.continuations += 1
    state.last_open_signature = signature
    return FinalExitDecision(should_continue=True)


# LLM: 问句型零交付出口守卫(R11b 实锤:单次 run 干了 13 轮检索后列"方案A/B"
#   问用户并以问句结束——零产物零子代理,_closeout_candidate 三条件全不沾,
#   "礼貌请示"原样放行=白跑)。主信号全结构化:cli_run 单次模式+本 run 用过
#   工具(纯问答 run 通常零工具,不误伤)+零交付零派工;问句/等待指示形态只作
#   最后区分器(区分"模型答完了"与"模型反过来问人",形式特征非语义判定)。
#   幂等一次:打回后模型仍问 → 放行(避免无人应答场景死循环)。打回注入的
#   指令只重申 prompt 已有授权(自主决策/如实标注缺失),不替模型做任何选择。
_QUESTION_WAIT_PHRASES = ("等待您", "等待你", "请您选择", "请你选择", "请指示", "请告诉我", "请确认", "等待指示", "您的指示")


# 函数用途: 这个收尾是不是"单次运行里反过来问用户"的逃逸形态?
def _question_exit_guard_applies(params, final_response, state: FinalExitState) -> bool:
    if state.question_guard_fired:
        return False
    if str(getattr(params, "source", "") or "").strip() != "cli_run":
        return False
    if not list(getattr(params, "executed_tools", None) or []):
        return False
    text = str(getattr(final_response, "text", "") or "").rstrip()
    if not text:
        return False
    tail = text[-300:]
    return text.endswith(("?", "？")) or any(phrase in tail for phrase in _QUESTION_WAIT_PHRASES)


_QUESTION_GUARD_MARKER = "[exit-question-guard]"


# 函数用途: 打回时告诉模型"没人会回答你的提问,按既有授权自主决策并交付"。
def _append_question_guard_instruction(params) -> None:
    context = getattr(params, "tool_context", None)
    if not isinstance(context, list):
        return
    context.append(
        f"{_QUESTION_GUARD_MARKER}\n"
        "本次是单次运行(run),没有用户在线,刚才的提问不会得到任何回复。"
        "请按任务 prompt 已经给出的授权自主决策：可行的部分立即执行并产出；"
        "确实拿不到的部分按 prompt 的授权如实标注缺失与原因。"
        "把成果写入本次任务的交付目录并提交验收，不要再以提问或等待指示结束。"
    )


_EXIT_HINT_MARKER = "[final-exit-contract]"


# LLM: 出口打回的兜底指令(结构化,通用,零任务专项):closeout 任何路径失败而
#   没注入指令时,这里保证模型下一轮能看到"为什么被打回 + 该做什么"。幂等:
#   tool_context 已有本标记则不重复堆叠(B2 同款语义)。
# 函数用途: 被打回的模型必须知道原因——补一条带未收口计数与动作清单的指令。
def _ensure_exit_rework_hint(params, open_summary: dict) -> None:
    context = getattr(params, "tool_context", None)
    if not isinstance(context, list):
        return
    if any(_EXIT_HINT_MARKER in str(item) for item in context):
        return
    import json as _json

    payload = {
        "open_children": int(open_summary.get("open_children") or 0),
        "open_capability_requests": int(open_summary.get("open_capability_requests") or 0),
        "required_actions": [
            "inspect_agent_tree",
            "resolve_capability_requests",
            "read_child_result_or_wait_for_done",
            "cancel_subagents_or_takeover_if_child_is_no_longer_needed",
            "write_result_or_infeasibility_report_into_task_output",
            "submit_for_acceptance",
        ],
    }
    context.append(
        f"{_EXIT_HINT_MARKER}\n"
        + _json.dumps(payload, ensure_ascii=False, sort_keys=True)
        + "\n本轮不能用普通回复直接收尾：任务还有未收口的子代理/能力申请或未交付产物。"
        "请按 required_actions 处理后再提交验收；确认无法完成时，把结构化不可行报告"
        "写进任务交付目录再提交。"
    )


# LLM: 闸断放行时的余留合同(P1-2 的出口侧实现):保留模型原文,追加结构化
#   未完成声明(open 计数 + resume 入口),保证"停也停得可恢复"不依赖 finalize
#   的 REWORK 路径(R6a 实锤:contract 产物缺失会短路 finalize 的兜底)。
#   退出前先做孤儿子代理回收(R6a 实锤:后台 dispatch 是 start_new_session 独立
#   进程,不随主代理退出而停止,曾继续写占位符 21 分钟)——配置
#   run_exit_orphan_recovery_enabled 控制,回收结果进 resume 块 orphan_recovery
#   字段。仅在确有未收口子代理/capreq 时介入;返回 None 表示无需介入。
# 函数用途: run 带着没做完的任务退出时,先把后台帮手进程收掉,再把"怎么继续"
#   钉在最终回复末尾。
def _unfinished_exit_response(request: FinalExitRequest, open_summary: dict):
    agent, params, final_response = request.agent, request.params, request.final_response
    open_children = int(open_summary.get("open_children") or 0)
    open_requests = int(open_summary.get("open_capability_requests") or 0)
    if not open_children and not open_requests:
        return None
    import json as _json

    from ...backends import ModelResponse

    # 自学习复盘钩子(稳而不管 2-3):没做完的退出更要回头学——失败经验进
    # learning drafts 待审(enable_self_learning 才跑,失败静默)。
    from .._finalization_service import _latest_closeout_report
    from ..run_learning_review import maybe_run_learning_review

    maybe_run_learning_review(agent, params, _latest_closeout_report(agent, params))
    recovery_payload, recovery_caveat = _exit_orphan_recovery(agent, params)
    resume = {
        "task_root": str(_task_root(agent, params) or ""),
        "open_children": open_children,
        "open_capability_requests": open_requests,
        "orphan_recovery": recovery_payload,
        "how_to_continue": [
            "再次对同一任务发起 run（启动检测会带出未完成任务）",
            "my-agent subagents-dispatch --apply --start-runners 续派未完成子代理",
            "my-agent task-list / task-show <id> 查看任务状态",
        ],
    }
    text = (
        str(getattr(final_response, "text", "") or "")
        + "\n\n[RUN_UNFINISHED_EXIT]\n"
        + _json.dumps(resume, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n[/RUN_UNFINISHED_EXIT]\n"
        "本轮结束时任务尚未收口（见上方结构化字段）；"
        + recovery_caveat
        + "可按 how_to_continue 恢复推进。"
    )
    return ModelResponse(text=text, backend=str(getattr(final_response, "backend", "") or ""))


# LLM: 出口回收接线:开关 run_exit_orphan_recovery_enabled(默认开)。开 → 调
#   exit_orphan_recovery 终止后台进程并 requeue 被打断任务;关 → 不动进程,但
#   resume 块必须如实声明"后台子代理进程不会随本进程退出而停止"(旧文案与事实
#   相反,R6a 实锤后修正)。返回(resume 块 payload, 末尾文案句)。
# 函数用途: 按配置决定退出前收不收后台进程,并给用户一句符合事实的交代。
def _exit_orphan_recovery(agent, params) -> tuple[dict, str]:
    enabled = bool(getattr(getattr(agent, "config", None), "run_exit_orphan_recovery_enabled", True))
    if not enabled:
        return (
            {"enabled": False},
            "注意：后台子代理进程不会随本进程退出而停止（回收开关已关闭），"
        )
    from .exit_orphan_recovery import recover_orphan_subagents

    payload = recover_orphan_subagents(agent, _task_root(agent, params))
    payload["enabled"] = True
    return (
        payload,
        "未收口子代理的后台进程已按出口合同回收（结果见 orphan_recovery 字段），"
    )


# 函数用途: 读主配置的续航预算(异常值按 0=关闭处理)。
def _max_continuations(agent) -> int:
    try:
        return max(0, int(getattr(getattr(agent, "config", None), "run_repair_max_continuations", 0) or 0))
    except (TypeError, ValueError):
        return 0


# LLM: 进展签名只用结构化计数(closeout 报告/canonical 状态),不读模型文本。
# 函数用途: 给"这轮修复有没有真改变局面"算一个可对比的指纹。
def _open_state_signature(agent, params) -> tuple:
    summary = open_task_state_summary(_task_root(agent, params))
    report = _latest_closeout_counts(agent, params)
    return (
        int(summary.get("open_children") or 0),
        int(summary.get("open_capability_requests") or 0),
        report,
    )


# 函数用途: 从最近一次 closeout 报告取 task_progress 的 open 计数(取不到记 -1)。
def _latest_closeout_counts(agent, params) -> int:
    from .._finalization_service import _latest_closeout_report

    report = _latest_closeout_report(agent, params)
    gate = report.get("task_progress_closeout_gate")
    if not isinstance(gate, dict):
        return -1
    evidence = gate.get("evidence")
    if not isinstance(evidence, dict):
        return -1
    try:
        return int(evidence.get("open_count"))
    except (TypeError, ValueError):
        return -1


# 函数用途: 解析当前 run 的任务工作区根(出口合同所有事实都从这里读)。
def _task_root(agent, params):
    from ..run_task_workspace_writer import current_run_task_workspace_root

    return current_run_task_workspace_root(agent, params)


__all__ = [
    "FinalExitDecision",
    "FinalExitRequest",
    "FinalExitState",
    "final_exit_closeout_decision",
    "unfinished_exit_passthrough",
]
