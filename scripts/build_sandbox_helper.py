# LLM: 只在 Linux 上编译仓库自带的沙箱 helper 源码，产物与校验和落在仓库内 vendor/bin；
# 不下载依赖、不改宿主环境、不触碰用户目录；非 Linux 明确说明不构建而不是假装成功。
# 模块用途: 把 scripts/sandbox_helper/sandbox_helper.c 编译成随包原生 helper 及其 sha256。
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
from pathlib import Path

# 编译加固选项：与发行说明一致；静态 libc 缺失时只回退链接方式，不放宽这些选项。
COMPILE_FLAGS: tuple[str, ...] = ("-O2", "-Wall", "-Wextra", "-fstack-protector-strong", "-D_FORTIFY_SOURCE=2")
# 机器别名归一：Linux 上 uname -m 与发行目录名可能不同，统一成 helper 文件名里的架构名。
ARCH_ALIASES: dict[str, str] = {
    "x86_64": "x86_64", "amd64": "x86_64", "x64": "x86_64",
    "aarch64": "aarch64", "arm64": "aarch64",
}
# helper 文件名模板：运行期按 platform.machine() 拼同一个名字。
HELPER_NAME = "my-agent-sandbox-helper.linux-{arch}"


class UnsupportedPlatform(RuntimeError):
    """当前平台不提供 Linux 语义（helper 用到 Landlock 与 /proc）。"""


# LLM: 别名表是唯一映射来源，未知架构直接报错而不是猜测；调用方据此给出明确提示。
# 函数用途: 把机器名归一成 helper 文件名使用的标准架构名。
def normalize_arch(name: str) -> str:
    key = str(name).strip().lower()
    if key not in ARCH_ALIASES:
        raise ValueError(f"不支持的架构: {name}（仅支持 x86_64/amd64/aarch64/arm64）")
    return ARCH_ALIASES[key]


# LLM: 只读当前进程信息，不写任何状态；非 Linux 抛 UnsupportedPlatform 让调用方明确跳过。
# 函数用途: 给出本机标准架构名，供默认编译目标使用。
def detect_arch() -> str:
    return normalize_arch(platform.machine())


# LLM: 编译只写目标文件；静态失败才回退动态，失败或ELF架构不符时清掉半成品，不允许仅改文件名伪装交叉编译。
# 函数用途: 用指定编译器产出 helper 二进制，返回编译调用与链接方式。
def compile_helper(source: Path, target: Path, compiler: str, arch: str) -> dict:
    if not source.is_file():
        raise FileNotFoundError(f"缺少 helper 源码: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    base = [compiler, *COMPILE_FLAGS]
    attempts = []
    for link in ("static", "dynamic"):
        argv = [*base, *(["-static"] if link == "static" else []), "-o", str(target), str(source)]
        completed = subprocess.run(argv, capture_output=True, text=True, check=False)
        attempts.append({"link": link, "argv": argv, "returncode": completed.returncode,
                         "stderr": completed.stderr[-2000:]})
        if completed.returncode == 0:
            data = target.read_bytes()
            machine = {"x86_64": 62, "aarch64": 183}[arch]
            if len(data) < 20 or data[:6] != b"\x7fELF\x02\x01" or int.from_bytes(data[18:20], "little") != machine:
                target.unlink(missing_ok=True)
                Path(str(target) + ".sha256").unlink(missing_ok=True)
                raise ValueError(f"helper ELF 架构与目标 {arch} 不一致")
            target.chmod(0o755)
            return {"link": link, "attempts": attempts}
        target.unlink(missing_ok=True)  # 失败不留半成品，避免下次误判为已构建。
    detail = attempts[-1]["stderr"].strip().splitlines()[-1:] or ["编译失败"]
    raise RuntimeError(f"编译 {arch} helper 失败（静态与动态都失败）: {detail[0]}")


# LLM: 校验和文件只写目标文件同目录的同名 .sha256，内容是本机算出的字节摘要。
# 函数用途: 为已构建的 helper 生成可核对的 sha256 旁文件。
def write_checksum(target: Path) -> str:
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    Path(str(target) + ".sha256").write_text(f"{digest}  {target.name}\n", encoding="utf-8")
    return digest


# LLM: 非 Linux 直接说明不构建；Linux 才编译一次目标架构并落盘二进制与 sha256，不修改其它文件。
# 函数用途: 构建一个架构的 helper，返回可写入构建记录的元数据。
def build(source: Path, output: Path, compiler: str = "cc", arch: str | None = None) -> dict:
    if platform.system() != "Linux":
        raise UnsupportedPlatform(
            f"helper 只在 Linux 构建（当前 {platform.system()}），其它平台不构建也不产出文件")
    target_arch = normalize_arch(arch) if arch else detect_arch()
    output = Path(output)
    target = output / HELPER_NAME.format(arch=target_arch)
    compiled = compile_helper(Path(source), target, compiler, target_arch)
    digest = write_checksum(target)
    return {"file": target.name, "path": str(target), "arch": target_arch, "compiler": compiler,
            "link": compiled["link"], "sha256": digest, "bytes": target.stat().st_size}


# LLM: 参数只决定源码与产物位置，不安装依赖也不联网；非 Linux 以 skipped 结果说明原因。
# 函数用途: 为 wheel/Linux 发行和本机运行时构建原生沙箱 helper。
def main() -> int:
    repository = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="构建原生沙箱 helper")
    parser.add_argument("--source", type=Path, default=repository / "scripts/sandbox_helper/sandbox_helper.c")
    parser.add_argument("--output", type=Path, default=repository / "agent_py_agent/vendor/bin")
    parser.add_argument("--arch", help="目标架构别名，默认本机架构")
    parser.add_argument("--cc", default="cc", help="C 编译器，默认 cc")
    args = parser.parse_args()
    try:
        result = build(args.source, args.output, compiler=args.cc, arch=args.arch)
    except UnsupportedPlatform as skipped:
        print(json.dumps({"status": "skipped", "reason": str(skipped),
                          "platform": platform.system()}, ensure_ascii=False))
        return 0
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
