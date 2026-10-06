# LLM: 续跑关联只读结构化来源（wake 信封的来源 run + 父 run 的持久父级），不按时间或线程猜；
#   解析失败/冲突留固定原因诊断，不猜测归属。写入口在 TaskStore（唯一权威位置），本模块只做解析与投影。
# 模块用途: 记录「子代理完成触发的续跑回合」对应哪个原请求，把最终回复的结构化指向写进原请求档案，
#   供管理员完整结果与取数侧找到最终回复；只存结构化指向，不存正文。
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

_LOGGER = logging.getLogger("agent.conversation.continuation_refs")

# 关联行/投影/诊断的 schema 标识；取数侧按这些键判断版本，不解析正文。
CONTINUATION_REF_SCHEMA = "continuation-ref.v1"
CONTINUATION_PROJECTION_SCHEMA = "continuation-projection.v1"
CONTINUATION_DIAGNOSTIC_SCHEMA = "continuation-diagnostic.v1"

# 固定原因码：解析不到原请求或目标档案缺失时只暴露这些代码，不猜归属、不写正文。
CONTINUATION_REASON_SOURCE_MISSING = "continuation_source_missing"
CONTINUATION_REASON_SOURCE_LOAD_FAILED = "continuation_source_load_failed"
CONTINUATION_REASON_PARENT_MISSING = "continuation_parent_missing"
CONTINUATION_REASON_SOURCE_CONFLICT = "continuation_source_conflict"
CONTINUATION_REASON_TARGET_MISSING = "continuation_target_missing"

# 只有子代理生命周期完成事件触发的回合才可能续接某个原请求；其它 wake 来源（goal/定时/派活）不写关联。
_CONTINUATION_WAKE_REASON = "subagent_runner_finished"


# LLM: 解析结果只携带结构化身份（请求/来源/父 run 与固定原因码）；warnings 暴露两侧冲突事实，调用方不得改写成猜测。
# 类用途: 承载一次「续跑回合续的是哪个原请求」的解析结论。
@dataclass(frozen=True)
class ContinuationSourceResolution:
    request_id: str = ""
    source_run_id: str = ""
    parent_run_id: str = ""
    reason: str = ""
    warnings: tuple[str, ...] = ()

    # 函数用途: 是否解析出了可写入的原请求。
    @property
    def resolved(self) -> bool:
        return bool(self.request_id)


# LLM: 续跑回合交付 final 的结构化事实快照（取自回合请求、交付计划与提交结果），只存身份、计数与交付原因，不存正文；
#   关联行与诊断行共用这一份，函数之间不再各自传 request/plan/commit 三件套。
# 类用途: 承载一次续跑交付要记录的事实。
@dataclass(frozen=True)
class ContinuationDelivery:
    reason: str = ""
    wake_signal: object = None
    turn_id: str = ""
    message_id: str = ""
    content_chars: int = 0
    persisted: bool = False
    delivery_reason: str = ""

    # LLM: 只读三个对象上的结构化属性，正文只取长度；调用方是 conversation.runtime 的后台交付收尾。
    # 函数用途: 从回合请求、交付计划和提交结果取出续跑关联要用的事实。
    @classmethod
    def from_turn(cls, turn: object, plan: object, commit: object) -> ContinuationDelivery:
        return cls(
            reason=str(getattr(turn, "reason", "") or "").strip().lower(),
            wake_signal=getattr(turn, "wake_signal", None),
            turn_id=str(getattr(turn, "conversation_turn_id", "") or ""),
            message_id=str(getattr(commit, "message_id", "") or "").strip(),
            content_chars=len(str(getattr(commit, "content", "") or "")),
            persisted=bool(getattr(commit, "persisted", False)),
            delivery_reason=str(getattr(plan, "delivery_reason", "") or ""),
        )


# LLM: 身份链路只用结构化来源：wake 信封的 source_agent_id（来源 run）+ parent_agent_id（父 run），
#   并用来源 run 的持久父级交叉验证；不一致时按冲突处理（不静默选一侧）。取不到就返回固定原因，不按时间/线程猜。
# 函数用途: 从 wake 信封解析原请求 id；解析失败/冲突时返回固定原因与两侧事实。
def resolve_continuation_source(agent: object, wake_signal: object) -> ContinuationSourceResolution:
    wake = wake_signal if isinstance(wake_signal, dict) else {}
    source_run_id = str(wake.get("source_agent_id") or "").strip()
    parent_run_id = str(wake.get("parent_agent_id") or "").strip()
    if not source_run_id or not parent_run_id:
        return ContinuationSourceResolution(reason=CONTINUATION_REASON_SOURCE_MISSING)
    manager = getattr(agent, "subagents", None)
    task = None
    if manager is not None:
        try:
            task = manager.load(source_run_id)
        except Exception:
            task = None
    if task is None:
        return ContinuationSourceResolution(
            source_run_id=source_run_id,
            parent_run_id=parent_run_id,
            reason=CONTINUATION_REASON_SOURCE_LOAD_FAILED,
        )
    task_parent = str(getattr(task, "parent_id", "") or "").strip()
    if not task_parent:
        return ContinuationSourceResolution(
            source_run_id=source_run_id,
            parent_run_id=parent_run_id,
            reason=CONTINUATION_REASON_PARENT_MISSING,
        )
    if task_parent != parent_run_id:
        # 冲突不静默猜：两侧事实都进诊断，等人工核对。
        return ContinuationSourceResolution(
            source_run_id=source_run_id,
            parent_run_id=parent_run_id,
            reason=CONTINUATION_REASON_SOURCE_CONFLICT,
            warnings=(
                f"wake_parent_agent_id={parent_run_id}",
                f"task_parent_id={task_parent}",
            ),
        )
    return ContinuationSourceResolution(
        request_id=parent_run_id,
        source_run_id=source_run_id,
        parent_run_id=parent_run_id,
    )


# LLM: 交付主链路不得被关联写入失败影响：本函数吞掉写入异常并记 warning；只有「已落盘 final 交付」才写。
#   message-tool 直投（persisted=False）不经过 canonical final，没有可指向的消息 id，不写关联。
#   成功写原请求档案的 continuation_refs；解析失败/目标缺失写 root_task_id 档案的固定原因诊断（不猜归属）。
# 函数用途: 续跑回合交付 final 后记录与原请求的结构化关联（或诊断），幂等、可重入。
def record_continuation_ref(runtime: object, delivery: ContinuationDelivery) -> None:
    if delivery.reason != _CONTINUATION_WAKE_REASON or not delivery.persisted or not delivery.message_id:
        return
    resolution = resolve_continuation_source(getattr(runtime, "agent", None), delivery.wake_signal)
    tasks = getattr(getattr(runtime, "store", None), "tasks", None)
    if tasks is None:
        return
    root_task_id = _wake_root_task_id(delivery.wake_signal)
    if not resolution.resolved:
        _append_diagnostic(tasks, root_task_id, _build_diagnostic_row(delivery, resolution))
        return
    if not _append_row(tasks, "append_continuation_ref", resolution.request_id, _build_ref_row(delivery, resolution)):
        # 目标档案不存在/写不进：落到 root 档案的固定原因诊断，不猜测归属。
        _append_diagnostic(
            tasks, root_task_id, _build_diagnostic_row(delivery, resolution, CONTINUATION_REASON_TARGET_MISSING))


# LLM: 投影只读任务档案的 continuation_refs，输出结构化指向（不复制正文）；无关联返回 None，不报错。
# 函数用途: 给管理员完整结果附「有续跑，最终回复见 …」的展示投影。
def read_continuation_projection(agent: object, request_id: str) -> dict | None:
    tasks = getattr(getattr(agent, "conversation_store", None), "tasks", None)
    if tasks is None or not str(request_id or "").strip():
        return None
    try:
        link = tasks.load(str(request_id).strip())
    except Exception:
        return None
    refs = [item for item in (getattr(link, "continuation_refs", ()) or ()) if isinstance(item, dict)]
    if not refs:
        return None
    latest = refs[-1]
    return {
        "schema_version": CONTINUATION_PROJECTION_SCHEMA,
        "ref_count": len(refs),
        "latest": {
            key: latest.get(key)
            for key in (
                "turn_id",
                "message_id",
                "content_chars",
                "delivered_at",
                "background_delivery_reason",
            )
        },
    }


# LLM: 追加失败（档案缺失/坏档）不抛给交付主链路；返回是否已落账（幂等命中视为已落账）。
# 函数用途: 调用 TaskStore 的续跑写入口，吞掉异常并报告是否成功。
def _append_row(tasks: object, method_name: str, task_id: str, row: dict) -> bool:
    if not task_id:
        return False
    method = getattr(tasks, method_name, None)
    if not callable(method):
        return False
    try:
        return method({"task_id": task_id, "row": row}) is not None
    except Exception:
        _LOGGER.warning("continuation row append failed task=%s", task_id, exc_info=True)
        return False


# LLM: 诊断目标取 wake 自带的 root_task_id（发布时写入的结构化事实）；缺失或写不进时放弃（debug 日志），
#   不引入第二套账本、不按线程猜。
# 函数用途: 把固定原因诊断写到 root 任务档案；目标不可用时静默放弃。
def _append_diagnostic(tasks: object, root_task_id: str, row: dict) -> None:
    if not root_task_id:
        return
    if not _append_row(tasks, "append_continuation_diagnostic", root_task_id, row):
        _LOGGER.debug(
            "continuation diagnostic dropped root=%s reason=%s",
            root_task_id,
            row.get("reason"),
        )


# LLM: 关联行只存结构化指向：回合/消息身份、字符数、交付时间与交付原因；不存正文、不存命令或路径。
# 函数用途: 构造一条 continuation_refs 行。
def _build_ref_row(delivery: ContinuationDelivery, resolution: ContinuationSourceResolution) -> dict:
    return {
        "schema_version": CONTINUATION_REF_SCHEMA,
        "turn_id": delivery.turn_id,
        "message_id": delivery.message_id,
        "content_chars": delivery.content_chars,
        "delivered_at": time.time(),
        "background_delivery_reason": delivery.delivery_reason,
        "source_run_id": resolution.source_run_id,
    }


# LLM: 诊断行只存固定原因码与两侧结构化事实（run id / warning 文本），不存正文；reason_override 用于目标缺失场景。
# 函数用途: 构造一条 continuation_diagnostics 行。
def _build_diagnostic_row(
    delivery: ContinuationDelivery, resolution: ContinuationSourceResolution, reason_override: str = ""
) -> dict:
    return {
        "schema_version": CONTINUATION_DIAGNOSTIC_SCHEMA,
        "turn_id": delivery.turn_id,
        "reason": reason_override or resolution.reason,
        "source_run_id": resolution.source_run_id,
        "parent_run_id": resolution.parent_run_id,
        "warnings": list(resolution.warnings),
        "at": time.time(),
    }


# 函数用途: 读 wake 信封里发布时就有的 root 任务 id；不是 dict 或缺失时返回空串。
def _wake_root_task_id(wake: object) -> str:
    if not isinstance(wake, dict):
        return ""
    return str(wake.get("root_task_id") or "").strip()


__all__ = [
    "CONTINUATION_DIAGNOSTIC_SCHEMA",
    "ContinuationDelivery",
    "CONTINUATION_PROJECTION_SCHEMA",
    "CONTINUATION_REF_SCHEMA",
    "ContinuationSourceResolution",
    "read_continuation_projection",
    "record_continuation_ref",
    "resolve_continuation_source",
]
