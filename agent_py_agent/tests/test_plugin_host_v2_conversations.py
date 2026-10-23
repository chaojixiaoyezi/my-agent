# LLM: B1领域测试使用真实临时ConversationStore；A0发行器尚未提供，仅在此替换其上下文解析边界，不声称HTTP授权验收。
# 模块用途: 验证线程生命周期、持久幂等、CAS以及完整可见历史。
from __future__ import annotations

import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import pytest

from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.plugin_host import contracts
from agent_py_agent.agent.plugin_host.conversations import ConversationDomain
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.plugin_host.schemas import canonical_json, thaw
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home


# LLM: 仅测试宿主注入的游标端口；生产MAC实现归A，不在B复制其授权或游标模块。
# 类用途: 给领域测试提供可验证且可重建的游标编解码器。
class TestCursorCodec:
    __test__ = False

    # LLM: 测试固定密钥不进入任何生产发行器。
    # 函数用途: 编码测试游标。
    def encode(self, value):
        raw = canonical_json(value).encode().hex()
        return raw + "." + hmac.new(b"test-only", raw.encode(), hashlib.sha256).hexdigest()

    # LLM: 先核MAC，坏游标不返回内容。
    # 函数用途: 解码测试游标。
    def decode(self, value):
        try:
            raw, signature = value.rsplit(".", 1)
            if not hmac.compare_digest(signature, hmac.new(b"test-only", raw.encode(), hashlib.sha256).hexdigest()):
                raise ValueError
            return json.loads(bytes.fromhex(raw))
        except (ValueError, TypeError):
            raise HostAPIError("CURSOR_EXPIRED", 409) from None


# LLM: 临时源只认可本fixture实际登记的AuthorizedCall对象；copy/replace伪造、撤销和错误owner必须拒绝。
# 类用途: 隔离A0未到位的发行器，真实读写两个owner的规范会话存储。
class Harness:
    # LLM: 不使用真实账号或插件凭据，所有文件位于pytest临时目录。
    # 函数用途: 建立两个独立owner及B1领域端口。
    def __init__(self, tmp_path, monkeypatch):
        self.homes = {name: resolve_owner_home(tmp_path, OwnerIdentity("local", "user", name)) for name in ("alice", "bob")}
        self.stores = {name: ConversationStore(home.data_dir / "conversations") for name, home in self.homes.items()}
        self.authorized = {}
        self.revoked = False
        self.default = ""
        monkeypatch.setattr(contracts, "resolve_read_context", self.resolve)
        self.domain = ConversationDomain(self.store_for, TestCursorCodec(), default_thread=self.is_default)

    # LLM: 原上下文来源必须已登记，撤销先于任何领域读写。
    # 函数用途: 模拟A0的精确对象来源核验，不按DTO字段自证授权。
    def resolve(self, authorized):
        if self.revoked or self.authorized.get(id(authorized)) is not authorized:
            raise HostAPIError("PLUGIN_TOKEN_INVALID", 401)
        home = self.homes[authorized.effective_owner.owner_id]
        return contracts.OwnerReadContext(home.identity, home, authorized.binding)

    # LLM: store选择只读宿主上下文，不能从payload取路径。
    # 函数用途: 返回真实目标owner的临时Store。
    def store_for(self, context):
        return self.stores[context.identity.owner_id]

    # LLM: 默认会话事实来自宿主，不由客户端指定。
    # 函数用途: 为归档测试报告当前默认线程。
    def is_default(self, context, thread_id):
        return self.default == thread_id

    # LLM: scope和capabilities是测试发行器登记的固定范围，未授权线程不会自动加入。
    # 函数用途: 构造一个已登记的领域调用。
    def request(self, action, payload, *, key=None, owner="alice", installation="install-one", caps=None, explicit=(), activation="b" * 64):
        identity = self.homes[owner].identity
        grant = {"installation_ref": installation, "package_sha256": "a" * 64, "activation_id": activation,
                 "grant_revision": 1, "threads": {"created_by_installation": True, "explicit_ids": explicit}}
        binding = contracts.VerifiedPluginBinding(grant, object(), identity, contracts.PluginIngressSource(object(), object()),
                                                  caps if caps is not None else {"read", "chat", "chat.history", "threads.manage"})
        call = contracts.Call("plugin_host_call.v2", "conversations." + action, payload, key)
        authorized = contracts.AuthorizedCall(call, binding, identity, {"threads": grant["threads"]}, None)
        self.authorized[id(authorized)] = authorized
        return authorized

    # LLM: 调用真实领域入口，而不是直接断言存储元数据。
    # 函数用途: 构造并执行一条B1调用。
    def call(self, action, payload, **kwargs):
        return self.domain.call(self.request(action, payload, **kwargs))

    # LLM: 每个创建意图用独立稳定键，重放测试显式复用。
    # 函数用途: 创建线程并返回公共投影。
    def create(self, title="首个线程", key="create_thread_001", **kwargs):
        result = self.call("threads.create", {"title": title}, key=key, **kwargs)
        assert result is not None
        assert result.http_status in {200, 201}
        return thaw(result.data)


# LLM: 测试各自使用独立真实文件，避免借用部署或既有会话。
# 函数用途: 提供领域测试环境。
@pytest.fixture
def case(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch)


# LLM: 六动作必须在真实Store上持久化，公共字段不得包含宿主路径。
# 函数用途: 验证创建、读取、列表、重命名、归档和恢复。
def test_thread_lifecycle_and_cas(case):
    thread = case.create()
    tid = thread["thread_id"]
    assert thread["revision"] == 1 and thread["workspace_ref"] == "default"
    assert thread["created_by_installation"] is True
    assert str(case.homes["alice"].home_dir) not in canonical_json(thread)
    assert thaw(case.call("threads.get", {"thread_id": tid}).data) == thread
    assert thaw(case.call("threads.list", {}).data)["items"] == [thread]
    renamed = case.call("threads.rename", {"thread_id": tid, "title": "新名", "expected_revision": 1}, key="rename_thread_001")
    assert renamed.data["revision"] == 2 and renamed.data["title"] == "新名"
    with pytest.raises(HostAPIError, match=".*") as error:
        case.call("threads.archive", {"thread_id": tid, "expected_revision": 1}, key="archive_stale_001")
    assert (error.value.code, error.value.status) == ("REVISION_CONFLICT", 409)
    archived = case.call("threads.archive", {"thread_id": tid, "expected_revision": 2}, key="archive_thread_1")
    assert archived.data["status"] == "archived" and archived.data["revision"] == 3
    assert not case.call("threads.list", {}).data["items"]
    restored = case.call("threads.restore", {"thread_id": tid, "expected_revision": 3}, key="restore_thread_1")
    assert restored.data["status"] == "active" and restored.data["revision"] == 4


# LLM: 重放结果来自同次原子提交，后续改名或重建服务不能改写原回执。
# 函数用途: 验证跨服务重建的幂等与冲突，不仅比较新生成ID。
def test_idempotency_replay_and_different_payload(case):
    original = case.create()
    case.domain = ConversationDomain(case.store_for, TestCursorCodec(), default_thread=case.is_default)
    assert case.create() == original
    with pytest.raises(HostAPIError) as error:
        case.create("不同标题")
    assert (error.value.code, error.value.status) == ("IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD", 409)
    payload = {"thread_id": original["thread_id"], "title": "第二名", "expected_revision": 1}
    first = case.call("threads.rename", payload, key="rename_replay_001")
    case.call("threads.rename", {**payload, "title": "第三名", "expected_revision": 2}, key="rename_replay_002")
    assert case.call("threads.rename", payload, key="rename_replay_001").data == first.data


# LLM: 并发测试共享真实文件锁，不以mock call次数代替磁盘状态。
# 函数用途: 验证同键并发创建只落一个线程，CAS只允许一个胜者。
def test_concurrent_create_and_cas(case):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: case.create(), range(4)))
    assert len({row["thread_id"] for row in results}) == 1
    assert len(case.stores["alice"].threads.list()) == 1
    tid = results[0]["thread_id"]

    # LLM: 返回结构化结果，异常只在测试层收集以便断言。
    # 函数用途: 竞争同一修订的重命名。
    def rename(index):
        try:
            return case.call("threads.rename", {"thread_id": tid, "title": str(index), "expected_revision": 1},
                             key=f"rename_compete_{index:02d}").http_status
        except HostAPIError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(map(str, pool.map(rename, range(2)))) == ["200", "REVISION_CONFLICT"]


# LLM: 不能通过存在性错误区分另一owner或另一安装的线程。
# 函数用途: 验证跨owner、跨安装与路径伪造统一返回404。
@pytest.mark.parametrize("target", ["other_owner", "other_installation", "path"])
def test_resource_scope_hides_foreign_threads(case, target):
    tid = case.create()["thread_id"]
    options = {"owner": "bob"} if target == "other_owner" else {"installation": "install-two"}
    if target == "path":
        tid, options = "../secrets", {}
    with pytest.raises(HostAPIError) as error:
        case.call("threads.get", {"thread_id": tid}, **options)
    assert (error.value.code, error.value.status) == ("RESOURCE_NOT_FOUND", 404)


# LLM: 归档只改变线程状态，不暗停活跃任务/目标或默认会话。
# 函数用途: 验证三种忙碌原因都拒绝归档。
@pytest.mark.parametrize("busy", ["default", "turn", "goal", "claim"])
def test_archive_rejects_busy_thread(case, busy):
    tid = case.create()["thread_id"]
    store = case.stores["alice"]
    if busy == "default":
        case.default = tid
    elif busy == "turn":
        store.threads.update_atomic(tid, lambda row: replace(row, active_task_ids=("task-one",)))
    elif busy == "claim":
        assert store.claims.acquire({"thread_id": tid, "task_id": "task-claim"}) is not None
    else:
        store.goals.create({"thread_id": tid, "objective": "未完成目标"})
    with pytest.raises(HostAPIError) as error:
        case.call("threads.archive", {"thread_id": tid, "expected_revision": 1}, key="archive_busy_001")
    assert (error.value.code, error.value.status) == ("THREAD_BUSY", 409)
    assert store.threads.require(tid).status == "active"


# LLM: read不隐式授历史或管理；伪造DTO与撤销先于重放。
# 函数用途: 验证能力和真实来源边界。
def test_capability_forgery_and_revocation(case):
    tid = case.create()["thread_id"]
    with pytest.raises(HostAPIError) as error:
        case.call("messages.list", {"thread_id": tid}, caps={"read"})
    assert error.value.code == "CAPABILITY_DENIED"
    request = case.request("threads.get", {"thread_id": tid})
    with pytest.raises(HostAPIError) as error:
        case.domain.call(replace(request))
    assert error.value.status == 401
    case.revoked = True
    with pytest.raises(HostAPIError) as error:
        case.create()
    assert error.value.status == 401


# LLM: 目录分页冻结排序与过滤；有变化时明确过期，不假称覆盖。
# 函数用途: 验证线程列表分页、过滤绑定与目录变化。
def test_thread_cursor_binding_and_snapshot(case):
    first, second = case.create(), case.create("第二", key="create_thread_002")
    page = thaw(case.call("threads.list", {"limit": 1}).data)
    assert page["items"] == [first] and not page["complete"]
    tail = thaw(case.call("threads.list", {"limit": 1, "cursor": page["next_cursor"]}).data)
    assert tail["items"] == [second] and tail["complete"]
    with pytest.raises(HostAPIError) as error:
        case.call("threads.list", {"limit": 1, "status": "all", "cursor": page["next_cursor"]})
    assert error.value.code == "CURSOR_EXPIRED"
    case.create("第三", key="create_thread_003")
    with pytest.raises(HostAPIError) as error:
        case.call("threads.list", {"limit": 1, "cursor": page["next_cursor"]})
    assert error.value.code == "CURSOR_EXPIRED"


# LLM: 使用原消息追加入口，包含实际display检查点；测试不造paired-turn摘要代替原历史。
# 函数用途: 写一条真实canonical消息。
def append_message(case, tid, role, content, metadata=None):
    return case.stores["alice"].messages.append({"thread_id": tid, "role": role, "content": content,
                                                "metadata": metadata or {"turn_id": "turn-one"}})


# LLM: 宿主检查点关联必须满足原校验器；thinking记录只作为排除对照。
# 函数用途: 经原入口保存公开过程块。
def append_display(case, tid, kind, payload):
    from agent_py_agent.agent.conversation.display_checkpoint import display_checkpoint_metadata

    request_id = f"bg-main:{tid}:test-turn"
    metadata = display_checkpoint_metadata(thread_id=tid, task_id="task-one", request_id=request_id,
                                           kind=kind, phase="completed", block_id=f"{request_id}:{kind}", payload=payload)
    return case.stores["alice"].messages.append_display_checkpoint(tid, metadata)


# LLM: 按原存储顺序恢复所有可见角色，不丢未配对用户、过程块或工具结果，不暴露系统/思考/工具原始秘密。
# 函数用途: 验证完整可见历史及公开结果白名单。
def test_history_complete_visible_sequence_and_privacy(case):
    tid = case.create()["thread_id"]
    append_message(case, tid, "user", "第一问")
    append_message(case, tid, "user", "补充问题")
    append_message(case, tid, "system", "INTERNAL_SYSTEM_PROMPT")
    append_message(case, tid, "assistant", "PRIVATE_REASONING", {"phase": "analysis"})
    append_display(case, tid, "thinking_completed", {"text": "PRIVATE_REASONING_2"})
    append_display(case, tid, "assistant_completed", {"process": True, "text": "公开进展"})
    append_display(case, tid, "tool_completed", {"tool": "read_file", "ok": True,
                                                "arguments": {"password": "CREDENTIAL_SECRET"},
                                                "output": "/private/host-control-file"})
    append_message(case, tid, "assistant", "最后回答", {"turn_id": "turn-one", "media_refs": ["artifact-one", "/private/file"]})
    append_message(case, tid, "user", "尚未回复")
    data = thaw(case.call("messages.list", {"thread_id": tid}).data)
    assert data is not None
    rows = data["items"]
    assert [(row["role"], row["phase"]) for row in rows] == [
        ("user", "final"), ("user", "final"), ("assistant", "commentary"),
        ("tool", "tool_result"), ("assistant", "final"), ("user", "final")]
    assert [rows[index]["content"] for index in (0, 1, 2, 4, 5)] == ["第一问", "补充问题", "公开进展", "最后回答", "尚未回复"]
    assert json.loads(rows[3]["content"])["ok"] is True
    assert rows[4]["media_refs"] == ["artifact-one"]
    assert [row["seq"] for row in rows] == list(range(1, 7))
    expected_keys = {"message_id", "thread_id", "turn_id", "seq", "role", "phase", "content", "content_truncated",
                     "content_bytes", "media_refs", "created_at"}
    assert all(set(row) == expected_keys for row in rows)
    assert data["complete"] and not data["load_errors"]
    assert all(secret not in canonical_json(data) for secret in ("INTERNAL_SYSTEM", "PRIVATE_REASONING", "CREDENTIAL_SECRET", "/private/"))


# LLM: 不能对多字节UTF8使用字符数或replacement解码凑预算；报告完整公开内容原字节数。
# 函数用途: 验证中文和emoji截断边界及默认预算。
@pytest.mark.parametrize("budget,expected", [(1, ""), (3, "鲸"), (6, "鲸"), (7, "鲸🐳"), (8, "鲸🐳A")])
def test_history_utf8_truncation(case, budget, expected):
    tid = case.create()["thread_id"]
    append_message(case, tid, "user", "鲸🐳A")
    data = thaw(case.call("messages.list", {"thread_id": tid, "max_content_bytes": budget}).data)
    assert data is not None
    row = data["items"][0]
    assert row["content"] == expected and row["content_bytes"] == 8
    assert row["content_truncated"] is (budget < 8)


# LLM: 每页都重建Domain以证明游标不依赖进程内缓存；快照外追加留到下一次查询。
# 函数用途: 验证多页全内容恢复及并发追加隔离。
def test_history_cursor_restores_order_and_excludes_appends(case):
    tid = case.create()["thread_id"]
    expected = [f"用户{index}🐳" for index in range(7)]
    for content in expected:
        append_message(case, tid, "user", content)
    page = thaw(case.call("messages.list", {"thread_id": tid, "limit": 2}).data)
    assert page is not None
    snapshot, collected = page["snapshot_id"], list(page["items"])
    append_message(case, tid, "user", "快照之后")
    for _ in range(4):
        if page["complete"]:
            break
        case.domain = ConversationDomain(case.store_for, TestCursorCodec(), default_thread=case.is_default)
        page = thaw(case.call("messages.list", {"thread_id": tid, "limit": 2, "cursor": page["next_cursor"]}).data)
        assert page["snapshot_id"] == snapshot
        collected.extend(page["items"])
    assert page["complete"] and [row["content"] for row in collected] == expected
    assert len({row["message_id"] for row in collected}) == 7
    assert len(case.call("messages.list", {"thread_id": tid}).data["items"]) == 8


# LLM: 包括同大小同首尾的中段改写，单看mtime或首尾不够；删除/替换不能变空成功。
# 函数用途: 验证旧游标对retention与原位改写失败关闭。
@pytest.mark.parametrize("mutation", ["truncate", "rewrite_middle", "replace"])
def test_history_cursor_expires_on_rewrite(case, mutation):
    tid = case.create()["thread_id"]
    for index in range(5):
        append_message(case, tid, "user", str(index) + "A" * 6000)
    page = thaw(case.call("messages.list", {"thread_id": tid, "limit": 1}).data)
    assert page is not None
    path = case.stores["alice"].storage.message_path(tid)
    raw = path.read_bytes()
    if mutation == "truncate":
        path.write_bytes(raw[:raw.index(b"\n") + 1])
    elif mutation == "rewrite_middle":
        middle = len(raw) // 2
        assert raw[middle:middle + 1] == b"A"
        path.write_bytes(raw[:middle] + b"B" + raw[middle + 1:])
    else:
        replacement = path.with_suffix(".replacement")
        replacement.write_bytes(raw)
        replacement.replace(path)
    with pytest.raises(HostAPIError) as error:
        case.call("messages.list", {"thread_id": tid, "limit": 1, "cursor": page["next_cursor"]})
    assert (error.value.code, error.value.status) == ("CURSOR_EXPIRED", 409)


# LLM: 坏行即使位于本页之后也不能给出完整快照；不允许跳过再声称complete。
# 函数用途: 验证JSON损坏、错误身份和未完成行均明确失败。
@pytest.mark.parametrize("bad", [b"not json\n", b"{", b'{"role":"user"}\n', b'{}\n', b'{"x":1,"x":2}\n'])
def test_history_corrupt_jsonl_is_not_empty_success(case, bad):
    tid = case.create()["thread_id"]
    append_message(case, tid, "user", "有效第一行")
    with case.stores["alice"].storage.message_path(tid).open("ab") as handle:
        handle.write(bad)
    with pytest.raises(HostAPIError) as error:
        result = case.call("messages.list", {"thread_id": tid, "limit": 1})
        assert result.data is not None
    assert (error.value.code, error.value.status) == ("CORRUPT_HISTORY", 500)


# LLM: 历史不能通过链接导出同用户控制文件或另一owner记录。
# 函数用途: 验证消息文件符号链接和硬链接都拒绝。
@pytest.mark.parametrize("link", ["symlink", "hardlink", "hardlink_denied"])
def test_history_rejects_linked_files(case, tmp_path, link, monkeypatch):
    tid = case.create()["thread_id"]
    outside = tmp_path / "secret.jsonl"
    outside.write_text("secret", encoding="utf-8")
    path = case.stores["alice"].storage.message_path(tid)
    if link == "symlink":
        path.symlink_to(outside)
    else:
        try:
            if link == "hardlink_denied":
                raise PermissionError("forced link(2) denial")
            path.hardlink_to(outside)
        except PermissionError:
            import os
            path.write_bytes(outside.read_bytes())
            inode = path.stat().st_ino
            original = os.fstat

            # LLM: 仅模拟被测inode的nlink，其余FD照真实结果；仍实际运行产品nofollow拒绝门。
            # 函数用途: 在内核禁止link时保留硬链接攻击语义。
            def linked_stat(fd):
                info = original(fd)
                if info.st_ino != inode:
                    return info
                fields = list(info)
                fields[3] = 2
                return os.stat_result(fields)

            monkeypatch.setattr(os, "fstat", linked_stat)
    with pytest.raises(HostAPIError) as error:
        result = case.call("messages.list", {"thread_id": tid})
        assert result.data is not None
    assert error.value.status in {404, 500}


# LLM: public工具只返回宿主白名单状态，不传原工具参数/输出；普通可见正文沿现有秘密打码器。
# 函数用途: 验证直接tool行和正文凭据都经过公开投影。
def test_history_direct_tool_outcome_and_credentials(case):
    tid = case.create()["thread_id"]
    append_message(case, tid, "tool", json.dumps({"tool": "web_fetch", "ok": False, "status": "failed",
                                                "api_key": "PRIVATE_KEY_VALUE", "output": "SECRET_RAW_OUTPUT"}))
    append_message(case, tid, "assistant", "api_key=PRIVATE_KEY_VALUE")
    data = thaw(case.call("messages.list", {"thread_id": tid}).data)
    assert data is not None
    assert len(data["items"]) == 2 and json.loads(data["items"][0]["content"])["ok"] is False
    assert "PRIVATE_KEY_VALUE" not in canonical_json(data) and "SECRET_RAW_OUTPUT" not in canonical_json(data)


# LLM: 原宿主写线程后，插件旧修订不能覆盖它；CAS不是只在B1各实例间成立。
# 函数用途: 验证跨入口改名推进公共修订。
def test_host_thread_update_invalidates_old_plugin_revision(case):
    original = case.create()
    tid = original["thread_id"]
    case.stores["alice"].threads.update_atomic(tid, lambda row: replace(row, title="宿主的新标题"))
    current = case.call("threads.get", {"thread_id": tid}).data
    assert current["revision"] == original["revision"] + 1
    with pytest.raises(HostAPIError) as error:
        case.call("threads.rename", {"thread_id": tid, "title": "陈旧覆盖", "expected_revision": original["revision"]},
                  key="host_stale_cas_01")
    assert error.value.code == "REVISION_CONFLICT"


# LLM: 通用metadata更新不能抹掉安装归属和已落盘幂等回执，否则同键会再次创建。
# 函数用途: 验证原Store更新合并保留B1的持久控制字段。
def test_host_metadata_replacement_preserves_plugin_receipts(case):
    original = case.create()
    case.stores["alice"].threads.update_atomic(original["thread_id"], lambda row: replace(row, metadata={"host_note": "ok"}))
    assert case.create() == original
    assert len(case.stores["alice"].threads.list()) == 1


# LLM: 初次插件编辑旧canonical线程也不能无条件使用固定revision=1；原Store变化必须使旧CAS失效。
# 函数用途: 验证显式授予旧线程时的初次公共修订。
def test_legacy_explicit_thread_revision_tracks_host_changes(case):
    from agent_py_agent.agent.conversation.models import ConversationThread
    from agent_py_agent.agent.conversation.store_threads import create_thread_atomic

    home = case.homes["alice"]
    row = ConversationThread(thread_id="thread-legacy", canonical_user_id=home.owner_id,
                             owner_id=home.owner_id, owner_home=str(home.home_dir), title="旧线程")
    create_thread_atomic(case.stores["alice"].threads, row)
    old = case.call("threads.get", {"thread_id": row.thread_id}, explicit=(row.thread_id,)).data
    case.stores["alice"].threads.update_atomic(row.thread_id, lambda current: replace(current, title="宿主已改"))
    with pytest.raises(HostAPIError) as error:
        case.call("threads.rename", {"thread_id": row.thread_id, "title": "旧CAS", "expected_revision": old["revision"]},
                  key="legacy_cas_key_01", explicit=(row.thread_id,))
    assert error.value.code == "REVISION_CONFLICT"


# LLM: 参数非法不能变成静默裁剪或路径选择；空历史只针对真实授权线程。
# 函数用途: 验证读写参数边界和无消息会话。
def test_schema_limits_and_empty_history(case):
    tid = case.create()["thread_id"]
    empty = case.call("messages.list", {"thread_id": tid}).data
    assert empty["complete"] and not empty["items"] and not empty["load_errors"]
    for payload in ({"limit": True}, {"limit": 0}, {"limit": 101}, {"max_content_bytes": 0},
                    {"max_content_bytes": 65537}, {"unknown": 1}):
        with pytest.raises(HostAPIError) as error:
            case.call("messages.list", {"thread_id": tid, **payload})
        assert error.value.code == "INVALID_SCHEMA"
    for payload in ({"title": "x" * 121}, {"workspace_ref": "/tmp"}, {"workspace_ref": "../secret"}):
        with pytest.raises(HostAPIError) as error:
            case.call("threads.create", payload, key="invalid_create_01")
        assert error.value.code == "INVALID_SCHEMA"


# LLM: 整页JSON预算独立于原文UTF8预算，转义膨胀不能超限；后续页仍保留有序连续记录。
# 函数用途: 验证大页、极端转义和默认截断。
def test_history_page_byte_budget_and_default_limit(case):
    from agent_py_agent.agent.plugin_host.schemas import PAGE_MAX_BYTES

    tid = case.create()["thread_id"]
    for _ in range(5):
        append_message(case, tid, "user", "\x01" * 65536)
    page = thaw(case.call("messages.list", {"thread_id": tid, "limit": 100, "max_content_bytes": 65536}).data)
    assert len(canonical_json(page).encode()) <= PAGE_MAX_BYTES
    assert page["items"] and page["next_cursor"] and not page["complete"]
    assert page["items"][0]["content_truncated"] and page["items"][0]["content_bytes"] == 65536
    default = case.call("messages.list", {"thread_id": tid}).data["items"][0]
    assert len(default["content"].encode()) == 16384 and default["content_truncated"]


# LLM: 不同线程/安装、坏MAC和不同过滤不能复用旧游标；撤权在返回前仍有效。
# 函数用途: 验证历史游标的作用域与读后撤销。
def test_history_cursor_scope_and_late_revocation(case, monkeypatch):
    from agent_py_agent.agent.plugin_host import history

    tid, other = case.create()["thread_id"], case.create("第二", key="create_other_001")["thread_id"]
    for content in ("第一条", "第二条"):
        append_message(case, tid, "user", content)
    token = case.call("messages.list", {"thread_id": tid, "limit": 1}).data["next_cursor"]
    for payload, options in (({"thread_id": other}, {}), ({"thread_id": tid, "max_content_bytes": 1}, {}),
                              ({"thread_id": tid}, {"installation": "other", "explicit": (tid,)}),
                              ({"thread_id": tid, "cursor": token[:-1] + ("0" if token[-1] != "0" else "1")}, {})):
        with pytest.raises(HostAPIError) as error:
            case.call("messages.list", {"thread_id": tid, "limit": 1, "cursor": token, **payload}, **options)
        assert error.value.code == "CURSOR_EXPIRED"
    original = history._verify_current

    # LLM: 用真实读取完成后撤权测试返回fence，不改写公开结果。
    # 函数用途: 模拟读后令牌失效。
    def revoke(*args):
        original(*args)
        case.revoked = True

    monkeypatch.setattr(history, "_verify_current", revoke)
    with pytest.raises(HostAPIError) as error:
        case.call("messages.list", {"thread_id": tid})
    assert error.value.status == 401


# LLM: 新激活必须重新取得有效授权；同安装的持久回执不能按短期游标生命周期过期。
# 函数用途: 验证创建和改名在安装重新激活后仍返回原始结果。
@pytest.mark.parametrize("action", ["create", "rename"])
def test_idempotency_survives_new_activation(case, action):
    thread = case.create()
    payload = {"title": "首个线程"} if action == "create" else {
        "thread_id": thread["thread_id"], "title": "重新命名", "expected_revision": 1}
    key = "create_thread_001" if action == "create" else "activation_rename_01"
    first = case.call("threads." + action, payload, key=key)
    replay = case.call("threads." + action, payload, key=key, activation="c" * 64)
    assert (replay.http_status, replay.data) == (first.http_status, first.data)
    case.revoked = True
    with pytest.raises(HostAPIError) as error:
        case.call("threads." + action, payload, key=key, activation="c" * 64)
    assert error.value.status == 401


# LLM: 损坏只注入pytest的canonical夹具，不通过已加保护的宿主更新入口规避验证。
# 函数用途: 为异常持久数据测试替换插件内部字段。
def damage_plugin_state(case, tid, state):
    path = case.stores["alice"].storage.thread_path(tid)
    row = json.loads(path.read_text(encoding="utf-8"))
    row["metadata"]["plugin_host_v2"] = state
    path.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")


# LLM: 入口曾校验输入不等于读取可信，持久化绝对路径也不能进入公开摘要。
# 函数用途: 验证get和list对坏workspace引用明确失败。
@pytest.mark.parametrize("action", ["get", "list"])
def test_thread_summary_rejects_stored_absolute_workspace(case, action):
    tid = case.create()["thread_id"]
    state = case.stores["alice"].threads.require(tid).metadata["plugin_host_v2"]
    damage_plugin_state(case, tid, {**state, "workspace_ref": "/Users/example/private"})
    with pytest.raises(HostAPIError) as error:
        case.call("threads." + action, {"thread_id": tid} if action == "get" else {})
    assert (error.value.code, error.value.status) == ("CORRUPT_THREAD", 500)
    assert "/Users/example/private" not in str(error.value)


# LLM: 线程损坏不能绕过404隔离；只有显式有权访问的调用方才能收到损坏诊断。
# 函数用途: 验证越scope的坏状态与坏JSON不会暴露为500。
@pytest.mark.parametrize("damage", ["state", "json"])
@pytest.mark.parametrize("action", ["threads.get", "messages.list", "threads.rename"])
def test_out_of_scope_corruption_stays_not_found(case, damage, action):
    tid = case.create()["thread_id"]
    if damage == "state":
        damage_plugin_state(case, tid, [])
    else:
        case.stores["alice"].storage.thread_path(tid).write_text("broken json", encoding="utf-8")
    payload = {"thread_id": tid}
    if action == "threads.rename":
        payload.update(title="新名", expected_revision=1)
    with pytest.raises(HostAPIError) as error:
        case.call(action, payload, installation="install-two", key="foreign_thread_001")
    assert (error.value.code, error.value.status) == ("RESOURCE_NOT_FOUND", 404)
    with pytest.raises(HostAPIError) as allowed:
        case.call("threads.get", {"thread_id": tid}, explicit=(tid,))
    assert (allowed.value.code, allowed.value.status) == ("CORRUPT_THREAD", 500)


# LLM: 旧快照在解析前失效不能变成首读损坏或大记录错误；缩小单行阈值仅让边界夹具更小。
# 函数用途: 验证路径变链接与原位改成超长记录均使旧游标过期。
@pytest.mark.parametrize("damage", ["symlink", "oversized_prefix"])
def test_history_cursor_damage_is_expired(case, monkeypatch, damage):
    from agent_py_agent.agent.plugin_host import history

    monkeypatch.setattr(history, "HISTORY_RECORD_MAX_BYTES", 2048)
    tid = case.create()["thread_id"]
    for _ in range(2):
        append_message(case, tid, "user", "A" * 1500)
    page = case.call("messages.list", {"thread_id": tid, "limit": 1}).data
    path = case.stores["alice"].storage.message_path(tid)
    original = path.with_suffix(".original")
    if damage == "symlink":
        path.rename(original)
        path.symlink_to(original)
    else:
        path.write_bytes(b"X" * path.stat().st_size)
    with pytest.raises(HostAPIError) as error:
        case.call("messages.list", {"thread_id": tid, "limit": 1, "cursor": page["next_cursor"]})
    assert (error.value.code, error.value.status) == ("CURSOR_EXPIRED", 409)


# LLM: 首次读取真正过大的原记录仍须413，不能因续页修复而把所有错误统一409。
# 函数用途: 保留首次读取的大小上限语义。
def test_first_history_oversized_record_keeps_413(case, monkeypatch):
    from agent_py_agent.agent.plugin_host import history

    monkeypatch.setattr(history, "HISTORY_RECORD_MAX_BYTES", 2048)
    tid = case.create()["thread_id"]
    append_message(case, tid, "user", "A" * 3000)
    with pytest.raises(HostAPIError) as error:
        case.call("messages.list", {"thread_id": tid})
    assert (error.value.code, error.value.status) == ("PAYLOAD_TOO_LARGE", 413)


# LLM: 用真实Goal/Claim写端和原文件锁验证检查至提交的临界区，不能以重复读忙碌状态代替串行化。
# 函数用途: 验证归档持锁期间活跃状态写入不会抢在归档提交之前落盘。
@pytest.mark.parametrize("kind", ["goal", "claim", "detached_claim"])
def test_archive_serializes_activity_writers(case, monkeypatch, kind):
    from agent_py_agent.agent.conversation import store_claims, store_goals
    from agent_py_agent.agent.conversation.run_claim import detached_task_claim_scope_id
    from agent_py_agent.agent.gateway_parts.io import try_locked_file_transition

    tid = case.create()["thread_id"]
    store = case.stores["alice"]
    module = store_goals if kind == "goal" else store_claims
    scope = tid
    if kind == "detached_claim":
        store.tasks.bind({"thread_id": tid, "task_id": "task-race", "status": "completed", "cancellation_scope": "detached"})
        assert not store.threads.require(tid).active_task_ids
        scope = detached_task_claim_scope_id(tid, "task-race")
    path = store.storage.goal_path(tid) if kind == "goal" else store.storage.background_claim_path(scope)
    checked, release, attempted = Event(), Event(), Event()
    original_check, original_write = case.domain.check_archive, module.update_json_file_atomic
    observations = []

    # LLM: 仅暂停已完成检查的归档，finally始终释放，避免断言失败遗留挂起线程。
    # 函数用途: 建立检查与提交之间的可控交错点。
    def paused_check(*args):
        original_check(*args)
        checked.set()
        assert release.wait(5)

    # LLM: 保持真实写端和原锁，只在锁内updater记录线程状态，不替代存储实现。
    # 函数用途: 观察Goal或Claim真正取得提交权的时刻。
    def observe_write(target, updater, **kwargs):
        attempted.set()

        # LLM: 由原JSON锁内调用，不自行提交。
        # 函数用途: 记录写入活动状态前已提交的线程状态。
        def wrapped(data):
            observations.append(store.threads.require(tid).status)
            return updater(data)

        return original_write(target, wrapped, **kwargs)

    monkeypatch.setattr(case.domain, "check_archive", paused_check)
    monkeypatch.setattr(module, "update_json_file_atomic", observe_write)
    with ThreadPoolExecutor(max_workers=2) as pool:
        archive = pool.submit(case.call, "threads.archive", {"thread_id": tid, "expected_revision": 1}, key="archive_race_001")
        try:
            assert checked.wait(5)
            with try_locked_file_transition(path) as acquired:
                lock_held = not acquired
            writer = pool.submit(store.goals.create if kind == "goal" else store.claims.acquire,
                                 {"thread_id": tid, "objective": "并发目标", "task_id": "task-race", "claim_scope_id": scope})
            assert attempted.wait(5)
        finally:
            release.set()
        assert archive.result().data["status"] == "archived"
        assert writer.result() is not None
    assert lock_held, "归档检查到提交期间未持有活动状态的真实写锁"
    assert observations == ["archived"]


# LLM: 已登记但缺revision是损坏状态，不可在API和Store分别选择摘要/1再生成不一致回执；必须无副作用拒绝。
# 函数用途: 验证缺失插件修订时读取、插件改名及原宿主写入都明确失败。
@pytest.mark.parametrize("entry", ["get", "rename", "store"])
def test_incomplete_plugin_revision_fails_closed(case, entry):
    from agent_py_agent.agent.conversation.store_threads import DataCorruptionError

    tid = case.create()["thread_id"]
    store = case.stores["alice"]
    state = dict(store.threads.require(tid).metadata["plugin_host_v2"])
    del state["revision"]
    damage_plugin_state(case, tid, state)
    path = store.storage.thread_path(tid)
    before = path.read_bytes()
    if entry == "store":
        with pytest.raises(DataCorruptionError):
            store.threads.update_atomic(tid, lambda row: replace(row, title="宿主改名"))
    else:
        payload = {"thread_id": tid}
        if entry == "rename":
            payload.update(title="插件改名", expected_revision=1)
        with pytest.raises(HostAPIError) as error:
            case.call("threads." + entry, payload, key="missing_revision_01")
        assert (error.value.code, error.value.status) == ("CORRUPT_THREAD", 500)
    assert path.read_bytes() == before


# LLM: TaskLink结案与执行claim结算不是同一时刻；活动索引为空不能代替独立车道的真实运行状态。
# 函数用途: 验证已结案detached任务仍持运行claim时拒绝归档。
def test_archive_rejects_unsettled_detached_claim(case):
    from agent_py_agent.agent.conversation.run_claim import detached_task_claim_scope_id

    tid = case.create()["thread_id"]
    store = case.stores["alice"]
    store.tasks.bind({"thread_id": tid, "task_id": "task-detached", "status": "completed", "cancellation_scope": "detached"})
    assert not store.threads.require(tid).active_task_ids
    scope = detached_task_claim_scope_id(tid, "task-detached")
    assert store.claims.acquire({"thread_id": tid, "task_id": "task-detached", "claim_scope_id": scope}) is not None
    revision = case.call("threads.get", {"thread_id": tid}).data["revision"]
    with pytest.raises(HostAPIError) as error:
        case.call("threads.archive", {"thread_id": tid, "expected_revision": revision}, key="archive_detached_01")
    assert (error.value.code, error.value.status) == ("THREAD_BUSY", 409)
    assert store.threads.require(tid).status == "active"
