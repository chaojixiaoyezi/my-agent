# LLM: 订阅账号的模型只从 Gateway discover（订阅目录）来，勾选后经唯一 save_model 入口逐个保存：接口固定 openai_responses、
#   上下文用目录值、其它字段全用默认；已添加的同名模型不再列出。不调用模型、不切换当前会话模型。
#   改动须同步 test_tui_subscription_models 与 docs/design/MODEL_OAUTH.md。
# 模块用途: 登录 ChatGPT 订阅后（或在账号菜单里）列出这个账号能用的模型，让用户勾选（可多选）后一次添加。
from __future__ import annotations

from uuid import uuid4

from prompt_toolkit.layout import HSplit
from prompt_toolkit.widgets import CheckboxList, Label

from .tui_model_menu import _dialog, _request, _request_data

_SWITCH_HINT = "在 /model →「选择已有模型（当前会话）」里切换。"
_LATER_HINT = "以后可在 /model →「登录认证」→ 这个账号 →「选择模型」里添加。"


# LLM: 目录读取失败只给可重试提示；已添加判断按同一服务商下的模型名，不按显示文字。
# 函数用途: 读取账号可用模型，弹出勾选框，把勾中的模型一次添加，返回给用户看的结果文字。
async def pick_subscription_models(app, agent, session: str, provider_id: str) -> str:
    catalog = await _request(app, agent, session, "discover", {"provider_id": provider_id})
    if not catalog.get("ok"):
        return f"没能读取这个账号的模型列表：{catalog.get('message') or '未知原因'} {_LATER_HINT}"
    listing = await _request_data(agent, session, "list", {})
    existing = {row.get("model_name") for row in listing.get("profiles", []) if row.get("provider_id") == provider_id}
    rows = [row for row in catalog.get("models", []) if row["model_name"] not in existing]
    if not rows:
        return "这个账号能用的模型都已添加。" + _SWITCH_HINT
    box = CheckboxList([(row["model_name"], _label(row)) for row in rows])
    picked = await _dialog(app, "选择要使用的模型（可多选）",
                           HSplit([Label("上下键移动，空格勾选；选好后按 Tab 到「添加」再回车。"), box]),
                           (("添加", lambda: list(box.current_values)), ("以后再说", None)), focus=box)
    if not picked:
        return "暂未添加模型。" + _LATER_HINT
    saved, failed = await _save_models(agent, session, provider_id, [row for row in rows if row["model_name"] in picked])
    text = f"已添加 {len(saved)} 个模型：{'、'.join(saved)}。" if saved else "没有添加成功的模型。"
    if failed:
        text += f"有 {len(failed)} 个没加上：{'、'.join(failed)}，可以再选一次。"
    return text + (_SWITCH_HINT if saved else "")


# LLM: 只展示显示名与上下文，模型名作为勾选值；显示名来自服务商目录，不参与任何判断。
# 函数用途: 生成一行勾选项文字，如「GPT-6-Luna · 上下文 272K」。
def _label(row: dict) -> str:
    return f"{row.get('display_name') or row['model_name']} · 上下文 {row['model_context_window_tokens'] // 1000}K"


# LLM: 每个模型用新的 UUID 经 save_model 保存（锁内原子写）；单个失败不影响其它，结果按回执的 ok 字段区分。
# 函数用途: 逐个保存勾选的模型，返回成功与失败的显示名列表。
async def _save_models(agent, session: str, provider_id: str, rows: list[dict]) -> tuple[list[str], list[str]]:
    saved, failed = [], []
    for row in rows:
        result = await _request_data(agent, session, "save_model", {"profile_id": str(uuid4()), "profile": {
            "provider_id": provider_id, "model_name": row["model_name"], "model_backend": row["model_backend"],
            "model_context_window_tokens": row["model_context_window_tokens"], "capability": "agentic", "enabled": True}})
        (saved if result.get("ok") else failed).append(row.get("display_name") or row["model_name"])
    return saved, failed
