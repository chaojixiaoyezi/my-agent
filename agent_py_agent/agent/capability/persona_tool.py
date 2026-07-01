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
            "把用户的长期设定写进人格文件(每轮整文件注入)。区别于 remember(只记'需要时才想起'的具体事实/事件)。"
            "**target=user(用户画像/称呼/长期偏好)可直接写**;"
            "**target=soul(你自己的性格语气)/ agents(长期工作约定)是长期人设,不能随意自动改(会越堆越乱)——"
            "必须先在回复里明确征询用户'要不要把这条写进长期设定',用户明确同意后,才带 confirmed=true 调用。**"
        ),
        use_cases=[
            "用户说怎么称呼他 / 自我介绍身份角色 / 表达长期偏好 → target=user(直接写)",
            "确需调整你自己的性格/语气/风格 → target=soul(先问用户,同意后 confirmed=true)",
            "确需定长期工作约定/产物习惯 → target=agents(先问用户,同意后 confirmed=true)",
        ],
        avoid_when=[
            "改 soul/agents 却没先征得用户明确同意 → 会把长期人设改乱;先在回复里问,别直接写",
            "一次性临时语气(如'这次说话活泼点')→ 当场照做即可,别写进 soul",
            "只是'需要时才想起'的具体事实/事件(如'下周三交报告''项目叫X')→ 用 remember 记 memory",
        ],
        keywords=["以后叫我", "喊我", "称呼", "我是做", "人设", "画像", "性格", "语气", "长期偏好", "以后都", "写进设定"],
        parameters={
            "target": "必填。user(用户画像/称呼,可直接写)/ soul(你的性格语气)/ agents(长期工作约定)。",
            "content": "必填。要写进的一句话纯描述,如 '称呼:小王' 或 '语气偏活泼、少用正式措辞'。",
            "confirmed": "改 soul/agents 时必填=true,且只能在【先问过用户、用户明确同意】之后才置 true;改 user 不需要。",
        },
        parameter_schema={
            "target": {"type": "string", "enum": ["soul", "user", "agents"]},
            "content": {"type": "string"},
            "confirmed": {"type": "boolean"},
        },
        required_parameters=["target", "content"],
        examples=[
            '{"tool":"update_persona","target":"user","content":"称呼:小王"}',
            '{"tool":"update_persona","target":"soul","content":"语气偏活泼","confirmed":true}',
        ],
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
        # SOUL/AGENTS 是长期人设/工作约定(每轮注入、管所有行为),不能随意自动改(会越堆越乱)。
        if target in ("soul", "agents"):
            # 飞书通道:不再靠 confirmed 拒写,而是发一张交互卡片让用户按钮确认(非阻塞,工具立即返回)。
            if self._owner_provider() == "feishu":
                return self._feishu_confirm_flow(target, content)
            # 非飞书:保持现有 confirmed 闸行为不变(必须先问用户、拿到同意带 confirmed=true 才写)。
            if not bool(params.get("confirmed")):
                return _err(
                    f"{target.upper()}.md 是长期人设/工作约定,不能自动改(会越改越乱)。"
                    "请先在回复里明确问用户'要不要把这条写进长期设定',用户明确同意后,再带 confirmed=true 调用。",
                    "APPROVAL_REQUIRED",
                )
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

    def _owner_provider(self) -> str:
        """当前 agent 的 owner 通道(飞书=feishu),取自 home_paths(scoped owner 身份)。"""
        return str(getattr(getattr(self.agent, "home_paths", None), "owner_provider", "") or "").strip().lower()

    def _feishu_confirm_flow(self, target: str, content: str) -> ToolExecutionResult:
        """飞书改 SOUL/AGENTS:存一条待确认记录 + 发交互卡片,用户点『确认写入』才落写(回调侧完成)。
        非阻塞——本工具立即返回。凭据缺失/卡片发不出 → 清掉悬挂记录、回落"就地不写 + 提示用户"。"""
        scan = scan_memory_content(content)  # 卡片确认后照写,内容先过注入/外泄扫描
        if not scan.safe:
            return _err(scan.reason(), "PERSONA_INJECTION_BLOCKED", hint="人格文件每轮注入,改成纯描述再写")
        home = getattr(self.agent, "home_paths", None)
        root = getattr(home, "root", None)
        open_id = str(getattr(home, "owner_id", "") or "")
        if not root or not open_id:
            return _err("无法定位待确认存储或飞书身份", "TOOL_UNAVAILABLE")
        from ..adapter.feishu_card import build_persona_confirm_card, send_interactive_card
        from . import persona_pending

        owner = ("feishu", str(getattr(home, "owner_kind", "user") or "user"), open_id)
        token = persona_pending.add(root, owner, target, content)
        app_id, app_secret = self._feishu_creds()
        sent = send_interactive_card(app_id, app_secret, open_id, build_persona_confirm_card(token, target, content))
        if not sent:
            persona_pending.pop(root, token)  # 发不出去 → 清掉悬挂记录(用户永远收不到按钮)
            payload = {
                "ok": True, "target": target, "pending": False,
                "note": "现在没法给你发确认卡片(飞书没连上或缺凭据),这条长期设定我先没写。稍后可以再让我改。",
            }
            return ToolExecutionResult("update_persona", True, json.dumps(payload, ensure_ascii=False))
        label = "SOUL(长期人设)" if target == "soul" else "AGENTS(工作约定)"
        payload = {
            "ok": True, "target": target, "pending": True,
            "note": f"已给你发了确认卡片,点『✅ 确认写入』我才写进{label};你不点或点『❌ 不写』就不写。",
        }
        return ToolExecutionResult("update_persona", True, json.dumps(payload, ensure_ascii=False))

    def _feishu_creds(self) -> tuple[str, str]:
        """从 config 取飞书凭据并过 SecretRef 解析(值可为 env:/file: 间接引用)。取不到 → 空串,
        由 send_interactive_card fail-open 处理(回落不写)。"""
        from ..settings.secret_ref import resolve_secret_ref

        cfg = getattr(self.agent, "config", None)
        app_id = resolve_secret_ref(str(getattr(cfg, "feishu_app_id", "") or ""))
        app_secret = resolve_secret_ref(str(getattr(cfg, "feishu_app_secret", "") or ""))
        return app_id, app_secret


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
