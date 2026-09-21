# LLM: 参数绑定只消费结构化声明和词法 token；完整提交与补全共享同一状态机，不执行副作用。
# 模块用途: 统一处理位置参数、长短选项、组合旗标、帮助和双横线分界。

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .command_arguments import (
    HELP_OPTIONS,
    ArgumentSpec,
    ArgumentValue,
    CommandActionSpec,
    CommandArgumentError,
    convert_argument,
)


# LLM: awaiting/options_ended/positional_count 是补全读取的解析事实；values 不能用作执行授权。
# 类用途: 返回已绑定参数、帮助请求和当前缺值位置。
@dataclass(frozen=True)
class BoundArguments:
    values: Mapping[str, ArgumentValue | tuple[ArgumentValue, ...]]
    help_requested: bool
    seen: frozenset[str]
    options_ended: bool
    positional_count: int
    awaiting: ArgumentSpec | None = None


# LLM: 重复通过规范参数名检查，所以长短别名不能绕过单值限制；不修改原声明。
# 函数用途: 将一次出现的参数追加到本次局部绑定结果。
def _record_value(values: dict, seen: set[str], spec: ArgumentSpec, value: ArgumentValue) -> None:
    if spec.name in seen and not spec.multiple:
        raise CommandArgumentError("duplicate_argument", f"参数 {spec.name} 不能重复。", spec.name)
    if spec.multiple:
        values[spec.name] = (*values.get(spec.name, ()), value)
    else:
        values[spec.name] = value
    seen.add(spec.name)


# LLM: 仅当每个短选项均已声明且无需值时才拆组合；带值短选项禁止 -n20 或 -xn 的猜测。
# 函数用途: 从一个选项 token 找到参数声明及可选的等号值。
def _option_parts(token: str, options: Mapping[str, ArgumentSpec]) -> tuple[tuple[ArgumentSpec, ...], str | None]:
    name, separator, value = token.partition("=")
    if name in options:
        if separator and not name.startswith("--"):
            raise CommandArgumentError("short_option_value", "短选项的值必须单独写在后面。")
        return (options[name],), value if separator else None
    if token.startswith("-") and not token.startswith("--") and len(token) > 2 and not separator:
        bundle = tuple(options.get("-" + char) for char in token[1:])
        if all(spec is not None and spec.value_type == "boolean" for spec in bundle):
            return bundle, None
    raise CommandArgumentError("unknown_option", "存在未声明的选项或无效的短选项组合。")


# LLM: 只有数值声明可以直接消费以负号起始的数值；字符串旗标值应通过 --name=-x 明确表达。
# 函数用途: 判定当前 token 能否作为前一个选项的值，避免吞掉下一个选项。
def _is_option_value(spec: ArgumentSpec, token: str) -> bool:
    if not token.startswith("-") or token == "-":
        return True
    if spec.value_type not in {"integer", "number"} or token == "--":
        return False
    try:
        (int if spec.value_type == "integer" else float)(token)
        return True
    except ValueError:
        return False


# LLM: 选项缺值、布尔旗标和值转换共用一个消费入口；partial 只允许输入末尾仍等待值。
# 函数用途: 取出单个选项的值及下一个 token 位置，避免绑定主循环承担嵌套语法分支。
def _option_value(
    spec: ArgumentSpec, inline: str | None, tokens: tuple[str, ...], index: int, *, partial: bool,
) -> tuple[ArgumentValue | None, int, ArgumentSpec | None]:
    if spec.value_type == "boolean":
        if inline is not None:
            raise CommandArgumentError("unexpected_value", f"旗标 {spec.name} 不接受值。", spec.name)
        return True, index, None
    if inline is None:
        if index == len(tokens) and partial:
            return None, index, spec
        if index == len(tokens) or not _is_option_value(spec, tokens[index]):
            raise CommandArgumentError("missing_value", f"选项 {spec.name} 缺少值。", spec.name)
        inline = tokens[index]
        index += 1
    return convert_argument(spec, inline), index, None


# LLM: partial 只用于补全，跳过尾部缺值和必填检查；其他语法错误仍失败，不能借补全生成执行请求。
# 函数用途: 按声明绑定一组完整 token，同时提供无需另一套解析器的补全位置。
def bind_command_arguments(
    action: CommandActionSpec, tokens: tuple[str, ...], *, partial: bool = False,
) -> BoundArguments:
    options = {option: spec for spec in action.arguments for option in spec.options}
    positional = tuple(spec for spec in action.arguments if not spec.options)
    values: dict[str, ArgumentValue | tuple[ArgumentValue, ...]] = {}
    seen: set[str] = set()
    ended = help_requested = False
    awaiting = None
    index = position = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not ended and token == "--":
            ended = True
            continue
        if not ended and token in HELP_OPTIONS:
            help_requested = True
            continue
        if not ended and token.startswith("-") and token != "-":
            specs, inline = _option_parts(token, options)
            for spec in specs:
                value, index, awaiting = _option_value(spec, inline, tokens, index, partial=partial)
                if awaiting is not None:
                    break
                _record_value(values, seen, spec, value)
            continue
        if position >= len(positional):
            raise CommandArgumentError("unexpected_argument", "位置参数过多。")
        spec = positional[position]
        _record_value(values, seen, spec, convert_argument(spec, token))
        if not spec.multiple:
            position += 1
    for spec in action.arguments:
        if spec.name in seen:
            continue
        if spec.required and not partial and not help_requested:
            raise CommandArgumentError("missing_argument", f"缺少必填参数 {spec.name}。", spec.name)
        if spec.default is not None:
            values[spec.name] = spec.default
        elif spec.value_type == "boolean":
            values[spec.name] = False
        elif spec.multiple:
            values[spec.name] = ()
    return BoundArguments(MappingProxyType(values), help_requested, frozenset(seen), ended, position, awaiting)


# LLM: 帮助从参数声明生成，不另存一份用法字符串；只展示，不触发目标或文件访问。
# 函数用途: 显示某个动作的完整用法、参数和未开放状态。
def render_action_help(prefix: str, action: CommandActionSpec) -> str:
    usage = [prefix, action.name]
    details = []
    for spec in action.arguments:
        label = ", ".join(spec.options) if spec.options else spec.name
        value = "" if spec.value_type == "boolean" else f" <{spec.name}>"
        part = (spec.options[-1] + value) if spec.options else spec.name
        part += "..." if spec.multiple else ""
        if not spec.required:
            part = f"[{part}]"
        elif not spec.options:
            part = f"<{part}>"
        usage.append(part)
        facts = [spec.summary]
        if spec.choices:
            facts.append("候选：" + "、".join(str(item) for item in spec.choices))
        if spec.default is not None:
            facts.append(f"默认：{spec.default}")
        details.append(f"  {label}{value if spec.options else ''}  {'；'.join(facts)}")
    state = "" if action.available else "（业务尚未开放，仅可查看声明）"
    return "\n".join((f"{action.summary}{state}", "用法：" + " ".join(usage), *details, "  -h, --help  查看帮助；-- 之后全部作为位置参数"))
