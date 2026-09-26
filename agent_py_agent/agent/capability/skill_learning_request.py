# LLM: 自学习 S3 的触发与请求材料。只看结构化事实决定是否入队：do_save、context_scope、source、tool_rounds；
#   “任务已完成”由调用方（FinalizationService）按 conversation_task_completed 判定后才调用本模块。
#   材料有界且脱敏：用户输入与最终回复各截 3000 字，工具轨迹最多 80 条、参数取模型可见参数并截 300 字，
#   本轮用过的 Skill 只认成功的 skill_search action=get 调用里的 skill_id。不读回复正文做任何机器判断。
#   同步检查 agent_core/_finalization_service.py 与 test_skill_learning*.py。
# 模块用途: 在主代理任务完成收口时，把本轮经历压成一份有界、脱敏的自动总结请求。
from __future__ import annotations

import json

from ..common.log_redaction import redact_sensitive_text, redact_sensitive_value
from .skill_learning_store import REQUEST_SCHEMA_VERSION, request_key, utc_now

TEXT_LIMIT_CHARS = 3000
TRACE_LIMIT = 80
TRACE_ARGS_CHARS = 300
USED_SKILLS_LIMIT = 10
_SKILL_TOOL = "skill_search"
_EXCLUDED_SOURCES = frozenset({"background_main_agent"})


# LLM: 全部是结构化判据；子代理（task_local）、后台唤醒/定时/Goal 续跑回合、不落盘的回合和工具轮数不足的回合都不学。
# 函数用途: 判断这次收口是否值得登记学习请求。
def learning_request_eligible(ctx: object, min_tool_rounds: int) -> bool:
    return (
        bool(getattr(ctx, "do_save", False))
        and str(getattr(ctx, "context_scope", "") or "").strip().lower() != "task_local"
        and str(getattr(ctx, "source", "") or "").strip().lower() not in _EXCLUDED_SOURCES
        and int(getattr(ctx, "tool_rounds", 0) or 0) >= max(1, int(min_tool_rounds))
    )


# LLM: 不合格或缺少运行身份时返回 None；返回的字典就是要写进队列的请求，之后只读（重试时只改 attempts）。
# 函数用途: 从收口上下文构造一条学习请求。
def build_learning_request(ctx: object, min_tool_rounds: int) -> dict[str, object] | None:
    if not learning_request_eligible(ctx, min_tool_rounds):
        return None
    identity = str(getattr(ctx, "run_id", "") or getattr(ctx, "request_id", "") or "").strip()
    if not identity:
        return None
    attrs = getattr(ctx, "task_attributes", None)
    attrs = attrs if isinstance(attrs, dict) else {}
    records = [item for item in (getattr(ctx, "archive_tool_calls", None) or []) if isinstance(item, dict)]
    return {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "request_key": request_key(identity),
        "request_id": str(getattr(ctx, "request_id", "") or ""),
        "run_id": str(getattr(ctx, "run_id", "") or ""),
        "task_id": str(getattr(ctx, "task_id", "") or ""),
        "thread_id": str(attrs.get("conversation_thread_id") or ""),
        "source": str(getattr(ctx, "source", "") or ""),
        "tool_rounds": int(getattr(ctx, "tool_rounds", 0) or 0),
        "created_at": utc_now(),
        "attempts": 0,
        "user_prompt": bounded_text(getattr(ctx, "user_prompt", ""), TEXT_LIMIT_CHARS),
        "final_response": bounded_text(getattr(getattr(ctx, "final_response", None), "text", ""), TEXT_LIMIT_CHARS),
        "tool_calls_total": len(records),
        "tool_trace": [_trace_entry(item) for item in records[:TRACE_LIMIT]],
        "used_skill_ids": used_skill_ids(records),
    }


# LLM: 先脱敏再截断，保证截断点不会把半个密钥留下；超长时保留开头并加省略标记。
# 函数用途: 把任意文本脱敏并截到上限。
def bounded_text(value: object, limit: int) -> str:
    text = redact_sensitive_text(str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# LLM: 取模型可见参数（缺失时退回原参数），整体经 redact_sensitive_value 处理，敏感字段名的值一律遮蔽。
# 函数用途: 把一条工具归档记录压成轨迹项。
def _trace_entry(record: dict[str, object]) -> dict[str, object]:
    params = record.get("model_parameters")
    if not isinstance(params, dict):
        params = record.get("parameters") if isinstance(record.get("parameters"), dict) else {}
    args = json.dumps(redact_sensitive_value(params), ensure_ascii=False, default=str, sort_keys=True)
    return {
        "round": int(record.get("tool_round") or 0),
        "tool": str(record.get("tool") or "")[:80],
        "ok": record.get("ok") is True,
        "error_code": str(record.get("error_code") or "")[:80],
        "args": bounded_text(args, TRACE_ARGS_CHARS),
    }


# LLM: 与 skill_search 的 action 默认值一致（有 skill_id 且没写 action 视为 get）；只收成功调用，保持首次出现顺序。
# 函数用途: 找出本轮成功读取过完整正文的 Skill 的 skill_id。
def used_skill_ids(records: list[dict[str, object]]) -> list[str]:
    found: list[str] = []
    for record in records:
        skill_id = _read_skill_id(record)
        if skill_id and skill_id not in found:
            found.append(skill_id)
    return found[:USED_SKILLS_LIMIT]


# LLM: 只认 tool=skill_search、ok=True、action=get 的原始参数；其它记录返回空串。
# 函数用途: 从一条工具记录取出被读取的 skill_id。
def _read_skill_id(record: dict[str, object]) -> str:
    params = record.get("parameters")
    if record.get("tool") != _SKILL_TOOL or record.get("ok") is not True or not isinstance(params, dict):
        return ""
    action = str(params.get("action") or ("get" if params.get("skill_id") else "search")).strip()
    return str(params.get("skill_id") or "").strip() if action == "get" else ""


__all__ = [
    "TEXT_LIMIT_CHARS",
    "bounded_text",
    "build_learning_request",
    "learning_request_eligible",
    "used_skill_ids",
]
