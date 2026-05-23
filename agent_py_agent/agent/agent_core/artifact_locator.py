# LLM: Artifact locator resolves machine-declared deliverables without task-specific templates.
# 模块用途: 当合同只声明产物类型和允许输出根目录时，在工作区内定位唯一可验收产物。

from __future__ import annotations

import mimetypes
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_KIND_EXTENSIONS = {
    "csv": (".csv",),
    "docx": (".docx",),
    "html": (".html", ".htm"),
    "htm": (".html", ".htm"),
    "json": (".json",),
    "markdown": (".md", ".markdown"),
    "md": (".md", ".markdown"),
    "pdf": (".pdf",),
    "txt": (".txt",),
    "xlsx": (".xlsx",),
    "zip": (".zip",),
}
_DEFAULT_SEARCH_ROOTS = ("outputs", "artifacts")
_MAX_CANDIDATES = 128
_SAFE_EXTENSION_RE = re.compile(r"^[a-z0-9][a-z0-9._+-]{0,63}$")


# LLM: ArtifactLocatorResult is the structured output for locator gates.
# 类用途: 返回定位到的路径、机器 finding 和候选引用，供 closeout/repair 使用。
@dataclass(frozen=True)
class ArtifactLocatorResult:
    path: Path | None
    findings: list[dict[str, object]] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)


# LLM: locate_artifact uses only structured artifact fields and filesystem facts.
# 函数用途: 按 path/preferred_path 或 kind+allowed_output_roots 定位产物；不解析用户 prompt。
def locate_artifact(item: dict[str, Any], workspace_root: Path) -> ArtifactLocatorResult:
    workspace = Path(workspace_root).resolve(strict=False)
    if raw_path := _artifact_target_path(item):
        path = _bounded_path(raw_path, workspace)
        if path is None:
            return ArtifactLocatorResult(None, [_finding("ARTIFACT_PATH_INVALID", raw_path)])
        return ArtifactLocatorResult(path)
    kind = str(item.get("kind") or "").strip().lower()
    extensions = _extensions_for_item(item)
    if not extensions:
        return ArtifactLocatorResult(None, [_finding("ARTIFACT_LOCATOR_EXTENSION_MISSING", kind)])
    candidates = _candidate_paths(item, workspace, extensions)
    if not candidates:
        return ArtifactLocatorResult(None, [_finding("ARTIFACT_LOCATOR_NO_MATCH", kind)])
    selected = _select_by_structured_hint(item, candidates)
    if selected is not None:
        return ArtifactLocatorResult(selected, candidates=[_portable_ref(path, workspace) for path in candidates])
    if len(candidates) == 1:
        return ArtifactLocatorResult(candidates[0], candidates=[_portable_ref(candidates[0], workspace)])
    return ArtifactLocatorResult(
        None,
        [
            _finding(
                "ARTIFACT_LOCATOR_AMBIGUOUS",
                kind,
                evidence={"candidates": [_portable_ref(path, workspace) for path in candidates[:10]]},
            )
        ],
        candidates=[_portable_ref(path, workspace) for path in candidates],
    )


# LLM: artifact_can_be_located reports whether an artifact has enough structured target information.
# 函数用途: 支持合同 preflight，允许无固定路径但有 kind+allowed_output_roots 的通用产物。
def artifact_can_be_located(item: dict[str, Any]) -> bool:
    if _artifact_target_path(item):
        return True
    return bool(_extensions_for_item(item)) and bool(_search_roots(item))


# LLM: _artifact_target_path reads explicit machine path fields only.
# 函数用途: 从 artifact 合同中提取 path/preferred_path，不从说明文字猜路径。
def _artifact_target_path(item: dict[str, Any]) -> str:
    return str(item.get("preferred_path") or item.get("path") or "").strip()


# LLM: _candidate_paths scans bounded output roots for files matching the artifact kind.
# 函数用途: 收集候选产物并按修改时间排序，避免 closeout 因未声明固定路径直接中断。
def _candidate_paths(item: dict[str, Any], workspace: Path, extensions: tuple[str, ...]) -> list[Path]:
    candidates: list[Path] = []
    for root in _search_roots(item):
        root_path = _bounded_path(root, workspace)
        candidates.extend(_candidate_paths_under_root(root_path, extensions, remaining=_MAX_CANDIDATES - len(candidates)))
        if len(candidates) >= _MAX_CANDIDATES:
            break
    candidates.sort(key=lambda path: path.stat().st_mtime if path.exists() else 0.0, reverse=True)
    return candidates


# LLM: _candidate_paths_under_root keeps filesystem walking separate from root selection.
# 函数用途: 在一个已边界检查的目录下查找匹配扩展名的候选产物。
def _candidate_paths_under_root(root_path: Path | None, extensions: tuple[str, ...], *, remaining: int) -> list[Path]:
    if remaining <= 0 or root_path is None or not root_path.exists() or not root_path.is_dir():
        return []
    candidates: list[Path] = []
    for path in root_path.rglob("*"):
        if len(candidates) >= remaining:
            break
        if path.is_file() and path.suffix.lower() in extensions:
            candidates.append(path.resolve(strict=False))
    return candidates


# LLM: _extensions_for_item keeps artifact types open-world.
# 函数用途: 已知 kind 走映射优化，未知 kind/mime 可由合同扩展名或 key 本身推导。
def _extensions_for_item(item: dict[str, Any]) -> tuple[str, ...]:
    explicit = _explicit_extensions(item)
    if explicit:
        return explicit
    kind = str(item.get("kind") or "").strip().lower()
    mime_type = str(item.get("mime_type") or item.get("content_type") or "").strip().lower()
    if kind in _KIND_EXTENSIONS:
        return _KIND_EXTENSIONS[kind]
    return _extensions_from_open_key(kind) or _extensions_from_mime_type(mime_type or kind)


# LLM: _explicit_extensions reads caller-declared format overrides.
# 函数用途: 允许合同/配置声明新格式扩展名，避免写死类型表成为唯一判定路径。
def _explicit_extensions(item: dict[str, Any]) -> tuple[str, ...]:
    values: list[object] = []
    for holder in _extension_holders(item):
        values.extend(_extension_values(holder))
    return tuple(dict.fromkeys(_normalized_extension(value) for value in values if _normalized_extension(value)))


# LLM: _extension_holders returns structured objects that may declare extension overrides.
# 函数用途: 从 artifact 本身和 validation_contract 收集扩展名字段，不读取普通说明文字。
def _extension_holders(item: dict[str, Any]) -> list[dict[str, Any]]:
    holders = [item]
    validation = item.get("validation_contract")
    if isinstance(validation, dict):
        holders.append(validation)
    return holders


# LLM: _extension_values reads every supported explicit extension key.
# 函数用途: 支持 extension/extensions/file_extension/file_extensions 这些结构化覆盖字段。
def _extension_values(holder: dict[str, Any]) -> list[object]:
    return [
        value
        for key in ("extension", "extensions", "file_extension", "file_extensions")
        for value in _extension_value_items(holder.get(key))
    ]


# LLM: _extension_value_items flattens one explicit extension field.
# 函数用途: 把单值或列表值统一成列表，便于后续开放格式扩展名归一。
def _extension_value_items(value: object) -> list[object]:
    if isinstance(value, list):
        return list(value)
    return [value] if value else []


# LLM: _extensions_from_open_key derives a default extension from an unknown artifact kind.
# 函数用途: 让 parquet、svg、proto 等新类型不用先改代码枚举也能定位产物。
def _extensions_from_open_key(key: str) -> tuple[str, ...]:
    if not key or "/" in key:
        return ()
    extension = _normalized_extension(key)
    return (extension,) if extension else ()


# LLM: _extensions_from_mime_type accepts both stdlib guesses and subtype fallback.
# 函数用途: 对 MIME type 先用 mimetypes 优化，未知 MIME 再从 subtype 推导扩展名。
def _extensions_from_mime_type(mime_type: str) -> tuple[str, ...]:
    if "/" not in mime_type:
        return ()
    guessed = tuple(
        dict.fromkeys(
            value
            for value in mimetypes.guess_all_extensions(mime_type, strict=False)
            if _normalized_extension(value)
        )
    )
    if guessed:
        return guessed
    subtype = mime_type.rsplit("/", 1)[-1].removeprefix("x-").split("+", 1)[0]
    extension = _normalized_extension(subtype)
    return (extension,) if extension else ()


# LLM: _normalized_extension sanitizes open-world extension declarations.
# 函数用途: 把合同或 key 声明规整成 .ext 形式，并拒绝路径/空白/过长值。
def _normalized_extension(value: object) -> str:
    text = str(value or "").strip().lower().lstrip(".")
    if "/" in text or "\\" in text or not _SAFE_EXTENSION_RE.fullmatch(text):
        return ""
    return f".{text}"


# LLM: _search_roots reads only structured root fields.
# 函数用途: 从 allowed_output_roots/search_roots/artifact_roots 取搜索根，缺省走通用输出目录。
def _search_roots(item: dict[str, Any]) -> list[str]:
    raw = item.get("allowed_output_roots") or item.get("search_roots") or item.get("artifact_roots")
    if isinstance(raw, list):
        roots = [str(value).strip() for value in raw if str(value).strip()]
        if roots:
            return roots
    return list(_DEFAULT_SEARCH_ROOTS)


# LLM: _select_by_structured_hint disambiguates only by artifact_id/name fields.
# 函数用途: 在多个候选产物中用机器字段提示挑唯一匹配，绝不解析用户 prompt。
def _select_by_structured_hint(item: dict[str, Any], candidates: list[Path]) -> Path | None:
    hints = [
        str(item.get("artifact_id") or "").strip().lower().replace("-", "_"),
        str(item.get("name") or "").strip().lower().replace("-", "_"),
    ]
    hints = [hint for hint in hints if hint]
    matches = [
        path
        for path in candidates
        for stem in [path.stem.lower().replace("-", "_")]
        if any(hint in stem for hint in hints)
    ]
    return matches[0] if len(matches) == 1 else None


# LLM: _bounded_path is the workspace boundary for artifact locator reads.
# 函数用途: 把相对/绝对路径规整到 workspace 内，越界时返回 None。
def _bounded_path(raw_path: str, workspace: Path) -> Path | None:
    candidate = Path(raw_path).expanduser()
    path = candidate.resolve(strict=False) if candidate.is_absolute() else (workspace / candidate).resolve(strict=False)
    try:
        path.relative_to(workspace)
    except ValueError:
        return None
    return path


# LLM: _finding emits stable machine findings for locator failures.
# 函数用途: 构造 code/severity/evidence 结构，供 closeout 和返工循环读取。
def _finding(code: str, value: str, *, evidence: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "code": code,
        "severity": "hard",
        "message": code.lower(),
        "value": value,
        **({"evidence": evidence} if evidence else {}),
    }


# LLM: _portable_ref makes candidate refs stable across machines.
# 函数用途: 优先输出 workspace 相对路径，越界候选才退回原路径字符串。
def _portable_ref(path: Path, workspace: Path) -> str:
    try:
        return str(path.relative_to(workspace)).replace("\\", "/")
    except ValueError:
        return str(path)


__all__ = ["ArtifactLocatorResult", "artifact_can_be_located", "locate_artifact"]
