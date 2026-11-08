"""当前来源窗口不随历史目录增长，恢复仍保留原身份与不完整事实。"""
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RuntimeContextRequest
from agent_py_agent.agent.agent_core.runtime.loop_support import build_runtime_main_context_bundle
from agent_py_agent.agent.common import tool_output_paths
from agent_py_agent.agent.memory_archive import compact_tool_output_refs as refs
from agent_py_agent.agent.tooling.models import ToolRuntimeSnapshot
from agent_py_agent.agent.user_space import context_bundle as bundles
from agent_py_agent.agent.user_space.context_bundle_artifacts import (
    MainContextBundleArtifactUpdateRequest,
    _tool_index_window,
    update_main_context_bundle_artifacts,
)
from agent_py_agent.tests.test_r5db_closeout_tool_index_window import (
    SCOPE,
    _externalize,
    _history,
    _mark,
    _update,
    ensure_my_agent_home,
)


def test_current_two_indexes_have_constant_filesystem_work(tmp_path, record_property):
    owner, task = tmp_path / "owner", tmp_path / "owner/runs/live/current/work"
    paths = (_history(owner, 1), _history(task, 1))
    marks = tool_output_paths.tool_output_index_marks(owner, index_paths=paths)
    for root in (owner, task):
        _externalize(root, SCOPE, "now")
    counts = []
    for history in (0, 100, 1000, 10000):
        for number in range(history):
            (owner / "runs/history" / str(number) / "work/blobs/tool_outputs").mkdir(parents=True, exist_ok=True)
        with (patch.object(Path, "glob", side_effect=AssertionError("historical discovery")),
              patch.object(Path, "resolve", autospec=True, side_effect=Path.resolve) as resolved,
              patch.object(os, "stat", wraps=os.stat) as stated,
              patch.object(os, "scandir", wraps=os.scandir) as enumerated):
            assert len(refs.tool_output_source_refs(owner, SCOPE, marks=marks, index_paths=paths)) == 2
        counts.append((resolved.call_count, stated.call_count, enumerated.call_count))
    assert counts == [counts[0]] * len(counts)
    assert counts[0][2] == 0
    record_property("current_index_filesystem_counts", json.dumps(dict(zip((0, 100, 1000, 10000), counts))))


@pytest.mark.parametrize("mutation", ["append", "replace", "truncate", "unaligned"])
def test_explicit_sources_keep_verified_offsets_and_attempt_identity(tmp_path, mutation):
    index = _history(tmp_path, 10)
    marks = _mark(index, size_delta=-1 if mutation == "unaligned" else 0)
    if mutation == "replace":
        replacement = index.with_suffix(".replacement")
        replacement.write_bytes(index.read_bytes())
        replacement.replace(index)
    if mutation == "truncate":
        index.write_bytes(b"")
    for attempt in ("first", "second"):
        _externalize(tmp_path, {**SCOPE, "attempt_id": attempt, "turn_id": "turn"}, "same-call")
    assert len(refs.tool_output_source_refs(tmp_path, SCOPE, marks=marks, index_paths=(index,))) == 2
    carried = refs.carried_tool_call_records(tmp_path, SCOPE, index_paths=(index,))
    assert [row["attempt_id"] for row in carried] == ["first", "second"]
    with pytest.raises(FileNotFoundError):
        refs.tool_source_refs(tmp_path, SCOPE, index_paths=(index.with_name("missing.jsonl"),))


def test_runtime_bundle_keeps_task_source_and_first_window_through_compactions(tmp_path):
    home = ensure_my_agent_home(tmp_path / "home")
    owner, task_root = home.owner_home_dir, home.owner_home_dir / "runs/arbitrary/physical-location"
    task = task_root / "work"
    _history(task, 2)
    agent = SimpleNamespace(root=owner, home_paths=home, config=SimpleNamespace(auto_save_memory=True),
                            _current_run_task_workspace=str(task_root))
    request = RuntimeContextRequest("继续", None, None, **SCOPE, task_attributes={})
    snapshot = ToolRuntimeSnapshot(run_id=SCOPE["run_id"], runtimes=(), available_tool_names=frozenset(),
                                   unavailable_tools=(), allowed_tools=None)
    options = dict(memories=[], runtime_injections=[], routed_context=SimpleNamespace(),
                   resume_context_injected=False, task_local=False, tool_runtime_snapshot=snapshot)
    first = build_runtime_main_context_bundle(agent, request, **options)
    marks = first.bundle["tool_index_marks"]
    assert set(marks) == {str(tool_output_paths.tool_output_root(root) / "index.jsonl") for root in (owner, task)}
    for attempt in ("first", "second"):
        _externalize(task, {**SCOPE, "attempt_id": attempt, "turn_id": "turn"}, "same-call")
    _externalize(task, {**SCOPE, "request_id": "foreign"}, "other-scope")
    assert _update(owner, Path(first.json_path))["artifact_count"] == 2
    second = build_runtime_main_context_bundle(agent, request, **options)
    assert second.bundle["tool_index_marks"] == marks
    payload = json.loads(Path(second.json_path).read_text())
    with patch.object(bundles, "read_json_object_report", return_value=SimpleNamespace(payload=payload)):
        for _ in range(10000):
            carried = bundles._carried_tool_index_window(second.bundle, Path(second.json_path))
            assert carried["tool_index_marks"] == marks and carried["tool_index_sources_complete"] is True
    assert len(_tool_index_window(Path(second.json_path), carried)["index_paths"]) == 2
    Path(second.json_path).write_text("{")
    third = build_runtime_main_context_bundle(agent, request, **options)
    with patch.object(Path, "glob", side_effect=AssertionError("historical fallback")):
        assert _update(owner, Path(third.json_path))["status"] == "incomplete_tool_index_sources"


def test_missing_owner_index_is_reported_without_losing_task_records(tmp_path):
    task = tmp_path / "runs/arbitrary/task/work"
    _externalize(task, SCOPE, "task-only")
    sources = (refs.CarriedIndexSource(tmp_path, "owner_index"), refs.CarriedIndexSource(task, "task_index"))
    read = refs.carried_tool_call_records_for_requests(sources, [SCOPE["request_id"]])
    assert [row["call_id"] for row in read.records] == ["task-only"]
    assert read.unreadable_sources == ({"source": "owner_index", "error_type": "FileNotFoundError"},)


def test_closeout_keeps_host_bound_task_promotion_in_the_same_turn(tmp_path):
    _history(tmp_path, 1)
    bundle = bundles.build_main_context_bundle(bundles.MainContextBundleRequest(
        root=tmp_path, home_paths=ensure_my_agent_home(tmp_path / "home"), user_prompt="继续", **SCOPE,
        tool_index_sources=(refs.CarriedIndexSource(tmp_path, "owner_index"),),
    ))
    promoted_scope = {**SCOPE, "task_id": "promoted-task"}
    _externalize(tmp_path, promoted_scope, "promoted-call")
    result = update_main_context_bundle_artifacts(MainContextBundleArtifactUpdateRequest(
        context_bundle_path=bundle.json_path, workspace_root=tmp_path, **promoted_scope,
    ))
    assert result["ok"] is True and result["artifact_count"] == 1
