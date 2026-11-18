"""FC1 凭据授权、同人同会话延续、collected 发现、重放恢复与原任务唤醒回归。"""
from __future__ import annotations

import json
import threading
import time
import uuid
from copy import copy
from dataclasses import asdict, replace
from types import MappingProxyType, SimpleNamespace

import pytest

from agent_py_agent.agent.adapter.feishu_ws import FeishuWsClient
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.agent_core.runtime.loop_support import (
    ToolSectionsRequest,
    _background_preload_tool_names,
    _resolve_tool_sections,
)
from agent_py_agent.agent.agent_core.runtime.turn_trigger import (
    CREDENTIAL_INPUT_RECOMMENDED_TOOLS,
    CREDENTIAL_READY_CONTINUATION_GUIDANCE,
    current_turn_text,
    turn_trigger_recommendation,
)
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.command_arguments import ArgumentSpec, CommandActionSpec
from agent_py_agent.agent.common.json_io import locked_json_path, read_json_object_report
from agent_py_agent.agent.contracts.tool_input_schema import validate_tool_input
from agent_py_agent.agent.conversation import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
)
from agent_py_agent.agent.conversation import (
    background_tool_policy as bg_policy,
)
from agent_py_agent.agent.conversation.host_actor import actor_from_scope
from agent_py_agent.agent.conversation.input_dependencies import (
    arm_input_dependency,
    credential_wake_projection,
    input_dependency_path,
    reconcile_input_dependencies,
    resolve_input_dependency,
)
from agent_py_agent.agent.conversation.lifecycle_wake_event import lifecycle_wake_turn_trigger
from agent_py_agent.agent.conversation.runtime import (
    BackgroundRunRequest,
    _apply_background_wake_attributes,
    _background_model_inputs,
    _run_params,
)
from agent_py_agent.agent.conversation.tool_approval_scope import tool_approval_scope
from agent_py_agent.agent.delivery import (
    ChannelCapabilities,
    DeliveryContext,
    ReplyEnvelope,
)
from agent_py_agent.agent.gateway_parts.private_interaction_service import (
    handle_private_interaction,
    resolve_private_interaction,
)
from agent_py_agent.agent.plugin_activation import PluginActivationRequest
from agent_py_agent.agent.plugin_activation_record import PluginActivation
from agent_py_agent.agent.plugin_environment_plan import PluginEnvironmentPlan
from agent_py_agent.agent.plugin_install_store import PluginInstallStore
from agent_py_agent.agent.plugin_installation import PluginInstallRequest
from agent_py_agent.agent.plugin_package import read_plugin_package
from agent_py_agent.agent.tooling.runtime_contracts import (
    ProviderToolCapability,
    ToolProtocolSnapshot,
)
from agent_py_agent.agent.tooling.task_credential_tool import RequestCredentialsTool
from agent_py_agent.agent.tooling.web_fetch_runtime import FetchRawRequest, RawResponseParts
from agent_py_agent.agent.user_space import task_credentials as tc
from agent_py_agent.agent.user_space.admin_channel_identity import bind_admin_channel_identity
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home
from agent_py_agent.agent.user_space.task_credential_forms import (
    assert_credential_record_signature,
    configure_credential,
)
from agent_py_agent.agent.user_space.task_credential_groups import (
    discover_group_credentials,
    discover_owner_credentials,
)
from agent_py_agent.agent.user_space.task_credential_http import credential_authenticated_request
from agent_py_agent.agent.user_space.task_credential_storage import CredentialInputError
from agent_py_agent.agent.user_space.task_credentials import (
    CredentialCardOptions,
    CredentialMaterial,
    authorized_credential_material,
    credential_card,
    credential_origin,
    credential_path,
    current_credential_actor_scope,
    manage_credential,
    request_credential_card,
)
from agent_py_agent.tests.test_group_shared_credentials import _group_case, _switch_actor
from agent_py_agent.tests.test_im_credentials_security_matrix import _child
from agent_py_agent.tests.test_ingestion_secret_scope import _agent
from agent_py_agent.tests.test_ingestion_task_credential_scope import (
    _active,
    _scheduled_execution,
)
from agent_py_agent.tests.test_plugin_activation import publication, revocation
from agent_py_agent.tests.test_plugin_package import _bundle


def _install_browser_lite_fixture(tmp_path, root, *, version="1.0", operation_prefix="bl-1"):
    """在临时主目录安装并激活带 fill 声明的 browser-lite 测试包。"""
    main_owner = resolve_owner_home(root, OwnerIdentity.local_main())
    store = PluginInstallStore(main_owner)

    def _mutate_manifest(manifest: dict) -> None:
        manifest.update(
            plugin_id="browser-lite",
            version=version,
            summary="无头浏览器表单填充",
            actions=[
                asdict(
                    CommandActionSpec(
                        "fill",
                        "填写控件",
                        (ArgumentSpec("selector", "选择器", required=True),),
                        kind="tool",
                        target="fill",
                    )
                )
            ],
            default_action="fill",
            tools=[
                {
                    "name": "fill",
                    "description": "填写页面输入框",
                    "requested_effect": "mutating",
                    "input_schema": {
                        "type": "object",
                        "properties": {"selector": {"type": "string"}},
                        "required": ["selector"],
                    },
                }
            ],
        )

    pkg_zip = tmp_path / f"{operation_prefix}.zip"
    pkg_zip.write_bytes(_bundle(change=_mutate_manifest))
    existing = next(
        (item for item in store.snapshot() if item.manifest.plugin_id == "browser-lite"),
        None,
    )
    expected_rev = existing.revision if existing is not None else 0
    installed = store.install(
        PluginInstallRequest(
            read_plugin_package(pkg_zip),
            f"{operation_prefix}-install",
            expected_rev,
            installation_scope="public",
        )
    ).installation
    plan = PluginEnvironmentPlan(
        f"{operation_prefix}-enable",
        "browser-lite",
        installed.package_sha256,
        installed.revision,
        installed.settings_revision,
        "b" * 64,
    )
    prepared = store.change_activation(
        PluginActivationRequest(
            plan.operation_id, installed.revision, PluginActivation(plan, "preparing")
        )
    )
    active = store.change_activation(publication(prepared)).installation
    return store, active


def _dm_case(tmp_path, *, kind="user", actor_id="ou_dm_actor", conversation_id="oc_dm_main"):
    """建立带真实 Feishu HostActor 与控制卡运输回执的私聊或绑定管理员会话。"""
    agent = _agent(tmp_path, kind=kind)
    agent.config.feishu_session_lock_enabled = False
    if kind == "main":
        bind_admin_channel_identity(agent.home_paths.root, "feishu", actor_id)
    sent_cards: list[tuple[str, dict]] = []
    delivered_replies: list[tuple[DeliveryContext, ReplyEnvelope]] = []

    def _send_card(target, card, **_kwargs):
        sent_cards.append((target, card))
        return {"message_id": "om_" + uuid.uuid4().hex, "chat_id": conversation_id}

    def _send_reply(target, content, **_kwargs):
        msg_id = "om_reply_" + uuid.uuid4().hex
        delivered_replies.append((target, content))
        return {"message_id": msg_id, "chat_id": target}

    agent.channel_registry.register_adapter(
        "feishu",
        SimpleNamespace(send_control_card=_send_card, send_message=_send_reply),
        capabilities=ChannelCapabilities(control_cards=True, proactive=True),
    )
    thread, host_actor, claim = _start_feishu_turn(
        agent, actor_id=actor_id, conversation_id=conversation_id
    )
    return SimpleNamespace(
        agent=agent,
        thread=thread,
        actor=host_actor,
        claim=claim,
        sent_cards=sent_cards,
        delivered_replies=delivered_replies,
        conversation_id=conversation_id,
    )


def _start_feishu_turn(
    agent, *, actor_id: str, conversation_id: str, root_id: str = ""
):
    """在同一或不同 Feishu 会话中结束旧 claim 并启动新一轮主任务。"""
    store = agent.conversation_store
    prev = getattr(agent, "_current_run_params", None)
    if prev is not None:
        prev_tid = str((prev.task_attributes or {}).get("conversation_thread_id") or "")
        if prev_tid:
            existing_claim = store.claims.load(prev_tid)
            if existing_claim and existing_claim.get("status") == "running":
                store.claims.finish(
                    {
                        "thread_id": prev_tid,
                        "claim_id": existing_claim["claim_id"],
                        "status": "done",
                    }
                )
    root = root_id or ("gwreq-" + uuid.uuid4().hex)
    thread = store.threads.get_or_create(
        {
            "channel": "feishu",
            "channel_conversation_id": conversation_id,
            "channel_user_id": actor_id,
            "canonical_user_id": actor_id,
            "owner_id": agent.home_paths.owner_id,
            "owner_home": str(agent.home_paths.owner_home_dir),
        }
    )
    host_actor = actor_from_scope(
        agent,
        SimpleNamespace(
            channel="feishu",
            channel_user_id=actor_id,
            channel_conversation_id=conversation_id,
        ),
        thread.thread_id,
        root,
    )
    store.tasks.bind(
        {"thread_id": thread.thread_id, "task_id": root, "goal": "查询站点私有业务记录", "status": "active"},
        host_channel_actor=host_actor,
    )
    claim = store.claims.acquire(
        {"thread_id": thread.thread_id, "task_id": root, "reason": "gateway_foreground_turn"}
    )
    # 使用后台车道兼容模式避免无处理文件的 gateway_foreground_turn writer 门报错。
    store.claims.finish({"thread_id": thread.thread_id, "claim_id": claim["claim_id"], "status": "done"})
    claim = store.claims.acquire(
        {"thread_id": thread.thread_id, "task_id": root, "reason": "thread_goal_continue"}
    )
    agent._current_run_params = RunParams(
        request_id=root,
        task_id=root,
        run_id=root,
        task_attributes={"conversation_thread_id": thread.thread_id},
        host_channel_actor=host_actor,
    )
    return thread, host_actor, claim


def _count_decrypts(monkeypatch):
    """包装 task_credentials._cipher 以精确统计实际 Fernet.decrypt 调用次数。"""
    counts = {"decrypt": 0}
    original_cipher = tc._cipher

    def _wrapped_cipher(home, *, existing_only=False):
        fernet = original_cipher(home, existing_only=existing_only)
        real_decrypt = fernet.decrypt

        def _counted_decrypt(token, ttl=None):
            counts["decrypt"] += 1
            return real_decrypt(token) if ttl is None else real_decrypt(token, ttl=ttl)

        return SimpleNamespace(encrypt=fernet.encrypt, decrypt=_counted_decrypt)

    monkeypatch.setattr(tc, "_cipher", _wrapped_cipher)
    return counts


# ---------- 1. C1 材料结构、不回显 repr、逐码点原值与 browser_consumer 绑定 ----------


def test_c1_material_immutability_repr_verbatim_values_and_lock_release(tmp_path, monkeypatch):
    ctx = _dm_case(tmp_path)
    custom_fields = [
        {"name": "username", "label": "账号"},
        {"name": "secret", "label": "密码"},
        {"name": "tenant_code", "label": "租户码"},
    ]
    ref, _ = request_credential_card(
        ctx.agent,
        url="",
        authentication="collected",
        options=CredentialCardOptions(fields=custom_fields, access_scope="task"),
    )
    path = credential_path(ctx.agent.home_paths.owner_home_dir, ref)
    row = read_json_object_report(path, context="test").payload
    verbatim_values = {
        "username": "  张伟_admin  ",
        "secret": "  S3crët!@#中文🙂  ",
        "tenant_code": "TENANT-009 ",
    }
    private_url = "https://portal.example.com:8443/internal/signin?redirect=/dashboard"
    body = {
        "reference": row["reference"],
        "decision": "submit",
        "message_id": row["message_id"],
        "chat_id": row["chat_id"],
        "fields": {**verbatim_values, "target_url": private_url},
    }
    assert resolve_private_interaction(ctx.agent, body, channel="feishu", actor_id="ou_dm_actor")["ok"]

    counts = _count_decrypts(monkeypatch)
    # shell 不接受外部 target_url 参数，前置拒绝零解密。
    with pytest.raises(CredentialInputError) as bad_shell:
        with authorized_credential_material(
            ctx.agent, ref, consumer="shell", target_url="https://portal.example.com:8443/internal"
        ):
            pass
    assert bad_shell.value.code == "TOOL_INVALID_ARGUMENTS"
    assert bad_shell.value.effect_outcome == "not_started"
    assert counts["decrypt"] == 0

    # 未知 consumer、错误 expected_generation 均前置拒绝且零解密。
    with pytest.raises(CredentialInputError) as bad_consumer:
        with authorized_credential_material(ctx.agent, ref, consumer="unknown_consumer"):
            pass
    assert bad_consumer.value.code == "TOOL_INVALID_ARGUMENTS"
    assert counts["decrypt"] == 0

    with pytest.raises(CredentialInputError) as bad_gen:
        with authorized_credential_material(ctx.agent, ref, consumer="shell", expected_generation=2):
            pass
    assert bad_gen.value.code == "CREDENTIAL_INPUT_UNAVAILABLE"
    assert bad_gen.value.effect_outcome == "not_started"
    assert counts["decrypt"] == 0

    # shell 正常读取：材料不可变、repr 不含秘密或 URL、yield 期间不持有文件锁。
    with authorized_credential_material(
        ctx.agent, ref, consumer="shell", expected_generation=1
    ) as material:
        assert isinstance(material, CredentialMaterial)
        assert isinstance(material.values, MappingProxyType)
        assert dict(material.values) == verbatim_values
        assert material.origin == "https://portal.example.com:8443"
        assert material.target_url == private_url
        assert material.credential_ref == ref and material.generation == 1
        assert material.owner_id == "feishu/user/ou_synthetic"
        assert material.actor_id == "ou_dm_actor"
        assert material.thread_id == ctx.thread.thread_id
        for text_repr in (repr(material), str(material)):
            assert ref in text_repr
            for hidden in (*verbatim_values.values(), "portal.example.com", "ou_dm_actor", ctx.thread.thread_id):
                assert hidden not in text_repr
        with pytest.raises(TypeError):
            material.values["secret"] = "mutated"  # type: ignore[index]
        # 验证 yield 期间不持有记录锁（非阻塞加锁立即成功）。
        with locked_json_path(path, blocking=False):
            pass
    assert counts["decrypt"] == 1
    assert current_credential_actor_scope(ctx.agent) == (
        "feishu/user/ou_synthetic",
        "ou_dm_actor",
        ctx.thread.thread_id,
    )

    # browser/http 校验同源：跨端口或跨域名零解密拒绝；同源通过。
    with pytest.raises(CredentialInputError) as wrong_origin:
        with authorized_credential_material(
            ctx.agent, ref, consumer="http", target_url="https://portal.example.com:9443/api"
        ):
            pass
    assert wrong_origin.value.code == "CREDENTIAL_NOT_AUTHORIZED"
    assert counts["decrypt"] == 1

    with authorized_credential_material(
        ctx.agent, ref, consumer="http", target_url="https://portal.example.com:8443/api/account"
    ) as http_mat:
        assert dict(http_mat.values) == verbatim_values
    assert counts["decrypt"] == 2

    # 读模式不重建缺失密钥。
    key_file = path.parent / "key.json"
    backup = key_file.read_bytes()
    key_file.unlink()
    try:
        with pytest.raises(CredentialInputError):
            with authorized_credential_material(ctx.agent, ref, consumer="shell"):
                pass
        assert not key_file.exists()
    finally:
        key_file.write_bytes(backup)


def test_c1_optional_browser_consumer_mac_binding_and_reinstall_rejection(tmp_path, monkeypatch):
    ctx = _dm_case(tmp_path)
    ctx.agent.config.enable_plugins = True
    store, active_install = _install_browser_lite_fixture(
        tmp_path, ctx.agent.home_paths.root, version="1.0", operation_prefix="bl-v1"
    )
    ref, _ = request_credential_card(
        ctx.agent,
        url="https://web.example.com/signin",
        authentication="collected",
    )
    path = credential_path(ctx.agent.home_paths.owner_home_dir, ref)
    row = read_json_object_report(path, context="test").payload
    expected_bc = {
        "installation_ref": active_install.installation_ref,
        "package_sha256": active_install.package_sha256,
        "activation_id": active_install.activation_id,
        "tool": "fill",
    }
    assert row["browser_consumer"] == expected_bc
    rendered_card = json.dumps(credential_card(row), ensure_ascii=False)
    assert "供此浏览器填表" in rendered_card
    assert_credential_record_signature(ctx.agent, row)

    # 篡改或未签名注入 browser_consumer 必须被签名校验拒绝。
    with pytest.raises(ValueError, match="原凭据卡身份无法核验"):
        assert_credential_record_signature(
            ctx.agent,
            {**row, "browser_consumer": {**expected_bc, "activation_id": "other-activation"}},
        )

    body = {
        "reference": row["reference"],
        "decision": "submit",
        "message_id": row["message_id"],
        "chat_id": row["chat_id"],
        "fields": {"username": "web_user", "secret": "WebSecret_99"},
    }
    assert resolve_private_interaction(ctx.agent, body, channel="feishu", actor_id="ou_dm_actor")["ok"]

    counts = _count_decrypts(monkeypatch)
    with authorized_credential_material(
        ctx.agent,
        ref,
        consumer="browser",
        target_url="https://web.example.com/signin",
        browser_consumer=expected_bc,
    ) as mat:
        assert dict(mat.browser_consumer or {}) == expected_bc
        assert mat.values["secret"] == "WebSecret_99"
    assert counts["decrypt"] == 1

    # 模拟插件换代/停用：旧卡固定绑定不得自动继承新激活代次，零解密拒绝；同卡 shell 仍正常可用。
    mismatched_bc = {**expected_bc, "activation_id": "new-activation-gen-2"}
    with pytest.raises(CredentialInputError) as bc_err:
        with authorized_credential_material(
            ctx.agent,
            ref,
            consumer="browser",
            target_url="https://web.example.com/signin",
            browser_consumer=mismatched_bc,
        ):
            pass
    assert bc_err.value.code == "CREDENTIAL_NOT_AUTHORIZED"
    assert bc_err.value.effect_outcome == "not_started"
    assert counts["decrypt"] == 1

    store.change_activation(revocation(active_install))
    with pytest.raises(CredentialInputError) as revoked_plugin_err:
        with authorized_credential_material(
            ctx.agent,
            ref,
            consumer="browser",
            target_url="https://web.example.com/signin",
        ):
            pass
    assert revoked_plugin_err.value.code == "CREDENTIAL_NOT_AUTHORIZED"
    assert counts["decrypt"] == 1

    # 未启用浏览器时同张卡仍可给 shell 使用。
    with authorized_credential_material(ctx.agent, ref, consumer="shell") as shell_mat:
        assert shell_mat.values["secret"] == "WebSecret_99"
    assert counts["decrypt"] == 2


# ---------- 2. DM/群跨 root 同 actor 同 thread 延续、collected 发现与严格拒绝矩阵 ----------


@pytest.mark.parametrize("kind", ["user", "main"])
def test_dm_same_actor_same_thread_cross_root_continuation_and_rejections(
    tmp_path, monkeypatch, kind
):
    ctx = _dm_case(tmp_path, kind=kind, actor_id="ou_same_user", conversation_id="oc_dm_thread_1")
    ref, _ = request_credential_card(
        ctx.agent,
        url="https://intranet.example.com/login",
        authentication="collected",
        options=CredentialCardOptions(access_scope="task"),
    )
    path = credential_path(ctx.agent.home_paths.owner_home_dir, ref)
    row = read_json_object_report(path, context="test").payload
    assert row["access_scope"] == "task"
    body = {
        "reference": row["reference"],
        "decision": "submit",
        "message_id": row["message_id"],
        "chat_id": row["chat_id"],
        "fields": {"username": "alice", "secret": "SyntheticDmPass_42"},
    }
    assert resolve_private_interaction(ctx.agent, body, channel="feishu", actor_id="ou_same_user")["ok"]

    # 同一会话进入新一轮 root 任务：status、find(collected)、shell、configure、HTTP 全部一致放行。
    _start_feishu_turn(ctx.agent, actor_id="ou_same_user", conversation_id="oc_dm_thread_1")
    status_view = manage_credential(ctx.agent, ref, operation="status")
    assert status_view["usable_in_current_task"] is True
    assert status_view["authentication"] == "collected"
    assert status_view["authentication_configured"] is False

    found = discover_owner_credentials(
        ctx.agent, url="https://intranet.example.com/account", operation="find"
    )
    assert found["selected_credential_ref"] == ref
    assert found["authentication"] == "collected"
    assert found["authentication_configured"] is False
    assert found["usable_in_current_task"] is True
    assert set(found["available_consumers"]) == {"run_command", "browser_fill"}

    with authorized_credential_material(ctx.agent, ref, consumer="shell") as mat:
        assert mat.values["secret"] == "SyntheticDmPass_42"

    cfg_res = configure_credential(ctx.agent, ref, authentication="basic")
    assert cfg_res["authentication"] == "basic" and cfg_res["authentication_configured"] is True
    found_after = discover_owner_credentials(
        ctx.agent, url="https://intranet.example.com/account", operation="find"
    )
    assert found_after["selected_credential_ref"] == ref
    assert found_after["authentication"] == "basic"
    assert found_after["authentication_configured"] is True
    assert "web_fetch" in found_after["available_consumers"]

    http_calls: list[str] = []

    def _send(req):
        http_calls.append(req.url)
        return RawResponseParts(200, {}, b'{"ok":true}', req.url)

    resp, _ = credential_authenticated_request(
        ctx.agent,
        ref,
        FetchRawRequest(
            tool="web_fetch",
            url="https://intranet.example.com/account",
            method="GET",
            headers={},
            data=None,
            timeout=5,
        ),
        send=_send,
    )
    assert resp.status == 200 and len(http_calls) == 1

    # 反例矩阵：不同线程、子代理、已结束 claim/过期 attempt、不同 owner 全部零解密且零外发。
    counts = _count_decrypts(monkeypatch)
    http_calls.clear()

    # (a) 子代理不继承主回合跨 root 扩展
    _child(ctx.agent)
    with pytest.raises(CredentialInputError):
        with authorized_credential_material(ctx.agent, ref, consumer="shell"):
            pass
    assert counts["decrypt"] == 0 and len(http_calls) == 0

    # (b) 不同线程（即使同一用户）拒绝跨会话借用 task 卡
    _start_feishu_turn(ctx.agent, actor_id="ou_same_user", conversation_id="oc_dm_thread_2")
    assert manage_credential(ctx.agent, ref, operation="status")["usable_in_current_task"] is False
    other_thread_find = discover_owner_credentials(
        ctx.agent, url="https://intranet.example.com/account", operation="find"
    )
    assert other_thread_find["selected_credential_ref"] == ""
    assert other_thread_find["usable_in_current_task"] is False
    with pytest.raises(CredentialInputError):
        with authorized_credential_material(ctx.agent, ref, consumer="shell"):
            pass
    with pytest.raises(ValueError):
        credential_authenticated_request(
            ctx.agent,
            ref,
            FetchRawRequest(
                tool="web_fetch",
                url="https://intranet.example.com/account",
                method="GET",
                headers={},
                data=None,
                timeout=5,
            ),
            send=_send,
        )
    assert counts["decrypt"] == 0 and len(http_calls) == 0

    # (c) 回到原线程但 claim 已结束或 attempt 过期（stale attempt）拒绝
    orig_thread, _, orig_claim = _start_feishu_turn(
        ctx.agent, actor_id="ou_same_user", conversation_id="oc_dm_thread_1"
    )
    ctx.agent._current_run_params = replace(ctx.agent._current_run_params, attempt_id="stale-attempt-9")
    with pytest.raises(CredentialInputError):
        with authorized_credential_material(ctx.agent, ref, consumer="shell"):
            pass
    assert counts["decrypt"] == 0

    ctx.agent._current_run_params = replace(ctx.agent._current_run_params, attempt_id="")
    ctx.agent.conversation_store.claims.finish(
        {"thread_id": orig_thread.thread_id, "claim_id": orig_claim["claim_id"], "status": "done"}
    )
    with pytest.raises(CredentialInputError):
        with authorized_credential_material(ctx.agent, ref, consumer="shell"):
            pass
    assert counts["decrypt"] == 0 and len(http_calls) == 0

    # (d) 另一个 owner 持有同一引用字符串时零解密零外发拒绝
    other_owner_ctx = _dm_case(
        tmp_path / "foreign_owner", kind="user", actor_id="ou_foreign", conversation_id="oc_dm_thread_1"
    )
    with pytest.raises(CredentialInputError):
        with authorized_credential_material(other_owner_ctx.agent, ref, consumer="shell"):
            pass
    assert counts["decrypt"] == 0 and len(http_calls) == 0


def test_group_continuation_same_name_private_card_switch_departure_and_lifecycle_matrix(
    tmp_path, monkeypatch
):
    ctx = _group_case(tmp_path, actor="ou_actor_a")
    # 群成员 A 在话题 1 申请自己的 collected 私密 task 卡（名称与群内其它卡同名“生产后台”）。
    _switch_actor(ctx, "ou_actor_a", conversation_id="oc_synthetic:thread:om_topic_1")
    priv_ref, _ = request_credential_card(
        ctx.agent,
        url="https://ops.example.com/signin",
        authentication="collected",
        options=CredentialCardOptions(access_scope="task", ttl_seconds=120),
    )
    priv_path = credential_path(ctx.agent.home_paths.owner_home_dir, priv_ref)
    priv_row = read_json_object_report(priv_path, context="test").payload
    priv_body = {
        "reference": priv_row["reference"],
        "decision": "submit",
        "message_id": priv_row["message_id"],
        "chat_id": priv_row["chat_id"],
        "fields": {
            "username": "actor_a_priv",
            "secret": "ActorAPrivateSecret_77",
            "credential_name": "生产后台",
        },
        "submitter": {"actor_id": "ou_actor_a", "chat_id": "oc_synthetic", "current_group_member": True},
    }
    assert resolve_private_interaction(ctx.agent, priv_body, channel="feishu", actor_id="ou_actor_a")["ok"]

    # 同一话题内成员 A 进入新 root：find 选中本人 collected task 卡，shell 授权通过。
    _switch_actor(ctx, "ou_actor_a", conversation_id="oc_synthetic:thread:om_topic_1")
    found_a = discover_group_credentials(
        ctx.agent, url="https://ops.example.com/dashboard", name="生产后台", operation="find"
    )
    assert found_a["selected_credential_ref"] == priv_ref
    assert found_a["authentication"] == "collected"
    assert found_a["authentication_configured"] is False
    assert found_a["usable_in_current_task"] is True

    with authorized_credential_material(ctx.agent, priv_ref, consumer="shell") as mat_a:
        assert mat_a.values["secret"] == "ActorAPrivateSecret_77"

    counts = _count_decrypts(monkeypatch)
    outbound: list[str] = []

    def _send(req):
        outbound.append(req.url)
        return RawResponseParts(200, {}, b"{}", req.url)

    # 同群不同话题（不同 thread_id）：成员 A 也不能借用原话题的 task 私卡。
    _switch_actor(ctx, "ou_actor_a", conversation_id="oc_synthetic:thread:om_topic_2")
    diff_topic = discover_group_credentials(
        ctx.agent, url="https://ops.example.com/dashboard", name="生产后台", operation="find"
    )
    assert diff_topic["selected_credential_ref"] == ""
    with pytest.raises(CredentialInputError):
        with authorized_credential_material(ctx.agent, priv_ref, consumer="shell"):
            pass
    assert counts["decrypt"] == 0 and len(outbound) == 0

    # 同一话题内另一位群成员 B 按同名“生产后台”查找或直传 ref：均看不到、不能解密他人私卡。
    _switch_actor(ctx, "ou_actor_b", conversation_id="oc_synthetic:thread:om_topic_1")
    found_b = discover_group_credentials(
        ctx.agent, url="https://ops.example.com/dashboard", name="生产后台", operation="find"
    )
    assert found_b["selected_credential_ref"] == ""
    assert priv_ref not in json.dumps(found_b, ensure_ascii=False)
    with pytest.raises(CredentialInputError):
        with authorized_credential_material(ctx.agent, priv_ref, consumer="shell"):
            pass
    assert counts["decrypt"] == 0 and len(outbound) == 0

    # 成员 B 建立一张群共享 group 卡；随后关闭群共享开关：A 在原话题 find 只见自己的 task 私卡，不见 B 的卡。
    shared_b_ref, _ = request_credential_card(
        ctx.agent,
        url="https://ops.example.com/signin",
        authentication="bearer",
        options=CredentialCardOptions(access_scope="group"),
    )
    shared_b_path = credential_path(ctx.agent.home_paths.owner_home_dir, shared_b_ref)
    shared_b_row = read_json_object_report(shared_b_path, context="test").payload
    assert resolve_private_interaction(
        ctx.agent,
        {
            "reference": shared_b_row["reference"],
            "decision": "submit",
            "message_id": shared_b_row["message_id"],
            "chat_id": shared_b_row["chat_id"],
            "fields": {"secret": "ActorBSharedToken_88", "credential_name": "生产后台"},
            "submitter": {"actor_id": "ou_actor_b", "chat_id": "oc_synthetic", "current_group_member": True},
        },
        channel="feishu",
        actor_id="ou_actor_b",
    )["ok"]

    ctx.agent._capability_config_runtime_snapshot.config.im_group_shared_credentials_enabled = False
    _switch_actor(ctx, "ou_actor_a", conversation_id="oc_synthetic:thread:om_topic_1")
    disabled_find_a = discover_group_credentials(
        ctx.agent, url="https://ops.example.com/dashboard", operation="find"
    )
    assert disabled_find_a["selected_credential_ref"] == priv_ref
    assert shared_b_ref not in json.dumps(disabled_find_a, ensure_ascii=False)
    # 群共享关闭时成员 C 查找不到任何人的凭据，也不能使用 B 的 group 卡。
    _switch_actor(ctx, "ou_actor_c", conversation_id="oc_synthetic:thread:om_topic_1")
    disabled_find_c = discover_group_credentials(
        ctx.agent, url="https://ops.example.com/dashboard", operation="find"
    )
    assert disabled_find_c["selected_credential_ref"] == ""
    assert disabled_find_c["credentials"] == []
    with pytest.raises(CredentialInputError):
        with authorized_credential_material(ctx.agent, shared_b_ref, consumer="shell"):
            pass
    assert counts["decrypt"] == 0

    ctx.agent._capability_config_runtime_snapshot.config.im_group_shared_credentials_enabled = True

    # 离群检查：成员 A 退群后，即使回到原话题也不能再延续使用或发现原私卡。
    _switch_actor(ctx, "ou_actor_a", conversation_id="oc_synthetic:thread:om_topic_1")
    ctx.raw["member_ids"].remove("ou_actor_a")
    with pytest.raises(CredentialInputError):
        discover_group_credentials(ctx.agent, url="https://ops.example.com/dashboard", operation="find")
    with pytest.raises(CredentialInputError):
        with authorized_credential_material(ctx.agent, priv_ref, consumer="shell"):
            pass
    assert counts["decrypt"] == 0 and len(outbound) == 0
    ctx.raw["member_ids"].append("ou_actor_a")

    # 老格式记录缺少可靠 thread 绑定或 submitter 绑定时，跨 root 不放宽。
    legacy_snapshot = read_json_object_report(priv_path, context="test").payload
    with tc._credential_mutation(ctx.agent, priv_path) as admission:
        tampered_legacy = copy(legacy_snapshot)
        tampered_legacy.pop("submitter_actor_id", None)
        tc._save_credential_row(admission, priv_path, tampered_legacy)
    with pytest.raises(CredentialInputError):
        with authorized_credential_material(ctx.agent, priv_ref, consumer="shell"):
            pass
    assert counts["decrypt"] == 0
    with tc._credential_mutation(ctx.agent, priv_path) as admission:
        tc._save_credential_row(admission, priv_path, legacy_snapshot)

    # 过期检查：超过 ready_expires_at 后零解密拒绝（不篡改全局时钟以免租约 claim 过期）。
    with tc._credential_mutation(ctx.agent, priv_path) as admission:
        expired_snapshot = copy(legacy_snapshot)
        expired_snapshot["ready_expires_at"] = time.time() - 5
        tc._save_credential_row(admission, priv_path, expired_snapshot)
    with pytest.raises(CredentialInputError) as exp_err:
        with authorized_credential_material(ctx.agent, priv_ref, consumer="shell"):
            pass
    assert exp_err.value.code == "CREDENTIAL_INPUT_UNAVAILABLE"
    assert counts["decrypt"] == 0
    with tc._credential_mutation(ctx.agent, priv_path) as admission:
        tc._save_credential_row(admission, priv_path, legacy_snapshot)

    # 替换与撤销：新卡替换旧卡后 find 选新卡；撤销后零解密拒绝。
    new_priv_ref, _ = request_credential_card(
        ctx.agent,
        url="https://ops.example.com/signin",
        authentication="collected",
        options=CredentialCardOptions(refresh_ref=priv_ref, access_scope="task"),
    )
    new_priv_path = credential_path(ctx.agent.home_paths.owner_home_dir, new_priv_ref)
    new_priv_row = read_json_object_report(new_priv_path, context="test").payload
    assert resolve_private_interaction(
        ctx.agent,
        {
            "reference": new_priv_row["reference"],
            "decision": "submit",
            "message_id": new_priv_row["message_id"],
            "chat_id": new_priv_row["chat_id"],
            "fields": {"username": "actor_a_v2", "secret": "ActorAPrivV2_99", "credential_name": "生产后台"},
            "submitter": {"actor_id": "ou_actor_a", "chat_id": "oc_synthetic", "current_group_member": True},
        },
        channel="feishu",
        actor_id="ou_actor_a",
    )["ok"]
    replaced_find = discover_group_credentials(
        ctx.agent, url="https://ops.example.com/dashboard", name="生产后台", operation="find"
    )
    assert replaced_find["selected_credential_ref"] == new_priv_ref

    assert manage_credential(ctx.agent, new_priv_ref, operation="revoke")["status"] == "revoked"
    with pytest.raises(CredentialInputError) as rev_err:
        with authorized_credential_material(ctx.agent, new_priv_ref, consumer="shell"):
            pass
    assert rev_err.value.code == "CREDENTIAL_INPUT_UNAVAILABLE"
    assert counts["decrypt"] == 0 and len(outbound) == 0


def test_job_bound_card_keeps_strict_job_rule_without_inheriting_thread_continuation(
    tmp_path, monkeypatch
):
    ctx = _group_case(tmp_path, actor="ou_actor_a")
    job_id = _scheduled_execution(ctx.agent)
    ref, _ = request_credential_card(
        ctx.agent,
        url="https://job.example.com/api",
        authentication="bearer",
        job_id=job_id,
        options=CredentialCardOptions(access_scope="task"),
    )
    path = credential_path(ctx.agent.home_paths.owner_home_dir, ref)
    row = read_json_object_report(path, context="test").payload
    assert resolve_private_interaction(
        ctx.agent,
        {
            "reference": row["reference"],
            "decision": "submit",
            "message_id": row["message_id"],
            "chat_id": row["chat_id"],
            "fields": {"secret": "JobSecret_55"},
            "submitter": {"actor_id": "ou_actor_a", "chat_id": "oc_synthetic", "current_group_member": True},
        },
        channel="feishu",
        actor_id="ou_actor_a",
    )["ok"]

    # 同一人同一会话但普通非监控新 root：job 凭据必须保持原监控隔离，不因会话延续而放行。
    _switch_actor(ctx, "ou_actor_a")
    counts = _count_decrypts(monkeypatch)
    with pytest.raises(CredentialInputError) as err:
        with authorized_credential_material(ctx.agent, ref, consumer="shell"):
            pass
    assert err.value.code == "CREDENTIAL_NOT_AUTHORIZED"
    assert counts["decrypt"] == 0


def test_request_credentials_tool_schema_documents_collected_consumers_and_body_encoding():
    spec = RequestCredentialsTool(SimpleNamespace()).model_spec
    auth_cfg_props = spec.input_schema["properties"]["auth_config"]["properties"]
    assert auth_cfg_props["body_encoding"]["enum"] == ["json", "form"]
    assert auth_cfg_props["body_encoding"]["default"] == "json"
    assert "run_command" in spec.description and "body_encoding" in spec.description
    assert validate_tool_input(
        {
            "operation": "configure",
            "credential_ref": "cred_" + "a" * 32,
            "authentication": "login",
            "auth_config": {
                "url": "https://example.com/login",
                "body_fields": {"user": "username", "pass": "secret"},
                "cookies": True,
                "body_encoding": "form",
            },
        },
        spec.input_schema,
    ).ok
    assert not validate_tool_input(
        {
            "operation": "configure",
            "credential_ref": "cred_" + "a" * 32,
            "authentication": "login",
            "auth_config": {"body_encoding": "xml"},
        },
        spec.input_schema,
    ).ok


# ---------- 3. 错表可修正、WS/HTTP 并发与重启重放、两个中断断点恢复与唯一唤醒 ----------


def _http_handler_probe(body: dict, *, actor_id: str):
    """构造最小 HTTP handler 探针以直接走生产 handle_private_interaction 入口。"""
    responses: list[tuple[int, dict]] = []
    handler = SimpleNamespace(
        headers={"Authorization": "Bearer local-ok", "X-Channel": "feishu", "X-User-Id": actor_id},
        _auth_middleware=SimpleNamespace(has_gateway_credential=lambda _headers: True),
        _read_json=lambda: body,
        _send_json=lambda code, payload: responses.append((code, payload)),
    )
    return handler, responses


def test_form_error_correction_concurrent_ws_http_and_restart_replay_emit_single_wake(tmp_path):
    ctx = _dm_case(tmp_path, actor_id="ou_cb_user", conversation_id="oc_cb_chat")
    ref, _ = request_credential_card(
        ctx.agent,
        url="",
        authentication="collected",
        options=CredentialCardOptions(access_scope="task", ttl_seconds=600),
    )
    path = credential_path(ctx.agent.home_paths.owner_home_dir, ref)
    row = read_json_object_report(path, context="test").payload
    arm_input_dependency(ctx.agent, row["input_id"])
    server = SimpleNamespace(agent=ctx.agent, paths=None, control_plane=None)

    # 1. 可改正错表（空密码、非法 URL）返回 400 CREDENTIAL_FORM_INVALID，不消费待填卡，0 个 wake。
    for invalid_fields in (
        {"username": "alice", "secret": "", "target_url": "https://svc.example.com/login"},
        {"username": "alice", "secret": "ValidSecret_1", "target_url": "not-a-valid-url"},
    ):
        bad_body = {
            "reference": row["reference"],
            "decision": "submit",
            "message_id": row["message_id"],
            "chat_id": row["chat_id"],
            "fields": invalid_fields,
        }
        handler, responses = _http_handler_probe(bad_body, actor_id="ou_cb_user")
        handle_private_interaction(handler, server)
        assert responses[0][0] == 400
        assert responses[0][1]["error_code"] == "CREDENTIAL_FORM_INVALID"
    assert read_json_object_report(path, context="test").payload["status"] == "pending"
    assert ctx.agent.conversation_store.wakes.pending() == []

    # 2. 修正后通过 WS 长连与 HTTP 并发回调同一张卡：密文只写一次、不延寿，仅产生 1 个 wake。
    valid_body = {
        "reference": row["reference"],
        "decision": "submit",
        "message_id": row["message_id"],
        "chat_id": row["chat_id"],
        "fields": {
            "username": "alice",
            "secret": "FirstSavedSecret_Only",
            "target_url": "https://svc.example.com/private/signin?from=card",
        },
    }
    ws_client = FeishuWsClient(app_id="app", app_secret="sec", on_payload=lambda _p: None)
    ws_outcomes: list[dict] = []

    def _ws_sink(card_event):
        action_val = card_event["value"]
        outcome = resolve_private_interaction(
            ctx.agent,
            {
                "reference": action_val["reference"],
                "decision": action_val["decision"],
                "message_id": card_event["message_id"],
                "chat_id": card_event["chat_id"],
                "fields": card_event["form_value"],
            },
            channel="feishu",
            actor_id=card_event["operator_open_id"],
        )
        ws_outcomes.append(outcome)
        return {"toast": {"type": "success" if outcome.get("ok") else "error", "content": outcome.get("message", "")}}

    ws_client.on_card_action = _ws_sink
    barrier = threading.Barrier(3)
    http_codes: list[int] = []

    def _run_ws():
        barrier.wait()
        ws_event = SimpleNamespace(
            event=SimpleNamespace(
                action=SimpleNamespace(
                    tag="button",
                    value={
                        "kind": "my_agent_task_credential",
                        "reference": row["reference"],
                        "decision": "submit",
                    },
                    form_value=dict(valid_body["fields"]),
                ),
                operator=SimpleNamespace(open_id="ou_cb_user"),
                context=SimpleNamespace(
                    open_message_id=row["message_id"],
                    open_chat_id=row["chat_id"],
                ),
            )
        )
        ws_client._handle_card_action(ws_event)
        ws_client._handle_card_action(ws_event)

    def _run_http(extra_secret: str):
        barrier.wait()
        body_variant = {
            **valid_body,
            "fields": {**valid_body["fields"], "secret": extra_secret},
        }
        handler, responses = _http_handler_probe(body_variant, actor_id="ou_cb_user")
        handle_private_interaction(handler, server)
        http_codes.append(responses[0][0])

    threads = [
        threading.Thread(target=_run_ws),
        threading.Thread(target=_run_http, args=("FirstSavedSecret_Only",)),
        threading.Thread(target=_run_http, args=("FirstSavedSecret_Only",)),
    ]
    for worker in threads:
        worker.start()
    for worker in threads:
        worker.join(timeout=5)

    assert len(ws_outcomes) == 1 and ws_outcomes[0]["ok"]
    assert http_codes == [200, 200]
    saved_first = read_json_object_report(path, context="test").payload
    first_ciphertext = saved_first["ciphertext"]
    first_deadline = saved_first["ready_expires_at"]
    assert len(ctx.agent.conversation_store.wakes.pending()) == 1

    # 3. 模拟进程重启（全新 Agent 实例）后重放带不同密码的同代回调：不重写密文、不延寿、不新增第二条 wake。
    restarted_agent = _agent(tmp_path, kind="user")
    restarted_agent.config.feishu_session_lock_enabled = False
    replay_body = {
        **valid_body,
        "fields": {**valid_body["fields"], "secret": "DifferentReplaySecret_Ignored"},
    }
    assert resolve_private_interaction(
        restarted_agent, replay_body, channel="feishu", actor_id="ou_cb_user"
    )["ok"]
    saved_after_replay = read_json_object_report(path, context="test").payload
    assert saved_after_replay["ciphertext"] == first_ciphertext
    assert saved_after_replay["ready_expires_at"] == first_deadline
    assert len(restarted_agent.conversation_store.wakes.pending()) == 1

    # 旧代与错 actor 在重放入口同样拒绝。
    wrong_actor_handler, wrong_actor_resp = _http_handler_probe(valid_body, actor_id="ou_other")
    handle_private_interaction(wrong_actor_handler, SimpleNamespace(agent=restarted_agent, paths=None, control_plane=None))
    assert wrong_actor_resp[0][0] == 403


@pytest.mark.parametrize("crash_point", ["after_ciphertext_before_resolve", "after_resolve_before_wake"])
def test_two_submission_crash_points_recover_on_restart_reconcile_with_single_wake(
    tmp_path, monkeypatch, crash_point
):
    ctx = _dm_case(tmp_path, actor_id="ou_crash_user", conversation_id="oc_crash_chat")
    task_id = ctx.agent._current_run_params.task_id
    ctx.agent.conversation_store.messages.append(
        {
            "thread_id": ctx.thread.thread_id,
            "role": "user",
            "content": "请登录站点拉取我的账户编号并回复。",
            "metadata": {"conversation_request_id": task_id},
        }
    )
    # 先释放前台模拟 claim，模拟原回合已在等待输入处归档让出执行车道。
    ctx.agent.conversation_store.claims.finish(
        {"thread_id": ctx.thread.thread_id, "claim_id": ctx.claim["claim_id"], "status": "done"}
    )
    # 临时恢复活动 claim 仅用于发起卡片登记，登记并 arm 后立即释放车道。
    wait_claim = ctx.agent.conversation_store.claims.acquire(
        {"thread_id": ctx.thread.thread_id, "task_id": task_id, "reason": "thread_goal_continue"}
    )
    ref, _ = request_credential_card(
        ctx.agent,
        url="https://recover.example.com/signin?debug=1",
        authentication="collected",
        options=CredentialCardOptions(access_scope="task"),
    )
    path = credential_path(ctx.agent.home_paths.owner_home_dir, ref)
    row = read_json_object_report(path, context="test").payload
    arm_input_dependency(ctx.agent, row["input_id"])
    ctx.agent.conversation_store.claims.finish(
        {"thread_id": ctx.thread.thread_id, "claim_id": wait_claim["claim_id"], "status": "done"}
    )

    body = {
        "reference": row["reference"],
        "decision": "submit",
        "message_id": row["message_id"],
        "chat_id": row["chat_id"],
        "fields": {"username": "rec_user", "secret": "RecoveredSecret_321"},
    }
    if crash_point == "after_ciphertext_before_resolve":
        monkeypatch.setattr(
            "agent_py_agent.agent.conversation.input_dependencies.publish_credential_resolution",
            lambda *_a, **_kw: (_ for _ in ()).throw(OSError("simulated crash before dependency resolve")),
        )
        with pytest.raises(OSError, match="simulated crash"):
            resolve_private_interaction(ctx.agent, body, channel="feishu", actor_id="ou_crash_user")
        assert read_json_object_report(path, context="test").payload["status"] == "ready"
        assert (
            read_json_object_report(input_dependency_path(ctx.agent, row["input_id"]), context="test").payload["status"]
            == "pending"
        )
        assert ctx.agent.conversation_store.wakes.pending() == []
    else:
        orig_raise = ctx.agent.conversation_store.wakes.raise_signal
        monkeypatch.setattr(
            ctx.agent.conversation_store.wakes,
            "raise_signal",
            lambda *_a, **_kw: (_ for _ in ()).throw(OSError("simulated crash before wake signal")),
        )
        with pytest.raises(OSError, match="simulated crash"):
            resolve_private_interaction(ctx.agent, body, channel="feishu", actor_id="ou_crash_user")
        monkeypatch.setattr(ctx.agent.conversation_store.wakes, "raise_signal", orig_raise)
        dep_saved = read_json_object_report(
            input_dependency_path(ctx.agent, row["input_id"]), context="test"
        ).payload
        assert dep_saved["status"] == "ready"
        assert dep_saved["credential_result"]["credential_ref"] == ref
        assert row["input_id"] in ctx.agent.conversation_store.tasks.load(task_id).input_dependency_refs
        assert ctx.agent.conversation_store.wakes.pending() == []

    # 模拟进程重启：不依赖飞书再次回调，新 Scheduler 首拍自动恢复并仅唤醒一次原任务。
    restarted = _agent(tmp_path, kind="user")
    restarted.config.feishu_session_lock_enabled = False
    restarted_replies: list[tuple[str, object]] = []
    restarted.channel_registry.register_adapter(
        "feishu",
        SimpleNamespace(
            send_control_card=lambda *_a, **_kw: {"message_id": "om_unused", "chat_id": "oc_crash_chat"},
            send_message=lambda target, content, **_kw: (
                restarted_replies.append((target, content))
                or {"message_id": "om_final_1", "chat_id": target}
            ),
        ),
        capabilities=ChannelCapabilities(control_cards=True, proactive=True),
    )
    model_calls: list[dict] = []

    class _ResumeBackend:
        name = "synthetic-resume"

        def probe_tool_capability(self):
            return ProviderToolCapability(
                provider="synthetic",
                endpoint="local://resume",
                model="synthetic",
                stream=False,
                native_supported=True,
                evidence="canonical_test_fixture",
            )

        def generate(self, prompt, **kwargs):
            model_calls.append({"prompt": prompt, "tools": [t["name"] for t in (kwargs.get("tools") or [])], "messages": kwargs.get("messages") or []})
            return ModelResponse(text="已完成受保护账户记录核实：ACCOUNT_MARKER_01。", backend=self.name)

    restarted.backend = _ResumeBackend()
    runtime = BackgroundMainAgentRuntime(
        agent=restarted,
        store=restarted.conversation_store,
        channels=restarted.delivery_service,
    )
    scheduler = BackgroundMainAgentScheduler(
        {"store": restarted.conversation_store, "runtime": runtime}
    )
    reports = scheduler.tick(now=time.time())
    assert len(reports) == 1
    assert len(model_calls) == 1
    assert set(CREDENTIAL_INPUT_RECOMMENDED_TOOLS) <= set(model_calls[0]["tools"])
    rendered_turn = json.dumps(model_calls[0]["messages"], ensure_ascii=False)
    assert ref in rendered_turn
    assert "https://recover.example.com/signin" in rendered_turn
    assert "?debug=1" not in rendered_turn
    assert "RecoveredSecret_321" not in rendered_turn
    assert CREDENTIAL_READY_CONTINUATION_GUIDANCE in rendered_turn
    assert not restarted.conversation_store.tasks.load(task_id).input_dependency_refs
    assert restarted_replies and restarted_replies[0][0] == "ou_crash_user"
    assert "ACCOUNT_MARKER_01" in restarted_replies[0][1].content

    # 原任务执行完成闭环后，迟到回调不再唤醒已完结任务，再次 tick 亦无二次调用。
    monkeypatch.undo()
    with pytest.raises(ValueError, match="已失效"):
        resolve_private_interaction(restarted, body, channel="feishu", actor_id="ou_crash_user")
    assert restarted.conversation_store.wakes.pending() == []
    assert scheduler.tick(now=time.time() + 1) == []
    assert len(model_calls) == 1


# ---------- 4. C3 公开 URL 净化、非凭据/终态保持、原 chat 投递与受限工具上界反例 ----------


def test_c3_wake_projection_url_sanitization_non_ready_invariants_and_tool_upper_bounds(tmp_path):
    # 1. 原任务公开路径保留（剥离 query/fragment），卡内新填私有路径或带 userinfo 降至规范 origin。
    pub_row = {
        "status": "ready",
        "credential_ref": "cred_" + "1" * 32,
        "generation": 2,
        "origin": "https://api.example.com:8443",
        "target_url": "https://api.example.com:8443/v1/login?secret_token=xyz#part",
        "public_target_url": "https://api.example.com:8443/v1/login",
        "authentication": "collected",
    }
    proj_pub = credential_wake_projection(pub_row)
    assert proj_pub == {
        "credential_ref": "cred_" + "1" * 32,
        "generation": 2,
        "origin": "https://api.example.com:8443",
        "target_url": "https://api.example.com:8443/v1/login",
        "authentication": "collected",
        "authentication_configured": False,
    }

    card_filled_row = {
        "status": "ready",
        "credential_ref": "cred_" + "2" * 32,
        "generation": 1,
        "origin": "https://hidden.example.com:443",
        "target_url": "https://hidden.example.com/secret-tenant-path/signin?k=v",
        "authentication": "login",
    }
    proj_priv = credential_wake_projection(card_filled_row)
    assert proj_priv["target_url"] == "https://hidden.example.com:443"
    assert proj_priv["authentication_configured"] is True
    assert credential_wake_projection({**card_filled_row, "status": "revoked"}) == {}

    # 2. lifecycle_wake_turn_trigger 投影与 origin_task / 推荐短名单。
    wake_sig = {
        "wake_signal_id": "wake-cred-1",
        "reason": "task_input_resolved",
        "source_agent_id": "task-1",
        "root_task_id": "task-1",
        "evidence_refs": [proj_pub["credential_ref"]],
        "metadata": {
            "schema_version": "task-input-event.v1",
            "input_id": proj_pub["credential_ref"] + ":2",
            "input_status": "ready",
            "input_provider": "credential",
            "input_run_id": "task-1",
            "credential_result": proj_pub,
        },
    }
    trigger_with_origin = lifecycle_wake_turn_trigger(
        "task_input_resolved", wake_sig, [], "登录测试站查询昨日账单"
    )
    assert trigger_with_origin.origin_task_attached is True
    assert trigger_with_origin.credential_ready is True
    facts = json.loads(trigger_with_origin.event_facts)
    assert facts["origin_task"] == "登录测试站查询昨日账单"
    assert facts["input_dependency"]["credential"] == proj_pub
    turn_text = current_turn_text("登录测试站查询昨日账单", trigger_with_origin)
    assert CREDENTIAL_READY_CONTINUATION_GUIDANCE in turn_text
    assert "原任务见下方origin_task" in turn_text
    rec_names, _ = turn_trigger_recommendation(trigger_with_origin) or ((), "")
    assert rec_names == CREDENTIAL_INPUT_RECOMMENDED_TOOLS

    # 过期/取消/旧无 credential_result 的唤醒不附加凭据续跑提示，保持原投影。
    expired_sig = {
        **wake_sig,
        "evidence_refs": [],
        "metadata": {**wake_sig["metadata"], "input_status": "expired"},
    }
    expired_trigger = lifecycle_wake_turn_trigger("task_input_resolved", expired_sig, ["req-1"])
    assert expired_trigger.credential_ready is False
    assert "credential" not in json.loads(expired_trigger.event_facts)["input_dependency"]
    assert CREDENTIAL_READY_CONTINUATION_GUIDANCE not in current_turn_text("任务", expired_trigger)
    assert turn_trigger_recommendation(expired_trigger) is None

    # 3. 工具预载与严格上界交集反例：仅允许 tool_search 或禁用 run_command 时绝不越界扩权。
    attrs: dict[str, object] = {}
    req = BackgroundRunRequest(
        thread_id="th-1",
        task_id="task-1",
        reason="task_input_resolved",
        wake_signal=wake_sig,
    )
    _apply_background_wake_attributes(attrs, req, None)
    assert set(CREDENTIAL_INPUT_RECOMMENDED_TOOLS) <= set(
        attrs["background_wake_signal"]["tool_names"]  # type: ignore[index]
    )

    # 反例 A：任务策略仅允许 ["tool_search"]
    search_only_req = bg_policy.BackgroundToolPolicyRequest(
        reason="task_input_resolved",
        wake_signal=wake_sig,
        policy_snapshot={"allowed_tools": ["tool_search"]},
    )
    assert bg_policy.background_allowed_tools(request=search_only_req) == ["tool_search"]
    assert bg_policy.background_initially_presented_tools(search_only_req) == frozenset({"tool_search"})

    # 反例 B：owner 策略禁用 run_command，任务白名单仅放行 tool_search + web_fetch + run_command
    narrowed_req = bg_policy.BackgroundToolPolicyRequest(
        reason="task_input_resolved",
        wake_signal=wake_sig,
        owner_policy=SimpleNamespace(disabled_tools=("run_command",)),
        policy_snapshot={"allowed_tools": ["tool_search", "web_fetch", "run_command"]},
    )
    assert set(bg_policy.background_allowed_tools(request=narrowed_req)) == {
        "tool_search",
        "web_fetch",
    }
    assert bg_policy.background_initially_presented_tools(narrowed_req) == frozenset(
        {"tool_search", "web_fetch"}
    )

    # 校验真实 Agent 工具快照与推荐节同样严格受限于收紧后的 allowed_tools。
    ctx = _dm_case(tmp_path)
    narrow_allowed = ["tool_search", "web_fetch"]
    params = _run_params(
        ctx.thread.thread_id,
        BackgroundRunRequest(
            thread_id=ctx.thread.thread_id,
            task_id=ctx.agent._current_run_params.task_id,
            reason="task_input_resolved",
            wake_signal={**wake_sig, "policy_snapshot": {"allowed_tools": narrow_allowed}},
        ),
        ctx.agent,
        thread=ctx.thread,
    )
    params.turn_trigger = trigger_with_origin
    preloaded_names = _background_preload_tool_names(params, params.task_attributes or {})
    assert set(params.allowed_tools or ()) == {"tool_search", "web_fetch"}
    assert preloaded_names <= {"tool_search", "web_fetch"}
    assert "web_fetch" in preloaded_names
    assert "run_command" not in preloaded_names
    assert "request_credentials" not in preloaded_names

    runtime_snap = ctx.agent.tools.runtime_snapshot(
        allowed_tools=params.allowed_tools, run_id=params.run_id
    )
    _, rec_section = _resolve_tool_sections(
        ToolSectionsRequest(
            agent=ctx.agent,
            user_prompt="查询数据",
            allowed_tools=params.allowed_tools,
            runtime_snapshot=runtime_snap,
            protocol_snapshot=ToolProtocolSnapshot(
                "fc1-test",
                "native",
                ProviderToolCapability(
                    provider="test",
                    endpoint="local://fc1",
                    model="test",
                    stream=False,
                    native_supported=True,
                    evidence="canonical_test_fixture",
                ),
            ),
            turn_trigger=trigger_with_origin,
        )
    )
    assert "- web_fetch：" in rec_section
    assert "run_command" not in rec_section
    assert "request_credentials" not in rec_section


def test_inactive_owner_card_saves_without_waking_ended_task_and_reconcile_retires_cleanly(tmp_path):
    ctx = _dm_case(tmp_path, actor_id="ou_owner_inactive", conversation_id="oc_owner_inactive")
    task_id = ctx.agent._current_run_params.task_id
    ref, _ = request_credential_card(
        ctx.agent,
        url="https://vault.example.com/api",
        authentication="collected",
    )
    path = credential_path(ctx.agent.home_paths.owner_home_dir, ref)
    row = read_json_object_report(path, context="test").payload
    assert row["access_scope"] == "owner"
    arm_input_dependency(ctx.agent, row["input_id"])
    ctx.agent.conversation_store.tasks.update_status({"task_id": task_id, "status": "cancelled"})

    result = resolve_private_interaction(
        ctx.agent,
        {
            "reference": row["reference"],
            "decision": "submit",
            "message_id": row["message_id"],
            "chat_id": row["chat_id"],
            "fields": {"username": "vault_u", "secret": "VaultPass_001"},
        },
        channel="feishu",
        actor_id="ou_owner_inactive",
    )
    assert result["ok"] is True and result["saved"] is True
    assert result["resume_status"] == "task_inactive"
    assert ctx.agent.conversation_store.wakes.pending() == []
    reconcile_input_dependencies(ctx.agent, task_id=task_id)
    assert ctx.agent.conversation_store.wakes.pending() == []
    assert credential_origin("https://vault.example.com/api") == "https://vault.example.com:443"
    assert resolve_input_dependency is not None


def test_group_topic_credential_wake_routes_back_to_original_topic(tmp_path):
    ctx = _group_case(tmp_path)
    topic_target = "oc_synthetic:thread:om_topic_resume"
    _switch_actor(ctx, "ou_actor_a", conversation_id=topic_target)
    thread_id = ctx.agent._current_run_params.task_attributes["conversation_thread_id"]
    task_id = ctx.agent._current_run_params.task_id
    ctx.agent.conversation_store.messages.append(
        {
            "thread_id": thread_id,
            "role": "user",
            "content": "在当前话题核实内部状态页指标并回帖。",
            "metadata": {"conversation_request_id": task_id},
        }
    )
    ref, _ = request_credential_card(
        ctx.agent,
        url="https://topic-ops.example.com/status",
        authentication="collected",
        options=CredentialCardOptions(access_scope="task"),
    )
    path = credential_path(ctx.agent.home_paths.owner_home_dir, ref)
    row = read_json_object_report(path, context="test").payload
    arm_input_dependency(ctx.agent, row["input_id"])
    active_claim = ctx.agent.conversation_store.claims.load(thread_id)
    ctx.agent.conversation_store.claims.finish(
        {"thread_id": thread_id, "claim_id": active_claim["claim_id"], "status": "done"}
    )
    sent_messages: list[tuple[str, object]] = []
    ctx.agent.channel_registry.register_adapter(
        "feishu",
        SimpleNamespace(
            send_control_card=lambda *_a, **_kw: {"message_id": "om_card_x", "chat_id": "oc_synthetic"},
            group_authority_snapshot=lambda _chat: dict(ctx.raw),
            send_message=lambda target, content, **_kw: (
                sent_messages.append((target, content))
                or {"message_id": "om_reply_topic", "chat_id": target}
            ),
        ),
        capabilities=ChannelCapabilities(control_cards=True, proactive=True),
    )

    class _TopicResumeBackend:
        name = "synthetic-topic-resume"

        def probe_tool_capability(self):
            return ProviderToolCapability(
                provider="synthetic",
                endpoint="local://topic-resume",
                model="synthetic",
                stream=False,
                native_supported=True,
                evidence="canonical_test_fixture",
            )

        def generate(self, prompt, **kwargs):
            return ModelResponse(text="话题指标已核实：TOPIC_OK_99。", backend=self.name)

    ctx.agent.backend = _TopicResumeBackend()
    assert resolve_private_interaction(
        ctx.agent,
        {
            "reference": row["reference"],
            "decision": "submit",
            "message_id": row["message_id"],
            "chat_id": row["chat_id"],
            "fields": {"username": "topic_u", "secret": "TopicSecret_88"},
            "submitter": {"actor_id": "ou_actor_a", "chat_id": "oc_synthetic", "current_group_member": True},
        },
        channel="feishu",
        actor_id="ou_actor_a",
    )["ok"]
    runtime = BackgroundMainAgentRuntime(
        agent=ctx.agent,
        store=ctx.agent.conversation_store,
        channels=ctx.agent.delivery_service,
    )
    scheduler = BackgroundMainAgentScheduler(
        {"store": ctx.agent.conversation_store, "runtime": runtime}
    )
    reports = scheduler.tick_thread(thread_id, now=time.time())
    assert len(reports) == 1
    assert sent_messages and sent_messages[0][0] == "oc_synthetic"
    assert sent_messages[0][0] != "ou_actor_a"
    assert "TOPIC_OK_99" in sent_messages[0][1].content
    # 错误路由只按结构化异常类型裁决，不解析自然语言文案。
    assert tc._map_credential_use_error(ValueError("任意已过期文案")).code == "CREDENTIAL_NOT_AUTHORIZED"
    assert tc._map_credential_use_error(tc._CredentialUnavailableError("x")).code == "CREDENTIAL_INPUT_UNAVAILABLE"
