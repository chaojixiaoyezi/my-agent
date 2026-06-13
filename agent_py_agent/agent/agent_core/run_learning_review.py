# LLM: 主代理 run 收尾的自学习复盘钩子(稳而不管 2-3,长期助手 background_review
#   蓝本:"评审目标不是质量检查,而是学习与行为调整;多数会话至少产出一条
#   小更新")。契约:①只在 enable_self_learning=true 时运行(现有总开关,默认
#   false——零成本零打扰是默认态);②复盘=一次无工具的轻量模型调用,输入是
#   本轮 goal+closeout 结构化摘要(ok/产物数/quality_advisories 码),产出 1-3 条
#   可复用教训;③教训只进 learning drafts(复用 manager.learning 既有候选店,
#   draft→用户 my-agent learn 确认提升,绝不直接改正式 skill/记忆——AGENTS.md
#   自学习约束);④任何失败静默记日志,绝不影响 run 主链路。改动时同步检查
#   _finalization_service 接线与 tests/test_run_learning_review.py。
# 模块用途: 任务收尾后让代理"回头看一眼":这轮有什么下次能用上的经验,记成
#   待用户审核的草稿——做得不好也能学,这是自学习闭环的入口。
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

_LOGGER = logging.getLogger(__name__)

# 复盘提示:只给事实,问教训;明确"无教训就说无",防止为产出而编造。
_REVIEW_PROMPT_TEMPLATE = (
    "你刚完成一轮任务,现在做一次只为自己的复盘(用户不会看到这段对话)。\n"
    "任务目标:{goal}\n"
    "收尾事实:验收通过={ok};交付文件数={artifact_count};"
    "未尽事项码={advisory_codes}\n\n"
    "如果这轮经历里有【下次同类任务能直接用上】的可复用经验"
    "(方法、工具用法、渠道、坑),每条一行写出,最多 3 条,"
    "每条以 LESSON: 开头,具体到可执行(不写空话)。"
    "如果没有值得记的,只回答 NO_LESSON。"
)


# LLM: 钩子唯一入口。返回写入的候选数(0=关闭/无教训/失败)。副作用:一次
#   模型调用(无工具)+ learning drafts 落盘。绝不抛异常。
# 函数用途: run 结束时复盘一次,把值得记的经验存成待审草稿。
def maybe_run_learning_review(agent: Any, params: Any, closeout_report: dict | None) -> int:
    if not bool(getattr(getattr(agent, "config", None), "enable_self_learning", False)):
        return 0
    learning = getattr(getattr(agent, "subagents", None), "learning", None)
    backend = getattr(agent, "backend", None)
    if learning is None or backend is None:
        return 0
    try:
        lessons = _review_lessons(backend, params, closeout_report or {})
        if not lessons:
            return 0
        task_stub = SimpleNamespace(
            id=str(getattr(params, "run_id", "") or "main"),
            output_json="",
            task_dir="",
        )
        return len(learning.record_learning_candidates(task_stub, lessons))
    except Exception:
        _LOGGER.debug("run learning review failed", exc_info=True)
        return 0


# 函数用途: 跑一次复盘模型调用并解析 LESSON: 行(NO_LESSON/空回答=零教训)。
def _review_lessons(backend: Any, params: Any, report: dict) -> list[str]:
    advisories = [
        str(item.get("gate") or "")
        for item in (report.get("quality_advisories") or [])
        if isinstance(item, dict)
    ]
    prompt = _REVIEW_PROMPT_TEMPLATE.format(
        goal=str(getattr(params, "user_prompt", "") or "")[:600],
        ok=report.get("ok"),
        artifact_count=len(report.get("artifacts") or []),
        advisory_codes=",".join(advisories) or "无",
    )
    response = backend.generate(prompt)
    text = str(getattr(response, "text", response) or "")
    lessons = [
        line.split("LESSON:", 1)[1].strip()
        for line in text.splitlines()
        if line.strip().startswith("LESSON:") and line.split("LESSON:", 1)[1].strip()
    ]
    return lessons[:3]


__all__ = ["maybe_run_learning_review"]
