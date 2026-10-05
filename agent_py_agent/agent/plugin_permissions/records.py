# LLM: 新授权是原激活的不可变子记录，旧激活 None 不改字节；解码只验结构，不读授权路径内业务正文。
# 模块用途: 严格核对持久授权与固定代次，避免从缺字段、真值或自然语言猜权限。
from __future__ import annotations

from dataclasses import asdict

from ..common.strict_json import load_strict_json
from .grants import PermissionPath, PermissionSelection


# LLM: 拒绝错误路径身份和超额输入，不在查询时触碰文件系统；启动前另行复核真实路径漂移。
# 函数用途: 从安装表恢复一组有限根的静态身份。
def _rows(value: object) -> tuple[PermissionPath, ...]:
    if not isinstance(value, list) or len(value) > 32:
        raise ValueError("授权路径列表无效")
    rows = tuple(_row(item) for item in value)
    if tuple(item.path for item in rows) != tuple(sorted({item.path for item in rows})):
        raise ValueError("授权根未规范排序或重复")
    return rows


# LLM: JSON bool 不能借 int 子类成为路径身份；这里不重新解释路径或扩大根。
# 函数用途: 核对单个持久路径身份的字段与类型。
def _row(value: object) -> PermissionPath:
    from pathlib import Path

    if not isinstance(value, dict) or set(value) != {"path", "device", "inode", "content_sha256"}:
        raise ValueError("授权路径字段无效")
    path = value["path"]
    if not isinstance(path, str) or not Path(path).is_absolute() or ".." in Path(path).parts or "\x00" in path:
        raise ValueError("授权路径地址无效")
    if any(type(value[key]) is not int or value[key] < 0 for key in ("device", "inode")):
        raise ValueError("授权路径身份无效")
    digest = value["content_sha256"]
    if not isinstance(digest, str) or digest and (len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)):
        raise ValueError("授权程序摘要无效")
    return PermissionPath(**value)


# LLM: 持久授权只接受四个结构维度，桌面或未来字段必须经显式新协议；查询不重读文件内容。
# 函数用途: 将已冻结的 JSON 权限恢复为不可变授权选择。
def selection_from_payload(value: object) -> PermissionSelection:
    if not isinstance(value, dict) or set(value) != {"read_roots", "write_roots", "network", "program_roots"}:
        raise ValueError("持久权限字段无效")
    if type(value["network"]) is not bool:
        raise ValueError("持久网络授权必须是布尔值")
    return PermissionSelection(_rows(value["read_roots"]), _rows(value["write_roots"]),
                               value["network"], _rows(value["program_roots"]))


# LLM: 授权绑定完整计划并参与原激活摘要；None 保持旧协议，不代表旧兼容或隐式授根。
# 函数用途: 拒绝换绑代次、未知授权模式和错误结构。
def validate_activation_permission(activation) -> None:
    if activation.permission_json is None:
        return
    value = load_strict_json(activation.permission_json)
    fields = {"kind", "policy_version", "mode", "plugin_id", "package_version", "installation_ref", "package_sha256",
              "activation_id", "plan", "permissions", "entry_module", "entry", "host_api", "sandbox_status"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("持久授权记录字段无效")
    if (value["kind"] != "plugin_legacy_permissions" or type(value["policy_version"]) is not int
            or value["policy_version"] != 1 or value["mode"] not in {"restricted", "wide"}
            or value["sandbox_status"] != ("platform_sandbox" if value["mode"] == "restricted" else "not_required")):
        raise ValueError("持久授权策略无效")
    if (value["activation_id"] != activation.activation_id or value["plan"] != asdict(activation.plan)
            or value["plugin_id"] != activation.plan.plugin_id or value["package_sha256"] != activation.plan.package_sha256):
        raise ValueError("授权与固定激活计划不同")
    selection_from_payload(value["permissions"])
