# LLM: 插件命令先识别保留命名空间，再从宿主提供的声明解析；本模块不加载插件、不绑定权限或执行目标。
# 模块用途: 为 CLI、HTTP 与补全提供一致的插件语法和静态帮助，错误不进入聊天或旧控制分派。

from __future__ import annotations

import re
from dataclasses import dataclass

from .command_arguments import (
    HELP_OPTIONS,
    CommandActionSpec,
    CommandArgumentError,
    lex_command_arguments,
)
from .command_binding import BoundArguments, bind_command_arguments, render_action_help
from .command_catalog import COMMAND_INDEX, system_slash_command_name

_PLUGIN_ID = re.compile(r"[A-Za-z][A-Za-z0-9._-]{0,63}\Z")


# LLM: 这是宿主目录的只读描述，不是安装注册表；enabled 不代替执行门的 owner、版本与撤销核对。
# 类用途: 让插件静态帮助和动作解析共享身份及显式默认动作。
@dataclass(frozen=True)
class PluginCommandSpec:
    plugin_id: str
    summary: str
    actions: tuple[CommandActionSpec, ...]
    enabled: bool = False
    default_action: str = ""

    # LLM: 拒绝重复动作和无效默认目标，不能依靠展示名或动作数量猜默认业务。
    # 函数用途: 在目录发布前检查插件命令身份。
    def __post_init__(self) -> None:
        if not isinstance(self.actions, tuple):
            raise ValueError("插件动作必须是不可变元组")
        names = tuple(action.name for action in self.actions)
        if not _PLUGIN_ID.fullmatch(self.plugin_id) or len(set(names)) != len(names):
            raise ValueError("插件 ID 无效或动作重名")
        if self.default_action and self.default_action not in names:
            raise ValueError("默认动作未声明")


# LLM: body_start 属于原文字符坐标，仅用于补全；插件 ID 保留大小写，不带 owner 或执行身份。
# 类用途: 表示已识别的管理入口或插件入口。
@dataclass(frozen=True)
class PluginNamespace:
    prefix: str
    plugin_id: str
    body: str
    body_start: int


# LLM: 解析请求只含声明及绑定值，不能直接作为已授权工具调用；无动作输入总是静态帮助。
# 类用途: 将插件命令的帮助意图和业务参数明确分开。
@dataclass(frozen=True)
class ParsedPluginCommand:
    namespace: PluginNamespace
    plugin: PluginCommandSpec | None
    action: CommandActionSpec | None
    arguments: BoundArguments | None = None
    help_requested: bool = False


# LLM: 命名空间判定复用公共目录；异常 ID 仍是系统输入，不能回退为文件路径、自然语言或插话。
# 函数用途: 拆开命令头与原参数正文，并保留准确的替换位置。
def plugin_namespace(text: str) -> PluginNamespace | None:
    name = system_slash_command_name(text)
    if name != "plugins" and not name.startswith("plugins@"):
        return None
    start = len(text) - len(text.lstrip())
    head_end = start
    while head_end < len(text) and not text[head_end].isspace():
        head_end += 1
    body_start = head_end
    while body_start < len(text) and text[body_start].isspace():
        body_start += 1
    plugin_id = name[len("plugins@"):] if name != "plugins" else ""
    prefix = "/plugins" + ("@" + plugin_id if name != "plugins" else "")
    return PluginNamespace(prefix, plugin_id, text[body_start:], body_start)


# LLM: 目录必须由调用宿主按 owner 过滤；拒绝重复 ID，不能静默挑选其中一个或扫描磁盘补齐。
# 函数用途: 为管理或插件命名空间取得唯一声明集合。
def namespace_actions(
    namespace: PluginNamespace, plugins: tuple[PluginCommandSpec, ...],
) -> tuple[tuple[CommandActionSpec, ...], PluginCommandSpec | None]:
    if namespace.prefix == "/plugins":
        return COMMAND_INDEX["plugins"].actions, None
    if not _PLUGIN_ID.fullmatch(namespace.plugin_id):
        raise CommandArgumentError("invalid_plugin_id", "插件 ID 应为 1—64 位英文字母开头的字母、数字、点、下划线或横线。")
    if len({plugin.plugin_id for plugin in plugins}) != len(plugins):
        raise ValueError("宿主插件目录包含重复 ID")
    plugin = next((item for item in plugins if item.plugin_id == namespace.plugin_id), None)
    if plugin is None:
        raise CommandArgumentError("unknown_plugin", "当前目录没有这个插件；输入 /plugins help 查看入口说明。")
    return plugin.actions, plugin


# LLM: 只有显式声明的默认动作能接收省略动作名的选项或参数；无参数及帮助不能触发默认业务。
# 函数用途: 从词法结果选定动作，并把剩余 token 留给公共参数绑定器。
def select_plugin_action(
    actions: tuple[CommandActionSpec, ...], tokens: tuple[str, ...], plugin: PluginCommandSpec | None,
) -> tuple[CommandActionSpec | None, tuple[str, ...]]:
    if not tokens or (len(tokens) == 1 and tokens[0] in HELP_OPTIONS):
        return None, ()
    action = next((item for item in actions if item.name == tokens[0]), None)
    if action is not None:
        return action, tokens[1:]
    if plugin is not None and plugin.default_action:
        return next(item for item in actions if item.name == plugin.default_action), tokens
    raise CommandArgumentError("unknown_action", "未声明这个动作；请查看该命令的帮助。")


# LLM: 完整提交必须通过同一词法与绑定器；未闭合引号不会被拆词回退掩盖，正文不会传给 Shell。
# 函数用途: 解析一条插件命令，普通聊天返回 None，命令错误保留结构化原因。
def parse_plugin_command(
    text: str, *, plugins: tuple[PluginCommandSpec, ...] = (),
) -> ParsedPluginCommand | None:
    namespace = plugin_namespace(text)
    if namespace is None:
        return None
    actions, plugin = namespace_actions(namespace, plugins)
    tokens = tuple(token.value for token in lex_command_arguments(namespace.body))
    action, arguments = select_plugin_action(actions, tokens, plugin)
    try:
        bound = bind_command_arguments(action, arguments) if action else None
    except CommandArgumentError as exc:
        exc.usage = render_action_help(namespace.prefix, action)
        raise
    help_requested = action is None or bool(bound and bound.help_requested) or (plugin is None and action.name == "help")
    return ParsedPluginCommand(namespace, plugin, action, bound, help_requested)


# LLM: 静态说明包括未开放动作，但不伪称已经安装或执行；停用插件也可读取声明，不启动其进程。
# 函数用途: 根据同一动作目录生成分组帮助。
def render_plugin_help(namespace: PluginNamespace, actions: tuple[CommandActionSpec, ...]) -> str:
    lines = [f"用法：{namespace.prefix} [动作] [参数]"]
    lines.extend(f"  {action.name}  {action.summary}" + ("（尚未开放）" if not action.available else "") for action in actions)
    lines.append(f"输入 {namespace.prefix} <动作> --help 查看参数。")
    if not namespace.plugin_id:
        lines.append("插件业务入口：/plugins@<插件ID> [动作] [参数]；当前版本尚未开放安装、启用与调用。")
    return "\n".join(lines)


# LLM: 当前只处理静态帮助与拒绝结果；未来业务执行应消费 ParsedPluginCommand 并接原工具链，不能在这里另造执行器。
# 函数用途: 为所有实际入口返回同一份只读命令回执，不修改任务、Goal 或请求队列。
def plugin_command_response(text: str) -> dict[str, object] | None:
    namespace = plugin_namespace(text)
    if namespace is None:
        return None
    result: dict[str, object] = {"kind": "plugin_command", "ok": False, "request_id": ""}
    actions: tuple[CommandActionSpec, ...] = ()
    action = None
    try:
        actions, _plugin = namespace_actions(namespace, ())
        parsed = parse_plugin_command(text)
        action = parsed.action
        if parsed.help_requested:
            if action is not None and action.name == "help" and not parsed.arguments.help_requested:
                target = parsed.arguments.values.get("action", "")
                action = next((item for item in actions if item.name == target), None)
                if target and action is None:
                    raise CommandArgumentError("unknown_action", "未声明这个管理动作。")
            result.update(ok=True, message=render_action_help(namespace.prefix, action) if action else render_plugin_help(namespace, actions))
        else:
            result.update(error_code="PLUGIN_COMMAND_UNAVAILABLE", reason="not_implemented", message="该插件管理动作尚未开放；本次没有执行操作。\n" + render_action_help(namespace.prefix, action))
    except CommandArgumentError as exc:
        result.update(
            error_code="UNKNOWN_PLUGIN" if exc.reason == "unknown_plugin" else "INVALID_COMMAND_ARGUMENTS",
            reason=exc.reason,
            argument=exc.argument,
            message=str(exc) + "\n" + (exc.usage or render_plugin_help(namespace, actions)),
        )
    return result
