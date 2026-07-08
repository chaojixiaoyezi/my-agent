"""/audit 逐条保证档的【结构化激活】判据(低层中性:gateway/orchestration/ingestion 共用)。

真机 1.9 网关实测缺口:发 `/audit 盯这5个API` 真任务,盯守被委派给子代理做,9 分钟后所有
watch 的 audit_guarantee 全 = None——/audit 从没激活、整轮跑默认 triage(用户以为开了零丢弃、
实际没开=最危险的静默失效)。根因是激活只靠"prompt 里的 /audit 词元"检测,而:
- 子代理的 goal 字段常为空,/audit 词元落在 runner_prompt/execution_context 里,查 goal 查不到;
- 后台唤醒轮的 root_user_prompt 被回填成机器拼的整合 prompt(见 requirement_coverage_seed 注释),
  不再是用户原文 → 靠 prompt 检测在长跑子代理的每一轮都不可靠。

治法(本模块提供判据,三处接线用它):把 /audit 意图在【前台创建路 root_user_prompt 还是用户
原文时】一次性结构化盖进 task_attributes 的 audit_guarantee 键;此后跨轮/跨 spawn 树都靠这个
结构化标志继承(不再从会变的 prompt 里重新猜)。消费侧(watch 工具)读 task_attributes 的标志、
不只读 goal/prompt 词元。呼应 no_nl_judgment 铁律:激活用结构化确定性信号,不靠模型记得传参数。
"""

from __future__ import annotations

import re
from typing import Any

# task_attributes / spawn 继承里承载"本任务树在保证档"的结构化键(跨轮/跨子代理稳定)。
AUDIT_ATTR = "audit_guarantee"

# 用户显式斜杠指令的词元(同 CLI /help 的显式指令语法,非对业务内容做自然语言语义判定)。
_AUDIT_TOKEN = re.compile(r"(^|\s)/audit\b")


def text_requests_audit(text: object) -> bool:
    """一段【用户原文】里是否显式点了 /audit 斜杠指令。只在 root_user_prompt 确为用户原文的
    前台创建路上用;后台唤醒轮的机器拼 prompt 不该喂进来(用结构化标志继承,别重新词元猜)。"""
    return bool(text) and bool(_AUDIT_TOKEN.search(str(text)))


def attributes_request_audit(attrs: Any) -> bool:
    """task_attributes(或子代理 task.attributes)里是否已结构化置位保证档标志。
    这是跨轮/跨 spawn 树可靠的激活判据——一次盖上,之后每轮每个子代理都读得到。"""
    return isinstance(attrs, dict) and bool(attrs.get(AUDIT_ATTR))


__all__ = ["AUDIT_ATTR", "attributes_request_audit", "text_requests_audit"]
