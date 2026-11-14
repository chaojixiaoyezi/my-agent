from __future__ import annotations

# LLM: 统一构建标识只暴露不可变的构建元数据：包版本、Git SHA 与运行时前缀；
# 顶层纯标准库无重量依赖，使用 lru_cache 确保单进程只读取一次，请求路径零子进程派生。
# 模块用途: 提供跨 CLI 与 Gateway 共享的构建标识契约（BIZ-007 / V15-01）。
import functools
import importlib.metadata
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from ... import __version__

_MODULE_ORIGIN = Path(__file__).resolve()
_RUNTIME_PREFIX = Path(sys.prefix).resolve()


def _commit(value: object) -> str:
    return value.lower() if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value) else ""


def _installed_commit() -> str:
    # 只认本进程模块所在的安装树，不追current、cwd或其它安装；旧包缺VCS字段保持unknown。
    if not _MODULE_ORIGIN.is_relative_to(_RUNTIME_PREFIX):
        return ""
    manifests = [(_RUNTIME_PREFIX / "my-agent-release.json", "commit", None)]
    if _RUNTIME_PREFIX.name == "runtime":
        manifests.append((_RUNTIME_PREFIX.parent / "release.json", "commit", "my-agent.private-release.v1"))
    for path, field, schema in manifests:
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
                continue
            row = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(row, dict) and (schema is None or row.get("schema") == schema):
                if sha := _commit(row.get(field)):
                    return sha
        except (OSError, ValueError):
            continue
    try:
        distribution = importlib.metadata.distribution("my-agent")
        if Path(distribution.locate_file("")).resolve().is_relative_to(_RUNTIME_PREFIX):
            text = distribution.read_text("direct_url.json") or ""
            if len(text) <= 65536:
                row = json.loads(text)
                vcs = row.get("vcs_info", {}) if isinstance(row, dict) else {}
                if isinstance(vcs, dict) and vcs.get("vcs") == "git":
                    return _commit(vcs.get("commit_id"))
    except (OSError, ValueError, importlib.metadata.PackageNotFoundError):
        pass
    return ""


# LLM: 通过环境变量优先、单次读取本仓Git HEAD备选的方式提取不可变构建标识，
# 并通过 lru_cache(maxsize=1) 彻底杜绝每个请求重复 spawn 进程消耗。
# 函数用途: 返回当前运行时的版本、git_sha 与 runtime_prefix 构建标识字典。
@functools.lru_cache(maxsize=1)
def get_build_identity() -> dict[str, str]:
    git_sha = _commit(
        os.environ.get("MY_AGENT_GIT_SHA")
        or os.environ.get("GIT_SHA")
        or os.environ.get("BUILD_GIT_SHA")
        or ""
    )
    if not git_sha:
        git_sha = _installed_commit()
    if not git_sha:
        try:
            repo_root = _MODULE_ORIGIN.parents[3]
            git_dir = repo_root / ".git"
            if git_dir.is_symlink() or not git_dir.exists():
                raise ValueError("module has no source checkout VCS metadata")
            # Git定位/配置环境不是显式构建身份合同；剥离整个命名空间，避免借其它checkout的HEAD。
            # 精确模块仓根必须自带.git；合法linked-worktree的gitfile仍由Git解析，不向祖先发现仓库。
            git_env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
            git_env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
            proc = subprocess.run(
                ["git", "--no-optional-locks", f"--git-dir={git_dir}", f"--work-tree={repo_root}",
                 "rev-parse", "--show-toplevel", "--verify", "HEAD^{commit}"],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                timeout=2.0,
                env=git_env,
            )
            lines = proc.stdout.splitlines()
            if (proc.returncode == 0 and len(lines) == 2
                    and os.path.normcase(str(Path(lines[0]).resolve())) == os.path.normcase(str(repo_root.resolve()))):
                git_sha = _commit(lines[1].strip())
        except Exception:
            git_sha = ""
    return {
        "version": str(__version__),
        "git_sha": str(git_sha),
        "runtime_prefix": str(sys.prefix),
    }
