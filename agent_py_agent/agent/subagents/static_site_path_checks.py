# LLM: Static-site path helpers validate local refs without executing browser or shell code.
# 模块用途: 解析站点内链接、脚本引用和路径展示，保持 validator 主流程短小。

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

REMOTE_SCHEMES = {"http", "https", "mailto", "tel", "data", "javascript"}


# LLM: broken_refs checks only local href/src/action targets and ignores remote URLs.
# 函数用途: 找出失效的本地页面、图片、脚本和表单 action，不访问网络。
def broken_refs(refs: list[tuple[str, str]], html_file: Path, site_root: Path) -> list[str]:
    broken: list[str] = []
    for attr, ref in refs:
        target = local_ref_target(ref, html_file, site_root)
        if target is not None and not target.exists():
            broken.append(f"{rel(html_file, site_root)}:{attr}={ref}")
    return broken


# LLM: local_ref_target resolves a local URL-like ref into a filesystem path when safe.
# 函数用途: 把相对/根相对链接转换为站点内路径；远程、锚点和越界引用返回 None。
def local_ref_target(ref: str, html_file: Path, site_root: Path) -> Path | None:
    cleaned = ref.strip()
    if not cleaned or cleaned.startswith("#") or cleaned.startswith("//"):
        return None
    parsed = urlsplit(cleaned)
    if parsed.scheme.lower() in REMOTE_SCHEMES:
        return None
    path_part = parsed.path.strip()
    if not path_part:
        return None
    candidate = _local_candidate(path_part, html_file, site_root)
    if candidate.is_dir():
        candidate = candidate / "index.html"
    return candidate if inside(candidate, site_root) else None


# LLM: local_script_refs reuses ref resolution but only returns local JavaScript files.
# 函数用途: 收集站点内 script src，用于检查表单绑定目标是否真实存在。
def local_script_refs(refs: list[tuple[str, str]], html_file: Path, site_root: Path) -> list[Path]:
    paths: list[Path] = []
    for attr, ref in refs:
        if attr != "src":
            continue
        target = local_ref_target(ref, html_file, site_root)
        if target is not None and target.suffix.lower() == ".js" and target.exists():
            paths.append(target)
    return paths


# LLM: unique_paths avoids repeated reads for shared app.js across many pages.
# 函数用途: 保持脚本读取顺序稳定并去重，减少验收时的重复 I/O。
def unique_paths(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


# LLM: small_text keeps local JS scans bounded and failure-tolerant.
# 函数用途: 读取小型本地脚本；过大或无法读取时返回空串，避免验收撑爆上下文。
def small_text(path: Path, max_bytes: int = 262144) -> str:
    try:
        if path.stat().st_size > max_bytes:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# LLM: string_list accepts runner JSON shapes without trusting non-string objects.
# 函数用途: 把 required_files 等字段规整成短字符串列表。
def string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


# LLM: inside centralizes path containment checks for refs and required files.
# 函数用途: 判断解析后的路径是否仍在站点根目录内。
def inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


# LLM: rel renders stable site-relative paths in reports.
# 函数用途: 把绝对路径压成站点内相对路径，避免报告噪音和隐私泄漏。
def rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


# LLM: _local_candidate applies root-relative and html-relative URL path rules.
# 函数用途: 根据 URL path 生成候选文件路径，不检查存在性。
def _local_candidate(path_part: str, html_file: Path, site_root: Path) -> Path:
    if path_part.startswith("/"):
        return (site_root / path_part.lstrip("/")).resolve()
    return (html_file.parent / path_part).resolve()
