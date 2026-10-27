# LLM: local_users为账号状态唯一权威；读取不物化目录，坏文件不回退为不存在或enabled。
# 模块用途: 保存普通本地账号、原始操作绑定与发布回执。
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..common.json_io import locked_json_path
from ..common.nofollow_fs import (
    ensure_private_dir,
    open_directory_beneath,
    read_bytes_beneath,
    write_bytes_atomic_beneath,
)
from ..plugin_host.errors import HostAPIError
from ..plugin_host.owner_context import reject_constant, unique_object, validate_local_user_id
from .owner_resolver import OwnerIdentity, resolve_owner_home


# LLM: 密码慢散列只进入私有序列化且不进入repr/public_view；配额仍以owner quota.json为准。
# 类用途: 保存普通账号、纯Web约束与审计组状态，区分私有和公开字段。
@dataclass(frozen=True)
class LocalUser:
    user_id: str
    status: str
    revision: int
    display_name: str
    created_at: str
    updated_at: str
    quota_revision: int
    has_running_work: bool | None = None
    password_hash: str | None = field(default=None, repr=False)
    password_updated_at: str | None = None
    in_audit_group: bool = False
    allowed_channels: tuple[str, ...] = ("web",)
    im_binding_allowed: bool = False
    audit_group_updated_at: str = ""
    audit_group_updated_by: str = ""

    # LLM: 普通账号通道策略固定且不由旧JSON或dataclass.replace放宽，无IO。
    # 函数用途: 将所有受管账号固定成纯Web账号，兼容旧通道字段。
    def __post_init__(self):
        object.__setattr__(self, "allowed_channels", ("web",))
        object.__setattr__(self, "im_binding_allowed", False)

    # LLM: 账号身份总是普通local/user，不接受插件提供provider或kind。
    # 函数用途: 构造与注册编号一一对应的宿主身份。
    @property
    def owner(self):
        return OwnerIdentity.local_user(self.user_id)

    # LLM: 包含密码散列，仅供0600私有存储；公开调用方必须使用public_view。
    # 函数用途: 序列化账号的完整私有字段。
    def to_dict(self):
        result = asdict(self)
        result["owner"] = asdict(self.owner)
        return result

    # LLM: 复用原严格账号解析，不接受未知状态或坏密码字段，不物化目录。
    # 函数用途: 从私有JSON恢复完整普通账号。
    @classmethod
    def from_dict(cls, payload):
        return parse_local_user(payload, validate_local_user_id(payload.get("user_id")))

    # LLM: 明确剔除散列并只给是否设密及更新时间，不输出秘密或宿主路径。
    # 函数用途: 为插件和宿主命令提供安全账号投影。
    def public_view(self):
        result = self.to_dict()
        result.pop("password_hash")
        result["has_password"] = bool(self.password_hash)
        return result


# LLM: 原严格账号解析新增审计组布尔和审计时间；旧记录默认非成员，旧通道声明不能授权IM。
# 函数用途: 完整恢复私有账号的已知字段，损坏时由存储层失败关闭。
def parse_local_user(value, expected_user_id):
    required = {"user_id", "status", "revision", "display_name", "created_at", "updated_at", "quota_revision"}
    if type(value) is not dict or not required <= set(value):
        raise ValueError("invalid account")
    if value["user_id"] != expected_user_id:
        raise ValueError("account identity changed")
    if value["status"] not in {"provisioning", "provision_failed", "enabled", "disabled"}:
        raise ValueError("invalid account status")
    for field in ("revision", "quota_revision"):
        if type(value[field]) is not int or value[field] < 1:
            raise ValueError("invalid account revision")
    for field in ("display_name", "created_at", "updated_at"):
        if type(value[field]) is not str:
            raise ValueError("invalid account text")
    for field in ("password_hash", "password_updated_at"):
        if value.get(field) is not None and type(value[field]) is not str:
            raise ValueError("invalid credential field")
    if value.get("has_running_work") is not None and type(value["has_running_work"]) is not bool:
        raise ValueError("invalid runtime status")
    if type(value.get("in_audit_group", False)) is not bool:
        raise ValueError("invalid audit group membership")
    if any(type(value.get(key, "")) is not str for key in ("audit_group_updated_at", "audit_group_updated_by")):
        raise ValueError("invalid audit group metadata")
    return LocalUser(value["user_id"], value["status"], value["revision"], value["display_name"],
                     value["created_at"], value["updated_at"], value["quota_revision"],
                     has_running_work=value.get("has_running_work"),
                     password_hash=value.get("password_hash"), password_updated_at=value.get("password_updated_at"),
                     in_audit_group=value.get("in_audit_group", False),
                     audit_group_updated_at=value.get("audit_group_updated_at", ""),
                     audit_group_updated_by=value.get("audit_group_updated_by", ""))


# LLM: 固定可信宿主根，账号写入由生命周期服务持短锁完成；只读不获取会创建文件的锁。
# 类用途: 提供无链接、有界、原子账号存储和操作索引。
class LocalUserStore:
    # LLM: 只规范化可信宿主输入，不产生文件或推断角色。
    # 函数用途: 绑定账号存储根目录。
    def __init__(self, root):
        self.root = Path(root).resolve()

    # LLM: 复用统一resolver而非另造local路径；不调用ensure_owner_home或物化目录。
    # 函数用途: 冷投影普通本地用户的唯一规范目录。
    def owner_home(self, user_id):
        uid = validate_local_user_id(user_id)
        return self.owner_home_for_identity(OwnerIdentity.local_user(uid))

    # LLM: 仅解析可信身份，不能用路径结果代替账号状态或权限核验。
    # 函数用途: 给双来源认证取得同一套规范owner路径。
    def owner_home_for_identity(self, identity):
        return resolve_owner_home(self.root, identity)

    # LLM: 只枚举local_users直接普通JSON文件，no-follow打开父目录，不把链接或非法文件名当账号。
    # 函数用途: 列出已登记的本地普通用户编号，不创建存储。
    def list_user_ids(self):
        try:
            descriptor = open_directory_beneath(self.root, ("local_users",), require_dir_fd=True)
        except FileNotFoundError:
            return []
        try:
            result = []
            with os.scandir(descriptor) as entries:
                for entry in entries:
                    if not entry.name.endswith(".json") or not entry.is_file(follow_symlinks=False):
                        continue
                    try:
                        result.append(validate_local_user_id(entry.name[:-5]))
                    except HostAPIError:
                        continue
            return sorted(result)
        finally:
            os.close(descriptor)

    # LLM: 通过原严格读口复核每条账号；坏记录失败关闭，不能静默隐藏治理故障。
    # 函数用途: 返回当前已登记本地账号的完整内部对象。
    def list_users(self):
        return [user for uid in self.list_user_ids() if (user := self.get(uid)) is not None]

    # LLM: 与get共用唯一账号JSON及原no-follow校验，不创建第二套存储。
    # 函数用途: 为统一认证读取指定本地账号。
    def read_user(self, user_id):
        return self.get(user_id)

    # LLM: 准确编号先校验；损坏、越界、硬链接均关闭而非视为缺失。
    # 函数用途: 读取当前账号公开状态。
    def get(self, user_id):
        record = self.read_record(user_id)
        return parse_local_user(record["user"], user_id) if record is not None else None

    # LLM: 不扫描全盘，只读管理/创建各至多100项的并集，结果排序且无分页泄漏。
    # 函数用途: 列出授权管理范围内已登记的账号。
    def list_registered(self, user_ids):
        if len(user_ids) > 200:
            raise HostAPIError("INVALID_SCHEMA")
        ids = tuple(sorted({validate_local_user_id(uid) for uid in user_ids}))
        return tuple(user for uid in ids if (user := self.get(uid)) is not None)

    # LLM: 文件根固定，单次读取最多2MiB，账户身份与schema双重核验。
    # 函数用途: 读取生命周期内部权威记录。
    def read_record(self, user_id):
        uid = validate_local_user_id(user_id)
        record = self.read_json(("local_users", f"{uid}.json"))
        if record is None:
            return None
        try:
            if record.get("schema") != "local-user.v1":
                raise ValueError("invalid schema")
            user = parse_local_user(record["user"], uid)
            if type(record["provision"]) is not dict or type(record["operations"]) is not dict:
                raise ValueError("invalid receipt")
            if user.status in {"enabled", "disabled"}:
                provision = record["provision"]
                receipt = record["operations"].get(provision.get("operation_id"))
                if provision.get("phase") != "committed" or receipt is None:
                    raise ValueError("uncommitted account")
                if parse_local_user(receipt, uid).status != "enabled":
                    raise ValueError("invalid provisioning receipt")
        except (ValueError, KeyError, TypeError) as exc:
            raise HostAPIError("STORE_UNAVAILABLE", 503) from exc
        return record

    # LLM: 仅允许服务构造的相对组件；重复字段、非有限数及过深JSON均视为损坏。
    # 函数用途: 读取账号或不可变操作索引JSON。
    def read_json(self, parts):
        try:
            data = read_bytes_beneath(self.root, parts, max_bytes=2097152, require_dir_fd=True)
            if data is None:
                return None
            value = json.loads(data, object_pairs_hook=unique_object, parse_constant=reject_constant)
            if type(value) is not dict:
                raise ValueError("invalid record")
            return value
        except (OSError, ValueError, TypeError, RecursionError) as exc:
            raise HostAPIError("STORE_UNAVAILABLE", 503) from exc

    # LLM: 调用方必须先持操作及用户锁；只做原子替换和耐久同步，不另取反向锁。
    # 函数用途: 写入一份完整记录，拒绝大于读回上限的数据。
    def write_json(self, parts, value):
        try:
            data = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
            if len(data) > 2097152:
                raise ValueError("record too large")
            write_bytes_atomic_beneath(self.root, parts, data)
        except (OSError, ValueError, TypeError) as exc:
            raise HostAPIError("STORE_UNAVAILABLE", 503) from exc

    # LLM: 与读路径同一UID，账号发布和终态回执在一份原子文件提交。
    # 函数用途: 保存一个用户的生命周期记录。
    def save(self, user_id, record):
        uid = validate_local_user_id(user_id)
        self.write_json(("local_users", f"{uid}.json"), record)

    # LLM: 操作锁在用户锁外，二者均在quota/JSON锁外；不在锁内等待模型、批准或进程。
    # 函数用途: 用现有跨线程跨进程锁保护短用户事务。
    @contextmanager
    def locked(self, parts):
        try:
            ensure_private_dir(self.root)
            parent = self.root.joinpath(*parts[:-1])
            ensure_private_dir(parent, canonical_root=self.root)
            before = self.directory_identity(parts[:-1])
            with locked_json_path(self.root.joinpath(*parts)):
                if self.directory_identity(parts[:-1]) != before:
                    raise HostAPIError("STORE_UNAVAILABLE", 503)
                yield
        except OSError as exc:
            raise HostAPIError("STORE_UNAVAILABLE", 503) from exc

    # LLM: 以严格dir_fd打开完整链，返回真实目录dev/inode；不跟随链接或创建目录。
    # 函数用途: 绑定用户物化目录身份，检测删除重建或调包。
    def directory_identity(self, parts):
        descriptor = open_directory_beneath(self.root, parts, require_dir_fd=True)
        try:
            info = os.fstat(descriptor)
            return [info.st_dev, info.st_ino]
        finally:
            os.close(descriptor)

    # LLM: 注册表enabled还需原目录身份和独立维护状态共同允许。
    # 函数用途: 在执行边界读取当前有效用户并核对修订。
    def require_enabled(self, user_id, revision=None):
        record = self.read_record(user_id)
        if record is None:
            raise HostAPIError("USER_NOT_FOUND", 404)
        user = parse_local_user(record["user"], user_id)
        if user.status != "enabled":
            raise HostAPIError("USER_DISABLED", 403)
        if revision is not None and revision != user.revision:
            raise HostAPIError("USER_REVISION_CHANGED", 409)
        try:
            identity = self.directory_identity(("owners", "local", user_id))
            if identity != record["provision"]["home_identity"]:
                raise ValueError("directory changed")
            status = self.read_json(("owners", "local", user_id, "owner_status.json"))
            if status is not None and status.get("status") != "active":
                raise HostAPIError("USER_DISABLED", 403)
        except HostAPIError:
            raise
        except (OSError, ValueError, KeyError) as exc:
            raise HostAPIError("STORE_UNAVAILABLE", 503) from exc
        return user
