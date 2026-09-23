# LLM: 公开过程检查点属于 canonical 会话显示历史；不得成为模型消息、控制事件或运行事实，根与 child 共用校验。
# 模块用途: 校验并恢复逐块保存的工具和思考；没有终态的开始块显示未知，不冒充任务已完成。

from __future__ import annotations

import copy
import hashlib
import json
import logging
import threading
from collections.abc import Mapping
from typing import TypedDict

DISPLAY_CHECKPOINT_ROLE = "display"
DISPLAY_CHECKPOINT_SCHEMA = "conversation_display_event.v1"
_TERMINAL_KINDS = frozenset({
    "assistant_completed", "thinking_completed", "tool_completed", "tool_failed", "system_message", "user_message",
})
_START_KINDS = frozenset({"tool_started"})


# LLM: 检查点输入列明宿主身份和公开事件字段；不是开放参数入口，也不能包含控制/授权决定。
# 类用途: 约束主子过程保存的入参结构；真实身份与公开事件仍由下方校验器检查。
class DisplayCheckpointInput(TypedDict):
    thread_id: str
    task_id: str
    request_id: str
    gateway_request_id: str
    kind: str
    phase: str
    block_id: str
    payload: Mapping[str, object]


# LLM: role 是显示记录的硬边界，即使 metadata 损坏也不能投给模型或 Memory；不解析正文。
# 函数用途: 标识只给界面看的历史记录，让模型侧读取统一排除。
def is_display_checkpoint(row: object) -> bool:
    return (row.get("role") if isinstance(row, Mapping) else getattr(row, "role", None)) == DISPLAY_CHECKPOINT_ROLE


# LLM: 只接受宿主精确 thread/request/block 关联及公开事件白名单；返回副本，无文件或运行状态副作用。
# 函数用途: 把完整过程事件转换成可持久化 metadata；逐 token、审批及其他控制事件不保存到这里。
def display_checkpoint_metadata(*, thread_id: str, task_id: str, request_id: str, gateway_request_id: str = "",
                                kind: str, phase: str, block_id: str, payload: Mapping) -> dict | None:
    if kind == "assistant_completed" and payload.get("process") is not True:
        return None
    if kind in {"context_window_compacted", "conversation_compaction_completed", "conversation_compaction_failed"}:
        label = "会话压缩失败" if phase == "failed" else "会话压缩完成"
        payload = {"text": f"{label} · generation {payload.get('generation')}"}
        kind = "system_message"
    valid_kind = kind in _START_KINDS and phase == "started" or kind in _TERMINAL_KINDS and phase in {"completed", "failed"}
    main = request_id.startswith(f"bg-main:{thread_id}:")
    child = bool(task_id) and request_id.startswith(f"bg-agent:{task_id}:") and thread_id == f"thread-{task_id}"
    if not thread_id or not (main or child) or not block_id.startswith(f"{request_id}:") or not valid_kind:
        return None
    if gateway_request_id and not main:
        return None
    return {
        "display_checkpoint": {
            "schema": DISPLAY_CHECKPOINT_SCHEMA, "thread_id": thread_id, "task_id": task_id,
            "request_id": request_id, "gateway_request_id": gateway_request_id,
            "kind": kind, "phase": phase, "block_id": block_id, "payload": copy.deepcopy(dict(payload)),
        },
        "background_transcript_request_id": request_id,
        **({"gateway_request_id": gateway_request_id} if gateway_request_id else {}),
        **({"agent_attempt_id": request_id.removeprefix(f"bg-agent:{task_id}:"), "agent_run_id": task_id} if child else {}),
    }


# LLM: 内外层thread/request/child身份必须一致；live忽略开始占位，不能借损坏关联隐藏别片或恢复控制事件。
# 函数用途: 从只读历史记录恢复一个稳定显示块；残留工具开始块说明未知，不重新执行工具或开动画。
def display_checkpoint_event(row: object, *, live: bool = False) -> dict | None:
    if not is_display_checkpoint(row):
        return None
    metadata = getattr(row, "metadata", None)
    value = metadata.get("display_checkpoint") if isinstance(metadata, dict) else None
    if not isinstance(value, dict) or value.get("schema") != DISPLAY_CHECKPOINT_SCHEMA:
        return None
    fields = ("thread_id", "task_id", "request_id", "gateway_request_id", "kind", "phase", "block_id")
    if any(not isinstance(value.get(key), str) for key in fields) or not isinstance(value.get("payload"), Mapping):
        return None
    if value["thread_id"] != getattr(row, "thread_id", None):
        return None
    if (metadata.get("background_transcript_request_id") != value["request_id"]
            or metadata.get("gateway_request_id", "") != value["gateway_request_id"]):
        return None
    if value["request_id"].startswith("bg-agent:") and (
        metadata.get("agent_run_id") != value["task_id"]
        or value["request_id"] != f"bg-agent:{value['task_id']}:{metadata.get('agent_attempt_id')}"
    ):
        return None
    checked = display_checkpoint_metadata(**{key: value[key] for key in (*fields, "payload")})
    if checked is None:
        return None
    kind, phase, payload = value["kind"], value["phase"], copy.deepcopy(value["payload"])
    if kind in _START_KINDS:
        if live:
            return None
        kind, phase = "system_message", "completed"
        payload = {"text": f"{payload.get('tool') or '过程块'}：本工作片没有保存完整终态。", "history_incomplete": True}
    return {
        "schema": "conversation_history_display.v1", "request_id": value["request_id"],
        "block_id": value["block_id"], "kind": kind, "phase": phase, "payload": payload,
        **({"gateway_request_id": value["gateway_request_id"]} if value["gateway_request_id"] else {}),
    }


# LLM: 同 block 的最新完整记录替换旧占位，迟到 started 不回退；完整 final 快照只覆盖自己的 request。
# 函数用途: 组合一组恢复检查点，避免开始/完成各显示一行或新工作片遮住崩溃前的旧过程。
def display_checkpoint_events(rows, *, covered_requests: set[str] | None = None) -> list[dict]:
    blocks: dict[str, dict] = {}
    for row in rows:
        event = display_checkpoint_event(row)
        if event is not None and event["request_id"] not in (covered_requests or set()):
            if row.metadata["display_checkpoint"]["kind"] in _START_KINDS and event["block_id"] in blocks:
                continue
            blocks[event["block_id"]] = event
    return list(blocks.values())


# LLM: 一个公开 sink 使用一个 writer；按事件身份/内容去重只影响展示落盘，不拥有运行或副作用幂等权。
# 类用途: 在投递前保存完整过程块，失败只记录明确警告，不中断真实模型和工具。
class DisplayCheckpointWriter:
    # LLM: 仅绑定已有 owner ConversationStore；不新建线程、目录或网络请求，锁只保护当前 sink 的写入去重。
    # 函数用途: 为一片过程准备持久化入口；无会话的独立显示夹具不产生磁盘副作用。
    def __init__(self, agent: object) -> None:
        self._append = getattr(getattr(getattr(agent, "conversation_store", None), 'messages', None), 'append_display_checkpoint', None)
        self._seen: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()
        self.failed = False

    # LLM: 输入是明确字段的公开检查点；只有保存成功才记去重，失败允许下一完整块重试并返回一次告警信号。
    # 函数用途: 保存一个过程检查点并报告新增保存故障；不会把异常路径或工具私密参数交给界面。
    def record(self, fields: DisplayCheckpointInput) -> bool:
        if not callable(self._append):
            return False
        metadata = display_checkpoint_metadata(**fields)
        if metadata is None:
            return False
        event = metadata["display_checkpoint"]
        with self._lock:
            try:
                digest = hashlib.sha256(json.dumps(event, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                key = (event["block_id"], event["phase"])
                if self._seen.get(key) == digest:
                    return False
                self._append(fields["thread_id"], metadata)
                self._seen[key] = digest
            except (OSError, RuntimeError, ValueError, TypeError) as exc:
                logging.getLogger(__name__).warning("display checkpoint persistence failed: %s", type(exc).__name__)
                first = not self.failed
                self.failed = True
                return first
        return False
