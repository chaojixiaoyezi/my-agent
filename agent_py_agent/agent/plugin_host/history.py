# LLM: 历史只读canonical原消息；每页完整验证冻结前缀，nofollow且返回前重核文件，不借paired turns或Compact摘要猜内容。
# 模块用途: 分页投影完整可见聊天记录、公开工具状态，并排除控制提示、思考与私密工具载荷。
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time

from ..common.log_redaction import redact_sensitive_text
from ..common.nofollow_fs import open_readonly_file_beneath, read_bytes_beneath
from ..conversation.display_checkpoint import display_checkpoint_event
from ..conversation.models import MessageLogEntry
from ..runtime_errors import DataCorruptionError
from .conversations import CURSOR_LIFETIME_SECONDS, BoundCursor, digest, public_time
from .errors import HostAPIError
from .schemas import PAGE_MAX_BYTES, canonical_json

# 原消息单行的内存上限，超出明确413，不伪装损坏或空历史（bytes）。
HISTORY_RECORD_MAX_BYTES = 8 * 1024 * 1024
# 冻结首尾摘要各取的字节窗口（bytes）。
ANCHOR_SAMPLE_BYTES = 4096
# 流式复核固定前缀时的块大小（bytes）。
HASH_CHUNK_BYTES = 65536
# 为页信封与MAC游标预留的JSON字节预算（bytes）。
PAGE_ENVELOPE_RESERVE_BYTES = 8192


# LLM: 重复键必须拒绝，避免正文/角色/归属被后键无声覆盖。
# 函数用途: 严格解码一行的唯一键对象。
def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate history field")
        result[key] = value
    return result


# LLM: 严格JSON不接受NaN和Infinity，错误不带原文。
# 函数用途: 拒绝非标准数字。
def _invalid_number(value):
    raise ValueError("nonfinite history value")


# LLM: 每个原记录都验证身份/类型，即使不公开也不能把损坏文件称作完整。
# 函数用途: 解码一条固定thread的完整JSONL行。
def _entry(raw, thread_id):
    if not raw.endswith(b"\n"):
        raise ValueError("incomplete history line")
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_fields, parse_constant=_invalid_number)
    if (not isinstance(value, dict) or value.get("thread_id") != thread_id
            or not isinstance(value.get("message_id"), str) or not value["message_id"]
            or not isinstance(value.get("role"), str) or not isinstance(value.get("content"), str)
            or not isinstance(value.get("metadata", {}), dict)
            or type(value.get("created_at")) not in (float, int) or not math.isfinite(value["created_at"])):
        raise ValueError("invalid history record")
    value["content"].encode("utf-8")
    return MessageLogEntry.from_dict(value)


# LLM: ID只公开逻辑标识，不接受路径、URL或任意宿主元数据作为media/turn引用。
# 函数用途: 保留安全的逻辑引用。
def _public_ref(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_:.\-]{1,256}", value) else None


# LLM: 工具白名单仅含名称和结构化成功事实；绝不转发arguments/output/result或任意摘要，哪怕已标为public。
# 函数用途: 从原工具记录中生成最小公开结果。
def _tool_outcome(payload, kind=""):
    value = payload if isinstance(payload, dict) else {}
    tool = _public_ref(value.get("tool") or value.get("tool_name"))
    ok = value.get("ok") if type(value.get("ok")) is bool else None
    if kind == "tool_failed":
        ok = False
    status = "succeeded" if ok is True else "failed" if ok is False else "unknown"
    return canonical_json({"tool": tool, "ok": ok, "status": status})


# LLM: 原检查点校验器决定display关联有效性；只投影公开assistant和tool，thinking/system/start均不出站。
# 函数用途: 选择一条原记录的可见角色、阶段和已脱敏正文。
def _visible_content(entry):
    if entry.role == "display":
        event = display_checkpoint_event(entry, live=True)
        if not event:
            return None
        if event["kind"] == "assistant_completed":
            text = event["payload"].get("text")
            return ("assistant", "commentary", redact_sensitive_text(text)) if isinstance(text, str) else None
        if event["kind"] in {"tool_completed", "tool_failed"}:
            return "tool", "tool_result", _tool_outcome(event["payload"], event["kind"])
        return None
    if entry.role == "user":
        return "user", "final", redact_sensitive_text(entry.content)
    if entry.role == "assistant":
        phase = entry.metadata.get("phase", "final")
        if phase in {"final", "commentary"}:
            return "assistant", phase, redact_sensitive_text(entry.content)
    if entry.role == "tool":
        try:
            value = json.loads(entry.content)
        except (ValueError, RecursionError):
            value = {}
        return "tool", "tool_result", _tool_outcome(value)
    return None


# LLM: 字节长度以完整公开正文计，不泄露打码前的秘密长度；decode(ignore)只切掉不完整尾codepoint。
# 函数用途: 创建固定字段的公开Message并按UTF8字节预算裁剪。
def _message(entry, visible, seq, budget):
    role, phase, text = visible
    raw = text.encode("utf-8")
    refs = entry.metadata.get("media_refs", [])
    refs = list(dict.fromkeys(ref for value in refs if (ref := _public_ref(value)))) if isinstance(refs, list) else []
    turn = next((ref for key in ("turn_id", "gateway_request_id", "background_transcript_request_id")
                 if (ref := _public_ref(entry.metadata.get(key)))), None)
    return {"message_id": entry.message_id, "thread_id": entry.thread_id, "turn_id": turn, "seq": seq,
            "role": role, "phase": phase, "content": raw[:budget].decode("utf-8", errors="ignore"),
            "content_truncated": len(raw) > budget, "content_bytes": len(raw), "media_refs": refs,
            "created_at": public_time(entry.created_at)}


# LLM: 整页预算先于limit，保留连续前缀；单项JSON转义膨胀时继续字节裁剪并明确标记，不丢整条消息。
# 函数用途: 将一条消息加入有界页面，返回是否已无空间。
def _collect(rows, message, used, limit):
    if len(rows) >= limit:
        return used, True
    size = len(canonical_json(message).encode("utf-8"))
    ceiling = PAGE_MAX_BYTES - PAGE_ENVELOPE_RESERVE_BYTES
    if rows and used + size + 1 > ceiling:
        return used, True
    while not rows and size > ceiling and message["content"]:
        raw = message["content"].encode("utf-8")
        message["content"] = raw[:len(raw) // 2].decode("utf-8", errors="ignore")
        message["content_truncated"] = True
        size = len(canonical_json(message).encode("utf-8"))
    if size > ceiling:
        raise HostAPIError("PAYLOAD_TOO_LARGE", 413)
    if used + size + 1 > ceiling:
        return used, True
    rows.append(message)
    return used + size + 1, False


# LLM: 流式读取冻结的canonical虚拟尾界（冷段+热尾），最多保留一页正文；坏行与未完成行暂记，旧快照变更优先报过期，
#   首次坏文件明确500。锚点封印虚拟前缀（记录数、字节数、首尾与全量SHA）及本快照热文件身份：原子replace同样失效。
# 函数用途: 校验JSONL并生成稳定序号、分页前缀及内容锚点。
def _scan(handle, through, thread_id, offset, payload):
    handle.seek(0)
    full_hash, head, tail = hashlib.sha256(), b"", b""
    rows, record_count, visible_count, used = [], 0, 0, 0
    error, full = False, False
    while handle.tell() < through:
        raw = handle.readline(min(through - handle.tell(), HISTORY_RECORD_MAX_BYTES + 1))
        if not raw:
            raise HostAPIError("CURSOR_EXPIRED", 409)
        if len(raw) > HISTORY_RECORD_MAX_BYTES:
            raise HostAPIError("PAYLOAD_TOO_LARGE", 413)
        full_hash.update(raw)
        head = (head + raw)[:ANCHOR_SAMPLE_BYTES]
        tail = (tail + raw)[-ANCHOR_SAMPLE_BYTES:]
        record_count += 1
        try:
            entry = _entry(raw, thread_id)
            visible = _visible_content(entry)
            if visible is not None:
                visible_count += 1
                if visible_count > offset and not full:
                    row = _message(entry, visible, visible_count, payload["max_content_bytes"])
                    used, full = _collect(rows, row, used, payload["limit"])
        except HostAPIError:
            raise
        except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
            error = True
    anchor = {"record_count": record_count, "byte_size": through,
              "head_tail_sha256": hashlib.sha256(head + tail).hexdigest(), "sha256": full_hash.hexdigest(),
              "identity": _hot_identity(handle)}
    return rows, visible_count, anchor, error


# LLM: 身份取自快照内已开的同一热FD，不重开路径；冷段由清单SHA封印，热文件被原子替换（含冷转）即换身份。
# 函数用途: 返回canonical快照热文件的[dev, ino]。
def _hot_identity(reader):
    info = os.fstat(reader.hot.fileno())
    return [info.st_dev, info.st_ino]


# LLM: 只从原规范存储根逐段nofollow打开，拒绝硬链接；逻辑thread归属由ConversationDomain先核。
#   物理热文件句柄只供Bot等需要fileno/fstat/seek的原调用方；插件历史/回复改走下方_canonical虚拟快照。
# 函数用途: 打开消息文件且不初始化任何目录。
def _open(store, thread_id):
    path = store.storage.message_path(thread_id)
    return os.fdopen(open_readonly_file_beneath(store.storage.root, path.relative_to(store.storage.root).parts), "rb")


# LLM: 受信存储根下逐段no-follow冻结同次清单、冷段、索引、热FD与物理EOF（拒链接/硬链接）；冷段按清单SHA核验。
#   EOF含未完成尾行，由扫描按坏行处理而不是静默截掉；热文件缺失而分卷清单仍在属于损坏，不能当成空历史；
#   真正从未写过消息时仍原样抛FileNotFoundError。
# 函数用途: 打开一个线程canonical转写本（冷段+热尾）的只读虚拟快照。
def _canonical(store, thread_id):
    from ..conversation.message_scan import TranscriptReader

    root = store.storage.root
    path = store.storage.message_path(thread_id)
    try:
        return TranscriptReader(path, root=root, complete_only=False)
    except FileNotFoundError:
        manifest = path.with_suffix(".manifest.json").relative_to(root).parts
        if read_bytes_beneath(root, manifest, max_bytes=65536, require_dir_fd=True) is not None:
            raise DataCorruptionError("canonical hot transcript is missing") from None
        raise


# LLM: 只在本次已打开的同一虚拟快照上分块核完整旧前缀；追加合法，截短/改写/原子替换（含冷转）/链接或硬链接均409。
# 函数用途: 验证一个canonical快照仍完整包含锚点封印的前缀内容。
def _verify_snapshot(reader, anchor):
    size = anchor.get("byte_size")
    if type(size) is not int or not 0 <= size <= reader.end or _hot_identity(reader) != anchor.get("identity"):
        raise HostAPIError("CURSOR_EXPIRED", 409)
    reader.seek(0)
    remaining, check = size, hashlib.sha256()
    while remaining:
        raw = reader.read(min(remaining, HASH_CHUNK_BYTES))
        if not raw:
            raise HostAPIError("CURSOR_EXPIRED", 409)
        check.update(raw)
        remaining -= len(raw)
    if check.hexdigest() != anchor.get("sha256") or os.fstat(reader.hot.fileno()).st_nlink != 1:
        raise HostAPIError("CURSOR_EXPIRED", 409)
    reader.seek(0)


# LLM: 首尾hash之外核全部冻结前缀；相同长度的中段改写与原子replace同样失效，追加不参与旧快照。
#   从当前规范路径重新打开canonical快照；冷段缺失/损坏或热文件消失都使旧快照过期，不当作空成功。
# 函数用途: 返回前从当前规范路径再次验证实际读取的固定前缀。
def _verify_current(store, thread_id, anchor):
    try:
        with _canonical(store, thread_id) as reader:
            _verify_snapshot(reader, anchor)
    except (OSError, DataCorruptionError, ValueError):
        raise HostAPIError("CURSOR_EXPIRED", 409) from None


# LLM: 缺文件仅在首次有效线程读取时为空；续页解析提前失败也先复核旧锚点，替换/改写优先报409而非500/413。
#   读取canonical虚拟快照（冷段+热尾）：首页冻结同次EOF，续页按旧锚点虚拟前缀重扫；冷段缺失/损坏不当作空历史。
# 函数用途: 读取冻结的canonical转写本快照并返回一页完整可见历史。
def history_page(store, thread, payload, authorized, context, cursor_codec):
    filters = {key: value for key, value in payload.items() if key != "cursor"}
    cursor = BoundCursor(cursor_codec, authorized, context, filters)
    saved = cursor.decode(payload.get("cursor"))
    try:
        handle = _canonical(store, thread.thread_id)
    except FileNotFoundError:
        if saved:
            raise HostAPIError("CURSOR_EXPIRED", 409) from None
        return {"items": [], "next_cursor": None, "snapshot_id": digest([thread.thread_id, 0]), "complete": True, "load_errors": []}
    except (OSError, DataCorruptionError, ValueError):
        raise HostAPIError("CURSOR_EXPIRED" if saved else "CORRUPT_HISTORY", 409 if saved else 500) from None
    try:
        with handle:
            through = saved["anchor"].get("byte_size") if saved else handle.end
            offset = saved["offset"] if saved else 0
            if (type(through) is not int or not 0 <= through <= handle.end
                    or (saved and saved["anchor"].get("identity") != _hot_identity(handle))):
                raise HostAPIError("CURSOR_EXPIRED", 409)
            try:
                rows, count, anchor, error = _scan(handle, through, thread.thread_id, offset, payload)
            except HostAPIError:
                if saved:
                    _verify_current(store, thread.thread_id, saved["anchor"])
                raise
            except DataCorruptionError:
                if saved:
                    raise HostAPIError("CURSOR_EXPIRED", 409) from None
                raise HostAPIError("CORRUPT_HISTORY", 500) from None
            if saved and (saved["anchor"] != anchor or offset > count):
                raise HostAPIError("CURSOR_EXPIRED", 409)
            if error:
                raise HostAPIError("CORRUPT_HISTORY", 500)
            _verify_current(store, thread.thread_id, anchor)
    except (OSError, DataCorruptionError):
        raise HostAPIError("CURSOR_EXPIRED" if saved else "CORRUPT_HISTORY", 409 if saved else 500) from None
    end = offset + len(rows)
    expires = saved["expires"] if saved else time.time() + CURSOR_LIFETIME_SECONDS
    token = cursor.encode(anchor, end, expires) if end < count else None
    page = {"items": rows, "next_cursor": token, "snapshot_id": digest(anchor), "complete": token is None, "load_errors": []}
    if len(canonical_json(page).encode("utf-8")) > PAGE_MAX_BYTES:
        raise HostAPIError("PAYLOAD_TOO_LARGE", 413)
    return page
