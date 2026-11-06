# LLM: 在 Ubuntu 20.04 整理原production wheel和native资源；飞书CLI后端缺失则拒绝出包，不打包工作树、运行数据或凭据。
# 归档时间归零，避免构建机和内网目标机时钟不同产生大量安装警告；不改变文件内容清单。
# 模块用途: 将已安装的自包含 Python 运行时整理成可校验、可重复升级的无业务源码离线包。
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import py_compile
import shutil
import tarfile
from pathlib import Path

from agent_py_agent.agent.settings.parameter_metadata import field_readers

SCHEMA = "my-agent.private-release.v1"
# 沙箱 helper 的发行架构：x86_64 必带，aarch64 有就带。
HELPER_ARCHES = ("x86_64", "aarch64")


# LLM: 构建阶段显式编译，文件名写相对路径；删除源码前要求对应字节码成功生成，不读取用户目录。
# 函数用途: 把原模块转换成 CPython 可直接加载的无源码模块，保留原 -m 和独立脚本语义。
def compile_module(path: Path, root: Path) -> None:
    destination = path.with_suffix(".pyc")
    py_compile.compile(str(path), cfile=str(destination), dfile=str(path.relative_to(root)), doraise=True)
    path.unlink()


# LLM: manifest 枚举所有发行文件及内部链接；原始源码仅记录构建摘要，不进入载荷。
# 函数用途: 生成安装器使用的逐文件 SHA256 清单，拒绝外部链接和缓存。
def inventory(root: Path) -> dict:
    (root / "release.json").unlink(missing_ok=True)
    members = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            if not path.resolve().is_relative_to(root.resolve()):
                raise ValueError(f"发行包链接逃逸: {relative}")
            members[relative] = {"link": os.readlink(path)}
        elif path.is_file():
            if "__pycache__" in path.parts or path.name.startswith("._"):
                raise ValueError(f"发行包包含缓存: {relative}")
            members[relative] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
    return members


# LLM: 仅重写发行副本中 pip 生成的 Python console-script 头，不改变 ELF、工具正文或宿主脚本。
# 函数用途: 去除构建目录的绝对解释器路径，使插件环境工具和命令行脚本可随版本搬到最终 prefix。
def relocate_console_scripts(runtime: Path) -> None:
    for path in (runtime / "bin").iterdir():
        if path.is_symlink() or not path.is_file():
            continue
        body = path.read_bytes()
        first, separator, remainder = body.partition(b"\n")
        if not first.startswith(b"#!") or b"python" not in first:
            continue
        header = b'''#!/bin/sh
''' + b"'''exec' \"$(dirname \"$(readlink -f \"$0\")\")/python3\" \"$0\" \"$@\"\n' '''\n"
        path.write_bytes(header + remainder if separator else header)


# LLM: 自解压头先核对完整 payload 哈希；临时目录自动清理，所有安装工作交同一版本的 installer.pyc。
# 函数用途: 生成仅依赖 bash/tar/gzip/coreutils 的一条命令离线安装文件。
def self_extracting_header(digest: str) -> bytes:
    header = '''#!/usr/bin/env bash
set -euo pipefail
umask 077
for command in tar gzip sha256sum tail mktemp; do
  command -v "$command" >/dev/null || { printf '缺少系统命令：%s\\n' "$command" >&2; exit 1; }
done
[ "$(uname -m)" = x86_64 ] || { printf '本包适用于 x86_64 服务器\\n' >&2; exit 1; }
temporary="$(mktemp -d "${TMPDIR:-/tmp}/my-agent-install.XXXXXXXX")"
trap 'rm -rf -- "$temporary"' EXIT
tail -n +@LINE@ "$0" > "$temporary/payload.tar.gz"
actual="$(sha256sum "$temporary/payload.tar.gz")"
[ "${actual%% *}" = "@DIGEST@" ] || { printf '安装包校验失败，请重新传输\\n' >&2; exit 1; }
tar -xzf "$temporary/payload.tar.gz" --no-same-owner -C "$temporary"
export PYTHONDONTWRITEBYTECODE=1
unset PYTHONHOME PYTHONPATH
"$temporary/bundle/runtime/bin/python3" -I -B "$temporary/bundle/installer.pyc" "$@"
exit $?
__MY_AGENT_PAYLOAD__
'''
    return header.replace("@LINE@", str(header.count("\n") + 1)).replace("@DIGEST@", digest).encode()


# LLM: 只读发行副本与已准备的 native 目录，不访问用户目录；找不到或文件无效时返回 None，由调用方按必选/可选处理。
# 函数用途: 在候选位置里找到沙箱 helper 二进制（发行机 native 目录优先于包内已有文件）。
def find_helper_source(root: Path, package: Path, arch: str) -> Path | None:
    name = f"my-agent-sandbox-helper.linux-{arch}"
    for base in (root / "native", package / "vendor/bin"):
        candidate = base / name
        if candidate.is_file():
            return candidate
    return None


# LLM: 校验ELF64小端机器号和普通文件身份；有sha旁文件时须与字节一致，不能把损坏、链接或错架构文件打进包。
# 函数用途: 判断某个沙箱 helper 是否可以作为发行内容。
def helper_is_valid(path: Path | None, arch: str) -> bool:
    if path is None or path.is_symlink() or not path.is_file():
        return False
    data = path.read_bytes()
    if (len(data) < 20 or data[:6] != b"\x7fELF\x02\x01"
            or int.from_bytes(data[18:20], "little") != {"x86_64": 62, "aarch64": 183}[arch]):
        return False
    sidecar = Path(str(path) + ".sha256")
    if sidecar.is_file():
        expected = sidecar.read_text(encoding="utf-8", errors="replace").split()
        if not expected or expected[0] != hashlib.sha256(data).hexdigest():
            return False
    return True


# LLM: 复制 helper 与校验和旁文件并设置可执行位；x86_64 缺失或无效即拒绝出包，aarch64 有就带上。
# 函数用途: 把沙箱 helper 装进发行包的 vendor/bin。
def copy_sandbox_helpers(root: Path, package: Path) -> None:
    target_dir = package / "vendor/bin"
    target_dir.mkdir(parents=True, exist_ok=True)
    for arch in HELPER_ARCHES:
        source = find_helper_source(root, package, arch)
        if not helper_is_valid(source, arch):
            if arch == "x86_64":
                raise ValueError("发行包缺少有效的沙箱 helper: vendor/bin/my-agent-sandbox-helper.linux-x86_64")
            continue
        target = target_dir / source.name
        if source.resolve() != target.resolve():
            shutil.copyfile(source, target)
        target.chmod(0o755)
        sidecar = Path(str(source) + ".sha256")
        if sidecar.is_file() and sidecar.resolve() != Path(str(target) + ".sha256").resolve():
            shutil.copyfile(sidecar, Path(str(target) + ".sha256"))


# LLM: main在编译和归档前调用；只改发行副本，native飞书后端优先，保留wheel资源，缺飞书后端/资源或有效x86沙箱helper硬拒绝。
# 函数用途: 复制bwrap、飞书CLI后端及lark资源，统一后端执行权限，避免发布残包。
def copy_native_assets(root: Path, package: Path) -> None:
    # 保持原vendor发现入口，子进程即使不继承PATH也能找到Ubuntu兼容版本。
    shutil.copyfile(root / "native/bwrap", package / "vendor/bin/bwrap.linux-x86_64")
    (package / "vendor/bin/bwrap.linux-x86_64").chmod(0o755)
    shutil.copytree(root / "native/lib", package / "vendor/bin/lib", dirs_exist_ok=True)
    shutil.copyfile(root / "native/PROVENANCE.txt", package / "vendor/bubblewrap/UBUNTU_BUILD.txt")
    backend = package / "vendor/bin/feishu-cli-host.linux-x86_64"
    if (root / "native/feishu-cli-host.linux-x86_64").is_file():
        shutil.copyfile(root / "native/feishu-cli-host.linux-x86_64", backend)
    if backend.is_file():
        backend.chmod(0o755)
    lark_dir = package / "vendor/lark_cli"
    if (root / "native/lark_cli").is_dir():
        shutil.copytree(root / "native/lark_cli", lark_dir, dirs_exist_ok=True)
    if backend.is_symlink() or not backend.is_file() or backend.stat().st_size <= 0:
        raise ValueError("发行包缺少随包飞书 CLI 后端: vendor/bin/feishu-cli-host.linux-x86_64")
    if lark_dir.is_symlink() or not lark_dir.is_dir() or not any(lark_dir.iterdir()):
        raise ValueError("发行包缺少随包飞书 CLI 资源: vendor/lark_cli")
    copy_sandbox_helpers(root, package)


# LLM: 只接收一次冻结后的payload；先核原生后端完整，再编译及归档，未来重打必须使用新version。
# 函数用途: 校验保留运行资源，编译业务模块，写构建清单和离线安装器。
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--helpers", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--base-commit", required=True)
    parser.add_argument("--source-sha256", required=True)
    args = parser.parse_args()
    root = args.bundle.resolve()
    package = root / "runtime/lib/python3.11/site-packages/agent_py_agent"
    relocate_console_scripts(root / "runtime")
    copy_native_assets(root, package)
    snapshot = {"schema": "my-agent.parameter-readers.v1", "readers": field_readers()}
    (package / "config/parameter_readers.json").write_text(json.dumps(snapshot, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    count = 0
    for path in sorted(package.rglob("*.py")):
        if "skills" in path.relative_to(package).parts:
            continue  # 开放的插件示例模板是资源，不是宿主业务源码。
        compile_module(path, root)
        count += 1
    if any("skills" not in path.relative_to(package).parts for path in package.rglob("*.py")):
        raise ValueError("发行包仍含宿主业务源码")
    for helper in ("installer", "configure"):
        destination = root / f"{helper}.py"
        shutil.copyfile(args.helpers / f"{helper}.py", destination)
        compile_module(destination, root)
    for cache in sorted(root.rglob("__pycache__"), reverse=True):
        shutil.rmtree(cache)
    # setuptools 原 RECORD 指向构建时源码，已转换的模块清单以 release.json 为发行权威。
    manifest = {
        "schema": SCHEMA, "version": args.version, "architecture": "x86_64",
        "ubuntu": ["20.04", "22.04", "24.04"], "python": "3.11.15",
        "base_commit": args.base_commit, "source_sha256": args.source_sha256,
        "format": "portable-cpython-sourceless", "compiled_modules": count,
        "protocols": {"offline_update": "drain.v1"},
        "dependencies": sorted({f"{item.metadata['Name']}=={item.version}" for item in importlib.metadata.distributions()}),
        "source_visibility": "宿主业务模块仅含字节码，可逆向；配置、提示词、公开模板、第三方源码及许可证保留。",
        "files": inventory(root),
    }
    (root / "release.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.output.mkdir(parents=True, exist_ok=True)
    archive = args.output / f"my-agent-{args.version}-linux-x86_64.tar.gz"
    with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as stream:
        stream.add(root, arcname="bundle", filter=portable_archive_member)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    installer = archive.with_suffix("").with_suffix(".run")
    with installer.open("wb") as stream:
        stream.write(self_extracting_header(digest))
        with archive.open("rb") as payload:
            shutil.copyfileobj(payload, stream)
    installer.chmod(0o755)
    result = {"file": installer.name, "sha256": hashlib.sha256(installer.read_bytes()).hexdigest(), "bytes": installer.stat().st_size, "compiled_modules": count}
    (args.output / "build-result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    return 0


# LLM: tar 成员时间只属于发行元数据，不是版本、权限或运行状态；内容 SHA256 不受修改。
# 函数用途: 清除构建机时间戳，避免时钟较早的目标机在解包时逐文件刷出未来时间警告。
def portable_archive_member(member: tarfile.TarInfo) -> tarfile.TarInfo:
    member.mtime = 0
    return member


if __name__ == "__main__":
    raise SystemExit(main())
