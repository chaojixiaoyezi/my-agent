# LLM: 声明是命令、工具及默认配置的唯一来源；这里只支持本插件实际使用的平面字段，不实现另一套宿主 schema 引擎。
# 模块用途: 读取随 wheel 发布的声明，校验工具参数和宿主注入的设置，并定义插件业务错误。

from __future__ import annotations

import json
from importlib.resources import files


# LLM: 业务错误只带稳定 code 与中文说明，由 server 转成 isError 工具结果；不携带本地路径或堆栈。
# 类用途: 表示可直接返回给调用方的插件业务错误。
class ConsoleError(Exception):
    # 函数用途: 记录错误码和中文说明。
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# LLM: 仅加载本包固定资源，不读 cwd、用户路径或宿主模块；返回调用方私有对象。
# 函数用途: 取得与安装清单构建同源的静态声明。
def declaration() -> dict:
    return json.loads(files("harness_console").joinpath("declaration.json").read_text(encoding="utf-8"))


# LLM: 宿主仍执行完整 schema 验证；此函数只处理 string/integer/boolean 平面字段，拒绝未知字段。
# 函数用途: 检查工具参数或设置并从唯一声明补默认值。
def fields(value: object, schema: dict) -> dict:
    properties = schema["properties"]
    if (not isinstance(value, dict) or set(value) - set(properties)
            or set(schema.get("required", ())) - set(value)):
        raise ConsoleError("INVALID_ARGUMENTS", "参数缺失或包含未声明字段。")
    result = dict(value)
    for name, spec in properties.items():
        if name not in result and "default" in spec:
            result[name] = spec["default"]
        if name in result and not _valid(result[name], spec):
            raise ConsoleError("INVALID_ARGUMENTS", "参数类型或取值范围无效。")
    return result


# LLM: 纯函数；未实现的字段类型直接视为无效，避免声明里写了却没校验。
# 函数用途: 判断单个字段值是否满足声明的类型和范围。
def _valid(item: object, spec: dict) -> bool:
    if spec["type"] == "integer":
        return type(item) is int and spec["minimum"] <= item <= spec["maximum"]
    if spec["type"] == "boolean":
        return type(item) is bool
    if spec["type"] == "string":
        return isinstance(item, str) and spec.get("minLength", 0) <= len(item) <= spec.get("maxLength", 8192)
    return False
