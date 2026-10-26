# LLM: 所有跨owner查询只投影白名单，态势筛选复用已登记owner校验；不执行资产存储构造器。
# 模块用途: 聚合成员可见的账号、资产、告警与态势。
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .access_policy import AuditGroupError, require_audit_group_member
from .observation_projection import collect_alerts, collect_assets, page
from .owner_catalog import registered_owners
from .readonly_store import KINDS


# LLM: owner筛选仅在已登记清单中选择，不接受任意路径或未知目标，清单错误由响应保留。
# 函数用途: 解析本次查询的目标owner集合。
def selected_owners(root, owner_ids, target_owner):
    registered, errors = registered_owners(root)
    known = {row["owner_id"] for row in registered}
    if owner_ids is not None and (not isinstance(owner_ids, (list, tuple)) or len(owner_ids) > 2000
                                  or any(type(owner) is not str for owner in owner_ids)):
        raise AuditGroupError("INVALID_OWNER_SELECTION", status_code=400)
    requested = [target_owner] if target_owner else list(owner_ids) if owner_ids is not None else sorted(known)
    if any(owner not in known for owner in requested):
        raise AuditGroupError("OWNER_NOT_FOUND", status_code=404)
    return list(dict.fromkeys(requested)), errors


# LLM: 时间过滤按带时区ISO的真实时刻比较，拒绝naive或损坏值，不能按字典序把偏移时区误判。
# 函数用途: 把调用方时间窗或安全告警时间转换为可比较的epoch。
def alert_timestamp(value):
    try:
        if type(value) is not str or not 1 <= len(value) <= 128:
            raise ValueError()
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if moment.utcoffset() is None:
            raise ValueError()
        return moment.timestamp()
    except (ValueError, OverflowError):
        raise AuditGroupError("INVALID_ALERT_TIME", status_code=400) from None


# LLM: 来源时间损坏保留partial来源错误，而不是静默洗成完整空列表；不回显原值。
# 函数用途: 应用告警字段和真实时间窗筛选。
def filter_alerts(rows, severity, status, since, errors):
    cutoff = alert_timestamp(since) if since else None
    selected = []
    for row in rows:
        if severity and row["severity"] != severity or status and row["status"] != status:
            continue
        try:
            if cutoff is None or alert_timestamp(row["created_at"]) >= cutoff:
                selected.append(row)
        except AuditGroupError:
            errors.append({"owner_id": row["owner_id"], "code": "INVALID_ALERT_TIME"})
    return selected


# LLM: 冷绑定宿主root，态势过滤与资产/告警共用选择器，不初始化任何source目录或缓存成员资格。
# 类用途: 提供跨用户的只读安全摘要。
class AuditGroupObservationService:
    # LLM: root只能来自宿主构造，不从请求替换。
    # 函数用途: 绑定观察范围。
    def __init__(self, root_dir):
        self.root_dir = Path(root_dir).resolve()

    # LLM: 每次核验caller后读取已登记owner。
    # 函数用途: 分页列出安全账号概况。
    def list_owners(self, caller_owner, *, cursor="", limit=50, include_feishu=True, include_local=True):
        require_audit_group_member(self.root_dir, caller_owner)
        if type(include_feishu) is not bool or type(include_local) is not bool:
            raise AuditGroupError("INVALID_OWNER_SELECTION", status_code=400)
        rows, errors = registered_owners(self.root_dir, include_local=include_local, include_feishu=include_feishu)
        for row in rows:
            assets, failures = collect_assets(self.root_dir, [row["owner_id"]], KINDS[:4])
            alerts, alert_errors = collect_alerts(self.root_dir, [row["owner_id"]])
            errors.extend([*failures, *alert_errors])
            row["asset_counts"] = {kind: sum(item["kind"] == kind for item in assets) for kind in KINDS[:4]}
            row["active_alert_count"] = len(alerts)
        require_audit_group_member(self.root_dir, caller_owner)
        return page(rows, cursor, limit, errors, query=[include_feishu, include_local])

    # LLM: 只返回资产白名单，不包含源码和提示词。
    # 函数用途: 合并选中owner的资产概况。
    def summarize_assets(self, caller_owner, *, owner_ids=None, target_owner="", kinds=None, cursor="", limit=50):
        require_audit_group_member(self.root_dir, caller_owner)
        owners, errors = selected_owners(self.root_dir, owner_ids, target_owner)
        kinds = KINDS[:4] if kinds is None else kinds
        if not isinstance(kinds, (tuple, list)) or any(kind not in KINDS for kind in kinds):
            raise AuditGroupError("INVALID_ASSET_KIND", status_code=400)
        rows, failures = collect_assets(self.root_dir, owners, tuple(dict.fromkeys(kinds)))
        require_audit_group_member(self.root_dir, caller_owner)
        return page(rows, cursor, limit, [*errors, *failures], query=[owners, kinds])

    # LLM: 读取失败和来源坏时间保留覆盖缺口，since按真实时刻过滤，不回显原始运行输出。
    # 函数用途: 分页合并监控异常摘要及时间窗。
    def list_alerts(self, caller_owner, *, owner_ids=None, target_owner="", severity="", status="", since="", cursor="", limit=50):
        require_audit_group_member(self.root_dir, caller_owner)
        owners, errors = selected_owners(self.root_dir, owner_ids, target_owner)
        rows, failures = collect_alerts(self.root_dir, owners)
        rows = filter_alerts(rows, severity, status, since, failures)
        require_audit_group_member(self.root_dir, caller_owner)
        return page(rows, cursor, limit, [*errors, *failures], query=[owners, severity, status, since])

    # LLM: 未传筛选沿原全局统计，显式空集合不扩大为全量；仅选已登记owner，重复来源错误只记一次。
    # 函数用途: 汇总全局或指定用户的态势计数，首末重核成员并保留partial事实。
    def posture_overview(self, caller_owner, *, owner_ids=None, target_owner=""):
        require_audit_group_member(self.root_dir, caller_owner)
        owners, errors = registered_owners(self.root_dir)
        if owner_ids is not None or target_owner:
            selected, selection_errors = selected_owners(self.root_dir, owner_ids, target_owner)
            selected = set(selected)
            owners = [row for row in owners if row["owner_id"] in selected]
            errors.extend(error for error in selection_errors if error not in errors)
        ids = [row["owner_id"] for row in owners]
        assets, asset_errors = collect_assets(self.root_dir, ids, KINDS[:4])
        alerts, alert_errors = collect_alerts(self.root_dir, ids)
        result = page([], "", 50, [*errors, *asset_errors, *alert_errors], query=[])
        result.pop("items")
        result.pop("next_cursor")
        require_audit_group_member(self.root_dir, caller_owner)
        return {**result, "owners_total": len(owners), "enabled_owners": sum(row["status"] == "enabled" for row in owners),
                "audit_group_members": sum(row["in_audit_group"] for row in owners),
                "assets_by_kind": {kind: sum(row["kind"] == kind for row in assets) for kind in KINDS[:4]},
                "active_monitors": sum(row["enabled"] and row["kind"] == "monitor" for row in assets),
                "alerts_summary": {"total": len(alerts), "error": len(alerts)}}
