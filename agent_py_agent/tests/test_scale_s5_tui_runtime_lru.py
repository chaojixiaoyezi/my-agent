"""展示 runtime 的有界缓存、导航保护与回收后重放回归。"""
from __future__ import annotations

import threading
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.settings.config_io import load_simple_yaml
from agent_py_agent.agent.settings.parameter_registry import parameter_registry
from agent_py_agent.agent.settings.user_config_capability import EFFECT_NEXT_SESSION, TUNABLE_KEYS
from agent_py_agent.cli.chat_parts import tui_agent_navigation as navigation
from agent_py_agent.cli.chat_parts.tui_background_agents import TuiBackgroundAgents
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
from agent_py_agent.tests.test_tui_agent_navigation import _goal, _row
from agent_py_agent.tests.test_v15_w11_tui_navigation import (
    InlineThread,
    NavigationClient,
    agent_row,
)


def _view(run_id, *, parent=""):
    return {
        "ok": True,
        "agent": {**_row(run_id, parent=parent, status="DONE", lifecycle_phase="terminal"),
                  "goal": f"查看 {run_id} 的任务"},
        "terminal": True,
        "children": [],
        "goals": [_goal(f"goal-{run_id}")],
        "transcript_events": [],
        "event_cursor": 11,
        "final_response": f"{run_id} 已完成",
        "thread_id": f"thread-{run_id}",
        "final_response_message_id": f"final-{run_id}",
    }


def _open(nav, run_id, *, parent=""):
    nav.update_rows(parent, [_row(run_id, parent=parent)])
    nav.clear_selection()
    assert nav.move_selection(1)
    if nav.snapshot().selected_run_id != run_id:
        assert nav.move_selection(1)
    assert nav.snapshot().selected_run_id == run_id
    assert nav.enter_selected()
    runtime = nav.active_runtime()
    assert nav.apply_agent_view(run_id, _view(run_id, parent=parent))
    return runtime


@pytest.mark.parametrize("visited", [6, 600])
def test_runtime_cap_cleans_only_evicted_view_state(visited):
    nav = navigation.TuiAgentNavigationState(TuiRuntime("bounded-views"), max_cached_runtimes=3)
    run_ids = [f"run-{number}" for number in range(visited)]
    for run_id in run_ids:
        runtime = _open(nav, run_id)
        assert nav._runtimes[run_id] is runtime
        assert len(nav._runtimes) <= 3
        assert nav.back()
    retained = set(run_ids[-3:])
    assert set(nav._runtimes) == retained
    assert set(nav._event_cursors) == retained
    assert set(nav._final_response_keys) == retained
    assert nav._goal_published == retained
    assert set(run_ids) <= nav._rows_by_run.keys()
    assert set(run_ids) <= nav._rows_by_parent.keys()
    assert set(run_ids) <= nav._child_goal_rows.keys()


def test_runtime_hits_protect_recently_visited_views():
    nav = navigation.TuiAgentNavigationState(TuiRuntime("lru-views"), max_cached_runtimes=3)
    originals = {}
    for run_id in ("a", "b", "c"):
        originals[run_id] = _open(nav, run_id)
        assert nav.back()
    assert _open(nav, "a") is originals["a"]
    assert nav.back()
    _open(nav, "d")
    assert set(nav._runtimes) == {"a", "c", "d"}
    assert nav._runtimes["a"] is originals["a"]
    assert "b" not in nav._event_cursors


def test_entire_navigation_path_and_returned_runtime_survive_over_cap():
    nav = navigation.TuiAgentNavigationState(TuiRuntime("pinned-path"), max_cached_runtimes=1)
    originals = {}
    parent = ""
    for run_id in ("parent", "child", "grandchild"):
        originals[run_id] = _open(nav, run_id, parent=parent)
        parent = run_id
    assert nav._path == list(originals)
    assert nav.apply_agent_view("prefetched", _view("prefetched"))
    assert "prefetched" in nav._runtimes
    for run_id, runtime in originals.items():
        assert nav._runtimes[run_id] is runtime
    assert nav.active_runtime() is originals["grandchild"]
    assert set(nav._runtimes) == set(originals)
    assert nav.back() and nav.active_runtime() is originals["child"]


@pytest.mark.parametrize("limit", [1, 3])
def test_business_targets_remain_pinned_when_read_is_denied(monkeypatch, limit):
    monkeypatch.setattr(navigation, "threading", SimpleNamespace(Thread=InlineThread, RLock=threading.RLock))
    client = NavigationClient()
    client.spaces = []
    client.agents = [agent_row("business-agent")]
    projection = TuiBackgroundAgents(client)
    projection.refresh()
    nav = navigation.TuiAgentNavigationState(TuiRuntime("business-pinned"), max_cached_runtimes=limit)
    nav.update_business_agents(projection)
    assert nav.move_selection(1) and nav.enter_selected()
    assert nav.move_selection(1) and nav.enter_selected()
    assert nav.snapshot().active_thread_id == "business-agent"
    runtime = nav.active_runtime()
    assert nav.back() and nav.back()
    for number in range(6):
        run_id = f"other-{number}"
        assert nav.apply_agent_view(run_id, _view(run_id))
        assert nav._runtimes["business:business-agent"] is runtime
        assert len(nav._runtimes) <= max(limit, 2)
    generation = runtime.conversation_reset_generation
    client.denied = True
    projection.refresh()
    nav.update_business_agents(projection)
    assert runtime.conversation_reset_generation == generation + 1
    assert nav._runtimes["business:business-agent"] is runtime
    assert not nav._business_messages and not nav._business_history


def test_evicted_view_gets_fresh_runtime_and_first_goal_replay(monkeypatch):
    nav = navigation.TuiAgentNavigationState(TuiRuntime("replay-view"), max_cached_runtimes=1)
    first_goals = []
    register = nav._register_agent_view

    def observe(view):
        result = register(view)
        if view.run_id == "first":
            first_goals.append(result[1])
        return result

    monkeypatch.setattr(nav, "_register_agent_view", observe)
    original = _open(nav, "first")
    assert nav.apply_agent_view("first", _view("first"))
    assert nav.back()
    _open(nav, "second")
    assert nav.back()
    assert "first" not in nav._runtimes
    assert nav.event_cursor("first") == 0
    assert "first" not in nav._goal_published
    assert "first" not in nav._final_response_keys
    reopened = _open(nav, "first")
    assert reopened is not original
    assert first_goals == [True, False, True]
    assert nav.event_cursor("first") == 11
    assert "first" in nav._goal_published


def test_default_constructor_keeps_32_runtime_limit():
    root = TuiRuntime("default-limit")
    nav = navigation.TuiAgentNavigationState(root)
    assert nav.active_runtime() is root
    for number in range(35):
        _open(nav, f"default-{number}")
        assert nav.back()
    assert nav._max_cached_runtimes == 32
    assert len(nav._runtimes) == 32


@pytest.mark.parametrize("configured,expected", [(0, 1), (-5, 1), (1, 1), ("3", 3)])
def test_constructor_clamps_runtime_limit_to_positive_integer(configured, expected):
    nav = navigation.TuiAgentNavigationState(TuiRuntime("clamped-limit"), max_cached_runtimes=configured)
    assert nav._max_cached_runtimes == expected


def test_config_yaml_tunable_and_parameter_registry_share_runtime_limit():
    key = "tui_agent_view_cache_max_count"
    config = AgentConfig()
    assert getattr(config, key) == 32
    metadata = next(item for item in fields(config) if item.name == key).metadata
    assert metadata["bounds"] == (1, None) and metadata["unit"] == "个"
    path = Path(__file__).resolve().parents[1] / "config" / "agent_config.yaml"
    assert load_simple_yaml(path)[key] == 32
    tunable = TUNABLE_KEYS[key]
    assert tunable.effect == EFFECT_NEXT_SESSION
    assert tunable.validate("0")[0] is False
    assert tunable.validate(True)[0] is False
    assert tunable.validate("5") == (True, "", 5)
    parameter = parameter_registry()[key]
    assert parameter.default == 32 and parameter.value_type == "int"
    assert parameter.effect == EFFECT_NEXT_SESSION and parameter.writable
    assert "TUI" in parameter.description and "32" in parameter.description
    configured = AgentConfig(**{key: 5})
    nav = navigation.TuiAgentNavigationState(TuiRuntime("configured-limit"), max_cached_runtimes=getattr(configured, key))
    for number in range(8):
        _open(nav, f"configured-{number}")
        assert nav.back()
    assert len(nav._runtimes) == 5
