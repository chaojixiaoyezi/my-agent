# LLM: 归档只含公开展示副本；脱链FD废弃并有限重读，合页保持无损、TTL读取权威墓碑，不接受多硬链接。
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
# 展示归档：单页最多 256 行，控制分页行数。
DISPLAY_PAGE_ROWS_COUNT = 256
# 展示归档：单页最多 12000 字符，控制分页大小。
DISPLAY_PAGE_CHARS = 12_000
# 展示归档：单行最多 2000 字符，超长行截断显示。
DISPLAY_ROW_CHARS = 2_000
# 展示归档单页最大 256000 字节：控制分页存储体积。
_MAX_PAGE_BYTES = 256_000
# 展示归档合页的总读取上限，独立于资产投递包的大小限制。
_MAX_DISPLAY_ARCHIVE_BUNDLE_BYTES = 16 * 1024 * 1024
DISPLAY_BUNDLE_SCHEMA = "display_archive_bundle.v1"
_ID = re.compile(r"[A-Za-z0-9_-]{1,160}\Z")
_ARCHIVE_ID = re.compile(r"[0-9a-f]{32}\Z")


# LLM: 公开错误只带稳定机器码和安全说明，不透露服务器磁盘路径。
# 类用途: 向 Gateway 返回原文缺失、引用非法或损坏的准确类别。
class DisplayArchiveError(ValueError):
    # LLM: 错误码与说明分开保存，调用方不得按自然语言判断授权或归档状态。
    # 函数用途: 保存可公开的错误类型和说明。
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# LLM: 可信producer靠独立UUID目录与manifest最后发表同维护根锁隔离，不获取维护锁；已发布页不可覆写，引用不含路径。
# 函数用途: 逐页保存本次真实显示快照，最后发布引用；返回后业务文件变化不影响历史原文。
def archive_display_rows(agent: object, *, thread_id: str, rows: Iterable[Mapping]) -> dict[str, object]:
    store = agent.conversation_store
    if not _ID.fullmatch(thread_id) or store.threads.load(thread_id) is None:
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
            if page and (len(page) >= DISPLAY_PAGE_ROWS_COUNT or page_chars + len(chunk) > DISPLAY_PAGE_CHARS):
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


# LLM: 调用方先认证owner/thread；只信有界重读的磁盘索引，脱链页按缺失重取墓碑和bundle，不吞危险叶错误。
# 函数用途: 只读取引用指向的一页原文，拒绝路径、负页码、软链接或不完整归档。
def read_display_archive_page(store: object, reference: object, page_index: object) -> dict[str, object]:
    archive_id, thread_id = validate_display_archive_reference(reference)
    if isinstance(page_index, bool) or not isinstance(page_index, int) or page_index < 0:
        raise DisplayArchiveError("DISPLAY_PAGE_INVALID", "原文页码无效。")
    directory = _archive_root(store) / archive_id
    if directory.is_symlink():
        raise DisplayArchiveError("DISPLAY_ARCHIVE_UNAVAILABLE", "原文归档不可读取。")
    manifest = _read_manifest(directory)
    count = manifest.get("page_count")
    if (manifest.get("schema") != DISPLAY_ARCHIVE_SCHEMA or manifest.get("archive_id") != archive_id
            or manifest.get("thread_id") != thread_id or type(count) is not int or count < 1):
        raise DisplayArchiveError("DISPLAY_ARCHIVE_INVALID", "原文归档身份或索引损坏。")
    _raise_if_expired(manifest)
    if page_index >= count:
        raise DisplayArchiveError("DISPLAY_PAGE_INVALID", "原文页码超出范围。")
    try:
        data = _read_json_bounded(directory / f"{page_index}.json", _MAX_PAGE_BYTES)
        rows = data.get("rows")
    except DisplayArchiveError as exc:
        if not isinstance(exc.__cause__, FileNotFoundError):
            raise
        try:
            _raise_if_expired(_read_manifest(directory))
            bundle = _read_json_bounded(directory / "pages_bundle.json", _bundle_limit(count))
            pages = _validate_bundle(bundle, manifest)
            rows = pages[page_index]
        except DisplayArchiveError:
            # 过期提交先发表墓碑再删页；跨越这个窗口的读者也必须得到明确过期。
            _raise_if_expired(_read_manifest(directory))
            raise
    _validate_page_rows(rows)
    return {"ok": True, "reference": manifest, "rows": rows, "page_index": page_index,
            "page_count": count, "has_previous": page_index > 0, "has_next": page_index + 1 < count}


# LLM: 只对缺失/已脱链manifest最多尝试三次；类型、链接、权限和损坏错误不被重试洗掉。
# 函数用途: 跨TTL原子替换窗口重新读取当前权威墓碑。
def _read_manifest(directory):
    for attempt in range(3):
        try:
            return _read_json_bounded(directory / "manifest.json", 4096)
        except DisplayArchiveError as exc:
            if not isinstance(exc.__cause__, FileNotFoundError) or attempt == 2:
                raise


# LLM: 此状态来自磁盘manifest，不接受客户端引用中的元数据来触发过期。
# 函数用途: 让TTL缺页与损坏、暂时不可读保持不同错误码。
def _raise_if_expired(manifest: dict) -> None:
    if manifest.get("expired") is True:
        raise DisplayArchiveError("DISPLAY_ARCHIVE_EXPIRED", "展示原文已按保留策略过期，请保留现有预览。")


# LLM: 只用已校验的磁盘page_count估算读取上限，总大小始终受16MiB硬界限制。
# 函数用途: 给合页读取保留JSON封装余量而不允许无界读取。
def _bundle_limit(count: int) -> int:
    return min(_MAX_DISPLAY_ARCHIVE_BUNDLE_BYTES, max(_MAX_PAGE_BYTES, _MAX_PAGE_BYTES * count))


# LLM: bundle必须绑定同一归档与线程且逐页满足原行合同；维护验证与读页共用此校验。
# 函数用途: 校验合页内容并返回原样行列表。
def _validate_bundle(bundle: dict, manifest: dict) -> list:
    pages = bundle.get("pages")
    if (bundle.get("schema") != DISPLAY_BUNDLE_SCHEMA
            or bundle.get("archive_id") != manifest["archive_id"]
            or bundle.get("thread_id") != manifest["thread_id"]
            or not isinstance(pages, list) or len(pages) != manifest["page_count"]):
        raise DisplayArchiveError("DISPLAY_ARCHIVE_INVALID", "原文合页身份或索引损坏。")
    for rows in pages:
        _validate_page_rows(rows)
    return pages


# LLM: API只接收不透明身份与原六字段引用；额外路径或TTL字段均不可作为调用引用。
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


# LLM: 归档根只来自规范owner的ConversationStore，不读取客户端工作目录，也不跟随归档根链接。
# 函数用途: 取得唯一显示归档目录，并阻止归档目录被软链接转向其它数据。
def _archive_root(store: object) -> Path:
    root = Path(store.storage.root) / "display_archives"
    if root.is_symlink():
        raise DisplayArchiveError("DISPLAY_ARCHIVE_UNAVAILABLE", "原文归档不可读取。")
    return root


# LLM: UUID目录及排他新建防覆写；fdopen失败也关闭裸FD，私有文件fsync后由producer发表manifest。
# 函数用途: 保存一个不可覆写 JSON 页，关闭前落盘，确保发布后的页面完整。
def _write_json_new(path: Path, payload: dict) -> None:
    descriptor = _open_archive_file(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    with _fdopen_owned(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())


# LLM: nofollow拒绝危险叶；脱链FD不接受正文而按缺失交调用方有限重读，前后fstat仍拒绝多链接。
# 函数用途: 安全读取小型原文页；缺失、损坏或过大的文件明确报错，不透露服务器路径。
def _read_json_bounded(path: Path, maximum: int) -> dict:
    try:
        descriptor = _open_archive_file(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with _fdopen_owned(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink > 1:
                raise ValueError("not regular")
            if metadata.st_nlink == 0:
                raise FileNotFoundError("archive leaf detached")
            raw = stream.read(maximum + 1)
            after = os.fstat(stream.fileno())
            if not stat.S_ISREG(after.st_mode) or after.st_nlink > 1:
                raise ValueError("not regular")
            if after.st_nlink == 0:
                raise FileNotFoundError("archive leaf detached")
        if len(raw) > maximum:
            raise ValueError("oversize")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("not object")
        return payload
    except (OSError, ValueError) as exc:
        raise DisplayArchiveError("DISPLAY_ARCHIVE_UNAVAILABLE", "完整原文缺失或损坏，请保留现有预览。") from exc


# LLM: 成功返回后所有权仅归文件对象；构造抛错时本层关闭裸FD，不二次关闭成功封装的资源。
# 函数用途: 为归档读写建立没有空档的FD交接。
def _fdopen_owned(descriptor: int, mode: str, *, encoding: str | None = None):
    try:
        return os.fdopen(descriptor, mode, encoding=encoding)
    except BaseException:
        os.close(descriptor)
        raise


# LLM: POSIX逐层nofollow打开根与UUID目录，避免替换竞态；无dirfd平台仍检查链接和归档内包含关系。
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


# LLM: 磁盘行同样是不可信输入；单页行数、字符及分片字段受限，损坏不得变成无界渲染。
# 函数用途: 校验磁盘页大小和分片字段，防止损坏记录拖垮终端。
def _validate_page_rows(rows: object) -> None:
    valid = isinstance(rows, list) and len(rows) <= DISPLAY_PAGE_ROWS_COUNT
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
