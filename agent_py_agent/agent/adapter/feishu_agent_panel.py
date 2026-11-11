"""飞书交互式任务与子代理控制面板：HMAC 绑定、分桶状态、私聊/群聊分流与 14 天换卡。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..common.authenticated_http import open_authenticated_request
from ..common.json_io import (
    locked_json_path,
    read_json_object_report,
    write_private_json_file_atomic_unlocked,
)
from ..common.nofollow_fs import ensure_private_dir
from ..gateway_parts.client_credentials import GatewayClientCredentials
from ..user_space.interaction_refs import interaction_key
from ..user_space.owner_resolver import OwnerIdentity, resolve_owner_home
from .feishu_progress_card import render_progress_card
from .private_controls import GroupMembershipError, _current_group_member
from .protocol import FeishuEventDisposition

logger = logging.getLogger(__name__)

# 飞书卡片最长更新生命周期（14 天减 5 分钟安全缓冲）
FEISHU_CARD_MAX_AGE_SECONDS = 14 * 86400 - 300
_DOMAIN_SEP = b"feishu-panel-v1\0"
_PANEL_BINDING_SCHEMA = "feishu_panel_binding.v1"
_PANEL_STATE_SCHEMA = "feishu_panel_state.v1"
_PANEL_CMD_REGEX = re.compile(r"^/(agents|panel)(?:\s+(.*))?$", re.IGNORECASE)


def sign_panel_binding(owner_home: Path, claims: dict[str, Any]) -> str:
    """对飞书面板声明生成 HMAC 签名令牌（fp1.<base64url>.<sha256_hex>）。"""
    raw = json.dumps(claims, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    key = interaction_key(owner_home, create=True)
    signature = hmac.new(key, _DOMAIN_SEP + raw, hashlib.sha256).hexdigest()
    return f"fp1.{encoded}.{signature}"


def verify_panel_binding(home_root: Path, binding_ref: str) -> dict[str, Any]:
    """验证飞书面板 HMAC 令牌并解析声明字典。"""
    if not isinstance(binding_ref, str) or len(binding_ref) > 8192:
        raise ValueError("面板引用格式无效")
    parts = binding_ref.split(".")
    if len(parts) != 3:
        raise ValueError("面板引用格式无效")
    prefix, encoded, signature = parts
    if prefix != "fp1" or re.fullmatch(r"[0-9a-f]{64}", signature) is None:
        raise ValueError("面板引用版本无效")
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        claims = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError("面板引用解码失败") from exc
    if not isinstance(claims, dict) or claims.get("schema") != _PANEL_BINDING_SCHEMA:
        raise ValueError("面板引用声明无效")
    if claims.get("channel") != "feishu":
        raise ValueError("面板引用渠道不匹配")
    owner = claims.get("owner")
    if not isinstance(owner, dict) or set(owner) != {"provider", "owner_kind", "owner_id"}:
        raise ValueError("面板引用归属无效")
    identity = OwnerIdentity(**owner)
    resolved = resolve_owner_home(home_root, identity)
    key = interaction_key(resolved.home_dir, create=False)
    expected = hmac.new(key, _DOMAIN_SEP + raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("面板引用签名无效")
    return claims


def _panel_state_path(owner_home: Path, binding_id: str) -> Path:
    clean_id = "".join(c for c in binding_id if c in "0123456789abcdefABCDEF")
    bucket = (clean_id[:2] if len(clean_id) >= 2 else "00").lower()
    return owner_home / "data" / "feishu_panels" / bucket / f"{clean_id}.json"


def _read_panel_state(state_path: Path) -> dict[str, Any] | None:
    if not state_path.is_file():
        return None
    report = read_json_object_report(state_path, context="feishu_panel_state")
    if report.load_error or not isinstance(report.payload, dict):
        return None
    return report.payload


def _write_panel_state(state_path: Path, payload: dict[str, Any]) -> None:
    ensure_private_dir(state_path.parent)
    write_private_json_file_atomic_unlocked(state_path, payload)


def _gateway_channel_metadata(base_chat_id: str, chat_type: str) -> dict[str, str]:
    return {
        "channel_chat_type": chat_type,
        "channel_chat_id": base_chat_id,
        "feishu_chat_type": chat_type,
        "feishu_chat_id": base_chat_id,
    }


def _valid_card_receipt(receipt: object) -> bool:
    """核验证书回执是否有效（包含非空 message_id 与 chat_id）。"""
    return (
        isinstance(receipt, dict)
        and bool(str(receipt.get("message_id") or "").strip())
        and bool(str(receipt.get("chat_id") or "").strip())
    )


def _call_gateway_api(
    adapter: Any,
    path: str,
    body: dict[str, Any],
    actor: str,
    origin_chat_type: str,
    *,
    base_chat_id: str = "",
    timeout: float = 0.85,
) -> tuple[int, dict[str, Any] | None]:
    port = getattr(adapter, "_gateway_port", 0)
    home_dir = getattr(adapter, "my_agent_home", None)
    if not port or not home_dir:
        return 503, None
    try:
        credentials = GatewayClientCredentials(
            Path(home_dir),
            getattr(adapter, "_gateway_auth_token", ""),
        )
        effective_base_chat_id = base_chat_id or str(
            ((body.get("metadata") or {}) if isinstance(body, dict) else {}).get("channel_chat_id") or ""
        )
        headers = {
            "Content-Type": "application/json",
            "X-Channel": "feishu",
            "X-User-Id": actor,
            "X-Channel-Chat-Type": origin_chat_type,
            **credentials.headers(),
        }
        if effective_base_chat_id:
            headers["X-Channel-Chat-Id"] = effective_base_chat_id
    except Exception:
        return 401, None

    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with open_authenticated_request(req, timeout=timeout) as resp:
            content = resp.read(65536).decode("utf-8")
            payload = json.loads(content) if content else {}
            return resp.status, payload
    except urllib.error.HTTPError as exc:
        try:
            content = exc.read(65536).decode("utf-8")
            payload = json.loads(content) if content else {}
        except Exception:
            payload = None
        finally:
            exc.close()
        return exc.code, payload
    except (TimeoutError, urllib.error.URLError, OSError, ValueError):
        return 504, None


def is_panel_command(text: str) -> bool:
    """匹配 /agents 或 /panel 指令（支持可选参数）。"""
    if not isinstance(text, str):
        return False
    return bool(_PANEL_CMD_REGEX.match(text.strip()))


def parse_panel_command_arg(text: str) -> str:
    """提取 /agents 或 /panel 后续的目标编号参数。"""
    if not isinstance(text, str):
        return ""
    m = _PANEL_CMD_REGEX.match(text.strip())
    if not m:
        return ""
    return str(m.group(2) or "").strip()


def build_panel_view(
    panel_data: dict[str, Any] | None,
    binding_ref: str,
    record: dict[str, Any],
    *,
    next_revision: int = 1,
) -> dict[str, Any]:
    """根据 Gateway 任务投影与当前面板状态构建 WS-F3 panel_view。"""
    audience = str(record.get("audience") or "dm")
    expired = record.get("expired") is True
    expired_notice = str(record.get("expired_notice") or "")
    if expired and not expired_notice:
        expired_notice = "该旧面板已停用，请使用最新面板卡片操作。"

    selected_task = (panel_data or {}).get("selected_task") if isinstance(panel_data, dict) else None
    tasks = (panel_data or {}).get("tasks", []) if isinstance(panel_data, dict) else []
    nodes = (panel_data or {}).get("nodes", []) if isinstance(panel_data, dict) else []
    selected_agent_view = (panel_data or {}).get("selected_agent_view") if isinstance(panel_data, dict) else None

    task_title = ""
    task_status_label = ""
    coordinator_label = ""
    if isinstance(selected_task, dict):
        task_title = str(selected_task.get("title") or "").strip()
        status_val = str(selected_task.get("status") or "").strip().lower()
        task_status_label = {
            "queued": "排队中",
            "running": "运行中",
            "done": "已完成",
            "failed": "失败",
            "cancelled": "已取消",
        }.get(status_val, status_val or "进行中")
        if selected_task.get("task_kind") == "delegated_session_task":
            coordinator_label = "后台协调主代理"
        else:
            coordinator_label = "当前会话主任务"

    branch_counts_text = ""
    subagent_nodes = [n for n in nodes if isinstance(n, dict) and n.get("node_kind") == "subagent"]
    if subagent_nodes:
        running_count = sum(1 for n in subagent_nodes if not n.get("terminal"))
        done_count = sum(1 for n in subagent_nodes if n.get("terminal"))
        branch_counts_text = f"子代理：{running_count} 个运行 / {done_count} 个已结束"

    task_selector = None
    if not expired and isinstance(tasks, list) and len(tasks) > 1:
        t_options = []
        for t in tasks:
            if isinstance(t, dict):
                t_ref = str(t.get("task_ref") or "")
                t_tit = str(t.get("title") or t_ref)[:24]
                t_st = str(t.get("status") or "")
                opt_text = f"{t_tit} ({t_st})"
                t_options.append({"text": opt_text, "label": opt_text, "value": t_ref})
        if t_options:
            sel_t_ref = str(selected_task.get("task_ref") or "") if isinstance(selected_task, dict) else ""
            task_selector = {
                "placeholder": "选择任务",
                "options": t_options[:20],
                "selected_value": sel_t_ref,
                "callback_value": {
                    "kind": "my_agent_panel_action",
                    "action": "task.select",
                    "binding_ref": binding_ref,
                    "display_revision": next_revision,
                },
            }

    agent_selector = None
    selected_target_ref = str(record.get("selected_target_ref") or "all_summary")
    if not expired and audience != "group_public":
        a_options = [{"text": "任务全部汇总", "label": "任务全部汇总", "value": "all_summary"}]
        for n in nodes:
            if isinstance(n, dict):
                nid = str(n.get("node_id") or "")
                ntit = str(n.get("title") or nid)[:20]
                nst = str(n.get("status") or "")
                opt_text = f"{ntit} ({nst})"
                a_options.append({"text": opt_text, "label": opt_text, "value": nid})
        agent_selector = {
            "placeholder": "选择代理分支",
            "options": a_options[:20],
            "selected_value": selected_target_ref,
            "callback_value": {
                "kind": "my_agent_panel_action",
                "action": "agent.select",
                "binding_ref": binding_ref,
                "display_revision": next_revision,
            },
        }

    selected_target: dict[str, Any] = {}
    target_node = None
    if selected_target_ref.startswith("run:"):
        target_run_id = selected_target_ref[4:]
        target_node = next((n for n in nodes if isinstance(n, dict) and n.get("run_id") == target_run_id), None)
    elif selected_target_ref.startswith("main:"):
        target_node = next((n for n in nodes if isinstance(n, dict) and n.get("node_id") == selected_target_ref), None)

    if target_node is not None and target_node.get("node_kind") == "subagent":
        agent_obj = (selected_agent_view or {}).get("agent") if isinstance(selected_agent_view, dict) else {}
        if not isinstance(agent_obj, dict):
            agent_obj = {}
        res_summary = str(
            (selected_agent_view or {}).get("final_response")
            or agent_obj.get("result_summary")
            or target_node.get("result_summary")
            or ""
        ).strip()
        target_artifacts = target_node.get("artifact_refs") or agent_obj.get("artifacts") or ()
        art_lines = []
        for a in target_artifacts:
            if isinstance(a, str) and a.strip():
                art_lines.append(f"- {a.strip()}")
            elif isinstance(a, dict) and a.get("path"):
                art_lines.append(f"- {str(a['path']).strip()}")
        art_summary = "\n".join(art_lines)

        current_action = str(
            agent_obj.get("activity")
            or agent_obj.get("current_action")
            or target_node.get("activity")
            or ""
        ).strip()
        latest_progress = str(
            target_node.get("latest_progress")
            or agent_obj.get("latest_progress")
            or agent_obj.get("description")
            or ""
        ).strip()
        sub_status = str(target_node.get("status") or "")

        selected_target = {
            "target_kind": "subagent",
            "title": str(target_node.get("title") or target_run_id),
            "parent_label": str(target_node.get("parent_run_id") or "主任务"),
            "status_label": sub_status,
            "current_action": current_action,
            "latest_progress": latest_progress,
            "control_status": str(record.get("last_control_status") or ""),
        }
        if audience != "group_public":
            selected_target["result_summary"] = res_summary
            selected_target["artifacts_summary"] = art_summary
    elif target_node is not None:
        selected_target = {
            "target_kind": str(target_node.get("node_kind") or "background_main"),
            "title": str(target_node.get("title") or "主协调任务"),
            "status_label": str(target_node.get("status") or ""),
            "control_status": str(record.get("last_control_status") or ""),
        }
    else:
        selected_target = {
            "target_kind": "overview",
            "title": task_title or "任务总览",
            "status_label": task_status_label,
            "latest_progress": f"共 {len(nodes)} 个节点" if nodes else "就绪",
            "control_status": str(record.get("last_control_status") or ""),
        }

    actions: list[dict[str, Any]] = []
    if not expired:
        if audience == "group_public":
            actions.append({
                "id": "refresh",
                "label": "刷新摘要",
                "style": "default",
                "type": "default",
                "value": {
                    "kind": "my_agent_panel_action",
                    "action": "panel.refresh",
                    "binding_ref": binding_ref,
                    "display_revision": next_revision,
                },
            })
            actions.append({
                "id": "open_dm",
                "label": "私聊查看详情与控制",
                "style": "primary",
                "type": "primary",
                "value": {
                    "kind": "my_agent_panel_action",
                    "action": "panel.open_dm_detail",
                    "binding_ref": binding_ref,
                    "display_revision": next_revision,
                },
            })
        else:
            actions.append({
                "id": "refresh",
                "label": "刷新状态",
                "style": "default",
                "type": "default",
                "value": {
                    "kind": "my_agent_panel_action",
                    "action": "panel.refresh",
                    "binding_ref": binding_ref,
                    "display_revision": next_revision,
                },
            })
            if target_node is not None and target_node.get("node_kind") == "subagent":
                if target_node.get("can_guide") is True and not target_node.get("terminal"):
                    actions.append({
                        "id": "guide",
                        "label": "补充指令",
                        "style": "primary",
                        "type": "primary",
                        "value": {
                            "kind": "my_agent_panel_action",
                            "action": "agent.guide_open",
                            "binding_ref": binding_ref,
                            "target_ref": selected_target_ref,
                            "display_revision": next_revision,
                        },
                    })
                if target_node.get("can_stop_branch") is True and not target_node.get("terminal"):
                    actions.append({
                        "id": "stop",
                        "label": "停止当前分支",
                        "style": "danger",
                        "type": "danger",
                        "value": {
                            "kind": "my_agent_panel_action",
                            "action": "agent.stop_branch_ask",
                            "binding_ref": binding_ref,
                            "target_ref": selected_target_ref,
                            "display_revision": next_revision,
                        },
                    })

    confirm_stop: dict[str, Any] = {"visible": False}
    if not expired and audience != "group_public":
        confirm_stop_run_id = str(record.get("confirm_stop_run_id") or "")
        if confirm_stop_run_id and (selected_target_ref == f"run:{confirm_stop_run_id}" or selected_target_ref == confirm_stop_run_id):
            confirm_stop = {
                "visible": True,
                "prompt_text": f"确认停止子代理分支【{confirm_stop_run_id}】吗？",
                "confirm_value": {
                    "kind": "my_agent_panel_action",
                    "action": "agent.stop_branch_confirm",
                    "binding_ref": binding_ref,
                    "target_ref": f"run:{confirm_stop_run_id}",
                    "display_revision": next_revision,
                },
                "cancel_value": {
                    "kind": "my_agent_panel_action",
                    "action": "agent.stop_branch_cancel",
                    "binding_ref": binding_ref,
                    "target_ref": f"run:{confirm_stop_run_id}",
                    "display_revision": next_revision,
                },
            }

    guidance_form: dict[str, Any] = {"visible": False}
    if not expired and audience != "group_public":
        if record.get("guide_open") is True and selected_target_ref.startswith("run:"):
            guidance_form = {
                "visible": True,
                "placeholder": "输入补充要求...",
                "submit_label": "发送指令",
                "submit_value": {
                    "kind": "my_agent_panel_action",
                    "action": "agent.guide_submit",
                    "binding_ref": binding_ref,
                    "target_ref": selected_target_ref,
                    "display_revision": next_revision,
                },
            }

    panel_view = {
        "task_title": task_title or "任务与子代理控制面板",
        "task_status_label": task_status_label,
        "coordinator_label": coordinator_label,
        "branch_counts_text": branch_counts_text,
        "expired_notice": expired_notice,
        "expired": expired,
        "audience": audience,
        "selected_target": selected_target,
        "actions": actions,
        "confirm_stop": confirm_stop,
        "guidance_form": guidance_form,
    }
    if task_selector is not None:
        panel_view["task_selector"] = task_selector
    if agent_selector is not None:
        panel_view["agent_selector"] = agent_selector
    return panel_view


def send_agent_panel_card(
    adapter: Any,
    target: str,
    *,
    actor_open_id: str = "",
    origin_conversation_id: str = "",
    chat_type: str = "p2p",
    session_task_id: str = "",
    bound_root_id: str = "",
    steps: Sequence[Mapping[str, Any]] = (),
    idempotency_key: str = "",
) -> dict[str, Any]:
    """创建并发送交互式面板卡片，初始化分桶持久状态。"""
    is_group = (chat_type == "group")
    audience = "group_public" if is_group else "dm"
    clean_actor = "" if is_group else (actor_open_id or target)
    origin_conv = origin_conversation_id or target
    base_chat_id = origin_conv.split(":")[0]
    send_target = target.split(":")[0] if is_group else target

    owner = {
        "provider": "feishu",
        "owner_kind": "group" if is_group else "user",
        "owner_id": send_target,
    }
    identity = OwnerIdentity(**owner)
    resolved = resolve_owner_home(Path(adapter.my_agent_home), identity)
    owner_home = resolved.home_dir

    binding_id = secrets.token_hex(16)
    claims = {
        "schema": _PANEL_BINDING_SCHEMA,
        "binding_id": binding_id,
        "channel": "feishu",
        "owner": owner,
        "audience": audience,
        "actor_id": clean_actor,
        "origin_conversation_id": origin_conv,
        "origin_chat_type": chat_type,
        "session_task_id": session_task_id,
        "bound_root_id": bound_root_id,
        "issued_at": time.time(),
    }
    binding_ref = sign_panel_binding(owner_home, claims)

    record: dict[str, Any] = {
        "schema_version": _PANEL_STATE_SCHEMA,
        "binding_id": binding_id,
        "audience": audience,
        "message_id": "",
        "chat_id": "",
        "display_revision": 1,
        "display_generation": 1,
        "created_at": time.time(),
        "selected_session_task_id": session_task_id,
        "selected_bound_root_id": bound_root_id,
        "selected_target_ref": "all_summary",
        "guide_open": False,
        "confirm_stop_run_id": "",
        "last_control_status": "",
        "expired": False,
        "dm_detail_cards": {},
    }

    # Query gateway panel data
    query_actor = (actor_open_id or "").strip() or clean_actor or f"group_panel:{send_target}"
    gw_status, panel_data = _call_gateway_api(
        adapter,
        "/client/agent-view",
        {
            "conversation_id": origin_conv,
            "user_id": query_actor,
            "channel": "feishu",
            "mode": "panel",
            "session_task_id": session_task_id,
            "bound_root_id": bound_root_id,
            "metadata": _gateway_channel_metadata(base_chat_id, chat_type),
        },
        query_actor,
        chat_type,
        base_chat_id=base_chat_id,
    )
    if isinstance(panel_data, dict) and panel_data.get("selected_task"):
        st = panel_data["selected_task"]
        if not session_task_id and st.get("session_task_id"):
            record["selected_session_task_id"] = str(st["session_task_id"])
        if not bound_root_id and st.get("bound_root_id"):
            record["selected_bound_root_id"] = str(st["bound_root_id"])

    panel_view = build_panel_view(panel_data, binding_ref, record, next_revision=1)
    card = render_progress_card({"steps": list(steps), "panel_view": panel_view})

    key = idempotency_key or f"panel:{binding_id}"
    receipt = adapter.send_control_card(send_target, card, idempotency_key=key)

    if not _valid_card_receipt(receipt):
        err = (
            str(receipt.get("error_code") or "CHANNEL_CONTROL_RECEIPT_MISSING")
            if isinstance(receipt, dict)
            else "CHANNEL_CONTROL_RECEIPT_MISSING"
        )
        raise RuntimeError(err)

    record["message_id"] = str(receipt.get("message_id") or "")
    record["chat_id"] = str(receipt.get("chat_id") or "")

    state_path = _panel_state_path(owner_home, binding_id)
    ensure_private_dir(state_path.parent)
    with locked_json_path(state_path):
        _write_panel_state(state_path, record)

    return {**receipt, "binding_id": binding_id, "binding_ref": binding_ref}


def handle_panel_command_event(adapter: Any, msg: Any) -> FeishuEventDisposition:
    """处理 /agents 或 /panel 文本命令，立即发出面板卡片并接受事件。"""
    chat_type = str((getattr(msg, "metadata", {}) or {}).get("feishu_chat_type", "p2p"))
    if chat_type == "group":
        target = str((getattr(msg, "metadata", {}) or {}).get("feishu_chat_id") or msg.conversation_id)
        origin_chat_type = "group"
        actor_open_id = str(getattr(msg, "user_id", "") or "")
    else:
        target = str(getattr(msg, "user_id", "") or "")
        origin_chat_type = "p2p"
        actor_open_id = target

    arg = parse_panel_command_arg(getattr(msg, "content", ""))
    session_task_id = ""
    bound_root_id = ""
    if arg:
        if arg.startswith("stask_") or arg.startswith("stask:"):
            session_task_id = arg[6:] if arg.startswith("stask:") else arg
        else:
            bound_root_id = arg[5:] if arg.startswith("root:") else arg

    try:
        send_agent_panel_card(
            adapter,
            target,
            actor_open_id=actor_open_id,
            origin_conversation_id=str(getattr(msg, "conversation_id", "") or target),
            chat_type=origin_chat_type,
            session_task_id=session_task_id,
            bound_root_id=bound_root_id,
        )
        return FeishuEventDisposition("accepted")
    except Exception as exc:
        logger.error("发送飞书控制面板异常: %s", type(exc).__name__)
        return FeishuEventDisposition("retryable", "FEISHU_PANEL_FAILED")


def handle_panel_card_action(adapter: Any, norm: dict[str, Any]) -> dict[str, Any] | None:
    """处理飞书卡片 my_agent_panel_action 回调操作。"""
    value = norm.get("value") or {}
    if not isinstance(value, dict) or value.get("kind") != "my_agent_panel_action":
        return None

    binding_ref = str(value.get("binding_ref") or "").strip()
    if not binding_ref:
        return {"toast": {"type": "error", "content": "卡片绑定标识缺失。"}}

    home_root = Path(getattr(adapter, "my_agent_home", "") or ".")
    try:
        claims = verify_panel_binding(home_root, binding_ref)
    except Exception:
        return {"toast": {"type": "error", "content": "卡片签名校验失败或已损坏。"}}

    actor = str(norm.get("operator_open_id") or "").strip()
    message_id = str(norm.get("message_id") or "").strip()
    chat_id = str(norm.get("chat_id") or "").strip()
    if not actor or not message_id or not chat_id:
        return {"toast": {"type": "error", "content": "回调身份信息不完整。"}}

    binding_id = str(claims.get("binding_id") or "")
    identity = OwnerIdentity(**claims["owner"])
    resolved = resolve_owner_home(home_root, identity)
    owner_home = resolved.home_dir
    state_path = _panel_state_path(owner_home, binding_id)

    with locked_json_path(state_path):
        record = _read_panel_state(state_path)
        if not isinstance(record, dict) or record.get("schema_version") != _PANEL_STATE_SCHEMA:
            return {"toast": {"type": "error", "content": "面板状态不存在或已过期。"}}

        # 防转发检查：卡片 message_id 和 chat_id 必须匹配绑定的原始记录（fail-closed）
        rec_msg = str(record.get("message_id") or "").strip()
        rec_chat = str(record.get("chat_id") or "").strip()
        if not rec_msg or rec_msg != message_id or not rec_chat or rec_chat != chat_id:
            return {"toast": {"type": "error", "content": "该卡片不支持转发后操作，请在原始会话中操作。"}}

        audience = str(claims.get("audience") or "dm")
        origin_conv_id = str(claims.get("origin_conversation_id") or "")
        base_group_chat_id = origin_conv_id.split(":")[0]

        # 观众身份核验
        if audience == "dm":
            if actor != claims.get("actor_id"):
                return {"toast": {"type": "error", "content": "仅限卡片接收者本人操作。"}}
            # 私聊会话锁检查
            lock_res = _check_session_lock(adapter, actor)
            if lock_res is not None:
                return lock_res
        elif audience == "group_public":
            # 实时群成员资格检查
            try:
                is_member = _current_group_member(adapter, base_group_chat_id, actor)
            except GroupMembershipError:
                return {"toast": {"type": "error", "content": "群成员状态暂时不可核验，请稍后重试。"}}
            except Exception:
                return {"toast": {"type": "error", "content": "群成员状态核验异常。"}}
            if not is_member:
                return {"toast": {"type": "error", "content": "您不在该群组中，无权操作此卡片。"}}
        elif audience == "dm_detail":
            if actor != claims.get("actor_id"):
                return {"toast": {"type": "error", "content": "仅限卡片接收者本人操作。"}}
            # 群成员资格核验（离开原群后立即丧失群任务私聊卡控制权）
            try:
                is_member = _current_group_member(adapter, base_group_chat_id, actor)
            except GroupMembershipError:
                return {"toast": {"type": "error", "content": "群成员状态暂时不可核验，请稍后重试。"}}
            except Exception:
                return {"toast": {"type": "error", "content": "群成员状态核验异常。"}}
            if not is_member:
                return {"toast": {"type": "error", "content": "您已不在原群组中，无法查看该群任务详情。"}}
            # 私聊会话锁检查
            lock_res = _check_session_lock(adapter, actor)
            if lock_res is not None:
                return lock_res

        # 14 天卡片过期生命周期检查
        now = time.time()
        created_at = float(record.get("created_at") or 0.0)
        is_expired = record.get("expired") is True or (now - created_at >= FEISHU_CARD_MAX_AGE_SECONDS)

        if record.get("expired") is True:
            expired_pv = build_panel_view(
                None,
                binding_ref,
                record,
                next_revision=int(record.get("display_revision") or 1),
            )
            expired_card = render_progress_card({"steps": (), "panel_view": expired_pv})
            return {
                "toast": {"type": "warning", "content": "该旧面板已停用，请使用最新面板卡片操作。"},
                "card": {"type": "raw", "data": expired_card},
            }

        if now - created_at >= FEISHU_CARD_MAX_AGE_SECONDS:
            succ_binding_id = secrets.token_hex(16)
            succ_claims = {
                **claims,
                "binding_id": succ_binding_id,
                "issued_at": now,
            }
            succ_binding_ref = sign_panel_binding(owner_home, succ_claims)
            succ_record: dict[str, Any] = {
                "schema_version": _PANEL_STATE_SCHEMA,
                "binding_id": succ_binding_id,
                "audience": audience,
                "message_id": "",
                "chat_id": "",
                "display_revision": 1,
                "display_generation": int(record.get("display_generation") or 1) + 1,
                "created_at": now,
                "selected_session_task_id": record.get("selected_session_task_id", ""),
                "selected_bound_root_id": record.get("selected_bound_root_id", ""),
                "selected_target_ref": record.get("selected_target_ref", "all_summary"),
                "guide_open": False,
                "confirm_stop_run_id": "",
                "last_control_status": "",
                "expired": False,
                "dm_detail_cards": {},
            }

            gw_code, panel_data = _fetch_panel_data(adapter, claims, succ_record, actor)
            succ_pv = build_panel_view(panel_data, succ_binding_ref, succ_record, next_revision=1)
            succ_card = render_progress_card({"steps": (), "panel_view": succ_pv})

            reissue_target = actor if audience != "group_public" else base_group_chat_id
            receipt = adapter.send_control_card(reissue_target, succ_card, idempotency_key=f"reissue:{succ_binding_id}")
            if not _valid_card_receipt(receipt):
                return {"toast": {"type": "error", "content": "新面板卡片发送未成功，请稍后重试原卡片。"}}

            succ_record["message_id"] = str(receipt.get("message_id") or "")
            succ_record["chat_id"] = str(receipt.get("chat_id") or "")

            succ_path = _panel_state_path(owner_home, succ_binding_id)
            ensure_private_dir(succ_path.parent)
            with locked_json_path(succ_path):
                _write_panel_state(succ_path, succ_record)

            record["expired"] = True
            record["expired_notice"] = "该旧面板已停用，请使用最新面板卡片操作。"
            _write_panel_state(state_path, record)

            expired_pv = build_panel_view(
                None,
                binding_ref,
                record,
                next_revision=int(record.get("display_revision") or 1),
            )
            expired_card = render_progress_card({"steps": (), "panel_view": expired_pv})

            return {
                "toast": {"type": "info", "content": "原卡片已达飞书 14 天更新上限，已为您发送新面板卡片。"},
                "card": {"type": "raw", "data": expired_card},
            }

        action_name = str(value.get("action") or "").strip()

        # Mutating action revision guard
        if action_name in {"agent.guide_submit", "agent.stop_branch_confirm"}:
            cb_rev = int(value.get("display_revision") or 0)
            cb_target = str(value.get("target_ref") or "")
            if cb_rev != record.get("display_revision") and cb_target != record.get("selected_target_ref"):
                record["display_revision"] = int(record.get("display_revision") or 1) + 1
                _write_panel_state(state_path, record)
                gw_code, panel_data = _fetch_panel_data(adapter, claims, record, actor)
                refreshed_pv = build_panel_view(
                    panel_data,
                    binding_ref,
                    record,
                    next_revision=int(record.get("display_revision") or 1),
                )
                refreshed_card = render_progress_card({"steps": (), "panel_view": refreshed_pv})
                return {
                    "toast": {"type": "warning", "content": "面板目标已变更，已刷新最新状态，请确认后重试。"},
                    "card": {"type": "raw", "data": refreshed_card},
                }

        toast_result: dict[str, Any] | None = None

        if action_name == "task.select":
            selected_option = str(norm.get("option") or value.get("option") or "").strip()
            if selected_option.startswith("stask:"):
                record["selected_session_task_id"] = selected_option[6:]
                record["selected_bound_root_id"] = ""
            elif selected_option.startswith("root:"):
                record["selected_bound_root_id"] = selected_option[5:]
                record["selected_session_task_id"] = ""
            elif selected_option:
                record["selected_session_task_id"] = selected_option
            record["selected_target_ref"] = "all_summary"
            record["guide_open"] = False
            record["confirm_stop_run_id"] = ""

        elif action_name == "agent.select":
            selected_option = str(norm.get("option") or value.get("option") or "").strip()
            if selected_option:
                record["selected_target_ref"] = selected_option
            record["guide_open"] = False
            record["confirm_stop_run_id"] = ""

        elif action_name == "panel.refresh":
            toast_result = {"type": "info", "content": "已刷新最新状态。"}

        elif action_name == "panel.open_dm_detail":
            if audience != "group_public":
                return {"toast": {"type": "warning", "content": "当前卡片已处于私聊模式。"}}
            dm_detail_cards = record.setdefault("dm_detail_cards", {})
            existing_dm = dm_detail_cards.get(actor)
            dm_info = None
            if isinstance(existing_dm, dict) and (now - float(existing_dm.get("created_at") or 0)) < FEISHU_CARD_MAX_AGE_SECONDS:
                dm_info = existing_dm

            gw_code, panel_data = _fetch_panel_data(adapter, claims, record, actor)

            if dm_info is not None:
                dm_binding_id = dm_info["binding_id"]
                dm_binding_ref = dm_info["binding_ref"]
                dm_state_path = _panel_state_path(owner_home, dm_binding_id)
                with locked_json_path(dm_state_path):
                    dm_rec = _read_panel_state(dm_state_path) or {}
                    dm_rec["selected_session_task_id"] = record.get("selected_session_task_id", "")
                    dm_rec["selected_bound_root_id"] = record.get("selected_bound_root_id", "")
                    dm_rec["display_revision"] = int(dm_rec.get("display_revision") or 1) + 1
                    _write_panel_state(dm_state_path, dm_rec)
                dm_pv = build_panel_view(
                    panel_data,
                    dm_binding_ref,
                    dm_rec,
                    next_revision=int(dm_rec.get("display_revision") or 1),
                )
                dm_card = render_progress_card({"steps": (), "panel_view": dm_pv})
                patched = adapter.edit_progress_message(dm_info["message_id"], "", render_kind="card", card=dm_card)
                if not patched:
                    dm_info = None

            if dm_info is None:
                new_dm_id = secrets.token_hex(16)
                new_dm_claims = {
                    "schema": _PANEL_BINDING_SCHEMA,
                    "binding_id": new_dm_id,
                    "channel": "feishu",
                    "owner": claims["owner"],
                    "audience": "dm_detail",
                    "actor_id": actor,
                    "origin_conversation_id": claims.get("origin_conversation_id", ""),
                    "origin_chat_type": "group",
                    "session_task_id": record.get("selected_session_task_id", ""),
                    "bound_root_id": record.get("selected_bound_root_id", ""),
                    "issued_at": now,
                }
                new_dm_ref = sign_panel_binding(owner_home, new_dm_claims)
                new_dm_rec: dict[str, Any] = {
                    "schema_version": _PANEL_STATE_SCHEMA,
                    "binding_id": new_dm_id,
                    "audience": "dm_detail",
                    "message_id": "",
                    "chat_id": "",
                    "display_revision": 1,
                    "display_generation": 1,
                    "created_at": now,
                    "selected_session_task_id": record.get("selected_session_task_id", ""),
                    "selected_bound_root_id": record.get("selected_bound_root_id", ""),
                    "selected_target_ref": "all_summary",
                    "guide_open": False,
                    "confirm_stop_run_id": "",
                    "last_control_status": "",
                    "expired": False,
                    "dm_detail_cards": {},
                }
                new_dm_pv = build_panel_view(panel_data, new_dm_ref, new_dm_rec, next_revision=1)
                new_dm_card = render_progress_card({"steps": (), "panel_view": new_dm_pv})
                receipt = adapter.send_control_card(actor, new_dm_card, idempotency_key=f"dm_detail:{new_dm_id}")
                if not _valid_card_receipt(receipt):
                    return {"toast": {"type": "error", "content": "私聊详情卡发送未成功，请稍后重试。"}}

                new_dm_rec["message_id"] = str(receipt.get("message_id") or "")
                new_dm_rec["chat_id"] = str(receipt.get("chat_id") or "")

                new_dm_path = _panel_state_path(owner_home, new_dm_id)
                ensure_private_dir(new_dm_path.parent)
                with locked_json_path(new_dm_path):
                    _write_panel_state(new_dm_path, new_dm_rec)

                dm_detail_cards[actor] = {
                    "binding_id": new_dm_id,
                    "binding_ref": new_dm_ref,
                    "message_id": new_dm_rec["message_id"],
                    "chat_id": new_dm_rec["chat_id"],
                    "created_at": now,
                }
                if len(dm_detail_cards) > 64:
                    oldest_k = min(dm_detail_cards.keys(), key=lambda k: dm_detail_cards[k].get("created_at", 0))
                    dm_detail_cards.pop(oldest_k, None)

            _write_panel_state(state_path, record)
            return {"toast": {"type": "success", "content": "已发送至私聊详情卡，请在私聊中查看或控制。"}}

        elif action_name == "agent.view":
            target_ref = str(value.get("target_ref") or "").strip()
            if target_ref:
                record["selected_target_ref"] = target_ref

        elif action_name == "agent.guide_open":
            record["guide_open"] = True
            record["confirm_stop_run_id"] = ""

        elif action_name == "agent.guide_submit":
            form_val = norm.get("form_value") or {}
            guidance_text = str(form_val.get("guidance_text") or "").strip()
            if not guidance_text:
                return {"toast": {"type": "warning", "content": "指导内容不能为空。"}}

            target_ref = str(value.get("target_ref") or record.get("selected_target_ref") or "")
            target_run_id = target_ref[4:] if target_ref.startswith("run:") else target_ref
            if not target_run_id:
                return {"toast": {"type": "warning", "content": "只能向子代理分支发送补充指令。"}}

            rev = int(record.get("display_revision") or 1)
            guide_msg_id = f"guide:{binding_id}:{rev}"
            code, resp = _call_gateway_api(
                adapter,
                "/client/agent-guidance",
                {
                    "conversation_id": origin_conv_id,
                    "user_id": actor,
                    "channel": "feishu",
                    "run_id": target_run_id,
                    "message": guidance_text,
                    "message_id": guide_msg_id,
                    "session_task_id": str(record.get("selected_session_task_id") or ""),
                    "bound_root_id": str(record.get("selected_bound_root_id") or ""),
                    "metadata": _gateway_channel_metadata(
                        base_group_chat_id,
                        str(claims.get("origin_chat_type") or "p2p"),
                    ),
                },
                actor,
                str(claims.get("origin_chat_type") or "p2p"),
                base_chat_id=base_group_chat_id,
            )
            if code not in {200, 202}:
                err_msg = (resp or {}).get("message") or "发送补充指令未成功，请稍后重试。"
                return {"toast": {"type": "error", "content": str(err_msg)}}

            record["guide_open"] = False
            record["last_control_status"] = "补充指令已发送"
            toast_result = {"type": "success", "content": "已发送补充指令。"}

        elif action_name == "agent.stop_branch_ask":
            target_ref = str(value.get("target_ref") or record.get("selected_target_ref") or "")
            target_run_id = target_ref[4:] if target_ref.startswith("run:") else target_ref
            record["confirm_stop_run_id"] = target_run_id
            record["guide_open"] = False

        elif action_name == "agent.stop_branch_cancel":
            record["confirm_stop_run_id"] = ""
            toast_result = {"type": "info", "content": "已取消停止操作。"}

        elif action_name == "agent.stop_branch_confirm":
            target_ref = str(value.get("target_ref") or record.get("confirm_stop_run_id") or "")
            target_run_id = target_ref[4:] if target_ref.startswith("run:") else target_ref
            if not target_run_id:
                return {"toast": {"type": "warning", "content": "未选定停止目标。"}}

            op_id = f"stop:{binding_id}:{target_run_id}"
            code, resp = _call_gateway_api(
                adapter,
                "/client/agent-stop",
                {
                    "conversation_id": origin_conv_id,
                    "user_id": actor,
                    "channel": "feishu",
                    "run_id": target_run_id,
                    "operation_id": op_id,
                    "session_task_id": str(record.get("selected_session_task_id") or ""),
                    "bound_root_id": str(record.get("selected_bound_root_id") or ""),
                    "metadata": _gateway_channel_metadata(
                        base_group_chat_id,
                        str(claims.get("origin_chat_type") or "p2p"),
                    ),
                },
                actor,
                str(claims.get("origin_chat_type") or "p2p"),
                base_chat_id=base_group_chat_id,
            )
            record["confirm_stop_run_id"] = ""
            if code not in {200, 202}:
                err_msg = (resp or {}).get("message") or "请求停止分支未成功，请稍后重试。"
                return {"toast": {"type": "error", "content": str(err_msg)}}

            status_resp = str((resp or {}).get("status") or "")
            if status_resp == "already_terminal":
                record["last_control_status"] = "该分支此前已结束"
                toast_result = {"type": "info", "content": "该分支此前已结束运行。"}
            else:
                record["last_control_status"] = "已请求停止分支"
                toast_result = {"type": "success", "content": "已请求停止该分支。"}

        else:
            return None

        # 单调增加 display_revision
        record["display_revision"] = int(record.get("display_revision") or 1) + 1
        _write_panel_state(state_path, record)

        gw_code, panel_data = _fetch_panel_data(adapter, claims, record, actor)
        updated_pv = build_panel_view(
            panel_data,
            binding_ref,
            record,
            next_revision=int(record.get("display_revision") or 1),
        )
        updated_card = render_progress_card({"steps": (), "panel_view": updated_pv})

        res: dict[str, Any] = {"card": {"type": "raw", "data": updated_card}}
        if toast_result is not None:
            res["toast"] = toast_result
        return res


def _fetch_panel_data(
    adapter: Any,
    claims: dict[str, Any],
    record: dict[str, Any],
    actor: str,
) -> tuple[int, dict[str, Any] | None]:
    """查询 Gateway 任务投影数据。"""
    origin_conv_id = str(claims.get("origin_conversation_id") or "")
    base_chat_id = origin_conv_id.split(":")[0]
    origin_chat_type = str(claims.get("origin_chat_type") or "p2p")
    selected_target = str(record.get("selected_target_ref") or "all_summary")
    selected_run_id = selected_target[4:] if selected_target.startswith("run:") else ""
    query_actor = (
        (actor or "").strip()
        or str(claims.get("actor_id") or "").strip()
        or (f"group_panel:{base_chat_id}" if origin_chat_type == "group" else "feishu_user")
    )
    return _call_gateway_api(
        adapter,
        "/client/agent-view",
        {
            "conversation_id": origin_conv_id,
            "user_id": query_actor,
            "channel": "feishu",
            "mode": "panel",
            "session_task_id": str(record.get("selected_session_task_id") or ""),
            "bound_root_id": str(record.get("selected_bound_root_id") or ""),
            "run_id": selected_run_id,
            "selected_run_id": selected_run_id,
            "metadata": _gateway_channel_metadata(base_chat_id, origin_chat_type),
        },
        query_actor,
        origin_chat_type,
        base_chat_id=base_chat_id,
    )


def _check_session_lock(adapter: Any, actor: str) -> dict[str, Any] | None:
    """检查私聊会话锁状态；锁定则发送密码卡并阻断面板操作。"""
    if not getattr(adapter, "_session_lock_enabled", True):
        return None
    unlock = getattr(adapter, "_unlock", None)
    if unlock is None:
        return None
    from ..session_lock import admission

    try:
        status = admission.check_session_admission(unlock, actor)
        if not status.allowed and not (status.locked or status.requires_password_setup):
            return {"toast": {"type": "warning", "content": "会话锁检查未通过。"}}
        if status.locked or status.requires_password_setup:
            mode = "set" if status.requires_password_setup else "unlock"
            send_pwd = getattr(adapter, "_send_password_card", None)
            if callable(send_pwd):
                send_pwd(actor, mode)
            else:
                from .feishu import _send_session_password_card
                _send_session_password_card(adapter, actor, mode)
            return {"toast": {"type": "warning", "content": "当前私聊已锁定，请先在密码卡完成解锁后再操作面板。"}}
    except Exception as exc:
        logger.warning("面板会话锁检查异常: %s", type(exc).__name__)
    return None
