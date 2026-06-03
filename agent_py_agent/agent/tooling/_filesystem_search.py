
from __future__ import annotations

import fnmatch
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ._filesystem_helpers import (
    _MAX_SEARCH_LINE_CHARS,
    _read_text_safe,
)
from ._filesystem_read import (
    _COMMON_FILE_DISCOVERY_IGNORES,
    FileSystemAccessOptions,
    FileSystemTool,
)
from ._filesystem_search_models import (
    SearchHit,
    SearchMatch,
    SearchMatcher,
    SearchRequest,
    path_has_ignored_part,
    render_files_with_matches,
    render_match_counts,
    rg_args,
    rg_text,
    search_page_notice,
    search_request_from_params,
    slice_hits,
)
from ._filesystem_search_spec import build_search_text_spec
from .filesystem_path_recovery import MissingPathRequest, missing_path_result
from .models import ToolExecutionResult


class SearchTextTool(FileSystemTool):

    def __init__(
        self,
        workspace_root: Path,
        max_matches: int,
        workspace_roots: list[Path] | None = None,
        access_options: FileSystemAccessOptions | None = None,
    ):
        super().__init__(
            workspace_root,
            workspace_roots,
            access_options,
        )
        self.max_matches = max_matches
        self.spec = build_search_text_spec()

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        try:
            request = search_request_from_params(params, self.max_matches)
            target = self.resolve_path(request.raw_path)
        except ValueError as exc:
            return ToolExecutionResult("search_text", False, str(exc))
        if not target.exists():
            return missing_path_result(MissingPathRequest(
                tool_name="search_text",
                raw_path=request.raw_path,
                target=target,
                workspace_roots=self.workspace_roots,
                display_path=self.display_path(target),
                expected_kind="any",
                retry_tool="search_text",
            ))
        return self._search_target(target, request)

    def _search_target(self, target: Path, request: SearchRequest) -> ToolExecutionResult:
        try:
            matcher = SearchMatcher.from_request(request)
        except ValueError as exc:
            return ToolExecutionResult("search_text", False, str(exc))
        hits = self._collect_hits_with_rg(target, request)
        if hits is None:
            hits = self._collect_hits(target, request, matcher)
        if request.output_mode == "files_with_matches":
            return ToolExecutionResult("search_text", True, render_files_with_matches(hits, request))
        if request.output_mode == "count":
            return ToolExecutionResult("search_text", True, render_match_counts(hits, request))
        return ToolExecutionResult("search_text", True, self._render_content_hits(hits, request))

    def _collect_hits_with_rg(self, target: Path, request: SearchRequest) -> list[SearchHit] | None:
        rg_path = shutil.which("rg")
        if not rg_path:
            return None
        args = rg_args(rg_path, target, request)
        try:
            result = subprocess.run(
                args,
                cwd=str(self.workspace_root),
                capture_output=True,
                text=True,
                check=False,
            )
        except Exception:
            return None
        if result.returncode == 1:
            return []
        if result.returncode != 0:
            return None
        return self._parse_rg_json_lines(result.stdout)

    def _parse_rg_json_lines(self, stdout: str) -> list[SearchHit]:
        hits: list[SearchHit] = []
        for raw_line in stdout.splitlines():
            if not raw_line.strip():
                continue
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                return []
            if event.get("type") != "match":
                continue
            hit = self._search_hit_from_rg_event(event)
            if hit is not None:
                hits.append(hit)
        return hits

    def _search_hit_from_rg_event(self, event: dict[str, Any]) -> SearchHit | None:
        data = event.get("data")
        if not isinstance(data, dict):
            return None
        path_text = rg_text(data.get("path"))
        line_text = rg_text(data.get("lines")).rstrip("\n")
        line_number = data.get("line_number")
        if not path_text or not isinstance(line_number, int):
            return None
        raw_path = Path(path_text)
        if not raw_path.is_absolute():
            raw_path = self.workspace_root / raw_path
        if raw_path.is_symlink():
            return None
        try:
            safe_item = self.resolve_path(raw_path)
        except ValueError:
            return None
        text = _read_text_safe(safe_item)
        lines = text.splitlines() if text is not None else [line_text]
        return SearchHit(
            rel=_item_relative_path(self, raw_path, safe_item),
            line_number=line_number,
            line=line_text,
            lines=lines,
        )

    def _iter_search_candidates(self, target: Path, request: SearchRequest) -> list[Path]:
        if target.is_file():
            return [target]
        candidates: list[Path] = []
        for root, dirnames, filenames in os.walk(target):
            dirnames.sort()
            filenames.sort()
            self._filter_search_dirs(dirnames, request)
            items = [Path(root) / filename for filename in filenames]
            items = [item for item in items if not item.is_symlink()]
            if not request.include_ignored:
                items = [item for item in items if not path_has_ignored_part(item, target)]
            candidates.extend(items)
        return candidates

    def _filter_search_dirs(self, dirnames: list[str], request: SearchRequest) -> None:
        if not request.include_ignored:
            dirnames[:] = [name for name in dirnames if name not in _COMMON_FILE_DISCOVERY_IGNORES]

    def _collect_hits(self, target: Path, request: SearchRequest, matcher: SearchMatcher) -> list[SearchHit]:
        hits: list[SearchHit] = []
        for item in self._iter_search_candidates(target, request):
            if request.file_glob and not self._matches_file_glob(item, request.file_glob):
                continue
            hits.extend(self._search_item_hits(item, request, matcher))
        return hits

    def _search_item_hits(
        self,
        item: Path,
        request: SearchRequest,
        matcher: SearchMatcher,
    ) -> list[SearchHit]:
        try:
            safe_item = self.resolve_path(item)
        except ValueError:
            return []
        text = _read_text_safe(safe_item)
        if text is None:
            return []
        rel = _item_relative_path(self, item, safe_item)
        lines = text.splitlines()
        return [
            SearchHit(rel=rel, line_number=idx, line=line, lines=lines)
            for idx, line in enumerate(lines, start=1)
            if matcher.matches(line)
        ]

    def _render_content_hits(self, hits: list[SearchHit], request: SearchRequest) -> str:
        matches: list[str] = []
        for hit in slice_hits(hits, request):
            _append_search_match(
                SearchMatch(
                    rel=hit.rel,
                    line_number=hit.line_number,
                    line=hit.line,
                    lines=hit.lines,
                    context=request.context,
                ),
                matches,
            )
        if len(hits) > request.offset + request.limit:
            matches.append(search_page_notice(request.offset + request.limit, request.limit))
        return "\n".join(matches) or "没有找到匹配项"

    def _matches_file_glob(self, item: Path, file_glob: str) -> bool:
        display = self.display_path(item)
        return fnmatch.fnmatch(item.name, file_glob) or fnmatch.fnmatch(display, file_glob)


def _append_search_match(match: SearchMatch, matches: list[str]) -> None:
    snippet = _make_snippet(match.line)
    matches.append(f"{match.rel}:{match.line_number}: {snippet}")
    if not match.lines or match.context <= 0:
        return
    start = max(1, match.line_number - match.context)
    end = min(len(match.lines), match.line_number + match.context)
    for idx in range(start, end + 1):
        if idx == match.line_number:
            continue
        matches.append(f"{match.rel}:{idx}: {_make_snippet(match.lines[idx - 1])}")


def _make_snippet(line: str) -> str:
    snippet = line.strip()
    if len(snippet) > _MAX_SEARCH_LINE_CHARS:
        snippet = snippet[:_MAX_SEARCH_LINE_CHARS] + "... 已截断"
    return snippet


def _item_relative_path(tool: SearchTextTool, item: Path, safe_item: Path) -> str:
    return tool.display_path(safe_item if safe_item.is_absolute() else item)
