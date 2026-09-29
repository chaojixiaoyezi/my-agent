# LLM: /endtask 只在已认证 scope 解析出的本机管理员 owner 上执行，只处理“定时执行 waiting、会话任务 active、执行树没有未结束 attempt”的任务。
#   列表与预览只读；只有 confirm 才写：会话任务按 expected_status=active 改为 cancelled，再对同一任务调 reconcile_waiting_run。
#   不读任务正文、不改运行库、不重做未确认的操作。改动须同步 test_end_task_control.py 与 CLI_REFERENCE.md 的 /endtask 说明。
# 模块用途: 让管理员在 TUI 或飞书里查看并结束卡在等待中的定时会话任务（结果未知后定时层永远在等的那种），解开被它堵住的定时任务。
from __future__ import annotations

import time
from dataclasses import dataclass

from ..conversation.control_commands import ConversationControlCommand, ConversationControlResult
from ..runtime_db.operations import attempt_status_is_terminal
from ..user_space.approval_mode import is_permission_admin

_KIND = "endtask"
_CANDIDATE_LIMIT = 20
_REFUSALS = {
    "not_waiting": (
        "END_TASK_NOT_WAITING_RUN",
        "{task_id} 不是等待中的定时执行（定时运行状态：{run_status}）；/endtask 只处理卡在等待中的定时会话任务。",
    ),
    "task_not_active": (
        "END_TASK_NOT_ACTIVE",
        "会话任务 {task_id} 当前状态是 {task_status}，不是 active，无需结束。",
    ),
    "live_attempts": (
        "END_TASK_LIVE_ATTEMPT",
        "会话任务 {task_id} 的执行树里还有没结束的执行（{attempts}），不能结束；等它结束后再试。",
    ),
    "runtime_unavailable": (
        "END_TASK_RUNTIME_UNAVAILABLE",
        "运行库暂时不可读，无法确认 {task_id} 没有正在执行的工作，没有做任何改动。",
    ),
}
_SHORT_REASONS = {
    "not_waiting": "不在等待",
    "task_not_active": "任务已不是 active",
    "live_attempts": "还有执行在跑",
    "runtime_unavailable": "运行库不可读",
}


# LLM: 只装结构化事实（状态码、ID、时间），不装任务正文；refusal 为空串表示满足全部结束条件。
# 类用途: 汇总一条定时会话任务能否结束的判断依据，供列表、预览和确认结束共用。
@dataclass(frozen=True)
class _EndTaskFacts:
    task_id: str
    thread_id: str
    run_status: str
    waiting_since: float
    task_status: str
    live_attempts: tuple[str, ...]
    refusal: str


# LLM: owner_agent 由 control_service 按已认证 scope 解析；只有 operation=apply 且事实全部满足时才写会话任务链接与定时账本，
#   其余路径只读。管理员判定只认 home_paths 的结构化 owner 身份。
# 函数用途: 执行 /endtask：列出候选、预览一条任务，或在管理员确认后结束它并结算被它堵住的定时执行。
def execute_end_task_control(
    owner_agent: object,
    command: ConversationControlCommand,
) -> ConversationControlResult:
    if not is_permission_admin(getattr(owner_agent, "home_paths", None)):
        return ConversationControlResult(
            _KIND, False, "只有管理员可以结束定时会话任务。", error_code="END_TASK_ADMIN_ONLY",
        )
    if command.operation == "list":
        return ConversationControlResult(_KIND, True, _render_candidates(owner_agent))
    facts = _task_facts(owner_agent, command.value)
    if facts.refusal:
        code, template = _REFUSALS[facts.refusal]
        return ConversationControlResult(_KIND, False, _refusal_text(template, facts), error_code=code)
    if command.operation != "apply":
        return ConversationControlResult(_KIND, True, _render_plan(facts))
    return _end_task(owner_agent, facts)


# LLM: 唯一写入口：会话任务链接按 expected_status=active 的 CAS 改为 cancelled（冲突则不写、提示重看），随后只对同一
#   task_id 调 reconcile_waiting_run；结算没成不回滚任务状态，定时层每轮入队前的批量对账会再次收口。
# 函数用途: 结束一条已核实可结束的定时会话任务，并立即结算它堵住的定时执行（写会话存储与定时账本）。
def _end_task(owner_agent: object, facts: _EndTaskFacts) -> ConversationControlResult:
    link = owner_agent.conversation_store.tasks.update_status(
        {"task_id": facts.task_id, "status": "cancelled", "expected_status": "active"}
    )
    if link is None:
        return ConversationControlResult(
            _KIND,
            False,
            f"会话任务 {facts.task_id} 的状态刚刚变化，没有改动；请重新输入 /endtask {facts.task_id} 查看。",
            error_code="END_TASK_STATE_CHANGED",
        )
    settled = owner_agent.scheduler_service.reconcile_waiting_run(facts.task_id)
    tail = ("它堵住的定时执行已结算，定时任务会按计划起新一轮运行。" if settled
            else "它堵住的定时执行会在下一轮定时对账时结算。")
    return ConversationControlResult(
        _KIND, True, f"已结束会话任务 {facts.task_id}（记为 cancelled）。{tail}未确认的操作不会被重做。",
    )


# LLM: 三类事实分别来自定时账本（run 状态）、会话存储（任务链接状态）和运行库（执行树 attempt），缺一不放行；
#   只读，不按任务正文或 ID 前缀猜任务类型。
# 函数用途: 读取判断一条定时会话任务能否结束所需的结构化事实，并给出第一条不满足的原因。
def _task_facts(owner_agent: object, task_id: str) -> _EndTaskFacts:
    run = owner_agent.scheduler_repository.get_active_run(task_id) or {}
    link = owner_agent.conversation_store.tasks.load(task_id)
    live = _live_attempts(owner_agent, task_id)
    run_status = str(run.get("status") or "")
    task_status = str(getattr(link, "status", "") or "")
    return _EndTaskFacts(
        task_id=task_id,
        thread_id=str(run.get("thread_id") or getattr(link, "thread_id", "") or ""),
        run_status=run_status,
        waiting_since=float(run.get("waiting_since") or 0.0),
        task_status=task_status,
        live_attempts=live or (),
        refusal=_refusal(run_status, task_status, live),
    )


# LLM: 判断顺序固定：先定时运行是否 waiting，再会话任务是否 active，最后运行库能否确认执行树已静止；
#   live 为 None 表示运行库不可读，必须拒绝而不是当作空闲。
# 函数用途: 按固定顺序给出第一条不能结束的原因码，全部满足时返回空串。
def _refusal(run_status: str, task_status: str, live: tuple[str, ...] | None) -> str:
    if run_status != "waiting":
        return "not_waiting"
    if task_status != "active":
        return "task_not_active"
    if live is None:
        return "runtime_unavailable"
    return "live_attempts" if live else ""


# LLM: 查这个任务全部 TaskRun 下整棵 AgentRun 树（含子代理）的 attempt，终态判定只用 runtime_db 的唯一出口；
#   没有运行库或读取失败都返回 None，让调用方拒绝结束。
# 函数用途: 列出这个任务执行树里还没结束的 attempt，确认结束任务不会打断正在跑的工作。
def _live_attempts(owner_agent: object, task_id: str) -> tuple[str, ...] | None:
    repo = getattr(getattr(owner_agent, "subagents", None), "runtime_db", None)
    if repo is None:
        return None
    try:
        runs = [run for task_run in repo.task_runs_for_task(task_id)
                for run in repo.agent_runs_for_task_run(str(task_run["task_run_id"]))]
        attempts = [row for run in runs for row in repo.attempts_for_run(str(run["agent_run_id"]))]
    except Exception:  # noqa: BLE001 - 运行库读失败一律按“无法确认已静止”处理，拒绝结束
        return None
    return tuple(str(row["attempt_id"]) for row in attempts if not attempt_status_is_terminal(row["status"]))


# LLM: 只拼结构化字段（ID、状态码），不含任务正文；未结束的 attempt 最多列 3 个。
# 函数用途: 把拒绝原因模板填成给用户看的一句话。
def _refusal_text(template: str, facts: _EndTaskFacts) -> str:
    return template.format(
        task_id=facts.task_id,
        run_status=facts.run_status or "无",
        task_status=facts.task_status or "不存在",
        attempts="、".join(facts.live_attempts[:3]),
    )


# LLM: 只渲染结构化字段（任务 ID、会话 ID、等待起点），不含任务正文，可以安全投到 IM。
# 函数用途: 生成 /endtask <任务ID> 的只读预览：说明确认后会做什么，以及确认命令。
def _render_plan(facts: _EndTaskFacts) -> str:
    return "\n".join((
        f"定时会话任务 {facts.task_id}（会话 {facts.thread_id}）自 {_clock(facts.waiting_since)} 起停在等待；"
        "会话任务仍是 active，执行树里没有还在跑的执行。",
        "确认结束后：会话任务记为 cancelled，它堵住的定时执行随即结算，定时任务按计划起新一轮运行；未确认的操作不会被重做。",
        f"确认请发：/endtask {facts.task_id} confirm",
    ))


# LLM: 候选只来自定时账本里 waiting 的运行，逐条补会话任务状态与执行树事实；最多列 _CANDIDATE_LIMIT 条，账本坏行只计数。
# 函数用途: 生成 /endtask 无参数时的候选清单，标出哪些可以结束、哪些为什么不行。
def _render_candidates(owner_agent: object) -> str:
    runs, errors = owner_agent.scheduler_repository.waiting_runs()
    lines = [_candidate_line(_task_facts(owner_agent, str(run.get("run_id") or ""))) for run in runs[:_CANDIDATE_LIMIT]]
    if not lines:
        return "没有等待中的定时执行。" + (f"（定时账本有 {len(errors)} 条无法解析的记录）" if errors else "")
    head = f"等待中的定时执行 {len(runs)} 条" + (f"，只列前 {_CANDIDATE_LIMIT} 条" if len(runs) > _CANDIDATE_LIMIT else "") + "："
    return "\n".join([head, *lines, "预览：/endtask <任务ID>；结束：/endtask <任务ID> confirm"])


# LLM: 一行只含任务 ID、会话 ID、等待起点、任务状态和结论，不含正文。
# 函数用途: 渲染候选清单里的一行。
def _candidate_line(facts: _EndTaskFacts) -> str:
    verdict = _SHORT_REASONS.get(facts.refusal, "可结束")
    return (f"- {facts.task_id}｜会话 {facts.thread_id}｜等待自 {_clock(facts.waiting_since)}"
            f"｜任务 {facts.task_status or '不存在'}｜{verdict}")


# LLM: 纯格式化，只用本机时区显示，不参与任何判断。
# 函数用途: 把时间戳显示成本地“月-日 时:分”，缺失时显示“时间未知”。
def _clock(timestamp: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(timestamp)) if timestamp > 0 else "时间未知"


__all__ = ["execute_end_task_control"]
