"""标准 SDK wheel 与真正独立解释器的开发验证，不代替真实 TUI 验收。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import venv
from pathlib import Path
from zipfile import ZipFile

import pytest

from agent_py_agent.agent.common import nofollow_fs as fs
from agent_py_agent.tests.test_workspace_read_context import read_context
from scripts.build_plugin_api import ROOT, SDK_SOURCES, build_plugin_api, verify_sdk_wheel
from scripts.plugin_build import publish_artifact


# LLM: 夹具只在临时目录构建和安装真实标准 wheel；独立解释器没有宿主包，不用 sys.path 注入伪造安装。
# 函数用途: 为 SDK 开发验收准备只装 SDK 的独立环境。
@pytest.fixture(scope="module")
def installed_sdk(tmp_path_factory):
    root = tmp_path_factory.mktemp("sdk-build")
    wheel = build_plugin_api(root / "wheels")
    venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(root / "env")
    python = root / "env" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "--isolated", "--python", str(python), "install",
         "--no-index", "--no-deps", "--no-cache-dir", str(wheel)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return python, wheel


def test_wheel_contains_exact_canonical_sources_and_license(installed_sdk):
    _, wheel = installed_sdk
    expected = {name: (ROOT / path).read_bytes() for name, path in SDK_SOURCES.items()}
    verify_sdk_wheel(wheel, expected)
    with ZipFile(wheel) as archive:
        for filename in ("LICENSE", "NOTICE"):
            name, = [name for name in archive.namelist() if name.endswith("/licenses/" + filename)]
            assert archive.read(name) == (ROOT / filename).read_bytes()


def test_sdk_installed_without_host_preserves_context_and_reads_fd(installed_sdk, tmp_path):
    python, _ = installed_sdk
    root = tmp_path.resolve()
    (root / "中文 文件.txt").write_text("分页读取🙂", encoding="utf-8")
    context = read_context(root)
    source = '''
import importlib.util, json, os, sys
from pathlib import Path
from my_agent_plugin_api.workspace_read_context import WorkspaceReadContext
from my_agent_plugin_api.nofollow_fs import open_readonly_file_beneath
assert importlib.util.find_spec("agent_py_agent") is None
context = WorkspaceReadContext.from_payload(json.load(sys.stdin))
path = context.cwd / "中文 文件.txt"
assert context.check(path).allowed
assert not context.check(context.cwd.parent / "outside").allowed
fd = open_readonly_file_beneath(Path(path.anchor), path.parts[1:])
try:
    content = os.read(fd, 100).decode("utf-8")
finally:
    os.close(fd)
print(json.dumps({"context": context.to_payload(), "content": content}))
'''
    result = subprocess.run([str(python), "-I", "-c", source], cwd=root,
                            input=json.dumps(context.to_payload()), capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"context": context.to_payload(), "content": "分页读取🙂"}


@pytest.mark.parametrize("kind", ["parent_link", "leaf_link", "hardlink", "fifo", "directory"])
def test_readonly_fd_rejects_nonregular_or_linked_path(tmp_path, kind):
    root = tmp_path.resolve()
    (root / "real").mkdir()
    (root / "real/file").write_bytes(b"original")
    leaf = root / "candidate"
    if kind == "parent_link":
        leaf.symlink_to(root / "real", target_is_directory=True)
        leaf /= "file"
    elif kind == "leaf_link":
        leaf.symlink_to(root / "real/file")
    elif kind == "hardlink":
        os.link(root / "real/file", leaf)
    elif kind == "fifo":
        os.mkfifo(leaf)
    else:
        leaf.mkdir()
    with pytest.raises(fs.NoFollowPathError):
        fs.open_readonly_file_beneath(Path(leaf.anchor), leaf.parts[1:])


def test_readonly_fd_cannot_fall_back_without_platform_capability(tmp_path, monkeypatch):
    monkeypatch.setattr(fs, "_supports_dir_fd", lambda: False)
    with pytest.raises(fs.NoFollowPathError):
        fs.open_readonly_file_beneath(tmp_path, ("missing",))


def test_publish_does_not_remove_preexisting_artifact(tmp_path):
    target = tmp_path / "artifact"
    target.write_bytes(b"before")
    with pytest.raises(FileExistsError):
        publish_artifact(target, b"after")
    assert target.read_bytes() == b"before"


def test_publish_cleans_created_artifact_on_close_failure(tmp_path, monkeypatch):
    target = tmp_path / "artifact"
    original = Path.open

    class FailedClose:
        def __enter__(self):
            self.stream = original(target, "xb")
            return self.stream

        def __exit__(self, *args):
            self.stream.close()
            raise OSError("close failed")

    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: FailedClose())
    with pytest.raises(OSError, match="close failed"):
        publish_artifact(target, b"partial")
    assert not target.exists()
