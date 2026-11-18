"""后台实时活动行与转录流回合级凭据脱敏回归测试（R2-P1-03）。"""

from __future__ import annotations

import gc
import json
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.conversation import background_delivery as delivery_module
from agent_py_agent.agent.conversation import background_execution as execution_module
from agent_py_agent.agent.conversation.agent_activity import (
    BackgroundMainActivitySink,
    background_main_activity,
)
from agent_py_agent.agent.conversation.background_transcript import (
    BackgroundTranscriptSink,
    read_background_transcript_events,
)
from agent_py_agent.agent.conversation.channels import DeliveryContext, FakeDeliveryService
from agent_py_agent.agent.conversation.display_archive import read_display_archive_page
from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.tooling.host_output_redaction import (
    TurnCredentialRedaction,
    credential_echo_variants,
    turn_credential_redaction,
)


def _assert_no_secret_fragments(serialized: str, secret: str) -> None:
    assert secret not in serialized
    assert secret[:6] not in serialized
    assert secret[6:] not in serialized
    for variant in credential_echo_variants((secret,)):
        if len(variant) >= 8:
            assert variant not in serialized


def test_split_chunk_thinking_delta_redacts_live_event_ring_and_snapshot(tmp_path) -> None:
    secret = "SynBgCredVal-987654"
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "owner-bg-split"})
    child_thread = store.threads.ensure_agent(
        {
            "thread_id": "thread-child-1",
            "canonical_user_id": "owner-bg-split",
            "agent_run_id": "child-1",
            "parent_agent_thread_id": thread.thread_id,
            "root_agent_thread_id": thread.thread_id,
        }
    )
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=str(tmp_path / "owner-home"), owner_id="owner-bg-split"),
        conversation_store=store,
    )

    live_events: list[dict[str, object]] = []

    def capture_writer(
        target_agent: object,
        *,
        thread_id: str,
        task_id: str,
        request_id: str,
        kind: str,
        phase: str,
        block_id: str,
        payload: dict[str, object],
    ) -> None:
        del target_agent
        live_events.append(
            {
                "thread_id": thread_id,
                "task_id": task_id,
                "request_id": request_id,
                "kind": kind,
                "phase": phase,
                "block_id": block_id,
                "payload": payload,
            }
        )

    turn_red = TurnCredentialRedaction(
        owner_key=str(tmp_path / "owner-home"),
        conversation_root=str(store.storage.root),
        thread_id=thread.thread_id,
        turn_key="turn-split-1",
    )
    try:
        turn_red.register_values((secret,))

        child_sink = BackgroundTranscriptSink(
            agent,
            thread_id=child_thread.thread_id,
            task_id="child-1",
            request_id="bg-agent:child-1:att-1",
            event_writer=capture_writer,
        )
        child_sink.bind_credential_redaction(turn_red)
        child_sink.write_thinking_delta("检查凭据状态 " + secret[:6])
        # 在半截前缀驻留期间重复绑定同一 turn_redaction，不得丢失或吐出未决前缀。
        filter_before = child_sink._credential_stream_filter
        child_sink.bind_credential_redaction(turn_red)
        assert child_sink._credential_stream_filter is filter_before
        child_sink.write_thinking_delta(secret[6:] + " 核对完成\n")
        child_sink.finish()

        live_dump = json.dumps(live_events, ensure_ascii=False)
        _assert_no_secret_fragments(live_dump, secret)
        completed_child = [item for item in live_events if item["kind"] == "thinking_completed"]
        assert len(completed_child) == 1
        assert "检查凭据状态" in str(completed_child[0]["payload"]["text"])
        assert "核对完成" in str(completed_child[0]["payload"]["text"])

        # 同时验证默认主后台环形缓冲与 display_history_snapshot。
        main_sink = BackgroundTranscriptSink(agent, thread_id=thread.thread_id, task_id="task-split")
        main_sink.bind_credential_redaction(turn_red)
        main_sink.write_thinking_delta("准备请求 " + secret[:6])
        main_sink.write_thinking_delta(secret[6:] + " 已就绪\n")
        main_sink.finish()

        ring_page = read_background_transcript_events(agent, thread_id=thread.thread_id, after=0)
        snapshot = main_sink.display_history_snapshot()
        history_rows = store.messages.history_page_report(thread.thread_id, limit=50).rows

        _assert_no_secret_fragments(json.dumps(ring_page, ensure_ascii=False), secret)
        _assert_no_secret_fragments(json.dumps(snapshot, ensure_ascii=False), secret)
        _assert_no_secret_fragments(
            json.dumps([row.metadata for row in history_rows], ensure_ascii=False),
            secret,
        )
        thinking_events = [item for item in snapshot["events"] if item["kind"] == "thinking_completed"]
        assert len(thinking_events) == 1
        assert "准备请求" in thinking_events[0]["payload"]["text"]
        assert "已就绪" in thinking_events[0]["payload"]["text"]
    finally:
        turn_red.close()


def test_finish_and_tool_boundary_without_full_write_thinking_flush_tail_safely(tmp_path) -> None:
    secret = "SynTailSecret-445566"
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "owner-tail"})
    agent = SimpleNamespace(conversation_store=store)

    turn_red = TurnCredentialRedaction(
        owner_key="owner-tail",
        conversation_root=str(store.storage.root),
        thread_id=thread.thread_id,
        turn_key="turn-tail-1",
    )
    try:
        turn_red.register_values((secret,))
        sink = BackgroundTranscriptSink(agent, thread_id=thread.thread_id, task_id="task-tail")
        sink.bind_credential_redaction(turn_red)

        # 第一段思考在无尾空格处被工具启动边界截断（无 provider write_thinking 全文事件）。
        sink.write_thinking_delta("第一段前缀 " + secret[:6])
        sink.write_thinking_delta(secret[6:] + " 尾部保留词")
        sink.write_progress(
            {
                "round": 1,
                "call_index": 0,
                "tool": "run_command",
                "phase": "started",
                "detail": f"python3 check.py --arg={secret}",
            }
        )
        sink.write_progress(
            {
                "round": 1,
                "call_index": 0,
                "tool": "run_command",
                "phase": "completed",
                "ok": True,
                "output": f"执行输出 {secret}",
            }
        )

        # 第二段思考仅输入半截凭据前缀后直接 finish（无完整凭据也无全文事件）。
        sink.write_thinking_delta("第二段收尾 " + secret[:6])
        sink.finish()

        ring_events = read_background_transcript_events(agent, thread_id=thread.thread_id, after=0)["events"]
        snapshot = sink.display_history_snapshot()

        ring_dump = json.dumps(ring_events, ensure_ascii=False)
        snap_dump = json.dumps(snapshot, ensure_ascii=False)
        _assert_no_secret_fragments(ring_dump, secret)
        _assert_no_secret_fragments(snap_dump, secret)

        completed_thinking = [
            item["payload"]["text"]
            for item in snapshot["events"]
            if item["kind"] == "thinking_completed"
        ]
        assert len(completed_thinking) == 2
        assert "第一段前缀" in completed_thinking[0]
        assert "尾部保留词" in completed_thinking[0]
        assert "第二段收尾" in completed_thinking[1]

        delta_texts = "".join(
            str(item["payload"].get("text") or "")
            for item in ring_events
            if item["kind"] == "thinking_delta"
        )
        assert "尾部保留词" in delta_texts
    finally:
        turn_red.close()


def test_full_write_thinking_archive_activity_row_commentary_and_repeated_bind(tmp_path) -> None:
    secret = "SynArchiveCred-778899"
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "owner-archive"})
    owner_home = tmp_path / "owner-home"
    owner_home.mkdir(parents=True, exist_ok=True)
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=str(owner_home), owner_id="owner-archive"),
        conversation_store=store,
    )

    activity_sink = BackgroundMainActivitySink(agent, thread_id=thread.thread_id, task_id="task-archive")
    assert activity_sink._turn_lifetime_owner is None
    pulse_updates: list[str] = []
    activity_sink._transcript._im_progress = SimpleNamespace(
        update=lambda text: pulse_updates.append(str(text)),
        close=lambda: None,
    )

    params = RunParams(request_id="req-archive-1", task_id="task-archive", on_chunk=activity_sink)
    turn_red = turn_credential_redaction(agent, params)
    try:
        assert activity_sink._turn_redaction is turn_red
        assert activity_sink._transcript._turn_redaction is turn_red
        first_filter = activity_sink._transcript._credential_stream_filter
        assert first_filter is not None

        turn_red.register_values((secret,))
        # 重复绑定必须幂等，保留同一流过滤器实例。
        activity_sink.bind_credential_redaction(turn_red)
        activity_sink.bind_credential_redaction(turn_red)
        assert activity_sink._transcript._credential_stream_filter is first_filter

        # 构造跨越 240 字符活动行截断边界且总长超过 12,000 字符（触发归档落盘）的完整思考。
        boundary_prefix = "思" * 234
        long_thinking = f"{boundary_prefix}{secret} " + ("后续分析内容 " * 1_800) + f" 末尾核验 {secret}"
        assert len(long_thinking) > 12_500
        activity_sink.write_thinking(long_thinking, duration_seconds=2.0)

        live_row = background_main_activity(agent, thread.thread_id, {"task-archive"})
        _assert_no_secret_fragments(json.dumps(live_row, ensure_ascii=False), secret)
        _assert_no_secret_fragments(json.dumps(pulse_updates, ensure_ascii=False), secret)

        # 验证过程回复（write_model -> 工具启动晋升 + commit_model_response）及结构化 _event 扇出。
        activity_sink.write_model(f"先执行检查，目标标识 {secret}")
        activity_sink.write_progress(
            {
                "round": 1,
                "call_index": 0,
                "tool": "read_file",
                "phase": "started",
                "detail": f"读取配置 {secret}",
            }
        )
        activity_sink.write_progress(
            {
                "round": 1,
                "call_index": 0,
                "tool": "read_file",
                "phase": "completed",
                "ok": True,
                "output": f"配置输出 {secret}",
                "display": {"preview": f"展示摘要 {secret}"},
            }
        )
        activity_sink.commit_model_response(
            {
                "schema": "assistant_messages.v1",
                "source": {"call_id": "call-1"},
                "boundary": "tools",
                "messages": [{"phase": "commentary", "text": f"二次确认说明 {secret}"}],
            }
        )
        activity_sink._transcript._event(
            "system_message",
            "completed",
            f"{activity_sink._transcript.request_id}:custom:1",
            {"text": f"系统提示 {secret}", "nested": {"note": f"嵌套字段 {secret}"}},
        )
        activity_sink.fail(RuntimeError("模拟执行异常 " + ("垫" * 76) + secret))

        failed_row = background_main_activity(agent, thread.thread_id, {"task-archive"})
        _assert_no_secret_fragments(json.dumps(failed_row, ensure_ascii=False), secret)
        _assert_no_secret_fragments(json.dumps(pulse_updates, ensure_ascii=False), secret)

        commentaries = activity_sink.assistant_commentary_messages()
        assert len(commentaries) == 2
        _assert_no_secret_fragments(json.dumps(commentaries, ensure_ascii=False), secret)

        ring_events = read_background_transcript_events(agent, thread_id=thread.thread_id, after=0)["events"]
        snapshot = activity_sink.display_history_snapshot()
        _assert_no_secret_fragments(json.dumps(ring_events, ensure_ascii=False), secret)
        _assert_no_secret_fragments(json.dumps(snapshot, ensure_ascii=False), secret)

        thinking_completed = next(
            item for item in snapshot["events"] if item["kind"] == "thinking_completed"
        )
        archive_ref = thinking_completed["payload"].get("display_archive_ref")
        assert isinstance(archive_ref, dict)
        archive_pages = [
            read_display_archive_page(store, archive_ref, idx)
            for idx in range(int(archive_ref["page_count"]))
        ]
        _assert_no_secret_fragments(json.dumps(archive_pages, ensure_ascii=False), secret)
    finally:
        turn_red.close()


def test_invoke_background_turn_sets_lifetime_owner_and_protects_delivery(tmp_path, monkeypatch) -> None:
    secret = "SynLifetimeCred-314159"
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "owner-lifetime"})
    owner_home = tmp_path / "owner-home"
    owner_home.mkdir(parents=True, exist_ok=True)
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=str(owner_home), owner_id="owner-lifetime"),
        conversation_store=store,
    )
    request = BackgroundRunRequest(
        thread_id=thread.thread_id,
        task_id="task-lifetime",
        reason="scheduled_progress_report",
    )

    observed: dict[str, object] = {}

    def fake_run_with_compact(
        execution: execution_module.BackgroundExecutionDependencies,
        current_thread: object,
        current_request: object,
        *,
        user_prompt: str,
        continuation_injection: list[str],
        proactive_delivery_available: bool | None,
        activity_sink: BackgroundMainActivitySink,
        turn_trigger: object | None = None,
        completed_run_callback: object | None = None,
    ) -> object:
        del current_thread, user_prompt, continuation_injection, proactive_delivery_available, turn_trigger
        del completed_run_callback
        observed["lifetime_owner"] = activity_sink._turn_lifetime_owner
        observed["sink"] = activity_sink
        inner_params = RunParams(
            request_id=current_request.conversation_turn_id,
            task_id=current_request.task_id,
            on_chunk=activity_sink,
        )
        turn_red = turn_credential_redaction(execution.agent, inner_params)
        turn_red.register_values((secret,))
        observed["turn_red"] = turn_red
        activity_sink.write_thinking_delta("后台轮思考 " + secret[:6])
        activity_sink.write_thinking_delta(secret[6:] + " 完毕")
        activity_sink.write_model(f"中间过程汇报 {secret}")
        activity_sink.write_progress(
            {
                "round": 1,
                "call_index": 0,
                "tool": "run_command",
                "phase": "started",
                "detail": f"执行脚本 {secret}",
            }
        )
        activity_sink.write_progress(
            {
                "round": 1,
                "call_index": 0,
                "tool": "run_command",
                "phase": "completed",
                "ok": True,
                "output": f"脚本输出 {secret}",
            }
        )
        return SimpleNamespace(
            response=f"任务已完成，状态摘要 {secret}",
            assistant_commentary_messages=activity_sink.assistant_commentary_messages(),
            archive_tool_calls=[],
            delivery_artifacts=(),
            message_tool_deliveries=(),
            operation_verification={},
            runtime_status="completed",
            runtime_reason="",
            internal_response="",
            public_receipt=None,
            active_turn_user_inputs=(),
        )

    monkeypatch.setattr(execution_module, "run_background_turn_with_compact", fake_run_with_compact)
    deps = execution_module.BackgroundExecutionDependencies(
        agent=agent,
        store=store,
        prepare_run=lambda *_a, **_k: RunParams(),
    )
    try:
        raw_result, snapshot = execution_module.invoke_background_turn(
            deps,
            thread,
            request,
            user_prompt="执行后台巡检",
            continuation_injection=[],
            proactive_delivery_available=True,
        )
        assert observed["lifetime_owner"] is request
        gc.collect()

        collected = execution_module.collect_background_execution_result(agent, raw_result, snapshot)
        channels = FakeDeliveryService()
        delivery_deps = delivery_module.BackgroundDeliveryDependencies(
            agent=agent,
            store=store,
            channels=channels,
            task_status=lambda _req: "completed",
        )
        commit = delivery_module.record_background_response(
            delivery_deps,
            request,
            DeliveryContext(channel="feishu", target="ou_test_user", thread_id=thread.thread_id),
            collected.response,
            assistant_commentaries=collected.assistant_commentaries,
            display_snapshot=collected.display_snapshot,
            deliver=True,
            delivery_reason=request.reason,
        )
        assert commit.persisted is True

        ring_events = read_background_transcript_events(agent, thread_id=thread.thread_id, after=0)["events"]
        persisted_rows = store.messages.recent(thread.thread_id, limit=20)
        sent = channels.adapter("feishu").sent_messages
        assert len(sent) == 1
        _assert_no_secret_fragments(json.dumps(snapshot, ensure_ascii=False), secret)
        _assert_no_secret_fragments(json.dumps(ring_events, ensure_ascii=False), secret)
        _assert_no_secret_fragments(json.dumps([m.content for m in sent], ensure_ascii=False), secret)
        _assert_no_secret_fragments(
            json.dumps([(row.content, row.metadata) for row in persisted_rows], ensure_ascii=False),
            secret,
        )
    finally:
        turn_red = observed.get("turn_red")
        if turn_red is not None:
            turn_red.close()


def test_withheld_turn_redaction_keeps_structured_event_and_snapshot_dicts(tmp_path) -> None:
    secret = "SynWithheldCred-909090"
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "owner-withheld"})
    agent = SimpleNamespace(conversation_store=store)

    turn_red = TurnCredentialRedaction(
        owner_key="owner-withheld",
        conversation_root=str(store.storage.root),
        thread_id=thread.thread_id,
        turn_key="turn-withheld-1",
    )
    try:
        # 触发单回合登记数量上限进入 withheld 状态。
        turn_red.register_values([f"overflow-secret-{idx:03d}" for idx in range(257)] + [secret])
        assert turn_red.withheld is True

        activity_sink = BackgroundMainActivitySink(agent, thread_id=thread.thread_id, task_id="task-withheld")
        activity_sink.bind_credential_redaction(turn_red)
        activity_sink.write_thinking_delta(f"增量 {secret}")
        activity_sink.write_thinking(f"完整思考 {secret}")
        activity_sink.write_model(f"过程回复 {secret}")
        activity_sink.write_progress(
            {"round": 1, "call_index": 0, "tool": "read_file", "phase": "started", "detail": secret}
        )
        activity_sink.finish()

        live_row = background_main_activity(agent, thread.thread_id, {"task-withheld"})
        snapshot = activity_sink.display_history_snapshot()
        assert isinstance(snapshot, dict)
        assert all(isinstance(ev.get("payload"), dict) for ev in snapshot.get("events", ()))
        _assert_no_secret_fragments(json.dumps(live_row, ensure_ascii=False), secret)
        _assert_no_secret_fragments(json.dumps(snapshot, ensure_ascii=False), secret)
    finally:
        turn_red.close()
