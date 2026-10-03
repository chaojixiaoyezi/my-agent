# LLM: 权限记录只保存在原安装表；迁移仅接受显式 v3 来源，不把缺字段当豁免。
# 模块用途: 计算一次性兼容与固定代次授权，不启动或终止任何进程。
from __future__ import annotations

import json
from dataclasses import replace

from ..common.strict_json import load_strict_json


# LLM: 只读原安装权威；None 不是旧兼容，调用方不得补猜授权。
# 函数用途: 取得固定安装的权限事实副本。
def legacy_permissions(entry) -> dict | None:
    if entry.activation is None or entry.activation.phase == "revoked":
        return None
    value = getattr(entry.activation, "permission_json", None) or entry.legacy_permission_json
    return load_strict_json(value) if value is not None else None


# LLM: 只供严格旧版本迁移调用；未来缺字段不能调用它取得豁免。
# 函数用途: 将升级前已启用的老格式激活标为一次性兼容。
def migrate_v3_entry(entry):
    if entry.enabled and not entry.manifest.is_content_only and entry.manifest.permissions is None:
        grant = {"policy_version": 1, "mode": "legacy_compat", "source_schema": "plugin_installations.v3",
                 "activation_id": entry.activation_id, "package_sha256": entry.package_sha256,
                 "installation_ref": entry.installation_ref}
        return replace(entry, legacy_permission_json=canonical_permission_json(grant))
    return entry


# LLM: 用规范 JSON 冻结授权，私有记录不输出到模型；这里只编码，不接受不完整权限形状。
# 函数用途: 为安装子记录生成不可变的授权文本。
def canonical_permission_json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


# LLM: 兼容记录只能绑定当前旧活跃激活，缺字段不推导豁免；结构无效由原安装解码拒绝。
# 函数用途: 检查安装中的一次性兼容标记，没有兼容标记时不授任何权限。
def validate_legacy_permission(entry) -> None:
    text = entry.legacy_permission_json
    if text is None:
        return
    value = load_strict_json(text)
    expected = {"policy_version", "mode", "source_schema", "activation_id", "package_sha256", "installation_ref"}
    if (not isinstance(value, dict) or set(value) != expected or value["policy_version"] != 1
            or value["mode"] != "legacy_compat" or value["source_schema"] != "plugin_installations.v3"):
        raise ValueError("旧权限迁移记录无效")
    if (not entry.enabled or entry.manifest.is_content_only or entry.manifest.permissions is not None
            or value["activation_id"] != entry.activation_id or value["package_sha256"] != entry.package_sha256
            or value["installation_ref"] != entry.installation_ref or text != canonical_permission_json(value)):
        raise ValueError("旧权限与激活身份不符")
