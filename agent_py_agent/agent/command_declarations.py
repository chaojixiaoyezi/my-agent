# LLM: 命令目录与包描述共用此 JSON 读取器；参数约束仍只由原 dataclass 裁决，读取不授予权限或加载实现。
# 模块用途: 把跨进程声明还原为不可变命令，避免包安装、帮助与补全出现不同的参数协议。

from __future__ import annotations

from dataclasses import fields

from .command_arguments import ArgumentSpec, CommandActionSpec
from .plugin_commands import PluginCommandSpec


# LLM: JSON 数组不能从字符串、对象或任意可迭代值推断；仅转换已验证的声明集合。
# 函数用途: 为动作、参数和候选列表提供一致的类型检查。
def declaration_list(value: object) -> list:
    if not isinstance(value, list):
        raise ValueError("声明集合必须是数组")
    return value


# LLM: 序列化字段与 dataclass 同源；完整字段及精确类型是协议要求，禁止隐式补全或强制转换。
# 函数用途: 校验跨进程命令声明的字段集合和基础类型。
def _record(
    value: object, kind: type, *, strings: tuple[str, ...], booleans: tuple[str, ...] = ()
) -> dict:
    if not isinstance(value, dict) or set(value) != {item.name for item in fields(kind)}:
        raise ValueError("声明字段不完整或存在未知字段")
    if any(not isinstance(value[key], str) for key in strings) or any(
        type(value[key]) is not bool for key in booleans
    ):
        raise ValueError("声明字段类型错误")
    return dict(value)


# LLM: 参数类型、默认值和候选约束继续由 ArgumentSpec 裁决；这里只恢复 JSON 丢失的不可变集合。
# 函数用途: 读取一个参数，不接受字符串冒充开关或数组。
def _argument(value: object) -> ArgumentSpec:
    row = _record(
        value,
        ArgumentSpec,
        strings=("name", "summary", "value_type"),
        booleans=("required", "multiple", "path"),
    )
    row["options"] = tuple(declaration_list(row["options"]))
    row["choices"] = tuple(declaration_list(row["choices"]))
    if isinstance(row["default"], list):
        row["default"] = tuple(row["default"])
    return ArgumentSpec(**row)


# LLM: kind/target 仍只是声明；目录与安装调用方必须继续核对作用域和实际执行权限。
# 函数用途: 用公共合同读取一个动作及其参数，不取得或运行 handler。
def command_action_from_payload(value: object) -> CommandActionSpec:
    row = _record(
        value,
        CommandActionSpec,
        strings=("name", "summary", "kind", "target"),
        booleans=("available",),
    )
    row["arguments"] = tuple(_argument(item) for item in declaration_list(row["arguments"]))
    return CommandActionSpec(**row)


# LLM: 这里保留宿主公开的版本和激活引用，不从包描述生成执行身份或启用资格。
# 函数用途: 读取宿主插件目录的静态信息和动作，不连接执行端点。
def plugin_command_from_payload(value: object) -> PluginCommandSpec:
    row = _record(
        value,
        PluginCommandSpec,
        strings=("plugin_id", "summary", "default_action", "package_version", "activation_id"),
        booleans=("enabled",),
    )
    row["actions"] = tuple(
        command_action_from_payload(item) for item in declaration_list(row["actions"])
    )
    return PluginCommandSpec(**row)
