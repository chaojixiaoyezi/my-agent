# LLM: 本模块消费已清洗的前台 chunk，复用会话公开过程映射；精确请求来自宿主，不拥有审批、模型或任务状态。
# 模块用途: 把前台思考、工具和候选回复同步给同会话窗口，最终快照随 canonical final 保存。

from __future__ import annotations

import threading

from ..conversation.background_transcript import (
    BackgroundTranscriptSink,
    public_background_transcript_text,
)


# LLM: 一个 sink 对应一个已认领的前台请求；锁保护显示顺序，默认后台/child 映射不改变候选正文策略。
# 类用途: 适配 Gateway 的公开事件，并用稳定块编号让最终消息接替临时流式回复。
class GatewayForegroundTranscriptSink(BackgroundTranscriptSink):
    # LLM: request 必须是宿主更新 task binding 的原对象；临时 display request 不得代替真实 Gateway ID。
    # 函数用途: 初始化独立显示片与完整块容器，不创建模型调用或第二份持久正文。
    def __init__(self, agent: object, *, thread_id: str, request_id: str, request: dict, task_id: str = "") -> None:
        super().__init__(agent, thread_id=thread_id, task_id=task_id, gateway_request_id=request_id)
        self._host_request = request
        self._lock = threading.RLock()
        self._candidate_id = ""
        self._closed = False
        self._prepared = False

    # LLM: 只校验 exact 宿主关联；任务晋升不能把旧 task 冻结在显示流里，也不允许冲突事件跨会话投递。
    # 函数用途: 跟随本请求真实 task 绑定；畸形或不匹配时停止本次显示更新，不改变运行状态。
    def _bind_task(self) -> bool:
        binding = self._host_request.get("conversation_runtime")
        if binding is None:
            return True
        if not isinstance(binding, dict) or binding.get("thread_id") != self.thread_id or binding.get("request_id") != self.gateway_request_id:
            return False
        self.task_id = str(binding.get("task_id") or "")
        return True

    # LLM: raw kind/typed payload 是映射依据；忽略普通 runtime prose 和 permission，后者仍只有原审批入口。
    # 函数用途: 将前台流接到现有思考、工具、Compact、重连和插话消费展示，不复制 CLI adapter。
    def __call__(self, event: dict[str, object]) -> None:
        with self._lock:
            if self._closed or self._prepared or not self._bind_task():
                return
            self._consume_event(event)

    # LLM: 调用方持显示锁；长思考只沿用 writer 已保存的引用与缺失事实，不把裁后预览再次归档成全文。
    # 函数用途: 分派公开事件并透传完整原文引用；未知种类保持忽略，不处理控制或动态工具参数。
    def _consume_event(self, event: dict[str, object]) -> None:
        kind = event.get("kind")
        text_writer = {"thinking_delta": self.write_thinking_delta, "model_delta": self.write_model}.get(kind)
        if text_writer is not None:
            text_writer(str(event.get("text") or ""))
            return
        structured = {
            "tool_progress": ("progress", self.write_progress),
            "tool_input_progress": ("progress", self.write_tool_input_progress),
            "context_window_compacted": ("context_compaction", self.write_context_compaction),
            "conversation_compaction_progress": ("compact_progress", self.write_conversation_compact_progress),
        }.get(kind)
        if structured is not None:
            value = event.get(structured[0])
            if isinstance(value, dict):
                structured[1](value)
            return
        if kind == "assistant_thinking":
            reference = event.get("display_archive_ref")
            self.write_thinking(
                str(event.get("text") or ""), duration_seconds=float(event.get("duration_seconds") or 0),
                display_archive_ref=reference if isinstance(reference, dict) else None,
                history_incomplete=event.get("history_incomplete") is True,
            )
            return
        if kind == "assistant_commentary":
            self._model_text = str(event.get("text") or "")
            self._flush_model_commentary()
            return
        if kind == "tool_input_reset":
            self._clear_tool_input_progress()
            return
        if kind == "runtime_progress" and isinstance(event.get("retry"), dict):
            retry = event["retry"]
            self.write_provider_retry(attempt=int(retry.get("attempt") or 1), total=int(retry.get("total") or 1), delay_seconds=float(retry.get("wait_seconds") or 0))
            return
        if kind == "conversation_compacted":
            generation = max(0, int(event.get("compact_generation") or 0))
            if generation:
                self._event("compact_boundary", "completed", f"{self.request_id}:compact:{generation}", {"compact_generation": generation})
                self.begin_model_attempt(generation)
            return
        if kind == "active_turn_input_submitted":
            # 已提交与已消费走同一条转发路径,但语义严格更弱:它只表示这批输入已进入本次提供方
            # 调用的 prompt,重连客户端据此立刻重建用户行,不得据此清等待项或结算回复欠账。
            ids = event.get("client_message_ids")
            rows = event.get("messages")
            self.submit_active_turn_input(
                tuple(ids) if isinstance(ids, list) else (),
                provider_call_id=str(event.get("provider_call_id") or ""),
                client_messages=tuple((row.get("message_id", ""), row.get("text", "")) for row in rows if isinstance(row, dict)) if isinstance(rows, list) else (),
            )
            return
        if kind == "active_turn_input_consumed":
            ids = event.get("client_message_ids")
            rows = event.get("messages")
            self.complete_active_turn_input(
                tuple(ids) if isinstance(ids, list) else (),
                client_messages=tuple((row.get("message_id", ""), row.get("text", "")) for row in rows if isinstance(row, dict)) if isinstance(rows, list) else (),
            )

    # LLM: 增量已由 Gateway 清洗；不做 strip 破坏分片边界，不把候选块写进 canonical transcript。
    # 函数用途: 实时显示正在生成的候选回复；之后的真实工具边界或 canonical final 决定如何收口。
    def write_model(self, text: str) -> None:
        if not text:
            return
        if not self._candidate_id:
            self._assistant_index += 1
            self._candidate_id = f"{self.request_id}:assistant:{self._assistant_index}"
            self._event("assistant_started", "started", self._candidate_id, {})
        self._model_text += text
        self._event("assistant_delta", "delta", self._candidate_id, {"text": text})

    # LLM: 全量 commentary 接替同一候选 ID；丢增量时也能终态恢复，不以文字相等判重。
    # 函数用途: 在工具边界把候选文字固定为过程说明，下一段回复使用新编号。
    def _flush_model_commentary(self) -> None:
        content = public_background_transcript_text(self._model_text, limit=0)
        if content:
            if not self._candidate_id:
                self._assistant_index += 1
                self._candidate_id = f"{self.request_id}:assistant:{self._assistant_index}"
            self._event("assistant_completed", "completed", self._candidate_id, {"text": content, "process": True})
            self._committed_commentary.append(content)
        elif self._candidate_id:
            self._event("assistant_discarded", "interrupted", self._candidate_id, {})
        self._candidate_id = ""
        self._model_text = ""

    # LLM: typed 插话边界只丢弃未确认候选；正式用户消息仍由消费回执和 canonical store 负责。
    # 函数用途: 防止用户插话之前的半句混进之后的回复，不删除任何稳定历史。
    def begin_active_turn_input(self, client_message_ids: tuple[str, ...]) -> None:
        with self._lock:
            if self._closed or self._prepared:
                return
            if self._candidate_id:
                self._event("assistant_discarded", "interrupted", self._candidate_id, {})
            self._candidate_id = ""
            super().begin_active_turn_input(client_message_ids)

    # LLM: 仅在存在可信最终结果时冻结完整显示片；候选不提前删除，canonical final 按精确 ID 原子接替。
    # 函数用途: 向正常持久化和延迟补交返回同一快照，不增加模型调用或改写原生上下文。
    def prepare_final(self) -> dict:
        with self._lock:
            if not self._bind_task() or self._closed:
                return {}
            super().finish()
            self._prepared = True
            snapshot = self.display_history_snapshot()
            snapshot.update(task_id=self.task_id, gateway_request_id=self.gateway_request_id)
            if self._candidate_id:
                snapshot["live_final_block_id"] = self._candidate_id
            return snapshot

    # LLM: 流关闭不是任务终态；没有可信 final 时只清当前显示片的活动块，未知工具不伪装执行成功。
    # 函数用途: 取消/异常后收起孤立动画；成功片等待 canonical final 接替候选，close 可重复调用。
    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if not self._prepared:
                super().finish()
                self._event("transcript_stream_closed", "interrupted", f"{self.request_id}:stream", {})
            self._closed = True
