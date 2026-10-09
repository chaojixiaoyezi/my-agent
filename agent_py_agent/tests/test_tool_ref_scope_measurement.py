"""1000 根同字节合成索引：在原压缩来源与收尾产物更新入口计量读取。"""
from __future__ import annotations

import gc
import hashlib
import json
import time
import tracemalloc
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._finalization_service import FinalizationService
from agent_py_agent.agent.common import tool_output_paths
from agent_py_agent.agent.memory_archive import compact_apply
from agent_py_agent.tests.test_tool_ref_scope_stream import compact_plan, index_row, write_index


class _MeteredFile:
    def __init__(self, handle, stats):
        self.handle, self.stats = handle, stats

    def __enter__(self):
        self.handle.__enter__()
        return self

    def __exit__(self, *args):
        return self.handle.__exit__(*args)

    def _count(self, value):
        self.stats["read_bytes"] += len(value.encode("utf-8") if isinstance(value, str) else value)
        return value

    def read(self, *args, **kwargs):
        return self._count(self.handle.read(*args, **kwargs))

    def read1(self, *args, **kwargs):
        return self._count(self.handle.read1(*args, **kwargs))

    def __iter__(self):
        for line in self.handle:
            yield self._count(line)

    def __getattr__(self, name):
        return getattr(self.handle, name)


def _corpus(root: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    for number in range(1000):
        directory = root if number == 0 else root / f"runs/2026-10-08/root{number:04d}/work"
        rows = [index_row(root, "tool_call" if i % 2 else "tool_output", "other", f"noise-{number}-{i}")
                for i in range(16)]
        rows = [{**row, "padding": "x" * 2048} for row in rows]
        if number in (0, 12, 998):
            rows += [index_row(root, kind, "request-hit", f"hit-{number}-{kind}")
                     for kind in ("tool_call", "tool_output")]
        content = b"\n".join(json.dumps(row, ensure_ascii=True).encode() for row in rows) + b"\n"
        write_index(directory, content)
        total += len(content)
        digest.update(content)
    return total, digest.hexdigest()


def _install_meter(monkeypatch, stats: dict) -> None:
    real_loads, real_open = json.loads, Path.open
    real_roots = tool_output_paths.tool_output_roots_for_lookup

    def loads(text, *args, **kwargs):
        stats["json_loads"] += 1
        return real_loads(text, *args, **kwargs)

    def open_path(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        if path.name == "index.jsonl" and path.parent.name == "tool_outputs":
            stats["index_opens"] += 1
            return _MeteredFile(handle, stats)
        return handle

    def roots(root):
        stats["lookups"] += 1
        return real_roots(root)

    monkeypatch.setattr(json, "loads", loads)
    monkeypatch.setattr(Path, "open", open_path)
    monkeypatch.setattr(tool_output_paths, "tool_output_roots_for_lookup", roots)


@pytest.mark.parametrize("mode", ["compact", "finalization"])
def test_large_scope_read_work(tmp_path: Path, monkeypatch, mode: str) -> None:
    index_bytes, corpus_sha256 = _corpus(tmp_path)
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text('{"artifact_refs":{"items":[]}}', encoding="utf-8")
    stats = {"json_loads": 0, "lookups": 0, "index_opens": 0, "read_bytes": 0}
    with monkeypatch.context() as context:
        _install_meter(context, stats)
        gc.collect()
        tracemalloc.start()
        start = time.perf_counter()
        try:
            result = _invoke(mode, tmp_path, bundle_path)
            stats["seconds"] = round(time.perf_counter() - start, 6)
            stats["peak_bytes"] = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
    stats.update(mode=mode, index_bytes=index_bytes, corpus_sha256=corpus_sha256)
    print("\nE11E_MEASURE=" + json.dumps(stats, sort_keys=True))
    if mode == "compact":
        assert [item["call_id"] for item in result["tool_calls"]] == [
            "hit-0-tool_call", "hit-12-tool_call", "hit-998-tool_call"]
        assert [item["call_id"] for item in result["tool_outputs"]] == [
            "hit-0-tool_output", "hit-12-tool_output", "hit-998-tool_output"]
    else:
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        assert [item["call_id"] for item in bundle["artifact_refs"]["items"]] == [
            "hit-0-tool_output", "hit-12-tool_output", "hit-998-tool_output"]
    assert stats["lookups"] == 1
    assert stats["index_opens"] == 1000
    assert stats["read_bytes"] == index_bytes
    assert stats["json_loads"] == (16012 if mode == "compact" else 16013)  # 全行资源合同，收尾另读bundle。
    assert stats["peak_bytes"] < 3 * 1024 * 1024


def _invoke(mode: str, root: Path, bundle_path: Path):
    if mode == "compact":
        return compact_apply._source_refs(compact_plan(root, {"request_id": "request-hit"}))
    service = FinalizationService(SimpleNamespace(root=root))
    ctx = SimpleNamespace(do_save=True, main_context_bundle_path=str(bundle_path),
                          request_id="request-hit", run_id="request-hit", task_id="request-hit")
    return service._update_main_context_bundle_artifacts(ctx, "request-hit")
