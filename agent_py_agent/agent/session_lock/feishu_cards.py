# LLM: 密码动作只由 UnlockService 的结构化结果裁决；卡片标题、颜色和说明是展示，不决定认证或续送。
# 模块用途: 私聊表单收集个人解锁密码并渲染不含密码的回执，不把输入传给模型或聊天历史。
"""会话锁的飞书密码卡:输入框收密码(走 form_value 回调、不进聊天记录),+ 回调分发到 UnlockService。

卡片 JSON 结构与 my-agent 现有
feishu_card 一致(config/header/elements),可用现成 send_interactive_card 发。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .service import UnlockService

# 按钮 value 里标识密码动作的键(与 persona 卡的 token/choice 区分开,回调据此分发)。
PWD_ACTION_FIELD = "session_lock_action"
_MODE_ACTION = {"set": "pwd_set", "unlock": "pwd_unlock", "change": "pwd_change"}


# LLM: 表单提交值走私聊卡片回调，不放入消息正文；模式是显式控制动作，不能从普通聊天文字推断。
# 函数用途: 生成首设、解锁或改密卡，说明认证前暂存消息以及告警仍可接收。
def build_password_card(*, mode: str, user_id: str, reference: str = "") -> dict[str, Any]:
    """密码输入卡:用卡片输入框收密码,值走表单回调、不进聊天记录(防密码留痕)。

    mode:'set'(首次设)/ 'unlock'(解锁)/ 'change'(改密码,旧+新两个框)。
    提交后回调 action.form_value = {输入框name: 值};按钮 value 带 session_lock_action + user_id。"""
    titles = {"set": "🔒 设置解锁密码", "unlock": "🔑 解锁会话", "change": "🔄 更改解锁密码"}
    hints = {
        "set": "请先设置自己的解锁密码，完成后继续处理刚才的消息；未设置时不能聊天或操作，但仍可收到告警。"
        "**密码只在输入框里,不会出现在聊天记录。**\n"
        "要求:≥8 位,含大小写字母和数字。",
        "unlock": "会话因闲置锁定。在下面输入框输入你的解锁密码恢复(**不会进聊天记录**)。",
        "change": "改密码需**先输旧密码再输新的**(新密码 ≥8 位、含大小写字母和数字)。**都不进聊天记录。**",
    }
    action = _MODE_ACTION[mode]
    # 保留既有字段形态；input_type/password和show_icon的真实客户端兼容尚未验证，不能把mock送达当成已遮蔽。
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
        "value": {PWD_ACTION_FIELD: action, "user_id": user_id, "reference": reference},
    }
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": titles[mode]}, "template": "blue"},
        "elements": [
            {"tag": "markdown", "content": hints[mode]},
            {"tag": "form", "name": "pwd_form", "elements": [*inputs, submit]},
            {"tag":"action","actions":[{"tag":"button","type":"default","text":{"tag":"plain_text","content":"取消"},
                "value":{PWD_ACTION_FIELD:'pwd_cancel','user_id':user_id,'reference':reference}}]},
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
    return isinstance(value, dict) and str(value.get(PWD_ACTION_FIELD) or "") in {"pwd_set", "pwd_unlock", "pwd_change", "pwd_cancel"}


# LLM: 只携带密码服务的裁决与无秘密提示；展示模板不能反向变成授权状态。
# 类用途: 将密码动作是否成功与卡片样式分开，供回调决定是否继续消息。
@dataclass(frozen=True)
class PasswordActionOutcome:
    ok: bool
    message: str


# LLM: 只按显式 action 调用原密码服务，可能写密码哈希或用户活跃；不记录表单值，不返回原密码。
# 函数用途: 验证首设、解锁或改密，返回机器可用的成功事实和安全提示。
def password_action_outcome(
    service: UnlockService, value: dict[str, Any], form_value: dict[str, Any]
) -> PasswordActionOutcome:
    action = str(value.get(PWD_ACTION_FIELD) or "")
    user_id = str(value.get("user_id") or "")
    fv = form_value if isinstance(form_value, dict) else {}
    if not user_id:
        return PasswordActionOutcome(False, "无法识别用户,请重试。")
    if action == "pwd_set":
        ok, msg = service.set_password(user_id, str(fv.get("pwd") or ""))
    elif action == "pwd_unlock":
        ok = service.unlock(user_id, str(fv.get("pwd") or ""))
        msg = "✅ 已解锁,会话恢复。" if ok else "解锁失败:密码不正确(多次错误会临时锁定)。"
    elif action == "pwd_change":
        ok, msg = service.change_password(user_id, str(fv.get("old_pwd") or ""), str(fv.get("new_pwd") or ""))
    else:
        ok, msg = False, "未知的密码操作。"
    return PasswordActionOutcome(ok, msg)


# LLM: 卡片渲染只消费原密码结果，不供运行时读取颜色判定认证；实际密码副作用归 password_action_outcome。
# 函数用途: 为直接卡片调用方生成不含密码的已决展示，保持现有卡片响应格式。
def handle_password_action(
    service: UnlockService, value: dict[str, Any], form_value: dict[str, Any]
) -> dict[str, Any]:
    result = password_action_outcome(service, value, form_value)
    return build_password_resolved_card(ok=result.ok, message=result.message)


__all__ = [
    "PWD_ACTION_FIELD",
    "PasswordActionOutcome",
    "build_password_card",
    "build_password_resolved_card",
    "handle_password_action",
    "is_password_action",
    "password_action_outcome",
]
