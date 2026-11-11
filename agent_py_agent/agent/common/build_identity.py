from __future__ import annotations

# LLM: 统一构建标识只暴露不可变的构建元数据：包版本、Git SHA 与运行时前缀；
# 顶层纯标准库无重量依赖，使用 lru_cache 确保单进程只读取一次，请求路径零子进程派生。
# 模块用途: 提供跨 CLI 与 Gateway 共享的构建标识契约（BIZ-007 / V15-01）。
import functools
import os
import subprocess
import sys
from pathlib import Path

from ... import __version__


# LLM: 通过环境变量优先、单次读取本仓Git HEAD备选的方式提取不可变构建标识，
# 并通过 lru_cache(maxsize=1) 彻底杜绝每个请求重复 spawn 进程消耗。
# 函数用途: 返回当前运行时的版本、git_sha 与 runtime_prefix 构建标识字典。
@functools.lru_cache(maxsize=1)
def get_build_identity() -> dict[str, str]:
    git_sha = (
        os.environ.get("MY_AGENT_GIT_SHA")
        or os.environ.get("GIT_SHA")
        or os.environ.get("BUILD_GIT_SHA")
        or ""
    )
    if not git_sha:
        try:
            repo_root = Path(__file__).resolve().parents[3]
            proc = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            if proc.returncode == 0:
                git_sha = proc.stdout.strip()
        except Exception:
            git_sha = ""
    return {
        "version": str(__version__),
        "git_sha": str(git_sha),
        "runtime_prefix": str(sys.prefix),
    }
