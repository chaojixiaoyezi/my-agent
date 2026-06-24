# LLM: update_persona 工具——让【只能用飞书/QQ 等通道聊天的普通用户】也能定制自己 agent 的身份与画像。
#   普通用户没有文件/CLI 访问,只能对话。当用户在聊天里明确表达"给你/我自己改设定"时(改名字、定语气、
#   说自己是谁、定回复规矩),由 agent 调本工具把它写进【该用户 owner 作用域】下的 SOUL/USER/AGENTS.md。
#   这三份文件每轮都会被 PromptBuilder 读进系统上下文,所以改完【下一条消息即生效】。
# 模块用途: 补齐"通道用户经对话自助定制 per-user 人格/画像"这一多用户基础能力(此前只能改文件,普通用户够不到)。
#   与 remember 的分工:remember=按需检索的长期记忆(零散事实/偏好);本工具=每轮常驻的结构化身份三件套。
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from ..tooling.models import BaseTool, ToolExecutionResult, ToolSpec
from .memory_threat_scan import scan_memory_content

if TYPE_CHECKING:
    from ..core import SimpleAgent

# target -> (home_paths 属性, 展示名, set 时的文件标题)
_TARGETS: dict[str, tuple[str, str, str]] = {
    "user": ("owner_user_md", "USER.md", "# USER"),
    "agent": ("owner_agents_md", "AGENTS.md", "# AGENTS"),
    "soul": ("owner_soul_md", "SOUL.md", "# SOUL"),
}
_ACTIONS = ("get", "append", "set")


def build_update_persona_spec() -> ToolSpec:
    return ToolSpec(
        name="update_persona",
        category="capability",
        effect="mutating",
        requires_idempotency=False,
        requires_approval=False,
        description=(
            "定制【当前用户自己的】agent 身份与画像三件套(每轮常驻系统上下文,改完下一条消息即生效)。"
            "通道用户(飞书/QQ)只能聊天、改不了文件,所以当用户在对话里明确要求改设定时,由你调本工具落库:"
            "target=soul 改 agent 自身(名字/语气/人设),target=user 记用户画像(身份/角色/称呼/长期偏好),"
            "target=agent 定协作规矩(回复语言/格式/边界)。action=set 整体替换(改名/重写),append 追加一条,get 读现状。"
        ),
        use_cases=[
            "用户说'你以后叫小美、说话活泼点' → target=soul, action=set",
            "用户说'我是做跨境电商财务的、叫我老张' → target=user, action=append",
            "用户说'以后回复都用中文、先给结论再展开' → target=agent, action=append",
            "想知道当前设定是什么 → action=get",
        ],
        avoid_when=[
            "只是一次性任务细节(写任务产物,不进人格文件)",
            "零散的长期事实/偏好且无需每轮常驻 → 用 remember 写长期记忆更合适",
            "用户没要求改设定、只是顺带提到",
        ],
        keywords=[
            "人设", "改名", "叫你", "你以后", "语气", "性格", "persona", "soul", "画像", "我是", "叫我",
            "称呼", "回复规矩", "用中文", "设定", "身份", "AGENTS", "USER", "定制",
        ],
        parameters={
            "target": "必填。改哪份:'soul'(agent 自身人设)/'user'(用户画像)/'agent'(协作规矩)。",
            "action": "可选,默认 append。'get' 读现状 / 'append' 追加一条 / 'set' 整体替换(改名、重写人设用 set)。",
            "content": "append/set 必填。要写入的内容,具体、自包含、用陈述句。",
        },
        parameter_schema={
            "target": {"type": "string", "enum": list(_TARGETS)},
            "action": {"type": "string", "enum": list(_ACTIONS)},
            "content": {"type": "string"},
        },
        required_parameters=["target"],
        examples=[
            '{"tool":"update_persona","target":"soul","action":"set","content":"你叫小美,语气活泼亲切,多用口语。"}',
            '{"tool":"update_persona","target":"user","action":"append","content":"用户是跨境电商财务,偏好称呼\'老张\'。"}',
            '{"tool":"update_persona","target":"agent","action":"get"}',
        ],
    )


class UpdatePersonaTool(BaseTool):
    # 类用途: 把"经对话自助定制 per-user SOUL/USER/AGENTS"暴露成模型工具,落到 owner 作用域(每用户隔离)。
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_update_persona_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        target = str(params.get("target") or "").strip().lower()
        if target not in _TARGETS:
            return self._fail("TOOL_INVALID_ARGUMENTS",
                              f"target 必须是 {list(_TARGETS)} 之一", hint="soul=agent自身 / user=用户画像 / agent=协作规矩")
        action = str(params.get("action") or "append").strip().lower()
        if action not in _ACTIONS:
            return self._fail("TOOL_INVALID_ARGUMENTS", f"action 必须是 {list(_ACTIONS)} 之一")

        attr, label, header = _TARGETS[target]
        path = self._resolve_path(attr)
        if path is None:
            return self._fail("TOOL_UNAVAILABLE", "当前没有可写的 owner 主目录(home 未就绪)")

        if action == "get":
            return self._ok({"target": target, "file": label, "path": str(path),
                             "content": _read(path), "hint": "这是当前设定;要改用 action=append/set。"})

        content = str(params.get("content") or "").strip()
        if not content:
            return self._fail("TOOL_INVALID_ARGUMENTS", "append/set 时 content 必填", hint="给一句具体、自包含的陈述句")
        # 人格三件套每轮常驻系统上下文,是注入长效攻击面(尤其 AGENTS/SOUL 直接改 agent 行为)。
        #   写入前做与 remember 同款威胁扫描:命中提示注入/凭证外泄即拒。系统级安全纪律在系统提示词里,
        #   不在这三份文件,所以用户/注入改不动"破坏操作先确认"等底线。中文画像/人设永不误命中(模式锚 ASCII)。
        scan = scan_memory_content(content)
        if not scan.safe:
            return self._fail("PERSONA_INJECTION_BLOCKED", scan.reason(),
                              hint="若确为正常人设/画像,改写成不含可执行指令/凭证语义的纯描述再写。")

        try:
            new_text = self._write(path, header, content, action)
        except OSError as exc:
            return self._fail("TOOL_EXECUTION_FAILED", f"写入失败: {exc}", hint="路径/权限异常,可重试一次")
        return self._ok({"ok": True, "target": target, "file": label, "path": str(path), "action": action,
                         "preview": new_text[:600],
                         "hint": f"已更新 {label}(owner 作用域,仅影响当前用户的 agent);下一条消息起生效。"})

    def _resolve_path(self, attr: str) -> Path | None:
        raw = getattr(getattr(self.agent, "home_paths", None), attr, "") or ""
        return Path(raw) if raw else None

    @staticmethod
    def _write(path: Path, header: str, content: str, action: str) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        if action == "set":
            new_text = f"{header}\n\n{content}\n"
        else:  # append
            existing = _read(path)
            base = existing if existing.strip() else header + "\n"
            new_text = base.rstrip("\n") + "\n" + content + "\n"
        path.write_text(new_text, encoding="utf-8")
        return new_text

    def _ok(self, payload: dict) -> ToolExecutionResult:
        return ToolExecutionResult(self.spec.name, True, json.dumps(payload, ensure_ascii=False))

    def _fail(self, code: str, error: str, hint: str | None = None) -> ToolExecutionResult:
        body: dict[str, object] = {"error": error}
        if hint:
            body["hint"] = hint
        return ToolExecutionResult(self.spec.name, False, json.dumps(body, ensure_ascii=False), error_code=code)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


__all__ = ["UpdatePersonaTool", "build_update_persona_spec"]
