"""toolrefs-2：相同入口/语料分别计量峰值和无探针的三次耗时；真基线也执行本文件。"""
from __future__ import annotations

import gc
import json
import statistics
import time
import tracemalloc
from pathlib import Path

import pytest

from agent_py_agent.tests.test_tool_ref_hot_measurement import (
    assert_hot_result,
    hot_corpus,
    invoke_hot,
)
from agent_py_agent.tests.test_tool_ref_scope_measurement import _corpus, _install_meter, _invoke


@pytest.mark.parametrize("mode", ["compact", "finalization", "artifact", "display", "estimate", "promotion", "control", "carry"])
def test_same_entry_separate_peak_and_median3(tmp_path: Path, monkeypatch, mode: str) -> None:
    scope_mode = mode in {"compact", "finalization"}
    index_bytes, corpus_sha256 = (_corpus if scope_mode else hot_corpus)(tmp_path)
    (tmp_path / "blobs/tool_outputs/hit-0-tool_output.txt").write_text(
        json.dumps({"kind": "tool_output", "content": "synthetic display"}), encoding="utf-8")
    bundle = tmp_path / "bundle.json"
    bundle.write_text('{"artifact_refs":{"items":[]}}', encoding="utf-8")
    stats = {"json_loads": 0, "lookups": 0, "index_opens": 0, "read_bytes": 0}
    with monkeypatch.context() as context:
        _install_meter(context, stats)
        gc.collect()
        tracemalloc.start()
        try:
            result = invoke_entry(mode, tmp_path, bundle)
            stats["peak_bytes"] = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
    check_entry_result(mode, result, bundle)
    samples = []
    for _ in range(3):
        bundle.write_text('{"artifact_refs":{"items":[]}}', encoding="utf-8")
        gc.collect()
        assert not tracemalloc.is_tracing()
        start = time.perf_counter()
        result = invoke_entry(mode, tmp_path, bundle)
        samples.append(time.perf_counter() - start)
        check_entry_result(mode, result, bundle)
    stats.update(mode=mode, index_bytes=index_bytes, corpus_sha256=corpus_sha256,
                 seconds_samples=samples, seconds_median3=statistics.median(samples))
    print("\nTOOLREFS2_MEASURE=" + json.dumps(stats, sort_keys=True))


def invoke_entry(mode: str, root: Path, bundle: Path):
    if mode in {"compact", "finalization"}:
        return _invoke(mode, root, bundle)
    return invoke_hot(mode, root)


def check_entry_result(mode: str, result, bundle: Path) -> None:
    if mode == "compact":
        assert [item["call_id"] for item in result["tool_calls"]] == [
            "hit-0-tool_call", "hit-12-tool_call", "hit-998-tool_call"]
        assert [item["call_id"] for item in result["tool_outputs"]] == [
            "hit-0-tool_output", "hit-12-tool_output", "hit-998-tool_output"]
        return
    if mode == "finalization":
        items = json.loads(bundle.read_text(encoding="utf-8"))["artifact_refs"]["items"]
        assert [item["call_id"] for item in items] == [
            "hit-0-tool_output", "hit-12-tool_output", "hit-998-tool_output"]
        return
    assert_hot_result(mode, result)
