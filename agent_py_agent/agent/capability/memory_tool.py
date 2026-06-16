# LLM: remember 工具——当用户【明确要求】记住某个长期偏好/事实/约定时,把它写进 owner 长期
#   记忆(agent.memory),未来会话能检索到。对标 长期助手 tools/memory_tool.py,但更克制:
#   ① 这是【执行用户直接指令】,不是自学习(自学习沉淀走 learning.py 草稿+确认,受
#      enable_self_learning;AGENTS.md 自学习约束只管 agent 自作主张的沉淀);
#   ② 只在用户说"记住/以后都/我喜欢/下次也这样"这类明确长期意图时用,一次性任务细节不写;
#   ③ 写 owner memory(跨会话持久),不写 task-scoped。改动时同步 tests/test_memory_tool.py。
# 模块用途: 补齐"用户让记住的事 agent 能真的记下来"这一基础能力(此前工具表缺 memory 写入)。
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ..tooling.models import BaseTool, ToolExecutionResult, ToolSpec

if TYPE_CHECKING:
    from ..core import SimpleAgent


def build_remember_spec() -> ToolSpec:
    return ToolSpec(
        name="remember",
        category="capability",
        effect="mutating",
        requires_idempotency=True,
        description=(
            "当用户明确要求记住一个【长期】偏好/事实/约定(如'记住我喜欢X''以后都Y''我的Z是W')时,"
            "把它写进长期记忆,未来会话可检索复用。这是执行用户的直接指令,不是自学习。"
        ),
        use_cases=[
            "用户说'记住我喜欢…的风格/格式',把偏好长期保存",
            "用户告诉一个稳定事实(角色、环境、约定)并希望以后都生效",
        ],
        avoid_when=[
            "一次性任务的临时细节(那写进任务产物/工作区,不进长期记忆)",
            "用户没要求记住、只是顺带提到的信息",
        ],
        keywords=["记住", "remember", "长期偏好", "以后都", "我喜欢", "保存偏好"],
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
