"""Gateway 启动时的 Linux 出站命名空间接管与管理员发布。"""
from __future__ import annotations

import json
import os
import stat
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from hashlib import sha256
from pathlib import Path
from threading import BoundedSemaphore, Condition, Lock

from ..common.nofollow_fs import (
    open_directory_beneath,
    read_bytes_beneath,
    read_tail_snapshot_beneath,
)
from ..common.strict_json import load_strict_json
from .owner_egress import (
    MAX_EGRESS_DEPLOYMENT_OWNERS,
    OwnerEgressPolicy,
    OwnerEgressPolicyError,
    OwnerEgressPolicyStore,
)
from .owner_resolver import owner_identity_from_owner_id, resolve_owner_home

_MANIFEST = ("config", "owner_egress_namespaces.json")
_SCHEMA = "owner_egress_namespaces.v1"
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_KERNEL_CONCURRENCY = 16
_KERNEL_SLOTS = BoundedSemaphore(_KERNEL_CONCURRENCY)
_ACTIVE: dict[Path, GatewayOwnerEgress] = {}
_LOCK = Lock()


def _denial(
    code: str, message: str, action: str, *, side_effect: str = "not_started", details: dict | None = None,
) -> OwnerEgressPolicyError:
    return OwnerEgressPolicyError(code, message, status_code=503, side_effect=side_effect, next_action=action, details=details)


def _manifest(root: Path) -> dict[str, tuple[int, int]]:
    try:
        snapshot = read_tail_snapshot_beneath(root, _MANIFEST, max_bytes=_MAX_MANIFEST_BYTES)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise _denial("EGRESS_DEPLOYMENT_CONFIG_UNAVAILABLE", f"无法安全读取出站部署文件（{type(exc).__name__}）。",
                      "核对 config/owner_egress_namespaces.json 的权限和路径。") from exc
    if snapshot is None:
        return {}
    geteuid = getattr(os, "geteuid", None)
    if (geteuid is None or snapshot.stat.st_uid != geteuid() or stat.S_IMODE(snapshot.stat.st_mode) != 0o600
            or snapshot.stat.st_size != len(snapshot.content)):
        raise _denial("EGRESS_DEPLOYMENT_CONFIG_UNTRUSTED", "出站部署文件的所有权、权限或容量不符合宿主约束。",
                      "由 Gateway 进程的宿主账号创建该文件，权限使用 0600，大小不超过 2 MiB。")
    try:
        data = load_strict_json(snapshot.content)
        if type(data) is not dict or set(data) != {"schema", "enabled", "owners"} or data["schema"] != _SCHEMA:
            raise ValueError()
        if type(data["enabled"]) is not bool or type(data["owners"]) is not list or len(data["owners"]) > MAX_EGRESS_DEPLOYMENT_OWNERS:
            raise ValueError()
        rows = {}
        for entry in data["owners"]:
            if type(entry) is not dict or set(entry) != {"owner_id", "holder_pid", "holder_start_ticks"}:
                raise ValueError()
            owner_id, pid, ticks = entry["owner_id"], entry["holder_pid"], entry["holder_start_ticks"]
            identity = owner_identity_from_owner_id(owner_id)
            if (identity.owner_kind != "user" or resolve_owner_home(root, identity).owner_id != owner_id or owner_id in rows
                    or type(pid) is not int or pid < 2 or type(ticks) is not int or ticks < 1):
                raise ValueError()
            rows[owner_id] = (pid, ticks)
        if data["enabled"] != bool(rows):
            raise ValueError()
    except (KeyError, TypeError, ValueError) as exc:
        raise _denial("EGRESS_DEPLOYMENT_CONFIG_INVALID", "出站配置结构、用户编号或命名空间进程身份无效。",
                      f"使用 owner_egress_namespaces.v1，提供 enabled 和 owners，最多登记 {MAX_EGRESS_DEPLOYMENT_OWNERS} 个不同普通用户。") from exc
    return rows


def _start_ticks(pid: int) -> int:
    raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    return int(raw.rsplit(")", 1)[1].split()[19])


def _holder_namespace_fds(stack: ExitStack, pid: int, start_ticks: int) -> tuple[int, int]:
    try:
        process = os.pidfd_open(pid)
        stack.callback(os.close, process)
        if _start_ticks(pid) != start_ticks:
            raise ValueError("holder_start_ticks_mismatch")
        for kind, identity in (("uid", os.geteuid()), ("gid", os.getegid())):
            mapping = Path(f"/proc/{pid}/{kind}_map").read_text(encoding="ascii").split()
            if mapping != [str(identity), str(identity), "1"]:
                raise _denial(
                    "EGRESS_NAMESPACE_UID_MAPPING_INVALID", "持有进程的 UID/GID 映射与宿主隔离合同不一致。",
                    "用 Gateway 的独立非 root 用户运行 unshare --user --map-current-user --net 建立持有进程。",
                )
        descriptors = []
        for kind in ("net", "user"):
            fd = os.open(f"/proc/{pid}/ns/{kind}", os.O_RDONLY | os.O_CLOEXEC)
            stack.callback(os.close, fd)
            descriptors.append(fd)
        from select import POLLIN, poll

        watcher = poll()
        watcher.register(process, POLLIN)
        if _start_ticks(pid) != start_ticks or watcher.poll(0):
            raise ValueError("holder_start_ticks_mismatch")
        return descriptors[0], descriptors[1]
    except OwnerEgressPolicyError:
        raise
    except (OSError, ValueError, IndexError, AttributeError) as exc:
        raise _denial("EGRESS_NAMESPACE_HOLDER_UNCONFIRMED", f"无法核对命名空间持有进程 {pid}（{type(exc).__name__}）。",
                      "确认受信宿主持有进程仍存活，重取其 PID 和 /proc/<pid>/stat 的第 22 个字段。") from exc


def _require_first_covered_generation(root: Path, gateway_root: Path) -> None:
    if read_bytes_beneath(gateway_root, ("gateway_state.json",), max_bytes=1024 * 1024) is not None:
        raise _denial("EGRESS_PREDECESSOR_UNPROVEN", "已有 Gateway 代次缺少完整的业务进程隔离和退出证明。",
                      "在独立的新部署根启用，或先由宿主证明旧代业务进程已全部退出。")
    for parts, allowed in (
        (("owners",), {"local", "providers"}),
        (("owners", "local"), {"main"}),
    ):
        if any(entry not in allowed for entry in _bounded_directory(root, parts)):
            raise _old_owners_unconfirmed()
    for provider in _bounded_directory(root, ("owners", "providers")):
        path = ("owners", "providers", provider)
        if any(entry not in {"groups", "users"} for entry in _bounded_directory(root, path)):
            raise _old_owners_unconfirmed()
        if _bounded_directory(root, (*path, "groups")) or _bounded_directory(root, (*path, "users")):
            raise _old_owners_unconfirmed()


def _bounded_directory(root: Path, parts: tuple[str, ...]) -> tuple[str, ...]:
    from itertools import islice

    try:
        fd = open_directory_beneath(root, parts, require_dir_fd=True)
    except FileNotFoundError:
        return ()
    try:
        with os.scandir(fd) as entries:
            names = tuple(entry.name for entry in islice(entries, 129))
        if len(names) > 128:
            raise _old_owners_unconfirmed()
        return names
    finally:
        os.close(fd)


def _old_owners_unconfirmed() -> OwnerEgressPolicyError:
    return _denial("EGRESS_EXISTING_OWNER_UNPROVEN", "已有业务 owner 目录或来源不完整，无法证明旧业务进程均已纳管。",
                   "在独立部署根启用治理，再由管理员登记普通用户。")


class GatewayOwnerEgress:
    def __init__(self, leases):
        self.leases = dict(leases)
        self._publish_locks = {owner_id: Lock() for owner_id in leases}
        self._http = {owner_id: Condition() for owner_id in leases}
        self._http_active = dict.fromkeys(leases, 0)
        self._http_blocked: set[str] = set()
        self._default_publish_lock = Lock()

    @contextmanager
    def http_scope(self, owner_id: str):
        gate = self._http[owner_id]
        with gate:
            if owner_id in self._http_blocked:
                raise _denial("EGRESS_POLICY_INSTALLING", "当前用户正在切换出站规则，未发送请求。",
                              "待管理员发布完成后再发起网页请求。")
            self._http_active[owner_id] += 1
        try:
            yield
        finally:
            with gate:
                self._http_active[owner_id] -= 1
                gate.notify_all()

    @contextmanager
    def _drain_http(self, owner_id: str, *, deadline: float | None = None):
        gate = self._http[owner_id]
        owner_deadline = time.monotonic() + 5
        deadline = owner_deadline if deadline is None else min(owner_deadline, deadline)
        with gate:
            self._http_blocked.add(owner_id)
        try:
            with gate:
                while self._http_active[owner_id]:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise _denial("EGRESS_WEB_REQUESTS_ACTIVE", "现有宿主网页请求尚未退出，策略未提交。",
                                      "待目标用户的网页请求结束或取消后再提交。")
                    gate.wait(left)
            yield
        finally:
            with gate:
                self._http_blocked.discard(owner_id)

    def observe(self, store: OwnerEgressPolicyStore, owner_id: str) -> dict:
        def projection(readback, *, managed, snapshot=None):
            row, effective = store.inspect_owner(owner_id) if snapshot is None else snapshot
            return {
                "policy": row.to_dict(), "effective": effective.to_dict(),
                "runtime_status": "managed" if managed else "blocked_unbound",
                "kernel_readback": {"status": readback},
            }

        lease = self.leases.get(owner_id)
        if lease is None:
            return projection("blocked_unbound", managed=False)
        lock = self._publish_locks[owner_id]
        if not lock.acquire(blocking=False):
            return projection("publishing", managed=True)
        try:
            snapshot = store.inspect_owner(owner_id)
            result = projection("readback_busy", managed=True, snapshot=snapshot)
            if not _KERNEL_SLOTS.acquire(blocking=False):
                return result
            try:
                lease.verify(snapshot[1])
            except OwnerEgressPolicyError as exc:
                result["kernel_readback"] = {
                    "status": "unconfirmed", "error_code": exc.code, "message": str(exc),
                    "next_action": "核对目标策略和宿主命名空间。如需使内核与保存策略一致，可只对该用户重新提交已核实的策略。",
                }
            else:
                result["kernel_readback"] = {"status": "verified"}
            finally:
                _KERNEL_SLOTS.release()
            return result
        finally:
            lock.release()

    @contextmanager
    def _publication(self, owner_id: str):
        lock = self._publish_locks[owner_id]
        if not lock.acquire(blocking=False):
            raise _denial("EGRESS_OWNER_PUBLISH_BUSY", f"用户 {owner_id} 的出站策略正在被核验或更新。",
                          "待当前用户的操作结束后，先核对其策略和内核状态。")
        try:
            yield
        finally:
            lock.release()

    def apply(self, store: OwnerEgressPolicyStore, owner_id: str, change):
        lease = self.leases.get(owner_id)
        if lease is None:
            raise _denial("EGRESS_OWNER_RUNTIME_UNMANAGED", f"用户 {owner_id} 没有该代 Gateway 的可核验运行绑定。",
                          "由宿主将该用户加入部署文件并验证业务进程出生后再设置白名单。")
        with self._publication(owner_id), self._drain_http(owner_id):
            old_row, default, proposed = store._preview_owner_change(owner_id, change)
            old, new = old_row.effective(default), proposed.effective(default)
            bridge = _intersection(old, new)
            _apply_kernel_policy(lease, bridge)
            policy = store._commit_owner_change(owner_id, old_row, default, proposed)
            if bridge != new:
                try:
                    _apply_kernel_policy(lease, new)
                except OwnerEgressPolicyError as exc:
                    raise _denial("EGRESS_PUBLISHED_KERNEL_UNCONFIRMED",
                                  f"用户 {owner_id} 的策略已持久提交，最终内核规则核验失败（{exc.code}）。",
                                  "核对当前已保存的策略和命名空间规则后修复；不要盲目重试批次。",
                                  side_effect="unconfirmed", details={"policy_committed": True}) from exc
            return policy


    def set_default(self, store: OwnerEgressPolicyStore, change) -> OwnerEgressPolicy:
        if not self._default_publish_lock.acquire(blocking=False):
            raise _denial("EGRESS_DEFAULT_PUBLISH_BUSY", "已有默认出站策略正在发布。",
                          "待前一项发布完成后核对当前默认策略。")
        deadline = time.monotonic() + 30
        try:
            with ExitStack() as frozen:
                for owner_id, lock in sorted(self._publish_locks.items()):
                    if not lock.acquire(blocking=False):
                        raise _denial("EGRESS_OWNER_PUBLISH_BUSY", f"用户 {owner_id} 有独立出站策略正在发布。",
                                      "待该用户的发布结束后再提交默认策略。")
                    frozen.callback(lock.release)
                old, proposed, inherited = store._preview_default_change(change, self.leases.keys())
                changed = old != proposed
                bridge = _intersection(old, proposed)
                for owner_id in inherited:
                    frozen.enter_context(self._drain_http(owner_id, deadline=deadline))
                failures = _apply_kernel_batch(self.leases, dict.fromkeys(inherited, bridge), deadline=deadline)
                if failures:
                    raise _denial(
                        "EGRESS_DEFAULT_PREPARE_UNCONFIRMED", "部分租户的准备期内核规则未核验，默认策略未提交。",
                        "核对报告里的租户，并读取已准备租户的过渡规则后再决定下一步。",
                        side_effect="unconfirmed", details={"policy_committed": False, "users": failures},
                    )
                store._commit_default_change(old, proposed, self.leases.keys(), inherited)
                failures = (
                    _apply_kernel_batch(self.leases, dict.fromkeys(inherited, proposed), deadline=deadline)
                    if changed and bridge != proposed else []
                )
                if failures:
                    raise _denial(
                        "EGRESS_DEFAULT_KERNEL_UNCONFIRMED",
                        "默认策略已提交，部分租户的最终内核规则尚未核验。",
                        "查看逐用户回执并核对实际规则，禁止盲目重试。",
                        side_effect="unconfirmed", details={
                            "policy_committed": True, "default": proposed.to_dict(), "users": failures,
                        },
                    )
                return proposed
        finally:
            self._default_publish_lock.release()


def _apply_kernel_policy(lease, policy, *, deadline: float | None = None) -> None:
    remaining = 5.0 if deadline is None else min(5.0, max(0.0, deadline - time.monotonic()))
    if not _KERNEL_SLOTS.acquire(timeout=remaining):
        raise _denial("EGRESS_KERNEL_CAPACITY_BUSY", "内核规则核验通道正忙，本用户尚未开始提交。",
                      "待当前出站规则操作结束后再重新核对。")
    try:
        lease.apply(policy)
        lease.verify(policy)
    finally:
        _KERNEL_SLOTS.release()


def _apply_kernel_batch(leases, policies, *, deadline: float | None = None) -> list[dict[str, str]]:
    def apply_one(owner_id):
        try:
            if deadline is not None and time.monotonic() >= deadline:
                raise _denial("EGRESS_KERNEL_BATCH_DEADLINE", "当前批次已达核验时间预算，本用户未开始。",
                              "核对原已提交策略与该用户的实际内核规则。")
            _apply_kernel_policy(leases[owner_id], policies[owner_id], deadline=deadline)
        except OwnerEgressPolicyError as exc:
            return {"owner_id": owner_id, "error_code": exc.code, "message": str(exc), "next_action": exc.next_action}
        return None

    if not policies:
        return []
    with ThreadPoolExecutor(max_workers=min(_KERNEL_CONCURRENCY, len(policies))) as pool:
        return [failure for failure in pool.map(apply_one, policies) if failure is not None]


def _intersection(old: OwnerEgressPolicy, new: OwnerEgressPolicy) -> OwnerEgressPolicy:
    if old.mode == "unrestricted":
        return new
    if new.mode == "unrestricted":
        return old
    allowed = frozenset(new.endpoints)
    return OwnerEgressPolicy("allowlist", tuple(endpoint for endpoint in old.endpoints if endpoint in allowed))


def current_gateway_owner_egress(root: Path) -> GatewayOwnerEgress | None:
    with _LOCK:
        return _ACTIVE.get(root)


def deployment_requires_managed_owner(root: Path) -> bool:
    active = current_gateway_owner_egress(root)
    if active is not None:
        return True
    if _manifest(root):
        return True
    store = OwnerEgressPolicyStore(root)
    return store._managed_deployment_recorded()


def _require_fd_budget(count: int) -> None:
    import resource

    # 每个常驻 lease 两个 FD。预留 Gateway 启动与并行核验所需的辅助 FD。
    needed = 2 * count + 128
    soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft != resource.RLIM_INFINITY and soft < needed:
        raise _denial("EGRESS_DEPLOYMENT_FD_BUDGET_INSUFFICIENT",
                      f"当前文件描述符上限为 {soft}，{count} 个部署租户至少需要 {needed}。",
                      "提高 Gateway 的宿主文件描述符上限后，根据实际负载复核容量。")


def _binding_file_identity(fd: int) -> list[int]:
    info = os.fstat(fd)
    return [info.st_dev, info.st_ino]


def _boot_identity() -> str:
    from uuid import UUID

    try:
        return str(UUID(Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()))
    except (ValueError, OSError) as exc:
        raise _denial("EGRESS_BOOT_ID_UNCONFIRMED", "无法确认当前 Linux 启动身份。",
                      "检查 /proc/sys/kernel/random/boot_id 的宿主文件访问。") from exc


def _verify_generation_transition(previous, current) -> None:
    from ..gateway_parts.daemon_metadata import process_identity_is_live

    valid = (previous["gateway_root"] == current["gateway_root"]
             and type(previous["bindings"]) is dict and previous["bindings"].keys() == current["bindings"].keys()
             and (previous["boot_id"] != current["boot_id"] or previous["bindings"] == current["bindings"]))
    if not valid:
        raise _denial("EGRESS_PREDECESSOR_UNPROVEN", "上一代受管用户、Gateway 根或本次启动内的命名空间身份发生变化。",
                      "继续使用原用户和命名空间。换绑定前须完成可信的旧业务进程退出核验。")
    if previous["boot_id"] == current["boot_id"]:
        try:
            raw = _prior_gateway_state(Path(current["gateway_root"]))
            if raw is None:
                return  # 先前启动还未进入 Gateway 状态发布，无宿主业务 HTTP。
            state = load_strict_json(raw)
            previous_dead = type(state) is dict and process_identity_is_live(state.get("process_identity")) is False
        except (OSError, ValueError):
            previous_dead = False
        if not previous_dead:
            raise _denial(
                "EGRESS_PRIOR_GATEWAY_EXIT_UNCONFIRMED", "上一 Gateway 实例仍存活或无法证明已经退出。",
                "待原 Gateway 进程完全退出后重新启动，以免遗漏其仍在运行的宿主网页请求。",
            )


@contextmanager
def _deployment_lock(root: Path):
    from ..gateway_parts.io import (
        _flock_unlock,
        _open_private_lock_descriptor,
        _try_flock_exclusive,
    )

    try:
        fd = _open_private_lock_descriptor(root / "config" / "owner_egress_namespaces.lock")
    except OSError as exc:
        raise _denial("EGRESS_DEPLOYMENT_LOCK_UNAVAILABLE", "无法取得部署根出站治理锁。",
                      "检查 Gateway 宿主账号和 config 目录权限。") from exc
    with os.fdopen(fd, "r+", encoding="utf-8") as stream:
        if not _try_flock_exclusive(stream):
            raise _denial("EGRESS_GATEWAY_ALREADY_BOUND", "该部署根已有活动的出站治理 Gateway。",
                          "等待原 Gateway 完成退出后重试。")
        try:
            yield
        finally:
            _flock_unlock(stream)


def _prior_gateway_state(gateway_root: Path) -> bytes | None:
    return read_bytes_beneath(gateway_root, ("gateway_state.json",), max_bytes=1024 * 1024)


def prepare_owner_egress_cold_takeover(root: Path, gateway_root: Path, lifecycle) -> dict:
    """同一失效窗口记录现有部署启动身份，只有实际 Linux 重启后才消费。"""
    from ..gateway_parts.daemon_metadata import process_identity_is_live

    if sys.platform != "linux":
        raise _denial("EGRESS_PLATFORM_UNSUPPORTED", "原地接管需要 Linux 启动身份与网络命名空间。",
                      "在受支持的 Linux Gateway 上执行。")
    owners = _manifest(root)
    if not owners:
        raise _denial("EGRESS_DEPLOYMENT_CONFIG_MISSING", "原地接管前须设置并保护完整的用户命名空间部署文件。",
                      "在 config/owner_egress_namespaces.json 登记受管用户，然后停止原 Gateway。")
    lifecycle.require_held()
    with _deployment_lock(root):
        try:
            raw = _prior_gateway_state(gateway_root)
            state = load_strict_json(raw) if raw is not None else None
            stopped = (type(state) is dict and state.get("status") in {"stopped", "failed", "interrupted", "killed"}
                       and process_identity_is_live(state.get("process_identity")) is False)
        except (OSError, ValueError):
            stopped = False
        if not stopped:
            raise _denial("EGRESS_PREDECESSOR_UNPROVEN", "无法证明原 Gateway 已停止。",
                          "先停止原服务，确认状态记录和原进程身份，再执行原地接管准备。")
        evidence = {"boot_id": _boot_identity(), "gateway_root": str(gateway_root),
                    "state_sha256": sha256(raw).hexdigest(), "owners": sorted(owners)}
        lifecycle.require_held()
        OwnerEgressPolicyStore(root)._stage_takeover(evidence)
        return {"status": "prepared_requires_cold_boot", "owners": evidence["owners"]}


def _validate_cold_takeover(gateway_root: Path, takeover: dict, rows: dict, boot_id: str) -> None:
    raw = _prior_gateway_state(gateway_root)
    if (takeover["boot_id"] == boot_id or takeover["gateway_root"] != str(gateway_root)
            or takeover["owners"] != sorted(rows) or raw is None or sha256(raw).hexdigest() != takeover["state_sha256"]):
        raise _denial("EGRESS_COLD_TAKEOVER_UNPROVEN", "未证明真实 Linux 重启，或旧 Gateway 状态与准备凭据不一致。",
                      "保持原 Gateway 停止，核对本次宿主启动身份、原状态和已登记用户。")


@contextmanager
def bind_gateway_owner_egress(root: Path, gateway_root: Path, lifecycle):
    """在真实单实例锁中固定 namespace、同步当前策略，再允许 Gateway 接收业务。"""
    if sys.platform != "linux":
        try:
            present = read_bytes_beneath(root, _MANIFEST, max_bytes=_MAX_MANIFEST_BYTES) is not None
        except (ValueError, OSError) as exc:
            raise _denial("EGRESS_DEPLOYMENT_CONFIG_UNAVAILABLE", "无法安全核对出站部署文件。",
                          "检查 config/owner_egress_namespaces.json。") from exc
        store = OwnerEgressPolicyStore(root)
        if present or store._managed_deployment_recorded():
            raise _denial("EGRESS_PLATFORM_UNSUPPORTED", "当前平台无 Linux 网络命名空间支持。",
                          "将白名单部署文件用于具备 nftables 的 Linux Gateway。")
        yield
        return
    rows = _manifest(root)
    store = OwnerEgressPolicyStore(root)
    if not rows:
        if store._managed_deployment_recorded():
            raise _denial("EGRESS_DEPLOYMENT_CONFIGURATION_REQUIRED", "此前已启用出站治理，当前未找到有效部署文件。",
                          "恢复受信部署文件及命名空间。若仅关闭全局白名单，请在管理端选 unrestricted。")
        yield
        return
    from .owner_egress_linux import bind_linux_owner_namespace

    lifecycle.require_held()
    with _deployment_lock(root), ExitStack() as stack:
        _require_fd_budget(len(rows))
        previous = store._coverage()
        takeover = store._takeover()
        boot_id = _boot_identity()
        try:
            if previous is None and takeover is not None:
                _validate_cold_takeover(gateway_root, takeover, rows, boot_id)
            elif previous is None:
                _require_first_covered_generation(root, gateway_root)
        except (ValueError, OSError) as exc:
            if isinstance(exc, OwnerEgressPolicyError):
                raise
            raise _denial("EGRESS_PREDECESSOR_UNPROVEN", "无法核对原 Gateway 或业务用户目录。",
                          "检查部署根的目录和权限，确认原有业务进程均已退出。") from exc
        leases, bindings = {}, {}
        for owner_id, (pid, ticks) in rows.items():
            with ExitStack() as temporary:
                network_fd, user_fd = _holder_namespace_fds(temporary, pid, ticks)
                leases[owner_id] = stack.enter_context(bind_linux_owner_namespace(
                    owner_id, network_fd, user_fd, authority_root=root,
                ))
                bindings[owner_id] = [_binding_file_identity(fd) for fd in (network_fd, user_fd)]
        current = {
            "version": 1, "boot_id": boot_id, "gateway_root": str(gateway_root),
            "bindings": bindings,
        }
        if previous is not None:
            _verify_generation_transition(previous, current)
        failures = _apply_kernel_batch(leases, store._effective_deployment_policies(leases))
        if failures:
            first = failures[0]
            raise _denial("EGRESS_STARTUP_KERNEL_UNCONFIRMED",
                          f"{len(failures)} 个租户的启动规则未核验，第一个是 {first['owner_id']}（{first['error_code']}）。",
                          first["next_action"], side_effect="unconfirmed", details={"users": failures})
        lifecycle.require_held()
        store._write_coverage(previous, current, takeover)
        runtime = GatewayOwnerEgress(leases)
        with _LOCK:
            if root in _ACTIVE:
                raise _denial("EGRESS_GATEWAY_ALREADY_BOUND", "该部署根已有活动的出站治理 Gateway。",
                              "等待现有 Gateway 完成停止后再启动。")
            _ACTIVE[root] = runtime
        try:
            yield
        finally:
            with _LOCK:
                del _ACTIVE[root]
