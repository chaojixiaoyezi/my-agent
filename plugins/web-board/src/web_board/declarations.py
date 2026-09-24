# LLM: 声明是命令、工具及默认配置的唯一来源；这里只支持本样本实际使用的平面字段，不实现另一套宿主 schema 引擎。
# 模块用途: 读取随 wheel 发布的声明，校验工具参数和宿主注入的设置。

from __future__ import annotations

import json
from importlib.resources import files

from .reading import BoardError


# LLM: 仅加载本包固定资源，不读 cwd、用户路径或宿主模块；返回调用方私有对象。
# 函数用途: 取得与安装清单构建同源的静态声明。
def declaration() -> dict:
    return json.loads(files("web_board").joinpath("declaration.json").read_text(encoding="utf-8"))


# LLM: 宿主仍执行完整 schema 验证；此函数仅处理本样本声明的 string/integer 平面参数，拒绝未知字段。
# 函数用途: 检查隔离入口参数并从唯一声明补默认值。
def fields(value: object, schema: dict) -> dict:
    properties = schema["properties"]
    if (not isinstance(value, dict) or set(value) - set(properties)
            or set(schema.get("required", ())) - set(value)):
        raise BoardError(400, "INVALID_ARGUMENTS", "参数缺失或包含未声明字段。")
    result = dict(value)
    for name, spec in properties.items():
        if name not in result and "default" in spec:
            result[name] = spec["default"]
        if name not in result:
            continue
        item = result[name]
        if spec["type"] == "integer":
            valid = type(item) is int and spec["minimum"] <= item <= spec["maximum"]
        elif spec["type"] == "string":
            valid = isinstance(item, str) and spec.get("minLength", 0) <= len(item) <= spec.get("maxLength", 8192)
        else:
            raise BoardError(400, "INVALID_DECLARATION", "插件声明使用了未实现的字段类型。")
        if not valid:
            raise BoardError(400, "INVALID_ARGUMENTS", "参数类型或取值范围无效。")
    return result
