"""#117：假 HTTP 的当前群历史合同，不读取真实凭据或发送消息。"""
from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

from agent_py_agent.agent.adapter import feishu
from agent_py_agent.agent.adapter import feishu_message_history as history
from agent_py_agent.agent.adapter.feishu_provider_facts import FeishuProviderFactError


# LLM: 本地测试消息构造器；只生成官方字段形状的假数据，不读取真实群消息。
# 函数用途: 给时间、发送者、群号和消息类型边界测试准备输入。
def message(number=1, *, sender="ou_a", text="hello", chat="oc_group", timestamp=101000, msg_type="text"):
    return {"message_id": f"om_{number}", "chat_id": chat, "create_time": str(timestamp),
            "sender": {"id": sender, "id_type": "open_id", "sender_type": "user"}, "msg_type": msg_type,
            "body": {"content": json.dumps({"text": text})}, "parent_id": "", "thread_id": ""}


# LLM: 测试分页响应工厂；只构造假提供商信封，不携带实际凭据或外部调用。
# 函数用途: 给分页、游标和中途失败测试准备可控的服务端响应。
def page(items=(), *, more=False, token=""):
    return {"items": list(items), "has_more": more, "page_token": token}


# LLM: pytest 隔离依赖夹具；用假 HTTP、假时间和可撤销证明替代外部服务。
# 函数用途: 让历史读取测试不碰真实凭据或飞书网络，也能观察每次请求。
@pytest.fixture
def environment(monkeypatch):
    config = SimpleNamespace(feishu_chat_history_enabled=True)
    policy = SimpleNamespace(network_enabled=True)
    home = SimpleNamespace(owner_kind="group", owner_provider="feishu", owner_id="providers/feishu/groups/oc_group")
    adapter = SimpleNamespace(app_id="fixture-app", _get_tenant_access_token=lambda: "fixture-only")
    agent = SimpleNamespace(home_paths=home, delivery_service=SimpleNamespace(
        registry=SimpleNamespace(adapter_for=lambda provider: adapter)))
    proof = {"provider": "feishu", "channel": "feishu", "chat_id": "oc_group", "app_id": adapter.app_id,
             "tenant_key": "fixture-tenant", "owner_id": "ou_owner", "owner_id_type": "open_id",
             "members_complete": True, "bot_is_member": True, "member_ids": ["ou_owner"]}
    monkeypatch.setattr(history, "capability_config_for_agent", lambda agent: config)
    monkeypatch.setattr(history, "deployment_capability_config", lambda home: config)
    monkeypatch.setattr(history, "resolve_effective_owner_policy", lambda home: policy)
    monkeypatch.setattr(history, "group_authority_snapshot", lambda agent, chat: dict(proof))
    replies, calls = [], []

    def transport(method, path, body, credential, *, timeout=10):
        assert method == "GET" and body == {} and credential == "fixture-only"
        assert path.startswith("/im/v1/messages?") and 0 < timeout <= history.HistoryLimits.BUDGET_SECONDS
        calls.append(parse_qs(urlsplit(path).query, keep_blank_values=True))
        result = replies.pop(0)
        if isinstance(result, Exception):
            raise result
        return {"code": 0, "data": result}

    monkeypatch.setattr(feishu, "_feishu_msg_api", transport)
    return SimpleNamespace(agent=agent, home=home, config=config, policy=policy, proof=proof,
                           replies=replies, calls=calls, adapter=adapter)


# LLM: 测试内统一调用入口；默认参数固定，覆盖项由当前测试显式给出。
# 函数用途: 用同一组时间范围调用正式历史 helper，避免各测试偷偷改口径。
def read(env, **kwargs):
    return history.list_group_messages(env.agent, "oc_group", start_time=100, end_time=200, **kwargs)


# LLM: pytest 协议回归；观察假 HTTP 查询及真实解析结果，保留秒与毫秒的差异。
# 函数用途: 检查时间单位、精确发送者过滤和返回字段都符合官方协议。
def test_official_query_seconds_item_milliseconds_exact_sender_and_contract(environment):
    env = environment
    env.replies.append(page([message(1), message(2, sender="ou_a-extra"), message(3, sender="ou_a", timestamp=201000)]))
    result = read(env, sender_ids=["ou_a"])
    assert result["schema_version"] == "channel_message_page.v1"
    assert {"chat_id", "items", "next_cursor", "has_more", "coverage", "fetched_count", "matched_count",
            "pages_read", "truncated", "window"} <= result.keys()
    assert [item["message_id"] for item in result["items"]] == ["om_1"]
    assert set(result["items"][0]) == {"message_id", "create_time", "sender_type", "sender_id", "sender_id_type",
                                      "msg_type", "text", "parent_id", "thread_id"}
    assert result["items"][0]["create_time"] == 101000
    assert result["fetched_count"] == 3 and result["matched_count"] == 1 and result["pages_read"] == 1
    assert not result["has_more"] and not result["next_cursor"]
    assert result["coverage"]["thread_replies_complete"] is False
    assert result["coverage"]["status"] == "complete"
    query = env.calls[0]
    assert query["container_id_type"] == ["chat"] and query["container_id"] == ["oc_group"]
    assert query["start_time"] == ["100"] and query["end_time"] == ["200"]
    assert query["sort_type"] == ["ByCreateTimeAsc"] and query["page_size"] == ["50"]


# LLM: pytest 参数边界回归；无效输入必须在任何 HTTP 前失败，不能用宽松转换放行。
# 函数用途: 检查反向时间、超大范围、布尔数值和错误列表类型都会被拒绝。
@pytest.mark.parametrize("arguments", [{"start_time": 2, "end_time": 1}, {"start_time": 0, "end_time": 604801},
    {"start_time": True, "end_time": 200}, {"start_time": float("nan"), "end_time": 200},
    {"start_time": 100, "end_time": 200, "limit": 0}, {"start_time": 100, "end_time": 200, "limit": 201},
    {"start_time": 100, "end_time": 200, "sender_ids": "ou_a"}])
def test_invalid_query_fails_before_http(environment, arguments):
    with pytest.raises(history.ChannelMessageHistoryError):
        history.list_group_messages(environment.agent, "oc_group", **arguments)
    assert not environment.calls


# LLM: pytest 权限矩阵回归；只改变夹具作用域，拒绝时不得发历史 HTTP 或暴露私有结果。
# 函数用途: 检查私聊、别群、错平台、关开关、禁网络和不完整群证明都不能读取。
@pytest.mark.parametrize("scope", ["private", "other_chat", "other_provider", "disabled", "network", "incomplete", "not_member"])
def test_scope_is_fail_closed_before_history_http(environment, scope):
    env = environment
    if scope == "private":
        env.home.owner_kind = "personal"
    elif scope == "other_chat":
        env.home.owner_id = "providers/feishu/groups/oc_other"
    elif scope == "other_provider":
        env.home.owner_provider = "other"
    elif scope == "disabled":
        env.config.feishu_chat_history_enabled = False
    elif scope == "network":
        env.policy.network_enabled = False
    else:
        env.proof["members_complete" if scope == "incomplete" else "bot_is_member"] = False
    with pytest.raises(history.ChannelMessageHistoryError):
        read(env)
    assert not env.calls


# LLM: pytest 部署上界回归；主人自己的配置不能突破共享部署关闭项。
# 函数用途: 检查本地打开开关也不能绕过管理员的总开关。
def test_deployment_flag_cannot_be_enabled_by_owner_local_config(environment, monkeypatch):
    monkeypatch.setattr(history, "deployment_capability_config", lambda home: SimpleNamespace(feishu_chat_history_enabled=False))
    with pytest.raises(history.ChannelMessageHistoryError):
        read(environment)
    assert not environment.calls


# LLM: pytest 续页回归；游标仅承载不可信续页状态，不授予跨查询或跨群权限。
# 函数用途: 检查小 limit 不会丢页尾，换查询后也不能沿用旧游标。
def test_cursor_resumes_without_skipping_page_tail_and_is_query_bound(environment):
    env = environment
    env.replies.extend([page([message(1)], more=True, token="provider-next"), page([message(2)])])
    first = read(env, limit=1)
    assert first["has_more"] and first["next_cursor"] and first["truncated"]
    assert env.calls[0]["page_size"] == ["1"]
    with pytest.raises(history.ChannelMessageHistoryError):
        history.list_group_messages(env.agent, "oc_group", start_time=100, end_time=201, cursor=first["next_cursor"], limit=1)
    second = read(env, cursor=first["next_cursor"], limit=1)
    assert [item["message_id"] for item in second["items"]] == ["om_2"]
    assert env.calls[1]["page_token"] == ["provider-next"]
    assert not second["has_more"]


# LLM: pytest 分页失败回归；只保留已完成且仍获授权的页，平台原始错误正文不得泄漏。
# 函数用途: 检查中途网络错误会诚实返回部分结果，而不会冒充完整历史。
def test_provider_error_keeps_only_completed_pages_and_safe_diagnostics(environment):
    env = environment
    env.replies.extend([page([message(1)], more=True, token="p2"), OSError("PRIVATE_BODY_AND_CREDENTIAL")])
    result = read(env)
    assert [item["message_id"] for item in result["items"]] == ["om_1"]
    assert result["pages_read"] == 1 and result["coverage"]["status"] == "partial"
    assert result["has_more"] and result["next_cursor"]
    assert "PRIVATE_BODY_AND_CREDENTIAL" not in json.dumps(result)


# LLM: 只把指定页的平台信封换成非0码；前面页仍走原假 HTTP，原 _read 负责映射原因码。
# 函数用途: 构造“第 N 页起平台返回某种失败”的传输替身。
def failing_from_page(env, number, failure):
    original = feishu._feishu_msg_api

    def transport(method, path, body, credential, *, timeout=10):
        response = original(method, path, body, credential, timeout=timeout)
        if len(env.calls) < number:
            return response
        if isinstance(failure, Exception):
            raise failure
        return failure

    return transport


# LLM: pytest 权限拒绝回归；平台文档码230027是读取消息的明确权限拒绝，已缓冲的页同样不得发布。
# 函数用途: 检查任一页遇到230027都整体失败并保留原因链，不返回部分页或页信封。
@pytest.mark.parametrize("failing_page", [1, 2])
def test_message_read_permission_rejection_is_fatal_and_drops_buffered_pages(environment, monkeypatch, failing_page):
    env = environment
    env.replies.extend([page([message(1)], more=True, token="p2"), page([message(2)])])
    monkeypatch.setattr(feishu, "_feishu_msg_api", failing_from_page(
        env, failing_page, {"code": 230027, "msg": "PRIVATE_PROVIDER_TEXT"}))
    with pytest.raises(history.ChannelMessageHistoryError) as failed:
        read(env)
    assert failed.value.error_code == "FEISHU_PROVIDER_READ_REJECTED"
    cause = failed.value.__cause__
    assert isinstance(cause, FeishuProviderFactError)
    assert (cause.reason_code, cause.provider_code, cause.stage) == ("FEISHU_PROVIDER_READ_REJECTED", 230027, "message")
    assert len(env.calls) == failing_page
    assert "PRIVATE_PROVIDER_TEXT" not in str(failed.value)


# LLM: pytest 暂态失败回归；传输、HTTP 不可用或其它非权限平台码在第1页之后仍诚实返回部分结果。
# 函数用途: 检查230027的失败关闭没有扩大到普通可用性故障。
@pytest.mark.parametrize("failure", [OSError("PRIVATE_TRANSPORT_TEXT"),
    HTTPError("https://invalid.example/", 503, "PRIVATE_HTTP_TEXT", {}, None),
    {"code": 99991400, "msg": "PRIVATE_PROVIDER_TEXT"}], ids=["transport", "http_503", "provider_frequency"])
def test_transient_failure_after_first_page_keeps_honest_partial_result(environment, monkeypatch, failure):
    env = environment
    env.replies.extend([page([message(1)], more=True, token="p2"), page([message(2)])])
    monkeypatch.setattr(feishu, "_feishu_msg_api", failing_from_page(env, 2, failure))
    result = read(env)
    assert [item["message_id"] for item in result["items"]] == ["om_1"]
    assert result["coverage"]["status"] == "partial" and "provider_read_failed" in result["coverage"]["reasons"]
    assert result["has_more"] and result["next_cursor"] and result["truncated"]
    assert result["coverage"]["provider_error"]["stage"] == "message"
    assert "PRIVATE" not in json.dumps(result)


# LLM: pytest 分页停机回归；页数上限、重复或缺失 token 都必须留有覆盖缺口。
# 函数用途: 检查坏游标和到达预算时不会误报已经读完。
def test_page_cap_repeated_cursor_and_missing_cursor_are_not_complete(environment):
    env = environment
    env.replies.extend(page([message(i)], more=True, token=f"p{i}") for i in range(1, 6))
    result = read(env)
    assert len(env.calls) == 5 and result["has_more"] and result["truncated"]
    assert result["coverage"]["status"] == "partial"
    env.calls.clear()
    env.replies.extend([page([message(1)], more=True, token="p2"), page([message(2)], more=True, token="p2")])
    result = read(env)
    assert len(env.calls) == 2 and "repeated_cursor" in result["coverage"]["reasons"]
    env.replies.append(page([], more=True))
    result = read(env)
    assert "missing_cursor" in result["coverage"]["reasons"] and result["coverage"]["status"] == "partial"


# LLM: pytest 消息内容边界回归；明确标注截断/不支持项，且不猜测机器人身份类型。
# 函数用途: 检查超长、删除、跨群和机器人发送者数据都按原始事实处理。
def test_truncation_deleted_cross_chat_and_sender_type_not_guessed(environment):
    env = environment
    app = message(1, text="x" * 3000)
    app["sender"] = {"id": "app_exact", "id_type": "app_id", "sender_type": "app"}
    deleted = message(2)
    deleted["deleted"] = True
    env.replies.append(page([app, deleted, message(3, chat="oc_other")]))
    result = read(env)
    assert len(result["items"]) == 1 and len(result["items"][0]["text"]) == 2000
    assert result["items"][0]["sender_id_type"] == "app_id" and result["items"][0]["sender_type"] == "app"
    assert result["truncated"] and result["coverage"]["status"] == "partial"


# LLM: pytest 假时钟回归；到期限后不得启动新的历史 HTTP，不声称已有请求会被硬中止。
# 函数用途: 检查耗时预算能阻止继续翻下一页。
def test_budget_stops_before_another_history_request(environment, monkeypatch):
    env, clock = environment, [0.0]
    monkeypatch.setattr(history.time, "monotonic", lambda: clock[0])
    env.replies.append(page([message(1)], more=True, token="p2"))
    original = feishu._feishu_msg_api

    def slow(*args, **kwargs):
        result = original(*args, **kwargs)
        clock[0] = 9.0
        return result

    monkeypatch.setattr(feishu, "_feishu_msg_api", slow)
    result = read(env)
    assert len(env.calls) == 1 and "time_budget" in result["coverage"]["reasons"]
    assert result["items"] and result["has_more"]


# LLM: pytest 实时撤权回归；即使已有私有页，发表前证明改变也必须整体丢弃。
# 函数用途: 检查机器人被移出群后，先前读到的消息不会再交出来。
def test_revoked_live_proof_discards_previously_read_private_items(environment, monkeypatch):
    env, checks = environment, []
    env.replies.extend([page([message(1)], more=True, token="p2"), page([message(2)])])

    def proof(agent, chat):
        checks.append(chat)
        return {**env.proof, "bot_is_member": len(checks) == 1}

    monkeypatch.setattr(history, "group_authority_snapshot", proof)
    with pytest.raises(history.ChannelMessageHistoryError):
        read(env)
    assert len(env.calls) == 1


# LLM: pytest 新接口冻结回归；核对完整参数类型、默认值和返回类型，不修改公共签名。
# 函数用途: 防止 helper 表面参数相同，但类型约定悄悄变化。
def test_frozen_new_helper_signature_includes_exact_types():
    import ast
    import inspect

    expected = ast.parse("def list_group_messages(agent: object, chat_id: str, *, start_time: int, end_time: int, sender_ids: tuple[str, ...]=(), cursor: str='', limit: int=50) -> dict[str, object]: pass").body[0]
    actual = ast.parse(inspect.getsource(history.list_group_messages)).body[0]
    assert ast.dump(actual.args) == ast.dump(expected.args)
    assert ast.dump(actual.returns) == ast.dump(expected.returns)


# LLM: pytest 恶形游标回归；固定深层假输入必须变成安全参数失败，不能触达网络。
# 函数用途: 检查很深的 JSON 游标不会把内部异常抛给调用者。
def test_deeply_nested_untrusted_cursor_is_safe_argument_failure(environment):
    import base64

    cursor = base64.urlsafe_b64encode(("[" * 2000 + "0" + "]" * 2000).encode()).decode()
    with pytest.raises(history.ChannelMessageHistoryError) as failed:
        read(environment, cursor=cursor)
    assert failed.value.error_code == "TOOL_INVALID_ARGUMENTS" and not environment.calls


# LLM: pytest 读取预算回归；预算按提供商实际返回行计算，不以筛选结果为零无限续读。
# 函数用途: 检查一个匹配项都没有时也会按总读取量及时停下。
def test_fetched_message_budget_applies_even_when_filter_matches_nothing(environment):
    env = environment
    env.replies.extend(page([message(50 * p + i, sender="other") for i in range(50)], more=True, token=f"p{p + 1}")
                       for p in range(4))
    result = read(env, sender_ids=["ou_a"], limit=200)
    assert len(env.calls) == 4 and result["fetched_count"] == 200 and result["matched_count"] == 0
    assert result["has_more"] and "message_budget" in result["coverage"]["reasons"]


# LLM: pytest FD 回归；在同一隔离进程重复正式读取入口，只比较观察到的句柄增量。
# 函数用途: 检查反复读历史不会留下越来越多的文件描述符。
def test_repeated_reader_loops_leave_no_file_descriptors(environment):
    import os
    import sys

    directory = "/dev/fd" if sys.platform == "darwin" else "/proc/self/fd"
    before = len(os.listdir(directory))
    environment.replies.extend(page() for _ in range(20))
    for _ in range(20):
        result = read(environment)
        assert not result["items"] and result["coverage"]["status"] == "complete"
    assert len(os.listdir(directory)) - before == 0
