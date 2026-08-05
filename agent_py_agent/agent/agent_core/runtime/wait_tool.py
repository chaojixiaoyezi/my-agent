from __future__ import annotations

import json

from ...tooling.models import BaseTool, ToolExecutionResult, ToolSpec
from ..runner.context import current_subagent_run_id
from .task_identity import durable_task_id

_TOOL_NAME = "wait"
_MIN_SECONDS = 60
_MAX_SECONDS = 7200


class WaitTool(BaseTool):
    def __init__(self, agent: object) -> None:
        self.agent = agent
        self.spec = build_wait_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        if _truthy(params.get("cancel")):
            # cancel 不走 _target:那条路会在没绑线程时顺手新建内部线程,取消场景不该有副作用。
            return _cancel_result(self.agent, params)
        _promote_wait_conversation(self.agent, params)
        interval = _seconds(params.get("seconds"), self.agent)
        target = _target(self.agent, params)
        if isinstance(target, ToolExecutionResult):
            return target
        thread_id, task_id = target
        actionable = _actionable_audit_input(self.agent, task_id)
        if actionable:
            return _error(
                "actionable_audit_input_pending",
                (
                    "当前 Audit 已有完整持久化但尚未签收的记录，wait 不能替代立即可推进的工作。"
                    "由你根据任务目标自行处理或委派；若已有真实后台执行者，可直接结束本轮，"
                    "其完成事件和持久任务续跑会继续唤醒，不要再次空等。"
                ),
                error_code="WAIT_ACTIONABLE_INPUT_PENDING",
                facts=actionable,
            )
        # 同一任务+线程重复登记 = 【更新】提醒(模型每轮唤醒常带新游标/新原因重新 wait),
        #   旧的先退休再登记新的——不堆积多份同任务 policy 互相打架(实测一次 5 分钟盯守
        #   堆出 11 份,全靠调度器去重兜底;更新语义从源头治)。
        _disable_same_watch_policies(self.agent.conversation_store, thread_id, task_id)
        policy = self.agent.conversation_store.set_progress_policy(
            {
                "thread_id": thread_id,
                "task_id": task_id,
                "interval_seconds": interval,
                # 会话运行时 wait is an internal runtime yield.  Keep the
                # route fixed at the execution boundary as well as hiding it
                # from the model schema: unknown/legacy extra arguments must
                # never turn wait into an outbound notification surface.
                "route_channel": "internal",
                "route_target": "",
                "metadata": {
                    "kind": "subagent_progress_watch",
                    "tool": _TOOL_NAME,
                    "scope": str(params.get("scope") or "own_task_tree"),
                    "reason": str(params.get("reason") or "").strip(),
                    "watch_run_id": str(params.get("run_id") or task_id),
                },
            }
        )
        payload = {
            "ok": True,
            "scheduled": True,
            "mode": "nonblocking_schedule",
            "delivery_scope": "internal_agent_only",
            "user_notification_created": False,
            "policy_id": policy.policy_id,
            "thread_id": thread_id,
            "task_id": task_id,
            "interval_seconds": policy.interval_seconds,
            "min_seconds": _MIN_SECONDS,
            "max_seconds": _MAX_SECONDS,
            "next_due_at": policy.next_due_at,
            "reason": str(params.get("reason") or "").strip(),
            "next_action": "end_turn_and_yield",
            "guidance": (
                "已登记当前任务的内部非阻塞唤醒,到点系统会自动唤醒你继续当前任务。"
                "它不会创建用户提醒、定时任务或出站消息；用户要求未来提醒时必须使用 schedule。"
                "wait 永不阻塞当前回合——不会原地睡等。"
                "现在请把本回合该说的说完并结束本回合:到点提醒、子代理完成事件或用户新消息都会把你叫回来接着干。"
                "提醒按 interval 循环触发;任务收口(验收通过)后自动停止,不再需要时也可用 cancel=true 手动停。"
                "不要原地循环轮询。"
            ),
        }
        return ToolExecutionResult(_TOOL_NAME, True, json.dumps(payload, ensure_ascii=False, indent=2))


def _promote_wait_conversation(agent: object, params: dict[str, object]) -> None:
    """真实 wait 是结构化定时/后台事实，此刻才把普通会话绑定成任务。"""
    from ...conversation.task_promotion import promote_current_conversation_task

    promote_current_conversation_task(agent, goal=str(params.get("reason") or ""))


_WAIT_USE_CASES = [
    "当前任务需要让出执行权，并在稍后由同一个 Agent 恢复",
    "外部状态尚未就绪，稍后根据持久任务状态重新决定下一步",
    "任务不再需要内部唤醒时，用 cancel=true 取消对应 policy",
]
_WAIT_AVOID_WHEN = [
    "用户要求未来提醒、定时执行或到点向用户发消息时不要使用 wait；必须使用 schedule。"
    "wait 只唤醒 agent 自己，route_channel=internal，不会创建用户通知。",
    "需要取消、接管、恢复或给子代理补充提示时不要只设提醒，应使用对应控制工具",
    "已有可立即推进的工作时，wait 不应替代正常工具调用",
    "当前持久任务已有未签收输入时不能使用 wait；先处理、委派，或在真实后台执行者已运行时直接结束本轮",
]


def build_wait_spec() -> ToolSpec:
    return ToolSpec(
        name=_TOOL_NAME,
        category="orchestration",
        effect="read_only",
        promotes_task=True,
        description=(
            "登记一个只在当前任务内部生效、到点自动唤醒 agent 的非阻塞等待(按间隔循环触发)："
            "等子代理进度、盯持续增长的"
            "文件/数据源、周期性自查、长任务阶段性推进都用它。登记后结束本回合，到点系统会自动"
            "唤醒你继续当前任务；永不原地睡等。它不创建用户提醒或出站消息；用户的未来提醒使用 schedule。"
        ),
        use_cases=_WAIT_USE_CASES,
        avoid_when=_WAIT_AVOID_WHEN,
        keywords=["等待", "watch", "yield", "wait", "冷却", "不要轮询", "监控", "盯", "持续", "巡检", "子代理进度"],
        parameters={
            "seconds": f"多少秒后内部唤醒 agent 查看；不填使用配置 subagent_watch_interval_seconds，最低 {_MIN_SECONDS}，最高 {_MAX_SECONDS}",
            "run_id": "可选，想查看的代理 run；默认当前 task/run",
            "task_id": "可选，绑定到哪个任务；默认当前运行任务",
            "thread_id": "可选，绑定到哪个会话线程；默认按 task_id 查找或自动创建内部线程",
            "scope": "可选，查看范围提示，默认 own_task_tree",
            "reason": "可选但强烈建议填：为什么等待/下次醒来该干什么——唤醒时会原样带给你",
            "cancel": "可选，true 时停掉当前任务/会话已登记的循环提醒（任务结束或不再需要盯守时用）",
        },
        parameter_schema={
            "seconds": {"type": "integer", "minimum": 0},
            "run_id": {"type": "string"},
            "task_id": {"type": "string"},
            "thread_id": {"type": "string"},
            "scope": {"type": "string"},
            "reason": {"type": "string"},
            "cancel": {"type": "boolean"},
        },
        examples=[
            '{"tool":"wait","seconds":120,"reason":"刚启动子代理，稍后看一次进度"}',
            '{"tool":"wait","seconds":120,"reason":"盯守日志文件增量，醒来后从上次行号继续读新行，有目标事件才上报"}',
            '{"tool":"wait","cancel":true,"reason":"盯守任务已结束，停止循环提醒"}',
        ],
    )


# 函数用途: 派工出口的机制层监督提醒(治"说了登记提醒却没真调"实锤:模型宣称已登记
#   非阻塞等待,owner store 的 progress_policies/ 却是空目录——整个盯守窗口零定时唤醒,
#   中途上报只能等完成事件)。create_subagents 成功后由机制层自动登记,不依赖模型自觉;
#   已有 enabled 同任务提醒(模型真调过 wait / 上次派工已登记)则不动,不覆盖模型显式
#   间隔。生命周期完全复用 wait 提醒既有机制:收口自动退休、终态链接抑制、无进展退避。
#   best-effort:任何失败返回 None,绝不影响派工本身。
def register_dispatch_supervision_policy(
    agent: object,
    *,
    run_ids: list[str] | None = None,
) -> dict[str, object] | None:
    try:
        return _register_dispatch_supervision(agent, run_ids=run_ids or [])
    except Exception:  # noqa: BLE001 - 监督提醒是增强,失败绝不影响派工
        import logging

        logging.getLogger(__name__).warning("dispatch supervision policy register failed", exc_info=True)
        return None


def _register_dispatch_supervision(
    agent: object,
    *,
    run_ids: list[str],
) -> dict[str, object] | None:
    interval = int(getattr(getattr(agent, "config", None), "dispatch_supervision_reminder_seconds", 0) or 0)
    if interval <= 0:
        return None
    target = _target(agent, {})
    if isinstance(target, ToolExecutionResult):
        return None
    thread_id, task_id = target
    store = agent.conversation_store
    if existing := _enabled_policy_for(store, thread_id, task_id):
        return _supervision_payload(existing, existing=True)
    from ...conversation.progress_fingerprint import subagent_material_signature

    watched_run_ids = _periodic_supervision_run_ids(agent, run_ids)
    if run_ids and not watched_run_ids:
        return None
    signature = subagent_material_signature(
        agent,
        task_id=task_id,
        watched_run_ids=watched_run_ids,
    )
    policy = store.set_progress_policy(
        {
            "thread_id": thread_id,
            "task_id": task_id,
            "interval_seconds": _clamp_interval(interval, default=180),
            "route_channel": "internal",
            "route_target": "",
            "metadata": {
                "kind": "subagent_progress_watch",
                "tool": "dispatch_supervision_auto",
                "scope": "own_task_tree",
                "reason": "机制层派工监督:巡查子代理进展/卡点,处理能力申请,有中途结论及时上报",
                "watch_run_id": task_id,
                "watched_run_ids": watched_run_ids,
                "material_signature": signature,
            },
        }
    )
    return _supervision_payload(policy, existing=False)


def _periodic_supervision_run_ids(agent: object, run_ids: list[str]) -> list[str]:
    """Exclude durable Audit source workers from periodic parent LLM polling.

    Ordinary delegated work still needs low-frequency supervision between
    lifecycle events.  A source worker is a long-lived lease consumer with its
    own stall recovery and finding outbox; every settled batch is material
    progress, so a generic progress policy would wake the main Agent forever.
    """

    from ...common.audit_activation import AUDIT_SOURCE_WORKER_ATTR

    selected: list[str] = []
    manager = getattr(agent, "subagents", None)
    loader = getattr(manager, "load", None)
    for value in sorted({str(item) for item in run_ids if str(item).strip()}):
        if not callable(loader):
            selected.append(value)
            continue
        try:
            task = loader(value)
        except Exception:
            selected.append(value)
            continue
        attrs = getattr(task, "attributes", None)
        if isinstance(attrs, dict) and attrs.get(AUDIT_SOURCE_WORKER_ATTR) is True:
            continue
        selected.append(value)
    return selected


# 函数用途: 监督登记回执只带原生类型(policy 对象可能是测试替身,原样塞进工具
#   payload 会让 create_subagents 的 json.dumps 在注册 try/except 之外炸)。
def _supervision_payload(policy: object, *, existing: bool) -> dict[str, object]:
    try:
        interval = int(getattr(policy, "interval_seconds", 0) or 0)
    except (TypeError, ValueError):
        interval = 0
    return {
        "policy_id": str(getattr(policy, "policy_id", "") or ""),
        "existing": existing,
        "interval_seconds": interval,
    }


def _enabled_policy_for(store, thread_id: str, task_id: str):
    for policy in store.list_progress_policies(enabled_only=True):
        if policy.thread_id == thread_id and policy.task_id == task_id:
            return policy
    return None


# 函数用途: 退休"同一线程+同一任务"上已登记的循环提醒——wait 重复登记按更新语义处理。
def _disable_same_watch_policies(store, thread_id: str, task_id: str) -> None:
    if not callable(getattr(store, "list_progress_policies", None)):
        return
    for policy in store.list_progress_policies(enabled_only=True):
        if policy.thread_id == thread_id and policy.task_id == task_id:
            store.disable_progress_policy(policy.policy_id)


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"true", "1", "yes"}


# 函数用途: 停掉当前任务/会话上已登记的循环提醒（模型显式收手的开关；任务结构化结束时
#   系统也会自动退休，这里是“任务仍在但确定不用再盯”的手动出口）。按 task_id 或
#   显式 thread_id 匹配,禁用所有命中的 enabled policy。
def _cancel_result(agent: object, params: dict[str, object]) -> ToolExecutionResult:
    store = getattr(agent, "conversation_store", None)
    if store is None or not callable(getattr(store, "list_progress_policies", None)):
        return _error(
            "conversation_store_unavailable",
            "当前运行时没有会话调度存储，没有可取消的提醒。",
            error_code="TOOL_UNAVAILABLE",
        )
    task_id = _task_id(agent, params)
    thread_id = str(params.get("thread_id") or "").strip()
    cancelled: list[str] = []
    for policy in store.list_progress_policies(enabled_only=True):
        if (task_id and policy.task_id == task_id) or (thread_id and policy.thread_id == thread_id):
            store.disable_progress_policy(policy.policy_id)
            cancelled.append(policy.policy_id)
    payload = {
        "ok": True,
        "mode": "cancel",
        "task_id": task_id,
        "cancelled_policy_ids": cancelled,
        "cancelled_count": len(cancelled),
        "guidance": "循环提醒已停止；若任务已有结果，请核对事实后直接给出最终回复。",
    }
    return ToolExecutionResult(_TOOL_NAME, True, json.dumps(payload, ensure_ascii=False, indent=2))


def _seconds(value: object, agent: object) -> int:
    if value is None:
        return _clamp_interval(getattr(getattr(agent, "config", None), "subagent_watch_interval_seconds", 120), default=120)
    return _clamp_interval(value, default=_MIN_SECONDS)


def _clamp_interval(value: object, *, default: int) -> int:
    parsed = _interval_int(value)
    if parsed is None:
        return default
    if parsed < _MIN_SECONDS:
        return _MIN_SECONDS
    if parsed > _MAX_SECONDS:
        return _MAX_SECONDS
    return parsed


def _interval_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit() or (stripped.startswith("-") and stripped[1:].isdigit()):
            return int(stripped)
    if isinstance(value, float) and value == int(value):
        return int(value)
    return None


def _target(agent: object, params: dict[str, object]) -> tuple[str, str] | ToolExecutionResult:
    store = getattr(agent, "conversation_store", None)
    if store is None or not callable(getattr(store, "set_progress_policy", None)):
        return _error(
            "conversation_store_unavailable",
            "当前运行时没有会话调度存储，无法登记非阻塞提醒。",
            error_code="TOOL_UNAVAILABLE",
        )
    task_id = _task_id(agent, params)
    if not task_id:
        return _error(
            "task_id_required",
            "缺少 task_id/run_id；无法知道要稍后查看哪棵代理树。",
            error_code="TOOL_PARAMETER_REQUIRED",
        )
    thread_id = str(params.get("thread_id") or "").strip()
    thread = None
    if not thread_id:
        try:
            thread = store.thread_for_task(task_id)
        except Exception:
            thread = None
        thread_id = str(getattr(thread, "thread_id", "") or "").strip()
    if not thread_id:
        thread = store.get_or_create_thread(
            {
                "canonical_user_id": "local-agent",
                "channel": "internal",
                "channel_conversation_id": f"task:{task_id}",
                "channel_user_id": "local-main-agent",
                "title": f"subagent-watch:{task_id}",
            }
        )
        thread_id = str(getattr(thread, "thread_id", "") or "").strip()
        store.bind_task({"thread_id": thread_id, "task_id": task_id, "goal": f"子代理进度查看: {task_id}"})
    return thread_id, task_id


def _task_id(agent: object, params: dict[str, object]) -> str:
    for key in ("task_id", "run_id", "root_id"):
        value = str(params.get(key) or "").strip()
        if value:
            return value
    current = getattr(agent, "_current_run_params", None)
    value = durable_task_id(current)
    if value:
        return value
    # A background-main run id identifies one execution attempt, not a durable
    # task.  Taskless internal events must stay taskless: using the transient
    # ``bg-main-*`` id here creates a synthetic task whose own wait policy wakes
    # it again forever.  Real background continuations already carry their
    # exact durable task id through ``RunParams``/task attributes above.
    if str(getattr(current, "source", "") or "").strip() == "background_main_agent":
        return ""
    return str(current_subagent_run_id(agent) or getattr(agent, "_main_agent_run_id", "") or "").strip()


# LLM: This guard reads only typed Audit activation plus exact durable receipt
# counters. It does not choose a judgment route, score threshold, worker count,
# or business meaning; it only prevents a delay primitive from replacing work
# that is already durably available.
# 函数用途: Audit 已有待签收数据时拒绝继续空等，并把精确积压事实返回给模型自行决策。
def _actionable_audit_input(
    agent: object,
    fallback_task_id: str,
) -> dict[str, object] | None:
    from ...common.audit_activation import (
        attributes_request_audit,
        audit_lineage_task_id,
        current_audit_attributes,
    )

    attrs = current_audit_attributes(agent)
    if not attributes_request_audit(attrs):
        return None
    task_id = audit_lineage_task_id(agent) or str(fallback_task_id or "").strip()
    if not task_id:
        return None
    from ...ingestion.audit_state import audit_task_source_facts

    pending_sources: list[dict[str, object]] = []
    pending_total = 0
    try:
        sources = audit_task_source_facts(agent, task_id)
    except Exception:
        return None
    for source in sources:
        receipt = source.get("audit_receipt")
        if not isinstance(receipt, dict):
            continue
        pending = _nonnegative_int(receipt.get("pending"))
        if pending <= 0:
            continue
        pending_total += pending
        pending_sources.append(
            {
                "watch_id": str(source.get("watch_id") or ""),
                "pending": pending,
                "collection_active": bool(source.get("collection_active")),
                "window_complete": bool(source.get("window_complete")),
            }
        )
    if pending_total <= 0:
        return None
    return {
        "task_id": task_id,
        "pending_records": pending_total,
        "sources": pending_sources,
        "wait_allowed": False,
        "reason_code": "durable_input_already_available",
    }


# LLM: Receipt counters are typed non-negative integers; malformed projections
# fail closed to zero here and remain visible through their source-state error.
# 函数用途: 安全读取积压计数，避免异常值把普通等待误拦。
def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _error(
    code: str,
    message: str,
    *,
    error_code: str | None = None,
    facts: dict[str, object] | None = None,
) -> ToolExecutionResult:
    # code 是给人/日志看的语义标签(写进 payload.error)；error_code 必须是 taxonomy 已注册码，
    # 否则 ToolExecutionResult 会把未注册的小写 code 兜底成 UNKNOWN_ERROR(retryable=False)，
    # 误导模型"放弃报阻塞"，而 store 缺失/缺 task_id 其实是可换工具/补参数修复的。
    payload = {"ok": False, "error": code, "message": message}
    if facts:
        payload["facts"] = facts
    return ToolExecutionResult(
        _TOOL_NAME,
        False,
        json.dumps(payload, ensure_ascii=False, indent=2),
        error_code=error_code or code,
    )


__all__ = ["WaitTool", "build_wait_spec", "register_dispatch_supervision_policy"]
