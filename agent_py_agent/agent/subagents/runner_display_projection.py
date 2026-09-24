# LLM: 子代理当前活动标签只投影已裁决的状态码和失败类型；不能读模型正文，也不能改变生命周期。
# 模块用途: 把子代理的结构化运行状态转换成给 TUI 看的简短中文标签。
from __future__ import annotations

from .models import FailureType, TaskStatus

_DETAIL_BY_FAILURE = {
    FailureType.CAPABILITY_REQUEST.value: "等待父级授权",
    FailureType.PERMISSION_BLOCKED.value: "等待授权",
    FailureType.WRITE_PERMISSION_BLOCKED.value: "等待授权",
    FailureType.PROVIDER_QUOTA_EXHAUSTED.value: "额度不足",
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
# 函数用途: 返回子代理当前状态的界面标签；空串表示保留正在显示的活动信息。
def runner_display_label(status: object, failure_type: object) -> str:
    normalized_status = str(status or "").strip().upper()
    if normalized_status == TaskStatus.RUNNING.value:
        return ""
    detail = _DETAIL_BY_FAILURE.get(str(failure_type or "").strip(), "")
    return detail or _LABEL_BY_STATUS.get(normalized_status, normalized_status)
