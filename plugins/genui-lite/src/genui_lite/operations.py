# LLM: 两个工具的业务流程；table 只读工作区；export 是唯一写工作区的入口，顺序固定为：
#   读取并渲染（先把输入错误全部暴露）→ 校验输出名 → 写入上下文 check → anchor 结果必须等于 lexical 路径 →
#   已存在且未带 overwrite 拒绝 → SDK no-follow 原子替换写出（0o644）。
# 模块用途: 实现表格渲染与 HTML 导出的具体步骤与结果结构。

from __future__ import annotations

import os
from pathlib import Path

from my_agent_plugin_api.nofollow_fs import NoFollowPathError, write_bytes_atomic_beneath
from my_agent_plugin_api.workspace_read_context import WorkspaceReadContext
from my_agent_plugin_api.workspace_write_context import WorkspaceWriteContext

from .html_page import render_html
from .tables import (
    GenuiError,
    Table,
    chart_values,
    display_path,
    markdown_table,
    parse_table,
    read_input,
    text_bar_chart,
    workspace_target,
)

OUTPUT_FILE_MODE = 0o644


# LLM: 读取授权与大小/行数上限都在这里汇合；返回的 Table 已按 max_rows 截断。
# 函数用途: 读取工作区 JSON 并解析成表格。
def load_table(context: WorkspaceReadContext, path: str, settings: dict) -> tuple[Path, Table]:
    target = workspace_target(context, path)
    return target, parse_table(read_input(target, settings["max_input_bytes"]), settings["max_rows"])


# LLM: 只读，不写任何文件；chart 为 None 时不返回 chart 字段。
# 函数用途: 生成 Markdown 表格、可选文字条形图和行列计数摘要。
def render_table(context: WorkspaceReadContext, path: str, chart: str | None, settings: dict) -> dict:
    target, table = load_table(context, path, settings)
    result = {"path": display_path(context.cwd, target), "rows": table.total_rows, "columns": len(table.columns),
              "shown_rows": len(table.rows), "truncated": table.truncated, "markdown": markdown_table(table)}
    if chart is not None:
        result["chart"] = text_bar_chart(chart_values(table, chart))
    result["summary"] = table.summary
    return result


# LLM: 有副作用：在工作区写一个 HTML 文件。输出路径只经写入上下文裁决；anchor 与 lexical 路径不一致说明路径链含链接，拒绝。
#   存在性检查与原子替换之间仍有并发窗口（SDK 没有“仅新建”原语），overwrite=False 是尽力拒绝。
# 函数用途: 把工作区 JSON 数据导出为独立 HTML 文件。
def export_html(context: WorkspaceReadContext, write: WorkspaceWriteContext, arguments: dict, settings: dict) -> dict:
    source, table = load_table(context, arguments["path"], settings)
    chart = arguments.get("chart")
    points = chart_values(table, chart) if chart is not None else None
    output = arguments["output"]
    if "\x00" in output or ".." in Path(output).parts or not output.lower().endswith(".html") or Path(output).name == ".html":
        raise GenuiError("INVALID_OUTPUT", "输出路径必须以 .html 结尾，且不能含 .. 上溯组件。")
    target = write.cwd / output
    decision = write.check(target)
    if not decision.allowed:
        raise GenuiError(decision.code, "输出不在本次允许写入的工作区范围内。")
    root, parts = write.anchor(target)
    if root.joinpath(*parts) != target:
        raise NoFollowPathError("write anchor differs from lexical target")
    shown = display_path(write.cwd, target)
    if os.path.lexists(target) and not arguments["overwrite"]:
        raise GenuiError("OUTPUT_EXISTS", "输出文件已存在；确认要覆盖时请加 --overwrite。", output=shown)
    title = arguments.get("title") or f"数据表：{source.name}"
    payload = render_html(table, title, chart, points)
    write_bytes_atomic_beneath(root, parts, payload, directory_mode=0o755, file_mode=OUTPUT_FILE_MODE)
    return {"output": shown, "bytes": len(payload), "rows": table.total_rows, "columns": len(table.columns),
            "shown_rows": len(table.rows), "truncated": table.truncated, "summary": table.summary}
