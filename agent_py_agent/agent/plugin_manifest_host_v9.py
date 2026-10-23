# LLM: v9是显式文件入口版本，不扩大旧协议read枚举；声明仅申请，绝不授予宿主执行权。
# 模块用途: 校验和序列化插件宿主v2能力清单。
from __future__ import annotations


# LLM: 返回固定能力集合，不由运行时插件扩展，不隐含角色继承。
# 函数用途: 列出v9认识的全部能力。
def host_capabilities() -> frozenset[str]:
    return frozenset({"read", "chat.history", "chat", "threads.manage", "requests.cancel", "execute",
                      "users.manage", "act_as_user", "interactions.read", "interactions.respond",
                      "tasks.read", "tasks.control", "goals.read", "goals.control", "memory.read", "artifacts.read"})


# LLM: list/tuple入口由调用者指定，不做字符串拆分或去重清洗。
# 函数用途: 拒绝空、重复和未知能力。
def validate_capabilities(value, *, sequence_type=tuple) -> tuple[str, ...]:
    if (not isinstance(value, sequence_type) or not value or len(value) > len(host_capabilities())
            or any(not isinstance(item, str) or item not in host_capabilities() for item in value)
            or len(set(value)) != len(value)):
        raise ValueError("v9宿主能力集合无效")
    return tuple(value)


# LLM: 直接构造也必须文件入口且禁止订阅，不因绕过JSON而放宽。
# 函数用途: 验证v9清单的版本标记和入口限制。
def validate_v9_manifest(manifest) -> None:
    if (manifest.host_api_v9 is not True or manifest.entry is None or manifest.is_content_only
            or manifest.permissions is not None or manifest.events or manifest.tool_gates):
        raise ValueError("v9只接受无事件订阅的文件入口")
    validate_capabilities(manifest.host_api)


# LLM: 闭合字段沿v6唯一目录读取，不复制旧协议的入口/工具校验。
# 函数用途: 解析显式v9声明并委托原文件入口构造。
def manifest_from_v9(cls, payload: dict):
    from .plugin_manifest import files_plugin_manifest_fields
    if (not isinstance(payload, dict) or set(payload) != set(files_plugin_manifest_fields(v8=False))
            or payload["schema_version"] != "plugin_package.v9"):
        raise ValueError("v9清单字段无效")
    validate_capabilities(payload["host_api"], sequence_type=list)
    return cls._from_v6(payload)


# LLM: 新版本只有显式标记才能输出；旧v6字节原样保留。
# 函数用途: 将共用文件入口投影定格为v9。
def serialize_manifest_v9(manifest, payload: dict) -> dict:
    validate_v9_manifest(manifest)
    return {**payload, "schema_version": "plugin_package.v9"}
