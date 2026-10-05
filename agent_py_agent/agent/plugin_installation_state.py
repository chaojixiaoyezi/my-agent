# LLM: 唯一安装表严格解码 v4；仅显式旧 v3 的已启用激活迁移一次性兼容，查询不写盘，下一次事务保存来源。
# 模块用途: 保留安装、配置和激活的同一权威及明确迁移来源，拒绝丢字段降级。

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass

from .common.strict_json import load_strict_json
from .plugin_installation import PluginInstallation, PluginInstallationError

INSTALLATION_SCHEMA = "plugin_installations.v4"
# 插件安装状态文件上限 16 MiB：防损坏/恶意状态文件撑爆内存。
INSTALLATION_STATE_LIMIT_BYTES = 16 * 1024 * 1024
_V1 = "plugin_installations.v1"
_V2 = "plugin_installations.v2"
_V3 = "plugin_installations.v3"


# LLM: migration_json 是原文件来源事实，不是另一个状态表；记录内有配置，默认 repr 不可用于日志。
# 类用途: 将完整安装清单和已有迁移标记交给同一次原子写入。
@dataclass(frozen=True, repr=False)
class PluginInstallationState:
    entries: tuple[PluginInstallation, ...] = ()
    migration_json: str = "null"


# LLM: 旧协议严格按版本迁移；v4 缺权限字段直接拒绝，不根据字段缺失补兼容；旧有效字节仅查询时不改盘。
# 函数用途: 恢复 owner 的私有记录，损坏或跨用户状态不覆盖。
def decode_installation_state(content: bytes | None, owner) -> PluginInstallationState:
    if content is None:
        return PluginInstallationState()
    payload = load_strict_json(content)
    if not isinstance(payload, dict):
        raise ValueError("安装表协议无效")
    version = payload.get("schema_version")
    fields = {"schema_version", "owner", "installations"}
    if version in {INSTALLATION_SCHEMA, _V2, _V3}:
        fields.add("migration")
    elif version != _V1:
        raise ValueError("安装表协议无效")
    if set(payload) != fields or not isinstance(payload["installations"], list):
        raise ValueError("安装表字段无效")
    if payload["owner"] != asdict(owner):
        raise PluginInstallationError("owner_mismatch", "安装表不属于当前用户。")
    if version in {_V1, _V2}:
        rows = tuple(_migrate_record(row, version) for row in payload["installations"])
        migration = {"from_schema": version, "source_sha256": hashlib.sha256(content).hexdigest()}
        if version == _V2:
            previous = payload["migration"]
            _validate_migration(previous)
            if previous is not None and previous["from_schema"] != _V1:
                raise ValueError("旧安装迁移来源无效")
            migration["previous"] = previous
    elif version == _V3:
        rows = tuple(_migrate_v3_record(row) for row in payload["installations"])
        _validate_migration(payload["migration"])
        migration = {"from_schema": _V3, "source_sha256": hashlib.sha256(content).hexdigest(),
                     "previous": payload["migration"]}
    else:
        rows = tuple(PluginInstallation.from_payload(row) for row in payload["installations"])
        migration = payload["migration"]
    _validate_migration(migration)
    if len({item.manifest.plugin_id for item in rows}) != len(rows):
        raise ValueError("插件身份重复")
    if len({item.last_commit.operation_id for item in rows}) != len(rows):
        raise ValueError("插件提交身份重复")
    return PluginInstallationState(rows, json.dumps(migration, sort_keys=True, separators=(",", ":")))


# LLM: v4 起顶层多一个 legacy_permission_grant；旧表迁移忽略它的存在与取值（迁移产物强制 None），
#   既兼容"由新格式删字段模拟出的旧表"，也不给新权限任何 grandfather 入口。
# 函数用途: 旧版本记录字段集检查：除可忽略的顶层 legacy_permission_grant 外必须恰好相等。
def _legacy_fields_match(value: object, expected: set[str]) -> bool:
    return isinstance(value, dict) and set(value) - {"legacy_permission_grant"} == expected


# LLM: 旧 v1/v2 都只接纳停用且空激活；旧回执摘要保持原值，补字段仅存在于这个明确迁移入口。
# 函数用途: 将严格合法的旧安装/配置记录转换成等价 v3 内存对象，不写文件。
def _migrate_record(value: object, version: str) -> PluginInstallation:
    expected = {"manifest", "package_sha256", "revision", "last_commit", "enabled", "activation_id"}
    if version == _V2:
        expected |= {"settings_json", "settings_revision"}
    if not _legacy_fields_match(value, expected):
        raise ValueError("旧安装记录字段无效")
    if value["enabled"] is not False or value["activation_id"] != "":
        raise ValueError("旧安装协议不能声明激活")
    receipt = value["last_commit"]
    receipt_fields = {"operation_id", "input_digest", "plugin_id", "package_sha256", "before_revision", "after_revision"}
    if version == _V2:
        receipt_fields |= {"action", "settings_sha256"}
    if not isinstance(receipt, dict) or set(receipt) != receipt_fields:
        raise ValueError("旧安装回执字段无效")
    receipt = {**receipt, "activation_sha256": ""}
    if version == _V1:
        receipt.update(action="install", settings_sha256="")
    if receipt["action"] not in {"install", "configure"}:
        raise ValueError("旧安装回执不能声明激活动作")
    return PluginInstallation.from_payload({
        **{key: value[key] for key in ("manifest", "package_sha256", "revision")},
        "settings_json": value["settings_json"] if version == _V2 else None,
        "settings_revision": value["settings_revision"] if version == _V2 else 0,
        "last_commit": receipt, "activation": None, "legacy_permission_grant": None,
    })


# LLM: v3 来源保存原字节摘要与至多旧两代链；迁移结构按版本递减，不无限递归、不接受 v4 缺字段旁路。
# 函数用途: 校验唯一表中至多两代的完整迁移链。
def _validate_migration(value: object) -> None:
    if value is None:
        return
    if (not isinstance(value, dict) or not {"from_schema", "source_sha256"} <= set(value)
            or not isinstance(value["source_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["source_sha256"])):
        raise ValueError("安装迁移记录无效")
    if value["from_schema"] == _V1 and set(value) == {"from_schema", "source_sha256"}:
        return
    if value["from_schema"] == _V2 and set(value) == {"from_schema", "source_sha256", "previous"}:
        previous = value["previous"]
        if previous is None or isinstance(previous, dict) and previous.get("from_schema") == _V1:
            _validate_migration(previous)
            return
    if value["from_schema"] == _V3 and set(value) == {"from_schema", "source_sha256", "previous"}:
        previous = value["previous"]
        if previous is None or isinstance(previous, dict) and previous.get("from_schema") in {_V1, _V2}:
            _validate_migration(previous)
            return
    raise ValueError("安装迁移记录无效")


# LLM: 只接受严格旧字段和原进程激活字节；新权限不能伪装成旧来源获得 grandfather。
# 函数用途: 迁移完整 v3 记录，仅旧 active 老格式进程得到一次性兼容。
def _migrate_v3_record(value: object) -> PluginInstallation:
    from .plugin_permissions.state import migrate_v3_entry

    old_fields = {"manifest", "package_sha256", "revision", "last_commit", "settings_json", "settings_revision", "activation"}
    if not _legacy_fields_match(value, old_fields):
        raise ValueError("旧 v3 安装记录字段无效")
    activation = value["activation"]
    if isinstance(activation, dict) and "permission_grant" in activation:
        raise ValueError("旧 v3 激活不能携带新授权")
    entry = PluginInstallation.from_payload({**value, "legacy_permission_grant": None})
    return migrate_v3_entry(entry)


# LLM: 私有配置只在此 owner 表编码，完整文件大小在提交前检查；不能将返回文本用于公开目录或结果。
# 函数用途: 为唯一安装表生成包含迁移来源的有界 JSON。
def encode_installation_state(entries: tuple[PluginInstallation, ...], owner, migration_json: str) -> str:
    migration = load_strict_json(migration_json)
    _validate_migration(migration)
    text = json.dumps({
        "schema_version": INSTALLATION_SCHEMA, "owner": asdict(owner),
        "installations": [entry.to_payload() for entry in entries], "migration": migration,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    if len(text.encode("utf-8")) > INSTALLATION_STATE_LIMIT_BYTES:
        raise PluginInstallationError("state_limit", "安装表超过存储预算。")
    return text
