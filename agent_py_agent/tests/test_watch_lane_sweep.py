"""编队补岗扫描:窗口未走完 + 岗上 run 终态 → 建接管 run 续盯(全结构化判据)。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from types import SimpleNamespace

from agent.agent_core.orchestration.dispatch.watch_lane_sweep import respawn_dead_watch_lanes
from agent.ingestion.watch_state import list_states, new_state, persist_state


@dataclass
class _StubTask:
    id: str
    status: str
    takeover_by: str = ""


@dataclass
class _StubManager:
    tasks: dict[str, _StubTask]
    created: list = field(default_factory=list)
    create_result_created: bool = True

    def load(self, run_id: str):
        task = self.tasks.get(run_id)
        if task is None:
            raise FileNotFoundError(run_id)
        return task

    def create_takeover_run(self, request):
        self.created.append(request)
        return SimpleNamespace(
            source_run_id=request.source_run_id,
            takeover_run_id=f"tk-{request.source_run_id}",
            created=self.create_result_created,
            applied=self.create_result_created,
            message="stub",
        )


def _agent(owner_home, manager):
    return SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=str(owner_home), owner_id="u"),
        subagents=manager,
        conversation_store=None,
    )


def _lane(owner_home, url: str, *, window: int, puller: str, opened_ago: float = 60.0, closed: bool = False):
    state = new_state(owner_home, url, {})
    state.watch_window_seconds = window
    state.opened_at = time.time() - opened_ago
    state.last_puller_run_id = puller
    state.closed = closed
    persist_state(state)
    return state


def test_dead_done_lane_gets_takeover_and_respawn_recorded(tmp_path):
    manager = _StubManager(tasks={"run-a": _StubTask("run-a", "DONE")})
    _lane(tmp_path, "http://127.0.0.1:9/pull", window=1200, puller="run-a")
    actions = respawn_dead_watch_lanes(_agent(tmp_path, manager))
    assert len(actions) == 1
    assert actions[0]["takeover_run_id"] == "tk-run-a"
    assert manager.created and manager.created[0].source_run_id == "run-a"
    assert "watch_stream" in manager.created[0].reason
    row = list_states(tmp_path)[0]
    assert row["respawn_count"] == 1


def test_window_complete_lane_left_alone(tmp_path):
    manager = _StubManager(tasks={"run-a": _StubTask("run-a", "DONE")})
    _lane(tmp_path, "http://127.0.0.1:9/pull", window=30, puller="run-a", opened_ago=120.0)
    assert respawn_dead_watch_lanes(_agent(tmp_path, manager)) == []
    assert manager.created == []


def test_closed_or_running_or_unmanned_lanes_left_alone(tmp_path):
    manager = _StubManager(tasks={"run-r": _StubTask("run-r", "RUNNING")})
    _lane(tmp_path, "http://127.0.0.1:1/pull", window=1200, puller="run-r")
    _lane(tmp_path, "http://127.0.0.1:2/pull", window=1200, puller="", opened_ago=10.0)
    _lane(tmp_path, "http://127.0.0.1:3/pull", window=1200, puller="run-r", closed=True)
    assert respawn_dead_watch_lanes(_agent(tmp_path, manager)) == []


def test_chain_walk_respawns_latest_dead_takeover(tmp_path):
    manager = _StubManager(tasks={
        "run-a": _StubTask("run-a", "TAKEN_OVER", takeover_by="tk-1"),
        "tk-1": _StubTask("tk-1", "DONE"),
    })
    _lane(tmp_path, "http://127.0.0.1:9/pull", window=1200, puller="run-a")
    actions = respawn_dead_watch_lanes(_agent(tmp_path, manager))
    assert len(actions) == 1
    assert manager.created[0].source_run_id == "tk-1"


def test_existing_alive_takeover_is_idempotent_no_new_action(tmp_path):
    manager = _StubManager(tasks={"run-a": _StubTask("run-a", "DONE")}, create_result_created=False)
    _lane(tmp_path, "http://127.0.0.1:9/pull", window=1200, puller="run-a")
    assert respawn_dead_watch_lanes(_agent(tmp_path, manager)) == []
    assert list_states(tmp_path)[0]["respawn_count"] == 0


def test_solo_main_agent_watch_not_managed(tmp_path):
    manager = _StubManager(tasks={})
    _lane(tmp_path, "http://127.0.0.1:9/pull", window=1200, puller="main-run-xyz")
    assert respawn_dead_watch_lanes(_agent(tmp_path, manager)) == []
