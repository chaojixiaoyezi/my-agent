"""R5：验证编译前拒绝、真实安装器启动及 portable 依赖准备的 ABI 分流。"""
from __future__ import annotations

import importlib.util
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.private_release import build

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts/private_release"


# LLM: 只创建临时合成发行副本；解释器委托真实测试Python，原生文件不执行。
# 函数用途: 为前置拒绝与真实安装器字节码启动提供同一个最小构建输入。
@pytest.fixture
def portable_build_case(tmp_path, monkeypatch):
    root, output = tmp_path / "bundle with spaces", tmp_path / "output"
    package = root / f"runtime/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages/agent_py_agent"
    for path in (root / "runtime/bin", root / "native/lib", root / "native/lark_cli",
                 package / "vendor/bin", package / "vendor/bubblewrap", package / "config"):
        path.mkdir(parents=True)
    python = root / "runtime/bin/python3"
    python.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
    python.chmod(0o755)
    (root / "runtime/bin/example").write_text("#!/old/build/python3\nprint('relocate')\n")
    for name in ("bwrap", "PROVENANCE.txt", "feishu-cli-host.linux-x86_64", "lib/libfixture.so", "lark_cli/release.json"):
        (root / "native" / name).write_bytes(b"synthetic native resource")
    helper = bytearray(64)
    helper[:7] = b"\x7fELF\x02\x01\x01"
    helper[18:20] = (62).to_bytes(2, "little")
    (root / "native/my-agent-sandbox-helper.linux-x86_64").write_bytes(helper)
    (package / "__init__.py").write_text("VALUE = 1\n")
    (package / "config/agent_config.yaml").write_text('workspace_root: ""\ngateway_port: 8420\n')
    argv = ["build", "--bundle", str(root), "--helpers", str(SCRIPTS), "--output", str(output),
            "--version", "test.r5", "--base-commit", "fixture", "--source-sha256", "0" * 64]
    monkeypatch.setattr(sys, "argv", argv)
    return SimpleNamespace(root=root, package=package, python=python, output=output, argv=argv)


def _file_snapshot(root):
    return {str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mode)
            for path in root.rglob("*") if path.is_file()}


def test_magic_mismatch_refuses_before_any_bundle_mutation(portable_build_case):
    case = portable_build_case
    # 版本号仍真实一致，唯独改变magic；不能只比较major/minor或目录名。
    magic = bytes([importlib.util.MAGIC_NUMBER[0] ^ 0xFF]) + importlib.util.MAGIC_NUMBER[1:]
    bootstrap = ("import importlib.util, sys; "
                 f"importlib.util.MAGIC_NUMBER = {magic!r}; exec(sys.argv[1])")
    case.python.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -I -c {shlex.quote(bootstrap)} "$3"\n')
    before = _file_snapshot(case.root)
    with pytest.raises(ValueError, match="MAGIC_NUMBER") as error:
        build.main()
    assert shlex.join([str(case.python), *case.argv]) in str(error.value)
    assert _file_snapshot(case.root) == before
    assert not case.output.exists()
    assert (case.package / "__init__.py").is_file()
    assert not list(case.root.rglob("*.pyc"))


@pytest.mark.parametrize("magic", ["", "not-hex", "00"])
def test_invalid_magic_probe_cannot_publish(portable_build_case, magic):
    case = portable_build_case
    version = ".".join(map(str, sys.version_info[:3]))
    case.python.write_text(f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(version)} {shlex.quote(magic)}\n")
    before = _file_snapshot(case.root)
    with pytest.raises(ValueError):
        build.main()
    assert _file_snapshot(case.root) == before
    assert not case.output.exists()


def test_same_interpreter_run_reaches_real_installer_help(portable_build_case, tmp_path):
    case = portable_build_case
    assert build.main() == 0
    assert not (case.package / "__init__.py").exists()
    assert (case.package / "__init__.pyc").read_bytes()[:4] == importlib.util.MAGIC_NUMBER
    assert (case.root / "installer.pyc").read_bytes()[:4] == importlib.util.MAGIC_NUMBER
    # 只替代外层架构观察；实际解包、哈希校验、解释器和原installer.pyc均真实执行。
    # 这是本机启动回归，不声称合成原生文件或Linux x86_64运行时已获验收。
    commands = tmp_path / "commands"
    commands.mkdir()
    uname = commands / "uname"
    uname.write_text("#!/bin/sh\nprintf 'x86_64\\n'\n")
    uname.chmod(0o755)
    result = subprocess.run(
        ["bash", str(case.output / "my-agent-test.r5-linux-x86_64.run"), "--help"],
        env={**os.environ, "PATH": str(commands) + os.pathsep + os.environ["PATH"]},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "my-agent 临时离线安装 / 更新" in result.stdout
    assert "--prefix" in result.stdout and "--verify-only" in result.stdout


# LLM: 执行未改写的prepare_runtime.sh；只替代pip和解释器环境，绝不联网安装依赖。
# 函数用途: 在同一个shell入口观察版本、GIL、精确依赖及失败传播。
@pytest.fixture
def prepare_case(tmp_path):
    root = tmp_path / "runtime build"
    for name in ("bin", "lib", "include", "share"):
        (root / name).mkdir(parents=True)
    driver = tmp_path / "interpreter.py"
    driver.write_text('''import builtins, importlib.metadata, json, os, sys, sysconfig, types
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["R5_LOG"], "a") as stream:
    stream.write(json.dumps(args) + "\\n")
if args[:2] == ["-m", "pip"]:
    binary = any(arg.startswith(("psycopg-binary==", "psycopg[binary]==")) for arg in args)
    if binary and os.environ.get("R5_PIP_FAIL"):
        raise SystemExit(17)
    raise SystemExit(0)
assert args[0] == "-c", args
sys.version_info = (*json.loads(os.environ["R5_VERSION"]), 4, "final", 0)
original_config = sysconfig.get_config_var
sysconfig.get_config_var = lambda name: int(os.environ["R5_GIL"]) if name == "Py_GIL_DISABLED" else original_config(name)
original_version = importlib.metadata.version
def version(name):
    if name != "psycopg":
        return original_version(name)
    if os.environ.get("R5_METADATA_FAIL"):
        raise importlib.metadata.PackageNotFoundError(name)
    return os.environ["R5_RESOLVED"]
importlib.metadata.version = version
for name in ("agent_py_agent", "psycopg", "psycopg_binary"):
    module = types.ModuleType(name)
    module.__version__ = os.environ["R5_RESOLVED"]
    module.pq = types.SimpleNamespace(__impl__="binary")
    sys.modules[name] = module
original_import = builtins.__import__
def imported(name, *args, **kwargs):
    if name.startswith("psycopg") and os.environ.get("R5_IMPORT_FAIL"):
        raise ImportError("synthetic missing libpq/binary")
    return original_import(name, *args, **kwargs)
builtins.__import__ = imported
exec(args[1])
''')
    python = root / "bin/python3"
    python.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -I {shlex.quote(str(driver))} "$@"\n')
    python.chmod(0o755)
    log = tmp_path / "calls.jsonl"

    def run(version=(3, 14), gil=0, resolved="3.2.9", **flags):
        result = subprocess.run(
            ["bash", str(SCRIPTS / "prepare_runtime.sh"), str(root), str(tmp_path / "source")],
            env={**os.environ, "R5_LOG": str(log), "R5_VERSION": json.dumps(version),
                 "R5_GIL": str(gil), "R5_RESOLVED": resolved, **flags},
            capture_output=True, text=True, timeout=30,
        )
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        return result, calls

    return run


@pytest.mark.parametrize("resolved", ["3.2.9", "3.3.1"])
def test_normal_314_installs_resolved_binary_wheel_then_imports(prepare_case, resolved):
    result, calls = prepare_case(resolved=resolved)
    assert result.returncode == 0, result.stderr
    installs = [call for call in calls if call[:3] == ["-m", "pip", "install"]]
    assert len(installs) == 2
    assert installs[1][-1] in {f"psycopg-binary=={resolved}", f"psycopg[binary]=={resolved}"}
    assert "--only-binary=:all:" in installs[1]
    imports = [call for call in calls if call[0] == "-c" and "import psycopg" in call[1]]
    assert imports
    assert calls.index(imports[0]) > calls.index(installs[1])


@pytest.mark.parametrize("version,gil", [((3, 14), 1), ((3, 11), 0), ((3, 12), 0), ((3, 15), 0)])
def test_free_threaded_and_legacy_prepare_do_not_add_cp314_binary(prepare_case, version, gil):
    result, calls = prepare_case(version=version, gil=gil)
    assert result.returncode == 0, result.stderr
    installs = [call for call in calls if call[:3] == ["-m", "pip", "install"]]
    assert len(installs) == 1
    assert installs[0][-1].endswith(".whl[scale,secrets]")
    assert all("psycopg-binary==" not in arg and "psycopg[binary]==" not in arg for call in calls for arg in call)


@pytest.mark.parametrize("flag,returncode", [("R5_PIP_FAIL", 17), ("R5_IMPORT_FAIL", 1), ("R5_METADATA_FAIL", 1)])
def test_normal_314_prepare_fails_closed_on_dependency_error(prepare_case, flag, returncode):
    result, calls = prepare_case(**{flag: "1"})
    assert result.returncode == returncode, (result.stdout, result.stderr)
    assert not any("import agent_py_agent, ssl, sqlite3" in arg for call in calls for arg in call)
