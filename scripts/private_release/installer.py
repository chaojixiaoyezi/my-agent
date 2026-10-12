# LLM: 私有离线发行的安装器；只管理显式 prefix 下的版本、入口和初始化数据，升级不覆盖数据。
# 模块用途: 用发行包自带 Python 完成校验、首次安装和原子版本切换，不依赖登录 HOME 或网络。
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import sys
from pathlib import Path

SCHEMA = "my-agent.private-release.v1"
MARKER = ".my-agent-private-install.json"


# LLM: 默认工作目录只由产品 owner resolver 决定；安装器不得另建 owner home 外的工作区或扩大权限。
# 函数用途: 返回该安装下本地主用户的默认工作目录，保持 WorkspaceOnly 和原 TUI 首装语义。
def workspace_path(prefix: Path) -> Path:
    from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home

    return resolve_owner_home(prefix / "data").home_dir


# LLM: 校验所有发行成员及链接目标；manifest 是构建记录，不读取任何用户运行目录。
# 函数用途: 安装前检查完整性、架构与发行包格式，坏包不能切换 current。
def verify(root: Path) -> dict:
    manifest = json.loads((root / "release.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != SCHEMA or not re.fullmatch(r"[A-Za-z0-9_.-]+", manifest["version"]):
        raise ValueError("不支持的发行包格式或版本")
    if os.uname().machine != manifest["architecture"]:
        raise ValueError("安装包 CPU 架构与本机不匹配")
    actual = {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file() or path.is_symlink()}
    if actual != set(manifest["files"]) | {"release.json"}:
        raise ValueError("发行成员清单不完整或包含额外文件")
    for relative, expected in manifest["files"].items():
        path = root / relative
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"发行成员越界: {relative}")
        if "link" in expected:
            if not path.is_symlink() or os.readlink(path) != expected["link"]:
                raise ValueError(f"发行链接损坏: {relative}")
        elif not path.is_file() or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected["sha256"]:
            raise ValueError(f"发行文件校验失败: {relative}")
    return manifest


# LLM: 只查看本安装目录所属进程的 exe/cwd 身份；不读取命令行、任务正文或其它应用状态。
# 函数用途: 阻止活动进程期间切换版本，避免新 TUI 连接旧 Gateway；不杀进程。
def active_processes(prefix: Path) -> list[int]:
    running = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            executable = (entry / "exe").resolve(strict=True)
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if executable.is_relative_to(prefix / "releases"):
            running.append(int(entry.name))
    return sorted(running)


# LLM: 启动入口固定解析安装记录；数据与配置是持久路径，-B 显式禁止隔离解释器在不可变版本写缓存。
# 函数用途: 生成可在任意工作目录调用的启动器，用户的 HOME 不参与后续升级目标选择。
def launcher_text(prefix: Path) -> str:
    quoted = shlex.quote(str(prefix))
    return f'''#!/usr/bin/env bash
set -euo pipefail
prefix={quoted}
release="$(readlink -f "$prefix/current")"
export MY_AGENT_HOME="$prefix/data"
export MY_AGENT_CONFIG="$prefix/data/config/agent_config.yaml"
export TMPDIR="$prefix/data/tmp"
export XDG_CACHE_HOME="$prefix/data/cache"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONUTF8=1
export PATH="$release/runtime/bin:$PATH:$release/native"
if [ -z "${{SSL_CERT_FILE:-}}" ]; then
  if [ -f /etc/ssl/certs/ca-certificates.crt ]; then export SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
  else export SSL_CERT_FILE="$release/native/ca-certificates.crt"; fi
fi
unset PYTHONHOME PYTHONPATH
if [ "${{1:-}}" = "--release-info" ]; then
  exec "$release/runtime/bin/python3" -I -B -X utf8 -c 'import json,sys; d=json.load(open(sys.argv[1])); d.pop("files"); print(json.dumps(d,ensure_ascii=False,indent=2))' "$release/release.json"
fi
if [ "${{1:-}}" = "--version" ]; then
  exec "$release/runtime/bin/python3" -I -B -X utf8 -c 'import json,sys; print("my-agent " + json.load(open(sys.argv[1]))["version"])' "$release/release.json"
fi
if [ "${{1:-}}" = "--configure-model" ]; then
  exec "$release/runtime/bin/python3" -I -B -X utf8 "$release/configure.pyc" "$MY_AGENT_CONFIG"
fi
exec "$release/runtime/bin/python3" -I -B -X utf8 -m agent_py_agent "$@"
'''


# LLM: 首装保留 canonical YAML 的空 workspace_root，由原 owner resolver 选目录；更新不覆盖持久数据。
# 函数用途: 在独立数据目录初始化空白配置与工作区，不带开发者账号或测试模型凭据。
def initialize_data(prefix: Path, release: Path) -> None:
    config = prefix / "data" / "config" / "agent_config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    (prefix / "data/tmp").mkdir(exist_ok=True, mode=0o700)
    (prefix / "data/cache").mkdir(exist_ok=True, mode=0o700)
    workspace_path(prefix).mkdir(parents=True, exist_ok=True)
    if config.exists():
        return
    package = release / "runtime" / "lib" / "python3.11" / "site-packages" / "agent_py_agent"
    content = (package / "config" / "agent_config.yaml").read_text(encoding="utf-8")
    # 空工作区配置保留原 owner-home 权威；环境绑定 data 后无需另建外部目录或放宽权限。
    content = re.sub(r'^gateway_port:.*$', 'gateway_port: 18420', content, flags=re.M)
    with config.open("x", encoding="utf-8") as stream:
        stream.write(content)
    config.chmod(0o600)


# LLM: 修改范围限于经过标记的安装目录；版本冲突、校验失败及活动进程均在切换前失败。
# 函数用途: 安装不可变版本，保留旧版本及用户数据，最后原子切换 current。
def install(bundle: Path, prefix: Path) -> dict:
    manifest = verify(bundle)
    prefix = prefix.expanduser().resolve()
    if any(character in str(prefix) for character in "\n\r\0"):
        raise ValueError("安装路径不能包含控制字符")
    prefix.mkdir(parents=True, exist_ok=True)
    if prefix.stat().st_uid != os.getuid():
        raise ValueError("安装目录属于另一个账户，请使用目录所属账户安装和更新")
    marker = prefix / MARKER
    if not marker.exists() and any(path.name != ".install.lock" for path in prefix.iterdir()):
        raise ValueError("目标目录非空且不属于本安装器，请使用空目录")
    with (prefix / ".install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if pids := active_processes(prefix):
            raise ValueError(f"请先退出本安装的 TUI 并停止 Gateway 后重试；活动 PID: {pids}")
        record = {"schema": SCHEMA, "prefix": str(prefix), "data": str(prefix / "data"), "workspace": str(workspace_path(prefix)), "owner_uid": os.getuid()}
        if marker.exists() and json.loads(marker.read_text(encoding="utf-8")) != record:
            raise ValueError("安装记录与目标路径不一致，请不要直接搬移运行中的安装目录")
        marker.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        releases = prefix / "releases"
        releases.mkdir(exist_ok=True)
        target = releases / manifest["version"]
        if target.exists():
            if (target / "release.json").read_bytes() != (bundle / "release.json").read_bytes():
                raise ValueError("同版本已有不同内容，必须使用新的发行版本号")
            verify(target)
        else:
            staging = releases / f".staging-{os.getpid()}"
            try:
                shutil.copytree(bundle, staging, symlinks=True)
                verify(staging)
                staging.rename(target)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        initialize_data(prefix, target)
        (prefix / "bin").mkdir(exist_ok=True)
        launcher = prefix / "bin" / "my-agent-preview"
        temporary = launcher.with_name(f".launcher-{os.getpid()}")
        temporary.write_text(launcher_text(prefix), encoding="utf-8")
        temporary.chmod(0o755)
        os.replace(temporary, launcher)
        current = prefix / f".current-{os.getpid()}"
        current.symlink_to(Path("releases") / manifest["version"])
        os.replace(current, prefix / "current")
    return {"version": manifest["version"], **record}


# LLM: CLI 是唯一安装入口；仅打印原 gateway start 与 TUI 命令，不在安装时启动后台或联网。
# 函数用途: 执行离线安装并打印启动、模型配置与更新命令。
def main() -> int:
    parser = argparse.ArgumentParser(description="my-agent 临时离线安装 / 更新")
    parser.add_argument("--prefix", type=Path, default=Path("/app/my-agent-preview"))
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    bundle = Path(__file__).resolve().parent
    try:
        if args.verify_only:
            print(json.dumps({"verified": verify(bundle)["version"]}, ensure_ascii=False))
            return 0
        result = install(bundle, args.prefix)
    except (OSError, ValueError, KeyError) as error:
        print(f"安装失败：{error}", file=sys.stderr)
        return 1
    command = shlex.quote(str(Path(result["prefix"]) / "bin" / "my-agent-preview"))
    print(f"安装完成：{result['version']}\n程序及更新目录：{result['prefix']}\n数据目录：{result['data']}")
    print(f"首次配置模型：{command} --configure-model\n启动后台：{command} gateway start --ready-timeout 30\n进入 TUI：{command}")
    print(f"更新：退出 TUI、执行 {command} gateway stop，然后 bash 新包.run --prefix {shlex.quote(result['prefix'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
