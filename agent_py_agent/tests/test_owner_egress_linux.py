"""Linux 实核验。仅在独立网络容器中显式开启，不接触宿主网络。"""
from __future__ import annotations

import json
import os
import select
import socket
import subprocess
import sys
import threading
import time
from contextlib import ExitStack
from pathlib import Path

import pytest
from agent.gateway_parts.multiprocess_executor_ledger import ExecutorLedger
from agent.gateway_parts.multiprocess_executor_start import (
    ExecutorCommand,
    ExecutorIO,
    start_executor,
)
from agent.gateway_parts.multiprocess_route import WorkerPartition, owner_digest
from agent.user_space.owner_egress import OwnerEgressPolicy, OwnerEgressPolicyError
from agent.user_space.owner_egress_linux import (
    apply_linux_namespace_egress,
    bind_linux_owner_namespace,
    current_linux_owner_namespace,
    launch_owner_egress_process,
)
from agent.user_space.owner_resolver import OwnerIdentity

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("MY_AGENT_TEST_ISOLATED_NETNS") != "1",
    reason="需要单独隔离的 Linux 测试容器以及 NET_ADMIN、SYS_ADMIN。",
)

_CLIENT = """
import socket, sys
sock = socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=0.5)
print('connected', flush=True)
for line in sys.stdin:
    try:
        sock.sendall(line.encode())
        print(sock.recv(1024).decode().strip(), flush=True)
    except TimeoutError:
        print('TIMEOUT', flush=True)
"""


def _run(*argv):
    return subprocess.check_output(argv, text=True, timeout=3)


def _reap(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
    for stream in (process.stdin, process.stdout):
        if stream is not None:
            stream.close()


def _server(stack, family, address):
    listener = stack.enter_context(socket.socket(family))
    listener.bind((address, 0))
    listener.listen()
    listener.settimeout(2)

    def echo():
        try:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(2)
                while data := connection.recv(1024):
                    connection.sendall(data)
        except OSError:
            return

    worker = threading.Thread(target=echo)
    worker.start()
    stack.callback(worker.join, 2.1)
    return listener.getsockname()[:2]


def _network(stack, index):
    process = subprocess.Popen(["unshare", "--net", "sleep", "30"])
    stack.callback(_reap, process)
    target = Path(f"/proc/{process.pid}/ns/net")
    original = Path("/proc/self/ns/net").readlink()
    deadline = time.monotonic() + 2
    while target.readlink() == original:
        if time.monotonic() >= deadline:
            raise TimeoutError("独立网络命名空间未创建")
        time.sleep(0.01)
    namespace = os.open(target, os.O_RDONLY)
    stack.callback(os.close, namespace)
    command = ("nsenter", "-t", str(process.pid), "-n", "--")
    host, peer = f"host{index}", f"peer{index}"
    _run("ip", "link", "add", host, "type", "veth", "peer", "name", peer)
    stack.callback(_run, "ip", "link", "del", host)
    _run("ip", "link", "set", peer, "netns", str(process.pid))
    host4, peer4 = f"198.18.{index}.1", f"198.18.{index}.2"
    host6, peer6 = f"fd12:3456:{index}::1", f"fd12:3456:{index}::2"
    _run("ip", "addr", "add", f"{host4}/24", "dev", host)
    _run("ip", "link", "set", host, "up")
    _run(*command, "ip", "addr", "add", f"{peer4}/24", "dev", peer)
    _run(*command, "ip", "link", "set", peer, "up")
    _run("ip", "-6", "addr", "add", f"{host6}/64", "dev", host, "nodad")
    _run(*command, "ip", "-6", "addr", "add", f"{peer6}/64", "dev", peer, "nodad")
    # 静态邻居让探测只发送业务包，避免依赖空名单之外的 ICMPv6 邻居发现。
    host_mac = Path(f"/sys/class/net/{host}/address").read_text().strip()
    peer_mac = json.loads(_run(*command, "ip", "-j", "link", "show", peer))[0]["address"]
    _run("ip", "-6", "neigh", "add", peer6, "lladdr", peer_mac, "nud", "permanent", "dev", host)
    _run(*command, "ip", "-6", "neigh", "add", host6, "lladdr", host_mac, "nud", "permanent", "dev", peer)
    return namespace, command, _server(stack, socket.AF_INET, host4), _server(stack, socket.AF_INET6, host6)


def _read(process):
    assert select.select([process.stdout], [], [], 2)[0]
    return process.stdout.readline().strip()


def _client(stack, namespace_command, address):
    process = subprocess.Popen(
        [*namespace_command, "setpriv", "--reuid", "65534", "--regid", "65534", "--clear-groups",
         "--bounding-set=-all", "--no-new-privs", sys.executable, "-c", _CLIENT, address[0], str(address[1])],
        text=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    stack.callback(_reap, process)
    assert _read(process) == "connected"
    return process


def _ping(process, line):
    process.stdin.write(line + "\n")
    process.stdin.flush()
    return _read(process)


def _policy(mode, *destinations):
    return OwnerEgressPolicy.from_dict({"mode": mode, "endpoints": [{"ip": ip, "port": port} for ip, port in destinations]})


def test_revoke_traffic_on_existing_ipv4_and_ipv6_connections():
    assert os.geteuid() == 0
    links = json.loads(_run("ip", "-j", "link", "show"))
    assert all(link["ifname"] == "lo" or "UP" not in link["flags"] for link in links)
    assert not json.loads(_run("ip", "-j", "route", "show", "default"))
    assert not json.loads(_run("ip", "-6", "-j", "route", "show", "default"))
    with ExitStack() as stack:
        fd_a, command_a, address_a, _ = _network(stack, 10)
        fd_b, command_b, address_b, address_b6 = _network(stack, 11)
        fd_c, command_c, address_c, _ = _network(stack, 12)
        apply_linux_namespace_egress(fd_a, _policy("unrestricted"))
        apply_linux_namespace_egress(fd_b, _policy("allowlist", address_b, address_b6))
        apply_linux_namespace_egress(fd_c, _policy("allowlist", address_c))
        a_client = _client(stack, command_a, address_a)
        b_client = _client(stack, command_b, address_b)
        b6_client = _client(stack, command_b, address_b6)
        c_client = _client(stack, command_c, address_c)
        assert _ping(a_client, "before") == "before"
        assert _ping(b_client, "before") == "before"
        assert _ping(b6_client, "before") == "before"
        assert _ping(c_client, "before") == "before"
        apply_linux_namespace_egress(fd_b, _policy("allowlist"))
        assert _ping(a_client, "after") == "after"
        assert _ping(b_client, "after") == "TIMEOUT"
        assert _ping(b6_client, "after") == "TIMEOUT"
        assert _ping(c_client, "after") == "after"
        wrong_port = address_b[1] + 1 if address_b[1] < 65535 else address_b[1] - 1
        apply_linux_namespace_egress(fd_b, _policy("allowlist", (address_b[0], wrong_port)))
        probe = subprocess.run(
            [*command_b, sys.executable, "-c",
             "import socket, sys; socket.create_connection((sys.argv[1], int(sys.argv[2])), .3)",
             address_b[0], str(address_b[1])],
            capture_output=True, text=True, timeout=2,
        )
        assert probe.returncode != 0 and "TimeoutError" in probe.stderr
        with open("/proc/self/ns/net", "rb") as parent_namespace:
            with pytest.raises(OwnerEgressPolicyError) as caught:
                apply_linux_namespace_egress(parent_namespace.fileno(), _policy("allowlist"))
            assert caught.value.code == "INVALID_EGRESS_NAMESPACE"


def test_rootless_registered_executor_birth_is_isolated(tmp_path):
    assert os.geteuid() != 0
    assert "CapEff:\t0000000000000000" in Path("/proc/self/status").read_text()
    with ExitStack() as stack:
        anchor = subprocess.Popen(["unshare", "--user", "--map-current-user", "--net", "sleep", "30"])
        stack.callback(_reap, anchor)
        net_path = Path(f"/proc/{anchor.pid}/ns/net")
        parent = Path("/proc/self/ns/net").readlink()
        deadline = time.monotonic() + 2
        while net_path.readlink() == parent:
            if time.monotonic() >= deadline:
                raise TimeoutError("子用户的独立网络命名空间未创建")
            time.sleep(0.01)
        net = stack.enter_context(net_path.open("rb"))
        user = stack.enter_context(Path(f"/proc/{anchor.pid}/ns/user").open("rb"))
        lease = stack.enter_context(bind_linux_owner_namespace(
            "local/alpha", net.fileno(), user.fileno(), gateway_root=tmp_path, authority_root=tmp_path,
        ))
        assert current_linux_owner_namespace(tmp_path, OwnerIdentity.local_user("alpha")) is lease
        assert current_linux_owner_namespace(tmp_path, OwnerIdentity.local_user("beta")) is None
        assert current_linux_owner_namespace(tmp_path / "other", OwnerIdentity.local_user("alpha")) is None
        with pytest.raises(OwnerEgressPolicyError, match="已绑定"):
            with bind_linux_owner_namespace("local/beta", net.fileno(), user.fileno()):
                pytest.fail("两个 owner 不能复用命名空间")
        lease.apply(_policy("allowlist"))
        lease.verify(_policy("allowlist"))
        with pytest.raises(OwnerEgressPolicyError) as mismatch:
            lease.verify(_policy("unrestricted"))
        assert mismatch.value.code == "EGRESS_KERNEL_READBACK_FAILED"
        ledger = ExecutorLedger(tmp_path, WorkerPartition(0, 1, "c-test"))
        reservation = {
            "schema_version": "multiprocess_executor_reservation.v1", "reservation_id": "exec-rootless",
            "kind": "shell", "c_run_id": "c-test", "owner_sha": owner_digest(OwnerIdentity.local_user("alpha")),
            "request_id": "request-test", "execution_attempt_id": "attempt-test", "lease_epoch": 1,
            "tool_call_id": "tool-test", "state": "reserved", "process_identity": None, "pgid": None, "sid": None,
        }
        probe = (
            "import os, subprocess; from pathlib import Path; "
            "print(os.readlink('/proc/self/ns/net')); "
            "status = Path('/proc/self/status').read_text().splitlines(); "
            "print(next(line for line in status if line.startswith('CapEff:'))); "
            "print(next(line for line in status if line.startswith('NoNewPrivs:'))); "
            "print(max(int(fd) for fd in os.listdir('/proc/self/fd'))); "
            "assert subprocess.run(['/usr/sbin/nft','list','ruleset'], capture_output=True).returncode != 0"
        )
        command = ExecutorCommand(
            (sys.executable, "-I", "-S", "-c", probe), io=ExecutorIO((subprocess.DEVNULL, subprocess.PIPE, subprocess.PIPE)),
            egress_namespace=lease,
        )
        refused = start_executor(ledger, {**reservation, "owner_sha": owner_digest(OwnerIdentity.local_user("beta"))}, command)
        assert not refused.ok
        assert ledger.read(reservation["reservation_id"]) is None
        started = start_executor(ledger, reservation, command)
        assert started.ok, started.error
        process = started.process
        stack.callback(_reap, process)
        stdout, stderr = process.communicate(timeout=3)
        assert process.returncode == 0, stderr
        assert stdout.decode().splitlines() == [
            str(net_path.readlink()), "CapEff:\t0000000000000000", "NoNewPrivs:\t1", "3",
        ]
        assert ledger.read(reservation["reservation_id"])["state"] == "registered"
        owner_home = tmp_path / "owners" / "local" / "alpha"
        left, right = socket.socketpair()
        with left as forbidden, right:
            with pytest.raises(OwnerEgressPolicyError) as inherited:
                launch_owner_egress_process(
                    owner_home, command.argv, authority_root=tmp_path, stdin=forbidden,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                )
        assert inherited.value.code == "EGRESS_INHERITED_SOCKET_UNVERIFIED"
        with launch_owner_egress_process(
            owner_home, command.argv, authority_root=tmp_path, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        ) as direct:
            direct_stdout, direct_stderr = direct.communicate(timeout=3)
            assert direct.returncode == 0, direct_stderr
            assert direct_stdout == stdout
