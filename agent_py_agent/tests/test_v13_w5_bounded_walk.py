"""W5：预算约束的是实际目录访问，不只是最终返回数。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_py_agent.agent.agent_core import artifact_locator
from agent_py_agent.agent.contracts import contract_status, real_run_review
from agent_py_agent.agent.subagents import result_artifact_evidence


def _observer(monkeypatch):
    original = os.scandir
    visited = []

    def observe(path):
        visited.append(Path(path))
        return original(path)

    monkeypatch.setattr(os, "scandir", observe)
    return visited


def _walk():
    from agent_py_agent.agent.common import bounded_walk as module
    return module


def test_walk_prunes_before_entering_and_preserves_open_world(tmp_path, monkeypatch):
    for name in (".git", "__pycache__", "Library", "node_modules", "ordinary"):
        child = tmp_path / name
        child.mkdir()
        (child / "report.json").write_text("{}")
    visited = _observer(monkeypatch)
    module = _walk()
    result = module.bounded_walk(tmp_path, include=lambda entry: entry.name.endswith(".json"))
    assert {path.parent.name for path in result.paths} == {"Library", "node_modules", "ordinary"}
    assert not any(path.name in {".git", "__pycache__"} for path in visited)
    assert result.truncated is False
    assert result.coverage["stop_reason"] == "complete"
    assert result.coverage["visited_dirs"] == len(visited)
    assert result.coverage["pruned_dirs"] == 2


@pytest.mark.parametrize("budget,reason", [
    ({"max_entries": 1}, "max_entries"),
    ({"max_dirs": 1}, "max_dirs"),
    ({"max_depth": 0}, "max_depth"),
    ({"deadline_seconds": 0}, "deadline"),
])
def test_walk_reports_each_budget(tmp_path, budget, reason):
    (tmp_path / "child").mkdir()
    (tmp_path / "child" / "report.json").write_text("{}")
    result = _walk().bounded_walk(tmp_path, include=lambda entry: entry.name.endswith(".json"), **budget)
    assert result.truncated is True
    assert result.coverage["stop_reason"] == reason
    assert result.coverage["elapsed_ms"] >= 0
    if "max_entries" in budget:
        assert result.coverage["visited_entries"] <= budget["max_entries"]
    if "max_dirs" in budget:
        assert result.coverage["visited_dirs"] <= budget["max_dirs"]


def test_walk_does_not_follow_directory_symlinks(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "report.json").write_text("{}")
    root = tmp_path / "root"
    root.mkdir()
    (root / "alias").symlink_to(outside, target_is_directory=True)
    visited = _observer(monkeypatch)
    result = _walk().bounded_walk(root, include=lambda entry: entry.name.endswith(".json"))
    assert result.paths == ()
    assert visited == [root]


@pytest.mark.parametrize("noise,old_calls", [(10, 30), (100, 210), (500, 1010)])
@pytest.mark.parametrize("max_files", [0, 1])
def test_json_paths_scandir_reproduction(tmp_path, monkeypatch, noise, old_calls, max_files):
    for number in range(noise):
        parent = tmp_path / ("ordinary", ".git", "__pycache__")[number % 3] / str(number)
        parent.mkdir(parents=True)
        (parent / "noise.json").write_text("{}")
    (tmp_path / "first.json").write_text("{}")
    (tmp_path / "second.json").write_text("{}")
    visited = _observer(monkeypatch)
    result = contract_status._json_paths(tmp_path, max_files=max_files)
    assert len(result) == max_files
    assert len(visited) < old_calls
    assert not any(".git" in path.parts or "__pycache__" in path.parts for path in visited)


def test_status_and_review_propagate_incomplete_candidates(tmp_path, monkeypatch):
    (tmp_path / "report.json").write_text('{"ok": true}')
    module = _walk()
    partial = module.BoundedWalkResult(
        paths=(tmp_path / "report.json",), truncated=True,
        coverage={"stop_reason": "max_entries", "visited_entries": 1, "visited_dirs": 1,
                  "pruned_dirs": 0, "elapsed_ms": 0.0},
    )
    monkeypatch.setattr(contract_status, "bounded_walk", lambda *args, **kwargs: partial)
    monkeypatch.setattr(real_run_review, "bounded_walk", lambda *args, **kwargs: partial)
    status = contract_status.summarize_contract_status(tmp_path).to_dict()
    review = real_run_review.review_real_run_directory(tmp_path).to_dict()
    assert status["incomplete"] is True
    assert status["coverage"]["stop_reason"] == "max_entries"
    assert review["candidate_coverage_incomplete"] is True
    assert review["final_status"] == "UNKNOWN"


def test_locator_preserves_early_result_limit(tmp_path, monkeypatch):
    (tmp_path / "hit.txt").write_text("yes")
    for number in range(30):
        (tmp_path / str(number)).mkdir()
    visited = _observer(monkeypatch)
    result = artifact_locator._candidate_paths_under_root(tmp_path, (".txt",), remaining=1)
    assert result == [(tmp_path / "hit.txt").resolve()]
    assert len(visited) == 1


def test_locator_no_match_is_bounded(tmp_path, monkeypatch):
    module = _walk()
    original = module.bounded_walk
    observed = []

    def small_budget(root, **kwargs):
        result = original(root, max_entries=3, **kwargs)
        observed.append(result)
        return result

    for number in range(8):
        (tmp_path / str(number)).mkdir()
    monkeypatch.setattr(artifact_locator, "bounded_walk", small_budget)
    result = artifact_locator.locate_artifact({"kind": "txt", "search_roots": [str(tmp_path)]}, tmp_path)
    assert result.path is None
    assert result.findings[0]["code"] == "ARTIFACT_LOCATOR_INCOMPLETE"
    assert observed[0].coverage["visited_entries"] <= 3


def test_suffix_first_match_is_not_unique_after_truncation(tmp_path, monkeypatch):
    module = _walk()
    found = tmp_path / "result.txt"
    found.write_text("x")
    partial = module.BoundedWalkResult((found,), True, {"stop_reason": "max_dirs"})
    monkeypatch.setattr(result_artifact_evidence, "bounded_walk", lambda *args, **kwargs: partial)
    assert result_artifact_evidence._resolve_by_suffix(Path("result.txt"), [tmp_path]) is None


def test_suffix_unique_match_when_coverage_complete(tmp_path):
    found = tmp_path / "nested" / "result.txt"
    found.parent.mkdir()
    found.write_text("x")
    assert result_artifact_evidence._resolve_by_suffix(Path("result.txt"), [tmp_path]) == found
