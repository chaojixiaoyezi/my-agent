# LLM: 会话控制保留原类型、参数语义和回执格式；插件只保留公共命名空间和原输入，参数与执行归共享插件服务。
# /experiment 与 /audit 准备轮同为任务命令：参数冻结进 system_task，只有任务正文进入模型。
# /admin、/approve 的密码只在 command.value 中，任何持久化之前经 persisted_control_command_text 脱敏。
# 模块用途: 将明确命令解释为会话控制或任务，供终端与 IM 共用，普通语言不获得控制权。

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Literal

from ..command_catalog import (
    match_conversation_command,
    system_slash_command_name,
    unavailable_command_message,
)
from ..plugin_commands import plugin_namespace
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
    "endtask",
    "wakes",
    "admin",
    "approve",
    "deny",
    "skills",
    "settings",
    "plugins",
    "unsupported",
]
TaskCommandKind = Literal["audit_prepare", "decision_experiment"]

_AUDIT_UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60}
# 审计窗口最大 400 天（秒）：防止误传超大窗口拖垮查询。
_AUDIT_WINDOW_MAX_SECONDS = 400 * 86400
# 任务工作名最多 64 字符：限制命令入参长度。
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
    # 本 owner 已结案、不再自动领取的后台唤醒条数（唤醒毒丸结案，不含已归档的）；0 时 /status 不显示这一行。
    quarantined_wakes: int = 0


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


# LLM: 名称和正文读取公共声明；插件仅以原文本进入专用控制分派，不能在这里重写参数、执行 stop 或退入模型。
#   /admin、/approve 的参数是密码，只进 command.value，调用方持久化前必须经 persisted_control_command_text 脱敏。
# 函数用途: 区分即时控制与模型任务，保留暂停目标、中断本轮和停止资源三种语义；/recover 只解析结构化处置值，
# /endtask 只解析任务 ID 与 confirm，/wakes 只解析 quarantined、replay、唤醒 ID 与 confirm，/model 只解析编号，
# /restart 的尾随文字只作为展示用原因。
def parse_conversation_command(
    text: object,
    *,
    reject_unknown_slash: bool = False,
) -> ConversationCommand | None:
    raw = str(text or "").strip()
    if plugin_namespace(raw) is not None:
        return ConversationControlCommand("plugins", value=raw)
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
        return _effort_command(trailing)
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
    parser = _TRAILING_PARSERS.get(name)
    if parser is not None:
        return parser(trailing)
    if name == "restart":
        return ConversationControlCommand("restart", value=str(trailing or "").strip()[:200], operation="apply")
    if name == "approve":
        password = str(trailing or "").strip()
        return ConversationControlCommand(
            "approve", value=password, operation="approve", valid=bool(password), usage=_APPROVE_USAGE,
        )
    if name == "deny":
        return ConversationControlCommand(
            "deny", operation="deny", valid=not str(trailing or "").strip(), usage=_DENY_USAGE,
        )
    if name == "audit":
        return _audit_command(raw)
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
        return ConversationControlCommand("effort", valid=False, usage=_EFFORT_USAGE)
    if reject_unknown_slash and (name := system_slash_command_name(raw)):
        return ConversationControlCommand(
            "unsupported",
            operation=name,
            valid=False,
            usage=unavailable_command_message(name),
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
_EFFORT_USAGE = ("用法：/effort [auto|off|low|medium|high|max|default]；/effort probe 检测当前模型是否支持调节；"
                 "/effort revert <编号> 撤销检测写入的档案修改。")
_EFFORT_WORDS = frozenset({"auto", "off", "low", "medium", "high", "max", "default", "help", "probe"})


# LLM: 档位取值与 backends/reasoning_control.REASONING_LEVELS 一致；default 清除本会话设置；probe 手动检测当前模型
#   （settings/reasoning_probe）；revert 只接一个至少 6 位的字母数字编号。current/status 视同查看。
# 函数用途: 把 `/effort` 的各种写法解析成结构化控制（查看、设置、帮助、检测、撤销）。
def _effort_command(trailing: object) -> ConversationControlCommand:
    value = str(trailing or "").strip().lower()
    value = "" if value in {"current", "status"} else value
    words = value.split()
    if words[:1] == ["revert"]:
        change_id = " ".join(words[1:])  # 多于一个词时含空格，校验不通过
        return ConversationControlCommand("effort", value=change_id, operation="revert",
                                          valid=len(change_id) >= 6 and change_id.isalnum(), usage=_EFFORT_USAGE)
    operation = value if value in {"help", "probe"} else "set" if value else "view"
    return ConversationControlCommand("effort", value=value, operation=operation,
                                      valid=not value or value in _EFFORT_WORDS, usage=_EFFORT_USAGE)


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


_SKILLS_USAGE = (
    "用法：/skills 查看待确认的技能提案与自动总结的 Skill；/skills proposals [all] 列出提案；"
    "/skills show <提案编号>；/skills confirm <提案编号> <版本>；/skills reject <提案编号> <版本>；"
    "/skills learned 列出自动总结的 Skill；/skills learned show|revert|remove <名称>。"
)
# 提案编号是 24 位十六进制，聊天里允许用至少 6 位的前缀；自动总结 Skill 的名字沿用生成合同的安全名字规则。
_SKILL_PROPOSAL_REF = re.compile(r"[0-9a-f]{6,24}")
_LEARNED_SKILL_NAME = re.compile(r"[a-z0-9][a-z0-9-]{1,62}[a-z0-9]")
_SKILLS_SIMPLE = {"": "overview", "help": "help", "proposals": "proposals", "learned": "learned"}


# LLM: 只做词法解析：子命令、提案编号前缀（十六进制）、版本号（正整数）与 Skill 名（安全名字）都在这里拒绝式校验，
#   不合规时 valid=False 并给出用法，绝不把任意文字当成编号或路径交给服务。value 保存规范化后的参数，TUI 据此还原命令文本。
# 函数用途: 把 `/skills …` 解析成结构化的技能提案/自动 Skill 控制。
def _skills_command(trailing: object) -> ConversationControlCommand:
    # 参数统一小写：提案编号是十六进制、Skill 名按合同只含小写，子命令大小写不敏感。
    tokens = str(trailing or "").casefold().split()
    head, rest = (tokens[0] if tokens else ""), tokens[1:]
    if head in _SKILLS_SIMPLE and not (head == "learned" and rest):
        valid = not rest or (head == "proposals" and rest == ["all"])
        return ConversationControlCommand("skills", value=" ".join(tokens), operation=_SKILLS_SIMPLE[head],
                                          valid=valid, usage=_SKILLS_USAGE)
    if head in {"show", "confirm", "reject"}:
        ref_ok = bool(rest) and bool(_SKILL_PROPOSAL_REF.fullmatch(rest[0]))
        shape_ok = len(rest) == 1 if head == "show" else len(rest) == 2 and rest[1].isdigit() and int(rest[1]) > 0
        return ConversationControlCommand("skills", value=" ".join(tokens), operation=head,
                                          valid=ref_ok and shape_ok, usage=_SKILLS_USAGE)
    action = rest[0] if head == "learned" and rest else ""
    known = action in {"show", "revert", "remove"}
    valid = known and len(rest) == 2 and bool(_LEARNED_SKILL_NAME.fullmatch(rest[1]))
    return ConversationControlCommand("skills", value=" ".join(tokens), operation=f"learned_{action}" if known else "unknown",
                                      valid=valid, usage=_SKILLS_USAGE)


_SETTINGS_USAGE = (
    "用法：/settings 查看常用参数；/settings all 查看全部参数与改过的项；/settings search <关键词> 找参数；"
    "/settings show <参数名> 看说明与当前值；"
    "/settings set <参数名> <值> 修改；/settings reset <参数名> 恢复默认；/settings history [参数名] 看修改记录；"
    "/settings revert <记录编号> 回滚一次修改；/settings internal <关键词> 查代码里的常数（只读，改动需改代码）。"
    "只有管理员可用，修改在重启 Gateway 后生效（发 /restart）。"
)
# 参数名沿用 AgentConfig 字段命名；修改记录编号是 12 位十六进制，聊天里允许用至少 6 位前缀。
_SETTING_KEY = re.compile(r"[a-z][a-z0-9_]{1,79}")
_SETTING_CHANGE_REF = re.compile(r"[0-9a-f]{6,12}")
# 设置值字符串最多 500 字符：防止超长注入。
_SETTING_VALUE_MAX_CHARS = 500


# LLM: 只做词法解析：子命令大小写不敏感，参数名与记录编号小写后拒绝式校验；set 的值保留原样（可含空格与中文），
#   但不许换行、不超过 500 字。不合规时 valid=False 并给出用法。value 保存规范化后的完整参数，TUI 据此还原命令文本。
#   不带参数是常用视图（overview），all 是全部参数视图，二者都不接受多余参数。
# 函数用途: 把 `/settings …` 解析成结构化的参数查看/修改控制。
def _settings_command(trailing: object) -> ConversationControlCommand:
    text = str(trailing or "").strip()
    head, _, rest = text.partition(" ")
    head, rest = head.casefold(), rest.strip()
    if head in {"", "help", "all"}:
        return ConversationControlCommand("settings", value=head, operation=head or "overview", valid=not rest,
                                          usage=_SETTINGS_USAGE)
    if head == "set":
        key, _, value = rest.partition(" ")
        key, value = key.casefold(), value.strip()
        valid = bool(_SETTING_KEY.fullmatch(key)) and 0 < len(value) <= _SETTING_VALUE_MAX_CHARS and "\n" not in value
        return ConversationControlCommand("settings", value=f"set {key} {value}", operation="set", valid=valid,
                                          usage=_SETTINGS_USAGE)
    argument = rest.casefold() if head != "search" else rest
    checks = {
        "search": lambda: 0 < len(argument) <= 80 and "\n" not in argument,
        "internal": lambda: 0 < len(argument) <= 80 and "\n" not in argument,
        "show": lambda: bool(_SETTING_KEY.fullmatch(argument)),
        "reset": lambda: bool(_SETTING_KEY.fullmatch(argument)),
        "history": lambda: not argument or bool(_SETTING_KEY.fullmatch(argument)),
        "revert": lambda: bool(_SETTING_CHANGE_REF.fullmatch(argument)),
    }
    check = checks.get(head)
    return ConversationControlCommand("settings", value=f"{head} {argument}".strip(),
                                      operation=head if check else "unknown", valid=bool(check and check()),
                                      usage=_SETTINGS_USAGE)


_ADMIN_USAGE = "用法：/admin <管理员密码> 在 IM 私聊中验证管理员身份；/admin status 查看；/admin logout 解除。"
_APPROVE_USAGE = "用法：/approve <管理员密码> 批准本会话当前唯一等待确认的操作（仅本次）；拒绝请发 /deny。"
_DENY_USAGE = "用法：/deny（拒绝本会话当前唯一等待确认的操作，不需要密码）"
_REDACTED_SECRET = "******"


# LLM: status/logout 是精确子命令，其余整段正文都当作一次性密码放进 command.value，只在内存中供 Gateway 校验一次；
#   密码最短 8 位，不会与两个子命令同名。解析器不校验密码对错，也不记录它。
# 函数用途: 把 `/admin <密码>`、`/admin status`、`/admin logout` 解析成结构化管理员身份控制。
def _admin_command(trailing: object) -> ConversationControlCommand:
    value = str(trailing or "").strip()
    operation = value.casefold() if value.casefold() in {"status", "logout"} else "login"
    return ConversationControlCommand(
        "admin",
        value=value if operation == "login" else "",
        operation=operation,
        valid=bool(value),
        usage=_ADMIN_USAGE,
    )


# LLM: 只看结构化 kind/operation：/approve 与 /admin 登录的 value 是密码；status/logout/deny 不带秘密。
# 函数用途: 判断一条控制命令的参数是否是管理员密码。
def control_command_carries_secret(command: ConversationControlCommand) -> bool:
    return command.kind == "approve" or (command.kind == "admin" and command.operation == "login")


# LLM: 管理员密码只在内存里的 command.value 中用于一次校验；控制回执、摘要和日志落盘前必须改用这里的脱敏正文。
#   脱敏正文仍能被同一解析器还原成相同 kind（回执读回校验依赖这一点）；其余命令原样返回。
# 函数用途: 生成可以安全持久化的控制命令正文，例如把 `/admin 密码` 变成 `/admin ******`。
def persisted_control_command_text(command: ConversationControlCommand, text: object) -> str:
    if control_command_carries_secret(command):
        return f"/{command.kind} {_REDACTED_SECRET}"
    return str(text or "")


_ENDTASK_USAGE = ("用法：/endtask 列出卡在等待中的定时会话任务；/endtask <任务ID> 预览结束会做什么；"
                  "/endtask <任务ID> confirm 确认结束（仅管理员）。")
_ENDTASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")


# LLM: 只认一个任务 ID 和可选的 confirm 词，不接受正文或其它参数；无参数是列候选，只给 ID 是只读预览，带 confirm 才是结束请求。
#   任务 ID 只做字符集校验，是不是定时执行由执行端按定时账本等结构化事实判断。
# 函数用途: 把 `/endtask`、`/endtask <任务ID>`、`/endtask <任务ID> confirm` 解析为列表、预览或结束请求。
def _endtask_command(trailing: object) -> ConversationControlCommand:
    parts = str(trailing or "").split()
    if not parts:
        return ConversationControlCommand("endtask", operation="list", usage=_ENDTASK_USAGE)
    confirm = [part.lower() for part in parts[1:]] == ["confirm"]
    return ConversationControlCommand(
        "endtask",
        value=parts[0],
        operation="apply" if confirm else "view",
        valid=bool(_ENDTASK_ID.fullmatch(parts[0])) and (len(parts) == 1 or confirm),
        usage=_ENDTASK_USAGE,
    )


_WAKES_USAGE = ("用法：/wakes 或 /wakes quarantined 列出已结案的后台唤醒；/wakes replay <唤醒ID> 预览重放会做什么；"
                "/wakes replay <唤醒ID> confirm 确认重放（仅管理员）。")


# LLM: 只认固定子命令词和一个唤醒 ID，不接受正文：无参数或 quarantined 是列表，replay <ID> 是只读预览，
#   replay <ID> confirm 才是重放请求。唤醒 ID 只做字符集校验（与 /endtask 同一字符集），是否存在、能否重放由执行端按结案记录判断。
# 函数用途: 把 `/wakes`、`/wakes quarantined`、`/wakes replay <ID>`、`/wakes replay <ID> confirm` 解析为列表、预览或重放请求。
def _wakes_command(trailing: object) -> ConversationControlCommand:
    parts = str(trailing or "").split()
    if not parts or [part.lower() for part in parts] == ["quarantined"]:
        return ConversationControlCommand("wakes", operation="list", usage=_WAKES_USAGE)
    if parts[0].lower() != "replay" or len(parts) < 2:
        return ConversationControlCommand("wakes", operation="list", valid=False, usage=_WAKES_USAGE)
    confirm = [part.lower() for part in parts[2:]] == ["confirm"]
    return ConversationControlCommand(
        "wakes",
        value=parts[1],
        operation="apply" if confirm else "view",
        valid=bool(_ENDTASK_ID.fullmatch(parts[1])) and (len(parts) == 2 or confirm),
        usage=_WAKES_USAGE,
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


# LLM: 会话间派活片的可中断名必须与前台请求的 `conversation-request:{id}` **不可能撞名**：
#   前缀带独立的 session-task 标记，名字只由**这条派活片**注册（按认领时写下的会话任务绑定）。
#   停止控制沿"会话任务 → 绑定的 request → 这个名字"去找；它不碰 task_id 语义。
# 函数用途: 生成派活片专用的可中断名（与前台请求名不同空间）。
def session_task_interrupt_name(turn_id: object) -> str:
    return f"session-task-turn:{str(turn_id or '').strip()}"


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
    if status.quarantined_wakes:
        lines.append(f"已结案的后台唤醒：{status.quarantined_wakes} 条（反复失败、不再自动领取；管理员可用 /wakes 查看）")
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


# 只把尾部参数交给专门解析函数的命令；名字互不重复，查表与原 if 链等价。新增这类命令时加在这里。
_TRAILING_PARSERS: dict[str, Callable[[object], ConversationControlCommand | ConversationTaskCommand]] = {
    "goal": _goal_command,
    "recover": _recover_command,
    "endtask": _endtask_command,
    "wakes": _wakes_command,
    "model": _model_command,
    "admin": _admin_command,
    "experiment": _experiment_command,
    "skills": _skills_command,
    "settings": _settings_command,
}


__all__ = [
    "DECISION_EXPERIMENT_TASK_KIND",
    "ConversationControlCommand",
    "ConversationControlResult",
    "ConversationTaskCommand",
    "ConversationTaskStatus",
    "NamedConversationWorkStatus",
    "control_command_carries_secret",
    "conversation_task_attributes",
    "decision_experiment_task",
    "conversation_compact_interrupt_name",
    "conversation_request_interrupt_name",
    "parse_conversation_control",
    "parse_conversation_command",
    "parse_conversation_task_command",
    "persisted_control_command_text",
    "render_conversation_task_status",
    "render_verbose_control",
]
