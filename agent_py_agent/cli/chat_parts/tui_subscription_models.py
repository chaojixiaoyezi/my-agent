# LLM: 模型只从 Gateway discover（服务商目录或订阅目录）来，勾选后经唯一 add_models 入口一次保存（全成或全不写）；
#   订阅账号接口固定 openai_responses、上下文用目录值；目录没写上下文的模型用勾选框里填的统一值，不按模型名猜。
#   已添加的同名模型不再列出（订阅账号），或由 add_models 跳过（新连接）。不调用模型、不切换当前会话模型。
#   改动须同步 test_tui_subscription_models、test_tui_model_add 与 docs/design/MODEL_OAUTH.md。
# 模块用途: 列出可用模型让用户勾选（可多选）后一次添加；登录 ChatGPT 订阅后和「新增模型」拉到列表后都用这里的勾选框。
from __future__ import annotations

from uuid import uuid4

from prompt_toolkit.layout import HSplit
from prompt_toolkit.widgets import CheckboxList, Label, TextArea

from .tui_model_menu import _dialog, _request, _request_data

_SWITCH_HINT = "在 /model →「选择模型」→「对话模型」里切换。"
_LATER_HINT = "以后可在 /model →「管理已有模型」→ 这个账号 →「添加模型」里添加。"
_MIN_WINDOW, _MAX_WINDOW = 4096, 2**31 - 1


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
    picked = await choose_models(app, rows, "选择要使用的模型（可多选）")
    if not picked:
        return "暂未添加模型。" + _LATER_HINT
    result = await _request(app, agent, session, "add_models", {
        "provider_id": provider_id, "model_backend": "openai_responses", "models": with_ids(picked)})
    return added_text(result, picked)


# LLM: 勾选值是模型名，显示文字只给人看；返回勾中的行（上下文缺失的行换成用户填的统一值），取消返回 None。
#   上下文只在列表里有缺失时才显示输入框；数值合法性最终由服务端 validate_model 判定。
# 函数用途: 弹出"上下键移动、空格勾选"的多选框，返回要添加的模型行。
async def choose_models(app, rows: list[dict], title: str) -> list[dict] | None:
    box = CheckboxList([(row["model_name"], _label(row)) for row in rows])
    window = TextArea(text="128000", height=1, multiline=False)
    parts = [Label("上下键移动，空格勾选；选好后按 Tab 到「添加」再回车。"), box]
    if not all(_window_known(row) for row in rows):
        parts += [Label("列表没写上下文窗口的模型按这个值保存（总 tokens，之后可在「管理已有模型」里逐个改）："), window]
    picked = await _dialog(app, title, HSplit(parts), (("添加", lambda: list(box.current_values)), ("返回", None)), focus=box)
    if not picked:
        return None
    return [{"model_name": row["model_name"], "display_name": row.get("display_name") or row["model_name"],
             "model_context_window_tokens": row["model_context_window_tokens"] if _window_known(row) else window.text.strip()}
            for row in rows if row["model_name"] in picked]


# LLM: 每个模型配一个新 UUID，重试时由 add_models 按同名同接口跳过，不会重复添加。
# 函数用途: 把勾选行整理成 add_models 需要的模型条目。
def with_ids(rows: list[dict]) -> list[dict]:
    return [{"profile_id": str(uuid4()), "model_name": row["model_name"],
             "model_context_window_tokens": row["model_context_window_tokens"]} for row in rows]


# LLM: 只按回执的 ok 与 added_models 字段组织文字；失败时整批都没写入（add_models 同次落盘）。
# 函数用途: 把一次批量添加的结果写成给用户看的一句话。
def added_text(result: dict, picked: list[dict]) -> str:
    if not result.get("ok"):
        return f"没有添加：{result.get('message') or '保存结果未知，请重新打开列表确认。'}"
    added = set(result.get("added_models") or [])
    names = [row["display_name"] for row in picked if row["model_name"] in added]
    skipped = len(picked) - len(names)
    text = f"已添加 {len(names)} 个模型：{'、'.join(names)}。" if names else "这些模型之前都已添加过。"
    if names and skipped:
        text += f"另有 {skipped} 个之前已添加，未重复添加。"
    return text + _SWITCH_HINT


# LLM: 上下文合法范围与 validate_model 一致；0 或缺失表示服务商没写，不能当作真实容量。
# 函数用途: 判断一行的上下文窗口是否是服务商给出的有效值。
def _window_known(row: dict) -> bool:
    value = row.get("model_context_window_tokens")
    return isinstance(value, int) and not isinstance(value, bool) and _MIN_WINDOW <= value <= _MAX_WINDOW


# LLM: 只展示显示名与上下文，模型名作为勾选值；显示名来自服务商目录，不参与任何判断。
# 函数用途: 生成一行勾选项文字，如「GPT-6-Luna · 上下文 272K」。
def _label(row: dict) -> str:
    window = f"上下文 {row['model_context_window_tokens'] // 1000}K" if _window_known(row) else "上下文未写明"
    return f"{row.get('display_name') or row['model_name']} · {window}"
