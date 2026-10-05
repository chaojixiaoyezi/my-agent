# LLM: 管理员完整确认文本只投影结构化事实；与原 HostCommand 共用运输，不截断授权根、摘要或程序清单。
# 模块用途: 生成可直接回填的管理员权限确认命令和 TUI/飞书同源说明。
from __future__ import annotations

import json
import shlex

from .display import permission_lines

# 常量用途: 老插件授权失败的同源中文说明，均不能把授权事实当 OS 隔离或退出证明。
_PERMISSION_PROBLEM_MESSAGES = {
    "legacy_permission_missing": "插件缺少固定授权记录，本次没有启动；请重新取得完整预览后再启用。",
    "legacy_permission_invalid": "权限路径、程序身份或确认请求绑定无效，本次没有启用新代；请重新取得完整预览。",
    "legacy_permission_changed": "固定授权之后权限根、程序或解释器已变化，本次没有启动或发布新代；请先核对原准备/清理状态，再重新取得完整预览。",
    "previous_cleanup_unconfirmed": "旧代执行权已撤销，进程退出和清理未验证；本次未开放新代，原请求结果仍未确认。",
}


# LLM: 只按结构化 reason 选择说明，不根据异常正文猜状态，也不改变原工具或 UNKNOWN 结果。
# 函数用途: 让真实管理入口明确展示拒启动或旧代退出未确认的原因。
def permission_problem_message(reason) -> str:
    return _PERMISSION_PROBLEM_MESSAGES.get(reason, "") if isinstance(reason, str) else ""


# LLM: 参数经引号词法还原，不交 Shell 执行；原授权身份与码必须随所有四维原样回填。
# 函数用途: 为完整预览生成确定的下一次管理命令，不丢掉任何授权根。
def permission_confirm_command(details: dict) -> str:
    parts = ["/plugins", "enable", details["plugin_id"]]
    groups = (("read_roots", "--read-root"), ("write_roots", "--write-root"), ("program_roots", "--program-root"))
    for key, option in groups:
        for row in details["permissions"][key]:
            parts.extend((option, shlex.quote(row["path"])))
    if details["permissions"]["network"]:
        parts.append("--network")
    parts.extend(("--authorization", details["authorization_id"], "--authorization-revision", details["catalog_revision"],
                  "--confirm", details["confirm_code"]))
    return " ".join(parts)


# LLM: 沿原 C10“启用前需要你确认”入口合同；码位于完整事实之后，纯文本不代表真实 IM 完整送达。
# 函数用途: 为本地和 IM 输出完整权限预览，保留既有确认提示，不截断事实或路径。
def permission_confirmation_message(details: dict) -> str:
    lines = ["启用前需要你确认：管理员启用授权预览（确认后重新启用会撤销旧代，退出未确认时不开放新代）：",
             permission_lines(details), "收紧模式由统一 B7 沙箱底座在每次启动时施加；本机沙箱不可用时不会启动。",
             "固定确认事实（完整，不截断）：", json.dumps(
                 {key: value for key, value in details.items() if key not in {"confirm_code", "confirm_command"}},
                 ensure_ascii=False, sort_keys=True, indent=2), "确认无误后输入：" + details["confirm_command"]]
    return "\n".join(lines)
