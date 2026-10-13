# LLM: 严格验证 v4/v5，仅显式导出全私有表为 v3；公共范围不可在旧协议中表达，拒绝整表而不丢记录或私有化。
# 模块用途: 在切回17i之前生成可读取的安装表；不覆盖原权威、不启停插件，收紧边界不会随旧版本保留。
from __future__ import annotations

import hashlib
import json

from ..common.strict_json import load_strict_json
from ..plugin_installation import plugin_input_digest
from ..plugin_installation_state import INSTALLATION_STATE_LIMIT_BYTES, decode_installation_state


# LLM: 输入必须为完整有效 v4/v5 且全私有；不丢安装、不改 enabled，公共表/未知版本/坏摘要失败，确认继续绑定全部损失事实。
# 函数用途: 返回独立 v3 字节与结构化损失报告，不写文件或重新授权。
def export_installations_v3(content: bytes, owner):
    if len(content) > INSTALLATION_STATE_LIMIT_BYTES:
        raise ValueError("安装表超过读取预算")
    payload = load_strict_json(content)
    if not isinstance(payload, dict) or payload.get("schema_version") not in {"plugin_installations.v4", "plugin_installations.v5"}:
        raise ValueError("回滚仅接受完整 v4/v5 私有安装表；未覆盖原记录")
    state = decode_installation_state(content, owner)
    if any(row.installation_scope != "private" for row in state.entries):
        raise ValueError("旧协议不能表达公共安装范围，不能导出或静默私有化；未覆盖原记录")
    report = {"source_schema": payload["schema_version"], "target_schema": "plugin_installations.v3",
              "source_sha256": hashlib.sha256(content).hexdigest(), "installation_count": len(state.entries),
              "permissions_lost": [], "compatibility_authorizations_lost": [],
              "enabled_preserved": [row.manifest.plugin_id for row in state.entries if row.enabled]}
    rows = [_rollback_row(row, report) for row in payload["installations"]]
    migration = payload["migration"]
    scope_marker = isinstance(migration, dict) and migration["from_schema"] == "plugin_installations.v4"
    if scope_marker:
        migration = migration["previous"]
    report["v5_scope_migration_marker_lost"] = scope_marker
    lost = isinstance(migration, dict) and migration["from_schema"] == "plugin_installations.v3"
    report["v4_migration_marker_lost"] = lost
    text = json.dumps({**payload, "schema_version": "plugin_installations.v3", "installations": rows,
                       "migration": migration["previous"] if lost else migration},
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    report["output_sha256"] = hashlib.sha256(text.encode()).hexdigest()
    report["warning"] = "新表的收紧/兼容授权记录将丢失；17i不施加新权限，已启用老插件恢复系统用户宽权限。原安装表必须另行保留备份。"
    return text.encode(), report


# LLM: 仅全私有表进入此处；删除可等价表达的 private 字段及权限字段，改变摘要时修复原回执，不伪造生命周期。
# 函数用途: 转换一条安装记录并登记授权损失，保留包/配置/版本/激活和已有操作身份。
def _rollback_row(row: dict, report: dict) -> dict:
    row = dict(row)
    row.pop("installation_scope", None)
    if row.pop("legacy_permission_grant") is not None:
        report["compatibility_authorizations_lost"].append(row["manifest"]["plugin_id"])
    activation = row["activation"]
    if not isinstance(activation, dict) or "permission_grant" not in activation:
        return row
    activation = dict(activation)
    grant = activation.pop("permission_grant")
    report["permissions_lost"].append({"plugin_id": row["manifest"]["plugin_id"], "mode": grant["mode"]})
    row["activation"] = activation
    receipt = dict(row["last_commit"])
    receipt["activation_sha256"] = hashlib.sha256(json.dumps(activation, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    receipt["input_digest"] = plugin_input_digest(receipt["action"], receipt["plugin_id"], receipt["package_sha256"],
        receipt["before_revision"], receipt["settings_sha256"], activation_sha256=receipt["activation_sha256"])
    row["last_commit"] = receipt
    return row
