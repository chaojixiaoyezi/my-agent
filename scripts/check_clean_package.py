#!/usr/bin/env python3

from __future__ import annotations

"""检查工作树和发布制品，阻止缓存、未跟踪文件和运行期数据进入交付物。

目录模式读取 Git 的 tracked/untracked 事实，并汇总大体积运行目录；制品模式直接读取
tar.gz/zip/wheel 成员，不依赖 Git。所有硬失败都有稳定 reason code，便于本地 gate 和 CI 消费。
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

DIRTY_PREFIXES = ("._",)
DIRTY_NAMES = {
    ".DS_Store",
    ".AppleDouble",
    ".LSOverride",
}
DIRTY_PARTS = {
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "htmlcov",
}
DIRTY_SUFFIXES = {".pyc", ".pyo"}
RUNTIME_ROOTS = (
    "data",
    "agent_py_agent/data",
    "memory",
    "agent_py_agent/memory",
    "memory_archive",
    "agent_py_agent/memory_archive",
    "logs",
    "outputs",
    "live-agent-runs",
    "validation/real_runs",
)
DEFAULT_RUNTIME_WARNING_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_ARTIFACT_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_MEMBER_BYTES = 50 * 1024 * 1024
REPORT_LIMIT = 20


# LLM: Finding.code/severity 是 package gate 的机器协议；展示文案可以变，调用方不得解析 detail。
# 类用途: 保存一次可排序、可 JSON 输出的工作树或制品检查发现。
@dataclass(frozen=True)
class PackageFinding:
    severity: str
    code: str
    path: str
    size_bytes: int = 0
    detail: str = ""

    # LLM: JSON 字段供 CI 和后续 support bundle 消费，保持稳定且不读取文件正文。
    # 函数用途: 把检查发现转换成普通字典。
    def to_dict(self) -> dict[str, object]:
        return asdict(self)


# LLM: 脏文件分类必须只依赖路径事实，禁止读取潜在用户数据内容。
# 函数用途: 判断路径中是否出现缓存、macOS 元数据或 Python 字节码。
def _is_dirty(name: str) -> bool:
    parts = name.replace("\\", "/").split("/")
    for part in parts:
        if part in DIRTY_NAMES or part in DIRTY_PARTS:
            return True
        if any(part.startswith(prefix) for prefix in DIRTY_PREFIXES):
            return True
        if any(part.endswith(suffix) for suffix in DIRTY_SUFFIXES):
            return True
    return False


# LLM: Git 是工作树 tracked/untracked 的唯一事实源；返回 None 表示目标不是 Git 仓库。
# 函数用途: 执行 git ls-files 并安全解析 NUL 分隔路径，兼容文件名中的空格和换行。
def _git_paths(root: Path, arguments: Sequence[str]) -> list[str] | None:
    try:
        result = subprocess.run(
            ["git", *arguments, "-z"],
            cwd=root,
            check=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return [os.fsdecode(item) for item in result.stdout.split(b"\0") if item]


# LLM: 统计只能在给定根目录内且不得跟随 symlink，避免 package gate 误扫仓库外数据。
# 函数用途: 计算文件或目录占用的普通文件字节数。
def _path_size(path: Path) -> int:
    try:
        if path.is_symlink():
            return 0
        if path.is_file():
            return path.stat().st_size
    except OSError:
        return 0
    measured = _du_size(path)
    return measured if measured is not None else _walk_size(path)


# LLM: 大运行目录优先交给系统 du 做有界汇总；失败返回 None 交标准库 fallback。
# 函数用途: 快速取得目录磁盘占用字节数，避免 Python 对数万 inode 逐个多轮 stat。
def _du_size(path: Path) -> int | None:
    du = shutil.which("du")
    if not du or not path.is_dir():
        return None
    try:
        result = subprocess.run(
            [du, "-sk", str(path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return int(result.stdout.split()[0]) * 1024
    except (OSError, ValueError, IndexError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


# LLM: 这是无 du 平台的保守 fallback，不跟随 symlink，读取错误只跳过对应文件。
# 函数用途: 用标准库遍历目录并合计普通文件大小。
def _walk_size(path: Path) -> int:
    try:
        children = path.rglob("*") if path.is_dir() else ()
        return sum(_regular_file_size(child) for child in children)
    except OSError:
        return 0


# LLM: fallback 遍历的单文件错误在这里收口，且明确不跟随 symlink。
# 函数用途: 返回一个普通文件的大小；目录、链接或读取失败都按零处理。
def _regular_file_size(path: Path) -> int:
    try:
        return path.stat().st_size if not path.is_symlink() and path.is_file() else 0
    except OSError:
        return 0


# LLM: 运行目录只做可见性 warning；是否进入制品由 artifact gate 硬判。未忽略 untracked
#   文件仍在 worktree gate 硬失败，不能靠把目录列进 RUNTIME_ROOTS 绕过。
# 函数用途: 汇总仓库中较大的运行期目录，让本地状态不再被“clean”结论隐藏。
def _runtime_warnings(root: Path, *, minimum_bytes: int) -> list[PackageFinding]:
    findings: list[PackageFinding] = []
    for relative in RUNTIME_ROOTS:
        path = root / relative
        if not path.exists():
            continue
        size = _path_size(path)
        if size < minimum_bytes:
            continue
        findings.append(
            PackageFinding(
                "warning",
                "RUNTIME_DATA_PRESENT",
                relative,
                size,
                "运行期数据不会因 .gitignore 自动变成发布安全；应确认不在实际制品和 Docker context 中",
            )
        )
    return findings


# LLM: 目录 gate 在 Git 仓库必须同时覆盖 tracked 脏文件和所有未忽略 untracked 文件；
#   这是远端提交前 package cleanliness 的当前唯一权威实现。
# 函数用途: 检查源码工作树并返回结构化错误与大运行目录警告。
def check_directory_findings(
    root: Path,
    *,
    runtime_warning_bytes: int = DEFAULT_RUNTIME_WARNING_BYTES,
) -> list[PackageFinding]:
    root = root.resolve()
    tracked = _git_paths(root, ("ls-files",))
    if tracked is None:
        findings = _non_git_findings(root)
    else:
        findings = _git_worktree_findings(root, tracked)
    return findings + _runtime_warnings(root, minimum_bytes=runtime_warning_bytes)


# LLM: 非 Git 目录无法区分源码和本地状态，只执行完整 dirty path 扫描。
# 函数用途: 检查普通目录中的缓存、元数据和 Python 字节码。
def _non_git_findings(root: Path) -> list[PackageFinding]:
    findings: list[PackageFinding] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = str(path.relative_to(root))
        if _is_dirty(relative):
            findings.append(PackageFinding("error", "DIRTY_ARTIFACT", relative, _path_size(path)))
    return findings


# LLM: Git 工作树同时检查 tracked dirty path 和未忽略 untracked，二者不能互相替代。
# 函数用途: 根据 Git 文件事实生成发布阻塞项。
def _git_worktree_findings(root: Path, tracked: Sequence[str]) -> list[PackageFinding]:
    findings = [
        PackageFinding("error", "DIRTY_TRACKED_FILE", relative, _path_size(root / relative))
        for relative in tracked
        if _is_dirty(relative)
    ]
    untracked = _git_paths(root, ("ls-files", "--others", "--exclude-standard")) or []
    findings.extend(
        PackageFinding(
            "error",
            "UNTRACKED_FILE",
            relative,
            _path_size(root / relative),
            "未忽略文件不属于可复现源码事实，可能被目录打包或 Docker context 带入",
        )
        for relative in untracked
    )
    return findings


# LLM: 保留旧 Python 调用接口，但语义升级为返回所有 error 路径；warning 不作为 offender。
# 函数用途: 兼容原有调用方，返回使目录检查失败的路径列表。
def check_directory(root: Path) -> list[str]:
    return [item.path for item in check_directory_findings(root) if item.severity == "error"]


# LLM: archive path 必须防绝对路径和 .. 穿越；它是制品可安全解包的客观硬门。
# 函数用途: 判断归档成员名是否可能逃出解包目录。
def _unsafe_archive_path(name: str) -> bool:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    return path.is_absolute() or ".." in path.parts


# LLM: 发布归档常带一个版本根目录，因此同时检查原路径和剥掉首段后的路径。
# 函数用途: 判断归档成员是否属于运行状态、日志或真实运行产物目录。
def _is_runtime_member(name: str) -> bool:
    normalized = name.replace("\\", "/").strip("./")
    candidates = [normalized]
    parts = normalized.split("/")
    if len(parts) > 1:
        candidates.append("/".join(parts[1:]))
    return any(
        candidate == root or candidate.startswith(root + "/")
        for candidate in candidates
        for root in RUNTIME_ROOTS
    )


# LLM: tar/zip 共用同一成员合同，避免不同发布格式出现安全语义漂移。
# 函数用途: 根据成员路径、大小和制品预算生成错误发现。
def _member_findings(
    members: Iterable[tuple[str, int]],
    *,
    max_artifact_bytes: int,
    max_member_bytes: int,
) -> list[PackageFinding]:
    findings: list[PackageFinding] = []
    total = 0
    for name, size in members:
        total += max(0, size)
        if _unsafe_archive_path(name):
            findings.append(PackageFinding("error", "UNSAFE_ARCHIVE_PATH", name, size))
        if _is_dirty(name):
            findings.append(PackageFinding("error", "DIRTY_ARTIFACT", name, size))
        if _is_runtime_member(name):
            findings.append(PackageFinding("error", "RUNTIME_DATA_IN_ARTIFACT", name, size))
        if size > max_member_bytes:
            findings.append(
                PackageFinding(
                    "error",
                    "ARTIFACT_MEMBER_TOO_LARGE",
                    name,
                    size,
                    f"单文件超过 {max_member_bytes} 字节预算",
                )
            )
    if total > max_artifact_bytes:
        findings.append(
            PackageFinding(
                "error",
                "ARTIFACT_TOO_LARGE",
                "<artifact>",
                total,
                f"制品总大小超过 {max_artifact_bytes} 字节预算",
            )
        )
    return findings


# LLM: tar 检查读取成员元数据而不解包，禁止在 gate 中把不可信归档写入文件系统。
# 函数用途: 检查 tar.gz/tgz 发布制品中的路径、运行数据、缓存和大小预算。
def check_tarball_findings(
    tar_path: Path,
    *,
    max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
    max_member_bytes: int = DEFAULT_MAX_MEMBER_BYTES,
) -> list[PackageFinding]:
    with tarfile.open(tar_path, "r:*") as archive:
        members = ((member.name, member.size) for member in archive.getmembers() if member.isfile())
        return _member_findings(
            members,
            max_artifact_bytes=max_artifact_bytes,
            max_member_bytes=max_member_bytes,
        )


# LLM: wheel 本质是 zip；使用同一 artifact contract，避免 wheel 跳过 package cleanliness。
# 函数用途: 检查 zip/wheel 制品中的路径、运行数据、缓存和解压后大小预算。
def check_zip_findings(
    zip_path: Path,
    *,
    max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
    max_member_bytes: int = DEFAULT_MAX_MEMBER_BYTES,
) -> list[PackageFinding]:
    with zipfile.ZipFile(zip_path) as archive:
        members = ((item.filename, item.file_size) for item in archive.infolist() if not item.is_dir())
        return _member_findings(
            members,
            max_artifact_bytes=max_artifact_bytes,
            max_member_bytes=max_member_bytes,
        )


# LLM: 保留旧 tarball API，只返回错误路径；新调用方优先使用结构化 findings。
# 函数用途: 兼容原有调用方，返回 tar 制品中的阻塞路径列表。
def check_tarball(tar_path: Path) -> list[str]:
    return [item.path for item in check_tarball_findings(tar_path) if item.severity == "error"]


# LLM: CLI 输出必须限制明细数量，同时保留总数/总字节，避免海量 untracked 文件刷爆终端。
# 函数用途: 以中文摘要展示错误和警告，并返回是否存在硬失败。
def _print_human(target: Path, findings: list[PackageFinding]) -> bool:
    errors = [item for item in findings if item.severity == "error"]
    warnings = [item for item in findings if item.severity == "warning"]
    if errors:
        print(
            f"FAILED: {target} 有 {len(errors)} 个阻塞项,总计 "
            f"{sum(item.size_bytes for item in errors)} 字节"
        )
        for item in sorted(errors, key=lambda row: (-row.size_bytes, row.path))[:REPORT_LIMIT]:
            print(f"  [{item.code}] {item.path} ({item.size_bytes} bytes)")
        if len(errors) > REPORT_LIMIT:
            print(f"  ... 另有 {len(errors) - REPORT_LIMIT} 项未展开；使用 --json 查看完整清单")
    else:
        print(f"OK: {target} 未发现发布阻塞项")
    for item in sorted(warnings, key=lambda row: (-row.size_bytes, row.path)):
        print(f"WARNING: [{item.code}] {item.path} ({item.size_bytes} bytes) {item.detail}")
    return bool(errors)


# LLM: 参数定义集中在此，main 只负责编排和退出码，避免 CLI 分支吞掉检查错误。
# 函数用途: 创建工作树/制品检查命令行解析器。
def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="检查 my-agent 工作树或发布制品是否干净")
    parser.add_argument("target", help="源码目录、tar.gz/tgz、zip 或 wheel")
    parser.add_argument("--mode", choices=("auto", "worktree", "artifact"), default="auto")
    parser.add_argument("--json", action="store_true", help="输出完整机器可读 findings")
    parser.add_argument("--runtime-warning-bytes", type=int, default=DEFAULT_RUNTIME_WARNING_BYTES)
    parser.add_argument("--max-artifact-bytes", type=int, default=DEFAULT_MAX_ARTIFACT_BYTES)
    parser.add_argument("--max-member-bytes", type=int, default=DEFAULT_MAX_MEMBER_BYTES)
    return parser


# LLM: mode 决定事实来源，所有 artifact 类型仍复用同一成员合同和预算字段。
# 函数用途: 按已解析参数运行目录、tar 或 zip/wheel 检查。
def _run_check(target: Path, args: argparse.Namespace) -> tuple[str, list[PackageFinding]]:
    mode = args.mode
    if mode == "auto":
        mode = "worktree" if target.is_dir() else "artifact"
    if mode == "worktree":
        if not target.is_dir():
            raise ValueError("worktree 模式要求目录目标")
        return mode, check_directory_findings(
            target,
            runtime_warning_bytes=max(0, args.runtime_warning_bytes),
        )
    options = {
        "max_artifact_bytes": max(0, args.max_artifact_bytes),
        "max_member_bytes": max(0, args.max_member_bytes),
    }
    if target.name.endswith((".tar.gz", ".tgz", ".tar")):
        return mode, check_tarball_findings(target, **options)
    if target.suffix.lower() in {".zip", ".whl"}:
        return mode, check_zip_findings(target, **options)
    raise ValueError("artifact 模式仅支持 tar/tar.gz/tgz/zip/whl")


# LLM: JSON 输出保留完整 findings，供 CI 消费；ok 只看 severity=error。
# 函数用途: 输出一次检查的机器可读结果。
def _print_json(target: Path, mode: str, findings: list[PackageFinding]) -> None:
    print(
        json.dumps(
            {
                "target": str(target),
                "mode": mode,
                "ok": not any(item.severity == "error" for item in findings),
                "findings": [item.to_dict() for item in findings],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


# LLM: 这是本地严格 gate 和 CI 的统一入口；异常必须转非零退出，不能打印 OK 后吞掉。
# 函数用途: 解析目标、执行检查，并以退出码阻止不干净发布。
def main(argv: Sequence[str] | None = None) -> int:
    args = _argument_parser().parse_args(argv)
    target = Path(args.target)
    if not target.exists():
        print(f"ERROR: target does not exist: {target}", file=sys.stderr)
        return 1
    try:
        mode, findings = _run_check(target, args)
    except (OSError, tarfile.TarError, zipfile.BadZipFile, ValueError) as exc:
        print(f"ERROR: 无法检查 {target}: {exc}", file=sys.stderr)
        return 1
    if args.json:
        _print_json(target, mode, findings)
    else:
        _print_human(target, findings)
    return 1 if any(item.severity == "error" for item in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
