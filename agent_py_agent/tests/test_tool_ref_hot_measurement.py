"""07 steer2：同1000根字节语料计量高频真实调用入口。"""
from __future__ import annotations

import gc
import hashlib
import json
import time
import tracemalloc
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.tool_loop import display_archive
from agent_py_agent.agent.common.tool_output_paths import tool_output_index_paths_for_lookup
from agent_py_agent.agent.memory_archive import compact_tool_output_refs as refs
from agent_py_agent.agent.memory_archive import control_plane
from agent_py_agent.agent.memory_archive.artifact import reader
from agent_py_agent.agent.memory_store.promotion import LocalStoreToolEvidenceVerifier
from agent_py_agent.agent.tooling.artifact import ReadArtifactTool
from agent_py_agent.tests.test_tool_ref_scope_measurement import _corpus, _install_meter


@pytest.mark.parametrize("mode", ["artifact", "display", "estimate", "promotion", "control", "carry"])
def test_large_hot_entry_read_work(tmp_path: Path, monkeypatch, mode: str) -> None:
    index_bytes, corpus_sha256 = hot_corpus(tmp_path)
    (tmp_path / "blobs/tool_outputs/hit-0-tool_output.txt").write_text(json.dumps({"kind": "tool_output", "content": "synthetic display"}))
    stats = {"json_loads": 0, "lookups": 0, "index_opens": 0, "read_bytes": 0}
    with monkeypatch.context() as context:
        _install_meter(context, stats)
        gc.collect()
        tracemalloc.start()
        start = time.perf_counter()
        try:
            result = invoke_hot(mode, tmp_path)
            stats["seconds"] = round(time.perf_counter() - start, 6)
            stats["peak_bytes"] = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
    stats.update(mode=mode, index_bytes=index_bytes, corpus_sha256=corpus_sha256)
    print("\nE11E_HOT_MEASURE=" + json.dumps(stats, sort_keys=True))
    assert_hot_result(mode, result)
    assert stats["lookups"] == 1  # 计规范索引枚举；artifact路径权限复核另有根目录枚举，不读取索引。
    assert stats["index_opens"] == 1000
    assert stats["read_bytes"] == index_bytes
    expected = {"artifact": 16007, "display": 16007, "estimate": 16006, "promotion": 16006}
    assert stats["json_loads"] == expected.get(mode, 16012)
    assert stats["peak_bytes"] < 3 * 1024 * 1024


def invoke_hot(mode: str, root: Path):
    if mode == "artifact":
        return ReadArtifactTool(root).execute({"artifact_ref": "hit-0-tool_output", "max_chars": 64})
    if mode == "display":
        event = SimpleNamespace(result=SimpleNamespace(metadata={"archive_output_record": {
            "output_externalized": True, "call_id": "hit-0-tool_output"}}),
            request=SimpleNamespace(agent=SimpleNamespace(root=root, owner_home=root), params=SimpleNamespace(task_attributes={})),
            call=SimpleNamespace(run_id="request-hit"))
        return display_archive._original_output(event)
    if mode == "estimate":
        return reader.estimate_tool_output_artifact_size(reader.ReadToolOutputArtifactRequest(root, "hit-0-tool_output"))
    if mode == "promotion":
        verifier = LocalStoreToolEvidenceVerifier(object(), owner_id="synthetic", owner_root=root)
        return verifier.verify({"owner_id": "synthetic", "run_id": "request-hit", "call_id": "hit-0-tool_output", "tool": "read_file"})
    if mode == "carry":
        return refs.carried_tool_call_records(root, {"request_id": "request-hit"})
    return control_plane.query_memory_control_plane(root, control_plane.MemoryControlPlaneQueryOptions(run_id="request-hit"))


def hot_corpus(root: Path) -> tuple[int, str]:
    _corpus(root)
    index = root / "blobs/tool_outputs/index.jsonl"
    original = json.dumps(str(root / "hit-0-tool_output.txt")).encode()
    canonical = json.dumps(str(root / "blobs/tool_outputs/hit-0-tool_output.txt")).encode()
    index.write_bytes(index.read_bytes().replace(original, canonical))
    total, digest = 0, hashlib.sha256()
    for path in tool_output_index_paths_for_lookup(root):
        content = path.read_bytes()
        total += len(content)
        digest.update(content)
    return total, digest.hexdigest()


def assert_hot_result(mode: str, result) -> None:
    if mode == "artifact":
        assert result.ok is True
        return
    if mode == "display":
        assert result == "synthetic display"
        return
    if mode == "estimate":
        assert result == 7
        return
    if mode == "promotion":
        assert result is None  # 原语料没有operation成功事实，不能因匹配身份而证明晋升成功
        return
    if mode == "carry":
        assert len(result) == 6
        return
    assert len(result["tool_outputs"]) == 6
