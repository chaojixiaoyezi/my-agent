
from __future__ import annotations

import os
from itertools import islice
from pathlib import Path
from typing import Any

from .artifact_integrity import ArtifactIntegrityDecision, ArtifactIntegrityIssue

_WEB_ASSET_SUFFIXES = {".html", ".htm", ".css", ".js", ".mjs", ".cjs"}
_STATIC_SITE_ISSUE_CODES = {
    "missing_required_files": "STATIC_SITE_MISSING_REQUIRED_FILES",
    "placeholder_hits": "STATIC_SITE_PLACEHOLDER_HITS",
    "broken_local_refs": "STATIC_SITE_BROKEN_LOCAL_REFS",
    "html_structure_hits": "STATIC_SITE_HTML_STRUCTURE_HITS",
    "inert_control_hits": "STATIC_SITE_INERT_CONTROL_HITS",
    "form_binding_hits": "STATIC_SITE_FORM_BINDING_HITS",
    "missing_dom_id_hits": "STATIC_SITE_MISSING_DOM_ID_HITS",
    "missing_js_api_hits": "STATIC_SITE_MISSING_JS_API_HITS",
}


def check_web_project_post_write(path: Path, workspace_root: Path) -> ArtifactIntegrityDecision:
    target = Path(path).expanduser().resolve(strict=False)
    workspace = Path(workspace_root).expanduser().resolve(strict=False)
    if target.suffix.lower() not in _WEB_ASSET_SUFFIXES:
        return ArtifactIntegrityDecision(ok=True)
    site_root = _nearest_site_root(target, workspace)
    if site_root is None:
        return ArtifactIntegrityDecision(ok=True)
    if _is_server_side_template_project(target, site_root):
        return ArtifactIntegrityDecision(ok=True)
    from ..subagents.static_site import run_static_site_check

    record = run_static_site_check(_static_site_request(site_root, workspace), workspace)
    return _web_project_decision(record.validation_result)


_TEMPLATE_MARKERS = ("{%", "{{")  # Jinja2/Django/Nunjucks/Handlebars 等模板/动态绑定语法


def _is_server_side_template_project(target: Path, site_root: Path) -> bool:
    """Flask/Django/FastAPI+Jinja2 等服务端模板项目不该用'静态站点交付'规则校验:模板是片段(含
    {% extends %}/{% block %})、引用运行期由框架解析(url_for/static),用 require_complete_html/
    check_local_refs/forbid_placeholders 会把合法模板全部误杀、卡死整个 web 后端任务。识别信号:
    templates/ 目录(主流框架约定)或 .html 内含模板语法;真静态网站(无模板语法)仍照常校验。"""
    if any(part == "templates" for part in (*target.parts, *site_root.parts)):
        return True
    try:
        with os.scandir(site_root) as entries:
            html_files = (Path(entry.path) for entry in entries if entry.name.endswith(".html"))
            for html in islice(html_files, 20):
                try:
                    with html.open("rb", buffering=0) as source:
                        raw = bytearray()
                        # UTF-8 每字符最多四字节；坏编码也不能令 ignore 解码无限读取。
                        for _ in range(4):
                            chunk = source.read(8192)
                            raw.extend(chunk)
                            head = raw.decode("utf-8", errors="ignore").replace("\r\n", "\n").replace("\r", "\n")
                            if len(head) >= 8192 or not chunk:
                                break
                except OSError:
                    continue
                if any(marker in head[:8192] for marker in _TEMPLATE_MARKERS):
                    return True
    except OSError:
        return False
    return False


def _static_site_request(site_root: Path, workspace: Path) -> dict[str, object]:
    return {
        "validation_method": "static_site_check",
        "post_write_check": True,
        "site_root": _site_root_ref(site_root, workspace),
        "require_complete_html": True,
        "strict_dom_bindings": True,
        "check_local_refs": True,
        "forbid_placeholders": True,
        "check_inert_controls": True,
        "check_form_bindings": True,
        "max_files": 200,
    }


# LLM: 路径先 canonicalize，回溯不越过工作区且绝不把宿主根当站点；cjs 必须有 index 邻域。
# 函数用途: 只在真正的项目范围定位静态站点，避免写脚本触发全 home 扫描。
def _nearest_site_root(path: Path, workspace_root: Path) -> Path | None:
    path = path.expanduser().resolve(strict=False)
    workspace_root = workspace_root.expanduser().resolve(strict=False)
    try:
        path.relative_to(workspace_root)
    except ValueError:
        return None
    start = path.parent if path.is_file() or path.suffix else path
    for candidate in (start, *start.parents):
        if not _inside(candidate, workspace_root):
            break
        if _is_host_root(candidate):
            break
        has_html = ((candidate / "index.html").is_file() if path.suffix.lower() == ".cjs" else
                    any(item.is_file() for item in candidate.glob("*.html")))
        if has_html:
            return candidate
        if candidate == workspace_root:
            break
    return None


# LLM: 只读取目录事实与 canonical owner 布局，不依赖任务正文或当前进程 cwd。
# 函数用途: 识别不能作为站点根的宿主/owner 根目录。
def _is_host_root(path: Path) -> bool:
    if any((path / marker).exists() or (path / marker).is_symlink()
           for marker in ("runtime.db", "workspace/runtime", "sessions")):
        return True
    return path.parent.parent.name == "owners" or (
        path.parent.name in {"users", "groups"} and path.parent.parent.parent.name == "providers"
        and path.parent.parent.parent.parent.name == "owners"
    )


def _inside(candidate: Path, workspace_root: Path) -> bool:
    try:
        candidate.relative_to(workspace_root)
        return True
    except ValueError:
        return False


def _site_root_ref(site_root: Path, workspace_root: Path) -> str:
    try:
        return str(site_root.relative_to(workspace_root)).replace("\\", "/") or "."
    except ValueError:
        return str(site_root)


# LLM: 成功写入仍成功，扫描缺口以结构化 warning 暴露给调用方。
# 函数用途: 把整站发现及不完整事实转换成写后提醒。
def _web_project_decision(validation_result: dict[str, Any]) -> ArtifactIntegrityDecision:
    issues = [
        ArtifactIntegrityIssue(
            code=code,
            message=f"{field} failed.",
            severity="warning",
            count=len(values),
        )
        for field, code in _STATIC_SITE_ISSUE_CODES.items()
        if isinstance((values := validation_result.get(field)), list) and values
    ]
    if validation_result.get("scan_incomplete") is True:
        issues.append(ArtifactIntegrityIssue(code="STATIC_SITE_SCAN_INCOMPLETE", message="站点扫描不完整。",
                                            severity="warning", count=1))
    # 单文件已经原子落盘后，整站未收齐只是当前状态，不能把成功写入伪装成失败。
    # 最终是否可交付仍由显式 static_site_check 统一判定。
    return ArtifactIntegrityDecision(ok=True, kind="web_project", issues=issues)
