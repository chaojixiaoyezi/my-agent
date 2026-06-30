# LLM: update_persona 工具——把用户的【长期人设/画像/工作约定】写进对应人格文件(SOUL/USER/AGENTS.md),
#   这三件每轮整文件注入系统上下文、真正塑造每次交互。区别于 remember(记"需要时才想起"的具体事实/事件到
#   长期记忆,按相关性召回)。用户表达"以后叫我X/我是做Y的/你说话别太正式"这类长期设定时用本工具,不用 remember。
#   写入前 scan_memory_content 注入扫描(人格文件每轮注入=注入长效面)。改动时同步 tests/test_persona_tool.py。
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from ..tooling.models import BaseTool, ToolExecutionResult, ToolSpec
from .memory_threat_scan import scan_memory_content

if TYPE_CHECKING:
    from ..core import SimpleAgent

_TARGET_ATTR = {"soul": "owner_soul_md", "user": "owner_user_md", "agents": "owner_agents_md"}


def build_update_persona_spec() -> ToolSpec:
    return ToolSpec(
        name="update_persona",
        category="capability",
        effect="mutating",
        requires_idempotency=True,
        description=(
            "把用户的【长期人设/画像/工作约定】写进对应人格文件(每轮整文件注入、真正塑造每次交互)。"
            "用户说'以后叫我X'、'我是做Y的'、'你说话别太正式/带 emoji'这类关于他自己或你该怎么表现的长期设定时,"
            "用这个直接写进文件——**不要用 remember**(remember 只记'需要时才想起'的具体事实/事件)。"
        ),
        use_cases=[
            "用户说怎么称呼他 / 自我介绍身份角色 / 表达长期偏好 → target=user",
            "用户要调你的性格/语气/风格 → target=soul",
            "用户定长期工作约定/产物习惯 → target=agents",
        ],
        avoid_when=[
            "只是'需要时才想起'的具体事实/事件/任务知识(如'下周三交报告''项目叫X')→ 用 remember 记 memory",
            "一次性临时细节 → 写任务产物,不进人格文件",
        ],
        keywords=["以后叫我", "喊我", "称呼", "我是做", "你说话", "别太正式", "带emoji", "人设", "画像", "性格", "语气", "长期偏好", "以后都"],
        parameters={
            "target": "必填。soul(你的人格/语气)/ user(用户画像/称呼)/ agents(工作约定)之一。",
            "content": "必填。要写进的一句话纯描述,如 '称呼:小王' 或 '语气偏活泼、少用正式措辞'。",
        },
        parameter_schema={
            "target": {"type": "string", "enum": ["soul", "user", "agents"]},
            "content": {"type": "string"},
        },
        required_parameters=["target", "content"],
        examples=['{"tool":"update_persona","target":"user","content":"称呼:小王"}'],
    )


class UpdatePersonaTool(BaseTool):
    # 类用途: 把"更新用户人设/画像/工作约定"暴露成模型工具,落到 owner 的 SOUL/USER/AGENTS.md(每轮注入)。
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_update_persona_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        target = str(params.get("target") or "").strip().lower()
        content = str(params.get("content") or "").strip()
        if target not in _TARGET_ATTR or not content:
            return _err("target 须为 soul/user/agents,content 必填", "TOOL_INVALID_ARGUMENTS")
        scan = scan_memory_content(content)  # 人格文件每轮注入,写入前过注入/外泄扫描
        if not scan.safe:
            return _err(scan.reason(), "PERSONA_INJECTION_BLOCKED", hint="人格文件每轮注入,改成纯描述再写")
        path = getattr(getattr(self.agent, "home_paths", None), _TARGET_ATTR[target], None)
        if not path:
            return _err("人格文件路径不可用", "TOOL_UNAVAILABLE")
        written = _append_persona_line(Path(path), content)
        if written is None:
            return _err("写入失败", "TOOL_EXECUTION_FAILED")
        payload = {"ok": True, "target": target, "written": content, "hint": "已写进人格文件,下一轮起长期生效。"}
        return ToolExecutionResult("update_persona", True, json.dumps(payload, ensure_ascii=False))


def _append_persona_line(path: Path, content: str) -> str | None:
    """把一行纯描述追加到人格文件(已存在同句则幂等跳过)。失败返回 None。"""
    try:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if content not in existing:
            sep = "" if (not existing or existing.endswith("\n")) else "\n"
            path.write_text(f"{existing}{sep}- {content}\n", encoding="utf-8")
        return content
    except OSError:
        return None


def _err(msg: str, code: str, hint: str = "") -> ToolExecutionResult:
    body = {"error": msg}
    if hint:
        body["hint"] = hint
    return ToolExecutionResult("update_persona", False, json.dumps(body, ensure_ascii=False), error_code=code)


__all__ = ["UpdatePersonaTool", "build_update_persona_spec"]
