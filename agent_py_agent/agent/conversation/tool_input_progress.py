"""Provider 工具参数生成进度的公开展示合同。"""

# LLM: 本模块是 provider 流事件到 TUI/Web 的唯一脱敏字段边界；它只能保留
# 工具名、流内序号、阶段和累计字符数，绝不能携带 partial JSON 或工具参数。
# 模块用途: 校验并裁剪“模型仍在准备工具参数”的临时展示数据，供各类界面复用。

from __future__ import annotations

import time
from collections.abc import Callable, Mapping

TOOL_INPUT_PROGRESS_SCHEMA = "provider_tool_input_progress.v1"
TOOL_INPUT_PROGRESS_PHASES = frozenset({"started", "streaming", "ready"})
TOOL_INPUT_PROGRESS_TOOL_NAME_LIMIT = 80


# LLM: 该函数是公开进度 payload 的 fail-closed 白名单；新增字段前必须确认不会
# 泄露命令、路径、文件正文、凭据或半截 JSON，并同步 Gateway/TUI 测试。
# 函数用途: 把任意 provider 回调值清洗成安全、稳定的工具参数进度，非法值返回空字典。
def public_tool_input_progress(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    if value.get("schema") != TOOL_INPUT_PROGRESS_SCHEMA:
        return {}
    phase = str(value.get("phase") or "").strip().lower()
    if phase not in TOOL_INPUT_PROGRESS_PHASES:
        return {}
    tool = str(value.get("tool") or "Tool").strip()[:TOOL_INPUT_PROGRESS_TOOL_NAME_LIMIT]
    try:
        stream_index = max(0, int(value.get("stream_index") or 0))
        received_chars = max(0, int(value.get("received_chars") or 0))
    except (TypeError, ValueError):
        return {}
    return {
        "schema": TOOL_INPUT_PROGRESS_SCHEMA,
        "phase": phase,
        "stream_index": stream_index,
        "tool": tool or "Tool",
        "received_chars": received_chars,
    }


# LLM: 该投影器只维护临时 block identity，并把清洗后的 started/progress/completed
# 事件交给宿主；它不创建 ToolCall、不解析参数，也不持有任务或回合终态。
# 类用途: 让本地 TUI、Gateway 后台 main 和子代理共用同一套工具参数临时行生命周期。
class ToolInputProgressEventProjector:
    # LLM: event_writer 是宿主现有 typed event 出口；prefix 只用于显示关联，
    # 不能反向成为 run/task/tool 的权威身份。
    # 函数用途: 初始化一个回合内的临时工具参数进度投影。
    def __init__(
        self,
        *,
        block_prefix: str,
        event_writer: Callable[[str, str, str, Mapping[str, object]], None],
    ) -> None:
        self.block_prefix = str(block_prefix or "tool-input").rstrip(":")
        self.event_writer = event_writer
        self._next_block = 0
        self._blocks: dict[int, str] = {}

    # LLM: 只有清洗合同通过的 progress 才能改变临时显示；ready 仅删除 UI
    # block，不代表工具合法、执行成功或模型回合完成。
    # 函数用途: 发布或更新一条工具参数生成进度，并在参数闭合时原位收起。
    def publish(self, value: object) -> bool:
        public = public_tool_input_progress(value)
        if not public:
            return False
        stream_index = int(public["stream_index"])
        phase = str(public["phase"])
        block_id = self._blocks.get(stream_index, "")
        if phase == "started" and block_id:
            self._emit("tool_input_completed", "completed", block_id, public)
            self._blocks.pop(stream_index, None)
            block_id = ""
        if not block_id and phase != "ready":
            self._next_block += 1
            block_id = f"{self.block_prefix}:tool-input:{self._next_block}"
            self._blocks[stream_index] = block_id
            self._emit(
                "tool_input_started",
                "started",
                block_id,
                {**public, "started_at": time.time()},
            )
        if not block_id:
            return False
        if phase == "streaming":
            self._emit("tool_input_progress", "updated", block_id, public)
        elif phase == "ready":
            self._emit("tool_input_completed", "completed", block_id, public)
            self._blocks.pop(stream_index, None)
        return True

    # LLM: 清理只能关闭当前投影器自己创建的 block；调用原因不会进入 payload，
    # 也不能据此改变 provider 重试或真实工具生命周期。
    # 函数用途: 在真实工具开始、重试或回合终态时收起所有遗留临时行。
    def clear(self) -> int:
        block_ids = tuple(self._blocks.values())
        self._blocks.clear()
        for block_id in block_ids:
            self._emit(
                "tool_input_completed",
                "completed",
                block_id,
                {},
            )
        return len(block_ids)

    # LLM: UI 投影失败必须与 provider/tool 主链隔离；这里只吞掉常见展示层
    # 写入错误，不返回任何业务结论。
    # 函数用途: 安全调用宿主 typed event writer。
    def _emit(
        self,
        kind: str,
        phase: str,
        block_id: str,
        payload: Mapping[str, object],
    ) -> None:
        try:
            self.event_writer(kind, phase, block_id, payload)
        except (OSError, RuntimeError, TypeError, ValueError):
            return


# LLM: 该 mixin 只为拥有 request_id 与 typed _event 方法的 transcript sink
# 提供惰性临时投影；不得增加另一份 task/thread 或 provider 状态。
# 类用途: 让后台主代理和子代理接收同一个工具参数进度 callback，同时保持主 sink 类轻量。
class ToolInputProgressSinkMixin:
    # LLM: projector 按 sink 实例惰性创建，使用既有 request_id/_event；公开
    # callback 仍由共享白名单裁剪，不能访问工具参数正文。
    # 函数用途: 接收 provider 工具参数计数并发布临时事件。
    def write_tool_input_progress(self, value: object) -> bool:
        return self._tool_input_projector().publish(value)

    # LLM: 清理只作用于本 mixin 创建的易失 block；尚未收到任何进度时必须
    # 保持零事件，避免正常重试与工具调用产生无意义行。
    # 函数用途: 在重试或回合终态时收起遗留工具参数进度。
    def _clear_tool_input_progress(self) -> int:
        projector = getattr(self, "_provider_tool_input_projector", None)
        return projector.clear() if isinstance(projector, ToolInputProgressEventProjector) else 0

    # LLM: request_id 与 _event 由具体 transcript sink 提供；若合同不完整，
    # AttributeError 只会由调用方现有 display 隔离边界处理，不能静默造新身份。
    # 函数用途: 取得当前 sink 唯一的工具参数临时投影器。
    def _tool_input_projector(self) -> ToolInputProgressEventProjector:
        projector = getattr(self, "_provider_tool_input_projector", None)
        if isinstance(projector, ToolInputProgressEventProjector):
            return projector
        projector = ToolInputProgressEventProjector(
            block_prefix=str(self.request_id),
            event_writer=self._event,
        )
        self._provider_tool_input_projector = projector
        return projector


__all__ = [
    "TOOL_INPUT_PROGRESS_PHASES",
    "TOOL_INPUT_PROGRESS_SCHEMA",
    "ToolInputProgressEventProjector",
    "ToolInputProgressSinkMixin",
    "public_tool_input_progress",
]
