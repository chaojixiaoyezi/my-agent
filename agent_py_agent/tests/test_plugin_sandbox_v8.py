"""B7 安全底座：v8 插件沙箱强制断网 + 收窄读的真进程验证。

布局是“放行目录嵌在拒读根里”（插件环境和数据目录在 my-agent 数据根之下）：真实 Python 解释器启动、realpath，
读得到自己的环境和数据目录，读不到会话、记忆、secrets；network:false 连回环/外网都断，network:true 放开。

- macOS：真 Seatbelt 下跑整套（断网用 deny network*）。
- Linux：真 bwrap 下跑收窄读（network:true，--share-net）；network:false 的 --unshare-net 在 Docker LinuxKit 车道上
  建回环会失败（内核限制，真实主机不受影响），所以断网只在 argv 上断言用 --unshare-net，不在车道真连。
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import sysconfig
import threading
import venv
from pathlib import Path

import pytest

from agent_py_agent.agent.attempt.sandbox import (
    AttemptExecutionSandbox,
    SandboxUnavailableError,
    gateway_bound_ports,
    register_gateway_bound_port,
    unregister_gateway_bound_port,
)
from agent_py_agent.agent.plugin_sandbox import (
    PluginInterpreterPaths,
    PluginRestrictedSandbox,
    PluginV8Sandbox,
    plugin_sandbox_spec,
)

IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
needs_macos = pytest.mark.skipif(not IS_MACOS, reason="macOS Seatbelt")
needs_linux = pytest.mark.skipif(not IS_LINUX, reason="Linux bwrap")

_ENTRY = (
    "import json,os,socket,sys\n"
    "home=sys.argv[1]; base=sys.argv[2]; port=int(sys.argv[3]); gateway_port=int(sys.argv[4])\n"
    "def tryit(fn):\n"
    " try: return fn()\n"
    " except OSError as e: return f'DENIED({e.errno})'\n"
    "def rd(p): return tryit(lambda: open(p).read())\n"
    "def wr(p):\n"
    " def op():\n  open(p,'w').write('x'); return 'OK'\n"
    " return tryit(op)\n"
    "def net():\n"
    " s=socket.socket(); s.settimeout(2)\n"
    " try:\n  s.connect(('127.0.0.1',port)); return 'CONNECTED'\n"
    " except OSError as e: return f'errno{e.errno}'\n"
    " finally: s.close()\n"
    "def gateway_net():\n"
    " s=socket.socket(); s.settimeout(2)\n"
    " try:\n  s.connect(('127.0.0.1',gateway_port)); return 'CONNECTED'\n"
    " except OSError as e: return f'errno{e.errno}'\n"
    " finally: s.close()\n"
    "env=os.path.dirname(os.path.abspath(__file__))\n"
    "def chdir_files(): os.chdir(env); return 'OK'\n"
    "home_listing=tryit(lambda: sorted(os.listdir(home)))\n"
    "root_listing=tryit(lambda: sorted(os.listdir(base)))\n"
    "owner_listing=tryit(lambda: sorted(os.listdir(os.path.join(base,'owners/local/main'))))\n"
    "print(json.dumps({'chdir_files': tryit(chdir_files),\n"
    " 'realpath_ok': os.path.isabs(os.path.realpath(__file__,strict=True)),\n"
    " 'realpath_strict_file': tryit(lambda: os.path.realpath(__file__,strict=True)),\n"
    " 'realpath_strict_cwd': tryit(lambda: os.path.realpath(os.getcwd(),strict=True)),\n"
    " 'getcwd': tryit(os.getcwd),\n"
    " 'own_env': rd(os.path.join(env,'helper.txt')),\n"
    " 'own_data_write': wr(os.path.join(base,'owners/local/main/plugins/data/p/w.txt')),\n"
    " 'own_data_read': rd(os.path.join(base,'owners/local/main/plugins/data/p/w.txt')),\n"
    " 'other_plugin_data': rd(os.path.join(base,'owners/local/main/plugins/data/q/secret.txt')),\n"
    " 'other_plugin_env': rd(os.path.join(base,'owners/local/main/plugins/environments/other/files/x.txt')),\n"
    " 'sessions': rd(os.path.join(base,'owners/local/main/sessions/c.json')),\n"
    " 'secrets': rd(os.path.join(base,'secrets/t.json')),\n"
    " 'config': rd(os.path.join(base,'config/agent_config.yaml')),\n"
    " 'ssh': rd(os.path.join(home,'.ssh/id_rsa')),\n"
    " 'other_home': rd(os.path.join(home,'cloud/secret.json')),\n"
    " 'list_home': 'OK' if isinstance(home_listing,list) else home_listing,\n"
    " 'list_home_names': home_listing if isinstance(home_listing,list) else [],\n"
    " 'list_data_root': 'OK' if isinstance(root_listing,list) else root_listing,\n"
    " 'list_data_root_names': root_listing if isinstance(root_listing,list) else [],\n"
    " 'list_owner_home_names': owner_listing if isinstance(owner_listing,list) else [],\n"
    " 'net': net(), 'gateway_net': gateway_net()}))\n"
)

_RESTRICTED_PROBE = """
import json, socket, sys
def read(path):
    try:
        with open(path, encoding='utf-8') as stream:
            return stream.read()
    except OSError as exc:
        return f'DENIED:{exc.errno}'
def write(path):
    try:
        with open(path, 'w', encoding='utf-8') as stream:
            stream.write('x')
        return 'OK'
    except OSError as exc:
        return f'DENIED:{exc.errno}'
sock = socket.socket()
sock.settimeout(2)
try:
    sock.connect(('127.0.0.1', int(sys.argv[5])))
    network = 'CONNECTED'
except OSError as exc:
    network = f'DENIED:{exc.errno}'
finally:
    sock.close()
print(json.dumps({'read_allowed': read(sys.argv[1]), 'read_denied': read(sys.argv[2]),
                  'write_allowed': write(sys.argv[3]), 'write_denied': write(sys.argv[4]),
                  'network': network}))
"""

_ALLOWLIST_PROBE = """
import json, os, sys
def read(path):
    try:
        with open(path, encoding='utf-8') as stream:
            return stream.read()
    except OSError:
        return 'DENIED'
def write(path):
    try:
        with open(path, 'w', encoding='utf-8') as stream:
            stream.write('x')
        return 'OK'
    except OSError:
        return 'DENIED'
def can_stat(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False
def names(path):
    try:
        return sorted(os.listdir(path))
    except OSError:
        return None
with open(sys.argv[5], 'rb') as stream:
    system_read = 'OK' if stream.read(1) else 'EMPTY'
print(json.dumps({'read_allowed': read(sys.argv[1]), 'read_denied': read(sys.argv[2]),
                   'write_allowed': write(sys.argv[3]), 'write_denied': write(sys.argv[4]),
                   'system_read': system_read, 'ancestor_stat': can_stat(sys.argv[6]),
                   'denied_stat': can_stat(sys.argv[7]), 'ancestor_names': names(sys.argv[6])}))
"""


def _layout(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    home = tmp_path / "home"
    home.mkdir(parents=True)
    data_root = home / ".my-agent"
    (data_root / "owners/local/main/sessions").mkdir(parents=True)
    (data_root / "secrets").mkdir(parents=True)
    env = data_root / "owners/local/main/plugins/environments/ref1/files"
    env.mkdir(parents=True)
    data_dir = data_root / "owners/local/main/plugins/data/p"
    data_dir.mkdir(parents=True)
    (data_root / "owners/local/main/plugins/data/q").mkdir(parents=True)
    (data_root / "owners/local/main/plugins/environments/other/files").mkdir(parents=True)
    (data_root / "owners/local/main/plugins/data/q/secret.txt").write_text("OTHER_PLUGIN")
    (data_root / "owners/local/main/plugins/environments/other/files/x.txt").write_text("OTHER_ENV")
    (data_root / "owners/local/main/sessions/c.json").write_text("SESSION")
    (data_root / "secrets/t.json").write_text("TOKEN")
    (data_root / "config").mkdir(parents=True)
    (data_root / "config/agent_config.yaml").write_text("CONFIG")
    (home / ".ssh").mkdir()
    (home / ".ssh/id_rsa").write_text("SSH_PRIVATE_KEY")
    (home / "cloud").mkdir()
    (home / "cloud/secret.json").write_text("CLOUD_SECRET")
    (env / "entry.py").write_text(_ENTRY)
    (env / "helper.txt").write_text("OWN_ENV")
    return home, data_root, env, data_dir


# LLM: 只在 macOS 按 otool 清单补齐 venv 前缀运行所需的动态库，避免 dyld 缺库被误判成沙箱拒读。
# 函数用途: 为真 Seatbelt 用例准备可启动的临时 Python 前缀。
def _link_macos_python_runtime(prefix: Path, interpreter: Path) -> None:
    if sys.platform != "darwin":
        return
    output = subprocess.run(["otool", "-L", str(interpreter)], capture_output=True, text=True, check=True).stdout
    for directory, name, source in _macos_runtime_links(output, prefix, _macos_runtime_roots()):
        _install_runtime_library_link(directory, name, source)


# LLM: 依赖源仅从测试宿主当前 Python 的真实安装信息查找，不猜路径深度或使用目标外安装器。
# 函数用途: 收集临时前缀可链接的 Python 运行库候选目录。
def _macos_runtime_roots() -> tuple[Path, ...]:
    config_lib = sysconfig.get_config_var("LIBDIR")
    roots = [Path(sys.base_prefix) / "lib", Path(sys.base_prefix)]
    if config_lib:
        roots.append(Path(config_lib))
    roots.append(Path(sys.executable).resolve(strict=True).parent)
    return tuple(roots)


# LLM: 仅处理 dyld 使用的相对运行路径；系统绝对库留在系统原位，不扩展测试前缀。
# 函数用途: 将 otool 依赖映射到临时 venv 中 dyld 实际搜索的目录。
def _macos_dependency_directory(dependency: str, prefix: Path) -> Path | None:
    if dependency.startswith("@rpath/"):
        return prefix / "lib"
    if dependency.startswith(("@loader_path/", "@executable_path/")):
        return prefix / "bin"
    return None


# LLM: otool 的首行是目标程序本身；每个相对依赖必须能在当前解释器安装中定位，否则夹具立刻失败。
# 函数用途: 把动态库清单解析为临时前缀的链接任务。
def _macos_runtime_links(output: str, prefix: Path, roots: tuple[Path, ...]) -> list[tuple[Path, str, Path]]:
    links = []
    for line in output.splitlines()[1:]:
        dependency = line.strip().split(" (", 1)[0]
        directory = _macos_dependency_directory(dependency, prefix)
        if directory is None:
            continue
        name = Path(dependency.split("/", 1)[1]).name
        source = _macos_library_source(name, roots)
        assert source is not None, f"otool 依赖未能从当前 Python 安装中定位: {dependency}"
        links.append((directory, name, source))
    return links


# LLM: 仅在已核验的解释器安装根中按动态库文件名查找，不递归扫描磁盘或下载依赖。
# 函数用途: 为 otool 的一个 dylib 依赖找出现有本机库文件。
def _macos_library_source(name: str, roots: tuple[Path, ...]) -> Path | None:
    for root in roots:
        source = root / name
        if source.is_file():
            return source
    return None


# LLM: 已有同名文件不得被覆盖成另一库；空目录中只创建指向当前 Python 安装的临时 symlink.
# 函数用途: 安全地把一个解析出的动态库放入测试 venv 搜索目录。
def _install_runtime_library_link(directory: Path, name: str, source: Path) -> None:
    directory.mkdir(exist_ok=True)
    target = directory / name
    if target.exists():
        assert target.resolve(strict=True) == source.resolve(strict=True), target
        return
    target.symlink_to(source)


def _listener() -> tuple[socket.socket, int]:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)

    def loop() -> None:
        for _ in range(10):
            try:
                srv.accept()[0].close()
            except OSError:
                return  # 测试结束关套接字时 accept 会中止，正常退出，不抛进 daemon 线程

    threading.Thread(target=loop, daemon=True).start()
    return srv, srv.getsockname()[1]


def _run(spec, run_args: tuple[Path, Path, Path, Path, int, int]) -> dict:
    interpreter, env, home, data_root, port, gateway_port = run_args
    sandbox = AttemptExecutionSandbox(spec)
    res = sandbox.run([str(interpreter), str(env / "entry.py"), str(home), str(data_root),
                       str(port), str(gateway_port)], timeout=40)
    assert res.stdout.strip(), (getattr(res, "returncode", None), (res.stderr or "")[-600:])
    return json.loads(res.stdout.strip())


def _python_prefixes() -> tuple[Path, ...]:
    prefixes = []
    for value in (sys.prefix, sys.base_prefix):
        original = Path(value).expanduser().absolute()
        prefixes.extend((original, original.resolve(strict=True)))
    return tuple(dict.fromkeys(prefixes))


# ---------------------------------------------------------- 单元：spec 形状


def test_v8_spec_reads_base_is_env_not_owner_home(tmp_path):
    home, data_root, env, data_dir = _layout(tmp_path)
    spec = plugin_sandbox_spec(cwd=env, data_dir=data_dir, owner_home=data_root / "owners/local/main",
                               sandbox_policy=PluginV8Sandbox(network=False, hidden_read_root=home,
                                                  public_read_roots=_python_prefixes()))
    # 读基底是插件环境本身；整用户家目录拒读后，仅解释器前缀、插件环境和数据目录可见。
    assert spec.owner_home == env and spec.shared_workspace == env
    assert spec.private_read_roots == (home,)
    assert spec.public_read_roots == _python_prefixes()
    assert spec.network_access is False and spec.read_only_root is True


def _restricted_policy_symlink_fixture(tmp_path):
    home, data_root, env, data_dir = _layout(tmp_path)
    read_target, write_target = tmp_path / "read-real", tmp_path / "write-real"
    program_target = tmp_path / "tools" / "plugin-runner"
    read_target.mkdir()
    write_target.mkdir()
    program_target.parent.mkdir()
    program_target.write_text("host-verified program", encoding="utf-8")
    aliases = tuple(home / name for name in ("allowed-read", "allowed-write", "plugin-runner"))
    for alias, target in zip(aliases, (read_target, write_target, program_target), strict=True):
        alias.symlink_to(target, target_is_directory=target.is_dir())
    policy = PluginRestrictedSandbox(
        read_roots=(aliases[0],), write_roots=(aliases[1],), execute_roots=(aliases[2],),
        network=False, hidden_read_root=home,
    )
    spec = plugin_sandbox_spec(cwd=env, data_dir=data_dir,
                               owner_home=data_root / "owners/local/main", sandbox_policy=policy)
    return spec, home, data_dir, (*aliases, read_target, write_target, program_target)


def _assert_restricted_policy_seatbelt_paths(spec, home, paths):
    from agent_py_agent.agent.attempt.sandbox import _spec_rules

    rules = "\n".join(_spec_rules(spec))
    assert all(f"(allow file-read* (subpath {json.dumps(str(path))}))" in rules for path in paths)
    assert f"(deny file-read* (subpath {json.dumps(str(home))}))" in rules
    profile = AttemptExecutionSandbox._macos_profile(
        attempt_view=spec.attempt_view, staging=spec.staging_root, shared=spec.shared_workspace,
        write_roots=spec.extra_write_roots, implicit_attempt_write_roots=False,
    )
    write_allow = next(line for line in profile.splitlines() if line.startswith("(allow file-write*"))
    assert f"(subpath {json.dumps(str(paths[4]))})" in write_allow
    assert f"(subpath {json.dumps(str(paths[1]))})" not in write_allow


def test_restricted_policy_spec_maps_read_write_network_and_file_program_roots(tmp_path):
    from agent_py_agent import agent

    spec, home, data_dir, paths = _restricted_policy_symlink_fixture(tmp_path)
    assert getattr(agent.plugin_sandbox, "PluginRestrictedSandbox", None) is not None
    assert spec.private_read_roots == (home.resolve(strict=True),)
    assert {paths[0], paths[3], paths[5]} <= set(spec.public_read_roots)
    assert paths[2].parent not in spec.public_read_roots
    assert {data_dir, paths[1], paths[4]} <= set(spec.extra_write_roots)
    assert paths[4].parent not in spec.extra_write_roots
    assert spec.read_only_root is True and spec.network_access is False
    _assert_restricted_policy_seatbelt_paths(spec, home, paths)


class _TestActivationRef:
    def __init__(self, installation):
        self.installation = installation

    def require(self, allow_preparing=False):
        return self.installation


def _fake_v8_client_case(tmp_path):
    from types import SimpleNamespace

    root = tmp_path / "owner"
    plugins = root / "plugins"
    cwd = plugins / "environments" / "ref" / "files"
    data_dir = plugins / "data" / "sample"
    hidden = tmp_path / "hidden-home"
    for path in (cwd, data_dir, hidden):
        path.mkdir(parents=True)
    plan = SimpleNamespace(environment_ref="ref", interpreter_fingerprint="fingerprint")
    activation = SimpleNamespace(phase="active", activation_id="activation", plan=plan)
    manifest = SimpleNamespace(plugin_id="sample", permissions=object(), settings_schema={})
    installation = SimpleNamespace(activation=activation, manifest=manifest, settings_json="{}")
    owner = SimpleNamespace(root=root, plugins_dir=plugins, home_dir=root)
    return owner, installation, cwd, data_dir, hidden


def _capture_v8_client_sandbox(monkeypatch, installation, cwd, hidden):
    from agent_py_agent.agent import plugin_runtime

    captured = []
    reference = _TestActivationRef(installation)
    monkeypatch.setattr(
        plugin_runtime.PluginActivationRef,
        "from_owner",
        classmethod(lambda _cls, *_args: reference),
    )
    monkeypatch.setattr(plugin_runtime, "_launch_argv", lambda *_args: ("/fake/python", ["-c", "pass"], cwd))
    monkeypatch.setattr(plugin_runtime, "_plugin_v8_sandbox", lambda *_args: PluginV8Sandbox(
        network=False, hidden_read_root=hidden
    ))
    monkeypatch.setattr(plugin_runtime, "sandboxed_plugin_argv", lambda argv, spec:
                        captured.append(spec) or ["fake-sandbox", "--", *argv])
    monkeypatch.setattr(plugin_runtime, "canonical_plugin_settings", lambda *_args: "{}")

    def capture_config(client, config, activation=None):
        client.config = config
        client.activation = activation

    monkeypatch.setattr(plugin_runtime.MCPStdioClient, "__init__", capture_config)
    return captured


def test_v8_client_forces_sandbox_when_global_switch_is_off(tmp_path, monkeypatch):
    from agent_py_agent.agent import plugin_runtime

    owner, installation, cwd, data_dir, hidden = _fake_v8_client_case(tmp_path)
    captured = _capture_v8_client_sandbox(monkeypatch, installation, cwd, hidden)
    client = plugin_runtime.PluginMCPClient(owner, installation, process_sandbox=False)

    assert len(captured) == 1
    assert captured[0].private_read_roots == (hidden.resolve(strict=True),)
    assert captured[0].read_only_root is True
    assert client.config.command == "fake-sandbox"
    assert client.config.env["TMPDIR"] == str(data_dir / ".tmp")


def test_python_prefix_symlink_alias_and_realpath_reach_seatbelt_rules(tmp_path, monkeypatch):
    from agent_py_agent import agent
    from agent_py_agent.agent.attempt.sandbox import _spec_rules

    home, data_root, env, data_dir = _layout(tmp_path)
    prefix_real = tmp_path / "python-prefix-real"
    base_real = tmp_path / "python-base-real"
    prefix_real.mkdir()
    base_real.mkdir()
    prefix_alias = home / "python-prefix"
    base_alias = home / "python-base"
    prefix_alias.symlink_to(prefix_real, target_is_directory=True)
    base_alias.symlink_to(base_real, target_is_directory=True)
    monkeypatch.setattr(agent.plugin_sandbox.Path, "home", lambda: home)
    monkeypatch.setattr(agent.plugin_sandbox.sys, "prefix", str(prefix_alias))
    monkeypatch.setattr(agent.plugin_sandbox.sys, "base_prefix", str(base_alias))

    options, reason = agent.plugin_sandbox.plugin_v8_sandbox_options(
        data_root / "owners/local/main", network=False
    )
    assert reason == "" and options is not None
    assert options.hidden_read_root == home.resolve(strict=True)
    expected = {
        prefix_alias.absolute(), prefix_real.resolve(strict=True),
        base_alias.absolute(), base_real.resolve(strict=True),
    }
    assert expected <= set(options.public_read_roots)
    spec = plugin_sandbox_spec(
        cwd=env, data_dir=data_dir, owner_home=data_root / "owners/local/main", sandbox_policy=options
    )
    rules = "\n".join(_spec_rules(spec))
    assert all(f"(allow file-read* (subpath {json.dumps(str(path))}))" in rules for path in expected)


def test_non_v8_spec_unchanged(tmp_path):
    home, data_root, env, data_dir = _layout(tmp_path)
    spec = plugin_sandbox_spec(cwd=env, data_dir=data_dir, owner_home=data_root / "owners/local/main")
    assert spec.private_read_roots == () and spec.network_access is True
    assert spec.owner_home == data_root / "owners/local/main"


@needs_linux
def test_linux_network_false_argv_unshares_net_and_hides_data_root(tmp_path):
    # 直接用 build_bwrap_argv 核 argv 形状，绕过就绪探测——Docker LinuxKit 车道上 --unshare-net 建回环会失败，
    # 但生产真实主机正常；production 断网就是走 --unshare-net，这里钉住它和 tmpfs 盖数据根、挂回工作目录。
    from agent_py_agent.agent.tooling.sandbox import SandboxSpec, build_bwrap_argv, find_bwrap
    bwrap = find_bwrap()
    if not bwrap:
        pytest.skip("无 bwrap")
    home, data_root, env, data_dir = _layout(tmp_path)
    spec = SandboxSpec(owner_home=env, workspace=env, write_roots=(data_dir,), bwrap_path=bwrap,
                       read_only_root=True, network_access=False, hidden_paths=(home,))
    argv = build_bwrap_argv(spec)
    assert "--unshare-net" in argv and "--share-net" not in argv
    assert argv[argv.index("--tmpfs") + 1] == str(home)           # 整个用户家目录被 tmpfs 盖住
    joined = " ".join(argv)
    assert f"--ro-bind {env} {env}" in joined                    # 工作目录（插件环境）挂回来可读
    assert f"--bind {data_dir} {data_dir}" in joined             # 数据目录挂回来可写


# ---------------------------------------------------------- macOS 真进程


@needs_macos
@pytest.mark.parametrize("network", [False, True])
def test_macos_v8_narrows_read_and_enforces_network(tmp_path, network):
    home, data_root, env, data_dir = _layout(tmp_path)
    srv, port = _listener()
    gateway_srv, gateway_port = _listener()
    register_gateway_bound_port(gateway_port)
    spec = plugin_sandbox_spec(cwd=env, data_dir=data_dir, owner_home=data_root / "owners/local/main",
                               sandbox_policy=PluginV8Sandbox(network=network, hidden_read_root=home,
                                                  public_read_roots=_python_prefixes()))
    sandbox = AttemptExecutionSandbox(spec)
    if not sandbox.probe().ready:
        srv.close()
        gateway_srv.close()
        unregister_gateway_bound_port(gateway_port)
        pytest.skip("sandbox-exec 不可用")
    try:
        sandbox.require_ready()
    except SandboxUnavailableError as exc:  # 沙箱外由 9b/3a 复核真实 Seatbelt。
        srv.close()
        gateway_srv.close()
        unregister_gateway_bound_port(gateway_port)
        pytest.skip(f"本执行环境的 Seatbelt 就绪自检未通过：{exc}")
    try:
        report = _run(spec, (Path(sys.executable), env, home, data_root, port, gateway_port))
    finally:
        srv.close()
        gateway_srv.close()
        unregister_gateway_bound_port(gateway_port)
    assert report["realpath_ok"] is True
    assert report["chdir_files"] == "OK"
    assert report["realpath_strict_file"].endswith("entry.py")
    assert report["realpath_strict_cwd"].endswith("files")
    assert report["getcwd"].endswith("files")
    assert report["own_env"] == "OWN_ENV"
    assert report["own_data_write"] == "OK"
    assert report["own_data_read"] == "x"
    assert report["sessions"].startswith("DENIED"), report
    assert report["secrets"].startswith("DENIED"), report
    _expect_hidden_home(report)
    if network:
        assert report["net"] == "CONNECTED"
        assert report["gateway_net"].startswith("errno"), "v8 network:true 必须只拒绝 Gateway 端口"
    else:
        assert report["net"].startswith("errno"), "network:false 必须连不上回环"
        assert report["gateway_net"].startswith("errno")


# ---------------------------------------------------------- Linux 真进程（network:true）


@needs_linux
def test_linux_v8_narrows_read_real_process(tmp_path):
    from agent_py_agent.agent.tooling.sandbox import find_bwrap
    if not find_bwrap():
        pytest.skip("无 bwrap")
    home, data_root, env, data_dir = _layout(tmp_path)
    srv, port = _listener()
    gateway_srv, gateway_port = _listener()
    # 低层 bwrap 进程只验收窄读挂法；生产启用时 Linux 的 network:true 由策略拒绝。
    spec = plugin_sandbox_spec(cwd=env, data_dir=data_dir, owner_home=data_root / "owners/local/main",
                               sandbox_policy=PluginV8Sandbox(network=True, hidden_read_root=home,
                                                  public_read_roots=_python_prefixes()))
    try:
        report = _run(spec, (Path(sys.executable), env, home, data_root, port, gateway_port))
    finally:
        srv.close()
        gateway_srv.close()
    assert report["chdir_files"] == "OK"
    assert report["own_env"] == "OWN_ENV"
    assert report["own_data_write"] == "OK"
    assert report["own_data_read"] == "x"
    assert report["sessions"].startswith("DENIED"), report
    assert report["secrets"].startswith("DENIED"), report
    _expect_hidden_home(report)


def _restricted_policy_case(tmp_path, read_mode="hide_home"):
    home, data_root, env, data_dir = _layout(tmp_path)
    allowed_read = home / "allowed-read"
    allowed_write = home / "allowed-write"
    denied = home / "denied"
    for path in (allowed_read, allowed_write, denied):
        path.mkdir()
    package_root = tmp_path / "allowed-package"
    package = package_root / "b7lnx_probe"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("VALUE = 'SYMLINK_EXEC_ROOT_OK'\n", encoding="utf-8")
    interpreter_alias = home / "python-alias"
    interpreter_alias.symlink_to(Path(sys.executable))
    readable = allowed_read / "value.txt"
    unreadable = denied / "secret.txt"
    readable.write_text("READABLE", encoding="utf-8")
    unreadable.write_text("HIDDEN", encoding="utf-8")
    allowed_write_file = allowed_write / "created.txt"
    denied_write_file = denied / "created.txt"
    read_roots = (allowed_read, package_root)
    if read_mode == "allowlist":
        read_roots = (*read_roots, env, *_python_prefixes())
    policy = PluginRestrictedSandbox(
        read_roots=read_roots,
        write_roots=(allowed_write,),
        execute_roots=(interpreter_alias,),
        network=False,
        hidden_read_root=home,
        read_mode=read_mode,
    )
    spec = plugin_sandbox_spec(cwd=env, data_dir=data_dir,
                               owner_home=data_root / "owners/local/main", sandbox_policy=policy)
    return AttemptExecutionSandbox(spec), readable, unreadable, allowed_write_file, denied_write_file, interpreter_alias, package_root


@pytest.mark.skipif(not (IS_LINUX or IS_MACOS), reason="Linux bwrap / macOS Seatbelt")
def test_restricted_policy_real_process_enforces_read_write_and_offline_roots(tmp_path):
    sandbox, readable, unreadable, allowed_write_file, denied_write_file, _, _ = _restricted_policy_case(tmp_path)
    if not sandbox.probe().ready:
        pytest.skip("当前平台的沙箱自检未就绪")
    try:
        sandbox.require_ready()
    except SandboxUnavailableError as exc:
        pytest.skip(f"当前环境未能执行受限沙箱：{exc}")
    listener, port = _listener()
    try:
        result = sandbox.run([
            sys.executable, "-c", _RESTRICTED_PROBE, str(readable), str(unreadable),
            str(allowed_write_file), str(denied_write_file), str(port),
        ], timeout=30)
    finally:
        listener.close()

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.strip())
    assert report["read_allowed"] == "READABLE"
    assert report["read_denied"].startswith("DENIED:")
    assert report["write_allowed"] == "OK"
    assert report["write_denied"].startswith("DENIED:")
    assert report["network"].startswith("DENIED:")


@pytest.mark.skipif(not (IS_LINUX or IS_MACOS), reason="Linux bwrap / macOS Seatbelt")
def test_restricted_policy_allowlist_limits_reads_and_preserves_ancestor_metadata(tmp_path):
    sandbox, readable, unreadable, allowed_write_file, denied_write_file, _, _ = _restricted_policy_case(
        tmp_path, read_mode="allowlist"
    )
    if not sandbox.probe().ready:
        pytest.skip("当前平台的沙箱自检未就绪")
    try:
        sandbox.require_ready()
    except SandboxUnavailableError as exc:
        pytest.skip(f"当前环境未能执行受限沙箱：{exc}")
    result = sandbox.run([
        sys.executable, "-c", _ALLOWLIST_PROBE, str(readable), str(unreadable),
        str(allowed_write_file), str(denied_write_file), "/usr/bin/env",
        str(unreadable.parent.parent), str(unreadable.parent),
    ], timeout=30)

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.strip())
    assert report["read_allowed"] == "READABLE"
    assert report["read_denied"] == "DENIED"
    assert report["write_allowed"] == "OK"
    assert report["write_denied"] == "DENIED"
    assert report["system_read"] == "OK"
    assert report["ancestor_stat"] is True
    assert report["denied_stat"] is False
    assert report["ancestor_names"] is None


@needs_linux
@pytest.mark.parametrize("read_mode", ["hide_home", "allowlist"])
def test_linux_restricted_policy_symlink_execute_root_runs_and_reads_package(tmp_path, read_mode):
    from agent_py_agent.agent.tooling.sandbox import find_bwrap

    if not find_bwrap():
        pytest.skip("无 bwrap")
    sandbox, _, _, _, _, interpreter, package_root = _restricted_policy_case(tmp_path, read_mode=read_mode)
    if not sandbox.probe().ready:
        pytest.skip("Linux bwrap 沙箱自检未就绪")
    code = "import sys; sys.path.insert(0, sys.argv[1]); import b7lnx_probe; print(b7lnx_probe.VALUE)"
    result = sandbox.run([str(interpreter), "-c", code, str(package_root)], timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "SYMLINK_EXEC_ROOT_OK"


@needs_linux
def test_linux_python_prefix_symlink_alias_runs_and_reads_package(tmp_path, monkeypatch):
    from agent_py_agent import agent

    home, data_root, env, data_dir = _layout(tmp_path)
    real_prefix = tmp_path / "python-prefix-real"
    venv.EnvBuilder(with_pip=False).create(real_prefix)
    alias = home / "python-prefix-alias"
    alias.symlink_to(real_prefix, target_is_directory=True)
    interpreter = alias / "bin" / "python"
    package_dir = (
        real_prefix / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages" / "b7fix_probe"
    )
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("VALUE = 'ALIAS_PACKAGE'\n", encoding="utf-8")
    assert interpreter.is_file()
    monkeypatch.setattr(agent.plugin_sandbox.Path, "home", lambda: home)
    monkeypatch.setattr(agent.plugin_sandbox.sys, "prefix", str(alias))
    options, reason = agent.plugin_sandbox.plugin_v8_sandbox_options(
        data_root / "owners/local/main", network=True
    )
    assert reason == "" and options is not None
    assert alias.absolute() in options.public_read_roots
    assert real_prefix.resolve(strict=True) in options.public_read_roots
    spec = plugin_sandbox_spec(
        cwd=env,
        data_dir=data_dir,
        owner_home=data_root / "owners/local/main",
        sandbox_policy=options,
    )
    sandbox = AttemptExecutionSandbox(spec)
    if not sandbox.probe().ready:
        pytest.skip("Linux bwrap 沙箱自检未就绪")

    result = sandbox.run(
        [str(interpreter), "-c", "import b7fix_probe; print(b7fix_probe.VALUE)"], timeout=30
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ALIAS_PACKAGE"



def _expect_hidden_home(report: dict, *, allowed_home_names: list[str] | None = None) -> None:
    for key in ("other_plugin_data", "other_plugin_env", "sessions", "secrets", "config", "ssh", "other_home"):
        assert report[key].startswith("DENIED"), (key, report)
    if sys.platform == "darwin":
        assert report["list_home"].startswith("DENIED"), report
    else:
        # Linux tmpfs 只留下必要挂载骨架，以及明确获准的解释器前缀名。
        assert report["list_home_names"] == (allowed_home_names or [".my-agent"]), report
        assert report["list_data_root_names"] == ["owners"], report
        assert report["list_owner_home_names"] == ["plugins"], report


def test_v8_spec_registers_gateway_ports_for_deny_rules(tmp_path):
    home, data_root, env, data_dir = _layout(tmp_path)
    srv, port = _listener()
    register_gateway_bound_port(port)
    try:
        spec = plugin_sandbox_spec(cwd=env, data_dir=data_dir, owner_home=data_root / "owners/local/main",
                                   sandbox_policy=PluginV8Sandbox(network=True, hidden_read_root=home,
                                                      public_read_roots=_python_prefixes()))
        assert port in gateway_bound_ports()
        assert port in spec.deny_gateway_ports
    finally:
        srv.close()
        unregister_gateway_bound_port(port)


def test_python_installation_prefix_inside_hidden_home_runs(tmp_path, monkeypatch):
    from agent_py_agent import agent

    home, data_root, env, data_dir = _layout(tmp_path)
    venv_root = home / "python"
    venv.EnvBuilder(with_pip=False).create(venv_root)
    interpreter = venv_root / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    assert interpreter.is_file()
    _link_macos_python_runtime(venv_root, interpreter)
    monkeypatch.setattr(agent.plugin_sandbox.Path, "home", lambda: home)
    monkeypatch.setattr(agent.plugin_sandbox.sys, "prefix", str(venv_root))
    options, reason = agent.plugin_sandbox.plugin_v8_sandbox_options(
        data_root / "owners/local/main", network=True)
    assert reason == "" and options is not None
    assert venv_root.absolute() in options.public_read_roots
    assert venv_root.resolve(strict=True) in options.public_read_roots
    assert Path(sys.base_prefix).expanduser().absolute() in options.public_read_roots
    assert Path(sys.base_prefix).resolve(strict=True) in options.public_read_roots
    spec = plugin_sandbox_spec(cwd=env, data_dir=data_dir, owner_home=data_root / "owners/local/main",
                               sandbox_policy=options)
    sandbox = AttemptExecutionSandbox(spec)
    if not sandbox.probe().ready:
        pytest.skip("sandbox not ready")
    srv, port = _listener()
    try:
        report = _run(spec, (interpreter, env, home, data_root, port, port))
    finally:
        srv.close()
    assert report["chdir_files"] == "OK"
    assert report["realpath_strict_file"].endswith("entry.py")
    assert report["own_env"] == "OWN_ENV"
    assert report["own_data_write"] == "OK"
    _expect_hidden_home(report, allowed_home_names=[".my-agent", "python"])


@pytest.mark.skipif(shutil.which("node") is None, reason="no node")
def test_node_realpath_and_own_data_under_hidden_home(tmp_path, monkeypatch):
    from agent_py_agent import agent

    home, data_root, env, data_dir = _layout(tmp_path)
    node_path = Path(shutil.which("node"))
    node = node_path.resolve(strict=True)
    monkeypatch.setattr(agent.plugin_sandbox.Path, "home", lambda: home)
    options, reason = agent.plugin_sandbox.plugin_v8_sandbox_options(
        data_root / "owners/local/main", network=True,
        interpreter=agent.plugin_sandbox.PluginInterpreterPaths("node", node_path, node),
    )
    assert reason == "" and options is not None
    node_prefix = node.parent.parent.resolve(strict=True)
    assert node_prefix in options.public_read_roots
    spec = plugin_sandbox_spec(cwd=env, data_dir=data_dir, owner_home=data_root / "owners/local/main",
                               sandbox_policy=options)
    sandbox = AttemptExecutionSandbox(spec)
    if not sandbox.probe().ready:
        pytest.skip("sandbox not ready")
    script = env / "node_probe.js"
    script.write_text(
        "const fs=require('fs'),path=require('path');\n"
        "const root=process.argv[2], data=process.argv[3];\n"
        "process.chdir(__dirname);\n"
        "fs.writeFileSync(path.join(data,'node.txt'),'ok');\n"
        "console.log(JSON.stringify({cwd:process.cwd(),real:fs.realpathSync.native(__filename),"
        "own:fs.readFileSync(path.join(__dirname,'helper.txt'),'utf8'),"
        "data:fs.readFileSync(path.join(data,'node.txt'),'utf8'),"
        "ssh:(()=>{try{return fs.readFileSync(path.join(root,'.ssh/id_rsa'),'utf8')}catch(e){return 'DENIED'}})()}));\n",
        encoding="utf-8",
    )
    result = sandbox.run([str(node), str(script), str(home), str(data_dir)], timeout=40)
    assert result.stdout.strip(), (getattr(result, "returncode", None), (result.stderr or "")[-600:])
    report = json.loads(result.stdout.strip())
    assert report["cwd"].endswith("files") and report["real"].endswith("node_probe.js")
    assert report["own"] == "OWN_ENV" and report["data"] == "ok"
    assert report["ssh"] == "DENIED"


# ---------------------------------------------------------- 两配置项是管理员边界


def test_plugin_event_switch_is_a_boundary_the_model_cannot_flip(tmp_path):
    key, value = "plugin_events_enabled", True
    from agent_py_agent.agent.settings.parameter_changes import (
        ChangeOrigin,
        WritePaths,
        set_parameter,
    )
    from agent_py_agent.agent.settings.parameter_registry import parameter_registry
    from agent_py_agent.agent.settings.user_config_capability import USER_SETTINGS_BOUNDARY_KEYS

    assert key in USER_SETTINGS_BOUNDARY_KEYS
    assert parameter_registry()[key].writable is False
    user_config = tmp_path / "user.yaml"
    user_config.write_text("", encoding="utf-8")
    report = set_parameter(key, value, paths=WritePaths(user_path=user_config), origin=ChangeOrigin("model"))
    assert report["ok"] is False and report["code"] == "PARAMETER_BOUNDARY"
    assert user_config.read_text(encoding="utf-8") == ""   # 模型改不动，文件一字节不写


@needs_linux
def test_linux_attempt_keeps_v8_private_root_in_hidden_mounts(tmp_path, monkeypatch):
    from dataclasses import replace

    from agent_py_agent.agent.tooling import sandbox as linux_sandbox

    home, data_root, env, data_dir = _layout(tmp_path)
    spec = plugin_sandbox_spec(cwd=env, data_dir=data_dir, owner_home=data_root / "owners/local/main",
                               sandbox_policy=PluginV8Sandbox(network=False, hidden_read_root=home,
                                                  public_read_roots=_python_prefixes()))
    spec = replace(spec, bwrap_path="/test/bwrap")
    monkeypatch.setattr(linux_sandbox, "_proc_mount_args", lambda: ["--dir", "/proc"])
    argv = AttemptExecutionSandbox(spec)._linux_argv([sys.executable, "-c", "pass"])
    assert argv[argv.index("--unshare-net")] == "--unshare-net"
    assert argv[argv.index("--tmpfs") + 1] == str(home)


@pytest.mark.parametrize("hidden_root", [None, "missing"])
def test_v8_hidden_root_missing_is_structured_sandbox_unavailable(tmp_path, hidden_root):
    from agent_py_agent.agent.attempt.sandbox import SandboxUnavailableError

    home, data_root, env, data_dir = _layout(tmp_path)
    root = None if hidden_root is None else tmp_path / "not-created"
    with pytest.raises(SandboxUnavailableError):
        plugin_sandbox_spec(cwd=env, data_dir=data_dir, owner_home=data_root / "owners/local/main",
                            sandbox_policy=PluginV8Sandbox(network=False, hidden_read_root=root))


def test_linux_network_true_v8_refuses_without_gateway_port_isolation(tmp_path, monkeypatch):
    from agent_py_agent.agent import plugin_sandbox
    from agent_py_agent.agent.contracts.error_taxonomy import error_contract

    home, data_root, env, data_dir = _layout(tmp_path)
    owner_home = data_root / "owners/local/main"
    prefixes = _python_prefixes()
    monkeypatch.setattr(plugin_sandbox.platform, "system", lambda: "Linux")

    def readiness(self):
        if not self.spec.network_access:
            return None
        return None

    monkeypatch.setattr(plugin_sandbox.AttemptExecutionSandbox, "require_ready", readiness)
    networked = PluginV8Sandbox(network=True, hidden_read_root=home, public_read_roots=prefixes)
    offline = PluginV8Sandbox(network=False, hidden_read_root=home, public_read_roots=prefixes)
    restricted_networked = PluginRestrictedSandbox(
        read_roots=prefixes, write_roots=(), execute_roots=(), network=True, hidden_read_root=home
    )
    assert plugin_sandbox.plugin_sandbox_problem(True, owner_home, sandbox_policy=networked) == "gateway_port_isolation_unavailable"
    assert plugin_sandbox.plugin_sandbox_problem(True, owner_home, sandbox_policy=restricted_networked) == "gateway_port_isolation_unavailable"
    assert plugin_sandbox.plugin_sandbox_problem(True, owner_home, sandbox_policy=offline) == ""
    contract = error_contract("PLUGIN_GATEWAY_PORT_ISOLATION_UNAVAILABLE")
    assert contract.code == "PLUGIN_GATEWAY_PORT_ISOLATION_UNAVAILABLE" and contract.retryable is False


def test_interpreter_inside_my_agent_data_root_is_structured_rejection(tmp_path):
    from agent_py_agent.agent.plugin_sandbox import plugin_v8_sandbox_options

    home, data_root, env, data_dir = _layout(tmp_path)
    hidden_interpreter = data_root / "runtime/python"
    hidden_interpreter.parent.mkdir(parents=True)
    hidden_interpreter.symlink_to(Path(sys.executable))
    options, reason = plugin_v8_sandbox_options(
        owner_home=data_root / "owners/local/main", network=False,
        interpreter=PluginInterpreterPaths("python3", hidden_interpreter, Path(sys.executable)),
    )
    assert options is None and reason == "interpreter_inside_hidden_root"


def test_missing_interpreter_prefix_is_structured_sandbox_unavailable(tmp_path, monkeypatch):
    from agent_py_agent.agent import plugin_sandbox
    from agent_py_agent.agent.plugin_sandbox import plugin_v8_sandbox_options

    home, data_root, env, data_dir = _layout(tmp_path)
    missing = tmp_path / "missing-python-prefix"
    monkeypatch.setattr(plugin_sandbox.sys, "prefix", str(missing))
    monkeypatch.setattr(plugin_sandbox.sys, "base_prefix", str(missing))
    options, reason = plugin_v8_sandbox_options(
        owner_home=data_root / "owners/local/main", network=False,
        interpreter=PluginInterpreterPaths("python3", Path(sys.executable), Path(sys.executable)))
    assert options is None and reason == "sandbox_unavailable"


def test_restricted_sandbox_spec_propagates_allowlist_read_mode(tmp_path):
    home, data_root, env, data_dir = _layout(tmp_path)
    policy = PluginRestrictedSandbox(
        read_roots=(env,), write_roots=(), execute_roots=(), network=False, hidden_read_root=home,
        read_mode="allowlist",
    )

    spec = plugin_sandbox_spec(
        cwd=env, data_dir=data_dir, owner_home=data_root / "owners/local/main", sandbox_policy=policy
    )

    assert getattr(spec, "read_mode", None) == "allowlist"


def test_v8_spec_keeps_hide_home_read_mode(tmp_path):
    home, data_root, env, data_dir = _layout(tmp_path)
    spec = plugin_sandbox_spec(
        cwd=env,
        data_dir=data_dir,
        owner_home=data_root / "owners/local/main",
        sandbox_policy=PluginV8Sandbox(network=False, hidden_read_root=home, public_read_roots=()),
    )

    assert spec.read_mode == "hide_home"


def test_allowlist_mode_does_not_implicitly_reopen_cwd(tmp_path):
    from agent_py_agent.agent.attempt.sandbox import SandboxUnavailableError

    home, data_root, env, data_dir = _layout(tmp_path)
    allowed = home / "allowed"
    allowed.mkdir()
    policy = PluginRestrictedSandbox(
        read_roots=(allowed,), write_roots=(), execute_roots=(), network=False,
        hidden_read_root=None, read_mode="allowlist",
    )

    with pytest.raises(SandboxUnavailableError, match="RESTRICTED_CWD_NOT_ALLOWLISTED"):
        plugin_sandbox_spec(
            cwd=env, data_dir=data_dir, owner_home=data_root / "owners/local/main", sandbox_policy=policy
        )


def test_allowlist_mode_can_omit_hidden_root_but_keeps_system_roots_separate(tmp_path):
    from agent_py_agent.agent.attempt.sandbox import system_read_roots_for_platform

    home, data_root, env, data_dir = _layout(tmp_path)
    policy = PluginRestrictedSandbox(
        read_roots=(env,), write_roots=(), execute_roots=(), network=False,
        hidden_read_root=None, read_mode="allowlist",
    )

    spec = plugin_sandbox_spec(
        cwd=env, data_dir=data_dir, owner_home=data_root / "owners/local/main", sandbox_policy=policy
    )

    assert spec.read_mode == "allowlist"
    assert spec.private_read_roots == ()
    assert spec.system_read_roots == system_read_roots_for_platform()
    assert not set(spec.system_read_roots) & set(spec.public_read_roots)
