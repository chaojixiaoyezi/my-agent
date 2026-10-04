"""G4（Gateway 本机信任第 (1) 层）：模型命令沙箱按端口拒绝连本机 Gateway。

覆盖：
- 进程内端口注册表（登记/注销/快照、坏输入忽略）；
- Seatbelt profile 只在有端口时加 `*:<port>` 出站拒绝，空时逐字节不变；
- `_sandbox_exec` 构造模型命令沙箱时带上注册表里的端口；
- 真实 Seatbelt 下：带规则时连 Gateway 端口（127.0.0.1 / ::1 / ::ffff:127.0.0.1 / ::ffff:7f00:1 /
  0.0.0.0 / 局域网地址）都被拒(EPERM)，其它端口通、沙箱内自起监听通；不带规则时 IPv4 映射地址能连上、
  服务端认成 127.0.0.1（= 会被当本机管理员，证明为什么必须用 `*:`）；full_access 也拒；断网档不变；
- Gateway HTTP 服务启动登记端口、停机注销。

真实 Seatbelt 测试只连测试进程自起的随机端口，不碰生产 8420。
"""

from __future__ import annotations

import errno
import json
import socket
import sys
import threading
from pathlib import Path

import pytest

from agent_py_agent.agent.attempt.sandbox import (
    AttemptExecutionSandbox,
    AttemptSandboxSpec,
    _gateway_port_denies,
    _spec_rules,
    gateway_bound_ports,
    register_gateway_bound_port,
    unregister_gateway_bound_port,
)

IS_MACOS = sys.platform == "darwin"
needs_macos = pytest.mark.skipif(not IS_MACOS, reason="macOS Seatbelt 用例")
needs_nested_sandbox_exec = pytest.mark.sandbox_capability("nested_sandbox_exec")


# LLM: LAN Seatbelt 用例只有在本机有非回环 IPv4 地址时才适用；先取本机事实，
#   让平台/前置条件 skip 在能力门之前生效，避免把本来无需运行的 case 计作能力缺失。
# 函数用途: 读取本机用于 LAN 规则测试的 IPv4 地址；无法解析时返回空串并跳过该测试。
def _local_lan_address() -> str:
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return ""


_LAN_ADDRESS = _local_lan_address()
needs_lan_address = pytest.mark.skipif(
    not _LAN_ADDRESS or _LAN_ADDRESS.startswith("127."),
    reason="没有非回环本机地址",
)

# 沙箱里跑的客户端：连 host:port，回报成功或 errno。只连不收发，立即关闭。
_CLIENT = r"""
import json, socket, sys
host, port = sys.argv[1], int(sys.argv[2])
fam = socket.AF_INET6 if ":" in host else socket.AF_INET
s = socket.socket(fam, socket.SOCK_STREAM); s.settimeout(3)
try:
    s.connect((host, port)); print(json.dumps({"ok": True}))
except OSError as exc:
    print(json.dumps({"ok": False, "errno": exc.errno}))
finally:
    s.close()
"""

# 沙箱里自起监听再连自己：证明端口拒绝只挡出站连 Gateway，不挡命令自建服务。
_SELF_SERVE = r"""
import json, socket
srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
c = socket.socket(); c.settimeout(3)
try:
    c.connect(("127.0.0.1", srv.getsockname()[1])); print(json.dumps({"ok": True}))
except OSError as exc:
    print(json.dumps({"ok": False, "errno": exc.errno}))
"""


# ----------------------------------------------------------------- 注册表


def test_register_and_unregister_bound_ports():
    register_gateway_bound_port(48201)
    register_gateway_bound_port(48201)  # 幂等
    register_gateway_bound_port(48202)
    try:
        assert set(gateway_bound_ports()) >= {48201, 48202}
    finally:
        unregister_gateway_bound_port(48201)
        unregister_gateway_bound_port(48202)
    assert 48201 not in gateway_bound_ports() and 48202 not in gateway_bound_ports()


def test_registry_ignores_bad_ports():
    before = gateway_bound_ports()
    for bad in (0, -1, None, "x", 1.5):
        register_gateway_bound_port(bad)
    assert gateway_bound_ports() == before


# ----------------------------------------------------------------- profile


def test_gateway_port_denies_uses_star_host_not_localhost():
    rules = _gateway_port_denies((8420,))
    assert rules == ['(deny network-outbound (remote tcp "*:8420"))']
    # 不能是 localhost:：那样挡不住 ::ffff:127.0.0.1
    assert "localhost" not in rules[0]


def test_gateway_port_denies_empty_is_bytewise_unchanged():
    assert _gateway_port_denies(()) == []


def _spec(tmp_path: Path, *, ports: tuple[int, ...] = (), network: bool = True,
          full_access: bool = False) -> AttemptSandboxSpec:
    root = tmp_path / "s"
    root.mkdir(exist_ok=True)
    return AttemptSandboxSpec(
        attempt_view=root, staging_root=root, shared_workspace=root, owner_home=root,
        network_access=network, full_access=full_access, deny_gateway_ports=ports,
    )


def test_spec_rules_add_gateway_deny_only_when_ports_present(tmp_path):
    assert not any("network-outbound" in r for r in _spec_rules(_spec(tmp_path)))
    with_port = _spec_rules(_spec(tmp_path, ports=(8420,)))
    assert '(deny network-outbound (remote tcp "*:8420"))' in with_port


def test_spec_rules_network_off_still_denies_all(tmp_path):
    rules = _spec_rules(_spec(tmp_path, network=False))
    assert "(deny network*)" in rules


# ------------------------------------------------- _sandbox_exec 透传注册表


def test_sandbox_exec_carries_registered_gateway_port(tmp_path):
    from agent_py_agent.agent.tooling.shell import _sandbox_exec

    register_gateway_bound_port(49311)
    try:
        argv, _ = _sandbox_exec("echo hi", tmp_path / "task", None)
    finally:
        unregister_gateway_bound_port(49311)
    if not IS_MACOS:
        pytest.skip("非 macOS 不检查 Seatbelt profile 文本")
    profile = argv[argv.index("-p") + 1]
    assert '(deny network-outbound (remote tcp "*:49311"))' in profile


# ------------------------------------------------------- 真实 Seatbelt 网络


class _Listener:
    """只绑 IPv4 127.0.0.1 的监听（和 Gateway 一样），记录服务端认出的对端地址。"""

    def __init__(self) -> None:
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(16)
        self.port = self.srv.getsockname()[1]
        self.peers: list[str] = []
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while True:
            try:
                conn, addr = self.srv.accept()
            except OSError:
                return
            self.peers.append(addr[0])
            conn.close()

    def close(self) -> None:
        self.srv.close()


def _run_client(sandbox: AttemptExecutionSandbox, host: str, port: int) -> dict:
    result = sandbox.run([sys.executable, "-c", _CLIENT, host, str(port)], timeout=30)
    return json.loads(result.stdout.strip())


def _blocked(outcome: dict) -> bool:
    return outcome.get("ok") is False and outcome.get("errno") == errno.EPERM


@needs_macos
@needs_nested_sandbox_exec
def test_localhost_rule_is_bypassed_by_mapped_v4_but_star_rule_is_not(tmp_path):
    """没有端口规则时 ::ffff:127.0.0.1 能连上只绑 IPv4 的 Gateway 式监听、服务端认成 127.0.0.1（= 本机管理员）；
    这正是必须用 `*:<port>` 而不是 localhost:<port> 的原因。"""
    listener = _Listener()
    open_sandbox = AttemptExecutionSandbox(_spec(tmp_path))
    if not open_sandbox.probe().ready:
        listener.close()
        pytest.skip("sandbox-exec 不可用")
    try:
        mapped = _run_client(open_sandbox, "::ffff:127.0.0.1", listener.port)
        assert mapped.get("ok") is True, "无规则时 IPv4 映射地址应连得上"
        assert listener.peers and all(p == "127.0.0.1" for p in listener.peers), \
            "服务端把 IPv4 映射连接认成 127.0.0.1——无规则时就是本机管理员"
    finally:
        listener.close()


@needs_macos
@needs_nested_sandbox_exec
@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "::ffff:127.0.0.1", "::ffff:7f00:1", "0.0.0.0"])
def test_star_rule_blocks_every_gateway_port_target(tmp_path, host):
    listener = _Listener()
    sandbox = AttemptExecutionSandbox(_spec(tmp_path, ports=(listener.port,)))
    if not sandbox.probe().ready:
        listener.close()
        pytest.skip("sandbox-exec 不可用")
    try:
        outcome = _run_client(sandbox, host, listener.port)
        assert _blocked(outcome), f"{host}:{listener.port} 应被 Seatbelt 拒绝(EPERM)，实得 {outcome}"
    finally:
        listener.close()
    # 被拒的连接不应到达服务端
    assert listener.peers == [], f"被拒目标不该被服务端看到对端：{listener.peers}"


@needs_macos
@needs_lan_address
@needs_nested_sandbox_exec
def test_star_rule_blocks_lan_address_for_gateway_port(tmp_path):
    lan = _LAN_ADDRESS
    listener = _Listener()
    sandbox = AttemptExecutionSandbox(_spec(tmp_path, ports=(listener.port,)))
    if not sandbox.probe().ready:
        listener.close()
        pytest.skip("sandbox-exec 不可用")
    try:
        assert _blocked(_run_client(sandbox, lan, listener.port))
    finally:
        listener.close()


@needs_macos
@needs_nested_sandbox_exec
def test_other_ports_and_self_listen_still_work(tmp_path):
    gateway = _Listener()
    other = _Listener()
    sandbox = AttemptExecutionSandbox(_spec(tmp_path, ports=(gateway.port,)))
    if not sandbox.probe().ready:
        gateway.close()
        other.close()
        pytest.skip("sandbox-exec 不可用")
    try:
        assert _run_client(sandbox, "127.0.0.1", other.port).get("ok") is True, "其它端口应照常可连"
        served = json.loads(sandbox.run([sys.executable, "-c", _SELF_SERVE], timeout=30).stdout.strip())
        assert served.get("ok") is True, "沙箱内自起监听应连得上"
    finally:
        gateway.close()
        other.close()


@needs_macos
@needs_nested_sandbox_exec
def test_full_access_also_blocks_gateway_port(tmp_path):
    listener = _Listener()
    sandbox = AttemptExecutionSandbox(_spec(tmp_path, ports=(listener.port,), full_access=True))
    if not sandbox.probe().ready:
        listener.close()
        pytest.skip("sandbox-exec 不可用")
    try:
        assert _blocked(_run_client(sandbox, "127.0.0.1", listener.port)), "full_access 档也要拒 Gateway 端口"
    finally:
        listener.close()


# ----------------------------------------------------- Gateway 启停登记端口


def test_gateway_start_registers_and_stop_unregisters_bound_port(tmp_path):
    from agent_py_agent.agent.gateway_parts.http_service import GatewayHTTPServer
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

    server = GatewayHTTPServer(0, gateway_paths_from_root(tmp_path / "gw"))
    server.start()
    try:
        bound = server.server.server_address[1]
        assert bound in gateway_bound_ports()
    finally:
        server.stop()
    assert bound not in gateway_bound_ports()


# ----------------------------------------------- gateway_isolation 结构化事实（唯一来源）


def test_isolation_status_three_values_by_platform(monkeypatch):
    from agent_py_agent.agent.attempt.sandbox import gateway_isolation_status

    assert gateway_isolation_status(()) == "not_applicable"
    assert gateway_isolation_status((8420,), system="Darwin") == "applied"
    assert gateway_isolation_status((8420,), system="SunOS") == "unavailable:unsupported_platform"
    # G5：Linux 的值由 landlock_net_readiness 决定（就绪 applied，否则 unavailable:<原因>）。
    monkeypatch.setattr("agent_py_agent.agent.attempt.sandbox.landlock_net_readiness",
                        lambda _p: (False, "LANDLOCK_NET_UNSUPPORTED"))
    assert gateway_isolation_status((8420,), system="Linux") == "unavailable:LANDLOCK_NET_UNSUPPORTED"


def test_sandbox_fact_delegates_to_status(tmp_path):
    # 沙箱对象的事实和模块级 status 同一口径（单一来源），不依赖 build_argv 副作用。
    from agent_py_agent.agent.attempt.sandbox import gateway_isolation_status

    with_port = AttemptExecutionSandbox(_spec(tmp_path, ports=(49555,)))
    assert with_port.gateway_isolation_fact() == gateway_isolation_status((49555,))
    without = AttemptExecutionSandbox(_spec(tmp_path))
    assert without.gateway_isolation_fact() == "not_applicable"


def test_status_endpoint_reports_gateway_isolation(tmp_path, monkeypatch):
    # /status 必须带 gateway_isolation 字段，三种值各一条；变异“写死 applied”要被抓到。
    import agent_py_agent.agent.gateway_parts.http_handlers as hh
    from agent_py_agent.agent.attempt import sandbox as sb

    sent = {}

    class _Handler:
        def _send_json(self, code, body):
            sent["code"] = code
            sent["body"] = body

    server = type("S", (), {"paths": type("P", (), {"state": tmp_path / "state.json"})()})()
    monkeypatch.setattr(hh, "gateway_request_counts", lambda *a, **k: {})
    monkeypatch.setattr(hh, "_usage_accounting_diagnostics", lambda: {})
    monkeypatch.setattr(hh, "adapter_process_facts", lambda *a, **k: {})
    # G5：Linux 分支会查 landlock_net_readiness，固定成不就绪以得到确定值。
    monkeypatch.setattr(sb, "landlock_net_readiness", lambda _p=None: (False, "LANDLOCK_NET_UNSUPPORTED"))

    for ports, system, expected in [((), "Linux", "not_applicable"),
                                    ((8420,), "Darwin", "applied"),
                                    ((8420,), "Linux", "unavailable:LANDLOCK_NET_UNSUPPORTED")]:
        monkeypatch.setattr(sb, "gateway_bound_ports", lambda p=ports: p)
        monkeypatch.setattr(sb, "platform", type("Pl", (), {"system": staticmethod(lambda s=system: s)}))
        hh.handle_status(_Handler(), server)
        assert sent["body"]["gateway_isolation"] == expected


# ----------------------------------------------- 启动顺序：登记早于线程


def test_gateway_run_threads_registers_port_before_request_loop(tmp_path, monkeypatch):
    """ae 必改：请求线程/后台线程在 HTTP 绑定前就能起模型命令，所以端口要在起线程之前预登记。
    这里让请求循环一进来就抓注册表快照，断言配置端口已经在里面。"""
    import agent_py_agent.cli.gateway_process as gp
    from agent_py_agent.cli.models import GatewayRunContext, GatewayThreadsRequest

    seen: dict[str, tuple[int, ...]] = {}
    started = threading.Event()

    def capture_request_loop(context, paths, stop_event):
        seen["at_request_loop"] = gateway_bound_ports()
        started.set()

    monkeypatch.setattr(gp, "_gateway_request_loop", capture_request_loop)
    monkeypatch.setattr(gp, "_gateway_background_main_loop", lambda *a, **k: None)
    monkeypatch.setattr(gp, "_gateway_heartbeat_loop", lambda *a, **k: None)
    monkeypatch.setattr(gp, "start_http_server", lambda *a, **k: None)
    monkeypatch.setattr(gp, "write_json_file", lambda *a, **k: None)
    monkeypatch.setattr(gp, "log_gateway_event", lambda *a, **k: None)
    monkeypatch.setattr(gp, "_build_run_state", lambda *a, **k: {})
    monkeypatch.setattr(gp, "_build_run_payload", lambda *a, **k: {})

    config = type("Cfg", (), {"gateway_bind_host": "127.0.0.1", "gateway_user_inflight_limit": 8,
                              "gateway_global_inflight_limit": 500})()
    agent = type("Agent", (), {"config": config})()
    paths = type("Paths", (), {"state": tmp_path / "state.json", "root": tmp_path})()
    context = GatewayRunContext(agent=agent, paths=paths, config_path=tmp_path / "c.yaml")
    request = GatewayThreadsRequest(context=context, requeued=0, failed=0, http_port=48550)

    monkeypatch.setattr(gp, "_build_gateway_auth_middleware", lambda *a, **k: None)
    gp._cmd_gateway_run_threads(request)
    assert started.wait(5)
    assert 48550 in seen["at_request_loop"], "请求循环启动时配置端口应已登记"
    unregister_gateway_bound_port(48550)


def test_start_gateway_http_unregisters_port_on_bind_failure(monkeypatch):
    """ae 必改：绑定失败时要注销起线程前预登记的端口，否则漏给同进程后续的模型命令。"""
    import agent_py_agent.cli.gateway_process as gp

    port = 48666
    register_gateway_bound_port(port)

    def boom(*_a, **_k):
        raise OSError("bind failed")

    monkeypatch.setattr(gp, "start_http_server", boom)
    monkeypatch.setattr(gp, "_build_gateway_auth_middleware", lambda *a, **k: None)
    config = type("Cfg", (), {"gateway_bind_host": "127.0.0.1"})()
    agent = type("Agent", (), {"config": config})()
    try:
        with pytest.raises(OSError):
            gp._start_gateway_http(agent, object(), port)
        assert port not in gateway_bound_ports(), "绑定失败必须注销预登记的端口"
    finally:
        unregister_gateway_bound_port(port)
