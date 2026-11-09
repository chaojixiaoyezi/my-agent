"""以真实索引和正式prepare口记录查找工作量，不把小正文等同廉价定位。"""
from __future__ import annotations

import json
import os

import pytest

from agent_py_agent.agent.memory_archive.artifact import reader


def test_legacy_scoped_ref_preparation_does_not_decode_owner_history_twice(tmp_path, monkeypatch):
    primary = tmp_path / "blobs/tool_outputs/index.jsonl"
    primary.parent.mkdir(parents=True)
    primary.write_text("".join(json.dumps({"scoped_call_id": f"old:{i}", "pad": "x" * 128}) + "\n"
                               for i in range(300)), encoding="utf-8")
    target = tmp_path / "runs/2001-01-01/old-task/work/blobs/tool_outputs/index.jsonl"
    target.parent.mkdir(parents=True)
    body = target.parent / "body.json"
    body.write_text("{}", encoding="utf-8")
    ref = "legacy-run:call-target"
    target.write_text(json.dumps({"scoped_call_id": ref, "run_id": "legacy-run",
        "call_id": "call-target", "path": str(body)}) + "\n", encoding="utf-8")
    monkeypatch.setattr(reader, "_ARTIFACT_INDEX_CACHE", reader._VerifiedArtifactCache(1024))
    visits = []
    original = reader.iter_decoded_tool_index_objects

    def counted(source, *args, **kwargs):
        visits.append(source)
        yield from original(source, *args, **kwargs)

    monkeypatch.setattr(reader, "iter_decoded_tool_index_objects", counted)
    prepared = reader.prepare_tool_output_artifact_read(
        reader.ReadToolOutputArtifactRequest(tmp_path, ref, max_chars=5))
    assert prepared.record is not None
    assert len(visits) == 2, "同次prepare只应读取主索引与目标索引各一次"


def test_prepare_negative_reuse_expires_at_request_boundary(tmp_path, monkeypatch):
    index = tmp_path / "blobs/tool_outputs/index.jsonl"
    index.parent.mkdir(parents=True)
    index.write_text(json.dumps({"pad": "x" * 4096}) + "\n", encoding="utf-8")
    monkeypatch.setattr(reader, "_ARTIFACT_INDEX_CACHE", reader._VerifiedArtifactCache(128))
    calls = []
    original = reader.iter_decoded_tool_index_objects

    def counted(*args, **kwargs):
        calls.append(1)
        yield from original(*args, **kwargs)

    monkeypatch.setattr(reader, "iter_decoded_tool_index_objects", counted)
    request = reader.ReadToolOutputArtifactRequest(tmp_path, "missing:call")
    assert reader.prepare_tool_output_artifact_read(request).record is None
    assert reader.prepare_tool_output_artifact_read(request).record is None
    assert len(calls) == 2
    index.write_text(json.dumps({"scoped_call_id": "missing:call", "run_id": "missing", "path": ""}) + "\n")
    assert reader.prepare_tool_output_artifact_read(request).record is not None


@pytest.mark.parametrize("change", ["append", "replace", "truncate", "same_size_mtime", "corrupt"])
def test_prepare_reopens_and_revalidates_changed_primary(tmp_path, monkeypatch, change):
    index = tmp_path / "blobs/tool_outputs/index.jsonl"
    index.parent.mkdir(parents=True)
    index.write_text(json.dumps({"pad": "x" * 4096}) + "\n", encoding="utf-8")
    monkeypatch.setattr(reader, "_ARTIFACT_INDEX_CACHE", reader._VerifiedArtifactCache(128))
    discover = reader.tool_output_index_paths_for_lookup
    seen = []
    original = reader.iter_decoded_tool_index_objects

    def counted(*args, **kwargs):
        seen.append(1)
        yield from original(*args, **kwargs)

    def changed(root):
        data = json.dumps({"scoped_call_id": "wanted:call", "run_id": "wanted", "path": ""}) + "\n"
        if change == "append":
            with index.open("a") as handle:
                handle.write(data)
        elif change == "replace":
            replacement = index.with_suffix(".new")
            replacement.write_text(data)
            replacement.replace(index)
        elif change == "truncate":
            index.write_text(data)
        elif change == "same_size_mtime":
            before = index.stat()
            index.write_text(data.rstrip() + " " * (before.st_size - len(data)) + "\n")
            os.utime(index, ns=(before.st_atime_ns, before.st_mtime_ns))
            assert index.stat().st_size == before.st_size
        else:
            index.write_text('{"bad":' + "1" * 5000 + '}\n')
        return discover(root)

    monkeypatch.setattr(reader, "iter_decoded_tool_index_objects", counted)
    monkeypatch.setattr(reader, "tool_output_index_paths_for_lookup", changed)
    request = reader.ReadToolOutputArtifactRequest(tmp_path, "wanted:call")
    if change == "corrupt":
        with pytest.raises(ValueError):
            reader.prepare_tool_output_artifact_read(request)
    else:
        assert reader.prepare_tool_output_artifact_read(request).record is not None
    assert len(seen) == 2
    assert reader._PREPARE_INDEX_READ.get() is None


def test_prepare_tokens_are_isolated_between_concurrent_threads(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    roots = [tmp_path / str(i) for i in range(2)]
    for root in roots:
        index = root / "blobs/tool_outputs/index.jsonl"
        index.parent.mkdir(parents=True)
        index.write_text(json.dumps({"pad": "x" * 4096}) + "\n")
    monkeypatch.setattr(reader, "_ARTIFACT_INDEX_CACHE", reader._VerifiedArtifactCache(128))
    barrier = threading.Barrier(2)
    original = reader.tool_output_index_paths_for_lookup

    def discover(root):
        barrier.wait(timeout=5)
        token = reader._PREPARE_INDEX_READ.get()
        assert token and str(root) in token[0][1]
        return original(root)

    monkeypatch.setattr(reader, "tool_output_index_paths_for_lookup", discover)

    def prepare(root):
        result = reader.prepare_tool_output_artifact_read(
            reader.ReadToolOutputArtifactRequest(root, "absent:call"))
        assert reader._PREPARE_INDEX_READ.get() is None
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(prepare, roots))
    assert all(result.record is None for result in results)
    assert reader._PREPARE_INDEX_READ.get() is None


def test_prepare_negative_token_keeps_no_parsed_source_rows(tmp_path, monkeypatch):
    index = tmp_path / "blobs/tool_outputs/index.jsonl"
    index.parent.mkdir(parents=True)
    index.write_text("".join(json.dumps({"scoped_call_id": f"noise:{i}", "pad": "x" * 100}) + "\n"
                             for i in range(300)))
    monkeypatch.setattr(reader, "_ARTIFACT_INDEX_CACHE", reader._VerifiedArtifactCache(0))
    original = reader.tool_output_index_paths_for_lookup

    def inspect(root):
        token = reader._PREPARE_INDEX_READ.get()
        assert type(token) is tuple and len(token) == 2
        identity, literal = token
        assert all(type(item) in (str, int) for item in identity)
        assert literal == "absent:call"
        return original(root)

    monkeypatch.setattr(reader, "tool_output_index_paths_for_lookup", inspect)
    assert reader.prepare_tool_output_artifact_read(
        reader.ReadToolOutputArtifactRequest(tmp_path, "absent:call")).record is None
