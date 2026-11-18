"""Linux 独立网络命名空间的出站包过滤。由可信宿主进程提供已固定的 namespace FD。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from weakref import WeakSet, WeakValueDictionary

from .owner_egress import OwnerEgressPolicy, OwnerEgressPolicyError
from .owner_resolver import _owner_id, owner_identity_from_owner_id

_TABLE = "my_agent_owner_egress"
_NS_GET_USERNS = 0xB701  # Linux uapi/linux/nsfs.h
_ISSUED: WeakSet = WeakSet()
_LEASE_LOCK = Lock()
_NAMESPACES: WeakValueDictionary = WeakValueDictionary()
_GATEWAY_OWNERS: WeakValueDictionary = WeakValueDictionary()
_AUTHORITY_OWNERS: WeakValueDictionary = WeakValueDictionary()


def _nft_batch(policy: OwnerEgressPolicy) -> str:
    lines = [f"destroy table inet {_TABLE}"]
    if policy.mode == "unrestricted":
        return "\n".join(lines) + "\n"
    if policy.mode != "allowlist":
        raise OwnerEgressPolicyError(
            "UNRESOLVED_EGRESS_POLICY", "宿主需要已经解析的有效出站策略。",
            next_action="先按全局默认解析 inherit，再应用宿主规则。",
        )
    lines += [
        f"add table inet {_TABLE}",
        f"add chain inet {_TABLE} egress {{ type filter hook output priority filter; policy drop; }}",
        f"add chain inet {_TABLE} ingress {{ type filter hook input priority filter; policy drop; }}",
    ]
    for version, family, address_type in ((4, "ip", "ipv4_addr"), (6, "ip6", "ipv6_addr")):
        entries = [f"{item.ip} . {item.port}" for item in policy.endpoints if (":" in item.ip) == (version == 6)]
        if not entries:
            continue
        endpoints = f"endpoints{version}"
        elements = ", ".join(entries)
        lines += [
            f"add set inet {_TABLE} {endpoints} {{ type {address_type} . inet_service; elements = {{ {elements} }}; }}",
            f"add rule inet {_TABLE} egress meta l4proto {{ tcp, udp }} {family} daddr . th dport @{endpoints} accept",
            # 每个回包也核对当前白名单。单独接受 established 会让撤销前的连接继续收数据。
            f"add rule inet {_TABLE} ingress ct direction reply meta l4proto {{ tcp, udp }} "
            f"{family} saddr . th sport @{endpoints} accept",
        ]
    return "\n".join(lines) + "\n"


def _pin_namespace(stack: ExitStack, fd: int, kind: str, *, check_pid1: bool) -> tuple[str, int]:
    if type(fd) is not int or fd < 3:
        raise OwnerEgressPolicyError(
            "INVALID_EGRESS_NAMESPACE", "缺少可信宿主持有的命名空间 FD。",
            next_action="先由宿主创建和固定普通用户的独立命名空间。",
        )
    pinned = os.dup(fd)
    stack.callback(os.close, pinned)
    path = f"/proc/self/fd/{pinned}"
    sources = (f"/proc/self/ns/{kind}", f"/proc/1/ns/{kind}") if check_pid1 else (f"/proc/self/ns/{kind}",)
    if (not os.readlink(path).startswith(f"{kind}:[")
            or any(os.path.samestat(os.fstat(pinned), os.stat(source)) for source in sources)):
        raise OwnerEgressPolicyError(
            "INVALID_EGRESS_NAMESPACE", "规则只能应用到独立的业务命名空间。",
            next_action="检查目标命名空间的归属，重新获取宿主固定的 FD。",
        )
    return path, pinned


def _pin_rootless_pair(stack: ExitStack, network_fd: int, user_namespace_fd: int):
    import fcntl

    network_path, net_fd = _pin_namespace(stack, network_fd, "net", check_pid1=False)
    user_path, user_fd = _pin_namespace(stack, user_namespace_fd, "user", check_pid1=False)
    owning_user_fd = fcntl.ioctl(net_fd, _NS_GET_USERNS)
    stack.callback(os.close, owning_user_fd)
    if not os.path.samestat(os.fstat(owning_user_fd), os.fstat(user_fd)):
        raise OwnerEgressPolicyError(
            "EGRESS_NAMESPACE_OWNER_MISMATCH", "网络命名空间不属于提供的用户命名空间。",
            next_action="重新读取同一受管运行环境的网络与用户命名空间 FD。",
        )
    return network_path, net_fd, user_path, user_fd


@dataclass(frozen=True, eq=False)
class LinuxOwnerEgressNamespace:
    owner_id: str
    _network_fd: int
    _user_fd: int

    def _require_active(self):
        if self not in _ISSUED:
            raise OwnerEgressPolicyError(
                "EGRESS_NAMESPACE_UNREGISTERED", "任务没有仍有效的宿主网络命名空间绑定。",
                status_code=503, next_action="由受信宿主重新核对并固定该用户的隔离运行环境。",
            )

    def require_active(self):
        with _LEASE_LOCK:
            self._require_active()

    @contextmanager
    def _pin(self):
        with ExitStack() as stack:
            with _LEASE_LOCK:
                self._require_active()
                pair = _pin_rootless_pair(stack, self._network_fd, self._user_fd)
            yield pair

    def apply(self, policy: OwnerEgressPolicy) -> None:
        with self._pin() as (_, network_fd, _, user_fd):
            apply_linux_namespace_egress(network_fd, policy, user_namespace_fd=user_fd)

    def verify(self, policy: OwnerEgressPolicy) -> None:
        with self._pin() as (network_path, net_fd, user_path, user_fd):
            selector = ("tables",) if policy.mode == "unrestricted" else ("table", "inet", _TABLE)
            try:
                result = _run_nft(
                    [f"--net={network_path}", f"--user={user_path}", "--preserve-credentials", "--keep-caps"],
                    (net_fd, user_fd),
                    ("-j", "list", *selector), capture=True,
                )
            except (subprocess.TimeoutExpired, OSError) as exc:
                raise _verify_error(type(exc).__name__) from exc
            if result.returncode != 0:
                raise _verify_error(f"nft 读回退出码 {result.returncode}")
            try:
                matches = _matches_nft_rules(json.loads(result.stdout), policy)
            except (ValueError, TypeError, KeyError) as exc:
                raise _verify_error(type(exc).__name__) from exc
            if matches:
                return
        raise _verify_error("规则读回与预期不同")

    @contextmanager
    def command_prefix(self):
        """准备进入命名空间并降权。接收该前缀的可信垫片必须在运行前关闭这两个 FD。"""
        with self._pin() as (network_path, network_fd, user_path, user_fd):
            command = [
                "/usr/bin/nsenter", "--preserve-credentials", "--keep-caps", f"--user={user_path}", f"--net={network_path}",
                "--", "/usr/bin/setpriv", "--bounding-set=-all", "--inh-caps=-all", "--ambient-caps=-all",
                "--no-new-privs", "--",
            ]
            yield command, (network_fd, user_fd)


@contextmanager
def bind_linux_owner_namespace(
    owner_id: str, network_fd: int, user_fd: int, *,
    gateway_root: Path | None = None, authority_root: Path | None = None,
):
    """固定宿主已经验证归属的两个内核命名空间。业务参数不能持有该对象。"""
    owner = owner_identity_from_owner_id(owner_id)
    if sys.platform != "linux" or owner.owner_kind != "user" or _owner_id(owner) != owner_id or any(
        root is not None and not root.is_absolute() for root in (gateway_root, authority_root)
    ):
        raise OwnerEgressPolicyError(
            "EGRESS_NAMESPACE_BIND_UNAVAILABLE", "只能在 Linux 为规范普通用户固定业务命名空间。",
            status_code=503, next_action="检查 Linux 宿主和可信用户目录。",
        )
    if os.geteuid() == 0 or any(group != os.getegid() for group in os.getgroups()):
        raise OwnerEgressPolicyError(
            "EGRESS_GATEWAY_CREDENTIALS_UNSUPPORTED", "出站隔离要求 Gateway 使用独立的非 root 账号且没有额外组。",
            status_code=503, next_action="使用专用 Gateway 账号和主组启动，再绑定已由宿主准备的命名空间。",
        )
    with ExitStack() as stack:
        _, net_pinned, _, user_pinned = _pin_rootless_pair(stack, network_fd, user_fd)
        lease = LinuxOwnerEgressNamespace(owner_id, net_pinned, user_pinned)
        entries = tuple((stats.st_dev, stats.st_ino) for stats in (os.fstat(net_pinned), os.fstat(user_pinned)))
        scopes = tuple((table, (root, owner)) for table, root in (
            (_GATEWAY_OWNERS, gateway_root), (_AUTHORITY_OWNERS, authority_root),
        ) if root is not None)
        with _LEASE_LOCK:
            if any(entry in _NAMESPACES for entry in entries) or any(scope in table for table, scope in scopes):
                raise OwnerEgressPolicyError(
                    "EGRESS_NAMESPACE_ALREADY_BOUND", "网络或用户命名空间已绑定到一个活跃业务环境。",
                    status_code=409, next_action="为该用户使用独立命名空间，或复用宿主已签发的原绑定。",
                )
            for entry in entries:
                _NAMESPACES[entry] = lease
            for table, scope in scopes:
                table[scope] = lease
            _ISSUED.add(lease)
        try:
            yield lease
        finally:
            with _LEASE_LOCK:
                _ISSUED.discard(lease)
                for entry in entries:
                    del _NAMESPACES[entry]
                for table, scope in scopes:
                    del table[scope]


def current_linux_owner_namespace(root: Path, owner, *, by_authority: bool = False) -> LinuxOwnerEgressNamespace | None:
    """原 C 从自身 Gateway 根和已核验 owner 查找仍有效的宿主绑定。"""
    with _LEASE_LOCK:
        return (_AUTHORITY_OWNERS if by_authority else _GATEWAY_OWNERS).get((root, owner))


def owner_egress_identity(owner_home: Path, authority_root: Path | None = None):
    """由宿主 owner home 解析规范普通用户与数据根。"""
    from ..path_access_policy import agent_home_root_for_owner

    home = Path(owner_home).resolve(strict=False)
    root = Path(authority_root).resolve(strict=False) if authority_root is not None else agent_home_root_for_owner(home)
    if root is None:
        return None
    try:
        owner = owner_identity_from_owner_id(home.relative_to(root / "owners").as_posix())
    except ValueError:
        return None
    return (root, owner) if owner.owner_kind == "user" else None


def launch_owner_egress_process(owner_home: Path, argv, *, authority_root: Path | None = None, **options):
    """已绑定的普通业务进程出生即进入同一出站作用域。"""
    with stage_owner_egress_process(owner_home, argv, authority_root=authority_root, **options) as launch:
        return launch()


@contextmanager
def stage_owner_egress_process(owner_home: Path, argv, *, authority_root: Path | None = None, **options):
    """隔离垫片在业务锁外准备，短门内只发一次放行信号。"""
    from .owner_egress import require_owner_process_egress

    scope = owner_egress_identity(owner_home, authority_root) if owner_home is not None else None
    namespace = current_linux_owner_namespace(*scope, by_authority=True) if scope is not None else None
    if namespace is None:
        if owner_home is not None:
            require_owner_process_egress(owner_home, authority_root)
        yield lambda: subprocess.Popen(argv, **options)
        return
    with _stage_isolated(namespace, argv, options) as release:
        yield release


@contextmanager
def _stage_isolated(namespace, argv, options):
    from ..common.executor_registration import ExecutorTextIO
    from ..gateway_parts.multiprocess_executor_process import (
        abort_waiting,
        await_waiting,
        prepare_waiting_context,
        release_prepared_waiting,
        spawn_waiting,
    )
    from ..gateway_parts.multiprocess_executor_start import ExecutorCommand, ExecutorIO

    if (options.keys() - {"stdin", "stdout", "stderr", "start_new_session", "close_fds", "cwd", "env", "pass_fds",
                           "text", "encoding", "errors", "bufsize", "shell", "creationflags"}
            or options.get("start_new_session") is not True or options.get("close_fds", True) is not True
            or options.get("creationflags", 0) != 0):
        raise OwnerEgressPolicyError(
            "EGRESS_PROCESS_OPTIONS_UNSUPPORTED", "隔离出生仅支持明确生命周期、标准流和批准的环境参数。",
            next_action="使用独立进程组、关闭额外 FD，并通过 stdin、stdout、stderr 显式配置标准流。",
        )
    args = (argv,) if isinstance(argv, (str, bytes, os.PathLike)) else tuple(argv)
    args = tuple(os.fsdecode(arg) for arg in args)
    if options.get("shell"):
        args = ("/bin/sh", "-c", *args)
    text = None
    if options.get("text") or options.get("encoding") or options.get("errors"):
        text = ExecutorTextIO(options.get("encoding"), options.get("errors"), options.get("bufsize", -1))
    command = ExecutorCommand(
        args, options.get("cwd"), options.get("env"),
        ExecutorIO(tuple(options.get(name) for name in ("stdin", "stdout", "stderr")),
                   tuple(options.get("pass_fds", ())), text),
        egress_namespace=namespace,
    )
    waiting, released = None, False
    try:
        waiting = spawn_waiting(command)
        await_waiting(waiting)
        if waiting.invalid_fds:
            raise ValueError("invalid_executor_fd_source")
        prepare_waiting_context(waiting)
    except BaseException as exc:
        cleanup = abort_waiting(waiting)
        if not isinstance(exc, Exception) or isinstance(exc, OwnerEgressPolicyError):
            raise
        raise OwnerEgressPolicyError(
            "EGRESS_PROCESS_START_FAILED", f"隔离进程未能完成启动（{type(exc).__name__}）。",
            status_code=503, side_effect="not_started" if cleanup["process_reaped"] else "unconfirmed",
            next_action="检查业务进程状态、Linux 命名空间和沙箱权限，再尝试启动。",
        ) from exc

    def launch():
        nonlocal released
        if released:
            raise RuntimeError("egress_isolated_process_already_released")
        namespace.require_active()
        try:
            release_prepared_waiting(waiting)
        except OSError as exc:
            raise OwnerEgressPolicyError(
                "EGRESS_PROCESS_RELEASE_UNCONFIRMED", "隔离启动器的放行回执未确认。",
                status_code=503, side_effect="unconfirmed",
                next_action="核对受管任务进程与业务状态，确认没有副作用后再尝试启动。",
            ) from exc
        released = True
        return waiting.process

    try:
        yield launch
    finally:
        if not released:
            abort_waiting(waiting)


def _verify_error(reason: str) -> OwnerEgressPolicyError:
    return OwnerEgressPolicyError(
        "EGRESS_KERNEL_READBACK_FAILED", f"无法确认独立命名空间的内核规则（{reason}）。",
        status_code=503, side_effect="unconfirmed",
        next_action="暂停该用户的新业务，核对内核规则并修复隔离后再提交。",
    )


def _matches_nft_rules(document: dict, policy: OwnerEgressPolicy) -> bool:
    rows = document["nftables"]
    tables = [entry["table"] for entry in rows if entry.get("table", {}).get("name") == _TABLE]
    if policy.mode == "unrestricted":
        return not tables
    if policy.mode != "allowlist" or len(tables) != 1:
        return False
    if {key: value for key, value in tables[0].items() if key != "handle"} != {"family": "inet", "name": _TABLE}:
        return False
    base = {"family": "inet", "table": _TABLE}
    expected = [
        ("chain", {**base, "name": name, "type": "filter", "hook": hook, "prio": 0, "policy": "drop"})
        for name, hook in (("egress", "output"), ("ingress", "input"))
    ]
    for version, family, address_type in ((4, "ip", "ipv4_addr"), (6, "ip6", "ipv6_addr")):
        entries = sorted((item.ip, item.port) for item in policy.endpoints if (":" in item.ip) == (version == 6))
        if not entries:
            continue
        name = f"endpoints{version}"
        expected.append(("set", {**base, "name": name, "type": [address_type, "inet_service"], "elem": entries}))
        for chain, address, port in (("egress", "daddr", "dport"), ("ingress", "saddr", "sport")):
            match = []
            if chain == "ingress":
                match.append({"match": {"op": "==", "left": {"ct": {"key": "direction"}}, "right": "reply"}})
            match += [
                {"match": {"op": "==", "left": {"meta": {"key": "l4proto"}}, "right": {"set": ["tcp", "udp"]}}},
                {"match": {"op": "==", "left": {"concat": [
                    {"payload": {"protocol": family, "field": address}},
                    {"payload": {"protocol": "th", "field": port}},
                ]}, "right": "@" + name}},
                {"accept": None},
            ]
            expected.append(("rule", {**base, "chain": chain, "expr": match}))
    found = []
    for entry in rows:
        if len(entry) != 1:
            return False
        name, item = next(iter(entry.items()))
        if name in {"table", "metainfo"}:
            continue
        value = {key: attr for key, attr in item.items() if key != "handle"}
        if name == "set":
            value["elem"] = sorted(tuple(element["concat"]) for element in value["elem"])
        found.append((name, value))
    return len(found) == len(expected) and all(entry in found for entry in expected)


def _run_nft(arguments, descriptors, command, *, batch=None, capture=False):
    return subprocess.run(
        ["/usr/bin/nsenter", *arguments, "--", "/usr/sbin/nft", *command],
        input=batch, text=True, stdout=subprocess.PIPE if capture else subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        pass_fds=descriptors, timeout=5, check=False, env={"PATH": "/usr/sbin:/usr/bin", "LC_ALL": "C"},
    )


def apply_linux_namespace_egress(
    namespace_fd: int, policy: OwnerEgressPolicy, *, user_namespace_fd: int | None = None,
) -> None:
    """原子替换指定隔离命名空间的规则。调用方须验证进程归属、隔离与控制面边界。"""
    if sys.platform != "linux":
        raise OwnerEgressPolicyError(
            "EGRESS_ENFORCEMENT_UNAVAILABLE", "当前平台没有 Linux nftables 命名空间强制能力。",
            status_code=503, next_action="使用受支持并完成隔离部署的 Linux 宿主。",
        )
    batch = _nft_batch(policy)
    try:
        with ExitStack() as stack:
            if user_namespace_fd is None:
                target, fd = _pin_namespace(stack, namespace_fd, "net", check_pid1=True)
                arguments, descriptors = [f"--net={target}"], (fd,)
            else:
                target, fd, user_path, user_fd = _pin_rootless_pair(stack, namespace_fd, user_namespace_fd)
                arguments, descriptors = [
                    f"--net={target}", f"--user={user_path}", "--preserve-credentials", "--keep-caps",
                ], (fd, user_fd)
            result = _run_nft(arguments, descriptors, ("-f", "-"), batch=batch)
    except subprocess.TimeoutExpired as exc:
        raise OwnerEgressPolicyError(
            "EGRESS_KERNEL_UPDATE_UNCONFIRMED", "等待 nftables 规则提交超过 5 秒，内核状态未确认。",
            status_code=503, side_effect="unconfirmed",
            next_action="暂停目标命名空间的业务执行，读取其内核规则后再决定是否重试。",
        ) from exc
    except OSError as exc:
        raise OwnerEgressPolicyError(
            "EGRESS_KERNEL_COMMAND_ERROR", f"无法访问命名空间或运行 nftables（{type(exc).__name__}，errno={exc.errno}）。",
            status_code=503, next_action="检查固定 FD、nsenter/nft 工具及宿主权限。",
        ) from exc
    if result.returncode != 0:
        raise OwnerEgressPolicyError(
            "EGRESS_KERNEL_UPDATE_FAILED", f"nsenter 或 nftables 更新失败（退出码 {result.returncode}）。",
            status_code=503, side_effect="unconfirmed",
            next_action="检查目标命名空间权限和 nftables 版本，并核对内核现存规则。",
        )
