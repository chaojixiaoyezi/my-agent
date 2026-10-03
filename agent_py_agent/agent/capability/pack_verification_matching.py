# LLM: 能力包 v2 块 3：宿主按包声明的“路径模式 + 可选字段匹配”认文件（交付物和检查程序的关联输入共用同一判定），
#   以及对工作区做有界快照。只读文件、不执行任何内容。glob 是工作区相对路径，`**` 整段匹配零个或多个目录；
#   字段匹配首期只认 json（顶层对象的某个字段等于声明取值之一）；format 是开放字符串，宿主不认识的格式只按路径认。
#   扫描不跟随符号链接，有走访条目数和匹配文件数上限，超限记 truncated，不静默当成完整。
#   改动同步 test_pack_verification_matching.py 与 docs/design/CAPABILITY_PACKS_V2.md 第 3 节。
# 模块用途: 给写后检查、收尾检查和输入解析提供同一套“这个文件算不算某个声明”的判定，以及回合开始时的工作区基线。

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

# 宿主认识、能做字段比对的格式名。
FIELD_MATCH_FORMAT_JSON = "json"
# 字段比对时读取文件的字节上限；更大的文件不解析，按“不匹配”处理。
MAX_FIELD_MATCH_FILE_BYTES = 4_194_304
# 工作区快照最多走访的目录条目数（文件和目录合计），防止在很大的目录树上卡住；超限记 truncated。
MAX_SCAN_VISITED_ENTRIES_COUNT = 20_000
# 工作区快照最多记录的匹配文件数；超限记 truncated。
MAX_SCAN_MATCHED_FILES_COUNT = 512
# 快照里计算摘要的单文件字节上限；更大的文件只记大小、sha256 为空，不会被当作可交给检查程序的输入。
MAX_SCAN_HASHED_FILE_BYTES = 16_777_216


# 类用途: 一个文件在某一时刻的内容摘要和大小（sha256 为空表示文件太大没算）。
@dataclass(frozen=True)
class FileState:
    sha256: str
    size: int


# LLM: files 的键是工作区相对的 posix 路径；truncated=True 表示扫描到了上限，结果不完整，调用方要把它当事实记下来。
# 类用途: 一次有界工作区扫描的结果，可序列化进运行账本。
@dataclass(frozen=True)
class WorkspaceScan:
    files: dict[str, FileState] = field(default_factory=dict)
    truncated: bool = False

    # 函数用途: 序列化为账本里的固定字段。
    def to_payload(self) -> dict:
        return {"files": {path: {"sha256": state.sha256, "size": state.size} for path, state in self.files.items()},
                "truncated": self.truncated}

    # LLM: 账本是宿主自己写的；形状不对就按空快照处理，不猜。
    # 函数用途: 从账本记录恢复快照。
    @classmethod
    def from_payload(cls, payload: object) -> WorkspaceScan:
        rows = payload.get("files") if isinstance(payload, dict) else None
        if not isinstance(rows, dict):
            return cls()
        files = {path: FileState(str(row.get("sha256") or ""), int(row.get("size") or 0))
                 for path, row in rows.items() if isinstance(path, str) and isinstance(row, dict)}
        return cls(files, bool(payload.get("truncated")))


# LLM: pattern 已由 capability_verification_manifest 校验过（无绝对路径、无 ..、** 只能整段出现）。
# 函数用途: 判断工作区相对路径是否匹配一个声明的路径模式。
def path_matches(pattern: str, relpath: str) -> bool:
    return _pattern_regex(pattern).fullmatch(relpath) is not None


# LLM: `**` 不在末尾时匹配零个或多个整段目录，在末尾时匹配一个或多个整段；`*` 和 `?` 不跨目录。
# 函数用途: 把路径模式编译成正则（带缓存）。
@lru_cache(maxsize=256)
def _pattern_regex(pattern: str) -> re.Pattern[str]:
    segments = pattern.split("/")
    pieces = []
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment == "**":
            pieces.append("(?:[^/]+/)*[^/]+" if last else "(?:[^/]+/)*")
        else:
            pieces.append(_segment_regex(segment) + ("" if last else "/"))
    return re.compile("".join(pieces))


# 函数用途: 把一个路径段里的 * 和 ? 换成不跨目录的正则，其余字符原样转义。
def _segment_regex(segment: str) -> str:
    return "".join("[^/]*" if char == "*" else "[^/]" if char == "?" else re.escape(char) for char in segment)


# LLM: declaration 是 DeliverableDeclaration 或 VerifierInput（都有 path_patterns 和 field_match）。路径不匹配直接 False，
#   路径匹配后才读文件做字段比对。
# 函数用途: 判断一个工作区文件是否符合某条声明。
def file_matches(declaration: object, relpath: str, path: Path) -> bool:
    if not any(path_matches(pattern, relpath) for pattern in declaration.path_patterns):
        return False
    return field_matches(path, declaration.field_match)


# LLM: 没声明字段匹配、或格式宿主不认识时只按路径认（返回 True）；json 读不了、不是对象、字段值不在声明取值里都返回 False。
# 函数用途: 按声明的结构化字段核对文件内容。
def field_matches(path: Path, field_match: object) -> bool:
    if field_match is None or field_match.format != FIELD_MATCH_FORMAT_JSON:
        return True
    document = _json_document(path)
    return isinstance(document, dict) and document.get(field_match.field) in field_match.equals


# LLM: 块 5 用来区分“写了但打不开”：声明了 json 字段匹配的要能在上限内解析成 JSON；其它格式（含没声明字段匹配）只要求是普通文件且能读。
# 函数用途: 判断一个文件按声明的格式能不能打开。
def file_readable(path: Path, field_match: object) -> bool:
    if field_match is not None and field_match.format == FIELD_MATCH_FORMAT_JSON:
        return _json_document(path) is not None
    try:
        with path.open("rb") as handle:
            handle.read(1)
    except OSError:
        return False
    return not path.is_symlink()


# 函数用途: 在字节上限内把文件解析成 JSON，失败返回 None。
def _json_document(path: Path) -> object:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FIELD_MATCH_FILE_BYTES:
            return None
        return json.loads(path.read_bytes())
    except (OSError, ValueError, RecursionError):
        return None


# LLM: 不在 root 之下（含经符号链接逃出）返回 None；返回值是 posix 相对路径。
# 函数用途: 把绝对路径换成工作区相对路径。
def workspace_relpath(path: Path, root: Path) -> str | None:
    try:
        return path.resolve(strict=False).relative_to(root.resolve(strict=False)).as_posix()
    except (OSError, ValueError):
        return None


# 函数用途: 计算一个普通文件的状态；不是普通文件或读不了返回 None，超过上限只记大小。
def file_state(path: Path) -> FileState | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        size = path.stat().st_size
        if size > MAX_SCAN_HASHED_FILE_BYTES:
            return FileState("", size)
        return FileState(hashlib.sha256(path.read_bytes()).hexdigest(), size)
    except OSError:
        return None


# LLM: 只记路径匹配任一模式的文件（不做字段比对，字段比对在用到时按当时内容做）；按排序后的顺序走访，结果可重复。
#   不跟随符号链接目录，跳过符号链接文件；走访条目或匹配文件超限即停，truncated=True。
# 函数用途: 对工作区做一次有界快照。
def scan_workspace(root: Path, patterns: tuple[str, ...]) -> WorkspaceScan:
    files: dict[str, FileState] = {}
    visited = 0
    for directory, dirnames, filenames in os.walk(root):
        dirnames.sort()
        visited += len(dirnames) + len(filenames)
        if visited > MAX_SCAN_VISITED_ENTRIES_COUNT or not _record_matches(Path(directory), sorted(filenames),
                                                                           (root, patterns), files):
            return WorkspaceScan(files, truncated=True)
    return WorkspaceScan(files)


# LLM: 只枚举本 run 的 written 路径，不重读全部基线文件或遍历工作区；基线摘要由调用方用于筛出变化。
#   当前状态沿用路径/普通文件/大小/匹配数上限；不跟随符号链接或越出工作区。收尾必须调用 scan_workspace。
# 函数用途: 只复查已知候选路径的当前状态，避免每次写后检查都遍历整个工作区。
def scan_workspace_candidates(root: Path, written: Iterable[str], patterns: tuple[str, ...]) -> WorkspaceScan:
    paths = sorted({path for path in written if isinstance(path, str)}, key=_walk_order_key)
    truncated = len(paths) > MAX_SCAN_VISITED_ENTRIES_COUNT
    files: dict[str, FileState] = {}
    for relpath in paths[:MAX_SCAN_VISITED_ENTRIES_COUNT]:
        path = _candidate_path(root, relpath)
        if path is None or not any(path_matches(pattern, relpath) for pattern in patterns):
            continue
        state = file_state(path)
        if state is None:
            continue
        if len(files) >= MAX_SCAN_MATCHED_FILES_COUNT:
            return WorkspaceScan(files, truncated=True)
        files[relpath] = state
    return WorkspaceScan(files, truncated=truncated)


# LLM: 与 scan_workspace 的排序规则对齐，按父目录段再按文件名排序；这里不访问文件系统。
# 函数用途: 按 os.walk 的稳定顺序排列已知路径，让候选上限优先保留旧扫描先访问的目录与文件。
def _walk_order_key(relpath: str) -> tuple[tuple[str, ...], str]:
    parts = relpath.split("/")
    return tuple(parts[:-1]), parts[-1]


# LLM: written 是宿主账本事实但仍要防损坏路径；resolve 后地址必须与账本相同，不能跟随符号链接改址。
# 函数用途: 校验账本路径仍是工作区内的相同普通相对地址，避免符号链接替换后读到其它位置。
def _candidate_path(root: Path, relpath: str) -> Path | None:
    parts = relpath.split("/")
    if not relpath or any(part in {"", ".", ".."} for part in parts):
        return None
    path = root.joinpath(*parts)
    return path if workspace_relpath(path, root) == relpath else None


# 函数用途: 记下一个目录里路径匹配的文件；匹配文件数超限时返回 False。
def _record_matches(directory: Path, filenames: list[str], scope: tuple[Path, tuple[str, ...]],
                    files: dict[str, FileState]) -> bool:
    root, patterns = scope
    for name in filenames:
        path = directory / name
        relpath = path.relative_to(root).as_posix()
        if not any(path_matches(pattern, relpath) for pattern in patterns):
            continue
        state = file_state(path)
        if state is None:
            continue
        if len(files) >= MAX_SCAN_MATCHED_FILES_COUNT:
            return False
        files[relpath] = state
    return True
