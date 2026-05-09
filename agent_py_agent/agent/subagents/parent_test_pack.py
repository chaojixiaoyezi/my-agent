# LLM: Parent-owned test pack support for subagent acceptance without trusting worker-declared tests.
# 模块用途: 读取/写入父级验收测试包，让共享代码仓库可以被父代理自己的 contract tests 检查。

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar


# LLM: ParentTestPackWriteRequest bundles all fields needed to persist a parent-owned test pack.
# 类用途: 保存父级测试包写入参数；新增 scope、version 或 owner 字段时扩展这个 bundle。
@dataclass(frozen=True)
class ParentTestPackWriteRequest:
    """Bundle for writing a parent-owned test pack."""

    __test__: ClassVar[bool] = False

    reports_dir: str | Path
    tests: list[dict[str, Any]]
    source: str = "parent"
    reserved: dict[str, Any] = field(default_factory=dict)


# LLM: write_parent_test_pack writes tests refs only; it does not execute commands.
# 函数用途: 写入 `parent_test_pack.json`，让后续 `subagents-tests --re-run` 合并父级测试项。
def write_parent_test_pack(request: ParentTestPackWriteRequest) -> Path:
    """Persist a parent-owned test pack beside subagent reports."""

    reports_dir = Path(request.reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / "parent_test_pack.json"
    path.write_text(
        json.dumps(
            {
                "schema": "parent_test_pack.v1",
                "source": request.source,
                "tests": [dict(item) for item in request.tests if isinstance(item, dict)],
                "reserved": {
                    "parent_owned": True,
                    "refs_only": True,
                    "worker_editable": False,
                    **dict(request.reserved),
                },
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


# LLM: load_parent_test_items reads parent-owned test declarations and tags their source.
# 函数用途: 从 task reports 目录读取父级测试项；坏 JSON 或缺失文件返回空列表，避免 CLI 崩溃。
def load_parent_test_items(task) -> list[dict[str, Any]]:
    """Load parent-owned tests for a task."""

    payload = _load_pack_payload(Path(getattr(task, "reports_dir", "")) / "parent_test_pack.json")
    source = str(payload.get("source") or "parent")
    tests = payload.get("tests") if isinstance(payload, dict) else []
    tests = tests if isinstance(tests, list) else []
    return [_tagged_test(item, source) for item in tests if isinstance(item, dict)]


# LLM: _load_pack_payload keeps old/missing/corrupt files non-fatal for subagents-tests.
# 函数用途: 读取 parent_test_pack JSON；异常时返回空结构，由调用方继续执行 worker tests。
def _load_pack_payload(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


# LLM: _tagged_test copies a test item and records that it came from the parent oracle.
# 函数用途: 给父级测试项加 source 字段，后续分类/报告能区分 worker 自测和父级 oracle。
def _tagged_test(item: dict[str, Any], source: str) -> dict[str, Any]:
    value = dict(item)
    value.setdefault("source", source)
    return value
