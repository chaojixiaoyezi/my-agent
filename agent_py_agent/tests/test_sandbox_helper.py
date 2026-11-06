"""SP1-W1：原生 pre-exec 与旧 Python 路径的等价、安全和 FD 回归。"""
from __future__ import annotations

import ctypes
import errno
import hashlib
import importlib.util
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.attempt import landlock_launcher as ll
from agent_py_agent.agent.attempt import sandbox as attempt
from agent_py_agent.agent.tooling import sandbox, shell

pytestmark = pytest.mark.usefixtures("sandbox_helper_opt_in")


# LLM: 仅本模块显式退出全局 Python 回退固定，不改变产品配置或默认测试路径。
# 函数用途: 让 helper 用例自行选择真实二进制或局部替身。
@pytest.fixture
def sandbox_helper_opt_in():
    """Opt in to native-helper discovery for this test only."""


# LLM: 只替换发现入口；原包装、裁剪和 spawn 代码仍真实运行，不执行替身路径。
# 函数用途: 给跨平台 argv 测试固定一个绝对 helper 路径。
def _helper(monkeypatch, path="/trusted/my-agent-sandbox-helper"):
    monkeypatch.setattr(sandbox, "find_sandbox_helper", lambda: path, raising=False)
    monkeypatch.setattr(attempt, "find_sandbox_helper", lambda: path, raising=False)
    return path


# LLM: 使用现有包装函数观察新行为；未实现时旧 Python 前缀必须让断言失败。
# 函数用途: 锁住原生资源参数及完整的绝对 bwrap 命令。
def test_resource_wrapper_uses_native_helper(monkeypatch):
    helper = _helper(monkeypatch)
    limits = sandbox.ResourceLimits(3, 512, 4096, 311)
    assert sandbox.wrap_resource_command(["/usr/bin/bwrap", "--", "/bin/true"], limits) == [
        helper, "--nice", "3", "--oom-score-adj", "311", "--max-memory-mb", "512",
        "--max-processes", "4096", "--", "/usr/bin/bwrap", "--", "/bin/true",
    ]


# LLM: 缺失 helper、相对命令、零资源限制沿旧合同，不增加不受限执行旁路。
# 函数用途: 逐字节核对 Python 回退与零包装分支。
def test_resource_wrapper_fallback_is_byte_exact(monkeypatch):
    limits = sandbox.ResourceLimits(3, 512, 4096, 311)
    argv = ["/usr/bin/bwrap", "--", "/bin/true"]
    _helper(monkeypatch, None)
    expected = [sys.executable, "-I", "-B", "-S", str(Path(sandbox.__file__).resolve()),
                "--resource-launcher", json.dumps(sandbox.asdict(limits), separators=(",", ":")), *argv]
    assert sandbox.wrap_resource_command(argv, limits) == expected
    _helper(monkeypatch)
    relative = ["bwrap", "--", "/bin/true"]
    assert sandbox.wrap_resource_command(relative, limits) == [*expected[:-len(argv)], *relative]
    assert sandbox.wrap_resource_command(argv, sandbox.ResourceLimits(0, 0, 0, 0)) == argv


# LLM: 就绪检查保持原入口；合并后只有一跳 helper，Landlock 位于资源和 bwrap 之前。
# 函数用途: 验证资源/网络限制合并，未就绪和无端口时不改 argv。
def test_landlock_merges_only_native_resource_prefix(monkeypatch, tmp_path):
    helper = _helper(monkeypatch)
    spec = attempt.AttemptSandboxSpec(tmp_path, tmp_path, tmp_path, tmp_path, deny_gateway_ports=(8420, 9000, 8420))
    backend = attempt.AttemptExecutionSandbox(spec)
    base = sandbox.wrap_resource_command(["/usr/bin/bwrap", "--", "/bin/true"])
    monkeypatch.setattr(attempt, "landlock_net_readiness", lambda _p: (True, "LANDLOCK_READY"))
    assert backend._wrap_with_landlock(base) == [helper, "--deny", "8420,9000", *base[1:]]
    monkeypatch.setattr(attempt, "landlock_net_readiness", lambda _p: (False, "UNAVAILABLE"))
    assert backend._wrap_with_landlock(base) is base


# LLM: 并发发现只探测一次；无参数探针必须返回96，不执行任何业务命令。
# 函数用途: 核对 helper 的平台、路径、自检参数与进程内锁缓存。
def test_helper_discovery_is_once_and_thread_safe(monkeypatch, tmp_path):
    candidate = tmp_path / "my-agent-sandbox-helper.linux-x86_64"
    candidate.write_text("fixture")
    candidate.chmod(0o755)
    monkeypatch.setattr(sandbox, "_VENDOR_BIN", tmp_path)
    monkeypatch.setattr(sandbox, "_SANDBOX_HELPER", [])
    monkeypatch.setattr(sandbox.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sandbox.platform, "machine", lambda: "x86_64")
    calls = []
    # LLM: 仅模拟 exec 探针结果，保留完整参数供断言，不冒充 Linux 安全实测。
    # 函数用途: 记录唯一一次无参数自检。
    def probe(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=ll.BAD_ARGV_EXIT_CODE)
    monkeypatch.setattr(sandbox.subprocess, "run", probe)
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(lambda _: sandbox.find_sandbox_helper(), range(32)))
    assert values == [str(candidate)] * 32
    assert calls == [([str(candidate)], {"capture_output": True, "timeout": 10, "check": False})]


# LLM: 发现失败只停用优化，坏架构/超时/缺文件均不得改变旧启动路径。
# 函数用途: 验证一次性负缓存和不可执行文件的短路。
@pytest.mark.parametrize("failure", ["missing", "not-executable", "bad-code", "exec-error", "timeout", "darwin"])
def test_helper_discovery_failure_keeps_python(monkeypatch, tmp_path, failure):
    candidate = tmp_path / "my-agent-sandbox-helper.linux-x86_64"
    if failure != "missing":
        candidate.write_text("fixture")
        candidate.chmod(0o644 if failure == "not-executable" else 0o755)
    monkeypatch.setattr(sandbox, "_VENDOR_BIN", tmp_path)
    monkeypatch.setattr(sandbox, "_SANDBOX_HELPER", [])
    monkeypatch.setattr(sandbox.platform, "system", lambda: "Darwin" if failure == "darwin" else "Linux")
    monkeypatch.setattr(sandbox.platform, "machine", lambda: "x86_64")
    calls = []
    # LLM: 只构造三种明确探针失败，不吞掉产品层错误。
    # 函数用途: 分别返回错误码或抛出 exec/超时异常。
    def probe(*args, **kwargs):
        calls.append(args)
        if failure == "exec-error":
            raise OSError("wrong architecture")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args[0], 10)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(sandbox.subprocess, "run", probe)
    assert sandbox.find_sandbox_helper() is None
    assert sandbox.find_sandbox_helper() is None
    assert len(calls) == (0 if failure in {"missing", "not-executable", "darwin"} else 1)


# LLM: 预建只换 deny 参数，缓存按规范端口集合隔离；fd 不交给其它 spawn。
# 函数用途: 验证去重、缓存复用和原 argv 不被就地修改。
def test_adopt_reuses_ruleset_by_port_set(monkeypatch):
    helper = _helper(monkeypatch)
    monkeypatch.setattr(attempt, "_PREBUILT_RULESETS", {})
    calls = []
    read_fd, write_fd = os.pipe()
    try:
        monkeypatch.setattr(ll, "build_connect_tcp_ruleset", lambda ports: (calls.append(ports), read_fd)[1], raising=False)
        argv = [helper, "--deny", "9000,8420,9000", "--nice", "5", "--", "/usr/bin/bwrap", "--", "/bin/true"]
        expected = [helper, "--landlock-fd", str(read_fd), *argv[3:]]
        assert attempt.adopt_prebuilt_landlock_ruleset(argv) == (expected, (read_fd,))
        assert attempt.adopt_prebuilt_landlock_ruleset([helper, "--deny", "8420,9000", *argv[3:]]) == (expected, (read_fd,))
        assert calls == [{8420, 9000}] and argv[1:3] == ["--deny", "9000,8420,9000"]
    finally:
        os.close(read_fd)
        os.close(write_fd)


# LLM: 自建失败时仍由 helper 自建并 fail-closed，不移除端口限制、不透传坏fd。
# 函数用途: 验证预建失败和非 helper argv 均原样返回。
@pytest.mark.parametrize("error", [ll.LandlockUnavailable("NO_ABI"), ll.LandlockApplyError("ADD_RULE"), OSError("fd limit")])
def test_adopt_failure_preserves_self_contained_argv(monkeypatch, error):
    helper = _helper(monkeypatch)
    monkeypatch.setattr(attempt, "_PREBUILT_RULESETS", {})
    # LLM: 明确制造预建错误；只有原始 deny 形式可安全继续。
    # 函数用途: 触发预建的异常回退。
    def failed(_ports):
        raise error
    monkeypatch.setattr(ll, "build_connect_tcp_ruleset", failed, raising=False)
    argv = [helper, "--deny", "8420", "--", "/bin/true"]
    rewritten, fds = attempt.adopt_prebuilt_landlock_ruleset(argv)
    assert rewritten is argv and fds == ()
    plain = ["/bin/true"]
    assert attempt.adopt_prebuilt_landlock_ruleset(plain) == (plain, ())


# LLM: 三类瞬时侧车错误仅删除 mount 区域中的 try-bind，不能动尾部业务参数。
# 函数用途: 覆盖 helper 前缀下完整24组侧车裁剪场景。
@pytest.mark.parametrize("kind", range(3))
@pytest.mark.parametrize("name", ["runtime.db-wal", "runtime.db-shm", "runtime.db-journal", "policy.lock"])
@pytest.mark.parametrize("native", [False, True])
def test_sidecar_pruning_keeps_helper_and_command(monkeypatch, tmp_path, kind, name, native):
    helper = _helper(monkeypatch)
    leaf, live, gone = tmp_path / name, tmp_path / "live-wal", tmp_path / "gone-shm"
    leaf.write_text("present")
    live.write_text("present")
    prefix = ([helper, "--deny", "8420", "--nice", "5", "--"] if native else
              ["python", "-I", "landlock_launcher.py", "--deny", "8420", "--"])
    base = [*prefix, "/usr/bin/bwrap", "--ro-bind", "/config", "/config"]
    tail = ["--", "echo", "--ro-bind-try", str(leaf), str(leaf)]
    argv = [*base, "--ro-bind-try", str(leaf), str(leaf), "--ro-bind-try", str(live), str(live),
            "--ro-bind-try", str(gone), str(gone), *tail]
    error = (f"bwrap: Can't find source path {leaf}: No such file or directory" if kind == 0 else
             f"bwrap: Can't bind mount /oldroot{leaf} on /newroot{leaf}: " +
             ("Unable to find mount" if kind == 1 else "Unable to remount destination"))
    before = argv.copy()
    assert sandbox.prune_transient_bwrap_sidecar_mounts(argv, error) == [
        *base, "--ro-bind-try", str(live), str(live), *tail]
    assert argv == before


# LLM: 用真实前台 spawn 构参和替身 Popen，观察 pass_fds；不启动任何业务进程。
# 函数用途: 只在明确采用预建 fd 时透传描述符，保留 stdin/session/env 合同。
@pytest.mark.parametrize("fds", [(), (17,)])
def test_foreground_spawn_passes_only_adopted_fds(monkeypatch, tmp_path, fds):
    argv = ["/trusted/helper", "--deny", "8420", "--", "/bin/true"]
    adopted = ["/trusted/helper", "--landlock-fd", "17", "--", "/bin/true"] if fds else argv
    seen = []
    monkeypatch.setattr(attempt, "adopt_prebuilt_landlock_ruleset", lambda arg: (adopted, fds), raising=False)
    monkeypatch.setattr(shell.subprocess, "Popen", lambda arg, **kw: seen.append((arg, kw)))
    tool = SimpleNamespace(path_access_policy=SimpleNamespace(owner_scope_root=None), host_private_roots=(),
                           host_credential_env_names=frozenset())
    shell._start_attempt_shell_process(argv, False, tmp_path, tool)
    assert seen[0][0] == adopted
    kwargs = seen[0][1]
    assert kwargs["stdin"] == subprocess.DEVNULL and kwargs["start_new_session"] is True
    assert kwargs.get("pass_fds", ()) == fds
    assert ("pass_fds" in kwargs) is bool(fds)
    assert argv[1:3] == ["--deny", "8420"]


# LLM: 编译当前源码到 pytest 私有临时目录；非 Linux 或缺 cc 才跳过，编译错误必须失败。
# 函数用途: 为真实 Linux 等价测试构建当前版本 helper。
@pytest.fixture(scope="module")
def native_helper(tmp_path_factory):
    if not sys.platform.startswith("linux") or shutil.which("cc") is None:
        pytest.skip("requires Linux C compiler")
    path = tmp_path_factory.mktemp("native-helper") / "helper"
    source = Path(__file__).resolve().parents[2] / "scripts/sandbox_helper/sandbox_helper.c"
    subprocess.run([shutil.which("cc"), "-O2", "-Wall", "-Wextra", "-fstack-protector-strong",
                    "-D_FORTIFY_SOURCE=2", str(source), "-o", str(path)], check=True, capture_output=True)
    return str(path)


# LLM: 只查 Landlock ABI，不在 pytest 本身施加限制；ABI<4 的内核按规范明确跳过。
# 函数用途: 给仅网络限制用例建立真实内核前置事实。
@pytest.fixture
def landlock_ready(native_helper):
    nums = ll.syscall_numbers()
    if nums is None or ll.landlock_abi(ctypes.CDLL(None, use_errno=True), nums) < 4:
        pytest.skip("requires Landlock network ABI >=4")


# LLM: 临时监听仅在测试回环随机端口，不触碰生产服务，不需要后台 accept 线程。
# 函数用途: 提供被拒和对照两个真实 TCP 端口。
@pytest.fixture
def listeners():
    with socket.socket() as denied, socket.socket() as allowed:
        for srv in (denied, allowed):
            srv.bind(("127.0.0.1", 0))
            srv.listen(16)
        yield denied.getsockname()[1], allowed.getsockname()[1]


_RESOURCE_REPORT = """import json,os,resource
from pathlib import Path
print(json.dumps({'nice':os.getpriority(os.PRIO_PROCESS,0),
 'oom':int(Path('/proc/self/oom_score_adj').read_text()),
 'as':resource.getrlimit(resource.RLIMIT_AS),'nproc':resource.getrlimit(resource.RLIMIT_NPROC),
 'session_leader':os.getsid(0)==os.getpid(),'ppid':os.getppid()}))
"""
_CONNECT_REPORT = """import json,os,socket,sys
from pathlib import Path
def connect(port):
    with socket.socket() as s:
        s.settimeout(2)
        try: s.connect(('127.0.0.1',port)); return 0
        except OSError as e: return e.errno
print(json.dumps({'blocked':connect(int(sys.argv[1])),'allowed':connect(int(sys.argv[2])),
 'fd_present':Path('/proc/self/fd/'+sys.argv[3]).exists() if len(sys.argv)>3 else False,
 'nnp':next(line.split()[1] for line in Path('/proc/self/status').read_text().splitlines() if line.startswith('NoNewPrivs:'))}))
"""


# LLM: 两条真实启动器运行同一配置；只比较可跨进程等价的 sid/ppid 事实，不强求不同pid相等。
# 函数用途: 核对 nice、OOM、AS/NPROC 硬上限及独立会话与父身份。
def test_native_resources_match_python_launcher(native_helper, monkeypatch):
    limits = sandbox.ResourceLimits(3, 512, 4096, 311)
    command = [sys.executable, "-I", "-B", "-c", _RESOURCE_REPORT]
    _helper(monkeypatch, None)
    old = sandbox.wrap_resource_command(command, limits)
    _helper(monkeypatch, native_helper)
    new = sandbox.wrap_resource_command(command, limits)
    reports = []
    for argv in (old, new):
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=15, start_new_session=True)
        assert proc.returncode == 0, proc.stderr
        reports.append(json.loads(proc.stdout))
    assert reports[0] == reports[1]
    assert reports[1]["session_leader"] and reports[1]["ppid"] == os.getpid()
    assert reports[1]["as"] == [512 * 1024**2] * 2 and reports[1]["nproc"] == [4096] * 2


# LLM: 无效fd、坏参数和exec失败都不能运行后面的命令；退出码/JSON与宿主识别合同一致。
# 函数用途: 以哨兵证明 fail-closed 并覆盖数值配置边界。
@pytest.mark.parametrize("flags,code", [
    (["--landlock-fd", "999"], 97), (["--nice", "20"], 96), (["--oom-score-adj", "1001"], 96),
    (["--max-memory-mb", str(2**63)], 96), (["--max-processes", str(2**31)], 96),
    (["--deny", "65536"], 96), (["--deny", ""], 96), (["--cgroup", "/tmp/forbidden"], 96),
])
def test_native_fail_closed_does_not_exec(native_helper, tmp_path, flags, code):
    sentinel = tmp_path / "must-not-exist"
    proc = subprocess.run([native_helper, *flags, "--", "/bin/sh", "-c", f"touch {shlex.quote(str(sentinel))}"],
                          capture_output=True, text=True, timeout=10)
    assert proc.returncode == code and not sentinel.exists()
    if code == 97:
        assert ll.is_apply_failed(proc.returncode, proc.stderr)


# LLM: 原生exec只接受绝对路径；合法语法但缺失程序映射资源守卫98，不冒充普通命令失败。
# 函数用途: 锁住探针96、相对路径96与exec失败98的区分。
def test_native_bad_argv_and_exec_error(native_helper):
    for argv in ([], ["--", "true"], ["--nice"], ["--deny", "1", "--landlock-fd", "9", "--", "/bin/true"]):
        assert subprocess.run([native_helper, *argv], capture_output=True, timeout=10).returncode == 96
    proc = subprocess.run([native_helper, "--", "/nonexistent/sp1-program"], capture_output=True, text=True, timeout=10)
    assert proc.returncode == 98
    assert json.loads(proc.stderr.splitlines()[0])["error_code"] == "SANDBOX_RESOURCE_GUARD_FAILED"


# LLM: 两种Landlock入口都真实拒绝P并允许对照端口；预建不改变父进程NNP/网络，命令看不到ruleset fd。
# 函数用途: 验证网络等价、FD_CLOEXEC、restrict_self后的fd关闭和父进程不受限。
@pytest.mark.parametrize("prebuilt", [False, True])
def test_native_port_deny_and_fd_hygiene(native_helper, landlock_ready, listeners, prebuilt):
    blocked, allowed = listeners
    fd = None
    inherited = ()
    before = Path("/proc/self/status").read_text().split("NoNewPrivs:", 1)[1].splitlines()[0]
    try:
        if prebuilt:
            fd = ll.build_connect_tcp_ruleset({blocked})
            assert not os.get_inheritable(fd)
            assert Path("/proc/self/status").read_text().split("NoNewPrivs:", 1)[1].splitlines()[0] == before
            with socket.create_connection(("127.0.0.1", blocked), timeout=2):
                pass
            flags, inherited = ["--landlock-fd", str(fd)], (fd,)
        else:
            # More than 16 distinct ports, including 0 and 65535, must be supported.
            ports = {blocked, *range(32), 65535} - {allowed}
            flags = ["--deny", ",".join(map(str, sorted(ports)))]
        command = [sys.executable, "-I", "-B", "-c", _CONNECT_REPORT, str(blocked), str(allowed), str(fd or 999)]
        proc = subprocess.run([native_helper, *flags, "--", *command], pass_fds=inherited,
                              capture_output=True, text=True, timeout=20)
        assert proc.returncode == 0, proc.stderr
        report = json.loads(proc.stdout)
        assert report == {"blocked": errno.EACCES, "allowed": 0, "fd_present": False, "nnp": "1"}
    finally:
        if fd is not None:
            os.close(fd)


# LLM: 真实ShellTool会走attempt/bwrap/前台adopt；只登记临时回环端口，结束后精确注销，不接触生产Gateway。
# 函数用途: 证明正常命令能运行、Gateway式端口被拒，且实际前台使用预建fd路径。
def test_native_gateway_shell_execute(native_helper, landlock_ready, listeners, monkeypatch, tmp_path):
    ready, reason = attempt.landlock_net_readiness(None)
    if not ready:
        pytest.skip(f"real bwrap unavailable: {reason}")
    _helper(monkeypatch, native_helper)
    blocked, allowed = listeners
    attempt.register_gateway_bound_port(blocked)
    calls = []
    original = attempt.adopt_prebuilt_landlock_ruleset
    # LLM: 包装真实adopt只采样返回事实，不替代其安全决策或内核调用。
    # 函数用途: 观察真实前台确实携带一个预建fd。
    def observed(argv):
        result = original(argv)
        calls.append(result)
        return result
    monkeypatch.setattr(attempt, "adopt_prebuilt_landlock_ruleset", observed)
    try:
        tool = shell.ShellTool(tmp_path, options=shell.ShellToolOptions(access_mode="full-access"))
        assert tool.execute({"command": "printf SP1_NORMAL"}).ok
        command = shlex.join([sys.executable, "-I", "-B", "-c", _CONNECT_REPORT, str(blocked), str(allowed)])
        result = tool.execute({"command": command})
        assert result.ok, result.output
        assert f'"blocked": {errno.EACCES}' in result.output and '"allowed": 0' in result.output
        assert calls and all(len(fds) == 1 and argv[1] == "--landlock-fd" for argv, fds in calls)
    finally:
        attempt.unregister_gateway_bound_port(blocked)


# LLM: 只加载当前工作树脚本，不用安装副本；构建器/发行器的临时验证通过同一入口。
# 函数用途: 返回构建脚本模块，避免依赖sys.path外的同名文件。
def _script(relative):
    path = Path(__file__).resolve().parents[2] / "scripts" / relative
    spec = importlib.util.spec_from_file_location("sp1_build_" + path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# LLM: 样本只作发行复制/摘要检查，不执行；标注真实ELF64小端机器号便于拒绝错架构。
# 函数用途: 生成可核对的二进制样本及旁文件。
def _elf_fixture(path, machine=62):
    body = bytearray(64)
    body[:7] = b"\x7fELF\x02\x01\x01"
    body[18:20] = machine.to_bytes(2, "little")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    Path(str(path) + ".sha256").write_text(hashlib.sha256(body).hexdigest() + "  " + path.name + "\n")
    return path


# LLM: wheel可能已带二进制和sha，源目标同址不能触发SameFileError；复制后旁文件摘要仍匹配。
# 函数用途: 复现并保护发行器复用包内helper的分支。
def test_release_reuses_in_package_helper_and_checksum(tmp_path):
    build = _script("private_release/build.py")
    package = tmp_path / "package"
    target = _elf_fixture(package / "vendor/bin/my-agent-sandbox-helper.linux-x86_64")
    build.copy_sandbox_helpers(tmp_path, package)
    assert target.stat().st_mode & 0o111
    assert Path(str(target) + ".sha256").read_text().split()[0] == hashlib.sha256(target.read_bytes()).hexdigest()


# LLM: 原生资源须先有x86_64才允许出包，aarch64可选；不依赖Ubuntu安装树或外部编译器。
# 函数用途: 核验发行必选/可选架构、校验失败拒绝以及完整旁文件复制。
def test_release_requires_x86_and_includes_available_arm(tmp_path):
    build = _script("private_release/build.py")
    package = tmp_path / "package"
    with pytest.raises(ValueError, match="helper"):
        build.copy_sandbox_helpers(tmp_path, package)
    x86 = _elf_fixture(tmp_path / "native/my-agent-sandbox-helper.linux-x86_64")
    arm = _elf_fixture(tmp_path / "native/my-agent-sandbox-helper.linux-aarch64", 183)
    build.copy_sandbox_helpers(tmp_path, package)
    for source in (x86, arm):
        assert (package / "vendor/bin" / source.name).read_bytes() == source.read_bytes()
        assert (package / "vendor/bin" / (source.name + ".sha256")).read_bytes() == Path(str(source) + ".sha256").read_bytes()
    x86.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="helper"):
        build.copy_sandbox_helpers(tmp_path, package)


# LLM: 用可观测编译器替身触发静态失败再动态成功，所有加固参数必须保留；不把替身当Linux编译证据。
# 函数用途: 保护构建器的回退顺序、产物路径、摘要与双失败清理。
def test_builder_static_fallback_and_checksum(monkeypatch, tmp_path):
    build = _script("build_sandbox_helper.py")
    source = tmp_path / "source.c"
    source.write_text("int main(void) { return 96; }")
    monkeypatch.setattr(build.platform, "system", lambda: "Linux")
    calls = []
    # LLM: 静态失败时不产出文件；动态成功写固定ELF样本供精确摘要核对。
    # 函数用途: 记录并模拟两次受控编译。
    def compile_command(argv, **_kwargs):
        calls.append(argv)
        code = 1 if "-static" in argv else 0
        if not code:
            _elf_fixture(Path(argv[argv.index("-o") + 1]))
        return SimpleNamespace(returncode=code, stderr="static libc unavailable" if code else "")
    monkeypatch.setattr(build.subprocess, "run", compile_command)
    result = build.build(source, tmp_path / "out", arch="x86_64")
    assert result["link"] == "dynamic" and len(calls) == 2
    assert "-static" in calls[0] and "-static" not in calls[1]
    for argv in calls:
        assert all(flag in argv for flag in ("-O2", "-Wall", "-Wextra", "-fstack-protector-strong", "-D_FORTIFY_SOURCE=2"))
    target = Path(result["path"])
    assert result["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert Path(str(target) + ".sha256").read_text().split()[0] == result["sha256"]


# LLM: --arch不可仅改文件名而留下错误机器码，否则发行包会静默走Python回退；使用本机真实cc做反例。
# 函数用途: 拒绝把本机x86编译结果伪标成aarch64，反之亦然。
def test_builder_rejects_mislabeled_architecture(native_helper, tmp_path):
    build = _script("build_sandbox_helper.py")
    actual = {"x86_64": "aarch64", "aarch64": "x86_64"}.get(os.uname().machine)
    if actual is None:
        pytest.skip("unsupported test compiler architecture")
    source = Path(__file__).resolve().parents[2] / "scripts/sandbox_helper/sandbox_helper.c"
    with pytest.raises(ValueError, match="architecture|架构"):
        build.build(source, tmp_path / "out", arch=actual)
