# LLM: 纯渲染模块：所有来自文件系统的名称和内容都经 html.escape 转义；只用内联 CSS，不引外部资源、不输出脚本。
#   HTML 文件只以 sandbox="" 的 iframe srcdoc 展示（禁止脚本、表单和同源），不直接当本站页面渲染。
# 模块用途: 生成目录列表页、文件预览页和错误页的 HTML 文本。

from __future__ import annotations

import html
import mimetypes
from urllib.parse import quote

STYLE = """
body{font:14px/1.5 -apple-system,"Segoe UI","PingFang SC",sans-serif;margin:0;color:#1d1d1f;background:#f6f6f4}
header{background:#1f2933;color:#f5f7fa;padding:10px 16px}header b{margin-right:12px}header code{color:#cbd2d9}
main{padding:12px 16px;max-width:1100px}a{color:#0b61a4;text-decoration:none}a:hover{text-decoration:underline}
table{border-collapse:collapse;width:100%;background:#fff}td,th{padding:4px 8px;border-bottom:1px solid #e4e7eb;text-align:left}
td.n{text-align:right;font-variant-numeric:tabular-nums;color:#52606d}.muted{color:#7b8794}
pre{background:#fff;border:1px solid #e4e7eb;padding:10px;overflow:auto;white-space:pre-wrap;word-break:break-word}
iframe{width:100%;height:75vh;border:1px solid #e4e7eb;background:#fff}img{max-width:100%;border:1px solid #e4e7eb}
.note{background:#fff8e1;border:1px solid #f0d58c;padding:6px 10px;margin:8px 0}
"""
HTML_SUFFIXES = (".html", ".htm")


# LLM: 标题栏固定显示插件名和浏览目录（转义后），供用户确认当前看的是哪个服务。
# 函数用途: 拼出带内联样式和顶部标题栏的完整 HTML 文档。
def document(title: str, root: str, body: str) -> str:
    return (f"<!doctype html><html lang=\"zh\"><head><meta charset=\"utf-8\"><title>{html.escape(title)}</title>"
            f"<style>{STYLE}</style></head><body><header><b>web-board</b><code>{html.escape(root)}</code></header>"
            f"<main>{body}</main></body></html>")


# LLM: 链接只携带相对路径查询参数，不带令牌（令牌靠 HttpOnly cookie 或用户自带的 query）。
# 函数用途: 生成指向某个页面和相对路径的站内链接。
def link(page: str, relative: str) -> str:
    return f"{page}?p={quote(relative, safe='')}" if relative else page


# LLM: 面包屑每一段都是站内目录链接；名称全部转义。
# 函数用途: 把相对路径渲染成可点击的面包屑导航。
def breadcrumb(relative: str) -> str:
    parts, items, current = [p for p in relative.split("/") if p], [f"<a href=\"{link('/', '')}\">根目录</a>"], ""
    for part in parts:
        current = f"{current}/{part}" if current else part
        items.append(f"<a href=\"{html.escape(link('/', current))}\">{html.escape(part)}</a>")
    return "<p>" + " / ".join(items) + "</p>"


# 函数用途: 把字节数显示成带单位的短文本。
def human_size(size: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return str(size)


# LLM: 不可打开的条目（链接、设备、被权限拒绝）只显示名称和类型，不生成链接。
# 函数用途: 渲染目录列表页，子目录进入列表、文件进入预览。
def listing_page(root: str, relative: str, entries: list[dict], truncated: bool) -> str:
    rows = []
    if relative:
        parent = relative.rsplit("/", 1)[0] if "/" in relative else ""
        rows.append(f"<tr><td><a href=\"{html.escape(link('/', parent))}\">..</a></td><td></td><td>上级目录</td></tr>")
    for entry in entries:
        path = f"{relative}/{entry['name']}" if relative else entry["name"]
        name = html.escape(entry["name"]) + ("/" if entry["kind"] == "directory" else "")
        if entry["openable"]:
            name = f"<a href=\"{html.escape(link('/' if entry['kind'] == 'directory' else '/view', path))}\">{name}</a>"
        kind = {"directory": "目录", "file": "文件"}.get(entry["kind"], "其它（不可打开）")
        if entry["kind"] != "other" and not entry["openable"]:
            kind += "（无权限）"
        size = "" if entry["kind"] == "directory" else human_size(entry["size"])
        rows.append(f"<tr><td>{name}</td><td class=\"n\">{size}</td><td>{kind}</td></tr>")
    note = f"<p class=\"note\">条目过多，只显示前 {len(entries)} 项。</p>" if truncated else ""
    empty = "<p class=\"muted\">（空目录）</p>" if not entries else ""
    body = (f"{breadcrumb(relative)}{note}<table><tr><th>名称</th><th>大小</th><th>类型</th></tr>"
            f"{''.join(rows)}</table>{empty}")
    return document(relative or "根目录", root, body)


# LLM: kind 决定展示方式：image 用 <img src=/raw>；html 用 sandbox iframe srcdoc；其余按文本 <pre> 转义。
#   类型只按扩展名推测，未知扩展名一律按文本尝试，不因不在表里而拒绝。
# 函数用途: 按文件名推测预览方式。
def preview_kind(name: str) -> str:
    if name.lower().endswith(HTML_SUFFIXES):
        return "html"
    guessed = mimetypes.guess_type(name)[0] or ""
    return "image" if guessed.startswith("image/") else "text"


# LLM: content 已按上限读取；含空字节视为二进制只给原始链接。所有文本转义后放进 <pre> 或 srcdoc 属性。
# 函数用途: 渲染单个文件的预览页。
def view_page(root: str, relative: str, kind: str, content: bytes, size: int, truncated: bool) -> str:
    raw = html.escape(link("/raw", relative))
    head = f"{breadcrumb(relative)}<p class=\"muted\">{human_size(size)} · <a href=\"{raw}\">原始文件</a></p>"
    note = f"<p class=\"note\">文件较大，只显示前 {len(content)} 字节（设置 max_preview_bytes）。</p>" if truncated else ""
    if kind == "image":
        body = f"<img src=\"{raw}\" alt=\"{html.escape(relative)}\">"
        note = ""
    elif b"\x00" in content:
        body = "<p class=\"note\">二进制文件，不做文本预览。</p>"
    elif kind == "html":
        body = f"<iframe sandbox=\"\" srcdoc=\"{html.escape(content.decode('utf-8', 'replace'))}\"></iframe>"
    else:
        body = f"<pre>{html.escape(content.decode('utf-8', 'replace'))}</pre>"
    return document(relative, root, head + note + body)


# 函数用途: 渲染带状态码说明的简单错误页。
def error_page(root: str, status: int, message: str) -> str:
    return document(f"{status}", root, f"<p class=\"note\">{status}：{html.escape(message)}</p><p><a href=\"/\">回到根目录</a></p>")
