# LLM: 配置接口沿用可信 owner 与共享 owner_conversation_store；冷用户不能触发 Agent/后端/工具初始化，配置读写仍独立。
# 文字 /model（IM 与 TUI 带编号形式）只做查看、会话选择和默认值，复用同一 owner/线程解析与同一写入口。
# 模块用途: 为菜单和/model提供模型列表、保存与选择；列表展示原档案输入模态，未知不猜能力，不向聊天写入密钥或启动后台服务。

from __future__ import annotations

import json
import time
from types import SimpleNamespace

from ..backends.bounded_call import BoundedCallExitUnconfirmedError
from ..common.request_execution_ports import (
    RequestExecutionContextUnavailable,
    RequestExecutionOutcomeUncertainError,
)
from ..common.tool_write_context import ToolWriteContextUnavailable
from ..conversation.control_commands import ConversationControlCommand, ConversationControlResult
from ..retrieval.embedding_usage import EMBEDDING_USAGE
from ..settings.embedding_selection import (
    EMBEDDING_OPERATIONS,
    apply_embedding_choice,
    embedding_choices,
    execute_embedding_operation,
)
from ..settings.model_profiles import ModelProfileError, execute_model_profile_operation
from ..user_space.group_authorization import GroupAuthorizationError
from ..user_space.owner_resolver import home_paths_with_owner, resolve_owner_home
from .control_service import _scope_request_payload, resolve_gateway_scope_owner
from .model_control_cards import _ModelCardDeliveryFailed
from .multiprocess_global_write import GlobalWriteError
from .owner_conversation_store import owner_conversation_store


# LLM: 冷C只消费原G1模型领域source，实际typed先同对象上抛，其它操作仍waiting；Count0原调用保持。
# LLM: 列表永不回传密钥；只从可信 owner/channel binding 取得会话，冷菜单可创建线程但不进入 owner pool。
# owner/线程解析与文字 /model 共用 _scoped_model_host，两处不能各自推导身份。
# 函数用途: 为已认证用户管理共享于本人各会话的模型目录，选择仅保存当前会话，不被完整 Agent 初始化拖慢。
def handle_client_models(handler, server) -> None:
    from .http_handlers import (
        _gateway_control_scope,
        _http_conversation_id,
        _request_channel,
        require_trusted_source,
    )

    if require_trusted_source(handler):
        return
    if server is None or server.agent is None and getattr(server, "control_plane", None) is None:
        handler._send_json(503, {"ok": False, "message": "Gateway 尚未就绪。"})
        return
    try:
        body = handler._read_json()
        if not isinstance(body, dict) or not _http_conversation_id(body):
            raise ModelProfileError("缺少会话编号。")
        user_id, channel = _request_channel(handler)
        scope = _gateway_control_scope(handler, body, user_id=user_id, channel=channel)
        from .model_metadata_transaction import coordinator_model_server
        if coordinator_model_server(server):
            from .model_metadata_transaction import (
                execute_coordinator_model_metadata,
                model_metadata_supported,
            )
            if not model_metadata_supported(handler, body):
                handler._send_json(503, {"ok": False, "status": "waiting", "error_code": "COORDINATOR_BUSINESS_NOT_READY", "terminal": False})
                return
            handler._send_json(200, execute_coordinator_model_metadata(handler, server, body))
            return
        config_host, thread = _scoped_model_host(server.agent, scope)
        operation = str(body.get("operation") or "")
        # 向量模型是全局设置（不属于会话），管理员判定在 embedding_selection 里按已认证 owner 做。
        result = (execute_embedding_operation(config_host, operation, body) if operation in EMBEDDING_OPERATIONS
                  else execute_model_profile_operation(config_host, operation, body, thread_id=thread.thread_id))
        if operation == "list":
            result["model_execution"] = _model_execution_snapshot(server.agent, scope, config_host, thread.thread_id)
    except (GlobalWriteError, RequestExecutionContextUnavailable, RequestExecutionOutcomeUncertainError,
            BoundedCallExitUnconfirmedError, ToolWriteContextUnavailable) as exc:
        handler._send_json(503, {"ok": False, "error_code": exc.error_code, "error_stage": "execution",
                                 "message": "模型设置宿主暂不可用，原已存状态保持。"})
        return
    except json.JSONDecodeError:
        handler._send_json(400, {"ok": False, "message": "模型配置请求格式错误。"})
        return
    except (ModelProfileError, GroupAuthorizationError) as exc:
        handler._send_json(400, {"ok": False, "message": str(exc)})
        return
    except Exception:  # noqa: BLE001 配置错误不得泄露凭证或内部路径
        handler._send_json(500, {"ok": False, "message": "无法读取或保存模型配置，原配置未被主动清除。"})
        return
    handler._send_json(200, result)


# LLM: 状态只复用已加载的同 owner Agent；冷查询保持轻量，不初始化后端，也不把其他用户的活动映射过来。
# 函数用途: 为模型菜单附加当前选择、待采用及最近请求状态。
def _model_execution_snapshot(base_agent, scope, config_host, thread_id: str) -> dict:
    from ..settings.model_scope import thread_model_execution_status
    from .request_worker import _resolve_loaded_request_agent_for_owner

    owner = resolve_gateway_scope_owner(base_agent, scope)
    loaded = _resolve_loaded_request_agent_for_owner(base_agent, owner)
    return thread_model_execution_status(loaded or config_host, thread_id)


# LLM: metadata_source仅原HTTP设置producer传入；线程取得沿原完整锁链非阻塞，竞争不等待业务执行。
# LLM: 菜单与文字 /model 共用认证 scope 解析 owner/线程，并向新冷 host 绑定原 HostChannelActor；不物化完整 Agent 或改共享身份。
# 函数用途: 构造只带 home/config/会话库的轻量宿主和当前会话线程，供模型配置读写使用；可能新建“会话设置”线程。
def _scoped_model_host(base_agent, scope, *, metadata_source=None) -> tuple[SimpleNamespace, object]:
    if metadata_source is not None:
        from .model_metadata_transaction import scoped_model_host
        return scoped_model_host(metadata_source, scope)
    owner = resolve_gateway_scope_owner(base_agent, scope)
    base_home = base_agent.home_paths
    scoped_home = home_paths_with_owner(base_home, resolve_owner_home(base_home.root, owner))
    config_host = SimpleNamespace(home_paths=scoped_home, config=base_agent.config)
    store = owner_conversation_store(base_agent, scoped_home)
    config_host.conversation_store = store
    thread = store.threads.get_or_create({
        "canonical_user_id": scope.user_id, "owner_id": scoped_home.owner_id,
        "owner_home": str(scoped_home.owner_home_dir), "channel": scope.channel,
        "channel_conversation_id": scope.conversation_id, "channel_user_id": scope.user_id,
        "title": "会话设置",
    }, blocking=False)
    from ..user_space.group_authorization_context import bind_group_control_host

    return bind_group_control_host(base_agent, config_host, scope, thread.thread_id), thread


# LLM: 原文字算法同时消费Count0与实际C同领域来源；C目录写逐段末核，Delivery在门外，actual typed不能变普通失败。
# LLM: 目标只认当前列表里的编号或精确配置编号；select 只改本会话，set_default 只改未来默认，都经原写入口。
# 列表不含密钥；群卡先核原群actor再把topic仅投影到运输basechat，签名thread仍保持原会话，私人表单走原控制。
# 函数用途: 执行/model查看及配置选择；群卡不向内部topic键或跨群地址外发，select/default沿原写入口。
def execute_model_text_control(base_agent, command: ConversationControlCommand, scope, *, metadata_source=None) -> ConversationControlResult:
    try:
        host, thread = _scoped_model_host(base_agent, scope, **({} if metadata_source is None else {"metadata_source": metadata_source}))
        if command.kind == "effort" and command.operation == "set":
            from .session_setting_control import save_effort
            return save_effort(host, thread, command, source=metadata_source)
        if command.operation == "vector":
            return _vector_text_control(host, command.value)
        listing = _model_operation(host, "list", {}, thread.thread_id, metadata_source)
        if command.operation == "view":
            if metadata_source is None:
                listing["model_execution"] = _model_execution_snapshot(base_agent, scope, host, thread.thread_id)
            else:
                from .model_metadata_transaction import model_execution_snapshot
                listing["model_execution"] = model_execution_snapshot(metadata_source, host, scope, thread.thread_id)
        if command.operation == "view" and _send_text_model_card(base_agent, host, scope, thread, listing, metadata_source):
            return ConversationControlResult("model", True, "请在模型卡选择本会话或新会话默认；新增、编辑将发送到本人私聊。")
        if command.operation == "view":
            return ConversationControlResult("model", True, render_model_choices(listing) + _admin_hint(base_agent if metadata_source is None else host, listing, scope))
        row = _choice_by_reference(listing, command.value)
        if row is None:
            return ConversationControlResult(
                "model", False, f"没有编号为 {command.value} 的可选模型。\n" + render_model_choices(listing),
            )
        from .session_setting_control import model_selection_payload

        saved = _model_operation(host, command.operation, model_selection_payload(scope, command, row), thread.thread_id, metadata_source)
    except (GlobalWriteError, RequestExecutionContextUnavailable, RequestExecutionOutcomeUncertainError,
            BoundedCallExitUnconfirmedError, ToolWriteContextUnavailable):
        raise
    except _ModelCardDeliveryFailed as exc:
        from .model_control_dispatch import _model_delivery_failure
        failure = _model_delivery_failure(exc.receipt)
        return ConversationControlResult('model', False, failure['message'], error_code=failure['error_code'],
            delivery_status='unknown' if failure['effect_outcome'] == 'unknown' else 'rejected',
            details={key: failure[key] for key in ('effect_outcome', 'provider_code') if key in failure})
    except (ModelProfileError, GroupAuthorizationError) as exc:
        return ConversationControlResult("model", False, str(exc))
    except Exception:  # noqa: BLE001 配置错误不得泄露凭证或内部路径
        return ConversationControlResult("model", False, "无法读取或保存模型配置，原配置未被主动清除。")
    if command.operation == "set_default":
        return ConversationControlResult(
            "model", True,
            f"新会话默认使用 {row['model_name']}；已打开的会话保持原模型，当前会话要换请发送 /model <编号>。",
        )
    return ConversationControlResult(
        "model", True,
        f"本会话已选择 {row['model_name']}，上下文 {row.get('model_context_window_tokens', '?')} tokens。当前响应和已开始的工具结束后，下一次模型请求采用新选择。",
        details={"profiles": saved.get("profiles", listing.get("profiles", [])),
                 **{key: saved[key] for key in ("thread_id", "selected", "selection_revision", "saved_at", "adoption_status", "selection_available", "warning") if key in saved}},
    )


# LLM: 原Count0仍直接调用settings虚调用缝；C只将同一实际非网络RMW交已验证领域source，不在配置锁中反取T。
# 函数用途: 让client/models与文字model共用原目录和选择CAS。
def _model_operation(host, operation, payload, thread_id, source):
    if source is None:
        return execute_model_profile_operation(host, operation, payload, thread_id=thread_id, blocking=False)
    from .model_metadata_transaction import model_operation
    return model_operation(source, host, operation, payload, thread_id)


# LLM: Count0原能力/发送hook不变；C仅显式原registry依赖，group仍核原HostActor且topic只投影运输目标；网络在所有短门外。
# 函数用途: 在原菜单查看路径发送第一张选择卡，不能借菜单开放监控或其它配置域。
def _send_text_model_card(base, host, scope, thread, listing, source):
    if scope.channel != 'feishu' or source is None and getattr(base, 'delivery_service', None) is None:
        return False
    if source is None:
        from ..capability.runtime_config_reload import capability_config_for_agent
        capability = capability_config_for_agent(base)
        owner = resolve_gateway_scope_owner(base, scope)
    else:
        from .model_metadata_transaction import model_card_capability
        capability = model_card_capability(source)
        owner = source.owner
    if capability is not None and not getattr(capability, "im_model_cards_enabled", True):
        return False
    from ..conversation.channels import binding_delivery_target
    from ..user_space.group_authorization_context import group_operation_actor
    if host.home_paths.owner_kind == 'group':
        group_operation_actor(host, thread.thread_id)
    target = binding_delivery_target(scope.channel, scope.user_id, scope.conversation_id, group=host.home_paths.owner_kind == 'group')
    if source is None:
        from .model_control_cards import send_model_control_card
        send_model_control_card(base, host, owner, actor_id=scope.user_id, thread_id=thread.thread_id, target=target, listing=listing)
    else:
        from .model_metadata_transaction import send_model_selection
        send_model_selection(source, host, thread.thread_id, target, listing)
    return True


# LLM: 向量模型（语义记忆）是全局设置：谁都能查看，改只给本机管理员（判定在 settings.embedding_selection）；编号按本人可用的
#   嵌入档案列表从 1 起，也接受精确档案编号；off/关闭 表示关闭语义记忆。回执带“保存后重启 Gateway 生效”的说明。有写文件副作用。
#   查看时，管理员（listing.can_change）另看到本 Gateway 进程启动以来的嵌入用量与召回方式（S7，只有数字和原因码；计数是全进程的，
#   含其他用户，所以普通用户看不到）。TUI 的 /model vector 也转到这里，两边同一份结果。
# 函数用途: 执行 /model vector [编号|off]，返回中文回执。
def _vector_text_control(host, value: str) -> ConversationControlResult:
    listing = embedding_choices(host)
    target = str(value or "").strip()
    if not target:
        usage = render_embedding_usage(EMBEDDING_USAGE.snapshot()) if listing.get("can_change") else ""
        return ConversationControlResult("model", True, render_vector_choices(listing) + usage)
    if target.lower() in {"off", "关闭"}:
        result = apply_embedding_choice(host, "", actor="chat")
    else:
        rows = listing["choices"]
        row = rows[int(target) - 1] if target.isdigit() and 1 <= int(target) <= len(rows) else next(
            (item for item in rows if item["id"] == target), None)
        if row is None:
            return ConversationControlResult("model", False, f"没有编号为 {target} 的向量模型。\n" + render_vector_choices(listing))
        result = apply_embedding_choice(host, row["id"], actor="chat")
    return ConversationControlResult("model", bool(result.get("ok")), str(result.get("message") or ""))


# 嵌入用途在“本次启动以来”几行里的中文名，顺序即展示顺序；other（没标注用途的请求）只有发生过才显示。
_EMBEDDING_PURPOSE_LABELS = (("memory_write", "记忆写入"), ("memory_recall", "召回"), ("memory_rebuild", "重建"),
                             ("tool_retrieval", "工具检索"), ("other", "其他"))


# LLM: 只渲染计数快照里的数字和结构化原因码，不含任何正文、模型名或地址。token 只写供应商回报的数；有请求没回报的单独说明，
#   全部没回报写“未回报”，不估算。计数随进程生灭（不落盘），所以写明从什么时候起、重启归零。
# 函数用途: 把进程内嵌入用量与召回方式计数排成几行中文。
def render_embedding_usage(snapshot: dict) -> str:
    started = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(snapshot.get("started_at") or 0)))
    lines = ["", f"本次 Gateway 启动以来（{started} 起，本进程计数，不落盘，重启归零）："]
    embedding = snapshot.get("embedding") or {}
    for key, label in _EMBEDDING_PURPOSE_LABELS:
        row = embedding.get(key) or {}
        if key == "other" and not row.get("requests"):
            continue
        lines.append(f"- 嵌入·{label}：{_embedding_usage_text(row)}")
    retrieval = snapshot.get("retrieval") or {}
    last = str(retrieval.get("last_mode") or "") or "还没有"
    reason = str(retrieval.get("last_fallback_reason") or "") or "无"
    lines.append(f"- 召回方式：semantic {retrieval.get('semantic', 0)} 次，keyword {retrieval.get('keyword', 0)} 次，"
                 f"none {retrieval.get('none', 0)} 次；最近一次 {last}，降级原因 {reason}")
    return "\n".join(lines)


# 函数用途: 一种用途的嵌入计数排成一句：请求次数、文本条数、失败次数、供应商回报的 token。
def _embedding_usage_text(row: dict) -> str:
    requests = int(row.get("requests") or 0)
    if not requests:
        return "请求 0 次"
    failures = int(row.get("failures") or 0)
    unreported = int(row.get("tokens_unreported_requests") or 0)
    reported_requests = requests - failures - unreported
    if reported_requests <= 0:
        tokens = "token 未回报" if unreported else "token 无（都失败了）"
    else:
        tokens = f"token {int(row.get('tokens') or 0)}" + (f"（另有 {unreported} 次未回报）" if unreported else "")
    return f"请求 {requests} 次、{int(row.get('texts') or 0)} 条，失败 {failures} 次，{tokens}"


# LLM: 只渲染模型名、服务商名和稳定档案编号，不渲染接口地址或密钥；列表序号与 /model vector <编号> 共用。
#   操作提示按结构化字段 can_change 给：非管理员只告诉他只能查看（向量模型是全局设置，他自己加嵌入模型也用不上）。
# 函数用途: 把向量模型的可选项和当前状态排成聊天文字。
def render_vector_choices(listing: dict) -> str:
    lines = ["向量模型（语义记忆用，不聊天；全局设置，只有管理员能改）", str(listing.get("message") or "")]
    for index, row in enumerate(listing.get("choices", []), 1):
        lines.append(f"{index}. {row['model_name']}（{row.get('provider_name') or '未命名服务商'}，编号 {row['id']}）")
    if not listing.get("can_change"):
        return "\n".join([*lines, "你不是本机管理员，只能查看；要改请让管理员操作。"])
    if not listing.get("choices"):
        lines.append("还没有嵌入模型：先在 TUI 的 /model → 新增模型 里添加，用途选 embedding（例如 MiniMax embo-01）。")
    lines.append("用法：/model vector <编号> 选用并打开语义记忆；/model vector off 关闭。保存后重启 Gateway 生效。")
    return "\n".join(lines)


# LLM: 只在没有可选模型时追加；判据复用 request_worker.admin_binding_hint_for_request（IM 私聊、开关、密码已设、未绑定）。
# 函数用途: 飞书里的管理员发 /model 看到“没有可选模型”时，同时告诉他可以先用 /admin 绑定身份。
def _admin_hint(base_agent: object, listing: dict, scope: object) -> str:
    if _selectable_choices(listing):
        return ""
    from .request_worker import admin_binding_hint_for_request

    hint = admin_binding_hint_for_request(base_agent, _scope_request_payload(scope))
    return f"\n{hint}" if hint else ""


# LLM: 与 TUI 菜单同一过滤：部署默认行或 agentic 可用行；顺序取 list 投影原顺序，编号从 1 开始。
# 函数用途: 返回可供选择的模型行，保证查看与选择两次命令用同一套编号。
def _selectable_choices(listing: dict) -> list[dict]:
    return [row for row in listing.get("profiles", []) if row.get("id") == "default" or row.get("available", True)]


# LLM: 只接受列表编号或精确配置编号，不按模型名模糊匹配，避免私有与共享重名时选错。
# 函数用途: 把用户给的编号解析成列表里的一行，找不到返回 None。
def _choice_by_reference(listing: dict, reference: str) -> dict | None:
    rows = _selectable_choices(listing)
    text = str(reference or "").strip()
    if text.isdigit() and 1 <= int(text) <= len(rows):
        return rows[int(text) - 1]
    return next((row for row in rows if row.get("id") == text), None)


# LLM: 只渲染公开字段与稳定档案编号；输入模态仅认原档案，缺省/空为未知，模型名和native工具协议不证明图像能力。
#   默认行按结构化字段 default_source 标注：部署默认，或管理员指定的初始模型（普通用户）。
# 函数用途: 展示当前/默认模型、编号及显式输入模态，不暴露接口地址或密钥，不探测或改变模型选择。
def render_model_choices(listing: dict) -> str:
    from ..settings.model_selection_status import render_model_execution_status
    rows = _selectable_choices(listing)
    names = {row.get("id"): row.get("model_name") for row in listing.get("profiles", [])}
    current = listing.get("selected", "default")
    lines = [
        f"当前会话已选模型：{names.get(current) or '未选择'}",
        f"新会话默认：{names.get(listing.get('default_selected', 'default')) or '未设置'}",
    ]
    execution = render_model_execution_status(listing.get("model_execution"))
    if execution:
        lines.append(execution)
    if not rows:
        lines.append("还没有可选模型。请让管理员在终端 TUI 的 /model →「默认模型与共享」里开放一个模型或指定初始模型，再发送 /model 查看。")
    else:
        lines.append("可选模型：")
        for index, row in enumerate(rows, start=1):
            mark = "● " if row.get("id") == current else ""
            label = (("（管理员指定的初始模型）" if row.get("default_source") == "admin_initial" else "（部署默认）")
                     if row.get("id") == "default" else ("（管理员共享）" if row.get("shared") else ""))
            provider_name = str(row.get("provider_name") or "")
            provider = f" · {provider_name}" if provider_name and provider_name != row.get("model_name") else ""
            modalities = ', '.join(row.get('input_modalities') or ()) or '未知（未声明）'
            lines.append(f"{index}. {mark}{row.get('model_name')}{provider} · {row.get('model_context_window_tokens', '?')} tokens{label}"
                         f" · 输入模态：{modalities} · 档案编号：{row.get('id')}")
        lines.append("用法：/model <编号> 选为本会话模型；/model default <编号> 设为新会话默认。")
    # 未选模型时投影给的是 TUI 菜单用语（“新增并选择”），聊天里改由上面的“未选择”和用法引导，不重复显示。
    if listing.get("warning") and not (current == "default" and not listing.get("selection_available")):
        lines.append(str(listing["warning"]))
    lines.append("新增或修改模型请使用模型卡的私人表单或终端 TUI，普通聊天里不收发密钥。")
    return "\n".join(lines)
