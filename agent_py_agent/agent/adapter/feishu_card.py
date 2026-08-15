# LLM: 飞书交互卡片——改 SOUL/AGENTS 长期人设时,不直接写,发一张按钮确认卡片,用户点『确认』才写
#   (全程不阻塞主代理)。build_ 造卡片 JSON;send_ 发卡片(自取 tenant token,fail-open);
#   extract_card_action / extract_webhook_card_action 把长连接对象或 webhook JSON 归一成同一结构;
#   apply_card_action 是核心回调
#   逻辑(纯函数、可单测):confirm→pop 待确认记录+append 进对应人格文件,decline/找不到→丢弃不写。
#   幂等靠 persona_pending.pop 的原子领取(并发/重复回调只有一个能拿到记录→只写一次)。改动同步测试。
from __future__ import annotations

import json
import logging
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_API_BASE = "https://open.feishu.cn/open-apis"
_TIMEOUT_S = 10.0
_TARGET_LABEL = {"soul": "长期人设(SOUL)", "agents": "工作约定(AGENTS)"}
_TARGET_FIELD = {"soul": "soul_md", "agents": "agents_md"}


def build_persona_confirm_card(
    token: str,
    target: str,
    content: str,
    *,
    action: str = "add",
) -> dict[str, Any]:
    """构造『确认写入长期设定』交互卡片:一句话说明 + 【✅ 确认写入】【❌ 不写】两个按钮。
    按钮 value 带 {token, choice}(confirm/decline),回调据此定位待确认记录并落写/取消。"""
    label = _TARGET_LABEL.get(target, "长期设定")
    verb = {"add": "写入", "replace": "替换", "remove": "删除"}.get(action, "修改")
    subject = content or "所选条目"
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": f"确认{verb}长期设定"},
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"要在你的{label}中{verb} **{subject}** 吗?\n(长期设定每轮都生效,点了确认我才改。)",
                },
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": f"✅ 确认{verb}"},
                        "type": "primary",
                        "value": {"token": token, "choice": "confirm"},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "❌ 不写"},
                        "type": "default",
                        "value": {"token": token, "choice": "decline"},
                    },
                ],
            },
        ],
    }


def send_interactive_card(app_id: str, app_secret: str, open_id: str, card: dict[str, Any]) -> bool:
    """发交互卡片到 open_id(自取 tenant token,纯 urllib)。任何失败(缺凭据/网络/飞书报错)返回
    False 且不抛(fail-open):发不出去 → 上层回落"就地不写 + 提示用户"。"""
    if not (app_id and app_secret and open_id):
        return False
    try:
        token = _tenant_access_token(app_id, app_secret)
        if not token:
            return False
        body = {
            "receive_id": open_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        }
        resp = _post_json(
            f"{_API_BASE}/im/v1/messages?receive_id_type=open_id",
            body,
            headers={"Authorization": f"Bearer {token}"},
        )
        return resp.get("code") == 0
    except Exception as exc:
        logger.warning(f"飞书确认卡片发送失败(fail-open,不写不阻塞): {type(exc).__name__}: {exc}")
        return False


def extract_card_action(data: Any) -> dict[str, Any] | None:
    """lark 卡片回调对象 → 归一化 {value: dict, operator_open_id: str, form_value: dict}。防御式 getattr
    (兼容真 lark 对象与测试假对象);取不到 action.value(dict)视为无效返回 None。value 若为 JSON 串则解析。
    form_value 是表单提交(密码卡)的输入框值 {name: 值},普通按钮卡为空 dict。
    (lark 回调结构:data.event.action.value / .form_value / data.event.operator.open_id。)"""
    try:
        event = getattr(data, "event", None)
        if event is None:
            return None
        action = getattr(event, "action", None)
        value = _coerce_action_value(getattr(action, "value", None) if action is not None else None)
        if value is None:
            return None
        operator = getattr(event, "operator", None)
        operator_open_id = (
            str(getattr(operator, "open_id", "") or "") if operator is not None else ""
        )
        raw_form = getattr(action, "form_value", None) if action is not None else None
        form_value = raw_form if isinstance(raw_form, dict) else {}
        return {"value": value, "operator_open_id": operator_open_id, "form_value": form_value}
    except Exception:
        return None


def extract_webhook_card_action(payload: object) -> dict[str, Any] | None:
    """飞书 webhook 卡片回调 JSON → 与长连接相同的归一化结构。"""
    if not isinstance(payload, dict):
        return None
    event = payload.get("event") if isinstance(payload.get("event"), dict) else payload
    action = event.get("action") if isinstance(event.get("action"), dict) else None
    if action is None:
        return None
    value = _coerce_action_value(action.get("value"))
    if value is None:
        return None
    operator = event.get("operator") if isinstance(event.get("operator"), dict) else {}
    operator_id = (
        operator.get("operator_id") if isinstance(operator.get("operator_id"), dict) else {}
    )
    operator_open_id = str(
        operator.get("open_id") or operator_id.get("open_id") or event.get("open_id") or ""
    )
    form_value = action.get("form_value") if isinstance(action.get("form_value"), dict) else {}
    return {
        "value": value,
        "operator_open_id": operator_open_id,
        "form_value": form_value,
    }


def _coerce_action_value(value: Any) -> dict[str, Any] | None:
    """action.value 归一成 dict:已是 dict 原样;JSON 串则解析;其余(含解析失败)→ None。"""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None


def apply_card_action(
    value: dict[str, Any],
    my_agent_home: str | Path,
    operator_open_id: str = "",
) -> dict[str, Any]:
    """核心回调逻辑(纯函数、可单测):按 {token, choice} 落写或取消。
      - choice=confirm 且 token 有效 → 原子 pop 待确认记录 → append 进 owner 的 SOUL/AGENTS.md;
      - choice=decline / token 找不到(过期/已处理/重复回调) → 丢弃、不写。
    返回 {"wrote": bool, "owner_id": str|None, "reply_text": str}。幂等靠 pop 的原子领取(重复回调
    第二次 pop 拿不到记录→不重复写)。全程 fail-open,不抛。"""
    from ..capability import persona_pending

    token = str((value or {}).get("token") or "").strip()
    choice = str((value or {}).get("choice") or "").strip().lower()
    if not token:
        return {"wrote": False, "owner_id": None, "reply_text": ""}
    pending = persona_pending.load(my_agent_home, token)
    if pending is None:
        return {
            "wrote": False,
            "owner_id": None,
            "reply_text": "这条确认已经失效了(可能超时或已处理过),没有改动。",
        }
    operator = str(operator_open_id or "").strip()
    if not operator or operator != pending.owner_id:
        return {
            "wrote": False,
            "owner_id": None,
            "reply_text": "只有这条长期设定的发起人可以确认或取消；待确认内容没有改动。",
        }
    if choice != "confirm":
        persona_pending.pop(my_agent_home, token)  # 明确取消:丢弃待确认记录(在的话)
        return {"wrote": False, "owner_id": None, "reply_text": "好的,已取消,没有改动。"}
    record = persona_pending.pop(my_agent_home, token)  # 原子领取(幂等基石)
    if record is None:
        return {
            "wrote": False,
            "owner_id": None,
            "reply_text": "这条确认已经失效了(可能超时或已处理过),没有改动。",
        }
    if not _write_confirmed_persona(record, my_agent_home):
        return {
            "wrote": False,
            "owner_id": record.owner_id,
            "reply_text": "写入的时候出了点问题,没能保存,你可以再让我改一次。",
        }
    label = _TARGET_LABEL.get(record.target, "长期设定")
    return {
        "wrote": True,
        "owner_id": record.owner_id,
        "reply_text": f"✅ 已写进你的{label}:{record.content}",
    }


def _write_confirmed_persona(record: Any, my_agent_home: str | Path) -> bool:
    """Resolve the token-bound owner and commit through the Persona repository."""
    from ..capability.persona_repository import PersonaMutationRequest, PersonaRepository
    from ..user_space.owner_resolver import OwnerIdentity, resolve_owner_home

    if record.target not in _TARGET_FIELD:
        return False
    identity = _identity_from_record(record, OwnerIdentity)
    owner = resolve_owner_home(my_agent_home, identity)
    try:
        repository = PersonaRepository.from_owner_result(owner)
        repository.mutate(
            PersonaMutationRequest(
                target=str(record.target),
                action=str(getattr(record, "action", "add") or "add"),
                content=str(record.content or ""),
                entry_id=str(getattr(record, "entry_id", "") or ""),
                confirmed=True,
                expected_sha256=str(getattr(record, "expected_sha256", "") or ""),
                rollback_version=getattr(record, "rollback_version", None),
                source="feishu_confirmation",
            )
        )
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _identity_from_record(record: Any, identity_cls: Any) -> Any:
    if record.owner_kind == "group":
        return identity_cls.provider_group(record.owner_provider, record.owner_id)
    return identity_cls.provider_user(record.owner_provider, record.owner_id)


def _tenant_access_token(app_id: str, app_secret: str) -> str | None:
    resp = _post_json(
        f"{_API_BASE}/auth/v3/tenant_access_token/internal",
        {"app_id": app_id, "app_secret": app_secret},
    )
    token = resp.get("tenant_access_token")
    return str(token) if token else None


def _post_json(
    url: str, body: dict[str, Any], headers: dict[str, str] | None = None
) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    hdrs = {"Content-Type": "application/json; charset=utf-8"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


__all__ = [
    "apply_card_action",
    "build_persona_confirm_card",
    "extract_card_action",
    "extract_webhook_card_action",
    "send_interactive_card",
]
