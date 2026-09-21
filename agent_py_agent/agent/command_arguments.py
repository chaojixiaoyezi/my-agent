# LLM: 命令参数合同不执行命令或绑定身份；声明校验、词法和类型转换供解析及补全共同使用。
# 模块用途: 描述位置参数与选项，保留引号中的路径原文，并返回可定位的语法错误。

from __future__ import annotations

import math
import re
import shlex
from dataclasses import dataclass
from io import StringIO
from typing import Literal

ArgumentValue = str | int | float | bool
_NAME = re.compile(r"[a-z][a-z0-9_-]*\Z")
_OPTION = re.compile(r"(?:-[A-Za-z]|--[a-z][a-z0-9-]*)\Z")
HELP_OPTIONS = ("-h", "--help")


# LLM: reason 是机器可读分类，message 只用于展示；调用方不能按中文错误反推恢复或执行。
# 类用途: 携带无副作用的参数错误及对应参数名。
class CommandArgumentError(ValueError):
    # LLM: 不保存整段输入，避免把凭据参数复制进异常；界面自行附上由声明生成的用法。
    # 函数用途: 创建结构化语法或绑定错误。
    def __init__(self, reason: str, message: str, argument: str = "") -> None:
        super().__init__(message)
        self.reason = reason
        self.argument = argument
        self.usage = ""


# LLM: 空 options 表示位置参数；bool 只表示无值旗标，path 仅控制补全，不授予文件权限。
# 类用途: 用一份不可变声明定义参数类型、别名、必填、重复、默认值与候选值。
@dataclass(frozen=True)
class ArgumentSpec:
    name: str
    summary: str
    options: tuple[str, ...] = ()
    value_type: Literal["string", "integer", "number", "boolean"] = "string"
    required: bool = False
    multiple: bool = False
    default: ArgumentValue | tuple[ArgumentValue, ...] | None = None
    choices: tuple[ArgumentValue, ...] = ()
    path: bool = False

    # LLM: 非法声明在发布目录前失败，不能依靠某次用户输入才发现冲突；这里不读取文件或加载插件。
    # 函数用途: 检查参数描述内部一致性及默认值类型。
    def __post_init__(self) -> None:
        if not isinstance(self.options, tuple) or not isinstance(self.choices, tuple):
            raise ValueError("参数选项和候选值必须是不可变元组")
        if not _NAME.fullmatch(self.name):
            raise ValueError("参数名必须是英文小写标识符")
        if self.value_type not in {"string", "integer", "number", "boolean"}:
            raise ValueError("不支持的参数类型")
        if any(not _OPTION.fullmatch(option) or option in HELP_OPTIONS for option in self.options):
            raise ValueError("选项名称无效或占用帮助选项")
        if self.value_type == "boolean" and (not self.options or self.multiple):
            raise ValueError("布尔旗标必须有选项名且不能重复")
        if self.value_type == "boolean" and self.choices:
            raise ValueError("布尔旗标不声明候选值")
        if self.path and self.value_type != "string":
            raise ValueError("路径参数必须是字符串")
        if self.required and self.default is not None:
            raise ValueError("必填参数不能同时声明默认值")
        for value in self.choices:
            self._check_value(value)
        if self.default is not None:
            values = self.default if isinstance(self.default, tuple) else (self.default,)
            if isinstance(self.default, tuple) != self.multiple:
                raise ValueError("多值参数默认值必须是元组，单值参数必须是标量")
            for value in values:
                self._check_value(value)
                if self.choices and value not in self.choices:
                    raise ValueError("默认值不在候选值中")

    # LLM: Python bool 是 int 的子类，但参数合同不能混同；浮点数必须有限，避免跨 JSON 边界改变值。
    # 函数用途: 验证声明中的候选值和默认值符合参数类型。
    def _check_value(self, value: ArgumentValue) -> None:
        types = {"string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,)}
        if type(value) not in types[self.value_type]:
            raise ValueError("参数值与声明类型不一致")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("数值必须有限")


# LLM: kind/target 是分派描述而非权限；available 仅声明当前入口是否实现，执行前仍需宿主核对。
# 类用途: 汇集一个动作的参数与用途，统一驱动帮助、解析和补全。
@dataclass(frozen=True)
class CommandActionSpec:
    name: str
    summary: str
    arguments: tuple[ArgumentSpec, ...] = ()
    kind: Literal["management", "tool", "model", "display"] = "management"
    target: str = ""
    available: bool = True

    # LLM: 同动作内参数身份及选项别名必须唯一；不从 summary 或 usage 推断必填与数量。
    # 函数用途: 拒绝歧义声明，保证多值位置参数只占最后一个位置。
    def __post_init__(self) -> None:
        if not isinstance(self.arguments, tuple):
            raise ValueError("动作参数必须是不可变元组")
        if not _NAME.fullmatch(self.name) or self.kind not in {"management", "tool", "model", "display"}:
            raise ValueError("动作名称或执行类型无效")
        names: set[str] = set()
        options: set[str] = set()
        optional_seen = multiple_seen = False
        for argument in self.arguments:
            if argument.name in names or any(option in options for option in argument.options):
                raise ValueError("参数名或选项别名重复")
            if len(set(argument.options)) != len(argument.options):
                raise ValueError("同一参数中的选项别名重复")
            names.add(argument.name)
            options.update(argument.options)
            if not argument.options:
                if multiple_seen or (optional_seen and argument.required):
                    raise ValueError("位置参数顺序有歧义")
                optional_seen = not argument.required
                multiple_seen = argument.multiple


# LLM: start/end 是原输入字符偏移，不是去引号后的长度；补全只替换当前 token，保留前文。
# 类用途: 保存一个参数的值、原文范围以及未闭合引号状态。
@dataclass(frozen=True)
class ArgumentToken:
    value: str
    start: int
    end: int
    open_quote: str = ""


# LLM: 同一词法器用于提交与补全，反斜杠、井号及 Shell 符号都只是数据；不做错误后 split 降级。
# 函数用途: 拆分引用参数，保留 Windows 路径，并在补全时允许最后一个引号尚未关闭。
def lex_command_arguments(text: str, *, partial: bool = False) -> tuple[ArgumentToken, ...]:
    stream = StringIO(text)
    lexer = shlex.shlex(stream, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    lexer.escape = ""
    lexer.whitespace = "".join(sorted({char for char in text if char.isspace()}))
    tokens = []
    while True:
        start = stream.tell()
        while start < len(text) and text[start].isspace():
            start += 1
        try:
            value = lexer.get_token()
        except ValueError as exc:
            if partial and lexer.state in lexer.quotes:
                tokens.append(ArgumentToken(lexer.token, start, len(text), lexer.state))
                break
            raise CommandArgumentError("unclosed_quote", "引号尚未闭合。") from exc
        if value is None:
            break
        end = stream.tell()
        if lexer.state == " " and end and text[end - 1].isspace():
            end -= 1
        tokens.append(ArgumentToken(value, start, end))
    return tuple(tokens)


# LLM: 转换只依据声明类型与候选值，不解释路径、环境变量或 Shell 表达式。
# 函数用途: 将一个参数字符串转成声明值，错误保留稳定分类。
def convert_argument(spec: ArgumentSpec, text: str) -> ArgumentValue:
    try:
        value: ArgumentValue = text
        if spec.value_type == "integer":
            value = int(text)
        elif spec.value_type == "number":
            value = float(text)
            if not math.isfinite(value):
                raise ValueError("non-finite")
        if spec.choices and value not in spec.choices:
            raise CommandArgumentError("invalid_choice", f"参数 {spec.name} 不在允许的候选值中。", spec.name)
        return value
    except ValueError as exc:
        if isinstance(exc, CommandArgumentError):
            raise
        raise CommandArgumentError("invalid_type", f"参数 {spec.name} 的值不符合 {spec.value_type} 类型。", spec.name) from exc
