# LLM: 只接收晋升模块刚成功持久化的回执；原请求 promotion 是唯一幂等账，旧回执不重发、不新增通知账或 IM 通道。
#   提示沿 pending_host_notices、Gateway host_notice 流、canonical final 和原 IM DeliveryService 送达；文案不进模型上下文。
# 模块用途: 用结构化晋升事实告诉用户本会话改了什么、凭什么改、怎样恢复继承。
from __future__ import annotations

from dataclasses import replace

from ..conversation.host_notices import HostNotice, host_notice, queue_host_notice
from .request_experiment import EXPERIMENT_GRANT_KEY

_MODE_LABELS = {"off": "关闭（off）", "observe": "仅观察（observe）", "apply": "正式使用（apply）"}
_SCOPE_LABELS = {"thread": "本会话", "owner": "长期设置"}


# LLM: 只读回执的有效模式、证据计数与当时冻结的规则，不读取当前设置、模型回答或自然语言原因来猜晋升依据。
# 函数用途: 把这次晋升的前后变化和证据门槛写成简短中文。
def _promotion_text(receipt: dict) -> str:
    before, after = receipt["before"]["effective_mode"], receipt["after"]["effective_mode"]
    evidence, field = receipt["evaluation"], receipt["field"]
    rule = evidence["rule"]
    return (f"决策实验已自动晋升：{receipt['point']}（{_SCOPE_LABELS[receipt['scope']]}）"
            f"{_MODE_LABELS[before]}→{_MODE_LABELS[after]}。"
            f"依据：样本窗口 {evidence['sample_count']}/{rule['window_samples']}，"
            f"可比较样本 {evidence['comparable_count']}（门槛 ≥{rule['min_comparable_samples']}），"
            f"召回率门槛 {rule['required_recall']:.0%}。"
            f"撤销设置：/model → 选择模型 → 决策模型 → 本会话临时设置 → 逐字段恢复继承 → {field}；"
            "也可让我恢复本会话该字段的继承。撤销实验授权不会回滚已晋升设置。")


# LLM: 非 applied、旧版缺唯一编号或规则的回执不生成提示，避免把不确定写入说成成功；notice_id 直接复用 promotion_id。
# 函数用途: 从本次新晋升回执生成原宿主提示对象，重复送达时保留稳定身份。
def promotion_host_notice(receipt: dict | None) -> HostNotice | None:
    if not isinstance(receipt, dict) or receipt.get("status") != "applied" or not receipt.get("promotion_id"):
        return None
    if not isinstance(receipt.get("evaluation", {}).get("rule"), dict):
        return None
    notice = host_notice("decision_experiment", "promotion_applied", _promotion_text(receipt), details={
        "promotion_id": receipt["promotion_id"], "point": receipt["point"], "field": receipt["field"],
        "scope": receipt["scope"],
    })
    return replace(notice, notice_id=receipt["promotion_id"])


# LLM: 调用方只能传原 promotion 写入器返回的新回执，不能从历史回执补投；队列写失败不改变业务结果，也不启第二条恢复通道。
# 函数用途: 把本轮新晋升排入同会话原提示队列，并返回当轮需要一起发布与提交的提示。
def queue_promotion_notice(context: object, receipt: dict | None) -> tuple[HostNotice, ...]:
    notice = promotion_host_notice(receipt)
    if notice is None:
        return ()
    store = getattr(context.agent, "conversation_store", None)
    thread_id = context.request[EXPERIMENT_GRANT_KEY]["thread_id"]
    return (notice,) if queue_host_notice(store, thread_id, notice, replace_same_code=True) else ()


__all__ = ["promotion_host_notice", "queue_promotion_notice"]
