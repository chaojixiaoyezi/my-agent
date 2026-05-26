# LLM: Pytest artifact inference is isolated from generic test-item preparation.
# 模块用途: 从结构化 artifact 路径推断可执行 pytest 项；只使用路径元数据，不读取测试正文。

from __future__ import annotations

from pathlib import Path
from typing import Any


# LLM: artifact_pytest_items gives closeout a bounded fallback when runners omit tests.
# 函数用途: 从 workspace 内 test_*.py artifact 生成 pytest 命令；普通源码和非 Python 文件不会被推断。
def artifact_pytest_items(
    artifact_paths: list[tuple[str, Path]],
    *,
    workspace_root: Path,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for _raw, path in artifact_paths:
        if path in seen or not _is_pytest_artifact(path):
            continue
        seen.add(path)
        items.append({
            "name": f"artifact pytest {path.name}",
            "validation_method": "command",
            "command": f"python3 -m pytest {path.name} -q",
            "working_dir": _relative_or_absolute(path.parent, workspace_root),
        })
    return items


# LLM: _is_pytest_artifact keeps inferred tests narrow to conventional Python test files.
# 函数用途: 判断 artifact 是否是可安全自动执行的 pytest 文件。
def _is_pytest_artifact(path: Path) -> bool:
    return path.is_file() and path.suffix == ".py" and path.name.startswith("test_")


# LLM: _relative_or_absolute keeps generated test refs portable across workspaces.
# 函数用途: 优先返回 workspace 相对路径；越界时保留绝对路径给执行器边界再校验。
def _relative_or_absolute(path: Path, workspace_root: Path) -> str:
    try:
        return str(path.relative_to(workspace_root)) or "."
    except ValueError:
        return str(path)
