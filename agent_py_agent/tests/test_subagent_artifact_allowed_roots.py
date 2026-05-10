"""LLM: artifact manifest regressions for authorized product output roots.

函数/模块用途: 确认 leaf 写到 `allowed_write_roots` 的业务产物能被 manifest 安全登记。
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.subagents.manager import SubAgentManager


# LLM: test_subagent_persistence_resolves_allowed_product_artifacts covers leaf outputs outside task_dir.
# 函数用途: 子代理被授权写入的产物目录也应成为 artifact manifest 的安全解析根。
def test_subagent_persistence_resolves_allowed_product_artifacts(tmp_path) -> None:
    manager = SubAgentManager(tmp_path / "subs")
    deliverables = tmp_path / "deliverables"

    task = manager.create_run(
        goal="外部产物目录引用",
        thought="leaf 写业务产物，manifest 只登记元数据。",
        plan=["写 artifact", "保存 manifest"],
        extra_write_roots=[str(deliverables)],
    )
    artifact_path = deliverables / "build" / "index.html"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text("<h1>ok</h1>\n", encoding="utf-8")
    task.artifact_refs = [str(artifact_path)]
    manager.save(task)

    loaded = manager.load(task.id)
    record = _read_jsonl(loaded.agent_run_artifact_manifest_jsonl)[0]

    assert record["ref"] == str(artifact_path)
    assert record["path"] == str(artifact_path)
    assert record["exists"] is True
    assert record["resolution_status"] == "resolved"
    assert record["size_bytes"] == artifact_path.stat().st_size


# LLM: _read_jsonl keeps the tiny test file self-contained.
# 函数用途: 读取 manifest JSONL，避免依赖大测试文件里的私有 helper。
def _read_jsonl(path: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
