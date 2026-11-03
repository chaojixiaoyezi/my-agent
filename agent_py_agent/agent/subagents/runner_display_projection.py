# LLM: 子代理当前活动标签只投影已裁决的状态码和失败类型；不能读模型正文，也不能改变生命周期。
# 模块用途: 把子代理的结构化运行状态转换成给 TUI 看的简短中文标签。
from __future__ import annotations

from .models import FailureType, TaskStatus

_DETAIL_BY_FAILURE = {
    FailureType.CAPABILITY_REQUEST.value: "等待父级授权",
    FailureType.PERMISSION_BLOCKED.value: "等待授权",
    FailureType.WRITE_PERMISSION_BLOCKED.value: "等待授权",
    FailureType.PROVIDER_QUOTA_EXHAUSTED.value: "额度不足",
    # 宿主停机关门后新模型调用被拒（J17 停机准入栅栏）：说清是停机打断，不笼统显示“失败”。
    FailureType.MODEL_CALL_ADMISSION_CLOSED.value: "宿主停机中断",
    # 执行器随网关停机退出、重启收尾按停机记号记的（自然停机），和被准入拒绝的同一个标签。
    FailureType.HOST_SHUTDOWN_INTERRUPTED.value: "宿主停机中断",
    FailureType.STRUCTURED_OUTPUT_PARSE_ERROR.value: "结果格式异常",
}

_LABEL_BY_STATUS = {
    TaskStatus.DONE.value: "已完成",
    TaskStatus.FAILED.value: "失败",
    TaskStatus.TIMEOUT.value: "超时",
    TaskStatus.CHANNEL_ERROR.value: "连接失败",
    TaskStatus.CANCELLED.value: "已停止",
    TaskStatus.ABANDONED.value: "已停止",
    TaskStatus.TAKEN_OVER.value: "已接管",
    TaskStatus.BLOCKED.value: "等待处理",
    TaskStatus.PAUSED.value: "已暂停",
    TaskStatus.PENDING.value: "等待继续",
    TaskStatus.PLANNING.value: "等待继续",
}


# LLM: 此函数属于只读展示投影，输入必须是宿主已裁决的 typed 字段；RUNNING 保留原活动，不得在此推断完成或失败。
# 函数用途: 返回子代理当前状态的界面标签；已提交的申请处置只改变终态标签，不授予重试权。
def runner_display_label(status: object, failure_type: object, *, capability_resolution: object = None) -> str:
    normalized_status = str(status or "").strip().upper()
    if normalized_status == TaskStatus.RUNNING.value:
        return ""
    return runner_failure_label(status, failure_type, capability_resolution=capability_resolution) or _LABEL_BY_STATUS.get(normalized_status, normalized_status)


# LLM: 失败类型专属标签的唯一出口（宿主停机中断、额度不足、等待授权等）；没有专属标签或仍在 RUNNING 时返回空串，
#   调用方保留自己按状态的默认文字。Gateway 名册行 failure_label、终态活动文字和 /status 分类都读这里，客户端不另写映射。
# 函数用途: 返回失败类型或已提交授权处置的专属界面标签；没有专属说法时返回空串。
def runner_failure_label(status: object, failure_type: object, *, capability_resolution: object = None) -> str:
    if str(status or "").strip().upper() == TaskStatus.RUNNING.value:
        return ""
    if str(status or "").strip().upper() == TaskStatus.CANCELLED.value and isinstance(capability_resolution, dict):
        label = {"parent_turn_ended": "已过期", "parent_turn_interrupted": "已过期",
                 "replaced": "已被替代", "user_denied": "已拒绝"}.get(capability_resolution.get("reason"), "")
        if label:
            return label
    return _DETAIL_BY_FAILURE.get(str(failure_type or "").strip(), "")


# LLM: 仅读取canonical attributes中的结构化处置；正文、current_step和模型文字都不能形成处置事实。
# 函数用途: 为所有展示出口提供同一授权收口事实。
def capability_request_resolution(task: object) -> dict:
    attrs = getattr(task, "attributes", {})
    closeout = attrs.get("capability_request_closeout") if isinstance(attrs, dict) else None
    resolution = closeout.get("resolution") if isinstance(closeout, dict) else None
    return dict(resolution) if isinstance(resolution, dict) else {}
