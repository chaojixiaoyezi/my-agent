# LLM: 纯文本层，不读写文件、不发请求。三个模板是自写的独立 HTML（内联 CSS、无外部资源、无脚本）；
#   可编辑字段只由 data-dl-field 标记定位，edit 只替换标记元素的内容，其余字符原样保留。
#   改模板时必须保持：标记属性紧跟标签名、元素内容不含 "<"、颜色元素内容固定为 COLOR_RULE 形态，否则 edit 会拒绝。
# 模块用途: 提供模板渲染、字段校验，以及在已生成的设计文件里读取和替换标题、副标题、颜色。

from __future__ import annotations

import html
import re
from string import Template

FIELDS = ("title", "subtitle", "color")
_COLOR = re.compile(r"#[0-9a-fA-F]{6}")
_GENERATOR = re.compile(r'<meta name="generator" content="design-lite [^"<>]*">')
# 标记属性必须紧跟标签名，内容不含 "<"；这样定位不依赖通用 HTML 解析，也不会误吞相邻元素
_ELEMENT = re.compile(r'(<([a-z][a-z0-9]*) data-dl-field="(title|subtitle|color)"[^<>]*>)([^<]*)(</\2>)')
_MARKER = re.compile(r'data-dl-field="([^"]*)"')
_COLOR_RULE = re.compile(r":root\{--dl-color:(#[0-9a-fA-F]{6})\}")


# LLM: code 是稳定的业务失败分类，正文是中文说明；不携带文件正文或异常栈。
# 类用途: 让协议入口把设计文件失败转成工具错误结果，不终止插件进程。
class DesignError(ValueError):
    # LLM: extra 只放结构化事实（如缺失字段名），协议入口原样并入错误结果。
    # 函数用途: 保存错误码、中文说明和可选附加字段。
    def __init__(self, code: str, message: str, **extra: object):
        super().__init__(message)
        self.code = code
        self.extra = extra


_HEAD = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="generator" content="design-lite ${version}; template=${template}">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title data-dl-field="title">${title}</title>
<style data-dl-field="color">:root{--dl-color:${color}}</style>
"""

_TEMPLATES = {
    "card": ("#3366ff", _HEAD + """<style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
  background:#f3f4f6;font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;color:#1f2937}
.dl-card{width:min(420px,calc(100vw - 32px));background:#fff;border-radius:16px;overflow:hidden;
  box-shadow:0 10px 30px rgba(0,0,0,.08)}
.dl-card .band{height:12px;background:var(--dl-color)}
.dl-card .body{padding:28px 28px 32px}
.dl-card h1{margin:0 0 12px;font-size:26px;line-height:1.3;color:var(--dl-color)}
.dl-card p{margin:0;font-size:16px;line-height:1.6;color:#4b5563}
.dl-card p:empty{display:none}
</style>
</head>
<body>
<main class="dl-card">
<div class="band"></div>
<div class="body">
<h1 data-dl-field="title">${title}</h1>
<p data-dl-field="subtitle">${subtitle}</p>
</div>
</main>
</body>
</html>
"""),
    "poster": ("#e4572e", _HEAD + """<style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#111827;
  font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
.dl-poster{width:min(600px,calc(100vw - 32px));aspect-ratio:3/4;box-sizing:border-box;padding:48px 40px;
  display:flex;flex-direction:column;justify-content:flex-end;background:var(--dl-color);color:#fff;border-radius:8px}
.dl-poster h1{margin:0 0 20px;font-size:clamp(36px,8vw,64px);line-height:1.1;letter-spacing:-.02em}
.dl-poster p{margin:0;font-size:20px;line-height:1.5;opacity:.9}
.dl-poster p:empty{display:none}
</style>
</head>
<body>
<main class="dl-poster">
<h1 data-dl-field="title">${title}</h1>
<p data-dl-field="subtitle">${subtitle}</p>
</main>
</body>
</html>
"""),
    "landing": ("#0f766e", _HEAD + """<style>
body{margin:0;font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;color:#1f2937;background:#fff}
.dl-bar{padding:16px 24px;border-bottom:1px solid #e5e7eb;font-weight:600;color:var(--dl-color)}
.dl-hero{padding:72px 24px;text-align:center}
.dl-hero h1{margin:0 auto 16px;max-width:760px;font-size:clamp(32px,6vw,52px);line-height:1.15}
.dl-hero p{margin:0 auto 28px;max-width:620px;font-size:18px;line-height:1.6;color:#4b5563}
.dl-hero p:empty{display:none}
.dl-cta{display:inline-block;padding:12px 28px;border-radius:999px;background:var(--dl-color);color:#fff;font-weight:600}
.dl-points{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;max-width:900px;
  margin:0 auto;padding:0 24px 72px}
.dl-points div{padding:20px;border-radius:12px;background:#f9fafb;border-top:4px solid var(--dl-color)}
</style>
</head>
<body>
<header class="dl-bar">Design Lite</header>
<main>
<section class="dl-hero">
<h1 data-dl-field="title">${title}</h1>
<p data-dl-field="subtitle">${subtitle}</p>
<span class="dl-cta">立即了解</span>
</section>
<section class="dl-points">
<div>要点一：在这里写第一条卖点</div>
<div>要点二：在这里写第二条卖点</div>
<div>要点三：在这里写第三条卖点</div>
</section>
</main>
</body>
</html>
"""),
}
TEMPLATE_NAMES = tuple(_TEMPLATES)


# LLM: 颜色只接受 #RRGGBB，统一转小写后写入 CSS 变量；不接受颜色名、rgb() 或短写，避免注入 CSS。
# 函数用途: 校验并规范化颜色参数。
def normalize_color(value: str) -> str:
    if not isinstance(value, str) or not _COLOR.fullmatch(value):
        raise DesignError("INVALID_COLOR", "颜色必须是 #RRGGBB 形式的十六进制值，例如 #3366ff。")
    return value.lower()


# LLM: 文本一律 html.escape(quote=True)，保证内容不含 "<"，标记定位与转义安全同时成立。
# 函数用途: 按模板名渲染完整 HTML 文本；未给颜色时用模板默认色。
def render(template: str, version: str, title: str, subtitle: str, color: str | None) -> str:
    if template not in _TEMPLATES:
        raise DesignError("UNKNOWN_TEMPLATE", f"未知模板 {template!r}；可选：{'、'.join(TEMPLATE_NAMES)}。")
    default_color, body = _TEMPLATES[template]
    return Template(body).substitute(
        version=html.escape(version), template=template, title=html.escape(title),
        subtitle=html.escape(subtitle), color=normalize_color(color) if color is not None else default_color)


# LLM: 只认头部含 generator=design-lite 的文件；每个 data-dl-field 标记都必须能被严格形态匹配，
#   否则说明文件被手工改坏，整体拒绝而不是猜测替换位置。返回 {字段: [匹配]}。
# 函数用途: 在设计文件里定位全部可编辑字段元素。
def locate_fields(text: str) -> dict[str, list[re.Match]]:
    head_end = text.find("</head>")
    if head_end < 0 or not _GENERATOR.search(text, 0, head_end):
        raise DesignError("NOT_DESIGN_FILE", "这不是 design-lite 生成的文件（缺少生成标记），已拒绝修改。")
    found: dict[str, list[re.Match]] = {name: [] for name in FIELDS}
    for match in _ELEMENT.finditer(text):
        found[match.group(3)].append(match)
    markers = _MARKER.findall(text)
    if len(markers) != sum(len(items) for items in found.values()) or not found["title"] or not found["color"]:
        raise DesignError("NOT_DESIGN_FILE", "文件里的 data-dl 标记缺失或被改动，无法安全定位字段，已拒绝修改。")
    if any(not _COLOR_RULE.fullmatch(match.group(4)) for match in found["color"]):
        raise DesignError("NOT_DESIGN_FILE", "颜色标记内容被改动，无法安全修改颜色，已拒绝修改。")
    return found


# LLM: 旧值取第一个匹配元素（同一字段的多个元素生成时内容一致）；文本字段返回反转义后的原文。
# 函数用途: 读出某字段当前值，供结果展示新旧对比。
def field_value(name: str, matches: list[re.Match]) -> str:
    content = matches[0].group(4)
    return _COLOR_RULE.fullmatch(content).group(1).lower() if name == "color" else html.unescape(content)


# LLM: 只替换 changes 中字段对应元素的内容区间，从后往前拼接，其余字符（含开闭标签、CSS、换行）原样保留。
#   changes 的值须已校验：颜色已规范化，文本未转义（这里统一转义）。
# 函数用途: 生成替换指定字段后的新文本。
def apply_fields(text: str, found: dict[str, list[re.Match]], changes: dict[str, str]) -> str:
    spans = []
    for name, value in changes.items():
        content = f":root{{--dl-color:{value}}}" if name == "color" else html.escape(value)
        spans.extend((match.start(4), match.end(4), content) for match in found[name])
    for start, end, content in sorted(spans, reverse=True):
        text = text[:start] + content + text[end:]
    return text
