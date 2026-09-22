# LLM: 这是唯一安装表的纯编解码，不执行迁移写入；v1 来源摘要随下一次真实提交写入 v2，不接受隐式协议别名。
# 模块用途: 严格读取安装表并记录明确迁移来源，让配置与安装共享一个私有持久权威。

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass

from .common.strict_json import load_strict_json
from .plugin_installation import PluginInstallation, PluginInstallationError

INSTALLATION_SCHEMA = "plugin_installations.v2"
INSTALLATION_STATE_LIMIT = 16 * 1024 * 1024
_PREVIOUS_SCHEMA = "plugin_installations.v1"


# LLM: migration_json 是原文件来源事实，不是另一个状态表；记录内有配置，默认 repr 不可用于日志。
# 类用途: 将完整安装清单和已有迁移标记交给同一次原子写入。
@dataclass(frozen=True, repr=False)
class PluginInstallationState:
    entries: tuple[PluginInstallation, ...] = ()
    migration_json: str = "null"


# LLM: 只有明确 v1 进入迁移解析，严格检查其原字段再映射；v2 缺字段不能借旧路径补默认值。
# 函数用途: 恢复 owner 的私有记录，损坏或跨用户状态不覆盖。
def decode_installation_state(content: bytes | None, owner) -> PluginInstallationState:
    if content is None:
        return PluginInstallationState()
    payload = load_strict_json(content)
    if not isinstance(payload, dict):
        raise ValueError("安装表协议无效")
    version = payload.get("schema_version")
    fields = {"schema_version", "owner", "installations"}
    if version == INSTALLATION_SCHEMA:
        fields.add("migration")
    elif version != _PREVIOUS_SCHEMA:
        raise ValueError("安装表协议无效")
    if set(payload) != fields or not isinstance(payload["installations"], list):
        raise ValueError("安装表字段无效")
    if payload["owner"] != asdict(owner):
        raise PluginInstallationError("owner_mismatch", "安装表不属于当前用户。")
    if version == _PREVIOUS_SCHEMA:
        rows = tuple(_migrate_v1_record(row) for row in payload["installations"])
        migration = {"from_schema": version, "source_sha256": hashlib.sha256(content).hexdigest()}
    else:
        rows = tuple(PluginInstallation.from_payload(row) for row in payload["installations"])
        migration = payload["migration"]
    _validate_migration(migration)
    if len({item.manifest.plugin_id for item in rows}) != len(rows):
        raise ValueError("插件身份重复")
    if len({item.last_commit.operation_id for item in rows}) != len(rows):
        raise ValueError("插件提交身份重复")
    return PluginInstallationState(rows, json.dumps(migration, sort_keys=True, separators=(",", ":")))


# LLM: v1 只接纳未配置停用安装，旧回执的摘要保持原值；字段增补仅存在于这一显式迁移边界。
# 函数用途: 将严格合法的旧安装记录转换成等价 v2 内存对象，不写文件。
def _migrate_v1_record(value: object) -> PluginInstallation:
    if not isinstance(value, dict) or set(value) != {
        "manifest", "package_sha256", "revision", "last_commit", "enabled", "activation_id",
    }:
        raise ValueError("旧安装记录字段无效")
    receipt = value["last_commit"]
    if not isinstance(receipt, dict) or set(receipt) != {
        "operation_id", "input_digest", "plugin_id", "package_sha256", "before_revision", "after_revision",
    }:
        raise ValueError("旧安装回执字段无效")
    return PluginInstallation.from_payload({
        **value, "settings_json": None, "settings_revision": 0,
        "last_commit": {**receipt, "action": "install", "settings_sha256": ""},
    })


# LLM: 来源摘要仅证明该次显式读取的旧表字节，不是可信签名；未知迁移结构不能静默保留或忽略。
# 函数用途: 校验原子表中迁移标记的完整字段。
def _validate_migration(value: object) -> None:
    if value is None:
        return
    if (not isinstance(value, dict) or set(value) != {"from_schema", "source_sha256"}
            or value["from_schema"] != _PREVIOUS_SCHEMA
            or not isinstance(value["source_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["source_sha256"])):
        raise ValueError("安装迁移记录无效")


# LLM: 私有配置只在此 owner 表编码，完整文件大小在提交前检查；不能将返回文本用于公开目录或结果。
# 函数用途: 为唯一安装表生成包含迁移来源的有界 JSON。
def encode_installation_state(entries: tuple[PluginInstallation, ...], owner, migration_json: str) -> str:
    migration = load_strict_json(migration_json)
    _validate_migration(migration)
    text = json.dumps({
        "schema_version": INSTALLATION_SCHEMA, "owner": asdict(owner),
        "installations": [entry.to_payload() for entry in entries], "migration": migration,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    if len(text.encode("utf-8")) > INSTALLATION_STATE_LIMIT:
        raise PluginInstallationError("state_limit", "安装表超过存储预算。")
    return text
