# LLM: 纯渲染，不读写文件；产出必须是独立 HTML：内联 CSS 与内联 SVG，不含 <script>、外部链接、字体或 url() 资源。
#   所有来自数据文件或参数的文本（标题、列名、单元格、图表标签）都经 html.escape 转义后再拼入。
# 模块用途: 把规范化表格和可选条形图数值渲染成可直接用浏览器打开的单文件 HTML。

from __future__ import annotations

from html import escape

from .tables import Table, cell_text

_CSS = """
:root{--surface:#fcfcfb;--text:#0b0b0b;--muted:#52514e;--rule:#e4e3df;--bar:#2a78d6;--stripe:#f4f3f0}
@media (prefers-color-scheme:dark){:root{--surface:#1a1a19;--text:#fff;--muted:#c3c2b7;--rule:#383835;
--bar:#3987e5;--stripe:#232322}}
*{box-sizing:border-box}
body{margin:0;background:var(--surface);color:var(--text);
font:15px/1.5 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
main{max-width:960px;margin:0 auto;padding:24px 16px}
h1{font-size:22px;margin:0 0 4px}
.meta{color:var(--muted);margin:0 0 20px}
figure{margin:0 0 24px}
figcaption{color:var(--muted);font-size:13px;margin-bottom:8px}
svg{width:100%;height:auto;display:block}
svg text{fill:var(--text);font-size:12px}
svg .value{fill:var(--muted)}
svg rect{fill:var(--bar)}
svg g:hover rect{opacity:.8}
.wrap{overflow-x:auto}
table{border-collapse:collapse;width:100%}
th,td{padding:6px 10px;border-bottom:1px solid var(--rule);text-align:left;white-space:nowrap}
th{color:var(--muted);font-weight:600}
td.num{text-align:right;font-variant-numeric:tabular-nums}
tbody tr:nth-child(even){background:var(--stripe)}
"""


# LLM: 数值列右对齐仅按值类型判断（int/float，非 bool），不从列名推断。
# 函数用途: 生成完整 HTML 文档的 UTF-8 字节。
def render_html(table: Table, title: str, chart: str | None, points: list[tuple[str, float]] | None) -> bytes:
    head = "".join(f"<th>{escape(name)}</th>" for name in table.columns)
    body = "".join("<tr>" + "".join(_cell(value) for value in row) + "</tr>" for row in table.rows)
    figure = _svg_chart(chart, points) if chart and points is not None else ""
    page = (f'<!DOCTYPE html>\n<html lang="zh-CN"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{escape(title)}</title><style>{_CSS}</style></head><body><main>"
            f'<h1>{escape(title)}</h1><p class="meta">{escape(table.summary)}</p>{figure}'
            f'<div class="wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'
            "</main></body></html>\n")
    return page.encode("utf-8")


# LLM: 单元格文本统一转义；数字加 num 类用于右对齐。
# 函数用途: 生成一个表格单元格。
def _cell(value: object) -> str:
    opening = '<td class="num">' if type(value) in (int, float) else "<td>"
    return f"{opening}{escape(cell_text(value))}</td>"


# LLM: 单系列横向条形图：一种颜色、无图例（标题说明列名），条长按最大值等比，每条带数值标签与 <title> 悬停提示；
#   不使用脚本。标签与数值都已转义。
# 函数用途: 生成内联 SVG 条形图的 <figure> 片段。
def _svg_chart(chart: str, points: list[tuple[str, float]]) -> str:
    width, label_w, value_w, step, bar_h = 720, 160, 90, 28, 18
    span = width - label_w - value_w
    top = max((value for _, value in points), default=0)
    rows = []
    for index, (label, value) in enumerate(points):
        y = 8 + index * step
        length = 0 if top == 0 else max(1 if value > 0 else 0, round(value / top * span))
        text, number = escape(label), escape(cell_text(value))
        rows.append(f"<g><title>{text}：{number}</title>"
                    f'<text x="{label_w - 8}" y="{y + 13}" text-anchor="end">{text}</text>'
                    f'<rect x="{label_w}" y="{y}" width="{length}" height="{bar_h}" rx="3"></rect>'
                    f'<text class="value" x="{label_w + length + 6}" y="{y + 13}">{number}</text></g>')
    height = 16 + len(points) * step
    return (f"<figure><figcaption>条形图：{escape(chart)}</figcaption>"
            f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{escape(chart)} 条形图">'
            f"{''.join(rows)}</svg></figure>")
