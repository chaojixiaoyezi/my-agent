# LLM: 会话控制保留原类型、参数语义和回执格式；词法声明读取公共目录，插件不可进入旧控制执行器。
# /experiment 与 /audit 准备轮同为任务命令：参数冻结进 system_task，只有任务正文进入模型。
# 模块用途: 将明确命令解释为会话控制或任务，供终端与 IM 共用，普通语言不获得控制权。

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Literal

from ..command_catalog import (
    match_conversation_command,
    system_slash_command_name,
    unavailable_command_message,
)
from ..plugin_commands import plugin_command_response
from ..runtime_db.operations import ATTEMPT_EFFECT_DISPOSITIONS
from .authority import (
    CONVERSATION_AUDIT_PREPARE_ATTR,
    CONVERSATION_CANCELLATION_SCOPE_ATTR,
    CONVERSATION_WORK_KIND_ATTR,
    CONVERSATION_WORK_NAME_ATTR,
)
from .channels import redact_host_absolute_paths

ControlKind = Literal[
    "status",
    "context",
    "compact",
    "effort",
    "steer",
    "stop",
    "goal",
    "audit",
    "verbose",
    "recover",
    "model",
    "restart",
    "unsupported",
]
TaskCommandKind = Literal["audit_prepare", "decision_experiment"]

_AUDIT_UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60}
_AUDIT_WINDOW_MAX_SECONDS = 400 * 86400
_WORK_NAME_MAX_CHARS = 64
DECISION_EXPERIMENT_TASK_KIND = "decision_experiment"
# 只开放有经验输入上界标定和只观察消费者的接入点；其它点即使授权也无法发送，因此在入口直接拒绝。
_EXPERIMENT_POINTS = frozenset({"skill_tool"})
# observe 只授权本轮只观察实验；apply 另外授权宿主在证据规则满足时把本会话该点改为 apply，实验调用本身仍只观察。
_EXPERIMENT_MODES = frozenset({"observe", "apply"})
_EXPERIMENT_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_EXPERIMENT_FIELDS = frozenset({"mode", "point", "duration_seconds", "max_http_requests", "max_input_tokens"})
_EXPERIMENT_USAGE = ("用法：/experiment observe|apply skill_tool 时长 HTTP次数 输入token上限 任务内容，"
                     "例如 /experiment observe skill_tool 10m 1 50000 整理本周资料。apply 另外授权：最近证据满足规则时，"
                     "宿主自动把本会话 skill_tool 改为 apply（实验本身仍只观察，用户后改优先，reset 恢复继承）。"
                     "输入上界是经验值，不是供应商保证。")


@dataclass(frozen=True)
class ConversationControlCommand:
    kind: ControlKind
    value: str = ""
    operation: str = ""
    name: str = ""
    duration_seconds: int | None = None
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


ConversationCommand = ConversationControlCommand | ConversationTaskCommand


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
    durable_work: tuple[NamedConversationWorkStatus, ...] = ()


@dataclass(frozen=True)
class NamedConversationWorkStatus:
    kind: Literal["audit", "goal"]
    name: str
    status: str
    elapsed_seconds: float = 0.0


# LLM: Control responses keep task status, transport delivery, and typed error identity separate;
# ``ok`` reports command semantics while error_code distinguishes interrupt/busy/failure without
# parsing the localized display message.
# 类用途: 表示一条会话控制命令的结构化结果，并在跨进程边界保留投递三态和错误码。
@dataclass(frozen=True)
class ConversationControlResult:
    kind: ControlKind
    ok: bool
    message: str
    request_id: str = ""
    status: ConversationTaskStatus | None = None
    delivery_status: Literal["", "accepted", "rejected", "unknown"] = ""
    guidance_dedupe_key: str = ""
    operation_id: str = ""
    control_state: str = ""
    error_code: str = ""

    # LLM: HTTP/IM boundaries receive a plain typed projection, never a dataclass repr.
    # 函数用途：把控制结果转换成可以安全跨进程传输的普通字典。
    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "kind": self.kind,
            "ok": self.ok,
            "message": self.message,
            "request_id": self.request_id,
        }
        if self.delivery_status:
            payload["delivery_status"] = self.delivery_status
        if self.guidance_dedupe_key:
            payload["guidance_dedupe_key"] = self.guidance_dedupe_key
        if self.operation_id:
            payload["operation_id"] = self.operation_id
        if self.control_state:
            payload["control_state"] = self.control_state
        if self.error_code:
            payload["error_code"] = self.error_code
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
    command = parse_conversation_command(text, reject_unknown_slash=reject_unknown_slash)
    return command if isinstance(command, ConversationControlCommand) else None


# LLM: 名称和正文读取公共声明；插件实际入口另行消费其只读回执，此控制解析器仍拒绝插件，不能执行旧 stop 分支。
# 函数用途: 区分即时控制与模型任务，保留暂停目标、中断本轮和停止资源三种语义；/recover 只解析结构化处置值，/model 只解析编号，
# /restart 的尾随文字只作为展示用原因。
def parse_conversation_command(
    text: object,
    *,
    reject_unknown_slash: bool = False,
) -> ConversationCommand | None:
    raw = str(text or "").strip()
    name, trailing = match_conversation_command(raw) or ("", None)
    if name == "status":
        return _argumentless_command("status", trailing, "/status")
    if name == "context":
        return _argumentless_command("context", trailing, "/context")
    if name == "compact":
        instructions = str(trailing or "").strip()
        return ConversationControlCommand(
            "compact",
            value=instructions,
            operation="run",
            usage="用法：/compact [可选的摘要要求]",
        )
    if name == "effort":
        value = str(trailing or "").strip().lower()
        if value in {"current", "status"}:
            value = ""
        return ConversationControlCommand(
            "effort",
            value=value,
            operation="set" if value and value != "help" else value or "view",
            valid=not value or value in {"low", "medium", "high", "max", "auto", "help"},
            usage="用法：/effort [low|medium|high|max|auto]",
        )
    if name == "stop":
        return _argumentless_command("stop", trailing, "/stop")
    if name == "interrupt":
        return ConversationControlCommand(
            "stop", operation="interrupt", valid=not str(trailing or "").strip(),
            usage="用法：/interrupt（中断本轮；活动 Goal 仍会继续）",
        )
    if name == "btw":
        value = str(trailing or "").strip()
        return ConversationControlCommand(
            "steer",
            value=value,
            valid=bool(value),
            usage="用法：/btw 你的补充要求",
        )
    if name == "goal":
        return _goal_command(trailing)
    if name == "recover":
        return _recover_command(trailing)
    if name == "restart":
        return ConversationControlCommand("restart", value=str(trailing or "").strip()[:200], operation="apply")
    if name == "model":
        return _model_command(trailing)
    if name == "audit":
        return _audit_command(raw)
    if name == "experiment":
        return _experiment_command(trailing)
    if name == "verbose":
        value = str(trailing or "").strip().lower()
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
    if raw.lower().startswith("/effort"):
        return ConversationControlCommand(
            "effort",
            valid=False,
            usage="用法：/effort [low|medium|high|max|auto]",
        )
    if reject_unknown_slash and (name := system_slash_command_name(raw)):
        plugin_result = plugin_command_response(raw)
        return ConversationControlCommand(
            "unsupported",
            operation=name,
            valid=False,
            usage=str(plugin_result["message"]) if plugin_result else unavailable_command_message(name),
        )
    return None


# LLM: `/audit` is parsed once at ingress; only its opaque task body may reach the model.
# 函数用途：把保证档命令拆成普通任务正文和结构化运行属性，命令词本身不进入上下文。
def parse_conversation_task_command(text: object) -> ConversationTaskCommand | None:
    command = parse_conversation_command(text)
    return command if isinstance(command, ConversationTaskCommand) else None


# LLM: Runtime task attributes are projected from the typed command payload, never re-inferred from prose.
# 函数用途：校验系统任务载荷并生成允许进入 RunParams 的最小结构化属性。
def conversation_task_attributes(system_task: object) -> dict[str, object]:
    if not isinstance(system_task, dict):
        return {}
    kind = str(system_task.get("kind") or "").strip()
    if kind != "audit_prepare":
        return {}
    raw_attributes = system_task.get("attributes")
    raw_attributes = raw_attributes if isinstance(raw_attributes, dict) else {}
    name = str(raw_attributes.get(CONVERSATION_WORK_NAME_ATTR) or "").strip()
    if not name or len(name) > _WORK_NAME_MAX_CHARS:
        return {}
    return {
        CONVERSATION_AUDIT_PREPARE_ATTR: True,
        CONVERSATION_WORK_KIND_ATTR: "audit",
        CONVERSATION_WORK_NAME_ATTR: name,
        CONVERSATION_CANCELLATION_SCOPE_ATTR: "foreground",
    }


# LLM: 只认显式 /experiment 语法；参数冻结进 system_task，模型只看到任务正文，不能从正文或模型参数取得授权。
# 函数用途: 把实验命令拆成任务正文和结构化实验参数，格式或数值不合法时返回用法。
def _experiment_command(trailing: object) -> ConversationTaskCommand:
    match = re.match(r"^(\S+)\s+(\S+)\s+(\d{1,9})([smhd])\s+(\d{1,9})\s+(\d{1,12})\s+(.+)$",
                     str(trailing or "").strip(), re.IGNORECASE | re.DOTALL)
    if match is None:
        return ConversationTaskCommand(DECISION_EXPERIMENT_TASK_KIND, valid=False, usage=_EXPERIMENT_USAGE)
    mode, point, amount, unit, http_requests, input_tokens, prompt = match.groups()
    attributes = {"mode": mode.lower(), "point": point,
                  "duration_seconds": int(amount) * _EXPERIMENT_UNIT_SECONDS[unit.lower()],
                  "max_http_requests": int(http_requests), "max_input_tokens": int(input_tokens)}
    valid = decision_experiment_task({"kind": DECISION_EXPERIMENT_TASK_KIND, "attributes": attributes}) is not None
    return ConversationTaskCommand(DECISION_EXPERIMENT_TASK_KIND, prompt=prompt.strip(), attributes=attributes,
                                   valid=valid and bool(prompt.strip()), usage=_EXPERIMENT_USAGE)


# LLM: 宿主授权只消费这里严格校验后的冻结参数；缺字段、多字段、非 observe/apply、未标定点或非正整数一律视为无实验。
# 函数用途: 从排队请求的 system_task 读取实验参数，供 Gateway 在模型前授权使用。
def decision_experiment_task(system_task: object) -> dict[str, object] | None:
    if not isinstance(system_task, dict) or system_task.get("kind") != DECISION_EXPERIMENT_TASK_KIND:
        return None
    attributes = system_task.get("attributes")
    if not isinstance(attributes, dict) or set(attributes) != _EXPERIMENT_FIELDS:
        return None
    if (type(attributes["mode"]) is not str or attributes["mode"] not in _EXPERIMENT_MODES
            or type(attributes["point"]) is not str or attributes["point"] not in _EXPERIMENT_POINTS):
        return None
    counts = (attributes["duration_seconds"], attributes["max_http_requests"], attributes["max_input_tokens"])
    if any(type(value) is not int or value <= 0 for value in counts):
        return None
    return dict(attributes)


# LLM: Parse only the explicit goal lifecycle grammar; the objective body remains opaque model/user text.
# 函数用途: 将 `/goal` 后缀解析为查看、创建、修改、暂停、恢复或清除操作。
# LLM: 显式 slash command 才有控制权；名称或 goal ID 只作为结构化选择器，不从目标正文猜状态。
# 函数用途: 解析持续目标的建立与精确暂停、恢复、清除命令，未命名目标仍可用简写。
def _goal_command(trailing: object) -> ConversationControlCommand:
    value = str(trailing or "").strip()
    if not value:
        return ConversationControlCommand("goal", operation="view")
    duration_seconds, remainder = _duration_prefix(value)
    if duration_seconds is not None:
        name, separator, objective = remainder.partition(" ")
        return ConversationControlCommand(
            "goal",
            value=objective.strip(),
            operation="create",
            name=name.strip(),
            duration_seconds=duration_seconds,
            valid=bool(
                name.strip()
                and len(name.strip()) <= _WORK_NAME_MAX_CHARS
                and separator
                and objective.strip()
            ),
            usage="用法：/goal 时长 名称 任务内容，例如 /goal 7d 周报整理 整理本周资料",
        )
    name, separator, action = value.partition(" ")
    if separator and action.strip().casefold() in {"pause", "resume", "clear"}:
        return ConversationControlCommand(
            "goal",
            operation=action.strip().casefold(),
            name=name.strip(),
            valid=bool(name.strip()),
            usage="用法：/goal 名称或目标编号 pause|resume|clear",
        )
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


_MODEL_USAGE = "用法：/model 查看可选模型；/model <编号> 选为本会话模型；/model default <编号> 设为新会话默认。"


# LLM: 文字形式只做查看、会话选择和默认值三件事，目标是列表编号或精确配置编号；新增/密钥永远不走聊天。
# 函数用途: 把 `/model`、`/model <编号>`、`/model default <编号>` 解析成结构化模型控制。
def _model_command(trailing: object) -> ConversationControlCommand:
    value = str(trailing or "").strip()
    if not value:
        return ConversationControlCommand("model", operation="view", usage=_MODEL_USAGE)
    head, _, rest = value.partition(" ")
    if head.lower() == "default":
        target = rest.strip()
        return ConversationControlCommand(
            "model", value=target, operation="set_default",
            valid=bool(target) and len(target.split()) == 1, usage=_MODEL_USAGE,
        )
    return ConversationControlCommand(
        "model", value=value, operation="select", valid=len(value.split()) == 1, usage=_MODEL_USAGE,
    )


# LLM: 处置值只认 runtime_db 的结构化取值表，不接受同义词或正文；无参数只读查看，带参数才会改运行库。
# 函数用途: 把 `/recover` 解析为查看，或把 `/recover <处置>` 解析为显式恢复请求。
def _recover_command(trailing: object) -> ConversationControlCommand:
    value = str(trailing or "").strip().lower()
    return ConversationControlCommand(
        "recover",
        value=value,
        operation="apply" if value else "view",
        valid=not value or value in ATTEMPT_EFFECT_DISPOSITIONS,
        usage="用法：/recover 查看未确认的操作；核对后用 /recover " + "|".join(ATTEMPT_EFFECT_DISPOSITIONS) + " 解除阻塞。",
    )


def _audit_command(raw: str) -> ConversationCommand:
    usage = (
        "用法：/audit help；/audit 名称 prepare 内容；/audit 时长 名称 任务内容；"
        "/audit 名称 status；/audit 名称 resume；/audit 名称 clear"
    )
    trailing = raw[len("/audit") :].strip()
    if not trailing:
        return ConversationControlCommand("audit", valid=False, usage=usage)
    first, remainder = _split_head(trailing)
    separator = bool(remainder)
    if first.lower() == "help" and not remainder.strip():
        return ConversationControlCommand("audit", operation="help")

    duration_seconds, after_duration = _duration_prefix(trailing)
    if duration_seconds is not None:
        name, prompt = _split_head(after_duration)
        valid = bool(name and prompt and len(name) <= _WORK_NAME_MAX_CHARS)
        return ConversationControlCommand(
            "audit",
            value=prompt,
            operation="start",
            name=name,
            duration_seconds=duration_seconds,
            valid=valid,
            usage=(
                f"Audit 名称不能超过 {_WORK_NAME_MAX_CHARS} 个字符。"
                if len(name) > _WORK_NAME_MAX_CHARS
                else usage
            ),
        )

    name = first.strip()
    action, prompt = _split_head(remainder)
    lowered_action = action.lower()
    if separator and lowered_action in {"status", "clear", "resume"} and not prompt.strip():
        return ConversationControlCommand(
            "audit",
            operation=lowered_action,
            name=name,
            valid=bool(name and len(name) <= _WORK_NAME_MAX_CHARS),
            usage=usage,
        )
    if separator and lowered_action == "prepare":
        prompt = prompt.strip()
        return ConversationTaskCommand(
            "audit_prepare",
            prompt=prompt,
            attributes={
                CONVERSATION_AUDIT_PREPARE_ATTR: True,
                CONVERSATION_WORK_KIND_ATTR: "audit",
                CONVERSATION_WORK_NAME_ATTR: name,
                CONVERSATION_CANCELLATION_SCOPE_ATTR: "foreground",
            },
            valid=bool(
                name
                and len(name) <= _WORK_NAME_MAX_CHARS
                and prompt
            ),
            usage=(
                f"Audit 名称不能超过 {_WORK_NAME_MAX_CHARS} 个字符。"
                if len(name) > _WORK_NAME_MAX_CHARS
                else usage
            ),
        )
    return ConversationControlCommand("audit", valid=False, usage=usage)


def _duration_prefix(value: str) -> tuple[int | None, str]:
    match = re.match(r"^(\d+)\s*([dhm])(?:\s+|$)(.*)$", value, re.IGNORECASE | re.DOTALL)
    if match is None:
        return None, value
    seconds = int(match.group(1)) * _AUDIT_UNIT_SECONDS[match.group(2).lower()]
    return min(seconds, _AUDIT_WINDOW_MAX_SECONDS), str(match.group(3) or "").strip()


def _split_head(value: str) -> tuple[str, str]:
    parts = str(value or "").strip().split(None, 1)
    return (parts[0], parts[1].strip() if len(parts) == 2 else "") if parts else ("", "")


# LLM: Status/stop reject trailing prose instead of silently changing command scope.
# 函数用途：构造不接参数的控制命令；多余内容会返回明确用法。
def _argumentless_command(
    kind: Literal["status", "context", "stop"],
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


# LLM: Manual Compact cancellation is scoped by authenticated channel identity, conversation, and
# the original opaque client message id.  The digest is process-local routing data, never a user
# or owner lookup, and prevents one TUI from naming another user's Compact worker.
# 函数用途：为一条手动 Compact 控制操作生成不泄露用户标识的进程内中断登记名。
def conversation_compact_interrupt_name(
    user_id: object,
    channel: object,
    conversation_id: object,
    client_message_id: object,
) -> str:
    identity = json.dumps(
        {
            "user_id": str(user_id or "").strip(),
            "channel": str(channel or "").strip(),
            "conversation_id": str(conversation_id or "").strip(),
            "client_message_id": str(client_message_id or "").strip(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "conversation-compact:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


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
    audits = [item for item in status.durable_work if item.kind == "audit"]
    goals = [item for item in status.durable_work if item.kind == "goal"]
    if audits:
        lines.append("Audit：")
        lines.extend(
            f"- {item.name}｜{_duration_text(item.elapsed_seconds)}｜{item.status}"
            for item in audits
        )
    if goals:
        lines.append("Goal：")
        lines.extend(
            f"- {item.name}｜{_duration_text(item.elapsed_seconds)}｜{item.status}"
            for item in goals
        )
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
    "DECISION_EXPERIMENT_TASK_KIND",
    "ConversationControlCommand",
    "ConversationControlResult",
    "ConversationTaskCommand",
    "ConversationTaskStatus",
    "NamedConversationWorkStatus",
    "conversation_task_attributes",
    "decision_experiment_task",
    "conversation_compact_interrupt_name",
    "conversation_request_interrupt_name",
    "parse_conversation_control",
    "parse_conversation_command",
    "parse_conversation_task_command",
    "render_conversation_task_status",
    "render_verbose_control",
]
