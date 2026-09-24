# LLM: 声明是命令、工具及默认配置的唯一来源；这里只支持本包实际使用的平面 string/boolean 字段与 enum，
#   不实现另一套宿主 schema 引擎。enum 不符交给业务层给出带候选的中文错误。
# 模块用途: 读取随 wheel 发布的声明，对隔离进程收到的工具参数做必要校验。

from __future__ import annotations

import json
from importlib.resources import files

from .templates import DesignError


# LLM: 仅加载本包固定资源，不读 cwd、用户路径或宿主模块；返回调用方私有对象。
# 函数用途: 取得与安装清单构建同源的静态声明。
def declaration() -> dict:
    return json.loads(files("design_lite").joinpath("declaration.json").read_text(encoding="utf-8"))


# LLM: 宿主仍执行完整 schema 验证；此函数拒绝未知字段、缺必填、类型不符和越界长度，并从声明补默认值。
#   bool 字段必须是真 bool，不接受 0/1 或字符串。
# 函数用途: 检查隔离入口收到的工具参数或插件设置。
def fields(value: object, schema: dict) -> dict:
    properties = schema["properties"]
    if (not isinstance(value, dict) or set(value) - set(properties)
            or set(schema.get("required", ())) - set(value)):
        raise DesignError("INVALID_ARGUMENTS", "参数缺失或包含未声明字段。")
    result = dict(value)
    for name, spec in properties.items():
        if name not in result and "default" in spec:
            result[name] = spec["default"]
        if name not in result:
            continue
        item = result[name]
        if spec["type"] == "boolean":
            valid = type(item) is bool
        elif spec["type"] == "string":
            valid = isinstance(item, str) and spec.get("minLength", 0) <= len(item) <= spec.get("maxLength", 8192)
        else:
            raise DesignError("INVALID_DECLARATION", "插件声明使用了未实现的字段类型。")
        if not valid:
            raise DesignError("INVALID_ARGUMENTS", f"参数 {name} 的类型或长度无效。")
    return result
