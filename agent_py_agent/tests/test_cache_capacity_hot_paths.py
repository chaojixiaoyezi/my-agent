"""用真实文件与构建计数验证跨工作区缓存命中、容量下限及推导预算。"""
from __future__ import annotations

import json
from collections import OrderedDict
from copy import copy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.conversation import message_selection as selection
from agent_py_agent.agent.tooling import _filesystem_find as find
from agent_py_agent.agent.user_space import managed_user_governance as governance

try:
    from agent_py_agent.agent.common import cache_capacity
except ImportError:
    cache_capacity = None


class _CapacityContractStub:
    """R0 尚未合入时提供消费方测试替身；R0 可用时用同一组测试验证真实实现。"""

    def configure_from_config(self, config):
        self.owners = config.owner_agent_pool_max_agents
        self.sessions = config.gateway_user_inflight_limit + config.background_admin_threads_per_owner

    def resident_owners(self, floor):
        return max(floor, self.owners)

    def owner_sessions(self, floor):
        return max(floor, self.sessions)

    def reset_for_tests(self):
        self.owners = self.sessions = 0


@pytest.fixture(autouse=True)
def isolated_caches(monkeypatch):
    monkeypatch.setattr(governance, "_COMPACT_POLICY_CACHE", OrderedDict())
    monkeypatch.setattr(selection, "_POSITION_CACHE", OrderedDict())


@pytest.fixture
def capacity(monkeypatch):
    module = cache_capacity if cache_capacity is not None else _CapacityContractStub()
    module.reset_for_tests()
    for consumer in (governance, selection, find):
        monkeypatch.setattr(consumer, "cache_capacity", module)
    yield module
    module.reset_for_tests()


def _configure_capacity(capacity, *, owners=8, sessions=50, background=64):
    capacity.configure_from_config(SimpleNamespace(
        owner_agent_pool_max_agents=owners,
        gateway_owner_inflight_limit=0,
        gateway_user_inflight_limit=sessions,
        background_threads_per_owner=0,
        background_admin_threads_per_owner=background,
        max_subagents=8,
        gateway_global_inflight_limit=500,
    ))


def _home(root, owner):
    return SimpleNamespace(root=root, owner_provider="feishu", owner_kind="user", owner_id=owner)


def _transcript(path: Path, *, count=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps({
        "message_id": f"message-{index}", "thread_id": path.stem,
        "role": "user", "content": "缓存计数样本", "metadata": {},
    }, ensure_ascii=False) + "\n" for index in range(count)), encoding="utf-8")
    return path, path.stat().st_size


@pytest.mark.parametrize("owners,workspaces", [(1, 100), (8, 1), (8, 100)])
def test_compact_and_message_caches_stay_warm_across_workspace_rounds(
    tmp_path, monkeypatch, capacity, owners, workspaces,
):
    _configure_capacity(capacity, owners=max(8, owners * workspaces))
    root = tmp_path / "host"
    root.mkdir()
    homes = [_home(root, f"owner-{owner}") for owner in range(owners)]
    cases = [(home, *_transcript(tmp_path / home.owner_id / f"workspace-{workspace}" / "messages.jsonl"))
             for home in homes for workspace in range(workspaces)]
    read = Mock(wraps=governance._read_compact_governance)
    parse = Mock(wraps=selection._position_line)
    monkeypatch.setattr(governance, "_read_compact_governance", read)
    monkeypatch.setattr(selection, "_position_line", parse)

    for turn in range(3):
        reads_before, parses_before = read.call_count, parse.call_count
        for home, path, through in cases:
            snapshot = governance.read_compact_governance_snapshot(home)
            assert snapshot == governance.CompactGovernanceSnapshot()
            index = selection.message_position_index(path, through)
            assert len(index.entries) == 1 and not index.errors
        if turn == 0:
            assert read.call_count == owners
            assert parse.call_count == len(cases)
        else:
            assert 1 - (read.call_count - reads_before) / len(cases) >= 0.99
            assert 1 - (parse.call_count - parses_before) / len(cases) >= 0.99


@pytest.mark.parametrize("configured", [False, True])
def test_compact_cache_keeps_original_floor_and_uses_configured_owner_capacity(
    tmp_path, monkeypatch, capacity, configured,
):
    floor = governance._COMPACT_POLICY_CACHE_MAX
    if configured:
        _configure_capacity(capacity, owners=floor + 1)
    homes = [_home(tmp_path, f"owner-{index}") for index in range(floor + 1)]
    read = Mock(wraps=governance._read_compact_governance)
    monkeypatch.setattr(governance, "_read_compact_governance", read)
    for home in homes:
        governance.read_compact_governance_snapshot(home)
    assert len(governance._COMPACT_POLICY_CACHE) == floor + int(configured)
    assert capacity.resident_owners(floor) == floor + int(configured)
    governance.read_compact_governance_snapshot(homes[-1])
    assert read.call_count == len(homes)
    governance.read_compact_governance_snapshot(homes[0])
    assert read.call_count == len(homes) + int(not configured)


@pytest.mark.parametrize("configured", [False, True])
def test_position_cache_keeps_original_floor_and_uses_configured_file_capacity(
    tmp_path, monkeypatch, capacity, configured,
):
    floor = selection._POSITION_CACHE_MAX_FILES
    if configured:
        _configure_capacity(capacity, owners=floor + 1)
    sources = [_transcript(tmp_path / f"thread-{index}.jsonl") for index in range(floor + 1)]
    parse = Mock(wraps=selection._position_line)
    monkeypatch.setattr(selection, "_position_line", parse)
    for path, through in sources:
        selection.message_position_index(path, through)
    assert len(selection._POSITION_CACHE) == floor + int(configured)
    assert capacity.resident_owners(floor) == floor + int(configured)
    selection.message_position_index(*sources[-1])
    assert parse.call_count == len(sources)
    selection.message_position_index(*sources[0])
    assert parse.call_count == len(sources) + int(not configured)


@pytest.mark.parametrize("configured", [False, True])
def test_position_entry_budget_scales_with_file_capacity(tmp_path, monkeypatch, capacity, configured):
    # 缩小测试数据预算，真实解析边界内外的文件，不构造十几万条消息。
    monkeypatch.setattr(selection, "_POSITION_CACHE_MAX_FILES", 2)
    monkeypatch.setattr(selection, "_POSITION_CACHE_MAX_ENTRIES", 2)
    if configured:
        _configure_capacity(capacity, owners=4)
    limit = 4 if configured else 2
    retained = selection.message_position_index(*_transcript(tmp_path / "retained.jsonl", count=limit))
    assert retained.identity in selection._POSITION_CACHE
    oversized = selection.message_position_index(*_transcript(tmp_path / "oversized.jsonl", count=limit + 1))
    assert len(oversized.entries) == limit + 1
    assert oversized.identity not in selection._POSITION_CACHE


@pytest.mark.parametrize("owners,workspaces", [(1, 100), (8, 1)])
def test_owner_shared_find_snapshots_survive_workspace_rotation(
    tmp_path, monkeypatch, capacity, owners, workspaces,
):
    _configure_capacity(capacity)
    cases = []
    for owner in range(owners):
        root = tmp_path / f"owner-{owner}"
        root.mkdir()
        tool = find.FindFilesTool(root, max_matches=1)
        for workspace in range(workspaces):
            path = root / f"workspace-{workspace}"
            path.mkdir()
            for number in range(3):
                (path / f"{number}.py").write_text("sample", encoding="utf-8")
            cases.append((tool, path))
    builds = []
    original = find.FindFilesTool._build_snapshot

    def build(tool, target, request):
        builds.append(target)
        return original(tool, target, request)

    monkeypatch.setattr(find.FindFilesTool, "_build_snapshot", build)
    cursors, snapshot_ids = {}, {}
    for turn in range(3):
        before = len(builds)
        for tool, path in cases:
            params = {"path": str(path), "pattern": "*.py", "snapshot": True, "limit": 1}
            if turn:
                params["cursor"] = cursors[path]
            # Registry 请求级浅拷贝会共享原 owner 的快照容器。
            result = copy(tool).execute(params)
            assert result.ok, result.output
            snapshot = result.result_envelope["snapshot"]
            assert not snapshot["partial"]
            if turn:
                assert snapshot["snapshot_id"] == snapshot_ids[path]
            snapshot_ids[path] = snapshot["snapshot_id"]
            cursors[path] = snapshot["next_cursor"]
            assert result.output.splitlines()[0].endswith(f"{turn}.py")
        if turn == 0:
            assert len(builds) == len(cases)
        else:
            assert 1 - (len(builds) - before) / len(cases) >= 0.99


def test_find_snapshot_capacity_uses_original_floor_without_registration(tmp_path, capacity):
    tool = find.FindFilesTool(tmp_path, max_matches=1)
    cursors = []
    for index in range(find._FIND_SNAPSHOT_MAX_CACHED + 1):
        path = tmp_path / f"workspace-{index}"
        path.mkdir()
        for number in range(2):
            (path / f"{number}.py").write_text("sample", encoding="utf-8")
        result = tool.execute({"path": str(path), "pattern": "*.py", "snapshot": True, "limit": 1})
        assert result.ok
        cursors.append((path, result.result_envelope["snapshot"]["next_cursor"]))
    assert len(tool._snapshots) == find._FIND_SNAPSHOT_MAX_CACHED
    assert capacity.owner_sessions(find._FIND_SNAPSHOT_MAX_CACHED) == find._FIND_SNAPSHOT_MAX_CACHED
    for path, cursor in (cursors[-1], cursors[0]):
        result = tool.execute({"path": str(path), "pattern": "*.py", "cursor": cursor, "limit": 1})
        assert result.ok == (cursor == cursors[-1][1])


@pytest.mark.parametrize("component", ["paths", "watches"])
@pytest.mark.parametrize("configured", [False, True])
def test_find_snapshot_byte_budgets_scale_with_session_capacity(
    tmp_path, monkeypatch, capacity, component, configured,
):
    if configured:
        _configure_capacity(capacity, sessions=8, background=0)
    if component == "paths":
        paths = [tmp_path / f"{index}.py" for index in range(3)]
        for path in paths:
            path.write_text("sample", encoding="utf-8")
        # 扫描器先观察所有候选，再保留匹配路径；只让后一项预算触顶。
        budget = sum(len(str(path).encode("utf-8")) + 256 for path in (tmp_path, *paths))
        budget += len(str(paths[0]).encode("utf-8")) + 64
    else:
        for name in ("a", "b"):
            (tmp_path / name).mkdir()
        budget = len(str(tmp_path).encode("utf-8")) + len(str(tmp_path / "a").encode("utf-8")) + 512
    monkeypatch.setattr(find, "_FIND_SNAPSHOT_MAX_BYTES", budget)
    result = find.FindFilesTool(tmp_path, max_matches=10).execute({
        "pattern": "*.py", "snapshot": True, "limit": 10,
    })
    assert result.ok
    snapshot = result.result_envelope["snapshot"]
    assert snapshot["scan_exhausted"] == configured
    assert snapshot["partial"] == (not configured)
    assert snapshot["limit_reason"] == ("" if configured else "manifest_byte_limit")


def test_missing_capacity_dependency_keeps_original_limits(tmp_path, monkeypatch):
    for consumer in (governance, selection, find):
        monkeypatch.setattr(consumer, "cache_capacity", None)
    monkeypatch.setattr(governance, "_COMPACT_POLICY_CACHE_MAX", 2)
    monkeypatch.setattr(selection, "_POSITION_CACHE_MAX_FILES", 2)
    monkeypatch.setattr(selection, "_POSITION_CACHE_MAX_ENTRIES", 2)
    monkeypatch.setattr(find, "_FIND_SNAPSHOT_MAX_CACHED", 2)
    tool = find.FindFilesTool(tmp_path, max_matches=1)
    for index in range(3):
        governance.read_compact_governance_snapshot(_home(tmp_path, f"owner-{index}"))
        path, through = _transcript(tmp_path / f"workspace-{index}" / "messages.jsonl")
        selection.message_position_index(path, through)
        result = tool.execute({"path": str(path.parent), "pattern": "*.jsonl", "snapshot": True})
        assert result.ok
    assert len(governance._COMPACT_POLICY_CACHE) == 2
    assert len(selection._POSITION_CACHE) == 2
    assert len(tool._snapshots) == 2
    oversized = selection.message_position_index(*_transcript(tmp_path / "oversized.jsonl", count=3))
    assert oversized.identity not in selection._POSITION_CACHE
