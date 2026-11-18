# LLM: 携卡回合的产物与附件路径判定集中于单一纯函数，不读文件内容、不维护业务词白名单。
# 模块用途: 统一校验调试/凭据导出文件名（扩展名 + 词元边界）与相对路径组件的凭据回显。
from __future__ import annotations

import os
import re
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

# 携卡回合禁止归档或外发的调试抓包、凭据材料、数据库及二进制文件扩展名集合。
_BLOCKED_CREDENTIAL_ARTIFACT_EXTS = frozenset({
    ".tmp",
    ".bak",
    ".swp",
    ".har",
    ".pcap",
    ".pcapng",
    ".cookie",
    ".cookies",
    ".jar",
    ".dump",
    ".pem",
    ".key",
    ".pfx",
    ".p12",
    ".netrc",
    ".session",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".bmp",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".tgz",
    ".bz2",
    ".xz",
    ".bin",
    ".sqlite",
    ".sqlite3",
    ".db",
    ".pkl",
    ".pickle",
    ".npy",
    ".npz",
    ".parquet",
})

# 携卡回合按非字母数字边界切分后拦截的独立调试与凭据文件名词元（避免将 chart/share 误判为 har）。
_BLOCKED_CREDENTIAL_STEM_TOKENS = frozenset({
    "har",
    "cookie",
    "cookies",
    "cookiejar",
    "debug",
    "trace",
    "traces",
    "dump",
    "dumps",
    "storagestate",
    "cred",
    "creds",
    "credential",
    "credentials",
    "secret",
    "secrets",
    "token",
    "tokens",
    "session",
    "sessions",
    "headers",
    "netrc",
    "keychain",
})

# 携卡回合按相邻双词元组合拦截的复合调试导出标记。
_BLOCKED_CREDENTIAL_STEM_BIGRAMS = frozenset({
    "storage_state",
    "auth_dump",
    "trace_dump",
    "cookie_jar",
    "header_dump",
    "session_state",
})

# 用于按词元边界切分文件名主干，不匹配任意内部子串。
_NON_ALNUM_TOKEN_RE = re.compile(r"[^a-z0-9]+")


def _split_path_components(parts_or_name: str | os.PathLike[str] | Sequence[str]) -> tuple[str, ...]:
    """将文件名、路径或路径片段序列拆分为非空相对组件元组。"""
    if isinstance(parts_or_name, (str, os.PathLike)):
        raw_items: Sequence[str] = (os.fspath(parts_or_name),)
    else:
        raw_items = tuple(str(item) for item in parts_or_name if item is not None)
    segments: list[str] = []
    for item in raw_items:
        for seg in item.replace("\\", "/").split("/"):
            if seg and seg != ".":
                segments.append(seg)
    return tuple(segments)


def is_blocked_credential_debug_path(
    parts_or_name: str | os.PathLike[str] | Sequence[str],
) -> bool:
    """按点前缀、扩展名与词元边界判断相对路径或展示文件名是否属于受阻断的调试/凭据产物。"""
    parts = _split_path_components(parts_or_name)
    if not parts:
        return False
    last_idx = len(parts) - 1
    for idx, comp in enumerate(parts):
        if comp.startswith("."):
            return True
        lowered = comp.lower()
        posix_part = PurePosixPath(lowered)
        if any(ext in _BLOCKED_CREDENTIAL_ARTIFACT_EXTS for ext in posix_part.suffixes):
            return True
        stem_text = posix_part.stem if idx == last_idx else lowered
        tokens = tuple(tok for tok in _NON_ALNUM_TOKEN_RE.split(stem_text) if tok)
        if any(tok in _BLOCKED_CREDENTIAL_STEM_TOKENS for tok in tokens):
            return True
        if any(
            f"{tokens[pos]}_{tokens[pos + 1]}" in _BLOCKED_CREDENTIAL_STEM_BIGRAMS
            for pos in range(len(tokens) - 1)
        ):
            return True
    return False


def relative_artifact_path_parts(
    path_value: str | os.PathLike[str],
    *,
    base_root: str | os.PathLike[str] = "",
) -> tuple[str, ...]:
    """提取产物相对于宿主隔离根的各层路径组件，供凭据回显与路径门核验。"""
    raw_text = str(os.fspath(path_value) if isinstance(path_value, os.PathLike) else path_value or "").strip()
    if not raw_text:
        return ()
    candidate = Path(raw_text)
    root_text = str(os.fspath(base_root) if isinstance(base_root, os.PathLike) else base_root or "").strip()
    if root_text:
        root_path = Path(root_text)
        for cand_variant, root_variant in (
            (candidate, root_path),
            (candidate.resolve(strict=False), root_path.resolve(strict=False)),
        ):
            try:
                rel = cand_variant.relative_to(root_variant)
                return _split_path_components(rel.parts)
            except ValueError:
                continue
    return _split_path_components(candidate.parts)


def user_artifact_subpath_parts(rel_parts: Sequence[str]) -> tuple[str, ...]:
    """剥离宿主固定容器前缀，保留任务/归档子目录内由脚本或工具生成的相对路径组件。"""
    parts = tuple(str(p) for p in rel_parts if p)
    if not parts:
        return ()
    dot_parts = tuple(p for p in parts[:-1] if p.startswith("."))
    if (
        len(parts) > 6
        and parts[0] == "data"
        and parts[1] == "workflow_runs"
        and parts[3] == "artifacts"
    ):
        return parts[6:]
    if len(parts) > 3 and parts[0] == "tasks" and parts[2] in {"output", "work"}:
        return (*dot_parts, *parts[3:])
    if len(parts) > 2 and parts[0] in {"tasks", "sessions"}:
        return (*dot_parts, *parts[2:])
    return (*dot_parts, parts[-1])


def path_contains_credential_echo(
    parts_or_name: str | os.PathLike[str] | Sequence[str],
    redactions: Sequence[object],
) -> bool:
    """检查相对路径的所有目录段、叶文件名及组合路径是否命中当前回合已登记的凭据回显变体。"""
    if not redactions:
        return False
    parts = _split_path_components(parts_or_name)
    if not parts:
        return False
    joined = "/".join(parts)
    for redactor in redactions:
        checker = getattr(redactor, "contains_credential_echo", None)
        if not callable(checker):
            continue
        if any(bool(checker(part)) for part in parts) or bool(checker(joined)):
            return True
    return False


__all__ = [
    "is_blocked_credential_debug_path",
    "path_contains_credential_echo",
    "relative_artifact_path_parts",
    "user_artifact_subpath_parts",
]
