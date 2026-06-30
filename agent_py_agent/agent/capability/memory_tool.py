# LLM: remember 工具——把【值得长期复用】的信息写进 owner 长期记忆(agent.memory),未来会话自动召回。
#   对标 长期助手 tools/memory_tool.py:
#   ① 这是【即时写入】:用户明确要求记→记;agent 在对话中【主动判断】值得长期复用(用户画像/稳定偏好/
#      踩过的坑/有效做法)也主动记——不必等用户明说("用户哪有空天天提示")。区别于自学习的 run 收尾复盘
#      草稿(走 learning.py,受 enable_self_learning,要审核);remember 是即时直接落库,不走草稿;
#   ② 写 owner memory(跨会话持久),不写 task-scoped;琐碎/一次性细节别记成噪音(会稀释召回);
#   ③ 写入前 scan_memory_content 注入扫描兜底。改动时同步 tests/test_memory_tool.py。
# 模块用途: 让 agent 主动把该长期记住的事记下来(不只等用户指令),自主记忆"轻档"。
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ..tooling.models import BaseTool, ToolExecutionResult, ToolSpec
from .memory_threat_scan import scan_memory_content

if TYPE_CHECKING:
    from ..core import SimpleAgent


def build_remember_spec() -> ToolSpec:
    return ToolSpec(
        name="remember",
        category="capability",
        effect="mutating",
        requires_idempotency=True,
        description=(
            "把【需要时才想起的具体事实/事件/任务知识】记进长期记忆,未来会话按相关性召回。"
            "例:'下周三交报告'、'项目叫 moneywise'、'某接口的坑'、下次同类任务能复用的做法。"
            "**注意:用户的长期人设/画像/称呼/性格/沟通偏好(如'以后叫我小王''你说话活泼点')不要用这个——"
            "那些用 update_persona 写进人格文件(每轮注入),写进 memory 不会每轮生效。**"
        ),
        use_cases=[
            "得知一个未来同类任务能直接复用的做法、或踩过的坑,主动沉淀",
            "用户提到具体的事实/日程/项目名等'需要时才想起'的信息",
            "用户明确要求'记住X'且 X 是事实/事件(不是称呼/性格/偏好那类人设)",
        ],
        avoid_when=[
            "用户长期人设/画像/称呼/性格/沟通偏好 → 用 update_persona 写人格文件(不是这个)",
            "一次性任务的临时细节(写进任务产物/工作区,不进长期记忆)",
            "不确定是否长期有用的琐碎信息——别记成噪音(会稀释召回)",
            "凭证/口令/密钥等敏感信息(本就不该长期留存)",
        ],
        keywords=["记住", "remember", "记一下", "记下", "别忘了", "事实", "日程", "项目名", "踩坑", "复用做法"],
        parameters={
            "content": "必填。要长期记住的一句话(偏好/事实/约定),具体、自包含。",
            "tags": "可选。标签列表(如 ['preference','format']),便于未来检索。",
        },
        parameter_schema={
            "content": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        required_parameters=["content"],
        examples=[
            '{"tool":"remember","content":"用户看技术简报偏好\'结论先行+要点列表\'风格","tags":["preference","format"]}',
        ],
    )


class RememberTool(BaseTool):
    # 类用途: 把"按用户指令写长期记忆"暴露成模型工具,落到 owner memory(跨会话持久)。
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_remember_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        content = str(params.get("content") or "").strip()
        if not content:
            return ToolExecutionResult(
                "remember",
                False,
                json.dumps({"error": "content 必填", "hint": "给一句具体、自包含的要记住的话"}, ensure_ascii=False),
                error_code="TOOL_INVALID_ARGUMENTS",
            )
        # 写入前威胁扫描:长期记忆跨会话持久,是注入长效攻击面(未来会话检索回来当可信
        #   上下文)。命中提示注入/凭证外泄特征即拒绝(对标 长期助手 写入前 scope 扫描)。
        #   防误伤中文:模式全锚定 ASCII 攻击语料,正常中文偏好/事实永不命中。
        scan = scan_memory_content(content)
        if not scan.safe:
            return ToolExecutionResult(
                "remember",
                False,
                json.dumps(
                    {
                        "error": scan.reason(),
                        "hint": "若确为正常长期偏好/事实,改写成不含可执行指令/凭证语义的纯描述再记;"
                                "外部网页/工具输出不要原样落库。",
                    },
                    ensure_ascii=False,
                ),
                error_code="MEMORY_INJECTION_BLOCKED",
            )
        memory = getattr(self.agent, "memory", None)
        if memory is None or not hasattr(memory, "add"):
            return ToolExecutionResult(
                "remember",
                False,
                json.dumps({"error": "长期记忆不可用"}, ensure_ascii=False),
                error_code="TOOL_UNAVAILABLE",
            )
        tags = _normalize_tags(params.get("tags"))
        try:
            memory.add("user", content, kind="preference", tags=tags)
        except Exception as exc:  # noqa: BLE001 — 任何写入异常(含首次索引时序)都要返回明确可重试码,不能逃逸成 UNKNOWN_ERROR
            return ToolExecutionResult(
                "remember",
                False,
                json.dumps({"error": f"写入失败: {exc}", "hint": "长期记忆写入异常,可原样重试一次"}, ensure_ascii=False),
                error_code="TOOL_EXECUTION_FAILED",
            )
        payload = {"ok": True, "remembered": content, "kind": "preference", "tags": tags,
                   "hint": "已写入长期记忆,未来会话可检索到。"}
        return ToolExecutionResult("remember", True, json.dumps(payload, ensure_ascii=False))


def _normalize_tags(raw: object) -> list[str]:
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    return []


__all__ = ["RememberTool", "build_remember_spec"]
