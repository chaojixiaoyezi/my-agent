"""/audit 逐条保证档的【结构化激活】判据(低层中性:gateway/orchestration/ingestion 共用)。

真机 1.9 网关实测缺口:发 `/audit 盯这5个API` 真任务,盯守被委派给子代理做,9 分钟后所有
watch 的 audit_guarantee 全 = None——/audit 从没激活、整轮跑默认 triage(用户以为开了零丢弃、
实际没开=最危险的静默失效)。根因是激活只靠"prompt 里的 /audit 词元"检测,而:
- 子代理的 goal 字段常为空,/audit 词元落在 runner_prompt/execution_context 里,查 goal 查不到;
- 后台唤醒轮的 root_user_prompt 是机器拼装的整合 prompt，并非用户原文，
  不再是用户原文 → 靠 prompt 检测在长跑子代理的每一轮都不可靠。

治法:统一系统命令入口先剥离 `/audit`，只把任务正文交给模型，并把保证档一次性盖进
task_attributes；此后跨轮/跨 spawn 树都靠这个结构化标志继承。这里仅保留消费侧共用的字段与
判据，命令语法只在 conversation/control_commands.py 定义一份。
"""

from __future__ import annotations

from typing import Any

# task_attributes / spawn 继承里承载"本任务树在保证档"的结构化键(跨轮/跨子代理稳定)。
AUDIT_ATTR = "audit_guarantee"
AUDIT_WINDOW_ATTR = "audit_window_seconds"


def attributes_request_audit(attrs: Any) -> bool:
    """task_attributes(或子代理 task.attributes)里是否已结构化置位保证档标志。
    这是跨轮/跨 spawn 树可靠的激活判据——一次盖上,之后每轮每个子代理都读得到。"""
    return isinstance(attrs, dict) and bool(attrs.get(AUDIT_ATTR))


__all__ = [
    "AUDIT_ATTR",
    "AUDIT_WINDOW_ATTR",
    "attributes_request_audit",
]
