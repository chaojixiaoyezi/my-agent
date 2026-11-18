"""管理员维护的普通用户业务出站策略。"""
from __future__ import annotations

import json
import sqlite3
import stat
from collections.abc import Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv6Address, ip_address
from pathlib import Path

_MODES = frozenset({"inherit", "unrestricted", "allowlist"})
_MAX_BATCH = 2000
_MAX_ENDPOINTS = 4096
MAX_EGRESS_DEPLOYMENT_OWNERS = 5000


class OwnerEgressPolicyError(ValueError):
    def __init__(self, code: str, message: str, *, status_code: int = 400,
                 side_effect: str = "not_started", next_action: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.side_effect = side_effect
        self.next_action = next_action
        self.details = details or {}


def _invalid(message: str, action: str) -> OwnerEgressPolicyError:
    return OwnerEgressPolicyError("INVALID_EGRESS_POLICY", message, next_action=action)


@dataclass(frozen=True)
class OwnerEgressEndpoint:
    ip: str
    port: int

    def __post_init__(self):
        if type(self.ip) is not str or "%" in self.ip or len(self.ip) > 45:
            raise _invalid("端点 ip 必须是单个 IPv4 或 IPv6 地址。", "例如填入 192.0.2.10 或 2001:db8::10。")
        try:
            address = ip_address(self.ip)
        except ValueError:
            raise _invalid("端点 ip 无法解析为单个 IPv4 或 IPv6 地址。", "移除主机名、CIDR、范围或方括号。") from None
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise _invalid("端点 port 必须是 1 至 65535 的整数。", "例如填入整数 443。")
        if isinstance(address, IPv6Address) and isinstance(address.ipv4_mapped, IPv4Address):
            address = address.ipv4_mapped
        object.__setattr__(self, "ip", str(address))


@dataclass(frozen=True)
class OwnerEgressPolicy:
    mode: str = "inherit"
    endpoints: tuple[OwnerEgressEndpoint, ...] = ()
    _endpoint_index: frozenset[OwnerEgressEndpoint] = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        if type(self.mode) is not str or self.mode not in _MODES:
            raise _invalid("mode 超出支持范围。", "设置 mode 为 inherit、unrestricted 或 allowlist。")
        if (type(self.endpoints) is not tuple or len(self.endpoints) > _MAX_ENDPOINTS
                or any(type(endpoint) is not OwnerEgressEndpoint for endpoint in self.endpoints)):
            raise _invalid("endpoints 的格式或数量无效。", f"填写最多 {_MAX_ENDPOINTS} 个 IP 与端口。")
        object.__setattr__(self, "endpoints", tuple(dict.fromkeys(self.endpoints)))
        object.__setattr__(self, "_endpoint_index", frozenset(self.endpoints))

    @classmethod
    def from_dict(cls, payload: object) -> OwnerEgressPolicy:
        if not isinstance(payload, Mapping) or set(payload) - {"mode", "endpoints"}:
            raise _invalid("出站策略字段无效。", "提交包含 mode 和 endpoints 的对象。")
        endpoints = payload.get("endpoints", [])
        if type(endpoints) is not list or len(endpoints) > _MAX_ENDPOINTS:
            raise _invalid("endpoints 必须是端点列表。", f"填写最多 {_MAX_ENDPOINTS} 个 IP 与端口。")
        for index, endpoint in enumerate(endpoints):
            if not isinstance(endpoint, Mapping) or set(endpoint) != {"ip", "port"}:
                raise _invalid(f"endpoints[{index}] 必须包含 ip 和 port 字段。", "填写 IP 字符串和整数端口。")
        return cls(payload.get("mode"), tuple(OwnerEgressEndpoint(**entry) for entry in endpoints))

    def to_dict(self) -> dict:
        return {"mode": self.mode, "endpoints": [{"ip": item.ip, "port": item.port} for item in self.endpoints]}

    def effective(self, default: OwnerEgressPolicy) -> OwnerEgressPolicy:
        return default if self.mode == "inherit" else self

    def permits(self, ip: str, port: int) -> bool:
        """检查实际的 socket 对端地址。调用方须先解析 inherit。"""
        try:
            peer = OwnerEgressEndpoint(ip, port)
        except OwnerEgressPolicyError:
            return False
        return self.mode == "unrestricted" or (self.mode == "allowlist" and peer in self._endpoint_index)


class OwnerEgressPolicyStore:
    """跨调用共享同一事务库，接入可核验的宿主强制之前禁止发布 allowlist。"""

    def __init__(self, root_dir):
        self.path = Path(root_dir) / "config" / "owner_egress.sqlite3"

    @contextmanager
    def _database(self, *, write: bool):
        if write:
            from ..common.global_write_guard import GlobalWriteInWorkerError, require_global_writer

            try:
                require_global_writer("owner_egress_policy")
            except GlobalWriteInWorkerError as exc:
                raise OwnerEgressPolicyError(
                    exc.error_code, "当前执行器无权修改宿主出站策略。",
                    status_code=403, next_action="由管理员从当前 Gateway 控制面操作。",
                ) from exc
        try:
            if self.path.parent.is_symlink():
                raise self._unsafe_database_path()
            try:
                info = self.path.lstat()
            except FileNotFoundError:
                if not write:
                    yield None
                    return
            else:
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise self._unsafe_database_path()
            if write:
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            uri = self.path.absolute().as_uri() + ("?mode=rwc" if write else "?mode=ro")
            with closing(sqlite3.connect(uri, uri=True, timeout=1, isolation_level=None)) as conn:
                if write:
                    conn.execute("PRAGMA journal_size_limit = 1048576")
                    conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute("CREATE TABLE IF NOT EXISTS policies (owner_key TEXT PRIMARY KEY, policy TEXT NOT NULL)")
                    conn.execute("CREATE TABLE IF NOT EXISTS deployment_coverage (id INTEGER PRIMARY KEY CHECK(id=1), evidence TEXT NOT NULL)")
                    conn.execute("CREATE TABLE IF NOT EXISTS deployment_takeover (id INTEGER PRIMARY KEY CHECK(id=1), evidence TEXT NOT NULL)")
                yield conn
                if write:
                    conn.execute("COMMIT")
        except (OSError, sqlite3.Error) as exc:
            raise OwnerEgressPolicyError(
                "EGRESS_POLICY_STORAGE_ERROR", f"出站策略库操作失败（{type(exc).__name__}）。",
                status_code=503, side_effect="unconfirmed" if write else "not_started",
                next_action="检查宿主数据目录和 SQLite 状态，再重新读取当前出站策略。",
            ) from exc

    @staticmethod
    def _unsafe_database_path() -> OwnerEgressPolicyError:
        return OwnerEgressPolicyError(
            "EGRESS_POLICY_STORAGE_UNSAFE", "策略数据库或配置目录指向未经核实的链接或特殊文件。",
            status_code=503, next_action="由宿主核对 config 目录，恢复唯一普通 owner_egress.sqlite3 文件。",
        )

    @staticmethod
    def _owner(owner_key: str) -> str:
        from .owner_resolver import _owner_id, owner_identity_from_owner_id

        try:
            identity = owner_identity_from_owner_id(owner_key)
        except ValueError:
            raise _invalid("缺少规范 owner_id。", "从可信用户列表重新选择普通用户。") from None
        if identity.owner_kind != "user" or _owner_id(identity) != owner_key:
            raise _invalid("仅支持规范普通用户编号。", "从可信用户列表重新选择普通用户。")
        return owner_key

    @staticmethod
    def _read(conn, key: str) -> OwnerEgressPolicy:
        row = None if conn is None else conn.execute("SELECT policy FROM policies WHERE owner_key = ?", (key,)).fetchone()
        if row is None:
            return OwnerEgressPolicy("inherit" if key else "unrestricted")
        try:
            policy = OwnerEgressPolicy.from_dict(json.loads(row[0]))
            if not key and policy.mode == "inherit":
                raise ValueError()
            return policy
        except (TypeError, ValueError) as exc:
            raise OwnerEgressPolicyError(
                "EGRESS_POLICY_STORAGE_INVALID", "出站策略库存在无效记录。", status_code=503,
                next_action="请管理员核对策略库与宿主强制状态，修复记录后重新读取。",
            ) from exc

    @staticmethod
    def _write(conn, key: str, policy: OwnerEgressPolicy) -> None:
        conn.execute(
            "INSERT INTO policies VALUES (?, ?) ON CONFLICT(owner_key) DO UPDATE SET policy = excluded.policy",
            (key, json.dumps(policy.to_dict())),
        )

    @staticmethod
    def _require_enforcement(policy: OwnerEgressPolicy) -> None:
        if policy.mode == "allowlist":
            raise OwnerEgressPolicyError(
                "EGRESS_ENFORCEMENT_UNAVAILABLE", "当前 Gateway 无法确认对该范围内全部业务进程实时强制出站规则。",
                status_code=503, next_action="由宿主管理员接入隔离执行与存量进程核验，再提交白名单。",
            )

    @staticmethod
    def _read_coverage(conn) -> dict | None:
        if conn is None or conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='deployment_coverage'"
        ).fetchone() is None:
            return None
        row = conn.execute("SELECT evidence FROM deployment_coverage WHERE id=1").fetchone()
        if row is None:
            return None
        try:
            from uuid import UUID

            value = json.loads(row[0])
            if (type(value) is not dict or set(value) != {"version", "boot_id", "gateway_root", "bindings"}
                    or type(value["version"]) is not int or value["version"] != 1
                    or type(value["boot_id"]) is not str or str(UUID(value["boot_id"])) != value["boot_id"]
                    or type(value["gateway_root"]) is not str or not Path(value["gateway_root"]).is_absolute()
                    or type(value["bindings"]) is not dict or not 1 <= len(value["bindings"]) <= MAX_EGRESS_DEPLOYMENT_OWNERS):
                raise ValueError()
            for owner_id, pair in value["bindings"].items():
                OwnerEgressPolicyStore._owner(owner_id)
                if (type(pair) is not list or len(pair) != 2 or
                        any(type(fd) is not list or len(fd) != 2 or
                            any(type(component) is not int or component < 0 for component in fd) for fd in pair)):
                    raise ValueError()
            return value
        except (TypeError, ValueError) as exc:
            raise OwnerEgressPolicyError(
                "EGRESS_COVERAGE_RECORD_INVALID", "持久出站覆盖记录无效。",
                status_code=503, next_action="核对同一部署根的覆盖记录与实际命名空间。",
            ) from exc

    def _coverage(self):
        with self._database(write=False) as conn:
            return self._read_coverage(conn)

    @staticmethod
    def _read_takeover(conn) -> dict | None:
        if conn is None or conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='deployment_takeover'"
        ).fetchone() is None:
            return None
        row = conn.execute("SELECT evidence FROM deployment_takeover WHERE id=1").fetchone()
        if row is None:
            return None
        try:
            from uuid import UUID

            value = json.loads(row[0])
            if (type(value) is not dict or set(value) != {"boot_id", "gateway_root", "state_sha256", "owners"}
                    or type(value["boot_id"]) is not str or str(UUID(value["boot_id"])) != value["boot_id"]
                    or type(value["gateway_root"]) is not str or not Path(value["gateway_root"]).is_absolute()
                    or type(value["state_sha256"]) is not str or len(value["state_sha256"]) != 64
                    or type(value["owners"]) is not list or not 1 <= len(value["owners"]) <= MAX_EGRESS_DEPLOYMENT_OWNERS):
                raise ValueError()
            keys = tuple(OwnerEgressPolicyStore._owner(owner) for owner in value["owners"])
            if keys != tuple(sorted(set(keys))):
                raise ValueError()
            return value
        except (TypeError, ValueError) as exc:
            raise OwnerEgressPolicyError(
                "EGRESS_TAKEOVER_RECORD_INVALID", "原地接管凭据失效。",
                status_code=503, next_action="核对正确的部署根及维护记录，再决定接管。",
            ) from exc

    def _takeover(self):
        with self._database(write=False) as conn:
            return self._read_takeover(conn)

    def _managed_deployment_recorded(self) -> bool:
        with self._database(write=False) as conn:
            if conn is None:
                return False
            conn.execute("BEGIN")
            return self._read_coverage(conn) is not None or self._read_takeover(conn) is not None

    def _stage_takeover(self, evidence) -> None:
        with self._database(write=True) as conn:
            if self._read_coverage(conn) is not None:
                raise OwnerEgressPolicyError(
                    "EGRESS_TAKEOVER_ALREADY_MANAGED", "当前部署已具备受管覆盖记录，无需重新准备冷启动接管。",
                    status_code=409, next_action="继续使用原 namespace 绑定启动。",
                )
            conn.execute(
                "INSERT INTO deployment_takeover VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET evidence = excluded.evidence",
                (json.dumps(evidence),),
            )

    def _write_coverage(self, before, current, takeover=None) -> None:
        with self._database(write=True) as conn:
            if self._read_coverage(conn) != before or self._read_takeover(conn) != takeover:
                raise OwnerEgressPolicyError(
                    "EGRESS_COVERAGE_RECORD_CHANGED", "出站覆盖记录在启动期间发生变化。",
                    status_code=409, side_effect="not_started", next_action="在持有部署根锁时重新核对覆盖状态。",
                )
            conn.execute(
                "INSERT INTO deployment_coverage VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET evidence = excluded.evidence",
                (json.dumps(current),),
            )
            if takeover is not None:
                conn.execute("DELETE FROM deployment_takeover WHERE id=1")

    def get(self, owner_key: str) -> OwnerEgressPolicy:
        key = self._owner(owner_key)
        with self._database(write=False) as conn:
            return self._read(conn, key)

    def get_default(self) -> OwnerEgressPolicy:
        with self._database(write=False) as conn:
            return self._read(conn, "")

    def inspect_owner(self, owner_key: str) -> tuple[OwnerEgressPolicy, OwnerEgressPolicy]:
        key = self._owner(owner_key)
        with self._database(write=False) as conn:
            if conn is not None:
                conn.execute("BEGIN")  # 用户覆盖与全局默认共享同一次快照。
            policy = self._read(conn, key)
            effective = self._read(conn, "") if policy.mode == "inherit" else policy
            return policy, effective

    def get_effective(self, owner_key: str) -> OwnerEgressPolicy:
        return self.inspect_owner(owner_key)[1]

    def _effective_deployment_policies(self, owners):
        with self._database(write=False) as conn:
            if conn is not None:
                conn.execute("BEGIN")
            default = self._read(conn, "")
            return {key: self._read(conn, key).effective(default) for key in owners}

    def set_default(self, policy: OwnerEgressPolicy | str) -> OwnerEgressPolicy:
        if not isinstance(policy, (OwnerEgressPolicy, str)) or policy == "inherit" or (
            isinstance(policy, OwnerEgressPolicy) and policy.mode == "inherit"
        ):
            raise _invalid("全局默认策略不能继承。", "设置全局 mode 为 unrestricted 或 allowlist。")
        return self._apply({"": policy})[""]

    def apply(self, updates: Mapping[str, OwnerEgressPolicy | str]) -> dict[str, OwnerEgressPolicy]:
        if not isinstance(updates, Mapping) or not 1 <= len(updates) <= _MAX_BATCH:
            raise _invalid("本次策略更新的用户数量无效。", f"选择 1 至 {_MAX_BATCH} 个普通用户。")
        return self._apply({self._owner(key): policy for key, policy in updates.items()})

    def _preview_owner_change(self, key, change):
        with self._database(write=False) as conn:
            if conn is not None:
                conn.execute("BEGIN")
            old, default = self._read(conn, key), self._read(conn, "")
        proposed = OwnerEgressPolicy(change, old.endpoints) if isinstance(change, str) else change
        if type(proposed) is not OwnerEgressPolicy:
            raise _invalid("用户策略格式无效。", "提交完整策略或仅提交 mode。")
        return old, default, proposed

    def _commit_owner_change(self, key, old, default, proposed):
        with self._database(write=True) as conn:
            if self._read(conn, key) != old or self._read(conn, "") != default:
                raise OwnerEgressPolicyError(
                    "EGRESS_POLICY_CONFLICT", "用户策略或全局默认在发布期间已变化。",
                    status_code=409, side_effect="unconfirmed", next_action="读取已保存策略并核对当前命名空间规则。",
                )
            self._write(conn, key, proposed)
        return proposed

    def _preview_default_change(self, change, owners):
        with self._database(write=False) as conn:
            if conn is not None:
                conn.execute("BEGIN")
            old = self._read(conn, "")
            inherited = tuple(key for key in owners if self._read(conn, key).mode == "inherit")
        proposed = OwnerEgressPolicy(change, old.endpoints) if isinstance(change, str) else change
        if type(proposed) is not OwnerEgressPolicy or proposed.mode == "inherit":
            raise _invalid("全局默认策略不能继承。", "设置全局 mode 为 unrestricted 或 allowlist。")
        return old, proposed, inherited

    def _commit_default_change(self, old, proposed, owners, inherited):
        with self._database(write=True) as conn:
            current = tuple(key for key in owners if self._read(conn, key).mode == "inherit")
            if self._read(conn, "") != old or current != inherited:
                raise OwnerEgressPolicyError(
                    "EGRESS_POLICY_CONFLICT", "默认策略或继承用户在发布期间已变化。",
                    status_code=409, side_effect="unconfirmed", next_action="读取已保存策略并核对当前命名空间规则。",
                )
            self._write(conn, "", proposed)

    def _apply(self, updates: Mapping[str, OwnerEgressPolicy | str]) -> dict[str, OwnerEgressPolicy]:
        for item in updates.values():
            policy = OwnerEgressPolicy(item) if isinstance(item, str) else item
            if not isinstance(policy, OwnerEgressPolicy):
                raise _invalid("用户策略格式无效。", "提交完整策略或仅提交 mode。")
            self._require_enforcement(policy)
        with self._database(write=True) as conn:
            default = self._read(conn, "")
            resolved = {}
            for key, change in updates.items():
                current = self._read(conn, key)
                self._require_enforcement(current.effective(default))
                policy = OwnerEgressPolicy(change, current.endpoints) if isinstance(change, str) else change
                self._require_enforcement(policy.effective(default))
                resolved[key] = policy
            for key, policy in resolved.items():
                self._write(conn, key, policy)
        return resolved


def require_owner_process_egress(owner_home: Path, authority_root: Path | None = None) -> bool:
    """受管 owner 始终使用同一内核出生路径，失去白名单强制证明时拒绝启动。"""
    from .owner_egress_linux import current_linux_owner_namespace, owner_egress_identity

    scope = owner_egress_identity(owner_home, authority_root)
    if scope is None:
        from ..path_access_policy import agent_home_root_for_owner
        from .owner_egress_gateway import deployment_requires_managed_owner
        from .owner_resolver import owner_identity_from_owner_id

        home = Path(owner_home).resolve(strict=False)
        root = Path(authority_root).resolve() if authority_root is not None else agent_home_root_for_owner(home)
        try:
            identity = owner_identity_from_owner_id(home.relative_to(root / "owners").as_posix()) if root else None
        except ValueError:
            identity = None
        if root is not None and deployment_requires_managed_owner(root):
            if identity is None:
                raise OwnerEgressPolicyError(
                    "EGRESS_OWNER_IDENTITY_UNPROVEN", "无法从宿主 owner 根确认业务进程身份，未启动进程。",
                    status_code=503, next_action="从原认证会话重试，核对 owner 目录和部署根的一致性。",
                )
            if identity.owner_kind == "group":
                raise OwnerEgressPolicyError(
                    "EGRESS_GROUP_ACTOR_UNPROVEN", "当前群业务进程没有可核验的普通用户出站绑定。",
                    status_code=503, next_action="请从绑定命名空间的个人会话发起业务进程。",
                )
        return False
    root, identity = scope
    from .owner_resolver import resolve_owner_home

    owner_id = resolve_owner_home(root, identity).owner_id
    namespace = current_linux_owner_namespace(root, identity, by_authority=True)
    if namespace is not None:
        namespace.require_active()
        return True
    from .owner_egress_gateway import deployment_requires_managed_owner

    if deployment_requires_managed_owner(root):
        raise OwnerEgressPolicyError(
            "EGRESS_OWNER_RUNTIME_UNMANAGED", "当前进程没有该 owner 的受信出站运行绑定，未启动业务进程。",
            status_code=503, next_action="从已完成命名空间绑定的 Gateway 发起执行。",
        )
    effective = OwnerEgressPolicyStore(root).get_effective(owner_id)
    OwnerEgressPolicyStore._require_enforcement(effective)
    return False


class _BoundOwnerEgress:
    def __init__(self, root: Path, owner_id: str, owner_kind: str):
        self.root, self.owner_id, self.owner_kind = root, owner_id, owner_kind
        self.store = OwnerEgressPolicyStore(root)

    def __call__(self) -> OwnerEgressPolicy:
        if self.owner_kind == "user":
            return self.store.get_effective(self.owner_id)
        if self.requires_managed_transport():
            return OwnerEgressPolicy("allowlist")
        return self.store.get_default()

    def requires_managed_transport(self) -> bool:
        from .owner_egress_gateway import deployment_requires_managed_owner

        return deployment_requires_managed_owner(self.root)

    def has_managed_namespace(self) -> bool:
        from .owner_egress_linux import current_linux_owner_namespace
        from .owner_resolver import owner_identity_from_owner_id

        if self.owner_kind != "user":
            return False
        namespace = current_linux_owner_namespace(
            self.root, owner_identity_from_owner_id(self.owner_id), by_authority=True,
        )
        return namespace is not None

    def http_scope(self):
        from contextlib import nullcontext

        from .owner_egress_gateway import current_gateway_owner_egress

        runtime = current_gateway_owner_egress(self.root)
        if runtime is not None and self.owner_id in runtime.leases:
            return runtime.http_scope(self.owner_id)
        if self.requires_managed_transport():
            raise OwnerEgressPolicyError(
                "EGRESS_OWNER_RUNTIME_UNMANAGED", "当前进程未持有此用户的 Gateway 出站绑定，未发送请求。",
                status_code=503, next_action="从已完成命名空间绑定的 Gateway 发起网页请求。",
            )
        return nullcontext()


def bound_owner_egress_policy(root, *, owner_id: str, owner_kind: str):
    """宿主按原 owner 绑定实时策略，不接收模型或工具提供的身份。"""
    if owner_id == "local/main" and owner_kind == "main":
        return None
    return _BoundOwnerEgress(root, owner_id, owner_kind)
