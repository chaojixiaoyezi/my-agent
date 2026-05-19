from __future__ import annotations

"""gateway client regression tests."""

import json
from argparse import Namespace
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.models import AgentRunResult
from agent_py_agent.agent.backends.errors import ProviderTimeoutError
from agent_py_agent.agent.cards import CardStore, TaskStatus
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import (
    GatewayAskParams,
    GatewayPaths,
    gateway_paths,
    gateway_response_path,
    read_json_file,
    submit_gateway_ask,
    write_gateway_request,
)
from agent_py_agent.agent.gateway_parts import runtime as gateway_runtime
from agent_py_agent.agent.gateway_parts.request_execution import (
    _gateway_background_recovery_max_cycles,
    _GatewayAskRunContext,
    _handle_gateway_request,
    _run_gateway_ask,
)
from agent_py_agent.cli import gateway_client
from agent_py_agent.cli.chat_parts.gateway_client import poll_gateway_chunks


def test_default_gateway_entry_can_reach_chat_handler():
    """默认 gateway 入口必须能找到 chat 处理函数。"""

    assert callable(gateway_client.cmd_chat)


def test_gateway_json_polling_suppresses_stream_chunks(tmp_path, capsys):
    """`gateway ask --json` must keep stdout parseable JSON, without streamed text before it."""

    request_id = "gw-json"
    paths = GatewayPaths(
        root=tmp_path,
        pid=tmp_path / "gateway.pid",
        adapter_pid=tmp_path / "adapter.pid",
        state=tmp_path / "state.json",
        heartbeat=tmp_path / "heartbeat.json",
        stop_request=tmp_path / "stop.request",
        log=tmp_path / "gateway.log",
        inbox=tmp_path / "pending",
        processing=tmp_path / "processing",
        done=tmp_path / "done",
        failed=tmp_path / "failed",
        responses=tmp_path / "responses",
        history=tmp_path / "history.jsonl",
    )
    paths.processing.mkdir(parents=True)
    paths.responses.mkdir(parents=True)
    (paths.processing / f"{request_id}.chunks.jsonl").write_text(
        json.dumps({"text": "STREAMED"}) + "\n",
        encoding="utf-8",
    )
    response_path = paths.responses / f"{request_id}.json"
    response_path.write_text(json.dumps({"ok": True, "response": "DONE"}), encoding="utf-8")

    payload = gateway_client._poll_gateway_response(
        gateway_client.GatewayAskContext(
            agent=object(),
            paths=paths,
            request_id=request_id,
            request_path=paths.processing / f"{request_id}.json",
            response_path=response_path,
            timeout=1,
            stream_output=False,
        )
    )

    assert payload["response"] == "DONE"
    assert capsys.readouterr().out == ""


def test_chat_gateway_poll_drains_chunks_when_response_is_ready(tmp_path):
    from agent_py_agent.cli.chat_parts.gateway_client import GatewayChunkPollRequest

    chunk_path = tmp_path / "req.chunks.jsonl"
    response_path = tmp_path / "response.json"
    chunk_path.write_text(
        json.dumps({"text": "hello"}) + "\n" + json.dumps({"text": " world"}) + "\n",
        encoding="utf-8",
    )
    response_path.write_text(json.dumps({"ok": True, "response": "hello world"}), encoding="utf-8")
    seen: list[str] = []
    visible_chunks_ref = [0]

    response = poll_gateway_chunks(
        GatewayChunkPollRequest(
            chunk_path,
            response_path,
            9999999999,
            lambda chunk: seen.append(chunk) or True,
            [0],
            visible_chunks_ref,
        )
    )

    assert response["response"] == "hello world"
    assert seen == ["hello", " world"]
    assert visible_chunks_ref == [2]


def test_chat_gateway_poll_consumes_but_does_not_show_invisible_chunks(tmp_path):
    from agent_py_agent.cli.chat_parts.gateway_client import GatewayChunkPollRequest

    chunk_path = tmp_path / "req.chunks.jsonl"
    response_path = tmp_path / "response.json"
    chunk_path.write_text(
        json.dumps({"text": "   \n"}) + "\n",
        encoding="utf-8",
    )
    response_path.write_text(json.dumps({"ok": True, "response": "fallback"}), encoding="utf-8")

    chunks_printed_ref = [0]
    visible_chunks_ref = [0]
    response = poll_gateway_chunks(
        GatewayChunkPollRequest(
            chunk_path,
            response_path,
            9999999999,
            lambda _chunk: False,
            chunks_printed_ref,
            visible_chunks_ref,
        )
    )

    assert response["response"] == "fallback"
    assert chunks_printed_ref == [1]
    assert visible_chunks_ref == [0]


def test_gateway_worker_continues_when_processing_lease_write_fails(tmp_path, monkeypatch):
    """A lease file write failure must not strand a user request in processing."""

    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            gateway_workspace="gateway",
            gateway_background_model_request_timeout=777,
            local_store_path="local_store/local.db",
            local_store_files_dir="local_store/files",
            local_store_events_path="local_store/events.jsonl",
        ),
        tmp_path,
    )
    paths = gateway_paths(agent)
    request_id = "gwreq-lease-fallback"
    write_gateway_request(
        paths,
        {
            "id": request_id,
            "kind": "ask",
            "prompt": "lease fallback should still answer",
            "inject": [],
            "prompt_files": [],
            "save": False,
            "include_prompt": False,
            "created_at": 1.0,
            "status": "pending",
            "attempts": 0,
        },
    )

    def fail_processing_lease_write(path, payload):
        if path.name == f"{request_id}.json":
            raise OSError("simulated long-path lease write failure")
        return None

    monkeypatch.setattr(gateway_runtime, "write_json_file_atomic", fail_processing_lease_write)

    processed = gateway_runtime._process_gateway_requests(agent, paths)

    assert processed == 1
    assert gateway_response_path(paths, request_id).exists()
    assert (paths.done / f"{request_id}.json").exists()
    assert not (paths.processing / f"{request_id}.json").exists()
    assert read_json_file(gateway_response_path(paths, request_id))["ok"] is True


def test_gateway_no_wait_request_creates_runtime_task_card(tmp_path):
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            gateway_workspace="gateway",
            local_store_path="local_store/local.db",
            local_store_files_dir="local_store/files",
            local_store_events_path="local_store/events.jsonl",
        ),
        tmp_path,
    )
    paths = gateway_paths(agent)

    request_id, request_path, _response_path = submit_gateway_ask(
        paths,
        params=GatewayAskParams(
            prompt="run long background work",
            client_wait=False,
            agent=agent,
            task_attributes={"delivery_contract_file": "contract.json"},
        ),
    )

    payload = read_json_file(request_path)
    tasks = CardStore(agent.runtime_cards_root).list_tasks()
    assert len(tasks) == 1
    assert payload["runtime_task_id"] == tasks[0].task_id
    assert tasks[0].metadata["gateway_request_id"] == request_id
    assert tasks[0].metadata["delivery_contract_file"] == "contract.json"


def test_gateway_no_wait_completion_updates_runtime_task_card(tmp_path):
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            gateway_workspace="gateway",
            local_store_path="local_store/local.db",
            local_store_files_dir="local_store/files",
            local_store_events_path="local_store/events.jsonl",
        ),
        tmp_path,
    )
    paths = gateway_paths(agent)
    _request_id, request_path, _response_path = submit_gateway_ask(
        paths,
        params=GatewayAskParams(
            prompt="complete background work",
            client_wait=False,
            agent=agent,
        ),
    )
    task_id = read_json_file(request_path)["runtime_task_id"]

    response = _handle_gateway_request(agent, request_path)

    task = CardStore(agent.runtime_cards_root).get_task(task_id)
    assert response["ok"] is True
    assert task.status == TaskStatus.COMPLETED
    assert task.artifact_refs == [str(_response_path)]


def test_gateway_control_plane_ask_uses_deterministic_board_snapshot(tmp_path):
    agent = SimpleNamespace()
    agent.run = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("model should not run"))
    agent.subagents = SimpleNamespace(
        write_board=lambda recent_limit=10: SimpleNamespace(
            summary={"total": 1, "RUNNING": 1},
            items=[
                SimpleNamespace(
                    id="subagent-1",
                    status="RUNNING",
                    verification_status="UNVERIFIED",
                )
            ],
        )
    )

    result = _run_gateway_ask(
        _GatewayAskRunContext(
            agent=agent,
            request={"prompt": "后台任务状态？", "context_scope": "control_plane", "save": True},
            request_path=tmp_path / "request.json",
            response_path=tmp_path / "response.json",
            request_id="gw-control",
            on_chunk=None,
        )
    )

    assert result.backend == "control_plane"
    assert result.tool_rounds == 0
    assert "subagent-1" in result.response
    assert "RUNNING 1" in result.response


def test_gateway_control_plane_uses_closeout_resolution_for_covered_blockers(tmp_path):
    stale = SimpleNamespace(
        id="stale-run",
        status="BLOCKED",
        verification_status="UNVERIFIED",
        attributes={"artifact_refs": ["lab_outputs/shop-demo/index.html"]},
        output_json="",
        result="",
        takeover_by="",
    )
    repair = SimpleNamespace(
        id="repair-run",
        status="DONE",
        verification_status="VERIFIED",
        attributes={"artifact_refs": ["lab_outputs/shop-demo/index.html"]},
        output_json="",
        result="",
        takeover_by="",
    )
    agent = SimpleNamespace()
    agent.run = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("model should not run"))
    agent.subagents = SimpleNamespace(
        list_runs=lambda: [stale, repair],
        write_board=lambda recent_limit=10: SimpleNamespace(
            summary={"total": 2, "BLOCKED": 1, "DONE": 1},
            items=[stale, repair],
        ),
    )

    result = _run_gateway_ask(
        _GatewayAskRunContext(
            agent=agent,
            request={"prompt": "后台任务状态？", "context_scope": "control_plane", "save": True},
            request_path=tmp_path / "request.json",
            response_path=tmp_path / "response.json",
            request_id="gw-control-covered",
            on_chunk=None,
        )
    )

    assert result.backend == "control_plane"
    assert "暂无阻塞任务" in result.response
    assert "stale-run" not in result.response
    assert "UNVERIFIED" not in result.response


def test_gateway_control_plane_surfaces_background_main_requests(tmp_path):
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            gateway_workspace="gateway",
            gateway_background_model_request_timeout=777,
            local_store_path="local_store/local.db",
            local_store_files_dir="local_store/files",
            local_store_events_path="local_store/events.jsonl",
        ),
        tmp_path,
    )
    agent.run = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("model should not run"))  # type: ignore[method-assign]
    agent.subagents = SimpleNamespace(  # type: ignore[assignment]
        write_board=lambda recent_limit=10: SimpleNamespace(summary={"total": 0}, items=[]),
        list_runs=lambda: [],
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    request_id = "gw-background-status"
    (paths.processing / f"{request_id}.json").write_text(
        json.dumps(
            {
                "id": request_id,
                "kind": "ask",
                "prompt": "background",
                "client_wait": False,
                "status": "processing",
                "created_at": 1.0,
                "updated_at": 2.0,
            }
        ),
        encoding="utf-8",
    )
    (paths.processing / f"{request_id}.chunks.jsonl").write_text(
        json.dumps({"t": 3.0, "text": "working on XLSX rows"}) + "\n",
        encoding="utf-8",
    )

    result = _run_gateway_ask(
        _GatewayAskRunContext(
            agent=agent,
            request={"prompt": "后台任务状态？", "context_scope": "control_plane", "save": True},
            request_path=tmp_path / "request.json",
            response_path=tmp_path / "response.json",
            request_id="gw-control-background",
            on_chunk=None,
        )
    )

    assert result.backend == "control_plane"
    assert "后台主代理请求 1 个" in result.response
    assert request_id in result.response
    assert "working on XLSX rows" in result.response


# LLM: gateway ask --no-wait must mark the request as background work at enqueue time.
# 函数用途: 防止长任务和前台聊天进入同一优先通道，导致用户查询被后台任务占满。
def test_gateway_ask_no_wait_marks_background_request(tmp_path, monkeypatch, capsys):
    submitted = {}
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            gateway_workspace="gateway",
            gateway_background_model_request_timeout=777,
            local_store_path="local_store/local.db",
            local_store_files_dir="local_store/files",
            local_store_events_path="local_store/events.jsonl",
        ),
        tmp_path,
    )
    paths = gateway_paths(agent)

    def fake_submit_gateway_ask(_paths, *, params):
        submitted["params"] = params
        return "gw-background", paths.inbox / "gw-background.json", paths.responses / "gw-background.json"

    monkeypatch.setattr(gateway_client, "make_agent", lambda args: agent)
    monkeypatch.setattr(gateway_client, "wait_for_gateway_running", lambda _paths, timeout=10.0: (123, True))
    monkeypatch.setattr(gateway_client, "submit_gateway_ask", fake_submit_gateway_ask)

    code = gateway_client.cmd_gateway_ask(Namespace(
        prompt="后台长任务",
        inject=[],
        prompt_file=[],
        no_save=False,
        show_prompt=False,
        no_wait=True,
        timeout=None,
        json=False,
        config="",
        resume_context=None,
    ))

    assert code == 0
    assert submitted["params"].client_wait is False
    assert "queued request_id=gw-background" in capsys.readouterr().out


# LLM: gateway foreground probes can request control-plane context without prompt-text heuristics.
# 函数用途: 确保状态查询类前台交互能用机器参数走轻上下文，避免长任务期间加载完整用户上下文。
def test_gateway_ask_passes_context_scope(tmp_path, monkeypatch):
    submitted = {}
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            gateway_workspace="gateway",
            local_store_path="local_store/local.db",
            local_store_files_dir="local_store/files",
            local_store_events_path="local_store/events.jsonl",
        ),
        tmp_path,
    )
    paths = gateway_paths(agent)

    def fake_submit_gateway_ask(_paths, *, params):
        submitted["params"] = params
        return "gw-control", paths.inbox / "gw-control.json", paths.responses / "gw-control.json"

    monkeypatch.setattr(gateway_client, "make_agent", lambda args: agent)
    monkeypatch.setattr(gateway_client, "wait_for_gateway_running", lambda _paths, timeout=10.0: (123, True))
    monkeypatch.setattr(gateway_client, "submit_gateway_ask", fake_submit_gateway_ask)
    monkeypatch.setattr(gateway_client, "_poll_gateway_response", lambda ctx: {"ok": True, "response": "ok"})
    monkeypatch.setattr(gateway_client, "print_gateway_response", lambda response, **kwargs: 0)

    code = gateway_client.cmd_gateway_ask(Namespace(
        prompt="状态",
        inject=[],
        prompt_file=[],
        no_save=False,
        show_prompt=False,
        no_wait=False,
        timeout=45,
        json=True,
        config="",
        resume_context=None,
        context_scope="control_plane",
    ))

    assert code == 0
    assert submitted["params"].context_scope == "control_plane"


# LLM: gateway ask delivery contracts must enter the request as structured task attributes.
# 函数用途: 防止后台交付验收只存在于 prompt 文本里，确保 tool loop 能读取 delivery_contract_file。
def test_gateway_ask_passes_delivery_contract_as_task_attributes(tmp_path, monkeypatch):
    submitted = {}
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            gateway_workspace="gateway",
            local_store_path="local_store/local.db",
            local_store_files_dir="local_store/files",
            local_store_events_path="local_store/events.jsonl",
        ),
        tmp_path,
    )
    paths = gateway_paths(agent)
    contract = tmp_path / "delivery_contract.json"
    contract.write_text('{"checks":[{"path":"out.txt"}]}', encoding="utf-8")

    def fake_submit_gateway_ask(_paths, *, params):
        submitted["params"] = params
        return "gw-contract", paths.inbox / "gw-contract.json", paths.responses / "gw-contract.json"

    monkeypatch.setattr(gateway_client, "make_agent", lambda args: agent)
    monkeypatch.setattr(gateway_client, "wait_for_gateway_running", lambda _paths, timeout=10.0: (123, True))
    monkeypatch.setattr(gateway_client, "submit_gateway_ask", fake_submit_gateway_ask)

    code = gateway_client.cmd_gateway_ask(Namespace(
        prompt="交付",
        inject=[],
        prompt_file=[],
        no_save=False,
        show_prompt=False,
        no_wait=True,
        timeout=None,
        json=False,
        config="",
        resume_context=None,
        context_scope="default",
        delivery_contract_file=str(contract),
    ))

    assert code == 0
    assert submitted["params"].task_attributes["delivery_contract_file"] == str(contract)
    assert submitted["params"].task_attributes["max_tool_rounds"] == 32
    assert submitted["params"].task_attributes["parent_product_write"] == "allow"
    assert submitted["params"].task_attributes["parent_body_read"] == "allow"


# LLM: gateway worker must pass persisted non-control context_scope into SimpleAgent.run.
# 函数用途: control_plane 已是确定性快路径；其它 scope 仍必须进入主代理运行参数。
def test_gateway_worker_runs_with_persisted_context_scope(tmp_path):
    seen = {}
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            gateway_workspace="gateway",
            local_store_path="local_store/local.db",
            local_store_files_dir="local_store/files",
            local_store_events_path="local_store/events.jsonl",
        ),
        tmp_path,
    )
    paths = gateway_paths(agent)
    submit_gateway_ask = gateway_client.submit_gateway_ask
    submit_gateway_ask(
        paths,
        params=gateway_client.GatewayAskParams(
            prompt="status",
            save=False,
            context_scope="task_local",
        ),
    )
    original_run = agent.run

    def fake_run(user_prompt: str, **kwargs):
        seen["context_scope"] = kwargs.get("context_scope") or getattr(kwargs.get("params"), "context_scope", "")
        return AgentRunResult(prompt="", response="ok", backend="test", used_memories=0)

    agent.run = fake_run  # type: ignore[method-assign]
    try:
        gateway_runtime._process_gateway_requests(agent, paths)
    finally:
        agent.run = original_run  # type: ignore[method-assign]

    assert seen["context_scope"] == "task_local"


# LLM: no-wait gateway intake must be a structured runtime flag, not a prompt convention.
# 函数用途: 确认后台长任务进入主代理时携带机器字段，后续工具层可据此延后 runner 执行。
def test_gateway_no_wait_sets_background_intake_run_param(tmp_path):
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            gateway_workspace="gateway",
            gateway_background_model_request_timeout=777,
            local_store_path="local_store/local.db",
            local_store_files_dir="local_store/files",
            local_store_events_path="local_store/events.jsonl",
        ),
        tmp_path,
    )
    seen = {}

    def fake_run(prompt, *, params):
        seen["prompt"] = prompt
        seen["background_intake"] = params.background_intake
        seen["model_request_timeout_seconds"] = params.model_request_timeout_seconds
        return AgentRunResult(prompt="", response="queued", backend="test-root", used_memories=0)

    original_run = agent.run
    agent.run = fake_run  # type: ignore[method-assign]
    try:
        _run_gateway_ask(
            _GatewayAskRunContext(
                agent=agent,
                request={
                    "id": "gw-no-wait",
                    "kind": "ask",
                    "prompt": "后台长任务",
                    "client_wait": False,
                },
                request_path=tmp_path / "request.json",
                response_path=tmp_path / "response.json",
                request_id="gw-no-wait",
                on_chunk=None,
            )
        )
    finally:
        agent.run = original_run  # type: ignore[method-assign]

    assert seen["prompt"] == "后台长任务"
    assert seen["background_intake"] is True
    assert seen["model_request_timeout_seconds"] == 777


# LLM: persisted gateway task attributes must reach SimpleAgent.run unchanged.
# 函数用途: 验证 gateway 队列里的机器合同字段能透传到 run 参数，不需要解析 prompt。
def test_gateway_worker_passes_persisted_task_attributes(tmp_path):
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            gateway_workspace="gateway",
            local_store_path="local_store/local.db",
            local_store_files_dir="local_store/files",
            local_store_events_path="local_store/events.jsonl",
        ),
        tmp_path,
    )
    seen = {}

    def fake_run(prompt, *, params):
        seen["task_attributes"] = params.task_attributes
        return AgentRunResult(prompt="", response="queued", backend="test-root", used_memories=0)

    original_run = agent.run
    agent.run = fake_run  # type: ignore[method-assign]
    try:
        _run_gateway_ask(
            _GatewayAskRunContext(
                agent=agent,
                request={
                    "id": "gw-contract",
                    "kind": "ask",
                    "prompt": "后台交付",
                    "client_wait": False,
                    "task_attributes": {"delivery_contract_file": "contract.json"},
                },
                request_path=tmp_path / "request.json",
                response_path=tmp_path / "response.json",
                request_id="gw-contract",
                on_chunk=None,
            )
        )
    finally:
        agent.run = original_run  # type: ignore[method-assign]

    assert seen["task_attributes"] == {"delivery_contract_file": "contract.json"}


# LLM: background gateway recovery must continue task-card work after the root model times out.
# 函数用途: 覆盖 no-wait 长任务：主模型已创建子代理但超时，gateway 不能把 PLANNING 任务晾死。
def test_background_gateway_timeout_salvages_existing_subagent_work(tmp_path):
    seen = {}
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            gateway_workspace="gateway",
            local_store_path="local_store/local.db",
            local_store_files_dir="local_store/files",
            local_store_events_path="local_store/events.jsonl",
            dispatch_default_max_runners=1,
            dispatch_default_limit=5,
        ),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    request_path = paths.processing / "gw-timeout.json"
    request_path.write_text(
        json.dumps(
            {
                "id": "gw-timeout",
                "kind": "ask",
                "prompt": "后台长任务",
                "client_wait": False,
                "save": False,
                "created_at": 1.0,
            }
        ),
        encoding="utf-8",
    )

    fake_subagents = SimpleNamespace()
    fake_subagents.workspace = tmp_path / "subagents"
    fake_subagents.items = [
        SimpleNamespace(id="subagent-1", status="PLANNING", verification_status="UNVERIFIED")
    ]
    fake_subagents.list_runs = lambda: list(fake_subagents.items)
    fake_subagents.write_board = lambda recent_limit=10: SimpleNamespace(
        summary={"total": len(fake_subagents.items)},
        items=list(fake_subagents.items),
    )
    agent.subagents = fake_subagents  # type: ignore[assignment]

    def fake_run(*args, **kwargs):
        raise ProviderTimeoutError("root model timeout after creating child")

    def fake_watch_subagents(*args, **kwargs):
        seen["watch_called"] = True
        raise AssertionError("background intake request worker must not run runner recovery inline")

    original_run = agent.run
    original_watch = agent.watch_subagents
    agent.run = fake_run  # type: ignore[method-assign]
    agent.watch_subagents = fake_watch_subagents  # type: ignore[method-assign]
    try:
        response = _handle_gateway_request(agent, request_path)
    finally:
        agent.run = original_run  # type: ignore[method-assign]
        agent.watch_subagents = original_watch  # type: ignore[method-assign]

    assert response["ok"] is True
    assert response["status"] == "done"
    assert response["backend"] == "gateway_subagent_deferred"
    assert response["gateway_recovered_from_error"] == "ProviderTimeoutError"
    assert seen.get("watch_called") is not True


# LLM: background no-wait tasks may return a normal blocker notice before repair acceptance is exhausted.
# 函数用途: 覆盖真实 MiniMax 长任务：主代理正常结束但仍有可推进任务卡时，gateway 应继续系统调度而不是把 blocker notice 当最终结果。
def test_background_gateway_notice_salvages_existing_subagent_work_after_normal_result(tmp_path):
    seen = {}
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            gateway_workspace="gateway",
            local_store_path="local_store/local.db",
            local_store_files_dir="local_store/files",
            local_store_events_path="local_store/events.jsonl",
            dispatch_default_max_runners=1,
            dispatch_default_limit=5,
        ),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    request_path = paths.processing / "gw-notice.json"
    request_path.write_text(
        json.dumps(
            {
                "id": "gw-notice",
                "kind": "ask",
                "prompt": "后台长任务",
                "client_wait": False,
                "save": False,
                "created_at": 1.0,
            }
        ),
        encoding="utf-8",
    )
    fake_subagents = SimpleNamespace()
    fake_subagents.workspace = tmp_path / "subagents"
    fake_subagents.items = [
        SimpleNamespace(id="subagent-1", status="AWAITING_ACCEPTANCE", verification_status="NEEDS_ACCEPTANCE")
    ]
    fake_subagents.list_runs = lambda: list(fake_subagents.items)
    fake_subagents.write_board = lambda recent_limit=10: SimpleNamespace(
        summary={"total": len(fake_subagents.items)},
        items=list(fake_subagents.items),
    )
    agent.subagents = fake_subagents  # type: ignore[assignment]

    def fake_run(*args, **kwargs):
        return AgentRunResult(
            prompt="",
            response="## Subagent State Notice\n- blocking_run_ids: subagent-1",
            backend="test-root",
            used_memories=0,
        )

    def fake_watch_subagents(*args, **kwargs):
        seen["watch_called"] = True
        raise AssertionError("background intake request worker must not run runner recovery inline")

    original_run = agent.run
    original_watch = agent.watch_subagents
    agent.run = fake_run  # type: ignore[method-assign]
    agent.watch_subagents = fake_watch_subagents  # type: ignore[method-assign]
    try:
        response = _handle_gateway_request(agent, request_path)
    finally:
        agent.run = original_run  # type: ignore[method-assign]
        agent.watch_subagents = original_watch  # type: ignore[method-assign]

    assert response["ok"] is True
    assert response["status"] == "done"
    assert response["backend"] == "gateway_subagent_deferred"
    assert response["gateway_recovered_after_result"] == "test-root"
    assert seen.get("watch_called") is not True


# LLM: background recovery must have enough cycles for repair -> takeover -> parent acceptance chains.
# 函数用途: 覆盖真实 MiniMax 购物站长链路，避免 gateway 恢复刚生成待验收接管任务就停止。
def test_background_gateway_recovery_cycle_budget_covers_takeover_acceptance_chain():
    assert _gateway_background_recovery_max_cycles(SimpleNamespace(dispatch_max_consecutive_rounds=20)) == 60
    assert _gateway_background_recovery_max_cycles(SimpleNamespace(dispatch_max_consecutive_rounds=3)) == 3


# LLM: foreground gateway requests must not be converted into long background dispatch recovery.
# 函数用途: 确保前台聊天通道遇到模型异常时不占用 worker 去跑长任务恢复。
def test_foreground_gateway_timeout_does_not_run_subagent_salvage(tmp_path):
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            gateway_workspace="gateway",
            local_store_path="local_store/local.db",
            local_store_files_dir="local_store/files",
            local_store_events_path="local_store/events.jsonl",
        ),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    request_path = paths.processing / "gw-foreground-timeout.json"
    request_path.write_text(
        json.dumps(
            {
                "id": "gw-foreground-timeout",
                "kind": "ask",
                "prompt": "前台聊天",
                "client_wait": True,
                "save": False,
                "created_at": 1.0,
            }
        ),
        encoding="utf-8",
    )
    agent.subagents = SimpleNamespace(
        list_runs=lambda: [SimpleNamespace(id="subagent-1", status="PLANNING", verification_status="UNVERIFIED")]
    )  # type: ignore[assignment]

    def fake_run(*args, **kwargs):
        raise ProviderTimeoutError("foreground timeout")

    def fail_watch(*args, **kwargs):
        raise AssertionError("foreground request must not run subagent recovery")

    original_run = agent.run
    original_watch = agent.watch_subagents
    agent.run = fake_run  # type: ignore[method-assign]
    agent.watch_subagents = fail_watch  # type: ignore[method-assign]
    try:
        response = _handle_gateway_request(agent, request_path)
    finally:
        agent.run = original_run  # type: ignore[method-assign]
        agent.watch_subagents = original_watch  # type: ignore[method-assign]

    assert response["ok"] is False
    assert response["status"] == "failed"
    assert response["error_code"] == "PROVIDERTIMEOUTERROR"


# LLM: foreground gateway requests need a reserved worker lane while no-wait jobs run long.
# 函数用途: 覆盖用户聊天通道和后台长任务隔离：worker-0 在多 worker 配置下只拿同步前台请求。
def test_reserved_gateway_worker_prefers_foreground_requests(tmp_path):
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            gateway_workspace="gateway",
            gateway_request_workers=2,
            local_store_path="local_store/local.db",
            local_store_files_dir="local_store/files",
            local_store_events_path="local_store/events.jsonl",
        ),
        tmp_path,
    )
    paths = gateway_paths(agent)
    write_gateway_request(
        paths,
        {
            "id": "gw-000-background",
            "kind": "ask",
            "prompt": "background long work",
            "client_wait": False,
            "status": "pending",
            "created_at": 1.0,
            "attempts": 0,
        },
    )
    write_gateway_request(
        paths,
        {
            "id": "gw-001-foreground",
            "kind": "ask",
            "prompt": "foreground chat",
            "client_wait": True,
            "status": "pending",
            "created_at": 2.0,
            "attempts": 0,
        },
    )

    processed = gateway_runtime._process_gateway_requests(agent, paths, worker_id="gw-worker-0")

    assert processed == 1
    assert gateway_response_path(paths, "gw-001-foreground").exists()
    assert not gateway_response_path(paths, "gw-000-background").exists()
    assert (paths.inbox / "gw-000-background.json").exists()


# LLM: reserved foreground workers are configurable for deployments that want all workers shared.
# 函数用途: 验证配置关闭保留通道后，0 号 worker 仍按兼容模式处理后台请求。
def test_gateway_foreground_reservation_can_be_disabled(tmp_path):
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            gateway_workspace="gateway",
            gateway_request_workers=2,
            gateway_foreground_reserved_workers=0,
            local_store_path="local_store/local.db",
            local_store_files_dir="local_store/files",
            local_store_events_path="local_store/events.jsonl",
        ),
        tmp_path,
    )
    paths = gateway_paths(agent)
    write_gateway_request(
        paths,
        {
            "id": "gw-background",
            "kind": "ask",
            "prompt": "background long work",
            "client_wait": False,
            "status": "pending",
            "created_at": 1.0,
            "attempts": 0,
        },
    )

    processed = gateway_runtime._process_gateway_requests(agent, paths, worker_id="gw-worker-0")

    assert processed == 1
    assert gateway_response_path(paths, "gw-background").exists()


# LLM: gateway ask must reject missing delivery contracts before queueing background work.
# 函数用途: 验证合同文件路径在 CLI 投递前校验，避免后台请求进入 failed 队列才暴露路径错误。
def test_gateway_contract_file_error_rejects_missing_file(tmp_path):
    args = Namespace(delivery_contract_file=str(tmp_path / "missing-contract.json"))

    assert "delivery contract 文件不存在" in gateway_client._gateway_contract_file_error(args)


def test_gateway_contract_file_error_accepts_existing_file(tmp_path):
    contract = tmp_path / "delivery_contract.json"
    contract.write_text('{"checks":[{"path":"out.txt"}]}', encoding="utf-8")
    args = Namespace(delivery_contract_file=str(contract))

    assert gateway_client._gateway_contract_file_error(args) == ""
