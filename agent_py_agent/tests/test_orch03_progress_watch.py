"""ORCH-03：子代理进展的只读分类与可选 git 观察。

分类表用结构化节点字典逐条验证规则；接线用真实 SimpleAgent(echo) + 真实子代理记录 + 真实 ListAgentsTool；
git 观察用真实临时仓库，超时和超量输出用 PATH 上的替身 git（仅 POSIX）。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from agent_py_agent.agent.agent_core.agent_tree.model_view import agent_tree_progress_observation
from agent_py_agent.agent.agent_core.agent_tree.status import agent_tree_status_payload
from agent_py_agent.agent.agent_core.orchestration import git_progress_adapter as git
from agent_py_agent.agent.agent_core.orchestration.progress_watch import (
    HEARTBEAT_STALE_AFTER_SECONDS,
    MAX_PROGRESS_CLASSIFIED_NODES_COUNT,
    PROGRESS_STALE_AFTER_SECONDS,
    classify_node_progress,
    classify_nodes,
    merge_git_observation,
)
from agent_py_agent.agent.agent_core.orchestration.tools.list_agents import ListAgentsTool
from agent_py_agent.agent.capability.config import CapabilityConfig, load_capability_config
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents.model_capabilities import CapabilityRequest

NOW = 1_800_000_000.0


def _node(**overrides) -> dict:
    node = {"status": "RUNNING", "heartbeat_at": NOW - 5, "last_progress_at": NOW - 30,
            "tool_contract": {"open_request_count": 0}, "blockers": []}
    node.update(overrides)
    return {key: value for key, value in node.items() if value is not ...}


@pytest.mark.parametrize(("node", "state", "reason"), [
    (_node(), "working", "RECENT_PROGRESS_EVENT"),
    (_node(status="DONE"), "completed", "TERMINAL_DONE"),
    (_node(status="FAILED"), "ended", "TERMINAL_NOT_DONE"),
    (_node(status="CANCELLED"), "ended", "TERMINAL_NOT_DONE"),
    (_node(status="PAUSED"), "unknown", "PAUSED_BY_CONTROL"),
    (_node(status="SOMETHING_NEW"), "unknown", "STATUS_UNRECOGNIZED"),
    (_node(status=...), "unknown", "STATUS_UNREADABLE"),
    (_node(tool_contract={"open_request_count": 1}), "waiting_permission", "OPEN_CAPABILITY_REQUEST"),
    (_node(tool_contract=...), "unknown", "CAPABILITY_FACTS_UNREADABLE"),
    (_node(tool_contract={"open_request_count": "1"}), "unknown", "CAPABILITY_FACTS_UNREADABLE"),
    (_node(blockers=["等上游产物"]), "waiting_dependency", "BLOCKERS_RECORDED"),
    (_node(status="BLOCKED"), "waiting_dependency", "BLOCKED"),
    (_node(blockers="x"), "unknown", "BLOCKER_FACTS_UNREADABLE"),
    (_node(status="PLANNING"), "unknown", "NOT_STARTED"),
    (_node(recent_tool_failure={"consecutive_failures": 3}), "retrying", "REPEATED_TOOL_FAILURE"),
    (_node(recent_tool_failure={"consecutive_failures": 1}), "working", "RECENT_PROGRESS_EVENT"),
    (_node(last_progress_at=NOW - PROGRESS_STALE_AFTER_SECONDS - 1), "no_progress", "PROGRESS_EVENTS_STALE"),
    # 进程/心跳在不等于在干活：活着但从没有进展事件 → unknown，不是 working，也不是 no_progress。
    (_node(last_progress_at=0), "unknown", "NO_PROGRESS_EVENT_YET"),
    # 心跳过时：进展事件再新也证明不了执行体还在。
    (_node(heartbeat_at=NOW - HEARTBEAT_STALE_AFTER_SECONDS - 1), "unknown", "HEARTBEAT_STALE_OR_MISSING"),
    (_node(heartbeat_at=...), "unknown", "HEARTBEAT_STALE_OR_MISSING"),
    (_node(heartbeat_at=True), "unknown", "HEARTBEAT_STALE_OR_MISSING"),
])
def test_classification_uses_only_structured_facts(node, state, reason):
    result = classify_node_progress(node, now=NOW)

    assert (result["state"], result["reason"]) == (state, reason)
    assert result["schema_version"] == "progress_watch.v1" and result["effect"] == "read_only"
    assert result["observed_at"] == NOW


def test_sources_keep_time_provenance_and_unknown_never_reads_as_healthy():
    result = classify_node_progress(_node(), now=NOW)
    stale = classify_node_progress(_node(last_progress_at=NOW - PROGRESS_STALE_AFTER_SECONDS - 1), now=NOW)

    assert result["sources"]["last_progress_at"] == NOW - 30 and result["sources"]["progress_age_seconds"] == 30
    assert result["sources"]["heartbeat_age_seconds"] == 5
    assert stale["sources"]["progress_age_seconds"] > PROGRESS_STALE_AFTER_SECONDS
    assert classify_node_progress({}, now=NOW)["state"] == "unknown"


def test_classification_is_bounded_per_snapshot():
    results = classify_nodes([_node() for _ in range(MAX_PROGRESS_CLASSIFIED_NODES_COUNT + 6)], now=NOW)

    assert [item["reason"] for item in results[-6:]] == ["NOT_CLASSIFIED_LIMIT"] * 6
    assert {item["state"] for item in results[:MAX_PROGRESS_CLASSIFIED_NODES_COUNT]} == {"working"}


def test_git_observation_only_upgrades_with_a_verified_change():
    base = classify_node_progress(_node(last_progress_at=NOW - PROGRESS_STALE_AFTER_SECONDS - 1), now=NOW)
    previous = {"head": "a" * 40, "dirty_paths_count": 0, "error_code": ""}
    same = merge_git_observation(base, dict(previous), previous)
    changed = merge_git_observation(base, {**previous, "head": "b" * 40}, previous)
    unavailable = merge_git_observation(base, {"head": "", "error_code": "GIT_UNAVAILABLE"}, previous)
    no_previous = merge_git_observation(base, {**previous, "head": "b" * 40}, None)
    waiting = merge_git_observation(classify_node_progress(_node(tool_contract={"open_request_count": 2}), now=NOW),
                                    {**previous, "head": "b" * 40}, previous)

    assert same["state"] == "no_progress" and same["sources"]["git"]["head"] == "a" * 40
    assert (changed["state"], changed["reason"]) == ("working", "GIT_CHANGED_SINCE_PREVIOUS")
    assert unavailable["state"] == "no_progress" and unavailable["sources"]["git"]["error_code"] == "GIT_UNAVAILABLE"
    assert no_previous["state"] == "no_progress"
    assert waiting["state"] == "waiting_permission"


# ---------- 真实接线：list_agents 的模型节点 ----------


@pytest.fixture
def agent_and_child(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"),
                                    enable_subagents=True), workspace)
    thread = agent.conversation_store.threads.get_or_create({
        "canonical_user_id": "u", "channel": "local", "channel_conversation_id": "c", "channel_user_id": "u",
        "owner_id": agent.home_paths.owner_id, "owner_home": str(agent.home_paths.owner_home_dir),
    })
    parent = agent.conversation_store.tasks.bind(
        {"thread_id": thread.thread_id, "task_id": "task-T", "goal": "长期工作", "status": "active"}).task_id
    child = agent.subagents.create_run(goal="子任务", thought="", plan=["做"], parent_id=parent, root_id=parent, depth=1)
    from types import SimpleNamespace

    agent._current_run_params = SimpleNamespace(
        run_id="gwreq-1", task_id="gwreq-1", request_id="gwreq-1",
        task_attributes={"conversation_task_id": parent, "conversation_thread_id": thread.thread_id})
    return agent, child.id, workspace


def _list(agent, run_id) -> dict:
    result = ListAgentsTool(agent).execute({"run_id": run_id})
    assert result.ok, result.output
    return json.loads(result.output)


def test_list_agents_nodes_carry_progress_and_reading_mutates_nothing(agent_and_child):
    agent, child_id, _ = agent_and_child
    task = agent.subagents.load(child_id)
    task.capability_requests = [CapabilityRequest(id="req-1", from_run_id=child_id, problem="缺权限",
                                                  needed_capability="x", status="OPEN", created_at=time.time())]
    agent.subagents.save(task)
    task_file = agent.subagents.persistence.workspace / child_id / "task.json"
    before = task_file.read_bytes()
    wakes = len(agent.conversation_store.wakes.pending(limit=1000))
    payload = _list(agent, child_id)
    [node] = [row for row in payload["nodes"] if row["run_id"] == child_id]

    assert node["progress"]["state"] == "waiting_permission"
    assert node["progress"]["sources"]["open_capability_requests"] == 1
    # 原有公开字段不变。
    assert {"run_id", "status", "readiness", "read_order"} <= set(node)
    assert task_file.read_bytes() == before
    assert len(agent.conversation_store.wakes.pending(limit=1000)) == wakes
    assert agent.conversation_store.guidance.pending("agent_run", child_id) == []
    assert agent.subagents.load(child_id).status == task.status


def test_planning_child_is_unknown_and_progress_stays_out_of_the_stable_fingerprint(agent_and_child, monkeypatch):
    agent, child_id, _ = agent_and_child
    first = _list(agent, child_id)
    snapshot = agent_tree_status_payload(agent, {"run_id": child_id, "scope": "own_subtree"})
    from agent_py_agent.agent.agent_core.agent_tree import model_view

    monkeypatch.setattr(model_view.time, "time", lambda: NOW + 10_000)
    later = model_view.agent_tree_model_payload(snapshot)

    [node] = [row for row in first["nodes"] if row["run_id"] == child_id]
    assert (node["progress"]["state"], node["progress"]["reason"]) == ("unknown", "NOT_STARTED")
    assert agent_tree_progress_observation(snapshot, first)["sha256"] == agent_tree_progress_observation(
        snapshot, later)["sha256"]


# ---------- 可选 git 观察 ----------


def _git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull}
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env=env).stdout.strip()


@pytest.fixture
def repo(agent_and_child):
    _agent, _child, workspace = agent_and_child
    path = Path(os.path.realpath(workspace)) / "proj"
    path.mkdir()
    _git(path, "init", "-q")
    (path / "a.txt").write_text("a")
    _git(path, "add", "a.txt")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _register(monkeypatch, repos: list[str]) -> None:
    from agent_py_agent.agent.capability import runtime_config_reload

    monkeypatch.setattr(runtime_config_reload, "capability_config_for_agent",
                        lambda _agent: CapabilityConfig(orchestration_git_progress_repos=repos))


def test_git_adapter_is_off_by_default_and_observes_only_registered_repos(agent_and_child, repo, monkeypatch):
    agent, _child, _ = agent_and_child
    assert load_capability_config("agent_py_agent/config/capability_config.yaml").orchestration_git_progress_repos == []
    assert git.observe_registered(agent, str(repo))["error_code"] in {"GIT_ADAPTER_DISABLED", "REPO_NOT_REGISTERED"}
    _register(monkeypatch, [])
    assert git.observe_registered(agent, str(repo))["error_code"] == "GIT_ADAPTER_DISABLED"
    _register(monkeypatch, [str(repo)])
    baseline = _git(repo, "rev-parse", "HEAD")
    (repo / "a.txt").write_text("changed")
    (repo / "new.txt").write_text("n")
    first = git.observe_registered(agent, str(repo), baseline=baseline)
    _git(repo, "commit", "-q", "-am", "next")
    second = git.observe_registered(agent, str(repo), baseline=baseline)

    assert first["error_code"] == "" and first["head"] == baseline and first["dirty_paths_count"] == 2
    assert first["ahead_of_baseline"] == 0 and first["observed_at"] > 0
    assert second["head"] != baseline and second["ahead_of_baseline"] == 1 and second["dirty_paths_count"] == 1
    assert git.observe_registered(agent, str(repo.parent / "other"))["error_code"] == "REPO_NOT_REGISTERED"


def test_git_adapter_rejects_symlinks_outside_roots_gitfiles_owner_and_injected_baselines(
        agent_and_child, repo, monkeypatch, tmp_path):
    agent, _child, workspace = agent_and_child
    link = Path(os.path.realpath(workspace)) / "link"
    link.symlink_to(repo)
    outside = Path(os.path.realpath(tmp_path)) / "outside"
    outside.mkdir()
    _git(outside, "init", "-q")
    gitfile = Path(os.path.realpath(workspace)) / "worktree"
    gitfile.mkdir()
    (gitfile / ".git").write_text(f"gitdir: {repo / '.git'}\n")
    _register(monkeypatch, [str(link), str(outside), str(gitfile), str(repo), "relative/path"])

    assert git.observe_registered(agent, str(link))["error_code"] == "REPO_SYMLINK"
    assert git.observe_registered(agent, str(outside))["error_code"] == "REPO_OUTSIDE_ALLOWED_ROOTS"
    assert git.observe_registered(agent, str(gitfile))["error_code"] == "REPO_NOT_A_GIT_DIR"
    assert git.observe_registered(agent, "relative/path")["error_code"] == "REPO_NOT_ABSOLUTE"
    assert git.observe_registered(agent, str(repo), baseline="--output=/tmp/x")["error_code"] == "BASELINE_INVALID"
    assert git.observe_registered(agent, str(repo), baseline="HEAD~1")["error_code"] == "BASELINE_INVALID"
    other_uid = os.getuid() + 1
    monkeypatch.setattr(git.os, "getuid", lambda: other_uid)
    assert git.observe_registered(agent, str(repo))["error_code"] == "REPO_OWNER_MISMATCH"


def test_repo_local_fsmonitor_hook_never_runs(agent_and_child, repo, monkeypatch, tmp_path):
    agent, _child, _ = agent_and_child
    marker = tmp_path / "fsmonitor-ran"
    hook = tmp_path / "hook.sh"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    _git(repo, "config", "core.fsmonitor", str(hook))
    _register(monkeypatch, [str(repo)])
    result = git.observe_registered(agent, str(repo))

    assert result["error_code"] == "" and not marker.exists()


@pytest.mark.skipif(os.name != "posix", reason="替身 git 脚本只在 POSIX 上可执行")
@pytest.mark.parametrize("behaviour", ["hang", "flood"])
def test_hanging_or_flooding_git_is_bounded(agent_and_child, repo, monkeypatch, tmp_path, behaviour):
    agent, _child, _ = agent_and_child
    fake_dir = tmp_path / "fakebin"
    fake_dir.mkdir()
    body = "sleep 30" if behaviour == "hang" else "head -c 200000 /dev/zero | tr '\\0' 'a'"
    (fake_dir / "git").write_text(f"#!/bin/sh\n{body}\n")
    (fake_dir / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(git, "GIT_COMMAND_TIMEOUT_SECONDS", 1)
    _register(monkeypatch, [str(repo)])
    started = time.monotonic()
    result = git.observe_registered(agent, str(repo))
    elapsed = time.monotonic() - started

    if behaviour == "hang":
        assert result["error_code"] == "GIT_TIMEOUT" and elapsed < 5
    else:
        # rev-parse 输出被截断后仍是一串 a；status 同样读到上限即停，改动数只算下限。
        assert result["error_code"] == "" and result["dirty_count_is_lower_bound"] is True


@pytest.mark.parametrize("node", [
    _node(status="PAUSED"), _node(status="PLANNING"), _node(status="SOMETHING_NEW"), _node(status=...),
    _node(heartbeat_at=...), _node(heartbeat_at=NOW - HEARTBEAT_STALE_AFTER_SECONDS - 1),
    _node(last_progress_at=0), _node(tool_contract=...), _node(status="FAILED"), _node(status="DONE"),
    _node(blockers=["等上游"]), _node(recent_tool_failure={"consecutive_failures": 5}),
])
def test_git_change_never_overrides_lifecycle_or_unproven_liveness(node):
    """PRO-MSG-017：仓库有变化证明不了暂停/未开始/状态不明/心跳缺失的子代理在干活，也不改等待、重试和终态。"""
    base = classify_node_progress(node, now=NOW)
    previous = {"head": "a" * 40, "dirty_paths_count": 0, "error_code": ""}
    merged = merge_git_observation(base, {**previous, "head": "b" * 40}, previous)

    assert (merged["state"], merged["reason"]) == (base["state"], base["reason"])
    assert merged["sources"]["git"]["head"] == "b" * 40
