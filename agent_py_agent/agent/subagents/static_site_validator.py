# LLM: Static site validator gives parent acceptance a deterministic check for generated web artifacts.
# 模块用途: 检查静态站点目录的必需文件、本地链接/资源和模板占位符，不执行 JS、不访问网络。

from __future__ import annotations

"""Bounded static-site validation for parent acceptance tests."""

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .execution_executor_helpers import _test_name, _utc_now_iso
from .execution_records import TestExecutionRecord

_REMOTE_SCHEMES = {"http", "https", "mailto", "tel", "data", "javascript"}


# LLM: StaticSiteCheckResult is the compact facts payload persisted in test_execution.json.
# 类用途: 保存静态站点验收的机器事实；只记录问题摘要和路径，不内联文件正文。
@dataclass
class StaticSiteCheckResult:
    """Collect static-site validation facts."""

    checked_root: str
    checked_files: list[str] = field(default_factory=list)
    missing_required_files: list[str] = field(default_factory=list)
    placeholder_hits: list[str] = field(default_factory=list)
    broken_local_refs: list[str] = field(default_factory=list)
    inert_control_hits: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # LLM: ok is the single pass/fail boolean consumed by parent acceptance.
    # 函数用途: 判断静态站点检查是否通过；任何缺文件、坏链接、占位符或失效控件都会失败。
    @property
    def ok(self) -> bool:
        return not (
            self.missing_required_files
            or self.placeholder_hits
            or self.broken_local_refs
            or self.inert_control_hits
        )

    # LLM: to_dict keeps result serialization stable and bounded for reports.
    # 函数用途: 转成 JSON 友好的验证结果；后续字段扩展应保持 refs-only 摘要。
    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked_root": self.checked_root,
            "checked_files": self.checked_files,
            "missing_required_files": self.missing_required_files,
            "placeholder_hits": self.placeholder_hits,
            "broken_local_refs": self.broken_local_refs,
            "inert_control_hits": self.inert_control_hits,
            "warnings": self.warnings,
        }


# LLM: StaticSiteHTMLParser extracts only refs and simple controls from one HTML file.
# 类用途: 使用标准库 HTMLParser 扫描 href/src/action 和按钮/链接控件，不执行网页代码。
class StaticSiteHTMLParser(HTMLParser):
    """Extract local refs and basic controls from HTML."""

    # LLM: __init__ stores per-file scanner state and avoids retaining large body text.
    # 函数用途: 初始化单个 HTML 文件的扫描容器；只保留 refs 和控件摘要。
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.refs: list[tuple[str, str]] = []
        self.controls: list[dict[str, object]] = []
        self._current_control: dict[str, object] | None = None

    # LLM: handle_starttag records href/src/action refs and simple clickable controls.
    # 函数用途: 收集本地资源引用和可点击控件，后续判断是否失效或明显无动作。
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key.lower(): value or "" for key, value in attrs}
        for ref_attr in ("href", "src", "action"):
            if attr_map.get(ref_attr):
                self.refs.append((ref_attr, attr_map[ref_attr]))
        if tag in {"button", "a"}:
            self._current_control = {
                "tag": tag,
                "href": attr_map.get("href", ""),
                "onclick": attr_map.get("onclick", ""),
                "type": attr_map.get("type", ""),
                "text": "",
            }

    # LLM: handle_data keeps only short button/link text for diagnostics.
    # 函数用途: 给控件问题生成可读摘要；不会保存完整页面正文。
    def handle_data(self, data: str) -> None:
        if self._current_control is not None:
            text = str(self._current_control.get("text") or "")
            self._current_control["text"] = (text + data).strip()[:80]

    # LLM: handle_endtag closes one simple control candidate.
    # 函数用途: 结束 button/a 扫描并把控件摘要放入列表。
    def handle_endtag(self, tag: str) -> None:
        if self._current_control and self._current_control.get("tag") == tag:
            self.controls.append(dict(self._current_control))
            self._current_control = None


# LLM: run_static_site_check is the public validation entrypoint used by TestExecutor.
# 函数用途: 检查 workspace 内静态站点目录，并返回父级验收能直接读取的执行记录。
def run_static_site_check(test: dict[str, Any], workspace_root: Path) -> TestExecutionRecord:
    site_root, error = _resolve_site_root(test, workspace_root)
    if error:
        return _static_site_record(test, StaticSiteCheckResult(str(site_root)), executed=False, error=error)
    result = _scan_site(test, site_root)
    error_text = "" if result.ok else _failure_summary(result)
    return _static_site_record(test, result, executed=True, error=error_text)


# LLM: _resolve_site_root enforces workspace bounds before any directory walk.
# 函数用途: 解析 site_root/root_dir/file_path，保证静态检查不能扫描工作区外部。
def _resolve_site_root(test: dict[str, Any], workspace_root: Path) -> tuple[Path, str]:
    raw = str(test.get("site_root") or test.get("root_dir") or test.get("file_path") or ".").strip()
    candidate = Path(raw).expanduser()
    path = candidate.resolve() if candidate.is_absolute() else (workspace_root / candidate).resolve()
    try:
        path.relative_to(workspace_root)
    except ValueError:
        return path, "site_root 超出 workspace 边界"
    if not path.exists() or not path.is_dir():
        return path, "site_root 不存在或不是目录"
    return path, ""


# LLM: _scan_site performs bounded HTML checks without executing JavaScript or fetching remote assets.
# 函数用途: 读取站点 HTML 文件，检查必需文件、`${` 占位符、本地链接和明显无动作控件。
def _scan_site(test: dict[str, Any], site_root: Path) -> StaticSiteCheckResult:
    result = StaticSiteCheckResult(checked_root=str(site_root))
    _check_required_files(result, test, site_root)
    html_files = _html_files(site_root, int(test.get("max_files") or 200))
    result.checked_files = [_rel(path, site_root) for path in html_files]
    check_refs = test.get("check_local_refs", True) is not False
    check_placeholders = test.get("forbid_placeholders", True) is not False
    check_controls = test.get("check_inert_controls", True) is not False
    for path in html_files:
        text = path.read_text(encoding="utf-8", errors="replace")
        if check_placeholders and _has_visible_template_placeholder(text):
            result.placeholder_hits.append(_rel(path, site_root))
        parser = StaticSiteHTMLParser()
        parser.feed(text)
        if check_refs:
            result.broken_local_refs.extend(_broken_refs(parser.refs, path, site_root))
        if check_controls:
            result.inert_control_hits.extend(_inert_controls(parser.controls, path, text, site_root))
    return result


# LLM: _check_required_files records missing pages/assets without opening arbitrary paths.
# 函数用途: 检查调用方声明的 required_files；相对路径必须仍在站点根目录内。
def _check_required_files(result: StaticSiteCheckResult, test: dict[str, Any], site_root: Path) -> None:
    for item in _string_list(test.get("required_files")):
        path = (site_root / item).resolve()
        if not _inside(path, site_root) or not path.exists() or not path.is_file():
            result.missing_required_files.append(item)


# LLM: _html_files limits directory scanning so large workspaces cannot explode validation cost.
# 函数用途: 返回站点根下有限数量的 HTML 文件；按路径排序保证报告稳定。
def _html_files(site_root: Path, max_files: int) -> list[Path]:
    limit = max(1, min(max_files, 500))
    return sorted(path for path in site_root.rglob("*.html") if path.is_file())[:limit]


# LLM: _broken_refs checks only local href/src/action targets and ignores remote URLs.
# 函数用途: 找出失效的本地页面、图片、脚本和表单 action，不访问网络。
def _broken_refs(refs: list[tuple[str, str]], html_file: Path, site_root: Path) -> list[str]:
    broken: list[str] = []
    for attr, ref in refs:
        target = _local_ref_target(ref, html_file, site_root)
        if target is None:
            continue
        if not target.exists():
            broken.append(f"{_rel(html_file, site_root)}:{attr}={ref}")
    return broken


# LLM: _local_ref_target resolves a local URL-like ref into a filesystem path when safe.
# 函数用途: 把相对/根相对链接转换为站点内路径；远程、锚点和越界引用返回 None。
def _local_ref_target(ref: str, html_file: Path, site_root: Path) -> Path | None:
    cleaned = ref.strip()
    if not cleaned or cleaned.startswith("#") or cleaned.startswith("//"):
        return None
    parsed = urlsplit(cleaned)
    if parsed.scheme.lower() in _REMOTE_SCHEMES:
        return None
    path_part = parsed.path.strip()
    if not path_part:
        return None
    candidate = (site_root / path_part.lstrip("/")).resolve() if path_part.startswith("/") else (html_file.parent / path_part).resolve()
    if candidate.is_dir():
        candidate = candidate / "index.html"
    return candidate if _inside(candidate, site_root) else None


# LLM: _inert_controls finds obvious clickable controls with no target or event handler.
# 函数用途: 标记明显失效的 button/a；表单 submit/reset 和带全局 addEventListener 的页面会跳过误报。
def _inert_controls(
    controls: list[dict[str, object]],
    html_file: Path,
    html_text: str,
    site_root: Path,
) -> list[str]:
    hits: list[str] = []
    has_script_handlers = "addEventListener" in html_text
    for control in controls:
        tag = str(control.get("tag") or "")
        text = str(control.get("text") or "").strip()
        href = str(control.get("href") or "").strip()
        onclick = str(control.get("onclick") or "").strip()
        button_type = str(control.get("type") or "").strip().lower()
        if tag == "a" and not href and not onclick:
            hits.append(f"{_rel(html_file, site_root)}:a:{text or '<empty>'}")
        if tag == "button" and not onclick and button_type not in {"submit", "reset"} and not has_script_handlers:
            hits.append(f"{_rel(html_file, site_root)}:button:{text or '<empty>'}")
    return hits


# LLM: _static_site_record converts validator facts into the common TestExecutionRecord contract.
# 函数用途: 构造 static_site_check 执行记录；失败原因放摘要，详细列表放 validation_result。
def _static_site_record(
    test: dict[str, Any],
    result: StaticSiteCheckResult,
    *,
    executed: bool,
    error: str,
) -> TestExecutionRecord:
    return TestExecutionRecord(
        test_name=_test_name(test),
        executed=executed,
        exit_code=0 if result.ok and executed else 1,
        executed_at=_utc_now_iso(),
        error=error,
        validation_method="static_site_check",
        validation_result=result.to_dict(),
    )


# LLM: _failure_summary keeps the top-level error concise while detailed facts stay in validation_result.
# 函数用途: 生成父级看板易读的静态站点失败摘要。
def _failure_summary(result: StaticSiteCheckResult) -> str:
    parts: list[str] = []
    if result.missing_required_files:
        parts.append(f"missing_required_files={len(result.missing_required_files)}")
    if result.placeholder_hits:
        parts.append(f"placeholder_hits={len(result.placeholder_hits)}")
    if result.broken_local_refs:
        parts.append(f"broken_local_refs={len(result.broken_local_refs)}")
    if result.inert_control_hits:
        parts.append(f"inert_control_hits={len(result.inert_control_hits)}")
    return "; ".join(parts)


# LLM: _has_visible_template_placeholder separates real leftover HTML placeholders from JS template literals.
# 函数用途: 检查页面可见/标记区域是否残留 `${...}`；会先剔除 script/style，避免误伤正常 JavaScript 模板字符串。
def _has_visible_template_placeholder(text: str) -> bool:
    visible_text = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", "", text, flags=re.IGNORECASE | re.DOTALL)
    return "${" in visible_text


# LLM: _string_list accepts runner JSON shapes without trusting non-string objects.
# 函数用途: 把 required_files 等字段规整成短字符串列表。
def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


# LLM: _inside centralizes path containment checks for refs and required files.
# 函数用途: 判断解析后的路径是否仍在站点根目录内。
def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


# LLM: _rel renders stable site-relative paths in reports.
# 函数用途: 把绝对路径压成站点内相对路径，避免报告噪音和隐私泄漏。
def _rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)
