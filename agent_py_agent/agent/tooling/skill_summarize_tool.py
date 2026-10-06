# LLM: "让她总结一下"的入口 skill_summarize（只注册给本机管理员主代理，默认收起）。把那次的活连同那次召回的相关记忆
#   （自学习服务在会话任务收尾时备好）交给自学习流水线。它不写技能、不调模型，按当前这一轮的
#   结构化参数 target 分两种落点（由她按用户的话选，宿主只认这个参数，不靠"这一轮有没有升格"去猜；复审 5 轮两次）：
#   - target=current：这一轮正在做的活（必须 conversation_task_turn_active、有会话任务 id、还没完成）：按会话任务 id 记标记，
#     这个任务收尾时由自学习流水线入队（不受最少工具轮数门槛限制）；不满足就回 SKILL_SUMMARIZE_NO_CURRENT_TASK；
#   - target=previous：这个会话最近一次完成的会话任务（收尾时已按自动总结同一规则备好）当场入队；找不到就如实说找不到，
#     同一次活已按用户要求入队过就不再重复。
#   之后同一套闸门、版本、回退，/skills learned 照样管。回执写明总结的是哪一次的活。自学习开关关着如实返回
#   SKILL_SUMMARIZE_DISABLED。改字段要同步 SkillLearningService.request_learning/summarize_recent 与 test_skill_summarize_tool。
# 模块用途: 用户让 my-agent 把这次的做法总结成内部技能时，交给自学习流水线。
from __future__ import annotations

import json

from ..capability.skill_learning import FOCUS_LIMIT_CHARS
from ..capability.skill_learning_request import bounded_text
from ..conversation.authority import CONVERSATION_TASK_TURN_ACTIVE_ATTR
from ..conversation.task_state import conversation_task_completed
from ..user_space.owner_access import is_complete_local_admin_owner
from .models import (
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ToolHandlerOutcome,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)

SKILL_SUMMARIZE_TOOL = "skill_summarize"
# 回执里展示"总结的是哪一次的活"时，那次提问最多显示的字符数。
_SUMMARIZED_PROMPT_PREVIEW_CHARS = 80
# 结果怎么看、怎么删、怎么退回（给模型照抄告诉用户）。
_MANAGE_HINT = "结果用 /skills learned show 查看，不想要用 /skills learned remove <名称> 删掉，改坏了用 /skills learned revert <名称> 退回。"


# LLM: 只认本机管理员；落点只按结构化参数 target 与当前运行参数里的会话任务属性（不读运行编号、不猜）；记标记、入队都交给
#   agent.skill_learning（自学习服务）：current 只改服务内存里的标记，previous 会经服务写 requests/ 与账本。
# 类用途: 把用户要求总结的那次活交给自学习流水线。
class SkillSummarizeTool(BaseTool):
    model_spec = ToolModelSpec(
        name=SKILL_SUMMARIZE_TOOL,
        description=(
            "用户明确让你把做法或经验总结成内部技能时调用：把那次的活连同那次召回的相关记忆交给自学习流水线总结（同一套闸门、版本和回退）。"
            "target 按用户的话选：要总结这一轮正在做的活填 current，要在这一轮动手做活之后再调（做完收尾时总结；还没动手就先去做）；"
            "只有用户要的确实是刚才已经做完的那次活，才填 previous。"
            "回执写明总结的是哪一次，照它告诉用户。focus 写用户想总结的重点（可省）。"
            "只总结你自己的做法和经验，不搬外部原文；外部 agent 的方法请做成能力包。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "target": {"type": "string", "enum": ["current", "previous"],
                           "description": "current=这一轮正在做的活；previous=这个会话里刚做完的那次活"},
                "focus": {"type": "string", "description": "用户想总结的重点，一两句话"},
            },
            "required": ["target"],
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="capability",
            use_cases=("用户说“把这次的做法总结一下记下来”",),
            avoid_when=("学外部 agent 的方法（做成能力包）", "记一个具体事实（用 remember）"),
            keywords=("总结", "内部技能", "自学习"),
            default_deferred=True,
            deferred_summary="用户让你把这次的做法总结成内部技能时，交给自学习流水线",
        ),
    )
    # current 只改服务内存里的标记，previous 会经服务写学习请求与账本；按原操作账去重。
    runtime_policy = ToolRuntimePolicy(effect_resolver=EffectResolverPolicy("mutating"),
                                       idempotency_policy=IdempotencyPolicy("operation"))

    # LLM: 构造不读写任何东西；身份、自学习服务与当前运行参数都在执行时现取。
    # 函数用途: 绑定当前 owner 的 agent。
    def __init__(self, agent: object) -> None:
        self._agent = agent

    # LLM: 顺序：身份 → 自学习是否开着 → 当前这一轮的任务属性 → 按 target 分流。失败都不改任何状态；previous 成功时经服务写
    #   requests/ 与账本（有写文件副作用），current 成功时只记内存标记。
    # 函数用途: 把要总结的那次活交给自学习流水线，并告诉模型总结的是哪一次、之后怎么跟用户说。
    def execute(self, params: dict) -> ToolHandlerOutcome:
        if not is_complete_local_admin_owner(getattr(self._agent, "home_paths", None)):
            return _error("TOOL_PERMISSION_DENIED", "第一期只对本机管理员开放。")
        service = getattr(self._agent, "skill_learning", None)
        if service is None:
            return _error("SKILL_SUMMARIZE_DISABLED", "自学习开关 enable_self_learning 关着，没法总结成内部技能；"
                          "管理员可发 /settings set enable_self_learning true 打开（回执会写明什么时候生效）。")
        attrs = getattr(getattr(self._agent, "_current_run_params", None), "task_attributes", None)
        if not isinstance(attrs, dict):
            return _error("SKILL_SUMMARIZE_NO_RUN", "当前不在一次对话回合里，没法确定要总结哪一次的活。")
        target, focus = str(params.get("target") or ""), str(params.get("focus") or "").strip()[:FOCUS_LIMIT_CHARS]
        if target == "previous":
            return _recent_receipt(service, str(attrs.get("conversation_thread_id") or ""), focus)
        if target != "current":
            return _error("TOOL_INVALID_ARGUMENTS", "target 只能是 current（这一轮正在做的活）或 previous（刚做完的那次活）。")
        task_id = str(attrs.get("conversation_task_id") or "")
        if attrs.get(CONVERSATION_TASK_TURN_ACTIVE_ATTR) is not True or not task_id or conversation_task_completed(attrs):
            return _error("SKILL_SUMMARIZE_NO_CURRENT_TASK", "这一轮还没有正在做的活：要边做边总结，先动手做活，做了之后再用 current 调；"
                          "只有用户要的确实是刚才已经做完的那次活，才填 previous。")
        service.request_learning(task_id, focus)
        asked = bounded_text(getattr(self._agent, "_current_user_prompt", "") or "", _SUMMARIZED_PROMPT_PREVIEW_CHARS)
        return _ok({"mode": "this_task", "task_id": task_id, "summarizes": asked, "focus": focus,
                    "message": f"已记下：这一轮正在做的活（提问：{asked}）收尾时连同这次召回的相关记忆交给自学习流水线总结。{_MANAGE_HINT}"})


# LLM: target=previous：当场入队这个会话最近完成的那次活；找不到、队列满都如实报，不说"已记下"；已按用户要求入队过的如实说
#   不再重复。有写文件副作用（经服务）。
# 函数用途: 把这个会话刚做完的那次活交给自学习流水线并生成回执。
def _recent_receipt(service: object, thread_id: str, focus: str) -> ToolHandlerOutcome:
    outcome, request = service.summarize_recent(thread_id, focus)
    if request is None:
        return _error("SKILL_SUMMARIZE_NOTHING_RECENT", "这个会话里找不到刚做完、能总结的活（中间重启过也会找不到）；"
                      "把要总结的活做完以后再说一次。")
    if outcome == "queue_full":
        return _error("SKILL_SUMMARIZE_QUEUE_FULL", "排着等总结的活已经满了，这次没有入队；过一会儿再说一次。")
    asked = bounded_text(request.get("user_prompt"), _SUMMARIZED_PROMPT_PREVIEW_CHARS)
    lead = ("这次活已经按你的要求交给总结了（或已经总结过），不再重复" if outcome == "already_requested"
            else "已交给自学习流水线：会总结这个会话里刚做完的那次活")
    memories = request.get("recalled_memories")
    count = len(memories) if isinstance(memories, list) else 0
    return _ok({"mode": "recent_task", "task_id": str(request.get("task_id") or ""), "summarizes": asked,
                "focus": str(request.get("user_focus") or ""), "queue": outcome, "recalled_memories": count,
                "message": f"{lead}（当时的提问：{asked}；连同那次召回的 {count} 条相关记忆）。{_MANAGE_HINT}"})


# LLM: 结构化回执给模型照抄；纯组装。
# 函数用途: 生成成功回执。
def _ok(payload: dict) -> ToolHandlerOutcome:
    body = {"ok": True, **payload}
    return ToolHandlerOutcome(SKILL_SUMMARIZE_TOOL, True, json.dumps(body, ensure_ascii=False),
                              result_envelope={SKILL_SUMMARIZE_TOOL: body})


# LLM: 失败时一律什么都没改（not_started）。纯组装。
# 函数用途: 生成失败回执（什么都没改）。
def _error(code: str, message: str) -> ToolHandlerOutcome:
    return ToolHandlerOutcome(SKILL_SUMMARIZE_TOOL, False, message, error_code=code, effect_outcome="not_started")


__all__ = ["SKILL_SUMMARIZE_TOOL", "SkillSummarizeTool"]
