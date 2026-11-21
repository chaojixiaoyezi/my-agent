"""检验项目记忆的自动准入、显式检索与正式晋升。"""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime
from threading import Barrier
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams, RuntimeContextRequest
from agent_py_agent.agent.agent_core.runtime.loop_support import _prepare_runtime_context
from agent_py_agent.agent.backends.base import EchoBackend
from agent_py_agent.agent.capability.memory_search_tool import MemorySearchTool, _scope_predicate
from agent_py_agent.agent.capability.memory_tool import RememberTool, _prepare_observations
from agent_py_agent.agent.conversation.models import PreviousUserTurn
from agent_py_agent.agent.gateway_parts.request_worker import _resolve_request_agent
from agent_py_agent.agent.memory_store import project_bindings
from agent_py_agent.agent.memory_store.recall import (
    MemoryRecallScope,
    formal_recall_suppressed,
    runtime_long_term_scope,
    runtime_recall_predicate,
)
from agent_py_agent.agent.tooling.registry_workspace import freeze_workspace_ref
from agent_py_agent.tests.test_memory_promotion import _advance_obligation_clock
from agent_py_agent.tests.test_memory_tool import _agent_with_current_user

QUERY = "thermometer atlas"
X = "project:x"
Y = "project:y"


def _add(memory, scope_type, key):
    return memory.add(
        "user", f"{QUERY} observation {key}", kind="fact",
        attributes={"scope_type": scope_type, "scope_key": key},
    )


def _prepare(agent, workspace, *, query=QUERY, context_scope="default"):
    agent.backend = EchoBackend()
    return _prepare_runtime_context(agent, RuntimeContextRequest(
        query, None, False, context_scope=context_scope, allowed_tools=[],
        request_id=getattr(agent._current_run_params, "request_id", ""),
        workspace_ref=freeze_workspace_ref(workspace),
    ))


def _project_request(key, *, origin="user_explicit", content=QUERY):
    return {
        "action": "add", "content": content, "kind": "project", "origin": origin,
        "subject_key": f"{key}.observation", "scope": {"scope_type": "project", "scope_key": key},
    }


def _search(agent, project=None):
    params = {"query": QUERY}
    if project is not None:
        params["project"] = project
    result = MemorySearchTool(agent).execute(params)
    assert result.ok, result.output
    return json.loads(result.output)["entries"]


def test_w13_replay_excludes_unrelated_project_memory(tmp_path):
    workspace = tmp_path / "x"
    workspace.mkdir()
    agent = _agent_with_current_user(tmp_path / "agent", content=QUERY)
    project_bindings.record(agent.home_paths.owner_home_dir, X, workspace)
    current = agent._current_run_params
    current.conversation_history_seed = SimpleNamespace(previous_user_turn=PreviousUserTurn(
        preview=f"{QUERY} project:y", owner_id=agent.home_paths.owner_id,
        thread_id=current.task_attributes["conversation_thread_id"], request_id=current.request_id,
    ))
    expected = _add(agent.memory, "project", X)
    for name in ("y", "z", "a", "b"):
        _add(agent.memory, "project", f"project:{name}")
    prepared = _prepare(agent, workspace)
    assert {row.entry_id for row in prepared.memories} == {expected.entry_id}


def test_explicit_project_search_returns_labeled_results(tmp_path):
    agent = _agent_with_current_user(tmp_path, content=QUERY)
    agent._current_run_params.task_attributes["project_id"] = X
    _add(agent.memory, "project", X)
    expected = _add(agent.memory, "project", Y)
    entries = _search(agent, project="y")
    assert [row["entry_id"] for row in entries] == [expected.entry_id]
    assert [(row["scope_type"], row["scope_key"]) for row in entries] == [("project", Y)]


def test_remember_promotion_records_only_committed_project_binding(tmp_path):
    agent = _agent_with_current_user(tmp_path / "agent", content=QUERY)
    workspace_a = tmp_path / "a"
    workspace_b = tmp_path / "b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    registry = agent.home_paths.owner_home_dir / "memory" / "project_bindings.json"
    agent._current_run_params.workspace_ref = freeze_workspace_ref(workspace_a)
    rejected = RememberTool(agent).execute(_project_request(X, origin="tool_verified"))
    assert rejected.ok and not json.loads(rejected.output)["active_memory_changed"]
    assert not registry.exists()

    agent._current_run_params.workspace_ref = freeze_workspace_ref(workspace_b)
    saved = RememberTool(agent).execute(_project_request(Y))
    assert saved.ok and json.loads(saved.output)["active_memory_changed"], saved.output
    assert registry.is_file(), "正式项目晋升后应保存宿主工作区绑定"
    assert not _prepare(agent, workspace_a).memories
    assert [row.attributes["scope_key"] for row in _prepare(agent, workspace_b).memories] == [Y]
    assert [row["scope_key"] for row in _search(agent, project="y")] == [Y]


def test_curator_promotes_candidate_with_frozen_workspace_provenance(tmp_path):
    agent = _agent_with_current_user(tmp_path / "agent", content=QUERY)
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    active = agent._current_run_params
    active.workspace_ref = freeze_workspace_ref(b)

    # 用 remember 生产的宿主引用进入候选账，后续由真实整理器批量入口归档。
    [draft] = _prepare_observations(agent, agent.memory, [_project_request(Y)], scope=None)
    candidate = agent.memory_candidates.observe(draft)
    active.workspace_ref = freeze_workspace_ref(a)
    promoted, warnings, _ = agent.memory_curator._promote_eligible([candidate.candidate_id])
    assert (promoted, warnings) == (1, ())
    home = agent.home_paths.owner_home_dir
    assert project_bindings.lookup(home, a) == ()
    assert project_bindings.lookup(home, b) == (Y,)
    assert not _prepare(agent, a).memories
    assert [row.attributes["scope_key"] for row in _prepare(agent, b).memories] == [Y]
    assert [row["scope_key"] for row in _search(agent, "y")] == [Y]
    assert str(b) not in str(agent.memory.all()[0].attributes)


def test_no_binding_for_provenance_free_or_reused_record(tmp_path):
    agent = _agent_with_current_user(tmp_path / "agent", content=QUERY)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = agent.home_paths.owner_home_dir
    first = RememberTool(agent).execute(_project_request(Y))
    assert first.ok and json.loads(first.output)["active_memory_changed"]
    agent._current_run_params.workspace_ref = freeze_workspace_ref(workspace)
    result = RememberTool(agent).execute(_project_request(Y))
    assert result.ok and not json.loads(result.output)["active_memory_changed"]
    assert not (home / "memory" / "project_bindings.json").exists()
    assert not _prepare(agent, workspace).memories


def test_nearest_canonical_binding_and_exact_query_references(tmp_path):
    agent = _agent_with_current_user(tmp_path / "agent", content=QUERY)
    home = agent.home_paths.owner_home_dir
    root = tmp_path / "tree"
    child = root / "child"
    child.mkdir(parents=True)
    link = tmp_path / "linked"
    link.symlink_to(child, target_is_directory=True)
    _add(agent.memory, "project", X)
    _add(agent.memory, "project", Y)
    project_bindings.record(home, X, root, alias="main-tree")
    project_bindings.record(home, Y, child, alias="child-tree")
    db = home / "memory" / "project_bindings.json"
    before = db.stat()
    project_bindings.record(home, Y, child)
    assert db.stat().st_ino == before.st_ino
    assert db.stat().st_mtime_ns == before.st_mtime_ns
    assert project_bindings.lookup(home, link) == (Y,)

    def allowed(query, workspace=None):
        scope = runtime_long_term_scope(agent.memory, purpose="auto_inject", workspace_root=workspace, query=query)
        assert not scope.compat_keys
        return tuple(key for key in (X, Y) if scope.allows("project", key))

    assert allowed("main-tree project:x", link) == (Y,)
    assert allowed("main-tree") == (X,)
    assert allowed("project:y") == (Y,)
    assert allowed("main-tree project:y") == (X, Y)
    assert allowed("main-tree-older project:xyz project:x/extension") == ()
    assert allowed("child tree") == ()
    child.rename(root / "retired")
    child.mkdir()
    assert project_bindings.lookup(home, child) == ()
    assert allowed("child-tree", child) == ()


@pytest.fixture
def matrix(tmp_path, request):
    owner = _agent_with_current_user(tmp_path / "owner", content=QUERY)
    owner.config.gateway_per_user_owner_scoping = True
    leases = ExitStack()
    request.addfinalizer(leases.close)
    foreign = _resolve_request_agent(owner, {
        "user_id": "foreign", "metadata": {"user_id": "foreign", "channel": "feishu"},
    }, leases)
    assert foreign.home_paths.owner_id != owner.home_paths.owner_id
    foreign._current_run_params = RunParams()
    paths = {name: tmp_path / relative for name, relative in {
        "unbound": "empty", "x": "projects/x/checkout", "nested": "projects/x/checkout/src",
        "x_worktree": "worktrees/other-name", "y": "projects/y/checkout",
        "same_name": "unregistered/checkout", "foreign": "foreign-project/checkout",
    }.items()}
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    home = owner.home_paths.owner_home_dir
    for key, name in [(X, "x"), (X, "x_worktree"), (Y, "y")]:
        project_bindings.record(home, key, paths[name])
    project_bindings.record(foreign.home_paths.owner_home_dir, X, paths["foreign"])
    assert project_bindings.lookup(home, paths["foreign"]) == ()

    base = {("personal", "personal"), ("global", "global")}
    owner_rows = {_add(owner.memory, scope, key).entry_id: key for scope, key in sorted(base) + [
        ("project", X), ("project", Y)]}
    foreign_rows = {_add(foreign.memory, "project", X).entry_id: X}

    def auto(agent, loc, rows, *, bound=None):
        scope = runtime_long_term_scope(agent.memory, purpose="auto_inject",
                                        workspace_root=paths[loc], query=QUERY, memory_recall_scope=bound)
        assert scope.compat_keys == ()
        return runtime_recall_predicate(scope, QUERY), set(rows)

    def explicit(project=""):
        return _scope_predicate(owner, owner.memory, "", QUERY, project=project), set(owner_rows)

    def suppressed(ctx, *, enabled=True):
        policy = SimpleNamespace(memory_enabled=enabled)
        assert formal_recall_suppressed(ctx, policy)
        owner.owner_policy = policy
        owner._current_run_params.context_scope = ctx
        try:
            assert not _prepare(owner, paths["x"], context_scope=ctx).memories
            assert not MemorySearchTool(owner).availability().available
        finally:
            owner.owner_policy = None
            owner._current_run_params.context_scope = "default"
        return lambda _: False, set(owner_rows)

    common = {"global", "personal"}
    cases = [
        ("unbound", auto(owner, "unbound", owner_rows), common),
        ("project_x", auto(owner, "nested", owner_rows), common | {X}),
        ("project_y", auto(owner, "y", owner_rows), common | {Y}),
        ("worktree_x", auto(owner, "x_worktree", owner_rows), common | {X}),
        ("same_name", auto(owner, "same_name", owner_rows), common),
        ("bound_space", auto(owner, "y", owner_rows, bound=MemoryRecallScope.from_runtime(
            task_attributes={"project_id": X})), common | {X}),
        ("explicit_y", explicit(Y), {Y}),
        ("explicit_owner", explicit(), common | {X, Y}),
        ("unknown_project", explicit("project:unknown"), set()),
        ("foreign_owner", auto(foreign, "foreign", foreign_rows), {X}),
        ("task_local", suppressed("task_local"), set()),
        ("control_plane", suppressed("control_plane"), set()),
        ("memory_disabled", suppressed("default", enabled=False), set()),
    ]
    # 矩阵先经真实正式检索，再将冻结谓词交给并发线程，避免把存储负载混入过滤器测试。
    for name, (predicate, ids), expected in cases:
        agent = foreign if name == "foreign_owner" else owner
        records, _ = agent.memory.search_scoped_candidates_report(QUERY, 10, predicate, latency_safe=True)
        assert all(row.entry_id in ids for row in records)
        assert {row.attributes["scope_key"] for row in records} == expected, name
    return cases, [*owner.memory.all(), *foreign.memory.all()]


@pytest.mark.parametrize("workers", [1, 16, 64, 128])
def test_parallel_scope_matrix(matrix, workers):
    cases, records = matrix
    ready = Barrier(workers)

    def verify(index):
        ready.wait(timeout=10)
        for offset in range(len(cases)):
            name, (predicate, ids), expected = cases[(index + offset) % len(cases)]
            selected = [row for row in records if row.entry_id in ids and predicate(row)]
            assert {row.attributes["scope_key"] for row in selected} == expected, name
        return len(cases)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        assert sum(executor.map(verify, range(workers))) == 13 * workers


def test_explicit_filter_keeps_bound_scope_and_task_scope_is_frozen(tmp_path):
    agent = _agent_with_current_user(tmp_path / "agent", content=QUERY)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project_bindings.record(agent.home_paths.owner_home_dir, Y, workspace)
    _add(agent.memory, "project", X)
    _add(agent.memory, "project", Y)
    bound = MemoryRecallScope.from_runtime(task_attributes={"project_id": X})
    agent._current_run_params.memory_recall_scope = bound
    assert [row["scope_key"] for row in _search(agent, "x")] == [X]
    assert _search(agent, "y") == []
    scope = runtime_long_term_scope(agent.memory, purpose="auto_inject", workspace_root=workspace,
                                    task_id="active", memory_recall_scope=bound, query=QUERY)
    assert scope.allows("project", "project:active") and scope.allows("project", X)
    assert not scope.allows("project", Y)


def test_committed_promotion_replays_failed_project_binding(tmp_path, monkeypatch):
    agent = _agent_with_current_user(tmp_path / "agent", content=QUERY)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agent._current_run_params.workspace_ref = freeze_workspace_ref(workspace)
    home = agent.home_paths.owner_home_dir
    request = _project_request(Y)
    tool = RememberTool(agent)

    def unavailable(*args, **kwargs):
        raise OSError("project binding write interrupted")

    with monkeypatch.context() as fault:
        fault.setattr(project_bindings, "write_json_file_atomic_unlocked", unavailable)
        result = tool.execute(request)
    assert result.ok, result.output
    saved = json.loads(result.output)
    [promoted] = saved["results"]
    assert saved["active_memory_changed"] and promoted["project_binding_status"] == "pending"
    assert project_bindings.lookup(home, workspace) == ()
    formal_before = agent.memory.path.read_bytes()

    replay = tool.execute(request)
    assert replay.ok, replay.output
    repeated = json.loads(replay.output)
    assert not repeated["active_memory_changed"]
    assert repeated["results"][0]["project_binding_status"] == "settled"
    assert project_bindings.lookup(home, workspace) == (Y,)
    assert agent.memory.path.read_bytes() == formal_before


@pytest.mark.parametrize("replacement", ["rebind", "newer_binding", "new_directory"])
def test_project_binding_replay_respects_current_registry_and_directory(tmp_path, monkeypatch, replacement):
    agent = _agent_with_current_user(tmp_path / "agent", content=QUERY)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agent._current_run_params.workspace_ref = freeze_workspace_ref(workspace)
    home = agent.home_paths.owner_home_dir
    project_bindings.record(home, X, workspace)
    [draft] = _prepare_observations(agent, agent.memory, [_project_request(Y)], scope=None)
    candidate = agent.memory_candidates.observe(draft)

    def unavailable(*args, **kwargs):
        raise OSError("project binding write interrupted")

    with monkeypatch.context() as fault:
        fault.setattr(project_bindings, "write_json_file_atomic_unlocked", unavailable)
        [result] = agent.memory_promotion.promote_batch([candidate], reviewer="test", automatic=True)
    assert result.promoted and result.formal_memory_changed and result.project_binding_status == "pending"
    assert project_bindings.lookup(home, workspace) == (X,)

    if replacement == "newer_binding":
        newer = RememberTool(agent).execute(_project_request("project:z"))
        assert newer.ok and json.loads(newer.output)["active_memory_changed"], newer.output
        expected = ("project:z",)
    elif replacement == "new_directory":
        workspace.rename(tmp_path / "retired")
        workspace.mkdir()
        expected = ()
    else:
        expected = (Y,)

    formal_before = agent.memory.path.read_bytes()
    updated = agent.memory_candidates.get(candidate.candidate_id)
    [replay] = agent.memory_promotion.promote_batch([updated], reviewer="test", automatic=True)
    assert not replay.formal_memory_changed
    assert replay.project_binding_status == ("settled" if replacement == "rebind" else "superseded")
    assert project_bindings.lookup(home, workspace) == expected
    assert agent.memory.path.read_bytes() == formal_before


def test_project_binding_acknowledges_already_published_generation(tmp_path, monkeypatch):
    agent = _agent_with_current_user(tmp_path / "agent", content=QUERY)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agent._current_run_params.workspace_ref = freeze_workspace_ref(workspace)
    home = agent.home_paths.owner_home_dir
    [draft] = _prepare_observations(agent, agent.memory, [_project_request(Y)], scope=None)
    candidate = agent.memory_candidates.observe(draft)
    original_write = project_bindings.write_json_file_atomic_unlocked

    def lose_confirmation(*args, **kwargs):
        original_write(*args, **kwargs)
        raise OSError("binding confirmation interrupted after publication")

    with monkeypatch.context() as fault:
        fault.setattr(project_bindings, "write_json_file_atomic_unlocked", lose_confirmation)
        result = agent.memory_promotion.promote(candidate.candidate_id, automatic=True)
    assert result.promoted and result.project_binding_status == "pending"
    assert project_bindings.lookup(home, workspace) == (Y,)
    registry = home / "memory" / "project_bindings.json"
    before = registry.stat()
    replay = agent.memory_promotion.promote(candidate.candidate_id, automatic=True)
    assert not replay.formal_memory_changed and replay.project_binding_status == "settled"
    after = registry.stat()
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)


@pytest.mark.parametrize("intervening_rebind", [False, True])
def test_project_rebind_recovers_after_one_predecessor_read_error(tmp_path, monkeypatch, intervening_rebind):
    agent = _agent_with_current_user(tmp_path / "agent", content=QUERY)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agent._current_run_params.workspace_ref = freeze_workspace_ref(workspace)
    home = agent.home_paths.owner_home_dir
    project_bindings.record(home, X, workspace)
    original_read = project_bindings.generation
    failures = []

    def read(owner_home, root):
        if not failures:
            failures.append(root)
            raise OSError("simulated predecessor read failure")
        return original_read(owner_home, root)

    tool = RememberTool(agent)
    request = _project_request(Y)
    with monkeypatch.context() as fault:
        fault.setattr(project_bindings, "generation", read)
        if intervening_rebind:
            def unavailable(*args, **kwargs):
                raise OSError("simulated binding publication failure")

            fault.setattr(project_bindings, "write_json_file_atomic_unlocked", unavailable)
        first = json.loads(tool.execute(request).output)
    assert failures == [workspace.resolve()]
    assert first["active_memory_changed"]
    assert first["results"][0]["project_binding_status"] == ("pending" if intervening_rebind else "settled")
    formal_before = agent.memory.path.read_bytes()
    if intervening_rebind:
        project_bindings.record(home, "project:z", workspace)
    replay = json.loads(tool.execute(request).output)
    assert replay["results"][0]["project_binding_status"] == ("superseded" if intervening_rebind else "settled")
    assert project_bindings.lookup(home, workspace) == (("project:z",) if intervening_rebind else (Y,))
    assert not replay["active_memory_changed"]
    assert agent.memory.path.read_bytes() == formal_before


@pytest.mark.parametrize("batch", [False, True])
def test_unavailable_project_predecessor_defers_before_formal_commit(tmp_path, monkeypatch, batch):
    agent = _agent_with_current_user(tmp_path / "agent", content=QUERY)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agent._current_run_params.workspace_ref = freeze_workspace_ref(workspace)
    home = agent.home_paths.owner_home_dir
    project_bindings.record(home, X, workspace)
    [draft] = _prepare_observations(agent, agent.memory, [_project_request(Y)], scope=None)
    candidate = agent.memory_candidates.observe(draft)
    service = agent.memory_promotion

    def promote():
        if batch:
            return service.promote_batch([agent.memory_candidates.get(candidate.candidate_id)],
                                         reviewer="test", automatic=True)[0]
        return service.promote(candidate.candidate_id, automatic=True)

    def unavailable(*args):
        raise OSError("simulated predecessor read failure")

    with monkeypatch.context() as fault:
        fault.setattr(project_bindings, "generation", unavailable)
        blocked = promote()
    assert not blocked.promoted and not blocked.formal_memory_changed
    assert blocked.status == "approved" and blocked.project_binding_status == "pending"
    assert project_bindings.lookup(home, workspace) == (X,)
    waiting = promote()
    assert waiting.reason_code == "COMMIT_OBLIGATION_BACKOFF" and waiting.project_binding_status == "pending"
    obligation = agent.memory_candidates.get(candidate.candidate_id)
    _advance_obligation_clock(monkeypatch, datetime.fromisoformat(obligation.obligation_retry_after))
    assert promote().project_binding_status == "settled"
    assert project_bindings.lookup(home, workspace) == (Y,)
