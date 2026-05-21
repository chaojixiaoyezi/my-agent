# LLM: Static site validator gives parent acceptance a deterministic check for generated web artifacts.
# 模块用途: 检查静态站点目录的必需文件、本地链接/资源和模板占位符，不执行 JS、不访问网络。
# 2026-05-18: strict_dom_bindings checks unguarded JS id lookups; required_dom_ids declares mandatory DOM.

from __future__ import annotations

"""Bounded static-site validation for parent acceptance tests."""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .execution_records import TestExecutionRecord
from .static_site_dom_checks import (
    InertControlCheckRequest,
    form_binding_hits,
    inert_control_hits,
    missing_dom_id_hits,
)
from .static_site_html_parser import StaticSiteHTMLParser
from .static_site_js_api_checks import missing_window_app_method_hits
from .static_site_path_checks import (
    broken_refs,
    inside,
    local_script_refs,
    rel,
    small_text,
    string_list,
    unique_paths,
)
from .static_site_records import static_site_failure_summary, static_site_record


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
    html_structure_hits: list[str] = field(default_factory=list)
    inert_control_hits: list[str] = field(default_factory=list)
    form_binding_hits: list[str] = field(default_factory=list)
    missing_dom_id_hits: list[str] = field(default_factory=list)
    missing_js_api_hits: list[str] = field(default_factory=list)
    repair_hints: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # LLM: ok is the single pass/fail boolean consumed by parent acceptance.
    # 函数用途: 判断静态站点检查是否通过；任何缺文件、坏链接、占位符或失效控件都会失败。
    @property
    def ok(self) -> bool:
        return not (
            self.missing_required_files
            or self.placeholder_hits
            or self.broken_local_refs
            or self.html_structure_hits
            or self.inert_control_hits
            or self.form_binding_hits
            or self.missing_dom_id_hits
            or self.missing_js_api_hits
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
            "html_structure_hits": self.html_structure_hits,
            "inert_control_hits": self.inert_control_hits,
            "form_binding_hits": self.form_binding_hits,
            "missing_dom_id_hits": self.missing_dom_id_hits,
            "missing_js_api_hits": self.missing_js_api_hits,
            "repair_hints": self.repair_hints,
            "warnings": self.warnings,
        }


# LLM: StaticSiteScanOptions keeps _scan_site thin as validators grow.
# 类用途: 保存一次静态站点扫描的布尔开关和显式 DOM 合同。
@dataclass(frozen=True)
class StaticSiteScanOptions:
    check_refs: bool
    check_placeholders: bool
    check_complete_html: bool
    check_controls: bool
    check_forms: bool
    strict_dom_bindings: bool
    required_dom_ids: list[str]


# LLM: StaticSiteScanState accumulates cross-file IDs and script text for bounded checks.
# 类用途: 汇总多 HTML 文件里的 form/id/script 摘要，供最终 DOM 和表单绑定检查使用。
@dataclass
class StaticSiteScanState:
    form_ids: set[str] = field(default_factory=set)
    element_ids: set[str] = field(default_factory=set)
    script_texts: list[str] = field(default_factory=list)


# LLM: StaticSiteHtmlScanRequest bundles per-file scan dependencies.
# 类用途: 避免 `_scan_html_file` 继续增加散参数；后续扫描字段扩展到这个请求包里。
@dataclass(frozen=True)
class StaticSiteHtmlScanRequest:
    result: StaticSiteCheckResult
    path: Path
    site_root: Path
    options: StaticSiteScanOptions
    state: StaticSiteScanState


# LLM: run_static_site_check is the public validation entrypoint used by TestExecutor.
# 函数用途: 检查 workspace 内静态站点目录，并返回父级验收能直接读取的执行记录。
def run_static_site_check(test: dict[str, Any], workspace_root: Path) -> TestExecutionRecord:
    site_root, error = _resolve_site_root(test, workspace_root)
    if error:
        return static_site_record(
            test, StaticSiteCheckResult(str(site_root)), executed=False, error=error
        )
    result = _scan_site(test, site_root)
    error_text = "" if result.ok else static_site_failure_summary(result)
    return static_site_record(test, result, executed=True, error=error_text)


# LLM: _resolve_site_root enforces workspace bounds before any directory walk.
# 函数用途: 解析 site_root/root_dir/file_path，保证静态检查不能扫描工作区外部。
def _resolve_site_root(test: dict[str, Any], workspace_root: Path) -> tuple[Path, str]:
    raw = str(test.get("site_root") or test.get("root_dir") or test.get("file_path") or ".").strip()
    workspace = workspace_root.expanduser().resolve()
    candidate = Path(raw).expanduser()
    path = candidate.resolve() if candidate.is_absolute() else (workspace / candidate).resolve()
    try:
        path.relative_to(workspace)
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
    html_files = _html_files(test, site_root, int(test.get("max_files") or 200))
    result.checked_files = [rel(path, site_root) for path in html_files]
    options = _scan_options(test)
    state = StaticSiteScanState()
    for path in html_files:
        _scan_html_file(StaticSiteHtmlScanRequest(result, path, site_root, options, state))
    _finalize_dom_checks(result, options, state)
    result.repair_hints.extend(_repair_hints(result))
    return result


# LLM: _scan_options extracts validator flags once so the scanner stays readable.
# 函数用途: 从测试项生成静态站点扫描选项；后续新增开关集中放这里。
def _scan_options(test: dict[str, Any]) -> StaticSiteScanOptions:
    return StaticSiteScanOptions(
        check_refs=test.get("check_local_refs", True) is not False,
        check_placeholders=test.get("forbid_placeholders", True) is not False,
        check_complete_html=test.get("require_complete_html", False) is True,
        check_controls=test.get("check_inert_controls", True) is not False,
        check_forms=test.get("check_form_bindings", True) is not False,
        strict_dom_bindings=test.get("strict_dom_bindings", True) is not False,
        required_dom_ids=string_list(test.get("required_dom_ids")),
    )


# LLM: _scan_html_file performs one-file checks and records cross-file facts in state.
# 函数用途: 读取一个 HTML 文件，检查局部引用/控件/占位符，并把 id/script 摘要写入扫描状态。
def _scan_html_file(request: StaticSiteHtmlScanRequest) -> None:
    result, path, site_root = request.result, request.path, request.site_root
    options, state = request.options, request.state
    text = path.read_text(encoding="utf-8", errors="replace")
    if options.check_placeholders and _has_visible_template_placeholder(text):
        result.placeholder_hits.append(rel(path, site_root))
    if options.check_complete_html:
        result.html_structure_hits.extend(_html_structure_hits(text, path, site_root))
    parser = StaticSiteHTMLParser()
    parser.feed(text)
    state.form_ids.update(parser.form_ids)
    state.element_ids.update(parser.element_ids)
    local_script_text = "\n".join(
        small_text(item) for item in unique_paths(local_script_refs(parser.refs, path, site_root))
    )
    state.script_texts.extend([text, local_script_text])
    if options.check_refs:
        result.broken_local_refs.extend(broken_refs(parser.refs, path, site_root))
    if options.check_controls:
        result.inert_control_hits.extend(
            inert_control_hits(
                InertControlCheckRequest(
                    controls=parser.controls,
                    rel_path=rel(path, site_root),
                    html_text=f"{text}\n{local_script_text}",
                    element_ids=set(parser.element_ids),
                )
            )
        )


# LLM: _finalize_dom_checks runs cross-file DOM checks after every HTML file is scanned.
# 函数用途: 汇总表单绑定、脚本引用 id 和 required_dom_ids 的最终机器失败事实。
def _finalize_dom_checks(
    result: StaticSiteCheckResult,
    options: StaticSiteScanOptions,
    state: StaticSiteScanState,
) -> None:
    combined_script_text = "\n".join(state.script_texts)
    if options.check_forms:
        result.form_binding_hits.extend(form_binding_hits(state.form_ids, combined_script_text))
    result.missing_dom_id_hits.extend(
        missing_dom_id_hits(
            state.element_ids,
            combined_script_text,
            allow_optional_missing=not options.strict_dom_bindings,
        )
    )
    result.missing_dom_id_hits.extend(
        _missing_required_dom_id_hits(state.element_ids, options.required_dom_ids)
    )
    result.missing_js_api_hits.extend(missing_window_app_method_hits(combined_script_text))


# LLM: _check_required_files records missing pages/assets without opening arbitrary paths.
# 函数用途: 检查调用方声明的 required_files；相对路径必须仍在站点根目录内。
def _check_required_files(
    result: StaticSiteCheckResult, test: dict[str, Any], site_root: Path
) -> None:
    for item in string_list(test.get("required_files")):
        path = (site_root / item).resolve()
        if not inside(path, site_root) or not path.exists() or not path.is_file():
            result.missing_required_files.append(item)


# LLM: _html_files limits directory scanning so large workspaces cannot explode validation cost.
# 函数用途: 返回站点根下有限数量的 HTML 文件；按路径排序保证报告稳定。
def _html_files(test: dict[str, Any], site_root: Path, max_files: int) -> list[Path]:
    scoped = _scoped_html_files(test, site_root)
    if scoped:
        return scoped[: max(1, min(max_files, 500))]
    limit = max(1, min(max_files, 500))
    return sorted(path for path in site_root.rglob("*.html") if path.is_file())[:limit]


# LLM: _scoped_html_files lets leaf acceptance validate one declared page without sibling interference.
# 函数用途: 当测试项声明 html_files/check_files 时，只扫描这些站点内 HTML 文件；不存在文件由 required_files 报告。
def _scoped_html_files(test: dict[str, Any], site_root: Path) -> list[Path]:
    paths: list[Path] = []
    for item in string_list(test.get("html_files") or test.get("check_files")):
        candidate = (site_root / item).resolve()
        if (
            inside(candidate, site_root)
            and candidate.is_file()
            and candidate.suffix.lower() in {".html", ".htm"}
        ):
            paths.append(candidate)
    return sorted(dict.fromkeys(paths))


# LLM: _missing_required_dom_id_hits checks explicit business-section contracts.
# 函数用途: static_site_check 调用方声明 required_dom_ids 时，缺少对应 id 就写入机器失败事实。
def _missing_required_dom_id_hits(element_ids: set[str], required_dom_ids: list[str]) -> list[str]:
    return [
        f"required_dom_id:{item}"
        for item in sorted(set(required_dom_ids))
        if item not in element_ids
    ]


# LLM: _html_structure_hits catches malformed full-page HTML without requiring a browser.
# 函数用途: 对声明为完整 HTML 的产物做轻量结构检查，避免正文落进 style/script 这类明显坏页通过验收。
def _html_structure_hits(text: str, html_file: Path, site_root: Path) -> list[str]:
    rel_path = rel(html_file, site_root)
    lower = text.lower()
    hits: list[str] = []
    required = (
        ("doctype", "<!doctype"),
        ("html_open", "<html"),
        ("html_close", "</html>"),
        ("head_close", "</head>"),
        ("body_open", "<body"),
        ("body_close", "</body>"),
    )
    for label, marker in required:
        if marker not in lower:
            hits.append(f"{rel_path}:{label}")
    for tag in ("style", "script"):
        opens = len(re.findall(rf"<{tag}\b", lower))
        closes = lower.count(f"</{tag}>")
        if opens != closes:
            hits.append(f"{rel_path}:unbalanced_{tag}")
    for tag in ("html", "body"):
        closes = lower.count(f"</{tag}>")
        if closes > 1:
            hits.append(f"{rel_path}:duplicate_{tag}_close")
    html_close = lower.find("</html>")
    if html_close != -1 and re.search(r"<[A-Za-z][^>]*>", lower[html_close + len("</html>") :]):
        hits.append(f"{rel_path}:trailing_markup_after_html_close")
    if (
        lower.find("<body") != -1
        and lower.find("</head>") != -1
        and lower.find("<body") < lower.find("</head>")
    ):
        hits.append(f"{rel_path}:body_before_head_close")
    return hits


# LLM: _repair_hints turns validation facts into short action hints for parent repair dispatch.
# 函数用途: 给父级/修复子代理一组不用读正文也能理解的修复方向，避免只靠自然语言猜。
def _repair_hints(result: StaticSiteCheckResult) -> list[str]:
    hints: list[str] = []
    if result.html_structure_hits:
        hints.append(
            "html_structure: repair or regenerate a complete HTML skeleton before DOM/id fixes"
        )
    if result.inert_control_hits:
        hints.append(
            "inert_controls: add real href targets, onclick handlers, or matching anchor sections for listed controls"
        )
    if result.form_binding_hits:
        hints.append(
            "form_bindings: create the referenced form id or update validateForm(...) to the existing form id"
        )
    if result.missing_dom_id_hits:
        hints.append(
            "missing_dom_ids: add the referenced id to a real element or remove the stale unguarded JS lookup"
        )
    if result.missing_js_api_hits:
        hints.append(
            "missing_js_api: export referenced window.app methods or update inline handlers/templates"
        )
    return hints[:6]


# LLM: _has_visible_template_placeholder separates real leftover HTML placeholders from JS template literals.
# 函数用途: 检查页面可见/标记区域是否残留 `${...}`；会先剔除 script/style，避免误伤正常 JavaScript 模板字符串。
def _has_visible_template_placeholder(text: str) -> bool:
    visible_text = re.sub(
        r"<(script|style)\b[^>]*>.*?</\1>", "", text, flags=re.IGNORECASE | re.DOTALL
    )
    return "${" in visible_text
