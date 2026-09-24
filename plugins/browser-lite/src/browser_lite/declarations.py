# LLM: 声明是命令、工具及默认配置的唯一来源；这里只支持本包实际使用的平面 string/integer/字符串数组字段，
#   不实现另一套宿主 schema 引擎。
# 模块用途: 读取随 wheel 发布的声明，对隔离进程收到的参数和设置做必要校验。

from __future__ import annotations

import json
from importlib.resources import files

from .errors import BrowserError


# LLM: 仅加载本包固定资源，不读 cwd、用户路径或宿主模块；返回调用方私有对象。
# 函数用途: 取得与安装清单构建同源的静态声明。
def declaration() -> dict:
    return json.loads(files("browser_lite").joinpath("declaration.json").read_text(encoding="utf-8"))


# LLM: 宿主仍执行完整 schema 验证；此函数拒绝未知字段、bool 冒充整数、越界长度和非字符串数组项，并从声明补默认值。
# 函数用途: 检查隔离入口参数或插件设置。
def fields(value: object, schema: dict) -> dict:
    properties = schema["properties"]
    if (not isinstance(value, dict) or set(value) - set(properties)
            or set(schema.get("required", ())) - set(value)):
        raise BrowserError("INVALID_ARGUMENTS", "参数缺失或包含未声明字段。")
    result = dict(value)
    for name, spec in properties.items():
        if name not in result and "default" in spec:
            result[name] = spec["default"]
        if name in result and not _valid(result[name], spec):
            raise BrowserError("INVALID_ARGUMENTS", f"参数 {name} 的类型或取值范围无效。")
    return result


# LLM: 只认本包声明用到的三种类型；遇到未实现类型视为声明错误。
# 函数用途: 校验单个字段值是否符合其声明。
def _valid(item: object, spec: dict) -> bool:
    if spec["type"] == "integer":
        return type(item) is int and spec["minimum"] <= item <= spec["maximum"]
    if spec["type"] == "string":
        return isinstance(item, str) and spec.get("minLength", 0) <= len(item) <= spec.get("maxLength", 8192)
    if spec["type"] == "array":
        return (isinstance(item, list) and spec.get("minItems", 0) <= len(item) <= spec.get("maxItems", 64)
                and all(_valid(entry, spec["items"]) for entry in item))
    raise BrowserError("INVALID_DECLARATION", "插件声明使用了未实现的字段类型。")
