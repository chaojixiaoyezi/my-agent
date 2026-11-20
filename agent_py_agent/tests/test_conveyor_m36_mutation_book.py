import ast
import json
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import _finalization_service as finalization
from agent_py_agent.agent.agent_core import _tool_loop_service as loop
from agent_py_agent.agent.assets import mutation_ledger
from agent_py_agent.agent.backends.base import EchoBackend
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig


def _agent(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "owner"
    home.mkdir()
    return SimpleNamespace(
        root=workspace,
        home_paths=SimpleNamespace(owner_home_dir=home, owner_id="owner"),
        config=SimpleNamespace(enable_self_learning=True),
    )


def _context(params, **overrides):
    fields = {
        "run_id": params.run_id,
        "attempt_id": params.attempt_id,
        "request_id": params.run_id,
        "do_save": True,
        "context_scope": "default",
        "archive_tool_calls": [],
        "tool_runtime_evidence": {},
        "user_prompt": "",
        "task_attributes": {"conversation_thread_id": "thread"},
    }
    return SimpleNamespace(**(fields | overrides))


def _observe(agent, params, tool, arguments, output):
    record = SimpleNamespace(
        params=params,
        call=SimpleNamespace(tool_name=tool, arguments=arguments, call_id=tool),
        result=SimpleNamespace(ok=True, output=output, error_code=""),
    )
    loop._observe_turn_asset_mutations(agent, record, {})


@pytest.mark.parametrize("capacity", [64, 128])
def test_c128_mutation_book(tmp_path, capacity):
    agent = _agent(tmp_path)
    ready = Barrier(capacity, timeout=30)

    def run(index):
        run_id = f"run-{index}"
        path = f"candidate-{index}.py"
        (agent.root / path).write_text(f"print({index})\n", encoding="utf-8")
        params = SimpleNamespace(run_id=run_id, attempt_id=f"attempt-{index}")
        book = loop.MutationBook(run_id, params.attempt_id, [])
        with loop.mutation_book_scope(book):
            _observe(agent, params, "write_file", {"path": path}, {})
            _observe(agent, params, "run_command", {"command": f"python {path}"}, {"exit_code": 0})
        ready.wait()
        ctx = _context(params, mutation_book=book)
        finalization._freeze_turn_asset_mutations(agent, ctx)
        return ctx.tool_runtime_evidence.get("asset_evolution_run_id")

    with ThreadPoolExecutor(max_workers=capacity) as pool:
        registrations = list(pool.map(run, range(capacity)))

    expected_ids = {f"run-{index}" for index in range(capacity)}
    assert sum(run_id is not None for run_id in registrations) == capacity
    assert set(registrations) == expected_ids
    directory = agent.home_paths.owner_home_dir / "data" / "asset_evolution" / "requests"
    assert {path.stem for path in directory.glob("*.json")} == expected_ids
    for index in range(capacity):
        request = json.loads((directory / f"run-{index}.json").read_text(encoding="utf-8"))
        assert request["schema_version"] == "asset_evolution_request.v1"
        assert request["run_id"] == f"run-{index}"
        [target] = request["targets"]
        assert target["path"] == str(agent.root / f"candidate-{index}.py")


def test_no_agent_wide_mutation_map():
    for module in (loop, finalization):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        assert not any(
            isinstance(node, ast.Attribute) and node.attr == "_asset_mutation_states"
            or isinstance(node, ast.Constant) and node.value == "_asset_mutation_states"
            for node in ast.walk(tree)
        ), module.__name__


def test_empty_book_skips_registration(tmp_path):
    agent = _agent(tmp_path)
    book = loop.MutationBook("run-empty", "attempt", [])
    with loop.mutation_book_scope(book):
        _observe(agent, book, "read_file", {"path": "candidate.py"}, {})
    ctx = _context(book, mutation_book=book)
    finalization._freeze_turn_asset_mutations(agent, ctx)
    assert not book.registered
    assert not ctx.tool_runtime_evidence
    assert not (agent.home_paths.owner_home_dir / "data" / "asset_evolution").exists()


def test_agent_run_hands_off_the_observed_book_to_finalization(tmp_path):
    config = AgentConfig(my_agent_home=str(tmp_path / "home"), enable_self_learning=True)
    agent = SimpleAgent(config, tmp_path / "workspace")
    agent.backend = EchoBackend()
    books = []

    def driver(host, params):
        book = loop.current_mutation_book(params.run_id, params.attempt_id)
        assert book is not None
        books.append(book)
        (host.root / "candidate.py").write_text("print(1)\n", encoding="utf-8")
        _observe(host, params, "write_file", {"path": "candidate.py"}, {})
        _observe(host, params, "run_command", {"command": "python candidate.py"}, {"exit_code": 0})
        return loop.execute_tool_loop(host, params)

    agent._loop_driver = driver
    result = agent.run("核对候选脚本", source="chat", save=True, resume_context=False, allowed_tools=[])
    assert result.runtime_status == "ok"
    [book] = books
    assert book.registered
    assert loop.current_mutation_book(book.run_id, book.attempt_id) is None
    path = agent.home_paths.owner_home_dir / "data" / "asset_evolution" / "requests" / f"{book.run_id}.json"
    request = json.loads(path.read_text(encoding="utf-8"))
    assert request["run_id"] == book.run_id
    assert request["targets"][0]["path"] == str(agent.root / "candidate.py")


def test_registration_checks_attempt_and_retries_once_after_io_error(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    book = loop.MutationBook("run-retry", "attempt-current", [])
    stale = SimpleNamespace(run_id=book.run_id, attempt_id="attempt-stale")
    with loop.mutation_book_scope(book):
        worker_context = copy_context()
        candidate = agent.root / "candidate.py"
        candidate.write_text("print(1)\n", encoding="utf-8")
        _observe(agent, stale, "write_file", {"path": candidate.name}, {})
        assert not book.mutations
        params = SimpleNamespace(run_id=book.run_id, attempt_id=book.attempt_id, live_archive_state={})
        _observe(agent, params, "write_file", {"path": candidate.name}, {})
        # Compact 换用新的归档状态后，执行回执仍与原写入对应。
        continued = SimpleNamespace(**(vars(params) | {"live_archive_state": {}}))
        _observe(agent, continued, "run_command", {"command": "python candidate.py"}, {"exit_code": 0})
    assert loop.current_mutation_book(book.run_id, book.attempt_id) is None
    assert worker_context.run(loop.current_mutation_book, book.run_id, book.attempt_id) is None
    original = mutation_ledger.freeze_asset_mutations
    calls = []

    def freeze(*args, **kwargs):
        calls.append(kwargs["run_id"])
        if len(calls) == 1:
            raise OSError("simulated I/O error")
        return original(*args, **kwargs)

    monkeypatch.setattr(mutation_ledger, "freeze_asset_mutations", freeze)
    finalization._freeze_turn_asset_mutations(agent, _context(stale, mutation_book=book))
    assert not calls
    ctx = _context(book, mutation_book=book)
    finalization._freeze_turn_asset_mutations(agent, ctx)
    assert not book.registered
    assert not ctx.tool_runtime_evidence

    ready = Barrier(2, timeout=10)

    def finalize():
        ready.wait()
        finalization._freeze_turn_asset_mutations(agent, ctx)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(finalize), pool.submit(finalize)
        first.result()
        second.result()
    assert calls == [book.run_id, book.run_id]
    assert book.registered
    assert ctx.tool_runtime_evidence == {"asset_evolution_run_id": book.run_id}
