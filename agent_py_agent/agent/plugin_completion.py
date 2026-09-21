# LLM: 插件补全只消费静态声明和公共解析状态；文件候选必须由宿主显式提供，不加载插件或执行动作。
# 模块用途: 将命名空间、动作、选项及参数候选转换为只编辑输入的补全项。

from __future__ import annotations

import shlex
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .command_arguments import (
    HELP_OPTIONS,
    ArgumentSpec,
    CommandActionSpec,
    CommandArgumentError,
    convert_argument,
    lex_command_arguments,
)
from .command_binding import BoundArguments, bind_command_arguments
from .command_catalog import COMMAND_INDEX
from .plugin_commands import (
    PluginCommandSpec,
    namespace_actions,
    plugin_namespace,
    select_plugin_action,
)

PathCandidates = Callable[[str], Iterable[tuple[str, bool]]]


# LLM: start 为原输入字符偏移，text 只替换当前 token；没有提交或调用处理器的能力。
# 类用途: 保存一个可展示的补全候选及是否需要后置空格。
@dataclass(frozen=True)
class PluginCompletion:
    text: str
    start: int
    label: str
    summary: str
    append_space: bool = True


# LLM: 候选引用使用与公共词法兼容的单引号拼接，反斜杠仍是数据；输入前文保持逐字不变。
# 函数用途: 将一个参数候选安全写回输入框，不进行 Shell 展开。
def _candidate(value: str, start: int, summary: str, *, append_space: bool = True) -> PluginCompletion:
    return PluginCompletion(shlex.quote(value), start, value, summary, append_space)


# LLM: 参数候选只读取声明及指定路径枚举器；普通字符串或未知参数不能扫描文件系统。
# 函数用途: 生成枚举值或显式路径参数的候选。
def _value_candidates(
    spec: ArgumentSpec, prefix: str, start: int, paths: PathCandidates | None,
) -> Iterable[PluginCompletion]:
    for value in spec.choices:
        text = str(value)
        if text.startswith(prefix):
            yield _candidate(text, start, spec.summary)
    if spec.path and paths is not None:
        for text, is_dir in paths(prefix):
            yield _candidate(text, start, spec.summary, append_space=not is_dir)


# LLM: 候选必须在同一绑定器中落到预期参数，不能把负号字符串变成选项；不另写负号或类型判定。
# 函数用途: 过滤当前语法位置不能接受的值，等号形式及 -- 后的字面值仍可正常补全。
def _bound_candidates(
    action: CommandActionSpec, completed: tuple[str, ...], bound: BoundArguments,
    spec: ArgumentSpec, prefix: str, start: int, paths: PathCandidates | None, *, option: str = "",
) -> Iterable[PluginCompletion]:
    for item in _value_candidates(spec, prefix, start, paths):
        token = option + "=" + item.label if option else item.label
        try:
            trial = bind_command_arguments(action, (*completed, token), partial=True)
            value = convert_argument(spec, item.label)
        except CommandArgumentError:
            continue
        previous = bound.values[spec.name] if spec.name in bound.seen else ()
        expected = (*previous, value) if spec.multiple else value
        if trial.values.get(spec.name) == expected and spec.name in trial.seen:
            yield _candidate(token, start, item.summary, append_space=item.append_space)


# LLM: 同一绑定器提供缺值、位置索引及 -- 状态；补全过程绝不使用局部 split 重新猜测语法。
# 函数用途: 根据已经完整输入的参数生成当前 token 的候选，语法错误则保持原文不动。
def _argument_candidates(
    action: CommandActionSpec, completed: tuple[str, ...], prefix: str, start: int,
    paths: PathCandidates | None, *, requested: bool = False,
) -> Iterable[PluginCompletion]:
    bound = bind_command_arguments(action, completed, partial=True)
    if bound.help_requested:
        return
    if bound.awaiting is not None:
        yield from _bound_candidates(action, completed, bound, bound.awaiting, prefix, start, paths)
        return
    if not bound.options_ended and prefix.startswith("--") and "=" in prefix:
        option, value_prefix = prefix.split("=", 1)
        spec = next((arg for arg in action.arguments if option in arg.options), None)
        if spec is not None and spec.value_type != "boolean":
            yield from _bound_candidates(action, completed, bound, spec, value_prefix, start, paths, option=option)
        return
    if not bound.options_ended and (requested or prefix.startswith("-")):
        declared = (*HELP_OPTIONS, *(option for spec in action.arguments for option in spec.options))
        if not requested and prefix in declared:
            return
        for spec in action.arguments:
            if spec.name in bound.seen and not spec.multiple:
                continue
            for option in spec.options:
                if option.startswith(prefix):
                    yield _candidate(option, start, spec.summary)
        for option in HELP_OPTIONS:
            if option.startswith(prefix):
                yield _candidate(option, start, "查看帮助")
        if prefix:
            return
    positional = tuple(spec for spec in action.arguments if not spec.options)
    if bound.positional_count < len(positional):
        spec = positional[bound.positional_count]
        if prefix or spec.required or requested:
            yield from _bound_candidates(action, completed, bound, spec, prefix, start, paths)


# LLM: 显式动作名优先；默认首位置候选必须仍由同一选择器认作参数，不能覆盖动作候选语义。
# 函数用途: 合并初始动作、帮助与默认参数候选，保持选择和严格解析一致。
def _initial_candidates(
    actions: tuple[CommandActionSpec, ...], plugin: PluginCommandSpec | None,
    prefix: str, start: int, paths: PathCandidates | None, *, requested: bool,
) -> tuple[PluginCompletion, ...]:
    if not requested and any(action.name == prefix for action in actions):
        return ()
    candidates = [_candidate(action.name, start, action.summary) for action in actions if action.available and action.name.startswith(prefix)]
    candidates.extend(_candidate(option, start, "查看帮助") for option in HELP_OPTIONS if option.startswith(prefix))
    if plugin is not None and plugin.default_action:
        default = next(item for item in actions if item.name == plugin.default_action)
        if default.available:
            for item in _argument_candidates(default, (), prefix, start, paths, requested=requested):
                selected, arguments = select_plugin_action(actions, (item.label,), plugin)
                if selected is default and arguments == (item.label,):
                    candidates.append(item)
    return tuple({item.text: item for item in candidates}.values())


# LLM: 插件与管理动作消费同一宿主目录；requested 只表示显式请求候选，完整输入不能被自动菜单改写后吞掉 Enter。
# 函数用途: 补全插件 ID、动作和参数；未开放的管理动作仅能通过 help 发现，不暗示可执行。
def complete_plugin_command(
    text: str, *, plugins: tuple[PluginCommandSpec, ...] = (), paths: PathCandidates | None = None,
    requested: bool = False, management_actions: tuple[CommandActionSpec, ...] = COMMAND_INDEX["plugins"].actions,
) -> tuple[PluginCompletion, ...]:
    namespace = plugin_namespace(text)
    if namespace is None:
        return ()
    if "@" in namespace.prefix and not any(char.isspace() for char in text.lstrip()):
        if not requested and any(plugin.plugin_id == namespace.plugin_id for plugin in plugins):
            return ()
        return tuple(PluginCompletion(
            "/plugins@" + plugin.plugin_id, len(text) - len(text.lstrip()), plugin.plugin_id, plugin.summary,
        ) for plugin in plugins if plugin.enabled and plugin.plugin_id.startswith(namespace.plugin_id))
    if not namespace.body and not text[-1].isspace() and not requested:
        return ()
    try:
        actions, plugin = namespace_actions(namespace, plugins, management_actions)
        tokens = lex_command_arguments(namespace.body, partial=True)
        current = tokens[-1] if tokens and tokens[-1].end == len(namespace.body) else None
        completed = tuple(token.value for token in (tokens[:-1] if current else tokens))
        prefix = current.value if current else ""
        start = namespace.body_start + current.start if current else len(text)
        if plugin is not None and not plugin.enabled:
            can_help = not completed or (len(completed) == 1 and any(item.name == completed[0] for item in actions))
            if not prefix and completed and not requested:
                return ()
            return tuple(_candidate(option, start, "查看停用插件的静态帮助") for option in HELP_OPTIONS if can_help and option.startswith(prefix))
        if not completed:
            return _initial_candidates(actions, plugin, prefix, start, paths, requested=requested)
        action, arguments = select_plugin_action(actions, completed, plugin)
        if action is None:
            return ()
        if plugin is None and action.name == "help" and not arguments and not prefix.startswith("-"):
            if not requested and (not prefix or any(item.name == prefix for item in actions)):
                return ()
            return tuple(_candidate(item.name, start, item.summary) for item in actions if item.name.startswith(prefix))
        return tuple(_argument_candidates(action, arguments, prefix, start, paths, requested=requested))
    except CommandArgumentError:
        return ()
