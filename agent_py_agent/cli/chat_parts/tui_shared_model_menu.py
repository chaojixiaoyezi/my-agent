# LLM: 共享与「其他用户的初始模型」必须由管理员点击确认，普通用户只可选择已发布引用；本菜单不接触 API Key、不写模型对话。
#   权限以 Gateway 回执为准（can_share / 保存结果），界面展示不是授权；改动须同步 test_shared_initial_model。
# 模块用途: 在 /model →「默认模型与共享」里显式发布或撤销跨用户可用模型，并指定其他用户没选过模型时用哪个。

from __future__ import annotations

from prompt_toolkit.layout import HSplit
from prompt_toolkit.widgets import Label, RadioList

from .tui_model_menu import _dialog, _request

_ADMIN_ONLY = "只有管理员可以设置；你仍可在「选择模型 → 对话模型」里使用已开放的模型。"


# LLM: 只列管理员自己的 API Key 聊天模型（订阅/OAuth 账号不能跨用户共享，服务端也会拒绝）；共享行和部署默认行不列。
# 函数用途: 取出管理员可以共享或设为初始模型的模型行；读取失败或无权限时返回提示文字。
async def _own_rows(app, agent, session_id: str) -> tuple[list[dict], dict, str]:
    result = await _request(app, agent, session_id, "list")
    if not result.get("ok"):
        return [], result, str(result.get("message") or "共享目录读取失败。")
    if not result.get("can_share"):
        return [], result, _ADMIN_ONLY
    rows = [row for row in result["profiles"] if row["id"] != "default" and not row.get("shared")
            and row.get("capability", "agentic") == "agentic" and row.get("auth_mode", "api_key") == "api_key"]
    return rows, result, "" if rows else "请先新增管理员自己的 API Key 模型；订阅账号不能给其他用户用。"


# LLM: 保存必须用结构化 profile_id/enabled，不解析标签；撤销初始模型的共享会同时清除初始模型（服务端同次保存）。
# 函数用途: 选择管理员自己的模型，再确认共享或撤销；不会默认共享全部模型。
async def manage_shared_models(app, agent, session_id: str) -> str:
    rows, _result, problem = await _own_rows(app, agent, session_id)
    if problem:
        return problem
    choices = RadioList([(row["id"], f"{row['model_name']} · {row.get('provider_name', '')}"
                         f" · {'已共享' if row.get('shared_enabled') else '私有'}"
                         f"{' · 其他用户初始模型' if row.get('initial_for_others') else ''}") for row in rows], select_on_focus=True)
    selected = await _dialog(app, "共享给其他用户", choices,
                             (("进入", lambda: choices.current_value), ("返回", None)), focus=choices)
    if selected is None:
        return ""
    row = next(row for row in rows if row["id"] == selected)
    enabled = not row.get("shared_enabled", False)
    notice = ("开放后，其他用户可调用此模型，费用由该模型账号承担。密钥不会显示或复制给用户。"
              if enabled else "撤销后，已在执行的请求不中断；后续调用将提示重新选模型，不会自动换模型。")
    if not enabled and row.get("initial_for_others"):
        notice += "\n它也是其他用户的初始模型：撤销后初始模型一并清除，没选过模型的用户回到部署默认。"
    confirmed = await _dialog(app, "确认共享设置", HSplit([Label(row["model_name"]), Label(notice)]),
                              (("开放共享" if enabled else "撤销共享", True), ("取消", None)))
    if not confirmed:
        return ""
    result = await _request(app, agent, session_id, "set_shared", {"profile_id": selected, "enabled": enabled})
    if not result.get("ok"):
        return str(result.get("message") or "共享设置未确认，请重新读取列表。")
    return f"{row['model_name']} 已{'开放' if enabled else '撤销'}共享；所有会话的模型选择均未修改。"


# LLM: 设置时服务端同次把该模型开放共享（set_initial）；空值清除。只影响没自己选过模型（选择仍是「默认」）的其他用户，
#   管理员自己的默认不变；用户自己选过的模型不被覆盖。
# 函数用途: 指定或清除「其他用户的初始模型」。
async def choose_initial_model(app, agent, session_id: str) -> str:
    rows, result, problem = await _own_rows(app, agent, session_id)
    if problem:
        return problem
    current = str(result.get("initial_profile") or "")
    options = [("", f"{'● ' if not current else ''}不指定（用部署默认）"),
               *[(row["id"], f"{'● ' if row.get('initial_for_others') else ''}{row['model_name']} · {row.get('provider_name', '')}"
                  f"{'' if row.get('shared_enabled') else ' · 会同时开放共享'}") for row in rows]]
    choices = RadioList(options, select_on_focus=True)
    notice = "只影响没自己选过模型的其他用户（包括飞书等 IM 用户）；他们自己选过的模型不变，你自己的默认也不变。"
    selected = await _dialog(app, "其他用户的初始模型", HSplit([Label(notice), choices]),
                             (("保存", lambda: choices.current_value), ("返回", None)), focus=choices)
    if selected is None:
        return ""
    result = await _request(app, agent, session_id, "set_initial", {"profile_id": selected})
    if not result.get("ok"):
        return str(result.get("message") or "初始模型设置未确认，请重新读取列表。")
    name = next((row["model_name"] for row in rows if row["id"] == selected), "")
    return f"其他用户的初始模型已设为 {name}（已开放共享）。" if name else "已清除其他用户的初始模型，没选过模型的用户用部署默认。"
