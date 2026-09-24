# LLM: 历史排序只读 canonical 行次序和显示块身份；不按正文或墙钟猜关联，不改变模型历史。
# 模块用途: 把跨工作片投影恢复到原事件顺序，并去重插话的显示检查点和消费后的用户记录。

from __future__ import annotations

from collections.abc import Sequence

from .display_checkpoint import display_checkpoint_event
from .history_page import history_group_identity


# LLM: 首次检查点位置固定，终态只替换内容；精确用户消息编号只用于展示去重，不证明模型消费。
# 函数用途: 对已投影的历史事件恢复源顺序，避免工作片分组把插话挤到所有回答后面。
def order_history_events(rows: Sequence[object], events: list[dict]) -> tuple[dict, ...]:
    positions, user_rows, group_ends, row_requests = {}, {}, {}, {}
    for index, row in enumerate(rows):
        group_ends[history_group_identity(row)] = index
        if event := display_checkpoint_event(row):
            positions.setdefault(event["block_id"], index)
        if getattr(row, "role", "") in {"user", "assistant"}:
            thread, message = getattr(row, "thread_id", ""), getattr(row, "message_id", "")
            row_requests[f"history:{thread}:{message}"] = index
            if row.role == "user":
                block_id = f"history:{history_group_identity(row)}:user:{message}"
                user_rows[block_id] = str(getattr(row, "channel_message_id", "") or "")
            else:
                block_id = f"history:{thread}:{message}:assistant"
            positions[block_id] = index
    displayed_inputs = {event.get("payload", {}).get("message_id") for event in events
                        if event.get("kind") == "user_message" and event["block_id"] not in user_rows}
    ordered = []
    for ordinal, event in enumerate(events):
        block_id = event["block_id"]
        if user_rows.get(block_id) and user_rows[block_id] in displayed_inputs:
            continue
        identity = str(event.get("gateway_request_id") or event.get("request_id") or "").removeprefix("history:")
        position = positions.get(block_id, row_requests.get(event.get("request_id"), group_ends.get(identity, len(rows))))
        ordered.append((position, ordinal, event))
    return tuple(event for _position, _ordinal, event in sorted(ordered, key=lambda item: item[:2]))
