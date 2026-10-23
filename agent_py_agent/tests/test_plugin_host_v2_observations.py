# LLM: D2领域测试使用真实JSONL/注册账/文件IO，仅替代尚未交付的A授权上下文接缝；不冒充HTTP或真实令牌验收。
# 模块用途: 锁住正式记忆和制品的只读、隔离、分页、完整性及路径安全合同。
from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from agent_py_agent.agent.artifacts.registry import (
    ArtifactRegistration,
    register_artifact,
    registry_path,
)
from agent_py_agent.agent.plugin_host import contracts
from agent_py_agent.agent.plugin_host.artifacts import (
    ArtifactReadService,
    ArtifactTaskScope,
    validate_artifact_read,
)
from agent_py_agent.agent.plugin_host.contracts import (
    AuthorizedCall,
    DomainResult,
    OwnerReadContext,
    PluginIngressSource,
    VerifiedPluginBinding,
    decode_call,
)
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.plugin_host.memory import MemoryReadService
from agent_py_agent.agent.plugin_host.schemas import thaw
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home


# LLM: 测试边界替身只认登记过的对象；真实授权实现仍由A负责，不能从此fixture推断授权验收。
# 类用途: 准备两个真实临时owner和可撤销的测试上下文。
class Harness:
    # LLM: 所有文件准备发生在读请求之前；缺目录测试可使用纯resolver结果。
    # 函数用途: 初始化owner、授权材料和领域服务。
    def __init__(self, root, monkeypatch):
        self.alice = resolve_owner_home(root, OwnerIdentity.provider_user("local", "alice"))
        self.bob = resolve_owner_home(root, OwnerIdentity.provider_user("local", "bob"))
        self.calls = {}
        self.resolutions = 0
        self.revoked = False
        self.scopes = ()
        self.grant = {"installation_ref": "install-one", "package_sha256": "a" * 64,
                      "activation_id": "b" * 64, "grant_revision": 1,
                      "tasks": {"explicit_ids": ["task-one"], "created_by_installation": False}}
        self.memory = MemoryReadService()
        self.artifacts = ArtifactReadService(self.task_scopes)
        monkeypatch.setattr(contracts, "resolve_read_context", self.resolve)

    # LLM: 这里只替代A的签发/末核，不替代D2内部文件、分页和安全逻辑。
    # 函数用途: 返回登记调用对应的只读owner上下文。
    def resolve(self, authorized):
        self.resolutions += 1
        if self.revoked or id(authorized) not in self.calls:
            raise HostAPIError("CAPABILITY_DENIED", 403)
        home = self.calls[id(authorized)]
        return OwnerReadContext(home.identity, home, authorized.binding)

    # LLM: 宿主组合根接缝只返回已准备的typed任务范围，不解析请求路径。
    # 函数用途: 提供fixture中的任务根清单。
    def task_scopes(self, authorized, context):
        return self.scopes

    # LLM: 参数走真实A0闭合decoder；此登记不是生产令牌工厂。
    # 函数用途: 构造并登记一个领域单元测试调用。
    def authorize(self, action, payload, *, home=None, caps=None):
        home = home or self.alice
        binding = VerifiedPluginBinding(self.grant, object(), home.identity,
                                        PluginIngressSource(object(), object()),
                                        frozenset(caps if caps is not None else {"memory.read", "artifacts.read"}))
        call = decode_call(json.dumps({"schema": "plugin_host_call.v2", "action": action,
                                      "payload": payload}).encode())
        authorized = AuthorizedCall(call, binding, home.identity, {}, None)
        self.calls[id(authorized)] = home
        return authorized


# LLM: 每例独立临时目录，未调用ensure_owner_home/Agent。
# 函数用途: 返回测试环境。
@pytest.fixture
def env(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch)


# LLM: 仅为测试准备canonical事件，不经过索引或外部服务。
# 函数用途: 构造一条正式记忆版本记录。
def memory_row(entry_id, content="中文事实", kind="fact", **changes):
    return {"entry_id": entry_id, "role": "user", "kind": kind, "content": content,
            "version": 1, "created_at": 1.0, "attributes": {"scope_type": "personal", "scope_key": "personal"},
            **changes}


# LLM: 写入仅是fixture准备；被测读取期间另外禁止写系统调用。
# 函数用途: 保存真实正式JSONL。
def put_memories(home, rows):
    home.long_term_dir.mkdir(parents=True, exist_ok=True)
    home.long_term_jsonl.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))


# LLM: 生产结果必须满足DTO和HTTP合同，不以导入成功代替行为验证。
# 函数用途: 执行一个动作并读回非空结构化结果。
def result(service, authorized):
    response = service.call(authorized)
    assert isinstance(response, DomainResult)
    assert response.http_status == 200
    data = thaw(response.data)
    assert data, "领域尚未实现"
    return data


# LLM: 使用原注册入口，不伪造第二套制品账。
# 函数用途: 在授权任务根准备一个文件并登记。
def put_artifact(env, data=b"abcdef", name="output/report.bin", ref="art-one"):
    root = env.alice.tasks_dir / "2026-10-23" / "sample"
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    registered = register_artifact(ArtifactRegistration(root, path, artifact_id=ref,
                                                       task_id="task-one", mime_type="application/octet-stream"))
    env.scopes = (ArtifactTaskScope(env.alice.identity, "task-one", root, (root,)),)
    return path, registered


# LLM: 覆盖派单指定红灯，不得偷偷保留path后门。
# 函数用途: 验证原始路径参数被明确拒绝。
def test_artifact_reader_has_no_raw_path_escape_hatch():
    with pytest.raises(HostAPIError) as caught:
        validate_artifact_read({"artifact_ref": "artifact-example", "expected_sha256": "a" * 64,
                                "path": "/another-owner/private"})
    assert (caught.value.code, caught.value.status) == ("INVALID_SCHEMA", 400)


# LLM: identity和数值边界均来自冻结A0合同，不能静默裁剪。
# 函数用途: 拒绝身份注入和非法读取范围。
@pytest.mark.parametrize("extra", [{"owner_id": "bob"}, {"__source": {}}, {"offset_bytes": True},
                                  {"offset_bytes": -1}, {"max_bytes": 0}, {"max_bytes": 65537},
                                  {"expected_sha256": "z" * 64}])
def test_artifact_read_closed_schema(extra):
    with pytest.raises(HostAPIError) as caught:
        validate_artifact_read({"artifact_ref": "art-one", "expected_sha256": "a" * 64, **extra})
    assert caught.value.code == "INVALID_SCHEMA"


# LLM: 搜索只认formal活动版本及允许kind，不读候选、lesson或其它owner。
# 函数用途: 检查词法匹配、白名单投影和版本物化。
def test_memory_search_uses_formal_owner_records_only(env):
    rows = [memory_row("m1"), memory_row("m2", kind="event"), memory_row("m3", kind="project"),
            memory_row("lesson", kind="lesson"), memory_row("removed"),
            memory_row("removed", action="remove", version=2), memory_row("expired", expires_at=1),
            memory_row("old", content="旧中文"), memory_row("old", content="新内容", version=2, action="replace")]
    put_memories(env.alice, rows)
    put_memories(env.bob, [memory_row("bob")])
    env.alice.memory_candidates_jsonl.write_text(json.dumps(memory_row("pending")))
    page = result(env.memory, env.authorize("memory.search", {"query": "中文"}))
    assert {row["entry_id"] for row in page["items"]} == {"m1", "m2", "m3"}
    assert page["query_evidence"] == {"query": "中文", "scope": "effective_owner", "engine": "lexical", "complete": True}
    assert page["complete"] and page["next_cursor"] is None and page["snapshot_id"]
    assert all(set(row) == {"entry_id", "kind", "content", "content_complete", "revision", "source_ref"}
               for row in page["items"])


# LLM: UTF8片段须在字符边界截断，不能把2048字符冒充2048字节。
# 函数用途: 检查中文及emoji搜索摘要的字节上限。
def test_memory_search_snippet_is_utf8_bounded(env):
    text = "鲸🐳" * 1000
    put_memories(env.alice, [memory_row("m1", text)])
    row = result(env.memory, env.authorize("memory.search", {"query": "鲸"}))["items"][0]
    assert len(row["content"].encode()) <= 2048
    assert text.startswith(row["content"]) and row["content_complete"] is False


# LLM: get分页使用公开正文偏移，拼接不得丢字、重复或引入replacement字符。
# 函数用途: 逐页恢复超过两片的中文正文。
@pytest.mark.parametrize("text", ["鲸🐳\n" * 20000, "\x00" * 160000], ids=["unicode", "json_escaped"])
def test_memory_get_reassembles_large_utf8_entry(env, text):
    put_memories(env.alice, [memory_row("large", text)])
    parts, payload = [], {"entry_id": "large"}
    for _ in range(10):
        page = result(env.memory, env.authorize("memory.get", payload))
        assert len(page["text"].encode()) <= 65536 and page["revision"] == 1
        parts.append(page["text"])
        if page["complete"]:
            assert page["next_cursor"] is None
            break
        payload = {"entry_id": "large", "cursor": page["next_cursor"]}
    assert len(parts) > 1 and "".join(parts) == text


# LLM: 改版游标必须409；不能混合新旧版本正文。
# 函数用途: 检查翻页期间的版本fence。
def test_memory_revision_change_expires_cursor(env):
    row = memory_row("large", "中" * 30000)
    put_memories(env.alice, [row])
    first = result(env.memory, env.authorize("memory.get", {"entry_id": "large"}))
    put_memories(env.alice, [row, {**row, "version": 2, "action": "replace", "content": "新" * 30000}])
    with pytest.raises(HostAPIError) as caught:
        env.memory.call(env.authorize("memory.get", {"entry_id": "large", "cursor": first["next_cursor"]}))
    assert (caught.value.code, caught.value.status) == ("CURSOR_EXPIRED", 409)


# LLM: 不存在、其它owner、候选及lesson统一404，不能枚举私有记录。
# 函数用途: 检查get的正式记录边界。
@pytest.mark.parametrize("entry_id", ["missing", "pending", "lesson", "bob"])
def test_memory_get_hides_nonformal_or_foreign_records(env, entry_id):
    put_memories(env.alice, [memory_row("lesson", kind="lesson")])
    put_memories(env.bob, [memory_row("bob")])
    env.alice.memory_candidates_jsonl.write_text(json.dumps(memory_row("pending")))
    with pytest.raises(HostAPIError) as caught:
        env.memory.call(env.authorize("memory.get", {"entry_id": entry_id}))
    assert (caught.value.code, caught.value.status) == ("RESOURCE_NOT_FOUND", 404)


# LLM: MAC必须绑定目标、过滤条件、安装和激活，不能仅base64编码偏移。
# 函数用途: 检查搜索游标的稳定页与跨条件拒绝。
def test_memory_search_cursor_is_bound_and_snapshot_fenced(env):
    rows = [memory_row(f"m{i}") for i in range(3)]
    put_memories(env.alice, rows)
    payload = {"query": "中文", "limit": 1}
    first = result(env.memory, env.authorize("memory.search", payload))
    assert first["complete"] is False and first["next_cursor"]
    later = {**payload, "cursor": first["next_cursor"]}
    second = result(env.memory, env.authorize("memory.search", later))
    assert second == result(env.memory, env.authorize("memory.search", later))
    assert second["items"][0]["entry_id"] != first["items"][0]["entry_id"]
    for invalid in ({**later, "query": "事实"}, {**later, "cursor": "forged"}):
        with pytest.raises(HostAPIError):
            env.memory.call(env.authorize("memory.search", invalid))
    env.grant = {**env.grant, "activation_id": "c" * 64}
    with pytest.raises(HostAPIError):
        env.memory.call(env.authorize("memory.search", later))
    env.grant = {**env.grant, "activation_id": "b" * 64}
    put_memories(env.alice, [*rows, memory_row("m4")])
    with pytest.raises(HostAPIError) as caught:
        env.memory.call(env.authorize("memory.search", later))
    assert caught.value.code == "CURSOR_EXPIRED"


# LLM: scope必须沿现有正式规则；session/company不能因词命中扩大，原始source路径不能回包。
# 函数用途: 检查scope、未确认标记和来源投影。
def test_memory_scope_and_source_projection(env):
    rows = [memory_row("personal", source="/private/secret"),
            memory_row("project", kind="project", attributes={"scope_type": "project", "scope_key": "project:demo"}),
            memory_row("session", attributes={"scope_type": "session", "scope_key": "session:other"}),
            memory_row("unconfirmed", attributes={"scope_type": "personal", "scope_key": "personal", "confirmed": False})]
    put_memories(env.alice, rows)
    page = result(env.memory, env.authorize("memory.search", {"query": "中文"}))
    assert {row["entry_id"] for row in page["items"]} == {"personal", "project"}
    assert all(row["source_ref"] is None for row in page["items"])


# LLM: list/get只显示闭合元数据，read按注册hash回读真实字节。
# 函数用途: 检查三动作与分片base64拼接。
def test_artifact_list_get_and_chunked_read(env):
    content = "报告🐳".encode() * 10000
    _, record = put_artifact(env, content)
    page = result(env.artifacts, env.authorize("workspace.artifacts.list", {"task_id": "task-one"}))
    meta = result(env.artifacts, env.authorize("workspace.artifacts.get", {"artifact_ref": record.artifact_id}))
    assert page["items"] == [meta] and page["complete"]
    assert set(meta) == {"artifact_ref", "task_id", "name", "media_type", "byte_size", "sha256", "created_at", "readable"}
    assert meta["byte_size"] == len(content) and meta["readable"] is True
    offset, chunks = 0, []
    while offset < len(content):
        data = result(env.artifacts, env.authorize("workspace.artifacts.read", {
            "artifact_ref": record.artifact_id, "expected_sha256": record.sha256, "offset_bytes": offset, "max_bytes": 50001}))
        chunk = base64.b64decode(data["data_base64"], validate=True)
        assert len(chunk) <= 50001 and data["offset_bytes"] == offset
        assert data["next_offset_bytes"] == offset + len(chunk)
        assert data["total_bytes"] == len(content) and data["sha256"] == hashlib.sha256(content).hexdigest()
        chunks.append(chunk)
        offset = data["next_offset_bytes"]
    assert data["eof"] is True and b"".join(chunks) == content


# LLM: 修改后或期待哈希不一致均不能回旧元数据配新正文。
# 函数用途: 验证注册指纹与客户端固定hash。
@pytest.mark.parametrize("change", ["same_size", "new_size", "expected_hash"])
def test_artifact_changes_are_conflicts(env, change):
    path, record = put_artifact(env)
    if change == "same_size":
        path.write_bytes(b"xxxxxx")
    if change == "new_size":
        path.write_bytes(b"longer file")
    with pytest.raises(HostAPIError) as caught:
        env.artifacts.call(env.authorize("workspace.artifacts.read", {
            "artifact_ref": record.artifact_id, "expected_sha256": "a" * 64 if change == "expected_hash" else record.sha256}))
    assert (caught.value.code, caught.value.status) == ("ARTIFACT_CHANGED", 409)


# LLM: symlink/hardlink在实际open处拒绝；中间目录和任务根链接同样不可用。
# 函数用途: 检查文件系统链接逃逸与文件类型。
@pytest.mark.parametrize("attack", ["symlink", "hardlink", "parent_symlink", "root_symlink", "fifo"])
def test_artifact_reader_rejects_unsafe_filesystem_nodes(env, attack, tmp_path):
    path, record = put_artifact(env)
    outside = tmp_path / "outside"
    outside.write_bytes(b"abcdef")
    if attack in {"symlink", "hardlink", "fifo"}:
        path.unlink()
        if attack == "symlink":
            path.symlink_to(outside)
        elif attack == "hardlink":
            source = path.with_name("link-source.bin")
            source.write_bytes(b"abcdef")
            os.link(source, path)
        else:
            os.mkfifo(path)
    else:
        parent = path.parent if attack == "parent_symlink" else env.scopes[0].workspace_root
        moved = parent.with_name(parent.name + "-moved")
        parent.rename(moved)
        parent.symlink_to(moved, target_is_directory=True)
    with pytest.raises(HostAPIError) as caught:
        env.artifacts.call(env.authorize("workspace.artifacts.read", {
            "artifact_ref": record.artifact_id, "expected_sha256": record.sha256}))
    assert caught.value.status in {404, 503}


# LLM: 被误登记的宿主控制/秘密文件也不能成为可下载业务制品。
# 函数用途: 检查控制文件拒绝策略。
@pytest.mark.parametrize("name", ["data/private_interactions/card.json", "data/artifacts/secret.json",
                                  "work/state.json", "config/capability_config.yaml", ".env", "permissions.json"])
def test_artifact_reader_rejects_misregistered_control_files(env, name):
    _, record = put_artifact(env, name=name)
    with pytest.raises(HostAPIError) as caught:
        env.artifacts.call(env.authorize("workspace.artifacts.read", {
            "artifact_ref": record.artifact_id, "expected_sha256": record.sha256}))
    assert caught.value.code == "RESOURCE_NOT_FOUND"


# LLM: 没有注册ref、其它owner和无task授权统一404，不透露存在性。
# 函数用途: 检查owner和任务范围不能被resolver输出偷偷扩大。
@pytest.mark.parametrize("attack", ["unregistered", "owner", "task", "root"])
def test_artifact_scope_is_verified(env, attack):
    path, record = put_artifact(env)
    ref = record.artifact_id
    scope = env.scopes[0]
    if attack == "unregistered":
        ref = "art-unregistered"
    elif attack == "owner":
        env.scopes = (replace(scope, owner=env.bob.identity),)
    elif attack == "task":
        env.grant = {**env.grant, "tasks": {"explicit_ids": [], "created_by_installation": False}}
    else:
        env.scopes = (replace(scope, workspace_root=env.bob.tasks_dir),)
    with pytest.raises(HostAPIError) as caught:
        env.artifacts.call(env.authorize("workspace.artifacts.get", {"artifact_ref": ref}))
    assert (caught.value.code, caught.value.status) == ("RESOURCE_NOT_FOUND", 404)


# LLM: 损坏行显式标不完整并脱敏；正常注册项仍可被列出，不伪报全量成功。
# 函数用途: 核对注册账诊断保留。
def test_corrupt_artifact_registry_is_reported(env):
    _, record = put_artifact(env)
    path = registry_path(env.scopes[0].workspace_root)
    with path.open("a") as stream:
        stream.write("{bad json\n")
    response = env.artifacts.call(env.authorize("workspace.artifacts.list", {"task_id": "task-one"}))
    data = thaw(response.data)
    assert data.get("complete") is False
    assert data["items"][0]["artifact_ref"] == record.artifact_id
    assert data["load_errors"] and response.warnings
    assert str(env.alice.home_dir) not in json.dumps(data)


# LLM: 调用边界必须检查能力并经A末核；不能以“已收DTO”绕过撤销。
# 函数用途: 验证调用前拒绝缺权和伪造对象。
def test_domain_rejects_missing_capability_and_forged_call(env):
    put_memories(env.alice, [memory_row("m1")])
    with pytest.raises(HostAPIError) as caught:
        env.memory.call(env.authorize("memory.search", {"query": "中文"}, caps=[]))
    assert caught.value.status == 403
    genuine = env.authorize("memory.search", {"query": "中文"})
    with pytest.raises(HostAPIError):
        env.memory.call(replace(genuine))


# LLM: 文件快照只比较名称/正文/mtime/权限，不把读取导致的atime当业务写入。
# 函数用途: 保存读取前后可核对的文件状态。
def tree_snapshot(root):
    return {str(path.relative_to(root)): (path.stat().st_mtime_ns, path.stat().st_mode,
                                         path.read_bytes() if path.is_file() else None)
            for path in root.rglob("*")}


# LLM: 将所有常见写入口变成断言失败，防“写后恢复”逃过前后快照。
# 函数用途: 在真实读取调用期间禁止文件创建、删除、重命名和写模式open。
def forbid_writes(monkeypatch):
    import builtins
    import io
    original_open, original_io_open, original_os_open = builtins.open, io.open, os.open

    # LLM: 只阻止写模式，仍执行真实读取。
    # 函数用途: 检查Python文件open标志。
    def guarded_open(file, mode="r", *args, **kwargs):
        assert not any(flag in mode for flag in "wax+")
        return original_open(file, mode, *args, **kwargs)

    # LLM: Path.open使用io.open，需要独立覆盖。
    # 函数用途: 检查Path写标志。
    def guarded_io(file, mode="r", *args, **kwargs):
        assert not any(flag in mode for flag in "wax+")
        return original_io_open(file, mode, *args, **kwargs)

    # LLM: os.open允许O_RDONLY与只读安全标志，不允许建文件或截断。
    # 函数用途: 检查FD打开是否写入。
    def guarded_fd(path, flags, *args, **kwargs):
        assert not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        return original_os_open(path, flags, *args, **kwargs)

    # LLM: 对目录与元数据变更直接失败。
    # 函数用途: 拒绝测试读取期间的变更系统调用。
    def forbidden(*args, **kwargs):
        raise AssertionError("read attempted filesystem mutation")

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(io, "open", guarded_io)
    monkeypatch.setattr(os, "open", guarded_fd)
    replacements = {original_os_open: guarded_fd}
    for name in ("mkdir", "makedirs", "unlink", "remove", "rename", "replace", "rmdir", "chmod", "utime"):
        replacements[getattr(os, name)] = forbidden
        monkeypatch.setattr(os, name, forbidden)
    # supports_dir_fd按函数对象判断；仅同步已支持函数的包装身份，不改变平台能力或屏蔽写入断言。
    monkeypatch.setattr(os, "supports_dir_fd", {replacements.get(fn, fn) for fn in os.supports_dir_fd})


# LLM: 冷读与已有数据读都要无写入，禁止构造记忆索引服务。
# 函数用途: 验证五动作和冷owner均不改变文件系统。
def test_read_actions_never_mutate_filesystem_or_initialize_memory(env, tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store.jsonl import JsonlMemory
    put_memories(env.alice, [memory_row("m1")])
    _, record = put_artifact(env)
    before = tree_snapshot(tmp_path)

    # LLM: 任何构造旧记忆仓库的行为均违反冷读合同。
    # 函数用途: 拒绝索引初始化路径。
    def no_memory_init(*args, **kwargs):
        raise AssertionError("memory repository initialized on read")

    with monkeypatch.context() as patch:
        forbid_writes(patch)
        patch.setattr(JsonlMemory, "__init__", no_memory_init)
        for action, payload in [("memory.search", {"query": "中文"}), ("memory.get", {"entry_id": "m1"})]:
            result(env.memory, env.authorize(action, payload))
        cold = result(env.memory, env.authorize("memory.search", {"query": "中文"}, home=env.bob))
        assert cold["items"] == [] and cold["complete"]
        for action, payload in [("list", {"task_id": "task-one"}), ("get", {"artifact_ref": record.artifact_id}),
                                ("read", {"artifact_ref": record.artifact_id, "expected_sha256": record.sha256})]:
            result(env.artifacts, env.authorize("workspace.artifacts." + action, payload))
    assert tree_snapshot(tmp_path) == before and not env.bob.home_dir.exists()


# LLM: 不允许坏字段崩溃或从旧版本恢复正文；同样不把损坏正式账报告为已完整检索。
# 函数用途: 复核正式记忆坏字段的显式诊断。
@pytest.mark.parametrize("changes", [{"attributes": {"scope_type": "personal", "scope_key": "personal", "status": []}},
                                     {"entry_id": "x" * 129}])
def test_memory_malformed_projection_is_reported(env, changes):
    put_memories(env.alice, [{**memory_row("broken"), **changes}])
    response = env.memory.call(env.authorize("memory.search", {"query": "中文"}))
    data = thaw(response.data)
    assert data["complete"] is False and data["items"] == []
    assert data["load_errors"] and response.warnings


# LLM: 拒绝读取的控制文件不应借list/get暴露名称、长度、hash等元信息。
# 函数用途: 检查受保护制品在元数据入口也不可见。
@pytest.mark.parametrize("attack", ["control", "symlink", "outside_read_root"])
def test_artifact_unsafe_metadata_is_hidden(env, attack):
    path, record = put_artifact(env, name=".env" if attack == "control" else "output/report.bin")
    if attack == "symlink":
        other = path.with_name("other.bin")
        other.write_bytes(b"abcdef")
        path.unlink()
        path.symlink_to(other)
    if attack == "outside_read_root":
        scope = env.scopes[0]
        env.scopes = (replace(scope, read_roots=(scope.workspace_root / "elsewhere",)),)
    with pytest.raises(HostAPIError) as caught:
        env.artifacts.call(env.authorize("workspace.artifacts.get", {"artifact_ref": record.artifact_id}))
    assert (caught.value.code, caught.value.status) == ("RESOURCE_NOT_FOUND", 404)
    page = result(env.artifacts, env.authorize("workspace.artifacts.list", {"task_id": "task-one"}))
    assert page["items"] == []


# LLM: 数字虽有限也可能超出时间转换范围，应作为坏行诊断，不能使整页报未分类错误。
# 函数用途: 检查不可投影时间戳的注册账诊断。
def test_artifact_bad_timestamp_is_reported(env):
    put_artifact(env)
    path = registry_path(env.scopes[0].workspace_root)
    raw = json.loads(path.read_text().strip())
    raw["updated_at"] = 1e100
    path.write_text(json.dumps(raw) + "\n")
    response = env.artifacts.call(env.authorize("workspace.artifacts.list", {"task_id": "task-one"}))
    data = thaw(response.data)
    assert not data["complete"] and data["load_errors"] and response.warnings


# LLM: 此项是受控stat故障注入，只验证nlink分支；真实硬链接用例保留，不能用本项冒充其系统验证。
# 函数用途: 分别在打开前和FD复核时注入多链接状态，确保真实nofollow原语拒绝。
@pytest.mark.parametrize("phase", ["before_open", "after_open"])
def test_artifact_nlink_guard_fault_injection(env, monkeypatch, phase):
    path, record = put_artifact(env)
    inode, observed = path.stat().st_ino, []
    name = "stat" if phase == "before_open" else "fstat"
    original = getattr(os, name)

    # LLM: 只改变目标文件nlink，其余stat字段和所有IO仍来自实际文件系统。
    # 函数用途: 模拟读取期间出现第二个硬链接。
    def linked_stat(*args, **kwargs):
        info = original(*args, **kwargs)
        if info.st_ino == inode:
            values = list(info)
            values[3] = 2
            observed.append(True)
            return os.stat_result(values)
        return info

    monkeypatch.setattr(os, name, linked_stat)
    monkeypatch.setattr(os, "supports_dir_fd", {linked_stat if fn is original else fn for fn in os.supports_dir_fd})
    with pytest.raises(HostAPIError) as caught:
        env.artifacts.call(env.authorize("workspace.artifacts.read", {
            "artifact_ref": record.artifact_id, "expected_sha256": record.sha256}))
    assert observed and (caught.value.code, caught.value.status) == ("RESOURCE_NOT_FOUND", 404)


# LLM: 正文hash相同时游标仍必须拒绝跨entry和owner；不能把快照摘要当身份。
# 函数用途: 检查get游标的目标隔离。
@pytest.mark.parametrize("target", ["entry", "owner"])
def test_memory_get_cursor_target_isolation(env, target):
    rows = [memory_row("one", "中" * 30000), memory_row("two", "中" * 30000)]
    put_memories(env.alice, rows)
    put_memories(env.bob, rows)
    first = result(env.memory, env.authorize("memory.get", {"entry_id": "one"}))
    payload = {"entry_id": "two" if target == "entry" else "one", "cursor": first["next_cursor"]}
    with pytest.raises(HostAPIError) as caught:
        env.memory.call(env.authorize("memory.get", payload, home=env.bob if target == "owner" else env.alice))
    assert (caught.value.code, caught.value.status) == ("CURSOR_EXPIRED", 409)


# LLM: 通过真实读链后再篡改或撤销，结果必须被返回前的fence拦下，不能回正文。
# 函数用途: 检查读取后的文件替换、改写和授权撤销。
@pytest.mark.parametrize("attack", ["replace", "modify", "revoke"])
def test_artifact_post_read_fences(env, monkeypatch, attack):
    from agent_py_agent.agent.plugin_host import artifacts
    path, record = put_artifact(env)
    original = artifacts._hash_and_slice

    # LLM: 原hash和切片完整执行，故障只放在其后，模拟返回前竞态。
    # 函数用途: 在最后校验前改变目标或撤销调用。
    def interfere(descriptor, payload):
        data = original(descriptor, payload)
        if attack == "replace":
            replacement = path.with_name("replacement.bin")
            replacement.write_bytes(b"abcdef")
            replacement.replace(path)
        elif attack == "modify":
            path.write_bytes(b"xxxxxx")
        else:
            env.revoked = True
        return data

    monkeypatch.setattr(artifacts, "_hash_and_slice", interfere)
    with pytest.raises(HostAPIError) as caught:
        env.artifacts.call(env.authorize("workspace.artifacts.read", {
            "artifact_ref": record.artifact_id, "expected_sha256": record.sha256}))
    expected = ("CAPABILITY_DENIED", 403) if attack == "revoke" else ("ARTIFACT_CHANGED", 409)
    assert (caught.value.code, caught.value.status) == expected


# LLM: canonical registry自身及其父目录也不能借链接换到其它位置。
# 函数用途: 检查注册账符号链接拒绝。
def test_artifact_registry_symlink_is_rejected(env):
    put_artifact(env)
    path = registry_path(env.scopes[0].workspace_root)
    moved = path.with_name("moved.jsonl")
    path.rename(moved)
    path.symlink_to(moved)
    with pytest.raises(HostAPIError) as caught:
        env.artifacts.call(env.authorize("workspace.artifacts.list", {"task_id": "task-one"}))
    assert (caught.value.code, caught.value.status) == ("STORE_UNAVAILABLE", 503)


# LLM: 页游标绑定注册快照；重复页稳定，账更新必须拒绝旧游标。
# 函数用途: 检查制品list的重放和快照变更。
def test_artifact_list_cursor_snapshot(env):
    put_artifact(env, name="output/one.bin", ref="one")
    put_artifact(env, name="output/two.bin", ref="two")
    payload = {"task_id": "task-one", "limit": 1}
    first = result(env.artifacts, env.authorize("workspace.artifacts.list", payload))
    later = {**payload, "cursor": first["next_cursor"]}
    second = result(env.artifacts, env.authorize("workspace.artifacts.list", later))
    assert second == result(env.artifacts, env.authorize("workspace.artifacts.list", later))
    assert first["items"][0]["artifact_ref"] != second["items"][0]["artifact_ref"] and second["complete"]
    put_artifact(env, name="output/three.bin", ref="three")
    with pytest.raises(HostAPIError) as caught:
        env.artifacts.call(env.authorize("workspace.artifacts.list", later))
    assert (caught.value.code, caught.value.status) == ("CURSOR_EXPIRED", 409)


# LLM: 原账不变但正式记录到期时可见集合仍变化，不能让位置游标静默跳过后续条目。
# 函数用途: 验证时间驱动的记忆快照失效。
def test_memory_search_expiration_fences_snapshot(env, monkeypatch):
    from agent_py_agent.agent.plugin_host import memory
    monkeypatch.setattr(memory.time, "time", lambda: 1000.0)
    put_memories(env.alice, [memory_row("a", expires_at=1001), memory_row("b"), memory_row("c")])
    payload = {"query": "中文", "limit": 1}
    first = result(env.memory, env.authorize("memory.search", payload))
    assert first["items"][0]["entry_id"] == "a"
    monkeypatch.setattr(memory.time, "time", lambda: 1002.0)
    with pytest.raises(HostAPIError) as caught:
        env.memory.call(env.authorize("memory.search", {**payload, "cursor": first["next_cursor"]}))
    assert (caught.value.code, caught.value.status) == ("CURSOR_EXPIRED", 409)
