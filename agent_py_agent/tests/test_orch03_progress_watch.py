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


# ---------- ORCH03-R5：有效 git-dir/工作树钉死，本地配置不能把读取引出仓库 ----------


def test_local_core_worktree_cannot_redirect_status_outside_the_repo(agent_and_child, repo, monkeypatch, tmp_path):
    agent, _child, _ = agent_and_child
    outside = Path(os.path.realpath(tmp_path)) / "outside-tree"
    outside.mkdir()
    for index in range(3):
        (outside / f"secret-{index}.txt").write_text("x")
    _register(monkeypatch, [str(repo)])
    clean = git.observe_registered(agent, str(repo))
    _git(repo, "config", "core.worktree", str(outside))
    redirected = git.observe_registered(agent, str(repo))

    # 未钉死时 git 会拿 outside 当工作树：a.txt 记成删除、secret-* 记成未跟踪，改动数变成 4。
    assert clean["error_code"] == "" and clean["dirty_paths_count"] == 0
    assert redirected["error_code"] == "" and redirected["dirty_paths_count"] == 0


def test_outside_excludes_file_from_local_config_is_not_read(agent_and_child, repo, monkeypatch, tmp_path):
    agent, _child, _ = agent_and_child
    excludes = Path(os.path.realpath(tmp_path)) / "outside-excludes"
    excludes.write_text("*.log\n")
    _git(repo, "config", "core.excludesFile", str(excludes))
    (repo / "x.log").write_text("l")
    _register(monkeypatch, [str(repo)])

    # 读了外部排除文件会把 x.log 藏起来（改动数 0）；被改成空设备后它照常算一处改动。
    assert git.observe_registered(agent, str(repo))["dirty_paths_count"] == 1


@pytest.mark.parametrize("redirect", ["include", "includeif", "commondir", "alternates", "config_symlink",
                                      "config_worktree_symlink"])
def test_git_dir_and_config_redirects_are_refused_before_any_status(agent_and_child, repo, monkeypatch, tmp_path,
                                                                    redirect):
    agent, _child, _ = agent_and_child
    outside = Path(os.path.realpath(tmp_path)) / "outside-config"
    outside.write_text("[core]\n\tworktree = /\n")
    git_dir = repo / ".git"
    _register(monkeypatch, [str(repo)])
    assert git.observe_registered(agent, str(repo))["error_code"] == ""
    if redirect == "include":
        _git(repo, "config", "include.path", str(outside))
    elif redirect == "includeif":
        _git(repo, "config", f"includeIf.gitdir:{git_dir}/.path", str(outside))
    elif redirect == "commondir":
        (git_dir / "commondir").write_text(str(outside.parent) + "\n")
    elif redirect == "alternates":
        (git_dir / "objects" / "info").mkdir(parents=True, exist_ok=True)
        (git_dir / "objects" / "info" / "alternates").write_text(str(outside.parent) + "\n")
    elif redirect == "config_worktree_symlink":
        # extensions.worktreeConfig 打开后 git 会读 config.worktree；它指向允许根外时不能被当成本地配置。
        _git(repo, "config", "extensions.worktreeConfig", "true")
        (git_dir / "config.worktree").symlink_to(outside)
    else:
        moved = Path(os.path.realpath(tmp_path)) / "moved-config"
        (git_dir / "config").rename(moved)
        (git_dir / "config").symlink_to(moved)
    marker = tmp_path / "git-ran"
    real_popen = git.subprocess.Popen
    monkeypatch.setattr(git.subprocess, "Popen", lambda *a, **k: (marker.write_text("x"), real_popen(*a, **k))[1])
    result = git.observe_registered(agent, str(repo))

    expected = "REPO_CONFIG_REDIRECT" if redirect.startswith("include") else "REPO_GIT_DIR_REDIRECT"
    assert result["error_code"] == expected and result["head"] == "" and result["dirty_paths_count"] is None
    # 结构重定向（含 config.worktree 符号链接）在启动任何 git 之前就拒绝；include 只能由 git 自己的解析结果发现。
    assert marker.exists() is redirect.startswith("include")


# ---------- ORCH03-R6：子进程树清理、预算与 HEAD 校验（替身 git 只替换指定子命令，其余转真实 git） ----------


def _fake_git(monkeypatch, tmp_path, subcommand: str, body: str) -> Path:
    import shutil

    real = shutil.which("git")
    fake_dir = tmp_path / "fakebin"
    fake_dir.mkdir(exist_ok=True)
    script = fake_dir / "git"
    script.write_text(
        "#!/bin/sh\n"
        f'for arg in "$@"; do if [ "$arg" = "{subcommand}" ]; then\n{body}\nexit 0\nfi; done\n'
        f'exec "{real}" "$@"\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return fake_dir


def _gone(pid: int, within: float = 3.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def _python_writer(payload: str) -> str:
    import shlex
    import sys

    return f"exec {shlex.quote(sys.executable)} -c {shlex.quote(payload)}"


@pytest.mark.skipif(os.name != "posix", reason="替身 git 脚本只在 POSIX 上可执行")
@pytest.mark.parametrize("behaviour", ["hang", "orphan_holds_stdout"])
def test_hanging_git_and_stdout_holding_descendants_are_killed_and_reaped(
        agent_and_child, repo, monkeypatch, tmp_path, behaviour):
    import threading

    agent, _child, _ = agent_and_child
    pid_file = tmp_path / "descendant.pid"
    head = _git(repo, "rev-parse", "HEAD")
    body = (f"sleep 30 &\necho $! > {pid_file}\nwait" if behaviour == "hang"
            else f"sleep 30 &\necho $! > {pid_file}\necho {head}")
    _fake_git(monkeypatch, tmp_path, "rev-parse", body)
    monkeypatch.setattr(git, "GIT_COMMAND_TIMEOUT_SECONDS", 1)
    _register(monkeypatch, [str(repo)])
    threads_before = threading.active_count()
    started = time.monotonic()
    result = git.observe_registered(agent, str(repo))
    elapsed = time.monotonic() - started

    assert result["error_code"] == "GIT_TIMEOUT" and result["head"] == ""
    assert elapsed < git.GIT_OBSERVATION_BUDGET_SECONDS
    # 后代 sleep 继承了 stdout：只杀直接子进程会留下它；这里要求整棵树被清掉、且没有遗留读线程。
    assert _gone(int(pid_file.read_text().strip()))
    assert threading.active_count() == threads_before


# LLM: 两次 fork + setsid 的后代既不在父链上也不在进程组里，共享清树找不到它（已知限制，HANDOFF 记录）；
#   这里只验证它一直占着 stdout 时调用方仍按超时返回、不留下卡住的读线程。测试自己清理该进程。
# 函数用途: 逃逸的后代占住管道也不会让观察卡住或泄漏线程。
@pytest.mark.skipif(os.name != "posix", reason="替身 git 脚本只在 POSIX 上可执行")
def test_escaped_descendant_holding_stdout_does_not_leave_a_reader_thread(agent_and_child, repo, monkeypatch, tmp_path):
    import shlex
    import signal
    import sys
    import threading

    agent, _child, _ = agent_and_child
    pid_file = tmp_path / "escaped.pid"
    escaper = (f"import os, time; os.setsid(); open({str(pid_file)!r}, 'w').write(str(os.getpid())); "
               "time.sleep(30)")
    _fake_git(monkeypatch, tmp_path, "rev-parse",
              f"( {shlex.quote(sys.executable)} -c {shlex.quote(escaper)} & )\nsleep 0.3")
    monkeypatch.setattr(git, "GIT_COMMAND_TIMEOUT_SECONDS", 1)
    _register(monkeypatch, [str(repo)])
    threads_before = threading.active_count()
    started = time.monotonic()
    try:
        result = git.observe_registered(agent, str(repo))
        elapsed = time.monotonic() - started
        threads_after = threading.active_count()
    finally:
        if pid_file.exists() and pid_file.read_text().strip():
            try:
                os.kill(int(pid_file.read_text().strip()), signal.SIGKILL)
            except ProcessLookupError:
                pass

    # 后台读线程方案在关管道时会一直等到逃逸进程自己退出（实测约 30 秒）。
    assert result["error_code"] == "GIT_TIMEOUT" and threads_after == threads_before
    assert elapsed < git.GIT_OBSERVATION_BUDGET_SECONDS


# LLM: 让 killpg(组, 0) 一直报“组还在”来模拟证明不了组已空；SIGKILL 仍真实下发。只验证证明不了时观察整体失败、
#   不交出输出，并且在总预算内返回。
# 函数用途: 组清理确认不了时观察失败且不超预算。
def test_unprovable_group_cleanup_fails_the_observation_within_budget(agent_and_child, repo, monkeypatch):
    agent, _child, _ = agent_and_child
    real_killpg = os.killpg

    def group_never_empty(group, sig):
        if sig == 0:
            return None
        return real_killpg(group, sig)

    monkeypatch.setattr(git.os, "killpg", group_never_empty)
    monkeypatch.setattr(git, "GIT_OBSERVATION_BUDGET_SECONDS", 3)
    _register(monkeypatch, [str(repo)])
    started = time.monotonic()
    result = git.observe_registered(agent, str(repo))
    elapsed = time.monotonic() - started

    assert result["error_code"] == "GIT_CLEANUP_UNCONFIRMED" and result["head"] == ""
    assert elapsed < 3 + 0.5


# LLM: PRO-MSG-022/023：协调者环境里 ps 被拒、共享启动身份恒为空。观察不再依赖这条路径：把它换成一调用就失败的
#   替身，干净仓库的正向对照仍成功。
# 函数用途: git 观察不依赖 ps 启动身份采集。
def test_observation_does_not_depend_on_ps_birth_identity(agent_and_child, repo, monkeypatch):
    from agent_py_agent.agent.tooling import process_registry

    agent, _child, _ = agent_and_child

    def denied(*_a, **_k):
        raise AssertionError("adapter must not need ps birth identity")

    monkeypatch.setattr(process_registry, "capture_process_birth_token", denied)
    monkeypatch.setattr(process_registry, "terminate_process_tree", denied)
    monkeypatch.setattr(process_registry, "finish_foreground_process", denied)
    _register(monkeypatch, [str(repo)])
    result = git.observe_registered(agent, str(repo))

    assert result["error_code"] == "" and result["dirty_paths_count"] == 0 and len(result["head"]) in (40, 64)


# LLM: ORCH03-R6（PRO-MSG-027）确定性时序回归：记录每次 killpg 时根进程是否仍未回收。任何破坏性信号（sig != 0）
#   都必须发生在根进程还被占着的时候；回收之后只允许 killpg(组, 0) 探测。覆盖正常结束、正常 EOF 但留下后代、
#   占着 stdout 的后代、挂住和灌输出五种路径。
# 函数用途: 根进程回收后绝不再发破坏性组信号。
@pytest.mark.skipif(os.name != "posix", reason="替身 git 脚本只在 POSIX 上可执行")
@pytest.mark.parametrize("scenario", ["clean", "detached_descendant", "stdout_descendant", "hang", "status_flood"])
def test_no_destructive_group_signal_after_the_root_is_reaped(agent_and_child, repo, monkeypatch, tmp_path, scenario):
    import shlex
    import sys

    agent, _child, _ = agent_and_child
    head = _git(repo, "rev-parse", "HEAD")
    pid_file = tmp_path / "descendant.pid"
    if scenario == "detached_descendant":
        _fake_git(monkeypatch, tmp_path, "rev-parse",
                  f"sleep 30 >/dev/null 2>&1 </dev/null &\necho $! > {pid_file}\necho {head}")
    elif scenario == "stdout_descendant":
        _fake_git(monkeypatch, tmp_path, "rev-parse", f"sleep 30 &\necho $! > {pid_file}\necho {head}")
    elif scenario == "hang":
        _fake_git(monkeypatch, tmp_path, "rev-parse", "sleep 30")
    elif scenario == "status_flood":
        payload = "import sys; sys.stdout.write('?? f\\0' * 100000)"
        _fake_git(monkeypatch, tmp_path, "status", f"exec {shlex.quote(sys.executable)} -c {shlex.quote(payload)}")
    monkeypatch.setattr(git, "GIT_COMMAND_TIMEOUT_SECONDS", 1)
    spawned = []
    real_popen, real_killpg = git.subprocess.Popen, os.killpg

    def popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        spawned.append(process)
        return process

    signals = []

    def killpg(group, sig):
        owner = next((item for item in spawned if item.pid == group), None)
        signals.append((int(sig), owner is not None and owner.returncode is None))
        return real_killpg(group, sig)

    monkeypatch.setattr(git.subprocess, "Popen", popen)
    monkeypatch.setattr(git.os, "killpg", killpg)
    _register(monkeypatch, [str(repo)])
    result = git.observe_registered(agent, str(repo))

    expected = {"clean": "", "detached_descendant": "GIT_DESCENDANTS_LEFT", "stdout_descendant": "GIT_TIMEOUT",
                "hang": "GIT_TIMEOUT", "status_flood": ""}[scenario]
    assert result["error_code"] == expected
    assert [sig for sig, pinned in signals if sig != 0 and not pinned] == []
    if scenario in {"detached_descendant", "stdout_descendant"}:
        assert _gone(int(pid_file.read_text().strip()))


# LLM: git 先关掉 stdout、过一会儿才退出：EOF 之后必须不回收地等它真正退出，拿到它自己的退出码；不能把仍在运行的
#   根进程当成“留下的后代”，也不能提前强杀改写退出码。
# 函数用途: EOF 早于根进程退出时保留真实退出码。
@pytest.mark.skipif(os.name != "posix", reason="替身 git 脚本只在 POSIX 上可执行")
@pytest.mark.parametrize(("code", "expected"), [(0, ""), (3, "GIT_FAILED")])
def test_root_exiting_after_eof_keeps_its_real_exit_code(agent_and_child, repo, monkeypatch, tmp_path, code, expected):
    agent, _child, _ = agent_and_child
    head = _git(repo, "rev-parse", "HEAD")
    _fake_git(monkeypatch, tmp_path, "rev-parse", f"echo {head}\nexec 1>&-\nsleep 0.4\nexit {code}")
    _register(monkeypatch, [str(repo)])
    result = git.observe_registered(agent, str(repo))

    assert result["error_code"] == expected
    assert (result["head"] == head) is (code == 0)


def test_unsupported_platform_refuses_before_spawning_git(agent_and_child, repo, monkeypatch):
    agent, _child, _ = agent_and_child
    monkeypatch.setattr(git, "_PLATFORM_SUPPORTED", False)
    monkeypatch.setattr(git.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned")))
    _register(monkeypatch, [str(repo)])

    assert git.observe_registered(agent, str(repo))["error_code"] == "GIT_PLATFORM_UNSUPPORTED"


@pytest.mark.skipif(os.name != "posix", reason="替身 git 脚本只在 POSIX 上可执行")
def test_hanging_git_total_time_is_capped_by_one_absolute_deadline(agent_and_child, repo, monkeypatch, tmp_path):
    agent, _child, _ = agent_and_child
    _fake_git(monkeypatch, tmp_path, "rev-parse", "sleep 30")
    monkeypatch.setattr(git, "GIT_OBSERVATION_BUDGET_SECONDS", 3)
    _register(monkeypatch, [str(repo)])
    started = time.monotonic()
    result = git.observe_registered(agent, str(repo))
    elapsed = time.monotonic() - started

    assert result["error_code"] == "GIT_TIMEOUT" and elapsed < 3 + 0.5


@pytest.mark.skipif(os.name != "posix", reason="替身 git 脚本只在 POSIX 上可执行")
@pytest.mark.parametrize(("label", "payload"), [
    ("flood", "import sys; sys.stdout.write('a' * 200000)"),
    ("short", "print('abc1234')"),
    ("garbage", "print('z' * 40)"),
    ("two_lines", "print('a' * 40); print('b' * 40)"),
])
def test_truncated_or_malformed_head_is_refused(agent_and_child, repo, monkeypatch, tmp_path, label, payload):
    agent, _child, _ = agent_and_child
    _fake_git(monkeypatch, tmp_path, "rev-parse", _python_writer(payload))
    _register(monkeypatch, [str(repo)])
    result = git.observe_registered(agent, str(repo))

    assert result["error_code"] == "GIT_HEAD_INVALID" and result["head"] == "" and result["dirty_paths_count"] is None


@pytest.mark.skipif(os.name != "posix", reason="替身 git 脚本只在 POSIX 上可执行")
def test_flooding_status_reports_an_honest_lower_bound_with_a_valid_head(agent_and_child, repo, monkeypatch, tmp_path):
    agent, _child, _ = agent_and_child
    head = _git(repo, "rev-parse", "HEAD")
    _fake_git(monkeypatch, tmp_path, "status", _python_writer("import sys; sys.stdout.write('?? f\\0' * 100000)"))
    _register(monkeypatch, [str(repo)])
    result = git.observe_registered(agent, str(repo))

    assert result["error_code"] == "" and result["head"] == head
    assert result["dirty_count_is_lower_bound"] is True and result["dirty_paths_count"] >= 1


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
