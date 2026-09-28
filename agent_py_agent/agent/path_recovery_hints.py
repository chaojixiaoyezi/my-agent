
from __future__ import annotations

import re
from pathlib import Path

_URL_PATTERN = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s\"'<>]*")
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")


def url_spans(text: str) -> list[tuple[int, int]]:
    return [(match.start(), match.end()) for match in _URL_PATTERN.finditer(text)]


def overlaps_spans(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < span_end and end > span_start for span_start, span_end in spans)


# LLM: 只在这个建议真的能纠正路径时才给：建议等于请求路径时它没有纠正价值，只会诱导调用方原地重试，
#   这种"建议"必须返回空，让上层按真实权限拒绝处理（2026-09-28 集成者裁定，对根内根外一视同仁）。
#   触发面很窄：**只针对"工作区根的位置写错"**——要求某个 workspace_root 的根名恰好出现在请求路径
#   中段（_tail_after_part 按 parts.index(root.name) 取尾段），重拼出的路径与原路径不同，才给建议。
#   它**不做邻近文件匹配**：请求的是某目录下拼错的文件名（如 reports/finl.md，旁边真有 final.md）时，
#   本函数只会把根名之后的尾段原样接到根上，得到的建议等于原路径 → 返回空，不会被当成拼写提示。
#   这一点是实测确认的（2026-09-28，见 TESTS.md 任务 2 一节），"按文件名找相近文件"属另一项未排期候选。
# 函数用途: 给出把写错前缀纠正到某个工作区根之后的路径；没有纠正价值时返回空。
def suggest_workspace_typo_target(raw: str, workspace_roots: Path | list[Path]) -> str:
    if _WINDOWS_ABSOLUTE_RE.match(raw):
        return ""
    candidate = Path(raw.replace("\\", "/")).expanduser()
    if not candidate.is_absolute():
        return ""
    for root in _workspace_roots(workspace_roots):
        suggested = _suggest_workspace_root_tail(root, candidate.parts)
        if suggested and Path(suggested) != candidate:
            return suggested
    return ""


def _suggest_workspace_root_tail(root: Path, candidate_parts: tuple[str, ...]) -> str:
    tail = _tail_after_part(candidate_parts, root.name)
    if not tail:
        return ""
    suggestion = root.joinpath(*tail).resolve(strict=False)
    return str(suggestion) if _is_relative_to(suggestion, root) else ""


def _tail_after_part(parts: tuple[str, ...], marker: str) -> tuple[str, ...]:
    if not marker:
        return ()
    try:
        index = parts.index(marker)
    except ValueError:
        return ()
    return parts[index + 1 :]


def _workspace_roots(workspace_roots: Path | list[Path]) -> list[Path]:
    raw_roots = workspace_roots if isinstance(workspace_roots, list) else [workspace_roots]
    return [Path(root).resolve(strict=False) for root in raw_roots]


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
