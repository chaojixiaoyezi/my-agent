"""引用保护按路径深度匹配，来源写入后不得沿用旧投影。"""
from pathlib import Path

import pytest

from agent_py_agent.agent.memory_store import retention_scan as scan
from agent_py_agent.tests.test_memory_retention_wave5 import NOW, old_file, runtime


@pytest.mark.parametrize("pin,target,protected", [
    ("a/b", "a/b", True), ("a/b", "a", True),
    ("a/b", "a/b/c", True), ("a/b", "a/bb", False),
])
def test_path_boundaries_and_writers(tmp_path, pin, target, protected):
    pins = scan._RetentionPins(tmp_path)
    pins.collect({"path": str(tmp_path / pin)}, tmp_path / "source.json")
    assert pins.protects(tmp_path / target, []) is protected
    pins.paths = {tmp_path / "replacement"}
    assert not pins.protects(tmp_path / "a/b", [])
    pins.paths.clear()
    assert not pins.protects(tmp_path / "replacement", [])
    pins.collect({"path": str(tmp_path / "later")}, tmp_path / "source.json")
    assert pins.protects(tmp_path / "later/child", [])
    recovered = scan._RetentionPins(tmp_path, paths={tmp_path / "recovered"})
    pins.complete, pins.target_rebuild = False, lambda errors: (recovered, True)
    assert pins.complete_for(tmp_path / "recovered", [])
    assert pins.protects(tmp_path / "recovered/child", [])


@pytest.mark.parametrize("count", [100, 1000, 10000])
def test_lookup_never_compares_history_paths(tmp_path, monkeypatch, count):
    pins = scan._RetentionPins(tmp_path, paths={tmp_path / "pins" / str(i) for i in range(count)})
    equal, resolve = Path.__eq__, Path.resolve
    calls = {"equal": 0, "resolve": 0}

    def compared(path, other):
        calls["equal"] += 1
        return equal(path, other)

    def resolved(path, *args, **kwargs):
        calls["resolve"] += 1
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "__eq__", compared)
    monkeypatch.setattr(Path, "resolve", resolved)
    for index in range(20):
        assert not pins.protects(tmp_path / f"missing-{index}", [])
    assert calls == {"equal": 0, "resolve": 20}


@pytest.mark.parametrize("kind", ["link", "unreadable"])
def test_links_and_unreadable_sources_keep_protection(tmp_path, monkeypatch, kind):
    home, _candidates, _service = runtime(tmp_path)
    source = old_file(home.owner_memory_curator_runs_dir / "pins.jsonl", '{"source_refs": ["evidence/a"]}\n')
    if kind == "link":
        target = tmp_path / "target"
        target.touch()
        link = tmp_path / "link"
        link.symlink_to(target)
        pins = scan._RetentionPins(tmp_path, paths={target})
        assert pins.protects(link, [])
    else:
        original = scan._fingerprint_source

        def unreadable(path):
            if path == source:
                raise PermissionError("single unreadable pin source")
            return original(path)

        monkeypatch.setattr(scan, "_fingerprint_source", unreadable)
        errors = []
        pins, complete = scan.current_retention_pins(home, (), errors, now=NOW)
        assert not complete and not pins.complete and errors


def test_shared_ancestor_counts_survive_remove_and_same_size_update(tmp_path):
    pins = scan._RetentionPins(tmp_path, paths={tmp_path / "a/x", tmp_path / "a/y"})
    pins.paths.discard(tmp_path / "a/x")
    assert pins.protects(tmp_path / "a", [])
    pins.paths -= {tmp_path / "a/y"}
    pins.paths |= {tmp_path / "b/x"}
    assert not pins.protects(tmp_path / "a", []) and pins.protects(tmp_path / "b", [])
    pins.paths.pop()
    assert not pins.protects(tmp_path / "b", [])
