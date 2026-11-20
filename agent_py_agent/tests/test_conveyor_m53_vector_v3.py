import json
import shutil
import struct
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import perf_counter

import pytest

from agent_py_agent.agent.retrieval import vector_store
from agent_py_agent.agent.retrieval.vector_store import VECTOR_STORE_SCHEMA_VERSION, VectorStore


@pytest.fixture(scope="module")
def large_snapshot(tmp_path_factory):
    path = tmp_path_factory.mktemp("large-vector-store") / "vectors"
    vec = [((i % 7) - 3) / 64 for i in range(64)]
    VectorStore(path).replace_all([(f"id-{i:05}", vec, "", {}) for i in range(50_000)])
    return path


def test_cold_load_memory_bound(large_snapshot):
    # 用新进程取向量装载峰值，避免建库过程污染 RSS。
    source = """
import json
try:
    import resource
except ImportError:
    resource = None
import sys
import tracemalloc
from agent_py_agent.agent.retrieval.vector_store import VectorStore
tracemalloc.start()
store = VectorStore(sys.argv[1])
assert len(store) == 50000
print(json.dumps({"peak": tracemalloc.get_traced_memory()[1], "rss": (
    resource.getrusage(resource.RUSAGE_SELF).ru_maxrss if resource is not None else None)}))
"""
    flags = [] if getattr(sys, "_is_gil_enabled", lambda: True)() else ["-X", "gil=0"]
    child = subprocess.run(
        [sys.executable, *flags, "-c", source, str(large_snapshot)],
        capture_output=True, text=True, check=True, timeout=45,
    )
    values = json.loads(child.stdout)
    print(values)
    assert values["peak"] <= 35 * 1024 * 1024, values


def test_remove_many_writes_only_tombstones(large_snapshot, tmp_path):
    path = tmp_path / "vectors"
    shutil.copy2(large_snapshot, path)
    store = VectorStore(path)
    before = path.stat()
    start = perf_counter()
    assert store.remove_many(["id-00000"]) == {"id-00000"}
    elapsed = perf_counter() - start
    after = path.stat()
    written = after.st_size - before.st_size if before.st_ino == after.st_ino else after.st_size
    print(f"remove bytes={written}, elapsed={elapsed:.6f}")
    assert 0 < written < 1024, written
    assert "id-00000" not in VectorStore(path).ids()


@pytest.mark.parametrize("legacy_version", [1, 2])
def test_legacy_top_k_matches_v3_with_ties_and_min_score(tmp_path, legacy_version):
    path = tmp_path / "vectors"
    items = {
        "b": {"vector": [1.0, 0.25], "text": "b", "metadata": {"id": 2}},
        "a": {"vector": [1.0, 0.25], "text": "a", "metadata": {"id": 1}},
        "c": {"vector": [0.5, 0.75], "text": "c", "metadata": {}},
        "d": {"vector": [-1.0, 0.0], "text": "d", "metadata": {}},
    }
    identity = {"profile": "fixture"}
    legacy = items if legacy_version == 1 else {
        "schema_version": VECTOR_STORE_SCHEMA_VERSION, "identity": identity,
        "dim": 2, "generation": "fixture-gen", "items": items,
    }
    before = json.dumps(legacy).encode()
    path.write_bytes(before)
    store = VectorStore(path, identity if legacy_version == 2 else None)
    hits = store.search([1.0, 0.0], min_score=0.5, top_k=3)
    assert [(h.id, h.score) for h in hits] == [("a", 1.0), ("b", 1.0), ("c", 0.5)]
    assert store.search([1.0, 0.0], min_score=0.5, top_k=2) == hits[:2]
    assert path.read_bytes() == before
    store.replace_all([(key, item["vector"], item["text"], item["metadata"]) for key, item in items.items()])
    loaded = VectorStore(path, identity if legacy_version == 2 else None)
    assert loaded.search([1.0, 0.0], min_score=0.5, top_k=3) == hits


def test_v3_writes_little_endian_vector_and_norm_blocks(tmp_path):
    path = tmp_path / "vectors"
    identity = {"profile": "known"}
    store = VectorStore(path, identity)
    store.replace_all([("a", [3.0, 4.0], "", {}), ("b", [0.0, -2.0], "", {})])
    with path.open("rb") as handle:
        header = json.loads(handle.readline())
        assert {k: header[k] for k in ("v", "dim", "count")} == {"v": 3, "dim": 2, "count": 2}
        assert header["space_id"]
        assert [json.loads(handle.readline())[0] for _ in range(2)] == ["a", "b"]
        assert handle.read() == struct.pack("<ffffff", 3.0, 4.0, 0.0, -2.0, 5.0, 2.0)
    before = path.read_bytes()
    path.write_bytes(before[:-1])
    assert VectorStore(path, identity).identity_status() == (False, "VECTOR_STORE_UNREADABLE")


def test_shared_reader_keeps_snapshot_during_tombstone_and_append(large_snapshot, tmp_path, monkeypatch):
    path = tmp_path / "vectors"
    shutil.copy2(large_snapshot, path)
    store = VectorStore(path)
    resident = VectorStore(path)
    captured, resume = Event(), Event()
    original_islice = vector_store.islice

    def pause_once(*args):
        if not captured.is_set():
            captured.set()
            assert resume.wait(10)
        return original_islice(*args)

    monkeypatch.setattr(vector_store, "islice", pause_once)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(store.search, [0.0] * 64, top_k=1)
        try:
            assert captured.wait(10)
            assert store.remove_many(["id-00000"]) == {"id-00000"}
            store.upsert("id-added", [1.0] * 64)
            assert "id-00000" not in resident.ids() and "id-added" in resident.ids()
        finally:
            resume.set()
        assert [hit.id for hit in future.result(timeout=10)] == ["id-00000"]
    assert store.remove_many(["id-00000"], purge=True) == set()
    assert b"id-00000" not in path.read_bytes()
