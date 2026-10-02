# LLM: declaration.json 是命令、工具和设置的唯一声明源；本模块只提供包内读取和最小平面参数校验。
# 模块用途: 读取 drama-media-shell 静态声明并拒绝缺失、未知或类型错误的工具参数。

from __future__ import annotations

import json
from importlib.resources import files


# LLM: 只读取当前 wheel 内固定资源，返回调用方私有对象，不读 cwd 或宿主配置。
# 函数用途: 载入用于包描述和 tools/list 的同源声明。
def declaration() -> dict:
    return json.loads(files("drama_media_shell").joinpath("declaration.json").read_text(encoding="utf-8"))


# LLM: 宿主仍执行完整 JSON Schema 校验；这里只覆盖本包实际使用的字符串字段和空对象，避免独立进程接受未知参数。
# 函数用途: 按工具声明检查调用参数并复制返回。
def fields(value: object, schema: dict) -> dict:
    properties = schema["properties"]
    if not isinstance(value, dict) or set(value) - set(properties) or set(schema.get("required", ())) - set(value):
        raise ValueError("参数缺失或包含未声明字段。")
    result = dict(value)
    for name, item in result.items():
        spec = properties[name]
        if spec.get("type") != "string" or not isinstance(item, str):
            raise ValueError(f"参数 {name} 的类型无效。")
        if not spec.get("minLength", 0) <= len(item) <= spec.get("maxLength", 8192):
            raise ValueError(f"参数 {name} 的长度无效。")
    return result
