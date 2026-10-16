"""唯一owner来源仓库的真实文件/CAS业务反例，全部使用临时home。"""
import json
import os
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

from agent_py_agent.agent.capability.agents_source_contract import AgentsContractError
from agent_py_agent.agent.capability.agents_source_repository import AgentsSourceRepository
from agent_py_agent.agent.capability.agents_write_contract import (
    AgentsWriteAuthorization,
    AgentsWriteContext,
    write_intent_digest,
)
from agent_py_agent.agent.path_access_policy import PathAccessPolicy


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "data"
    home = root / "owners/local/main"
    home.mkdir(parents=True)
    default = home / "AGENTS.md"
    default.write_bytes("\ufeff# AGENTS\r\n- 按用户要求做事。\r\n".encode())
    policy = PathAccessPolicy.from_values(mode="full_access", agent_home_root=root)
    return AgentsSourceRepository(owner="local/main", owner_home=home, path_policy=policy)


def grant(repository, action, value, *, content_sha=None):
    snapshot = repository.configuration()
    if content_sha is None:
        content_sha = repository.load().snapshot.content_sha256
    now = 1000.0
    auth = AgentsWriteAuthorization(
        1, "host-auth", repository.owner, repository.owner, "thread", "turn", "request",
        action, snapshot.configuration_cas, content_sha, now-1, now+100,
        "host-request", "", write_intent_digest(action, value),
    )
    context = AgentsWriteContext(
        1, repository.owner, repository.owner, "thread", "turn", "request", action,
        snapshot.configuration_cas, content_sha, now, "user_dialogue", auth, (),
        False, False, "", auth.intent_sha256,
    )
    return auth, context


def choose(repository, path):
    value = {"kind": "local_path", "locator": str(path)}
    auth, context = grant(repository, "set_source", value)
    return repository.set_source(value["kind"], value["locator"], authorization=auth, context=context)


def test_default_load_keeps_exact_bytes_and_creates_no_source_state(source):
    before = source.default_path.read_bytes()
    result = source.load()
    assert result is not None, "默认来源加载入口必须交回实际正文，不是API空壳"
    assert result.content.encode() == before
    assert result.snapshot.owner == "local/main"
    assert result.snapshot.kind == "owner_default"
    assert result.snapshot.revision == 0
    assert result.snapshot.status == "ready"
    assert result.snapshot.prefix_digest == sha256(before).hexdigest()
    assert not (source.owner_home / "persona").exists()


def test_explicit_configuration_and_local_write_are_bound_to_owner_and_full_cas(source):
    path = source.owner_home / "rulebook.txt"
    path.write_text("# 我的工作约定\n- 精确完成任务。\n")
    changed = choose(source, path)
    assert changed.kind == "local_path"
    assert changed.revision == 1
    assert source.load().content == path.read_text()
    record = json.loads(source.config_path.read_text())
    assert record["owner"] == "local/main"
    assert record["value"] == str(path)
    assert record["updated_by"] == "local/main"
    text = "# 我的工作约定\n- 不要改用户原件。\n"
    auth, context = grant(source, "write", text)
    inode = path.stat().st_ino
    result = source.write_text(text, authorization=auth, context=context)
    assert result.content == text
    assert path.read_bytes() == text.encode()
    assert path.stat().st_ino == inode, "提交应使用已核验同FD，不能重新按路径打开或原子替换别的文件"


def test_no_json_or_missing_authorization_can_set_source(source):
    path = source.owner_home / "rulebook.txt"
    path.write_text("约定")
    before = source.default_path.read_bytes()
    with pytest.raises(AgentsContractError):
        source.set_source("local_path", str(path), authorization={"role": "user"}, context=None)
    assert not source.config_path.exists()
    assert source.default_path.read_bytes() == before


@pytest.mark.parametrize("field,value", [("owner", "other"), ("revision", 99), ("config_version", 2)])
def test_stale_or_cross_owner_cas_cannot_modify_configuration(source, field, value):
    path = source.owner_home / "rulebook.txt"
    path.write_text("约定")
    intent = {"kind": "local_path", "locator": str(path)}
    auth, context = grant(source, "set_source", intent)
    cas = list(auth.target_cas)
    cas[{"owner": 0, "revision": 4, "config_version": 3}[field]] = value
    bad_auth = replace(auth, target_cas=tuple(cas), owner=value if field == "owner" else auth.owner)
    bad_context = replace(context, target_cas=tuple(cas), owner=bad_auth.owner, host_authorization=bad_auth)
    with pytest.raises(AgentsContractError):
        source.set_source("local_path", str(path), authorization=bad_auth, context=bad_context)
    assert not source.config_path.exists()


def test_old_authorization_cannot_replace_new_configuration(source):
    first = source.owner_home / "first"
    second = source.owner_home / "second"
    first.write_text("一")
    second.write_text("二")
    auth, context = grant(source, "set_source", {"kind": "local_path", "locator": str(second)})
    choose(source, first)
    before = source.config_path.read_bytes()
    with pytest.raises(AgentsContractError, match="AGENTS_SOURCE_CONFIG_CONFLICT"):
        source.set_source("local_path", str(second), authorization=auth, context=context)
    assert source.config_path.read_bytes() == before


def test_revision_check_cannot_be_replaced_by_only_content_digest(source):
    first, second = [source.owner_home / name for name in ("same-body-first", "same-body-second")]
    for path in (first, second):
        path.write_text("相同正文仍然是不同来源配置")
    auth, context = grant(source, "set_source", {"kind": "local_path", "locator": str(second)}, content_sha="")
    choose(source, first)
    before = source.config_path.read_bytes()
    with pytest.raises(AgentsContractError, match="AGENTS_SOURCE_CONFIG_CONFLICT"):
        source.set_source("local_path", str(second), authorization=auth, context=context)
    assert source.config_path.read_bytes() == before


def test_actual_new_value_must_match_authorized_intent(source):
    paths = [source.owner_home / name for name in ("one", "two")]
    for path in paths:
        path.write_text("安全约定")
    auth, context = grant(source, "set_source", {"kind": "local_path", "locator": str(paths[0])})
    with pytest.raises(AgentsContractError, match="AGENTS_WRITE_BINDING_MISMATCH"):
        source.set_source("local_path", str(paths[1]), authorization=auth, context=context)
    assert not source.config_path.exists()


def test_corrupt_or_other_owner_config_fails_closed_without_default_fallback(source):
    path = source.owner_home / "chosen"
    path.write_text("约定")
    choose(source, path)
    record = json.loads(source.config_path.read_text())
    record["owner"] = "feishu/users/other"
    source.config_path.write_text(json.dumps(record))
    with pytest.raises(AgentsContractError, match="AGENTS_SOURCE_OWNER_MISMATCH"):
        source.load()
    source.config_path.write_text("broken JSON")
    with pytest.raises(AgentsContractError, match="AGENTS_SOURCE_CONFIG_INVALID"):
        source.load()


@pytest.mark.parametrize("target", ["other_owner", "persona", "soul", "credentials", "parent_traversal"])
def test_general_full_access_does_not_grant_forbidden_source(source, target):
    targets = {"other_owner": source.path_policy.agent_home_root / "owners/feishu/users/bob/note.txt",
               "persona": source.owner_home / "persona/agents_source.json",
               "soul": source.owner_home / "SOUL.md", "credentials": source.owner_home / ".ssh/id_rsa",
               "parent_traversal": source.owner_home / "dir/../secret"}
    path = targets[target]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("绝不注入的原件")
    value = {"kind": "local_path", "locator": str(path)}
    if target == "parent_traversal":
        with pytest.raises(AgentsContractError):
            write_intent_digest("set_source", value)
        return
    # persona目标本身正是坏配置，构造授权不读取这份被测文件。
    if target == "persona":
        with pytest.raises(AgentsContractError):
            source.load()
        return
    auth, context = grant(source, "set_source", value)
    with pytest.raises(AgentsContractError):
        source.set_source("local_path", str(path), authorization=auth, context=context)
    assert not source.config_path.exists()


@pytest.mark.parametrize("replacement", ["leaf", "parent", "symlink", "hardlink"])
def test_configured_source_identity_cannot_be_replaced_even_with_same_bytes(source, replacement):
    parent = source.owner_home / "docs"
    parent.mkdir()
    path = parent / "rules.anything"
    path.write_text("# 工作约定\n- 保留输入。\n")
    choose(source, path)
    original = path.read_bytes()
    _replace_source_identity(path, replacement, original)
    result = source.load()
    assert result.content is None
    assert result.snapshot.status == "unavailable"
    assert result.snapshot.failure_code in {"AGENTS_SOURCE_IDENTITY_CHANGED", "AGENTS_SOURCE_UNSAFE_PATH"}


def test_strict_nofollow_unavailable_does_not_fall_back_to_plain_open(source, monkeypatch):
    from agent_py_agent.agent.common import nofollow_fs
    monkeypatch.setattr(nofollow_fs, "_supports_dir_fd", lambda: False)
    result = source.load()
    assert result.content is None
    assert result.snapshot.failure_code == "AGENTS_SOURCE_UNSAFE_PATH"


def test_write_rechecks_current_content_and_limit_before_any_changes(source):
    path = source.owner_home / "rules"
    path.write_text("初始约定")
    choose(source, path)
    auth, context = grant(source, "write", "新约定")
    path.write_text("用户刚更新的约定")
    with pytest.raises(AgentsContractError, match="AGENTS_SOURCE_CONTENT_MISMATCH"):
        source.write_text("新约定", authorization=auth, context=context)
    assert path.read_text() == "用户刚更新的约定"
    text = "常" * 20_001
    auth, context = grant(source, "write", text)
    with pytest.raises(ValueError, match="AGENTS_WRITE_LIMIT"):
        source.write_text(text, authorization=auth, context=context)
    assert path.read_text() == "用户刚更新的约定"


@pytest.mark.parametrize("body,code", [(b"\xff", "AGENTS_SOURCE_ENCODING_INVALID"),
                                      (b"x" * (2*1024*1024+1), "AGENTS_SOURCE_TOO_LARGE")])
def test_invalid_utf8_or_oversize_cannot_be_ready(source, body, code):
    source.default_path.write_bytes(body)
    result = source.load()
    assert result.content is None
    assert result.snapshot.failure_code == code


@pytest.mark.parametrize("relative", ["quota.json", "retention.json", "tool_policy.json",
                                     "runtime.db", "config/capability_config.yaml"])
def test_host_control_file_is_not_a_configurable_source_even_with_valid_grant(source, relative):
    path = source.owner_home / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    original = b'{"protected": true}\n'
    path.write_bytes(original)
    value = {"kind": "local_path", "locator": str(path)}
    auth, context = grant(source, "set_source", value)
    with pytest.raises(AgentsContractError, match="AGENTS_SOURCE_CONTROL_PATH_FORBIDDEN"):
        source.set_source(value["kind"], value["locator"], authorization=auth, context=context)
    assert path.read_bytes() == original
    assert not source.config_path.exists()


# LLM: 四种身份替换保留原字节；仅拆平变异夹具，失败断言仍由真实来源加载判定。
# 函数用途: 替换父目录或叶子，生成符号链接或硬链接反例。
def _replace_source_identity(path, replacement, original):
    parent = path.parent
    if replacement == "parent":
        parent.rename(parent.with_name("old-docs"))
        parent.mkdir()
        path.write_bytes(original)
        return
    saved = path.with_name("original")
    path.rename(saved)
    if replacement == "leaf":
        path.write_bytes(original)
        return
    if replacement == "symlink":
        path.symlink_to(saved)
        return
    os.link(saved, path)
