"""固定后台入口的源码/无源码闭包；不启动host，不运输或签发任何原生权限。"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import os
import py_compile
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import agent_py_agent

_ROUTES = (
    ("multiprocess_executor_delegation", "bootstrap_argv", "multiprocess_executor_bootstrap"),
    ("multiprocess_native_host", "_native_host_bootstrap_argv", "multiprocess_native_host_bootstrap"),
)
_PREFIX = "agent_py_agent.agent.gateway_parts."


# LLM: 子进程仅继承固定运行必需项，显式隔离home，不传播模型、网络或用户凭据；不能启动host。
# 函数用途: 给只解析入口和导入闭包的Python子进程提供最小合成环境。
def _child_environment(home):
    environment = {"HOME": str(home), "TMPDIR": str(home), "MY_AGENT_HOME": str(home / "explicit-home"),
                   "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1",
                   "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    for key in ("SYSTEMROOT", "WINDIR"):
        if key in os.environ:
            environment[key] = os.environ[key]
    return environment


# LLM: 只哈希临时包文件，用于证明只读入口测试没有修改发行投影，不读取真实配置。
# 函数用途: 记录隔离包在子进程执行前后的准确字节地图。
def _file_map(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


# LLM: 从当前实际包的.py或.pyc生成隔离无源码投影，保留原模块实现；不替换Agent/权限，不构建正式发行包。
# 函数用途: 临时编译或复制包内代码并设为只读，收尾恢复临时目录权限供pytest清理。
@pytest.fixture(scope="module")
def readonly_bytecode_package(tmp_path_factory):
    root = tmp_path_factory.mktemp("fixed-bootstrap-bytecode")
    package = Path(agent_py_agent.__file__).resolve().parent
    destination = root / "agent_py_agent"
    for entry in sorted(package.rglob("*")):
        relative = entry.relative_to(package)
        if not entry.is_file() or entry.suffix not in {".py", ".pyc"}:
            continue
        if any(part in {"tests", "skills", "__pycache__"} for part in relative.parts):
            continue
        target = (destination / relative).with_suffix(".pyc")
        target.parent.mkdir(parents=True, exist_ok=True)
        if entry.suffix == ".py":
            py_compile.compile(str(entry), cfile=str(target), dfile=str(target.with_suffix(".py")), doraise=True)
        else:
            shutil.copyfile(entry, target)
    assert (destination / "__init__.pyc").is_file()
    assert not list(destination.rglob("*.py"))
    before = _file_map(root)
    for entry in root.rglob("*"):
        entry.chmod(0o555 if entry.is_dir() else 0o444)
    root.chmod(0o555)
    try:
        yield root
        assert _file_map(root) == before
    finally:
        root.chmod(0o755)
        for entry in root.rglob("*"):
            entry.chmod(0o755 if entry.is_dir() else 0o644)


@pytest.mark.parametrize("route", _ROUTES, ids=["executor", "native-host"])
def test_fixed_bootstrap_argv_resolves_actual_packaged_entry(route):
    caller, helper, bootstrap = route
    module = importlib.import_module(_PREFIX + caller)
    argv = getattr(module, helper)(41)
    origin = Path(importlib.util.find_spec(_PREFIX + bootstrap).origin).resolve()
    assert argv == (sys.executable, "-I", str(origin), "41")
    assert origin.is_file()
    assert origin.parent == Path(module.__file__).resolve().parent
    assert origin.suffix in {".py", ".pyc"}


def test_fixed_argv_uses_real_sourceless_module_origins(readonly_bytecode_package, tmp_path):
    root = readonly_bytecode_package
    program = """
import importlib, importlib.util, pathlib, sys
sys.path.insert(0, sys.argv[1])
import agent_py_agent
assert pathlib.Path(agent_py_agent.__file__).resolve() == pathlib.Path(sys.argv[1])/'agent_py_agent/__init__.pyc'
for caller, helper, bootstrap in (
 ('multiprocess_executor_delegation', 'bootstrap_argv', 'multiprocess_executor_bootstrap'),
 ('multiprocess_native_host', '_native_host_bootstrap_argv', 'multiprocess_native_host_bootstrap')):
 prefix='agent_py_agent.agent.gateway_parts.'
 module=importlib.import_module(prefix+caller)
 assert pathlib.Path(module.__file__).suffix == '.pyc'
 argv=getattr(module, helper)(41)
 origin=pathlib.Path(importlib.util.find_spec(prefix+bootstrap).origin).resolve()
 assert argv == (sys.executable, '-I', str(origin), '41')
 assert origin.is_file() and origin.suffix == '.pyc'
 assert origin.is_relative_to(pathlib.Path(sys.argv[1]))
for name, module in tuple(sys.modules.items()):
 if name == 'agent_py_agent' or name.startswith('agent_py_agent.'):
  path=pathlib.Path(module.__file__).resolve()
  assert path.is_relative_to(pathlib.Path(sys.argv[1])) and path.suffix == '.pyc'
"""
    result = subprocess.run([sys.executable, "-I", "-B", "-c", program, str(root)],
                            env=_child_environment(tmp_path), cwd=tmp_path,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""
    assert not (tmp_path / "explicit-home").exists()


@pytest.mark.parametrize("bootstrap", [route[2] for route in _ROUTES])
def test_actual_pyc_entry_without_handoff_exits_125(readonly_bytecode_package, tmp_path, bootstrap):
    target = readonly_bytecode_package / "agent_py_agent/agent/gateway_parts" / (bootstrap + ".pyc")
    result = subprocess.run([sys.executable, "-I", "-B", str(target)],
                            env=_child_environment(tmp_path), cwd=tmp_path,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 125
    assert result.stdout == result.stderr == ""
    assert not (tmp_path / "explicit-home").exists()


def test_original_source_closure_assertions_bind_separate_bytecode_package(readonly_bytecode_package, tmp_path):
    original = Path(__file__).with_name("test_gateway_coordinator_source_closure.py")
    # 正式QA保留独立测试源码；业务模块只能来自实际无源码包，原home/身份断言完整运行。
    qa = tmp_path / "qa"
    qa.mkdir()
    copied = qa / original.name
    shutil.copyfile(original, copied)
    config = qa / "pytest.ini"
    config.write_text("[pytest]\n", encoding="utf-8")
    program = """
import pathlib, sys
sys.path.insert(0, sys.argv[1])
import agent_py_agent
assert pathlib.Path(agent_py_agent.__file__).suffix == '.pyc'
assert pathlib.Path(agent_py_agent.__file__).is_relative_to(pathlib.Path(sys.argv[1]))
import pytest
raise SystemExit(pytest.main(['-q', '--tb=short', '-p', 'no:cacheprovider',
 '--basetemp', str(pathlib.Path(sys.argv[2]).parent/'pytest-temp'), '-c', sys.argv[3], sys.argv[2]]))
"""
    result = subprocess.run([sys.executable, "-I", "-B", "-c", program,
                             str(readonly_bytecode_package), str(copied), str(config)],
                            env=_child_environment(tmp_path), cwd=tmp_path,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "7 passed" in result.stdout
    assert not (tmp_path / "explicit-home").exists()
