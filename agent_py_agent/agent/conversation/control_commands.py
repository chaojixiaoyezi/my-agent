from __future__ import annotations

"""Typed, adapter-neutral controls for an ordinary conversation task.

给人看的解释：
这里定义普通聊天里可以立即生效的控制动作。飞书、终端和以后新增的 IM
只负责传递命令，不各自猜测“停止”“纠偏”“查看状态”是什么意思。
"""

import re
from dataclasses import asdict, dataclass
from typing import Literal

from ..common.audit_activation import AUDIT_ATTR, AUDIT_WINDOW_ATTR
from .channels import redact_host_absolute_paths

ControlKind = Literal["status", "steer", "stop", "goal", "verbose", "unsupported"]
TaskCommandKind = Literal["audit"]

_STATUS_COMMAND = re.compile(r"^/status(?:\s+(.*))?$", re.IGNORECASE)
_STEER_COMMAND = re.compile(r"^/btw(?:\s+(.*))?$", re.IGNORECASE)
_STOP_COMMAND = re.compile(r"^/stop(?:\s+(.*))?$", re.IGNORECASE)
_GOAL_COMMAND = re.compile(r"^/goal(?:\s+(.*))?$", re.IGNORECASE)
_VERBOSE_COMMAND = re.compile(r"^/(?:verbose|v)(?:\s+(\S+))?\s*$", re.IGNORECASE)
_AUDIT_COMMAND = re.compile(
    r"^/audit(?:\s+(?:(\d+)\s*([dhm])(?:\s+|$))?(.*))?$",
    re.IGNORECASE | re.DOTALL,
)
_SYSTEM_SLASH = re.compile(r"^/([a-z][a-z0-9_-]*)(?:\s|$)", re.IGNORECASE)
_AUDIT_UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60}
_AUDIT_WINDOW_MAX_SECONDS = 400 * 86400


@dataclass(frozen=True)
class ConversationControlCommand:
    kind: ControlKind
    value: str = ""
    operation: str = ""
    valid: bool = True
    usage: str = ""


@dataclass(frozen=True)
class ConversationTaskCommand:
    """One explicit slash command that launches an ordinary model turn."""

    kind: TaskCommandKind
    prompt: str = ""
    attributes: dict[str, object] | None = None
    valid: bool = True
    usage: str = ""

    def to_request_payload(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "attributes": dict(self.attributes or {}),
        }


@dataclass(frozen=True)
class ConversationTaskStatus:
    state: Literal["idle", "queued", "running", "stopping"] = "idle"
    task: str = ""
    elapsed_seconds: float = 0.0
    queued_count: int = 0
    recent_progress: str = ""
    subagent_total: int = 0
    subagent_running: int = 0
    subagent_done: int = 0
    subagent_failed: int = 0
    model_name: str = ""
    compact_generation: int | None = None
    verbose_level: str = ""


@dataclass(frozen=True)
class ConversationControlResult:
    kind: ControlKind
    ok: bool
    message: str
    request_id: str = ""
    status: ConversationTaskStatus | None = None

    # LLM: HTTP/IM boundaries receive a plain typed projection, never a dataclass repr.
    # 函数用途：把控制结果转换成可以安全跨进程传输的普通字典。
    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "kind": self.kind,
            "ok": self.ok,
            "message": self.message,
            "request_id": self.request_id,
        }
        if self.status is not None:
            task_status = asdict(self.status)
            task_status["task"] = redact_host_absolute_paths(str(task_status.get("task") or ""))
            task_status["recent_progress"] = redact_host_absolute_paths(
                str(task_status.get("recent_progress") or "")
            )
            payload["task_status"] = task_status
        return payload


# LLM: Slash controls are an explicit protocol; ordinary Chinese prose never acquires authority.
# 函数用途：只识别完整斜杠命令，并把缺参数或多参数归成确定的用法错误。
def parse_conversation_control(
    text: object,
    *,
    reject_unknown_slash: bool = False,
) -> ConversationControlCommand | None:
    raw = str(text or "").strip()
    if match := _STATUS_COMMAND.fullmatch(raw):
        return _argumentless_command("status", match.group(1), "/status")
    if match := _STOP_COMMAND.fullmatch(raw):
        return _argumentless_command("stop", match.group(1), "/stop")
    if match := _STEER_COMMAND.fullmatch(raw):
        value = str(match.group(1) or "").strip()
        return ConversationControlCommand(
            "steer",
            value=value,
            valid=bool(value),
            usage="用法：/btw 你的补充要求",
        )
    if match := _GOAL_COMMAND.fullmatch(raw):
        return _goal_command(match.group(1))
    if match := _VERBOSE_COMMAND.fullmatch(raw):
        value = str(match.group(1) or "").strip().lower()
        return ConversationControlCommand(
            "verbose",
            value=value,
            operation="set" if value else "view",
            valid=not value or value in {"off", "on", "full"},
            usage="用法：/verbose off、/verbose on 或 /verbose full。",
        )
    if raw.lower().startswith("/btw"):
        return ConversationControlCommand(
            "steer",
            valid=False,
            usage="用法：/btw 你的补充要求",
        )
    if reject_unknown_slash and (name := system_slash_command_name(raw)):
        return ConversationControlCommand(
            "unsupported",
            operation=name,
            valid=False,
            usage=f"不支持的系统命令：/{name}。输入 /help 查看当前界面支持的命令。",
        )
    return None


# LLM: `/audit` is parsed once at ingress; only its opaque task body may reach the model.
# 函数用途：把保证档命令拆成普通任务正文和结构化运行属性，命令词本身不进入上下文。
def parse_conversation_task_command(text: object) -> ConversationTaskCommand | None:
    raw = str(text or "").strip()
    match = _AUDIT_COMMAND.fullmatch(raw)
    if match is None:
        return None
    count_text = str(match.group(1) or "").strip()
    unit = str(match.group(2) or "").strip().lower()
    prompt = str(match.group(3) or "").strip()
    attributes: dict[str, object] = {AUDIT_ATTR: True}
    if count_text and unit:
        seconds = int(count_text) * _AUDIT_UNIT_SECONDS[unit]
        attributes[AUDIT_WINDOW_ATTR] = min(seconds, _AUDIT_WINDOW_MAX_SECONDS)
    return ConversationTaskCommand(
        "audit",
        prompt=prompt,
        attributes=attributes,
        valid=bool(prompt),
        usage="用法：/audit [时长] 任务内容，例如 /audit 30d 逐条检查这些来源",
    )


# LLM: Slash syntax is a transport protocol marker, never a natural-language intent guess.
# 函数用途：识别位于整条消息开头的系统命令名；文件路径和正文中的斜杠不会命中。
def system_slash_command_name(text: object) -> str:
    match = _SYSTEM_SLASH.match(str(text or "").strip())
    return str(match.group(1) or "").lower() if match is not None else ""


# LLM: Runtime task attributes are projected from the typed command payload, never re-inferred from prose.
# 函数用途：校验系统任务载荷并生成允许进入 RunParams 的最小结构化属性。
def conversation_task_attributes(system_task: object) -> dict[str, object]:
    if not isinstance(system_task, dict) or system_task.get("kind") != "audit":
        return {}
    attributes: dict[str, object] = {AUDIT_ATTR: True}
    raw_attributes = system_task.get("attributes")
    if isinstance(raw_attributes, dict):
        try:
            window = int(raw_attributes.get(AUDIT_WINDOW_ATTR) or 0)
        except (TypeError, ValueError):
            window = 0
        if window > 0:
            attributes[AUDIT_WINDOW_ATTR] = min(window, _AUDIT_WINDOW_MAX_SECONDS)
    return attributes


# LLM: Parse only the explicit goal lifecycle grammar; the objective body remains opaque model/user text.
# 函数用途: 将 `/goal` 后缀解析为查看、创建、修改、暂停、恢复或清除操作。
def _goal_command(trailing: object) -> ConversationControlCommand:
    value = str(trailing or "").strip()
    if not value:
        return ConversationControlCommand("goal", operation="view")
    operation, _, remainder = value.partition(" ")
    operation = operation.lower()
    remainder = remainder.strip()
    if operation in {"pause", "resume", "clear"}:
        return ConversationControlCommand(
            "goal",
            operation=operation,
            valid=not remainder,
            usage="用法：/goal [目标] | /goal pause | /goal resume | /goal clear | /goal edit 新目标",
        )
    if operation == "edit":
        return ConversationControlCommand(
            "goal",
            value=remainder,
            operation="edit",
            valid=bool(remainder),
            usage="用法：/goal edit 新目标",
        )
    return ConversationControlCommand("goal", value=value, operation="create")


# LLM: Status/stop reject trailing prose instead of silently changing command scope.
# 函数用途：构造不接参数的控制命令；多余内容会返回明确用法。
def _argumentless_command(
    kind: Literal["status", "stop"],
    trailing: object,
    usage: str,
) -> ConversationControlCommand:
    value = str(trailing or "").strip()
    return ConversationControlCommand(kind, valid=not value, usage=f"用法：{usage}")


# LLM: Verbose acknowledgements are deterministic control-plane text, never model-authored claims.
# 函数用途：根据当前档位和已解析命令生成系统设置回执。
def render_verbose_control(current_level: str, command: ConversationControlCommand) -> str:
    if not command.valid:
        return command.usage
    level = command.value or str(current_level or "off").strip().lower()
    if not command.value:
        labels = {"off": "关闭", "on": "开启", "full": "完整"}
        return f"当前详细过程模式：{labels.get(level, '关闭')}。"
    if level == "off":
        return "详细过程已关闭。"
    if level == "on":
        return "详细过程已开启：执行任务时会发送工具步骤摘要。"
    return "完整过程已开启：除工具步骤摘要外，还会发送经过脱敏和长度限制的工具结果。"


# LLM: The interrupt registry key is stable across local and Gateway execution paths.
# 函数用途：为一轮主任务生成进程内协作中断登记名。
def conversation_request_interrupt_name(request_id: object) -> str:
    return f"conversation-request:{str(request_id or '').strip()}"


# LLM: Status is rendered only from typed runtime facts; guidance text/history is intentionally absent.
# 函数用途：把当前任务状态整理成用户能直接读懂、且不泄露内部命令和路径的文本。
def render_conversation_task_status(status: ConversationTaskStatus) -> str:
    labels = {
        "idle": "空闲",
        "queued": "排队中",
        "running": "运行中",
        "stopping": "正在停止",
    }
    lines = [f"状态：{labels.get(status.state, '空闲')}"]
    if status.task:
        lines.append(f"任务：{_short_text(redact_host_absolute_paths(status.task), 160)}")
    if status.elapsed_seconds > 0:
        lines.append(f"已运行：{_duration_text(status.elapsed_seconds)}")
    if status.recent_progress:
        lines.append(f"最近进展：{redact_host_absolute_paths(status.recent_progress)}")
    if status.state != "idle" or status.subagent_total:
        execution = "主代理 1"
        if status.subagent_total:
            execution += (
                f"；子代理 {status.subagent_total}（运行 {status.subagent_running}，"
                f"完成 {status.subagent_done}，异常 {status.subagent_failed}）"
            )
        lines.append(f"执行情况：{execution}")
    if status.queued_count:
        lines.append(f"等待中的消息：{status.queued_count}")
    if status.model_name:
        lines.append(f"模型：{status.model_name}")
    if status.compact_generation is not None:
        compact = (
            f"已压缩 {status.compact_generation} 次"
            if status.compact_generation > 0
            else "尚未压缩"
        )
        lines.append(f"上下文：{compact}")
    if status.verbose_level:
        lines.append(f"过程显示：{status.verbose_level}")
    return "\n".join(lines)


# LLM: Durations are deterministic status facts, not model-generated prose.
# 函数用途：把秒数压成紧凑的中文时长。
def _duration_text(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}小时{minutes}分{secs}秒"
    if minutes:
        return f"{minutes}分{secs}秒"
    return f"{secs}秒"


# LLM: User-visible status bounds untrusted task text before crossing an IM boundary.
# 函数用途：压平换行并限制状态里的任务摘要长度。
def _short_text(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


__all__ = [
    "ConversationControlCommand",
    "ConversationControlResult",
    "ConversationTaskCommand",
    "ConversationTaskStatus",
    "conversation_task_attributes",
    "conversation_request_interrupt_name",
    "parse_conversation_control",
    "parse_conversation_task_command",
    "render_conversation_task_status",
    "render_verbose_control",
    "system_slash_command_name",
]
