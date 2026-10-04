# LLM: 回滚转换先严格验证 v4，仅显式导出 v3；安装、配置、激活代和 enabled 保持，授权损失/摘要修复明确报告。
# 模块用途: 在切回17i之前生成可读取的安装表；不覆盖原权威、不启停插件，收紧边界不会随旧版本保留。
from __future__ import annotations

import hashlib
import json

from ..common.strict_json import load_strict_json
from ..plugin_installation import plugin_input_digest
from ..plugin_installation_state import INSTALLATION_STATE_LIMIT_BYTES, decode_installation_state


# LLM: 输入必须为完整有效 v4；不丢安装、不改 enabled，未知字段/新版本/坏摘要直接失败，调用者应先备份并显式确认授权损失。
# 函数用途: 返回独立 v3 字节与结构化损失报告，不写文件或重新授权。
def export_installations_v3(content: bytes, owner):
    if len(content) > INSTALLATION_STATE_LIMIT_BYTES:
        raise ValueError("安装表超过读取预算")
    payload = load_strict_json(content)
    if not isinstance(payload, dict) or payload.get("schema_version") != "plugin_installations.v4":
        raise ValueError("回滚仅接受完整 v4 安装表；未覆盖原记录")
    state = decode_installation_state(content, owner)
    report = {"source_schema": "plugin_installations.v4", "target_schema": "plugin_installations.v3",
              "source_sha256": hashlib.sha256(content).hexdigest(), "installation_count": len(state.entries),
              "permissions_lost": [], "compatibility_authorizations_lost": [],
              "enabled_preserved": [row.manifest.plugin_id for row in state.entries if row.enabled]}
    rows = [_rollback_row(row, report) for row in payload["installations"]]
    migration = payload["migration"]
    lost = isinstance(migration, dict) and migration["from_schema"] == "plugin_installations.v3"
    report["v4_migration_marker_lost"] = lost
    text = json.dumps({**payload, "schema_version": "plugin_installations.v3", "installations": rows,
                       "migration": migration["previous"] if lost else migration},
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    report["output_sha256"] = hashlib.sha256(text.encode()).hexdigest()
    report["warning"] = "v4收紧/兼容授权记录将丢失；17i不施加新权限，已启用老插件恢复系统用户宽权限。原v4文件必须另行保留备份。"
    return text.encode(), report


# LLM: 去除新字段改变激活内容摘要时，同步修复同一最后回执的摘要；代次仅由 plan 决定且不改变，不伪造新增生命周期。
# 函数用途: 转换一条安装记录并登记授权损失，保留包/配置/版本/激活和已有操作身份。
def _rollback_row(row: dict, report: dict) -> dict:
    row = dict(row)
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
