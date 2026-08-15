"""会话锁的飞书密码卡:输入框收密码(走 form_value 回调、不进聊天记录),+ 回调分发到 UnlockService。

卡片 JSON 结构与 my-agent 现有
feishu_card 一致(config/header/elements),可用现成 send_interactive_card 发。
"""

from __future__ import annotations

from typing import Any

from .service import UnlockService

# 按钮 value 里标识密码动作的键(与 persona 卡的 token/choice 区分开,回调据此分发)。
PWD_ACTION_FIELD = "session_lock_action"
_MODE_ACTION = {"set": "pwd_set", "unlock": "pwd_unlock", "change": "pwd_change"}


def build_password_card(*, mode: str, user_id: str) -> dict[str, Any]:
    """密码输入卡:用卡片输入框收密码,值走表单回调、不进聊天记录(防密码留痕)。

    mode:'set'(首次设)/ 'unlock'(解锁)/ 'change'(改密码,旧+新两个框)。
    提交后回调 action.form_value = {输入框name: 值};按钮 value 带 session_lock_action + user_id。"""
    titles = {"set": "🔒 设置解锁密码", "unlock": "🔑 解锁会话", "change": "🔄 更改解锁密码"}
    hints = {
        "set": "设个解锁密码保护会话(闲置锁定后防他人冒用)。**密码只在输入框里,不会出现在聊天记录。**\n"
        "要求:≥8 位,含大小写字母和数字。",
        "unlock": "会话因闲置锁定。在下面输入框输入你的解锁密码恢复(**不会进聊天记录**)。",
        "change": "改密码需**先输旧密码再输新的**(新密码 ≥8 位、含大小写字母和数字)。**都不进聊天记录。**",
    }
    action = _MODE_ACTION[mode]
    # input_type=password:飞书原生密文输入,内容显示为 • 圆点(不明文);show_icon 给眼睛图标可切换显隐。
    # 旧版卡片(config/header/elements)即支持此字段(飞书客户端 V6.8+)。
    def _pwd_input(name: str, label: str, placeholder: str) -> dict[str, Any]:
        return {"tag": "input", "name": name, "input_type": "password", "show_icon": True,
                "label": {"tag": "plain_text", "content": label},
                "placeholder": {"tag": "plain_text", "content": placeholder}}

    inputs: list[dict[str, Any]] = []
    if mode == "change":
        inputs.append(_pwd_input("old_pwd", "旧密码", "当前解锁密码"))
        inputs.append(_pwd_input("new_pwd", "新密码", "≥8位+大小写+数字"))
    else:
        inputs.append(_pwd_input("pwd", "密码", "在此输入,不会进聊天记录"))
    submit = {
        "tag": "button", "text": {"tag": "plain_text", "content": "✅ 提交"}, "type": "primary",
        "action_type": "form_submit", "name": "submit",
        "value": {PWD_ACTION_FIELD: action, "user_id": user_id},
    }
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": titles[mode]}, "template": "blue"},
        "elements": [
            {"tag": "markdown", "content": hints[mode]},
            {"tag": "form", "name": "pwd_form", "elements": [*inputs, submit]},
        ],
    }


def build_password_resolved_card(*, ok: bool, message: str) -> dict[str, Any]:
    """密码操作已决卡:替换原卡(绝不回显密码)。"""
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "✅ 已处理" if ok else "⚠️ 未通过"},
                   "template": "green" if ok else "grey"},
        "elements": [{"tag": "markdown", "content": message}],
    }


def is_password_action(value: dict[str, Any]) -> bool:
    """卡片回调 value 是不是密码卡动作(据此和 persona 等其它卡分流)。"""
    return isinstance(value, dict) and str(value.get(PWD_ACTION_FIELD) or "") in {"pwd_set", "pwd_unlock", "pwd_change"}


def handle_password_action(
    service: UnlockService, value: dict[str, Any], form_value: dict[str, Any]
) -> dict[str, Any]:
    """处理密码卡提交:据 action + form_value(密码输入)调 UnlockService。返回替换用的已决卡 JSON。
    密码只在 form_value 里、绝不落日志/聊天;失败给中文提示但不回显密码。"""
    action = str(value.get(PWD_ACTION_FIELD) or "")
    user_id = str(value.get("user_id") or "")
    fv = form_value if isinstance(form_value, dict) else {}
    if not user_id:
        return build_password_resolved_card(ok=False, message="无法识别用户,请重试。")
    if action == "pwd_set":
        ok, msg = service.set_password(user_id, str(fv.get("pwd") or ""))
    elif action == "pwd_unlock":
        ok = service.unlock(user_id, str(fv.get("pwd") or ""))
        msg = "✅ 已解锁,会话恢复。" if ok else "解锁失败:密码不正确(多次错误会临时锁定)。"
    elif action == "pwd_change":
        ok, msg = service.change_password(user_id, str(fv.get("old_pwd") or ""), str(fv.get("new_pwd") or ""))
    else:
        ok, msg = False, "未知的密码操作。"
    return build_password_resolved_card(ok=ok, message=msg)


__all__ = [
    "PWD_ACTION_FIELD",
    "build_password_card",
    "build_password_resolved_card",
    "handle_password_action",
    "is_password_action",
]
