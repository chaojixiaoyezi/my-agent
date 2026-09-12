# LLM: 共享必须由管理员点击确认，普通用户只可选择已发布引用；本菜单不接触 API Key、不写模型对话。
# 模块用途: 在 /model 显式发布或撤销跨用户可用模型，提示费用与现有会话影响。

from __future__ import annotations

from prompt_toolkit.layout import HSplit
from prompt_toolkit.widgets import Label, RadioList

from .tui_model_menu import _dialog, _request


# LLM: 管理权限以 Gateway 回执为准，UI 展示不是授权；保存必须用结构化 profile_id/enabled，不解析标签。
# 函数用途: 选择管理员自己的模型，再确认共享或撤销；不会默认共享全部模型。
async def manage_shared_models(app, agent, session_id: str) -> str:
    result = await _request(app, agent, session_id, "list")
    if not result.get("ok"):
        return str(result.get("message") or "共享目录读取失败。")
    if not result.get("can_share"):
        return "只有管理员可以管理共享；你仍可在“选择已有模型”中使用已开放的模型。"
    rows = [row for row in result["profiles"] if row["id"] != "default" and not row.get("shared")]
    if not rows:
        return "请先新增管理员自己的模型；部署默认项不需要在此发布。"
    choices = RadioList([(row["id"], f"{row['model_name']} · {row.get('provider_name', '')}"
                         f" · {'已共享' if row.get('shared_enabled') else '私有'}") for row in rows], select_on_focus=True)
    selected = await _dialog(app, "管理共享模型", choices,
                             (("进入", lambda: choices.current_value), ("返回", None)), focus=choices)
    if selected is None:
        return ""
    row = next(row for row in rows if row["id"] == selected)
    enabled = not row.get("shared_enabled", False)
    notice = ("开放后，其他用户可调用此模型，费用由该模型账号承担。密钥不会显示或复制给用户。"
              if enabled else "撤销后，已在执行的请求不中断；后续调用将提示重新选模型，不会自动换模型。")
    confirmed = await _dialog(app, "确认共享设置", HSplit([Label(row["model_name"]), Label(notice)]),
                              (("开放共享" if enabled else "撤销共享", True), ("取消", None)))
    if not confirmed:
        return ""
    result = await _request(app, agent, session_id, "set_shared", {"profile_id": selected, "enabled": enabled})
    if not result.get("ok"):
        return str(result.get("message") or "共享设置未确认，请重新读取列表。")
    return f"{row['model_name']} 已{'开放' if enabled else '撤销'}共享；所有会话的模型选择均未修改。"
