# LLM: /wakes 只在已认证 scope 解析出的本机管理员 owner 上执行，不开放给模型。列表与预览只读；只有 replay confirm 才写：
#   经 store.wakes.attempts.replay 在结案锁里按原 ID 和冻结内容写回 pending、结案记录移入 replayed 留档、尝试账清零。
#   能否重放的来源状态只读 attempts.replay_source（已归档、读不出、不存在各有码）；领域终态只用毒丸第 3 步 C4 的
#   conversation/wake_domain_closeout.wake_domain_status / wake_domain_terminal（唯一判定，读账异常照常上抛），这里只把裸状态拼成提示。
#   成功重放打一行 [background-wake-poison] wake_replayed 事件（只有结构化字段）。错误码都登记在 ERROR_CONTRACTS。
#   原因码是开放集合：已知码与已知前缀带中文短说明，认不出的只显示原码，不因不在表里拒绝。
#   改动须同步 test_wake_ops_control.py 与 CLI_REFERENCE.md 的 /wakes 说明。
# 模块用途: 让管理员在 TUI 或飞书里查看反复失败、已结案不再领取的后台唤醒，并在核对后把其中一条放回队列重新执行。
from __future__ import annotations

import json
import time
from functools import partial
from typing import Any

from ..conversation.control_commands import ConversationControlCommand, ConversationControlResult
from ..conversation.models import SESSION_TASK_WAKE_REASON, WakeSignal
from ..conversation.store_wake_attempts import (
    WAKE_REPLAY_ARCHIVED,
    WAKE_REPLAY_DOMAIN_TERMINAL,
    WAKE_REPLAY_NOT_FOUND,
    WAKE_REPLAY_PENDING_CONFLICT,
    WAKE_REPLAY_SOURCE_UNREADABLE,
)
from ..conversation.wake_domain_closeout import wake_domain_status, wake_domain_terminal
from ..conversation.wake_poison import (
    WAKE_REASON_ATTEMPT_ABANDONED,
    WAKE_REASON_CHANNEL_UNAVAILABLE,
    WAKE_REASON_DELIVERY_NOT_COMMITTED,
    WAKE_REASON_LEDGER_CORRUPT,
    WAKE_REASON_RUN_NO_REPORT,
    WAKE_REASON_WAKE_NOT_SETTLED,
)
from ..user_space.approval_mode import is_permission_admin

_KIND = "wakes"
_QUARANTINED_LIST_LIMIT = 30
_ADMIN_ONLY = "WAKE_OPS_ADMIN_ONLY"
_REASON_TEXT = {
    WAKE_REASON_RUN_NO_REPORT: "执行结束但没有报告",
    WAKE_REASON_DELIVERY_NOT_COMMITTED: "回复没能交付",
    WAKE_REASON_WAKE_NOT_SETTLED: "执行完唤醒仍未确认",
    WAKE_REASON_ATTEMPT_ABANDONED: "执行中途进程消失",
    WAKE_REASON_CHANNEL_UNAVAILABLE: "通知重投 24 小时仍失败",
    WAKE_REASON_LEDGER_CORRUPT: "尝试账读不出",
}
_PREFIX_TEXT = (("admission:", "领到租约但没执行"), ("error:", "执行时出错"))
_REFUSALS = {
    WAKE_REPLAY_ARCHIVED: "唤醒 {id} 的结案记录已超过 14 天移进归档，不能再重放；需要的话请重新发起这件事。",
    WAKE_REPLAY_SOURCE_UNREADABLE: "唤醒 {id} 的信封或结案记录读不出（原字节留在 quarantine/unreadable/ 或结案目录），不能重放。",
    WAKE_REPLAY_NOT_FOUND: "没有 ID 为 {id} 的已结案唤醒；用 /wakes 查看列表。",
    WAKE_REPLAY_PENDING_CONFLICT: "待处理队列里已经有 ID 为 {id} 的唤醒，没有做任何改动。",
    WAKE_REPLAY_DOMAIN_TERMINAL: "唤醒 {id} 对应的事已经结束（{domain}），重放没有意义；需要的话请重新派活。",
}


# LLM: owner_agent 由 control_service 按已认证 scope 解析；只有 operation=apply 且来源与领域核对都通过才写，其余只读。
#   管理员判定只认 home_paths 的结构化 owner 身份。
# 函数用途: 执行 /wakes：列出已结案的后台唤醒、预览一条的重放，或在管理员确认后重放它。
def execute_wake_ops_control(owner_agent: object, command: ConversationControlCommand) -> ConversationControlResult:
    if not is_permission_admin(getattr(owner_agent, "home_paths", None)):
        return ConversationControlResult(_KIND, False, "只有管理员可以查看或重放已结案的后台唤醒。", error_code=_ADMIN_ONLY)
    store = owner_agent.conversation_store
    if command.operation == "list":
        return ConversationControlResult(_KIND, True, _render_list(store))
    code, record = store.wakes.attempts.replay_source(command.value)
    signal = _recorded_signal(record) if not code else None
    code = code or _replay_blocker(store, signal)
    if code:
        return _refusal(code, command.value, store, signal)
    if command.operation != "apply":
        return ConversationControlResult(_KIND, True, _render_plan(signal, record))
    return _replay(store, signal, record)


# LLM: 唯一写入口：store 在结案锁内重新核对来源、pending 冲突和领域终态，任一不满足就不改文件并返回对应码；
#   成功后才打 wake_replayed 事件。重放次数取结案记录里此前的 replay_count 加一，只作展示。
# 函数用途: 把一条已核实可重放的结案唤醒放回待处理队列，并打运维事件（写唤醒队列、留档与尝试账）。
def _replay(store: object, signal: WakeSignal, record: dict[str, Any]) -> ConversationControlResult:
    result = store.wakes.attempts.replay(
        signal.wake_signal_id, now=time.time(), domain_terminal=partial(wake_domain_terminal, store),
    )
    if not result.ok:
        return _refusal(result.error_code, signal.wake_signal_id, store, result.signal or signal)
    replay_number = _replay_count(record) + 1
    _log_replayed(signal, replay_number)
    return ConversationControlResult(
        _KIND, True,
        f"已重放唤醒 {signal.wake_signal_id}（第 {replay_number} 次重放）：按原内容放回待处理队列，失败计数已清零，"
        "调度器下一轮会领取它。如果还是同一原因失败，满 5 次会再次结案，不会自己复活。",
    )


# LLM: 预览与确认前的同一组核对（来源已由 replay_source 判过）：待处理队列已有同 ID 唤醒、或领域已结束，都不能重放。
# 函数用途: 给出一条已结案唤醒现在不能重放的原因码，能重放时返回空串。
def _replay_blocker(store: object, signal: WakeSignal) -> str:
    if store.wakes.pending_one(signal.wake_signal_id) is not None:
        return WAKE_REPLAY_PENDING_CONFLICT
    return WAKE_REPLAY_DOMAIN_TERMINAL if wake_domain_terminal(store, signal) else ""


# LLM: 状态只取 wake_domain_status 的裸状态（任务 done/failed/cancelled、回执 consumed/rejected，未结束为空串），这里只负责
#   拼成给管理员看的文字；reason 与 C4 一样按 strip().lower() 比较。读取异常照常上抛，由控制入口兜底。
# 函数用途: 把一条唤醒对应的会话任务或会话消息的结束状态写成提示文字，未结束返回空串。
def _domain_text(store: object, signal: WakeSignal) -> str:
    status = wake_domain_status(store, signal)
    if not status:
        return ""
    noun = "会话任务" if str(signal.reason or "").strip().lower() == SESSION_TASK_WAKE_REASON else "会话消息回执"
    return f"{noun}已是 {status}"


# LLM: 模板按码取，认不出的码给通用句并带原码；领域终态时重读一次领域状态写进提示。只读，不改文件。
# 函数用途: 把一个重放拒绝码转成给管理员看的结果，领域终态时带上具体状态。
def _refusal(code: str, wake_signal_id: str, store: object, signal: WakeSignal | None) -> ConversationControlResult:
    domain = _domain_text(store, signal) if code == WAKE_REPLAY_DOMAIN_TERMINAL and signal is not None else ""
    text = _REFUSALS.get(code, "唤醒 {id} 暂时不能重放（{code}），没有做任何改动。")
    return ConversationControlResult(_KIND, False, text.format(id=wake_signal_id, domain=domain, code=code), error_code=code)


# LLM: 只展示结构化字段（ID、会话、reason、原因码、次数、时间、重放次数），不展示摘要、metadata 或错误消息。
# 函数用途: 生成重放预览：这条唤醒是什么、为什么结案、确认后会发生什么。
def _render_plan(signal: WakeSignal, record: dict[str, Any]) -> str:
    facts = record.get("quarantine") if isinstance(record.get("quarantine"), dict) else {}
    decision = facts.get("decision") if isinstance(facts.get("decision"), dict) else {}
    return "\n".join((
        f"唤醒 {signal.wake_signal_id}｜会话 {signal.thread_id}｜{signal.reason}",
        f"结案原因：{_reason_text(str(decision.get('reason_code') or ''))}；{_counts_text(decision)}；"
        f"结案于 {_clock(_number(facts.get('quarantined_at')))}；此前重放 {_replay_count(record)} 次。",
        "确认后：按原 ID 和原内容放回待处理队列，失败计数清零，这次的结案记录留档到 replayed/。"
        "如果还是同一原因失败，满 5 次会再次结案，不会自己复活。",
        f"确认请输入 /wakes replay {signal.wake_signal_id} confirm",
    ))


# LLM: 行来自 attempts.quarantined()（已排除归档与重放留档），按结案时间倒序最多列 _QUARANTINED_LIST_LIMIT 条；读不出的只报条数与位置。
# 函数用途: 生成已结案唤醒的列表文字。
def _render_list(store: object) -> str:
    rows, errors = store.wakes.attempts.quarantined()
    if not rows and not errors:
        return "没有已结案的后台唤醒。"
    rows = sorted(rows, key=lambda row: _number(row.get("quarantined_at")), reverse=True)
    lines = [f"已结案的后台唤醒 {len(rows)} 条（反复失败后不再自动领取；超过 14 天的会移进归档）："]
    lines.extend(_row_line(row) for row in rows[:_QUARANTINED_LIST_LIMIT])
    if len(rows) > _QUARANTINED_LIST_LIMIT:
        lines.append(f"……另有 {len(rows) - _QUARANTINED_LIST_LIMIT} 条没有列出。")
    if errors:
        lines.append(f"另有 {len(errors)} 条读不出（信封或结案记录损坏），原字节留在 quarantine/unreadable/ 或结案目录，不能重放。")
    if rows:
        lines.append("用 /wakes replay <唤醒ID> 预览重放。")
    return "\n".join(lines)


# LLM: 只用 attempts.quarantined() 投影出来的结构化字段，不读摘要或 metadata。
# 函数用途: 把列表里的一条结案记录格式化成一行。
def _row_line(row: dict[str, Any]) -> str:
    return (f"- {row.get('wake_signal_id')}｜会话 {row.get('thread_id')}｜{row.get('reason')}｜"
            f"{_reason_text(str(row.get('reason_code') or ''))}｜{_counts_text(row)}｜"
            f"结案 {_clock(_number(row.get('quarantined_at')))}｜已重放 {row.get('replay_count') or 0} 次")


# LLM: 开放集合：已知码带中文说明，已知前缀（admission:、error:）带类别说明，其余只显示原码。
# 函数用途: 把结案原因码显示成「原码（中文说明）」。
def _reason_text(code: str) -> str:
    if not code:
        return "原因未知"
    note = _REASON_TEXT.get(code) or next((text for prefix, text in _PREFIX_TEXT if code.startswith(prefix)), "")
    return f"{code}（{note}）" if note else code


# LLM: 次数字段坏值按 0 显示，mixed_causes 只认布尔 True。
# 函数用途: 把同因次数、总次数与是否多种原因显示成一段文字。
def _counts_text(facts: dict[str, Any]) -> str:
    text = f"同因 {_number(facts.get('same_cause_count')):.0f} 次、共 {_number(facts.get('total_count')):.0f} 次"
    return text + "（多种原因）" if facts.get("mixed_causes") is True else text


# LLM: 结案记录 = 原信封字段 + status/handled_at + 顶层 quarantine 键；还原成信号时去掉 quarantine 键、状态按 pending 看
#   （只用于判定和展示，不写回）。
# 函数用途: 从结案记录还原出原唤醒信号。
def _recorded_signal(record: dict[str, Any]) -> WakeSignal:
    payload = {key: value for key, value in record.items() if key != "quarantine"}
    return WakeSignal.from_dict({**payload, "status": "pending", "handled_at": 0.0})


# LLM: 与 store_wake_attempts 读 replay_count 同一口径：只认非负整数，布尔和其它形状按 0；只作展示。
# 函数用途: 读出结案记录里此前的重放次数（非负整数，其它形状按 0）。
def _replay_count(record: dict[str, Any]) -> int:
    facts = record.get("quarantine") if isinstance(record.get("quarantine"), dict) else {}
    value = facts.get("replay_count")
    return value if type(value) is int and value >= 0 else 0


# LLM: 布尔、非数值和非正数都按 0，只用于排序和显示，不参与任何判定。
# 函数用途: 把记录里的数值字段读成非负浮点数，坏值按 0。
def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value) if value > 0 else 0.0


# LLM: 纯格式化，只用本机时区显示，不参与任何判断。
# 函数用途: 把时间戳显示成本地「月-日 时:分」，缺失时显示「时间未知」。
def _clock(timestamp: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(timestamp)) if timestamp > 0 else "时间未知"


# LLM: 与毒丸其它事件同一格式族（前缀 + 排序 JSON + flush），只有结构化字段；打印失败（如磁盘写满）不影响重放结果。
# 函数用途: 打一行 wake_replayed 运维事件。
def _log_replayed(signal: WakeSignal, replay_number: int) -> None:
    body = json.dumps({"event": "wake_replayed", "wake_signal_id": signal.wake_signal_id, "thread_id": signal.thread_id,
                       "reason": signal.reason, "replay_count": replay_number}, ensure_ascii=False, sort_keys=True)
    try:
        print(f"[background-wake-poison] {body}", flush=True)
    except Exception:  # noqa: BLE001 - 日志写不出不能让已完成的重放报失败
        return


__all__ = ["execute_wake_ops_control"]
