"""原输入失效上限不得确认旧模型正文；只用真实循环/会话库与fake后端。"""
from types import SimpleNamespace

from agent_py_agent.agent.agent_core import _tool_loop_service as loop
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.backends.responses_wire import response_fields
from agent_py_agent.agent.conversation.store_guidance import GuidanceStore
from agent_py_agent.cli.run_output import make_run_chunk_writer
from agent_py_agent.tests.test_cli_run_conversation import _agent, _StaticBackend


def test_pending_invalidation_limit_keeps_counts_input_and_suppresses_old_public_text(tmp_path, monkeypatch, capsys):
    agent = _agent(tmp_path)
    states = []
    pending_id = []
    old_text = "失效候选不应成为公开终答"

    class InvalidatedBackend(_StaticBackend):
        def generate(self, prompt, **kwargs):
            self.calls += 1
            thread = agent.conversation_store.threads.list()[0]
            if not pending_id:
                entry = agent.conversation_store.guidance.append_once({"target_type": "thread", "target_id": thread.thread_id,
                    "message": "新待处理输入仍需保留", "sender": "user", "priority": "normal", "delivery": "next_turn"},
                    dedupe_key="pending-boundary-input")
                pending_id.append(entry.guidance_id)
            fields = response_fields({"status": "completed", "output": [{"type": "message", "id": "candidate",
                "phase": "final_answer", "role": "assistant", "content": [{"type": "output_text", "text": old_text}]}]}, "fake")
            return ModelResponse(backend=self.name, **fields)

    def pending(_agent, params):
        states.append(params.live_archive_state)
        thread_id = params.task_attributes["conversation_thread_id"]
        return bool(_agent.conversation_store.guidance.pending("thread", thread_id))

    # 精确故障注入：可观察到输入但原claim拒绝，原drain不消费；循环的上限/计数/模型生成仍真实执行。
    monkeypatch.setattr(loop, "has_pending_turn_input", pending)
    monkeypatch.setattr(loop, "_pending_drain_outcome", lambda _agent, _params, rounds: (rounds, None, False, ""))
    monkeypatch.setattr(GuidanceStore, "claim_for_turn", lambda *args, **kwargs: False)
    agent.backend = InvalidatedBackend()
    writer = make_run_chunk_writer(SimpleNamespace(stop=lambda: None), {})
    result = agent.run("合成输入失效边界", source="cli_run", on_chunk=writer, save=False)
    output = capsys.readouterr().out
    expected = loop.PENDING_TURN_INPUT_INVALIDATION_COUNT + 1
    assert agent.backend.calls == expected
    assert states[-1]["_pending_turn_input_invalidations"] == expected
    assert states[-1]["turn_end_reason"] == loop.PENDING_TURN_INPUT_LIMIT_REASON
    assert result.tool_rounds == 0 and result.executed_tools == []
    thread = agent.conversation_store.threads.list()[0]
    assert [row.guidance_id for row in agent.conversation_store.guidance.pending("thread", thread.thread_id)] == pending_id
    assert result.response == ""
    assert result.runtime_status == "failed" and result.runtime_reason == loop.PENDING_TURN_INPUT_LIMIT_REASON
    assert result.turn_end_reason == "error"
    assert old_text not in output
    assert result.canonical_native_messages  # 私有native事实仍保存，不从原块复活公开正文。
    rows = agent.conversation_store.messages.recent(thread.thread_id, limit=0)
    assert not any(row.role == "assistant" and old_text in row.content for row in rows)


def test_consumed_pending_turn_inputs_reset_invalidation_counter_across_long_session(tmp_path):
    """长会话内多次合法插话/后台进程完成事件在各自下一轮被成功 claim 注入时，不累计触发作废上限。"""
    agent = _agent(tmp_path)
    total_events = loop.PENDING_TURN_INPUT_INVALIDATION_COUNT + 4
    final_text = "长会话全部事件与插话消费完毕后的正式终答"
    task_id = "task-long-session-1"

    class ConsumingEventAndSteerBackend(_StaticBackend):
        def generate(self, prompt, **kwargs):
            self.calls += 1
            thread = agent.conversation_store.threads.list()[0]
            if self.calls <= total_events:
                if self.calls % 2 == 1:
                    agent.conversation_store.wakes.raise_signal(
                        {
                            "thread_id": thread.thread_id,
                            "root_task_id": task_id,
                            "reason": "managed_process_exited",
                            "summary": f"后台命令 #{self.calls} 已退出 (exit_code=0)",
                            "metadata": {"session_id": f"proc-{self.calls}", "exit_code": 0},
                        }
                    )
                else:
                    agent.conversation_store.guidance.append(
                        {
                            "target_type": "thread",
                            "target_id": thread.thread_id,
                            "message": f"合法运行中插话 #{self.calls}",
                            "sender": "user",
                            "priority": "normal",
                            "delivery": "steer",
                        }
                    )
                text = f"过期的中间候选 #{self.calls}"
            else:
                text = final_text
            fields = response_fields(
                {
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "id": f"msg-{self.calls}",
                            "phase": "final_answer",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": text}],
                        }
                    ],
                },
                "fake",
            )
            return ModelResponse(backend=self.name, **fields)

    agent.backend = ConsumingEventAndSteerBackend()
    result = agent.run(
        "测试长会话多次合法事件注入",
        source="cli_run",
        save=False,
        task_id=task_id,
    )
    assert agent.backend.calls == total_events + 1
    assert result.runtime_status == "ok"
    assert result.runtime_reason in ("", None)
    assert result.response == final_text


