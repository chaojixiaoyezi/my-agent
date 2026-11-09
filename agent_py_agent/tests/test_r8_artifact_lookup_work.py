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


@pytest.mark.parametrize("change", ["append", "replace", "truncate", "same_size_mtime", "same_observed_stamp", "corrupt"])
def test_prepare_reopens_and_revalidates_changed_primary(tmp_path, monkeypatch, change):
    index = tmp_path / "blobs/tool_outputs/index.jsonl"
    index.parent.mkdir(parents=True)
    index.write_text(json.dumps({"pad": "x" * 4096}) + "\n", encoding="utf-8")
    monkeypatch.setattr(reader, "_ARTIFACT_INDEX_CACHE", reader._VerifiedArtifactCache(128))
    if change == "same_observed_stamp":
        signature = reader._blob_stat_signature
        monkeypatch.setattr(reader, "_blob_stat_signature", lambda info: (*signature(info)[:3], 0, 0))
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
        elif change in {"same_size_mtime", "same_observed_stamp"}:
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
        assert type(token) is tuple and len(token) == 3
        identity, literal, digest = token
        assert type(digest) is bytes and len(digest) == 32
        assert all(type(item) in (str, int) for item in identity)
        assert literal == "absent:call"
        return original(root)

    monkeypatch.setattr(reader, "tool_output_index_paths_for_lookup", inspect)
    assert reader.prepare_tool_output_artifact_read(
        reader.ReadToolOutputArtifactRequest(tmp_path, "absent:call")).record is None


@pytest.mark.parametrize("cache_bytes", [4096, 16 * 1024 * 1024])
def test_positive_index_cache_verifies_bytes_with_identical_stat(tmp_path, monkeypatch, cache_bytes):
    index = tmp_path / "index.jsonl"
    ref = "wanted:call"
    line = (json.dumps({"scoped_call_id": ref, "marker": "old"}) + "\n"
            + json.dumps({"scoped_call_id": "noise:call", "pad": "x" * 8192}) + "\n")
    index.write_text(line)
    monkeypatch.setattr(reader, "_ARTIFACT_INDEX_CACHE", reader._VerifiedArtifactCache(cache_bytes))
    signature = reader._blob_stat_signature
    monkeypatch.setattr(reader, "_blob_stat_signature", lambda info: (*signature(info)[:3], 0, 0))
    decoded = []
    original = reader.iter_decoded_tool_index_objects

    def count(*args, **kwargs):
        decoded.append(1)
        yield from original(*args, **kwargs)

    monkeypatch.setattr(reader, "iter_decoded_tool_index_objects", count)
    cold = reader._parsed_artifact_index(index, literal_ref=ref)
    assert cold[1][ref][0]["marker"] == "old"
    assert reader._parsed_artifact_index(index, literal_ref=ref) is cold
    assert len(decoded) == 1
    index.write_text(line.replace('"old"', '"new"'))
    fresh = reader._parsed_artifact_index(index, literal_ref=ref)
    assert fresh[1][ref][0]["marker"] == "new" and len(decoded) == 2
    assert reader._parsed_artifact_index(index, literal_ref=ref) is fresh
    assert len(decoded) == 2
    # 同一物理长度也可变成资源损坏；旧正结果不能掩盖原严格解析错误。
    index.write_text('{"bad":' + "1" * 5000 + '}' + " " * (len(line) - 5009) + "\n")
    assert index.stat().st_size == len(line.encode())
    with pytest.raises(ValueError):
        reader._parsed_artifact_index(index, literal_ref=ref)
    assert len(decoded) == 3
    index.write_text(line.replace(ref, "newest:call"))
    newest = reader._parsed_artifact_index(index, literal_ref="newest:call")
    assert newest[1]["newest:call"][0]["marker"] == "old"


@pytest.mark.parametrize("cache_bytes", [4096, 16 * 1024 * 1024])
def test_positive_cache_hot_hit_still_reads_current_index_bytes(tmp_path, monkeypatch, cache_bytes):
    from contextlib import contextmanager

    index = tmp_path / "index.jsonl"
    ref = "wanted:call"
    index.write_text(json.dumps({"scoped_call_id": ref}) + "\n"
                     + json.dumps({"scoped_call_id": "noise:call", "pad": "x" * 8192}) + "\n")
    monkeypatch.setattr(reader, "_ARTIFACT_INDEX_CACHE", reader._VerifiedArtifactCache(cache_bytes))
    consumed = []

    class MeasuredRead:
        def __init__(self, handle):
            self.handle = handle

        def fileno(self):
            return self.handle.fileno()

        def seek(self, offset):
            return self.handle.seek(offset)

        def read(self, size):
            assert 0 < size <= 64 * 1024
            block = self.handle.read(size)
            consumed.append(len(block))
            return block

    class Source:
        path = index

        @contextmanager
        def open(self, mode):
            with index.open(mode) as handle:
                yield MeasuredRead(handle)

    source = Source()
    cold = reader._parsed_artifact_index(source, literal_ref=ref)
    assert sum(consumed) == index.stat().st_size
    consumed.clear()
    assert reader._parsed_artifact_index(source, literal_ref=ref) is cold
    assert sum(consumed) == index.stat().st_size


def test_oversized_literal_retains_only_structured_candidates_in_shared_budget(tmp_path, monkeypatch):
    index = tmp_path / "index.jsonl"
    ref = "wanted:call"
    noise = [{"scoped_call_id": f"noise:{i}", "pad": ref + "x" * 512} for i in range(256)]
    expected = {"scoped_call_id": ref, "run_id": "wanted", "path": ""}
    index.write_text("".join(json.dumps(row) + "\n" for row in [*noise, expected]))
    cache = reader._VerifiedArtifactCache(4096)
    monkeypatch.setattr(reader, "_ARTIFACT_INDEX_CACHE", cache)
    result = reader._parsed_artifact_index(index, literal_ref=ref)
    assert result[0] == (expected,)
    assert result[1][ref] == [expected]
    assert 0 < cache.bytes <= cache.max_bytes and len(cache.entries) == 1
    assert reader._parsed_artifact_index(index, literal_ref=ref) is result


def test_oversized_literal_over_budget_keeps_all_matches_without_caching(tmp_path, monkeypatch):
    index = tmp_path / "index.jsonl"
    ref = "wanted:call"
    rows = [{"scoped_call_id": ref, "sequence": i, "pad": "x" * 512} for i in range(128)]
    index.write_text("".join(json.dumps(row) + "\n" for row in rows))
    cache = reader._VerifiedArtifactCache(4096)
    monkeypatch.setattr(reader, "_ARTIFACT_INDEX_CACHE", cache)
    decoded = []
    original = reader.iter_decoded_tool_index_objects

    def count(*args, **kwargs):
        decoded.append(1)
        yield from original(*args, **kwargs)

    monkeypatch.setattr(reader, "iter_decoded_tool_index_objects", count)
    for _ in range(2):
        result = reader._parsed_artifact_index(index, literal_ref=ref)
        assert list(result[0]) == rows and result[1][ref] == rows
        assert cache.bytes == 0 and not cache.entries
    assert len(decoded) == 2


def test_oversized_literal_filter_preserves_unrelated_decode_resource_errors(tmp_path, monkeypatch):
    index = tmp_path / "index.jsonl"
    ref = "wanted:call"
    index.write_text(json.dumps({"scoped_call_id": ref, "path": ""}) + "\n"
                     + '{"bad":' + "1" * 5000 + '}\n')
    cache = reader._VerifiedArtifactCache(4096)
    monkeypatch.setattr(reader, "_ARTIFACT_INDEX_CACHE", cache)
    with pytest.raises(ValueError):
        reader._parsed_artifact_index(index, literal_ref=ref)
    assert not cache.entries and cache.bytes == 0


def test_literal_and_full_results_use_separate_shared_budget_entries(tmp_path, monkeypatch):
    index = tmp_path / "index.jsonl"
    refs = ["wanted:one", "wanted:two"]
    records = [{"scoped_call_id": ref, "path": ""} for ref in refs]
    index.write_text("".join(json.dumps(row) + "\n" for row in records)
                     + json.dumps({"pad": "x" * 8192}) + "\n")
    cache = reader._VerifiedArtifactCache(4096)
    monkeypatch.setattr(reader, "_ARTIFACT_INDEX_CACHE", cache)
    results = [reader._parsed_artifact_index(index, literal_ref=ref) for ref in refs]
    assert [result[0] for result in results] == [(records[0],), (records[1],)]
    for ref, result in zip(refs, results):
        assert reader._parsed_artifact_index(index, literal_ref=ref) is result
    assert 0 < cache.bytes <= 4096 and len(cache.entries) == 2
    full = reader._parsed_artifact_index(index)
    assert len(full[0]) == 3
    assert cache.bytes <= 4096 and len(cache.entries) == 2


def test_concurrent_oversized_literal_cache_keeps_ref_partitions(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    index = tmp_path / "index.jsonl"
    refs = ["wanted:one", "wanted:two"]
    index.write_text("".join(json.dumps({"scoped_call_id": ref, "path": ""}) + "\n" for ref in refs)
                     + json.dumps({"pad": "x" * 8192}) + "\n")
    cache = reader._VerifiedArtifactCache(4096)
    monkeypatch.setattr(reader, "_ARTIFACT_INDEX_CACHE", cache)
    original = reader.iter_decoded_tool_index_objects
    barrier = threading.Barrier(2)

    def simultaneous(*args, **kwargs):
        barrier.wait(timeout=5)
        yield from original(*args, **kwargs)

    monkeypatch.setattr(reader, "iter_decoded_tool_index_objects", simultaneous)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda ref: reader._parsed_artifact_index(index, literal_ref=ref), refs))
    for ref, result in zip(refs, results):
        assert [row["scoped_call_id"] for row in result[0]] == [ref]
        assert reader._parsed_artifact_index(index, literal_ref=ref) is result
    assert cache.bytes <= 4096 and len(cache.entries) == 2
