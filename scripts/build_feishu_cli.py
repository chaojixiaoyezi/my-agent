# LLM: 固定官方 tag/提交与原扩展 API，构建只写指定发行副本；不下载运行期程序或修改宿主登录。
# 模块用途: 将受控飞书 CLI 后端编译为随包原生文件并保存许可与来源。
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

UPSTREAM_COMMIT = "ac81f5056ab88f37d581d25916f3dab85abfca76"
UPSTREAM_VERSION = "v1.0.98"
PLATFORMS = {"linux-x86_64": ("linux", "amd64"), "darwin-arm64": ("darwin", "arm64")}


# LLM: 检查源码身份先于构建；传入变更或非指定提交不得伪造官方版本来源。
# 函数用途: 核实调用方准备的官方源码 checkout。
def verify_source(source: Path) -> None:
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=source, text=True).strip()
    if commit != UPSTREAM_COMMIT or dirty:
        raise ValueError("飞书 CLI 源码不是固定的干净官方版本。")


# LLM: 源副本只做本次构建；wrapper使用官方公开扩展，不修改上游产品代码。
# 函数用途: 复制准确源码并加入本项目宿主入口。
def prepare_source(source: Path, temporary: Path) -> Path:
    staging = temporary / "source"
    shutil.copytree(source, staging, ignore=shutil.ignore_patterns(".git", "node_modules"))
    wrapper = staging / "cmd" / "my-agent-host"
    wrapper.mkdir()
    shutil.copyfile(Path(__file__).parent / "feishu_cli_host" / "main.go", wrapper / "main.go")
    shutil.copyfile(Path(__file__).parent / "feishu_cli_host" / "main_test.go", wrapper / "main_test.go")
    return staging


# LLM: Go 构建环境只改变本次子进程；输出文件身份和hash属于发行元数据。
# 函数用途: 编译一个平台的受控后端，不要求安装目标机有 Go。
def build_platform(staging: Path, output: Path, platform: str) -> dict:
    system, arch = PLATFORMS[platform]
    env = {**os.environ, "GOOS": system, "GOARCH": arch, "CGO_ENABLED": "0"}
    target = output / "bin" / f"feishu-cli-host.{platform}"
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["go", "build", "-trimpath", "-ldflags=-s -w", "-o", str(target), "./cmd/my-agent-host"],
                   cwd=staging, env=env, check=True)
    target.chmod(0o755)
    return {"file": target.name, "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "bytes": target.stat().st_size}


# LLM: 编译依赖许可从实际 module cache 收集；不读取私人配置或向发行物写个人路径。
# 函数用途: 保存上游与编译依赖的许可文本。
def write_licenses(staging: Path, output: Path) -> None:
    modules = subprocess.check_output(["go", "list", "-m", "-json", "all"], cwd=staging, text=True)
    decoder = json.JSONDecoder()
    remaining = modules.strip()
    texts = []
    while remaining:
        module, end = decoder.raw_decode(remaining)
        remaining = remaining[end:].lstrip()
        directory = Path(module.get("Dir") or staging)
        for name in ("LICENSE", "LICENSE.txt", "LICENSE.md", "COPYING"):
            license_path = directory / name
            if license_path.is_file():
                texts.append(f"{module['Path']} {module.get('Version', UPSTREAM_VERSION)}\n" + license_path.read_text(errors="replace"))
                break
    (output / "lark_cli" / "LICENSES.txt").write_text("\n\n".join(texts), encoding="utf-8")


# LLM: manifest只证明构建身份，不参与用户资源授权；后端调用仍由ToolExecutor与云权限负责。
# 函数用途: 组织多平台构建及来源记录。
def build(source: Path, output: Path, platforms: list[str]) -> dict:
    verify_source(source)
    output = output.resolve()
    (output / "lark_cli").mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="my-agent-feishu-cli-build-") as name:
        staging = prepare_source(source, Path(name))
        subprocess.run(["go", "test", "./cmd/my-agent-host"], cwd=staging, check=True)
        records = [build_platform(staging, output, platform) for platform in platforms]
        write_licenses(staging, output)
    manifest = {"schema": "my-agent.feishu-cli-build.v1", "upstream_version": UPSTREAM_VERSION,
                "upstream_commit": UPSTREAM_COMMIT, "transport_protocol": "my-agent.feishu-cli.v1", "files": records,
                "wrapper_sources": {name: hashlib.sha256((Path(__file__).parent / "feishu_cli_host" / name).read_bytes()).hexdigest()
                                    for name in ("main.go", "main_test.go")},
                "go_sum_sha256": hashlib.sha256((source / "go.sum").read_bytes()).hexdigest(),
                "go_version": subprocess.check_output(["go", "version"], text=True).strip()}
    (output / "lark_cli" / "PROVENANCE.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


# LLM: 参数必须明确源码与发行副本，不操作用户安装目录。
# 函数用途: 为 wheel/Linux 发行和本机运行时构建原生后端。
def main() -> None:
    parser = argparse.ArgumentParser(description="构建随包受控飞书 CLI")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--platform", choices=tuple(PLATFORMS), action="append")
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.output, args.platform or list(PLATFORMS)), ensure_ascii=False))


if __name__ == "__main__":
    main()
