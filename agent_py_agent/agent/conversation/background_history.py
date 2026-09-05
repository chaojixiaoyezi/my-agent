# LLM: 本模块只收集一个后台工作片的公开展示块；最终快照由 canonical 消息提交，不能成为模型历史或运行状态。
# 模块用途: 保存临时事件环之外的完整终态展示，让恢复后的工具、思考和回复仍按原顺序排列。

from __future__ import annotations

import copy
import threading
from collections.abc import Mapping

BACKGROUND_DISPLAY_TURN_SCHEMA = "background_display_turn.v1"
_TERMINAL_KINDS = frozenset({
    "assistant_completed", "thinking_completed", "tool_completed", "tool_failed", "system_message",
})


# LLM: 按稳定 block_id 保留首次出现位置，后续终态替换同块；不从有界 transport ring 回读或按正文去重。
# 类用途: 累积一轮公开过程的完整块；逐 token 增量不重复存储，工具缺终态时保留明确的未知说明。
class BackgroundTurnHistory:
    # LLM: 身份由宿主 sink 提供，锁只保护展示快照；不读写文件、不改变 owner 或 task。
    # 函数用途: 创建本工作片的块容器，避免并发工具回调与最终快照互相覆盖。
    def __init__(self, thread_id: str, task_id: str, request_id: str) -> None:
        self.thread_id, self.task_id, self.request_id = thread_id, task_id, request_id
        self._blocks: dict[str, dict] = {}
        self._complete = False
        self._lock = threading.Lock()

    # LLM: 只保存展示型终态；开始事件占据原位置，消费回执和其他控制事件不进入静态历史。
    # 函数用途: 用完整终态替换同一工具或思考的占位，不让长思考的增量挤掉较早工具。
    def record(self, kind: str, phase: str, block_id: str, payload: Mapping) -> None:
        if kind in {"context_window_compacted", "conversation_compaction_completed", "conversation_compaction_failed"}:
            generation = payload.get("generation")
            label = "会话压缩失败" if phase == "failed" else "会话压缩完成"
            payload = {"text": f"{label} · generation {generation}"}
            kind = "system_message"
        terminal = kind in _TERMINAL_KINDS and phase in {"completed", "failed"}
        started = kind in {"thinking_started", "tool_started"}
        if not terminal and not started:
            return
        with self._lock:
            if self._complete:
                return
            if not terminal and block_id in self._blocks:
                return
            self._blocks[block_id] = {
                "request_id": self.request_id, "block_id": block_id,
                "kind": kind if terminal else "system_message",
                "phase": phase if terminal else "completed",
                "payload": copy.deepcopy(dict(payload)) if terminal else {
                    "text": f"{payload.get('tool') or '过程块'}：本工作片没有保存完整终态。",
                },
            }

    # LLM: finish 只冻结显示快照，必须在 sink 关闭最后一个 thinking 后调用；没有权限/完成判定副作用。
    # 函数用途: 标记这份工作片展示已收齐；未调用 finish 的活动快照不能授权客户端丢弃增量。
    def finish(self) -> None:
        with self._lock:
            self._complete = True

    # LLM: 返回独立副本，只有 canonical final 提交成功后客户端才可依此重基缓冲。
    # 函数用途: 导出完整块快照供最终消息 metadata 保存，不让调用方改写 sink 内部历史。
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "schema_version": BACKGROUND_DISPLAY_TURN_SCHEMA,
                "thread_id": self.thread_id, "task_id": self.task_id,
                "request_id": self.request_id, "complete": self._complete,
                "events": copy.deepcopy(list(self._blocks.values())),
            }


# LLM: 完整快照必须与 canonical 行的 exact thread/task/request 身份相符；不完整或畸形快照不能抑制实时事件。
# 函数用途: 校验消息中的展示快照，缺少新字段的旧历史继续按已有消息/native 内容恢复。
def background_display_turn_from_row(row: object) -> dict | None:
    metadata = getattr(row, "metadata", None)
    if not isinstance(metadata, dict) or metadata.get("assistant_part_id") != "final":
        return None
    value = metadata.get("background_display_turn")
    if not isinstance(value, dict):
        return None
    request_id = str(value.get("request_id") or "")
    if (
        value.get("schema_version") != BACKGROUND_DISPLAY_TURN_SCHEMA
        or value.get("complete") is not True
        or value.get("thread_id") != getattr(row, "thread_id", "")
        or value.get("task_id") != metadata.get("task_id")
        or not request_id.startswith(f"bg-main:{value['thread_id']}:")
        or request_id != metadata.get("background_transcript_request_id")
        or not isinstance(value.get("events"), list)
    ):
        return None
    seen = set()
    for event in value["events"]:
        if not isinstance(event, dict):
            return None
        block_id = event.get("block_id")
        if (
            event.get("request_id") != request_id
            or not isinstance(block_id, str) or not block_id.startswith(f"{request_id}:")
            or block_id in seen
            or event.get("kind") not in _TERMINAL_KINDS
            or event.get("phase") not in {"completed", "failed"}
            or not isinstance(event.get("payload"), dict)
        ):
            return None
        seen.add(block_id)
    return copy.deepcopy(value)
