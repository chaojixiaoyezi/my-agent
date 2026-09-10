# LLM: Recover exact path arguments from matched canonical native tool calls/results, never from
# prose or shell text. These are historical lookup hints, not cwd, permissions or current existence.
# 模块用途: 压缩时保留近期成功工具用过的原样路径，避免语义摘要漏掉项目位置后模型凭记忆乱找。

from __future__ import annotations

from collections.abc import Iterable
from itertools import chain

from .display_checkpoint import is_display_checkpoint
from .models import is_audit_background_transcript_entry
from .native_history import canonical_native_messages_from_metadata

_LIMIT = 24
_PATH_ARGUMENTS = ("path", "working_dir")


# LLM: The compact checkpoint owns the returned bounded projection; source messages remain the
# authority. Prior entries retain their exact bytes, and the latest observation of a path wins.
# 函数用途: 把已压缩路径索引和新历史合并，整条保留路径与来源；不读文件、不猜任务目录、不扩权限。
def merge_compact_tool_refs(previous: object, rows: Iterable[object]) -> list[dict[str, str]]:
    refs = normalize_compact_tool_refs(previous)
    for row in rows:
        if getattr(row, "role", "") != "assistant" or is_audit_background_transcript_entry(row) or is_display_checkpoint(row):
            continue
        metadata = getattr(row, "metadata", {})
        metadata = metadata if isinstance(metadata, dict) else {}
        refs.extend(_matched_path_refs(metadata))
    unique: dict[tuple[str, str], dict[str, str]] = {}
    for ref in refs:
        key = (ref["argument"], ref["path"])
        unique.pop(key, None)
        unique[key] = ref
    return list(unique.values())[-_LIMIT:]


# LLM: Accept only this projection's bounded scalar fields; incomplete entries cannot become a
# path hint. Never truncate a path into a different valid-looking location.
# 函数用途: 清洗恢复点里的路径提示，丢弃非法或过长整条记录，不从文本恢复缺失字段。
def normalize_compact_tool_refs(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value[-_LIMIT:]:
        if not isinstance(item, dict):
            continue
        fields = {key: item.get(key) for key in ("tool", "call_id", "argument", "path")}
        if any(not isinstance(text, str) or not text.strip() or len(text) > 1024 for text in fields.values()):
            continue
        if fields["argument"] not in _PATH_ARGUMENTS or any(char in fields["path"] for char in ("\n", "\r", "\x00")):
            continue
        result.append(fields)
    return result


# LLM: Match exact tool_use_id in one canonical transcript row and require explicit non-error
# tool_result. Do not interpret output text, shell commands, model statements or unmatched calls.
# 函数用途: 从真实工具往返提取原样路径参数；失败、孤立调用和模型口头说“文件在某处”均不采用。
def _matched_path_refs(metadata: dict) -> list[dict[str, str]]:
    calls: dict[str, dict] = {}
    result = []
    contents = (message.get("content") for message in canonical_native_messages_from_metadata(metadata))
    for block in chain.from_iterable(content for content in contents if isinstance(content, list)):
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use":
            call_id = block.get("id")
            if isinstance(call_id, str) and call_id:
                calls[call_id] = block
        elif block.get("type") == "tool_result" and block.get("is_error") is False:
            call_id = block.get("tool_use_id")
            call = calls.pop(call_id, None) if isinstance(call_id, str) else None
            if call is None or not isinstance(call.get("input"), dict):
                continue
            result.extend(normalize_compact_tool_refs([
                {"tool": call.get("name"), "call_id": call.get("id"), "argument": field, "path": call["input"][field]}
                for field in _PATH_ARGUMENTS if field in call["input"]
            ]))
    return result
