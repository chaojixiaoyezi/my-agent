# LLM: 管理员对每个 owner 的控制只存于该 owner 自己的 tool_policy.json 的 admin_controls 块（owner_admin_controls.v1），
#   与审批模式、长期授权同一文件、同一把锁；owner 自己的代理写不进（tool_policy.json 在 owner 受保护控制路径内），
#   只有 is_permission_admin(管理员 home) 的入口（admin_controls 工具）能写。
#   读：文件或块缺失按默认（Jev 与审计允许、跨用户审计不允许）；坏 JSON、schema 不符或值不是布尔一律按"不允许"失败关闭。
#   跨用户审计许可只在管理员自己的策略里有效，其它 owner 名下的同名值恒按 False。
#   改字段须同步 admin_controls 工具、decision_model_call/decision_service 硬门、audit_records 工具与 test_decision_audit_controls。
# 模块用途: 保存并读取管理员给每个用户开关的 Jev 决策使用权、审计工具使用权，以及管理员本人的跨用户审计许可。
from __future__ import annotations

import time
from pathlib import Path

from ..common.json_io import (
    locked_json_path,
    read_json_object_report,
    write_json_file_atomic_unlocked,
)
from .approval_mode import is_permission_admin
from .owner_policy_seed_payloads import default_tool_policy_payload

ADMIN_CONTROLS_FIELD = "admin_controls"
ADMIN_CONTROLS_SCHEMA = "owner_admin_controls.v1"
_TOOL_POLICY_SCHEMA = "tool-policy.v1"
# 没写过就等于管理员没关：Jev 与审计默认允许，跨用户审计默认不许
CONTROL_DEFAULTS = {"decision_model_allowed": True, "audit_allowed": True, "cross_owner_audit_allowed": False}
# 只能写在管理员自己策略里、也只在管理员名下生效的控制项
ADMIN_SELF_CONTROLS = frozenset({"cross_owner_audit_allowed"})
_FAIL_CLOSED = dict.fromkeys(CONTROL_DEFAULTS, False)


# LLM: reason 是稳定机器码（not_admin / invalid_changes / admin_self_only / policy_unreadable），调用方按它映射错误码，不读中文文案。
# 类用途: 表示管理员控制写入被拒绝的具体原因。
class OwnerAdminControlsError(ValueError):
    # 函数用途: 保存拒绝原因码和给人看的说明。
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


# LLM: 只认 home 上宿主解析的 owner_tool_policy_json；替身或未知对象返回 None（按默认处理，不猜路径）。
# 函数用途: 取得某个 owner 的策略文件路径。
def _policy_path(home: object) -> Path | None:
    value = getattr(home, "owner_tool_policy_json", None)
    return Path(value) if value else None


# LLM: 块缺失→默认；块存在但 schema 不符或已知键不是布尔→None（调用方失败关闭）。未知附加键（updated_at 等）忽略。
# 函数用途: 把策略文件里的 admin_controls 块规范成三项布尔值。
def _controls_from_policy(data: dict) -> dict | None:
    block = data.get(ADMIN_CONTROLS_FIELD)
    if block is None:
        return dict(CONTROL_DEFAULTS)
    if not isinstance(block, dict) or block.get("schema") != ADMIN_CONTROLS_SCHEMA:
        return None
    values = {key: block.get(key, default) for key, default in CONTROL_DEFAULTS.items()}
    return values if all(type(value) is bool for value in values.values()) else None


# LLM: 每次调用都读当前文件（不缓存），管理员改完下一次调用即生效；读失败失败关闭并标 source=unreadable。
#   非管理员 owner 的 cross_owner_audit_allowed 恒为 False。只读，不创建文件。
# 函数用途: 读取某个 owner 当前的管理员控制，附来源（default / policy / unreadable）。
def read_owner_admin_controls(home: object) -> dict:
    path = _policy_path(home)
    if path is None:
        return {**CONTROL_DEFAULTS, "source": "default"}
    report = read_json_object_report(path, context="owner.admin_controls.read")
    data = report.payload if report.load_error is None else None
    if data is not None and data and data.get("schema_version") != _TOOL_POLICY_SCHEMA:
        data = None
    values = _controls_from_policy(data) if data is not None else None
    if values is None:
        return {**_FAIL_CLOSED, "source": "unreadable"}
    if not is_permission_admin(home):
        values["cross_owner_audit_allowed"] = False
    source = "policy" if isinstance(data.get(ADMIN_CONTROLS_FIELD), dict) else "default"
    return {**values, "source": source}


# LLM: Jev 调用硬门与设置投影共用此判断；读失败按不允许。
# 函数用途: 判断这个 owner 现在能否使用 Jev 决策模型。
def owner_decision_model_allowed(home: object) -> bool:
    return read_owner_admin_controls(home)["decision_model_allowed"]


# LLM: 审计工具唯一的本人权限判断；读失败按不允许。
# 函数用途: 判断这个 owner 现在能否使用审计工具查自己的记录。
def owner_audit_allowed(home: object) -> bool:
    return read_owner_admin_controls(home)["audit_allowed"]


# LLM: 跨用户审计必须同时满足：当前 home 是本机管理员，且管理员自己的策略里显式开启；缺一即 False。
# 函数用途: 判断 my-agent 现在能否替管理员查询所有用户的审计记录。
def cross_owner_audit_allowed(home: object) -> bool:
    return is_permission_admin(home) and read_owner_admin_controls(home)["cross_owner_audit_allowed"]


# LLM: 只接受已知键与布尔值；cross_owner_audit_allowed 只能写在管理员自己名下（按策略文件路径判断是否同一 owner）。
# 函数用途: 在写入前校验管理员身份与改动内容，不合法时抛带原因码的错误。
def _validate_change(admin_home: object, target_home: object, changes: object) -> dict:
    if not is_permission_admin(admin_home):
        raise OwnerAdminControlsError("not_admin", "只有本机管理员能修改用户的 Jev 与审计开关。")
    if (not isinstance(changes, dict) or not changes or set(changes) - set(CONTROL_DEFAULTS)
            or any(type(value) is not bool for value in changes.values())):
        raise OwnerAdminControlsError("invalid_changes", "只能修改 decision_model_allowed、audit_allowed、"
                                                         "cross_owner_audit_allowed，值必须是 true 或 false。")
    admin_path, target_path = _policy_path(admin_home), _policy_path(target_home)
    if admin_path is None or target_path is None:
        raise OwnerAdminControlsError("policy_unreadable", "找不到用户策略文件位置，未修改。")
    if ADMIN_SELF_CONTROLS & set(changes) and admin_path.resolve() != target_path.resolve():
        raise OwnerAdminControlsError("admin_self_only", "跨用户审计许可只能写在管理员自己名下。")
    return dict(changes)


# LLM: 锁内读改写目标 owner 的 tool_policy.json，其它字段（审批模式、禁用工具、长期授权）原样保留；文件不存在时从默认策略种子建立。
#   基线取当前有效值（坏块时为全 False，避免修一项顺带放开其它项），再合并本次改动；块里写全部相关键和更新时间、操作者。
#   副作用：写目标 owner 策略文件并 chmod 600。坏 JSON 或 schema 不符时拒绝写，不覆盖用户其它策略。
# 函数用途: 管理员修改某个用户的 Jev / 审计开关，或自己的跨用户审计许可，返回修改后的控制值。
def set_owner_admin_controls(admin_home: object, target_home: object, changes: object, *, actor: str) -> dict:
    changes = _validate_change(admin_home, target_home, changes)
    path = _policy_path(target_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked_json_path(path):
        report = read_json_object_report(path, context="owner.admin_controls.write")
        if report.load_error is not None:
            raise OwnerAdminControlsError("policy_unreadable", "用户策略文件无法读取，未修改。")
        data = dict(report.payload or default_tool_policy_payload())
        if data.get("schema_version") != _TOOL_POLICY_SCHEMA:
            raise OwnerAdminControlsError("policy_unreadable", "用户策略文件版本不符，未修改。")
        values = {**(_controls_from_policy(data) or _FAIL_CLOSED), **changes}
        if not is_permission_admin(target_home):
            values.pop("cross_owner_audit_allowed")
        data[ADMIN_CONTROLS_FIELD] = {"schema": ADMIN_CONTROLS_SCHEMA, **values,
                                      "updated_at": time.time(), "updated_by": str(actor or "")}
        write_json_file_atomic_unlocked(path, data)
        path.chmod(0o600)
    return read_owner_admin_controls(target_home)


# LLM: 只接受规范 owner 编号：local/main，或 providers/<provider>/<users|groups>/<id>，provider 与 id 段须原样等于
#   safe_path_segment 的结果（拒绝路径分隔、.. 等）；目录不存在返回 None，不创建冷用户目录。只做路径解析、不授予权限。
# 函数用途: 把管理员给出的 owner 编号解析成那个用户的 home 路径集合。
def owner_home_paths_by_id(base_home: object, owner_id: object):
    from ..common.path_segments import safe_path_segment
    from .owner_resolver import OwnerIdentity, home_paths_with_owner, resolve_owner_home

    text = owner_id.strip() if type(owner_id) is str else ""
    parts = text.split("/")
    if text == "local/main":
        identity = OwnerIdentity.local_main()
    elif (len(parts) == 4 and parts[0] == "providers" and parts[2] in {"users", "groups"}
            and all(part and part == safe_path_segment(part) for part in (parts[1], parts[3]))):
        build = OwnerIdentity.provider_user if parts[2] == "users" else OwnerIdentity.provider_group
        identity = build(parts[1], parts[3])
    else:
        return None
    owner = resolve_owner_home(base_home.root, identity)
    return home_paths_with_owner(base_home, owner) if owner.home_dir.is_dir() else None


# LLM: local/main 在前，其余按 owner_wake_discovery 的规范排序取一页（总数不超过 limit）；只列已存在的 owner 目录，
#   不创建、不读内容。第二个返回值表示还有更多 owner 没列出。
# 函数用途: 列出本机已有的用户（owner）编号及其 home 路径集合，供管理员控制和跨用户审计使用。
def list_owner_home_paths(base_home: object, *, limit: int) -> tuple[list[tuple[str, object]], bool]:
    from ..owner_wake_discovery import discover_owner_home_page
    from .owner_resolver import OwnerIdentity, home_paths_with_owner, resolve_owner_home

    result = []
    main = resolve_owner_home(base_home.root, OwnerIdentity.local_main())
    if main.home_dir.is_dir():
        result.append((main.owner_id, home_paths_with_owner(base_home, main)))
    page = discover_owner_home_page(base_home.owners_dir, limit=max(1, limit - len(result)))
    for target in page.targets:
        owner = resolve_owner_home(base_home.root, target.identity)
        result.append((owner.owner_id, home_paths_with_owner(base_home, owner)))
    return result, page.next_cursor is not None


__all__ = [
    "ADMIN_CONTROLS_FIELD", "ADMIN_CONTROLS_SCHEMA", "CONTROL_DEFAULTS", "OwnerAdminControlsError",
    "cross_owner_audit_allowed", "list_owner_home_paths", "owner_audit_allowed", "owner_decision_model_allowed",
    "owner_home_paths_by_id", "read_owner_admin_controls", "set_owner_admin_controls",
]
