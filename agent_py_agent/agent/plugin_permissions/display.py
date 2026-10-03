# LLM: TUI/飞书共用纯文字投影，只读原安装表；路径仅在已鉴权管理员界面展示，不把授权当 OS 证明。
# 模块用途: 展示一次性兼容、有限授权和底座待接入的边界。
from __future__ import annotations

import json

from .state import legacy_permissions


# LLM: 根按 JSON 引号显示，防止路径换行伪装成确认命令；不启动或授予任何能力。
# 函数用途: 列出管理员将确认的读写、网络和额外程序宽度。
def permission_lines(details: dict) -> str:
    selected = details["permissions"]
    groups = (("工作区读", "read_roots"), ("工作区写", "write_roots"), ("额外程序/前缀", "program_roots"))
    lines = [f"{label}：" + ("、".join(json.dumps(row["path"], ensure_ascii=False) for row in selected[key]) or "未授予")
             for label, key in groups]
    lines.append("网络：这个插件能连网，也能连本机端口及监听（回环、公网、监听一起授权）。" if selected["network"]
                 else "网络：未授予；断网施加待统一 OS 底座接入。")
    lines.append("写根覆盖目录内其它子项，原子替换不等于只授权一个文件。桌面能力本批未支持。")
    if details["mode"] == "wide":
        lines.append("宽权限：不限制系统用户可读写范围，全部网络可用；有限根不是 OS 限制。")
    return "\n".join(lines)


# LLM: 缺字段不推导兼容；默认只用于未来授权说明，现有模式冻结，普通身份不泄露授权路径。
# 函数用途: 为同源管理列表和详情生成诚实的权限状态。
def permission_summary(entry, *, admin: bool, sandbox_default: bool = True) -> str:
    if entry.manifest.is_content_only or entry.manifest.permissions is not None:
        return ""
    grant = legacy_permissions(entry)
    if grant is None:
        return "权限：未授权（新启用按新规则）" if sandbox_default else "权限：未授权（新启用仍须显式宽权限确认）"
    if grant["mode"] == "legacy_compat":
        return "权限：兼容中（重新启用后按新规则）；旧权限未收紧，桌面功能沿旧激活已知边界。"
    status = "授权策略已收紧（OS 沙箱待 B7 接入，隔离未验证）" if grant["mode"] == "restricted" else "宽权限（管理员确认，未隔离）"
    return "权限：" + status + ("\n" + permission_lines(grant) if admin else "；授权详情仅管理员可见。")


# LLM: 同次安装快照与可信管理员身份来自管理服务；这里只渲染，不另读状态或初始化 Agent。
# 函数用途: 给 TUI 与飞书的列表生成同一份状态和权限文字。
def permission_listing(entries, admin: bool, sandbox_default: bool) -> str:
    return "\n".join(f"{row.manifest.plugin_id} {row.manifest.version}（{'启用' if row.enabled else '停用'}）  {row.manifest.summary}\n"
                     + permission_summary(row, admin=admin, sandbox_default=sandbox_default) for row in entries)
