# LLM: Runtime guidance is a soft inbox for steering active main/sub agents.
# 模块用途: 记录、查询和标记运行中补充提示；只给模型下一轮参考，不作为硬门。

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ..gateway_parts.io import read_json_file, update_json_file_atomic
from ..io.jsonl import append_jsonl
from .models import new_id
from .models_guidance import GuidanceEntry
from .store_common import now as current_time
from .store_common import read_jsonl, safe_file_stem
from .store_observations import ConversationObservationStore


# LLM: ConversationGuidanceStore extends the conversation ledger with soft steering inboxes.
# 类用途: 管理运行中 guidance 的追加、读取和投递标记，供主代理和子代理统一使用。
class ConversationGuidanceStore(ConversationObservationStore):
    # LLM: append_guidance persists a human/model steering note without forcing execution.
    # 函数用途: 向 thread、agent_run、task 或 case 的 guidance inbox 追加一条软提示。
    def append_guidance(self, request: dict[str, Any]) -> GuidanceEntry:
        target_type = normalize_guidance_target_type(request.get("target_type") or request.get("type"))
        target_id = str(request.get("target_id") or request.get("id") or "").strip()
        message = str(request.get("message") or request.get("body") or request.get("prompt") or "").strip()
        if not target_type or not target_id:
            raise ValueError("target_type and target_id are required")
        if not message:
            raise ValueError("guidance message is required")
        entry = GuidanceEntry(
            guidance_id=new_id("guidance"),
            target_type=target_type,
            target_id=target_id,
            message=message,
            sender=str(request.get("sender") or "").strip(),
            priority=str(request.get("priority") or "normal").strip() or "normal",
            delivery=str(request.get("delivery") or "next_turn").strip() or "next_turn",
            created_at=current_time(request.get("now")),
            metadata=request.get("metadata") if isinstance(request.get("metadata"), dict) else {},
        )
        append_jsonl(self._guidance_path(target_type, target_id), entry.to_dict(), sort_keys=True)
        return entry

    # LLM: recent_guidance reads the ledger with delivery timestamps attached.
    # 函数用途: 查询某个目标的 guidance 流水，include_delivered=false 时只返回未投递项。
    def recent_guidance(
        self,
        target_type: str,
        target_id: str,
        *,
        limit: int = 20,
        include_delivered: bool = True,
    ) -> list[GuidanceEntry]:
        normalized_type = normalize_guidance_target_type(target_type)
        delivered = self._read_guidance_delivered()
        rows = read_jsonl(self._guidance_path(normalized_type, str(target_id)))
        entries = [
            _with_guidance_delivered_at(GuidanceEntry.from_dict(row), delivered)
            for row in rows
            if isinstance(row, dict)
        ]
        if not include_delivered:
            entries = [item for item in entries if item.delivered_at <= 0]
        return entries if limit <= 0 else entries[-limit:]

    # LLM: pending_guidance returns only not-yet-delivered rows for a target.
    # 函数用途: 查询某个目标下一轮还需要看到的补充提示。
    def pending_guidance(self, target_type: str, target_id: str, *, limit: int = 20) -> list[GuidanceEntry]:
        return self.recent_guidance(target_type, target_id, limit=limit, include_delivered=False)

    # LLM: mark_guidance_delivered makes injection one-shot while keeping audit history.
    # 函数用途: 标记 guidance 已经进入某轮 prompt，避免每轮重复注入同一条提示。
    def mark_guidance_delivered(
        self,
        guidance_ids: list[str] | tuple[str, ...],
        *,
        now: float | None = None,
    ) -> None:
        ids = [str(item).strip() for item in guidance_ids if str(item or "").strip()]
        if not ids:
            return
        delivered_at = current_time(now)
        update_json_file_atomic(
            self.guidance_delivered_path,
            lambda data: {**data, **dict.fromkeys(ids, delivered_at)},
        )

    # LLM: _read_guidance_delivered loads delivery timestamps without failing on bad rows.
    # 函数用途: 读取 guidance 已投递索引，坏值跳过，避免账本损坏中断运行。
    def _read_guidance_delivered(self) -> dict[str, float]:
        delivered: dict[str, float] = {}
        for key, value in read_json_file(self.guidance_delivered_path).items():
            try:
                delivered[str(key)] = float(value or 0.0)
            except (TypeError, ValueError):
                continue
        return delivered


# LLM: normalize_guidance_target_type keeps aliases open-world but stable for known targets.
# 函数用途: 将常见 target 别名归一，未知类型保留安全文件名形态，避免封闭枚举卡住扩展。
def normalize_guidance_target_type(value: object) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "run": "agent_run",
        "runner": "agent_run",
        "subagent": "agent_run",
        "subagent_run": "agent_run",
        "agent": "agent_run",
        "agent_run": "agent_run",
        "thread": "thread",
        "conversation": "thread",
        "task": "task",
        "case": "case",
    }
    return aliases.get(text, safe_file_stem(text))


# LLM: _with_guidance_delivered_at projects delivery state onto immutable GuidanceEntry.
# 函数用途: 给读取到的 guidance 行补上 delivered_at，保持原始 JSONL 不被改写。
def _with_guidance_delivered_at(entry: GuidanceEntry, delivered: dict[str, float]) -> GuidanceEntry:
    return replace(entry, delivered_at=float(delivered.get(entry.guidance_id) or 0.0))
