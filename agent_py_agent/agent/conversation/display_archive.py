# LLM: Display archives contain already-public immutable presentation rows, never model context.
# 模块用途: 把完整工具展示保存为有界原文页；不重新读取业务文件，不参与模型输入或 Compact。

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from collections.abc import Iterable, Mapping
from pathlib import Path

DISPLAY_ARCHIVE_SCHEMA = "display_archive_ref.v1"
DISPLAY_PAGE_ROWS = 256
DISPLAY_PAGE_CHARS = 12_000
DISPLAY_ROW_CHARS = 2_000
_MAX_PAGE_BYTES = 256_000
_ID = re.compile(r"[A-Za-z0-9_-]{1,160}\Z")
_ARCHIVE_ID = re.compile(r"[0-9a-f]{32}\Z")


# LLM: Public errors carry stable machine codes without exposing private disk paths.
# 类用途: 向 Gateway 返回原文缺失、引用非法或损坏的准确类别。
class DisplayArchiveError(ValueError):
    # LLM: Keep code separate from text so callers never infer authorization from a message.
    # 函数用途: 保存可公开的错误类型和说明。
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# LLM: Only trusted producers provide the canonical thread id and pre-sanitized rows; refs never
# contain paths. Publishing the manifest last makes incomplete archives invisible after crashes.
# 函数用途: 逐页保存本次真实显示快照，最后发布引用；返回后业务文件变化不影响历史原文。
def archive_display_rows(agent: object, *, thread_id: str, rows: Iterable[Mapping]) -> dict[str, object]:
    store = agent.conversation_store
    if not _ID.fullmatch(thread_id) or store.load_thread(thread_id) is None:
        raise DisplayArchiveError("DISPLAY_THREAD_INVALID", "原文没有有效的会话身份。")
    archive_id = uuid.uuid4().hex
    root = _archive_root(store)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = root / archive_id
    directory.mkdir(mode=0o700)
    page: list[dict[str, object]] = []
    page_chars = page_index = row_count = char_count = 0
    for row_index, raw in enumerate(rows):
        if not isinstance(raw, Mapping) or not isinstance(raw.get("text"), str):
            raise DisplayArchiveError("DISPLAY_ROW_INVALID", "原文行格式错误。")
        text = raw["text"]
        kind = str(raw.get("kind") or "text")[:48]
        count = max(1, (len(text) + DISPLAY_ROW_CHARS - 1) // DISPLAY_ROW_CHARS)
        for part_index in range(count):
            chunk = text[part_index * DISPLAY_ROW_CHARS:(part_index + 1) * DISPLAY_ROW_CHARS]
            if page and (len(page) >= DISPLAY_PAGE_ROWS or page_chars + len(chunk) > DISPLAY_PAGE_CHARS):
                _write_json_new(directory / f"{page_index}.json", {"rows": page})
                page_index += 1
                page, page_chars = [], 0
            page.append({"kind": kind, "text": chunk, "row_index": row_index,
                         "part_index": part_index, "part_count": count})
            page_chars += len(chunk)
        row_count += 1
        char_count += len(text)
    if page or not page_index:
        _write_json_new(directory / f"{page_index}.json", {"rows": page})
        page_index += 1
    reference = {"schema": DISPLAY_ARCHIVE_SCHEMA, "archive_id": archive_id,
                 "thread_id": thread_id, "page_count": page_index,
                 "row_count": row_count, "char_count": char_count}
    _write_json_new(directory / "manifest.json", reference)
    return reference


# LLM: Callers authorize the exact owner/thread before invoking this read. Client counts are
# ignored; manifest identity and bounds are authoritative, and every file read is size-limited.
# 函数用途: 只读取引用指向的一页原文，拒绝路径、负页码、软链接或不完整归档。
def read_display_archive_page(store: object, reference: object, page_index: object) -> dict[str, object]:
    archive_id, thread_id = validate_display_archive_reference(reference)
    if isinstance(page_index, bool) or not isinstance(page_index, int) or page_index < 0:
        raise DisplayArchiveError("DISPLAY_PAGE_INVALID", "原文页码无效。")
    directory = _archive_root(store) / archive_id
    if directory.is_symlink():
        raise DisplayArchiveError("DISPLAY_ARCHIVE_UNAVAILABLE", "原文归档不可读取。")
    manifest = _read_json_bounded(directory / "manifest.json", 4096)
    count = manifest.get("page_count")
    if (manifest.get("schema") != DISPLAY_ARCHIVE_SCHEMA or manifest.get("archive_id") != archive_id
            or manifest.get("thread_id") != thread_id or type(count) is not int or count < 1):
        raise DisplayArchiveError("DISPLAY_ARCHIVE_INVALID", "原文归档身份或索引损坏。")
    if page_index >= count:
        raise DisplayArchiveError("DISPLAY_PAGE_INVALID", "原文页码超出范围。")
    data = _read_json_bounded(directory / f"{page_index}.json", _MAX_PAGE_BYTES)
    rows = data.get("rows")
    _validate_page_rows(rows)
    return {"ok": True, "reference": manifest, "rows": rows, "page_index": page_index,
            "page_count": count, "has_previous": page_index > 0, "has_next": page_index + 1 < count}


# LLM: Only opaque identifiers cross the API; known path-like or extra fields are not accepted.
# 函数用途: 在任何文件访问前严格校验显示引用，计数仅供展示而非文件定位。
def validate_display_archive_reference(reference: object) -> tuple[str, str]:
    allowed = {"schema", "archive_id", "thread_id", "page_count", "row_count", "char_count"}
    if not isinstance(reference, dict) or set(reference) - allowed:
        raise DisplayArchiveError("DISPLAY_REF_INVALID", "原文引用格式无效。")
    archive_id, thread_id = reference.get("archive_id"), reference.get("thread_id")
    if (reference.get("schema") != DISPLAY_ARCHIVE_SCHEMA or not isinstance(archive_id, str)
            or not _ARCHIVE_ID.fullmatch(archive_id) or not isinstance(thread_id, str)
            or not _ID.fullmatch(thread_id)):
        raise DisplayArchiveError("DISPLAY_REF_INVALID", "原文引用格式无效。")
    return archive_id, thread_id


# LLM: The archive lives only under the canonical owner ConversationStore, never client cwd.
# 函数用途: 取得唯一显示归档目录，并阻止归档目录被软链接转向其它数据。
def _archive_root(store: object) -> Path:
    root = Path(store.root) / "display_archives"
    if root.is_symlink():
        raise DisplayArchiveError("DISPLAY_ARCHIVE_UNAVAILABLE", "原文归档不可读取。")
    return root


# LLM: UUID directories and exclusive creation prevent accidental replacement; files are private.
# 函数用途: 保存一个不可覆写 JSON 页，关闭前落盘，确保发布后的页面完整。
def _write_json_new(path: Path, payload: dict) -> None:
    descriptor = _open_archive_file(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())


# LLM: No-follow and regular-file checks stop symlink/FIFO escapes before reading bounded bytes.
# 函数用途: 安全读取小型原文页；缺失、损坏或过大的文件明确报错，不透露服务器路径。
def _read_json_bounded(path: Path, maximum: int) -> dict:
    try:
        descriptor = _open_archive_file(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("not regular")
            raw = stream.read(maximum + 1)
        if len(raw) > maximum:
            raise ValueError("oversize")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("not object")
        return payload
    except (OSError, ValueError) as exc:
        raise DisplayArchiveError("DISPLAY_ARCHIVE_UNAVAILABLE", "完整原文缺失或损坏，请保留现有预览。") from exc


# LLM: POSIX opens each archive directory through no-follow descriptors, preventing directory
# replacement races; platforms without dir_fd still enforce resolved archive containment.
# 函数用途: 通过已打开的目录安全读写原文页，不能被替换目录的软链接导向其它位置。
def _open_archive_file(path: Path, flags: int) -> int:
    directory, root = path.parent, path.parent.parent
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if os.open not in os.supports_dir_fd:
        if root.is_symlink() or directory.is_symlink() or path.is_symlink():
            raise OSError("archive symlink")
        if not path.resolve().is_relative_to(root.resolve()):
            raise OSError("archive escaped")
        return os.open(path, flags | no_follow, 0o600)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | no_follow
    root_descriptor = os.open(root, directory_flags)
    try:
        directory_descriptor = os.open(directory.name, directory_flags, dir_fd=root_descriptor)
        try:
            return os.open(path.name, flags | no_follow, 0o600, dir_fd=directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        os.close(root_descriptor)


# LLM: Disk is untrusted input too; corruption cannot turn a bounded page into unlimited rendering.
# 函数用途: 校验磁盘页大小和分片字段，防止损坏记录拖垮终端。
def _validate_page_rows(rows: object) -> None:
    valid = isinstance(rows, list) and len(rows) <= DISPLAY_PAGE_ROWS
    chars = 0
    for row in rows if valid else ():
        if (not isinstance(row, dict) or not isinstance(row.get("text"), str)
                or len(row["text"]) > DISPLAY_ROW_CHARS or not isinstance(row.get("kind"), str)
                or any(type(row.get(key)) is not int for key in ("row_index", "part_index", "part_count"))
                or row["row_index"] < 0 or not 0 <= row["part_index"] < row["part_count"]):
            valid = False
            break
        chars += len(row["text"])
    if not valid or chars > DISPLAY_PAGE_CHARS:
        raise DisplayArchiveError("DISPLAY_ARCHIVE_INVALID", "原文页面格式损坏。")
