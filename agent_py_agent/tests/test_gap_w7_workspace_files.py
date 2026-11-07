"""W7 文件写入合同：真实临时文件、精确 CAS 和生产门户回环。"""
from __future__ import annotations

import base64
import hashlib
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.plugin_host import workspace_v2_handlers as files
from agent_py_agent.agent.plugin_host.action_specs import action_spec
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, ensure_owner_home


@pytest.fixture
def case(tmp_path):
    home = ensure_owner_home(tmp_path / "host", OwnerIdentity.local_user("alice"))
    return SimpleNamespace(owner_home=home, identity=home.identity)


def encoded(value):
    return base64.b64encode(value).decode("ascii")


def sha(value):
    return hashlib.sha256(value).hexdigest()


def test_mkdir_write_read_delete_cas(case):
    root = case.owner_home.workspace_dir
    assert files.mkdir_workspace_file(case, {"path": "docs/nested", "parents": True}) == {
        "path": "docs/nested", "created": True}
    assert files.mkdir_workspace_file(case, {"path": "docs/nested"})["created"] is False
    result = files.write_workspace_file(case, {"path": "docs/nested/a.txt", "content_b64": encoded(b"first"),
                                               "create_only": True})
    assert result == {"path": "docs/nested/a.txt", "sha256": sha(b"first"), "size": 5, "created": True}
    read = files.read_workspace_file(case, {"workspace_dir": str(root), "path": result["path"]})
    assert read["sha256"] == sha(b"first")
    for extra in ({"create_only": True}, {"expected_sha256": "0" * 64}):
        with pytest.raises(HostAPIError) as conflict:
            files.write_workspace_file(case, {"path": result["path"], "content_b64": encoded(b"bad"), **extra})
        assert conflict.value.code == "REVISION_CONFLICT"
        assert conflict.value.to_payload()["details"]["current_sha256"] == sha(b"first")
    assert (root / result["path"]).read_bytes() == b"first"
    changed = files.write_workspace_file(case, {"path": result["path"], "content_b64": encoded(b"second"),
                                                "expected_sha256": read["sha256"]})
    assert changed["created"] is False and changed["sha256"] == sha(b"second")
    with pytest.raises(HostAPIError) as conflict:
        files.delete_workspace_file(case, {"path": result["path"], "expected_sha256": read["sha256"]})
    assert conflict.value.code == "REVISION_CONFLICT"
    assert files.delete_workspace_file(case, {"path": result["path"], "expected_sha256": changed["sha256"]}) == {"deleted": True}
    assert files.delete_workspace_file(case, {"path": "docs/nested", "expected_sha256": "dir"}) == {"deleted": True}
    with pytest.raises(HostAPIError) as absent:
        files.delete_workspace_file(case, {"path": result["path"], "expected_sha256": changed["sha256"]})
    assert absent.value.code == "RESOURCE_NOT_FOUND"


def test_create_absent_and_empty_directory_contract(case):
    root = case.owner_home.workspace_dir
    created = files.write_workspace_file(case, {"path": "a", "content_b64": encoded(b"x"), "expected_sha256": "absent"})
    assert created["created"] is True
    files.mkdir_workspace_file(case, {"path": "dir"})
    (root / "dir" / "child").write_bytes(b"keep")
    with pytest.raises(HostAPIError):
        files.delete_workspace_file(case, {"path": "dir", "expected_sha256": "dir"})
    assert (root / "dir" / "child").read_bytes() == b"keep"
    with pytest.raises(HostAPIError) as exists:
        files.mkdir_workspace_file(case, {"path": "a"})
    assert exists.value.code == "STATE_CONFLICT"


@pytest.mark.parametrize("value", ["../outside", "/outside", "a/../outside", "-option", ".git/config", "a/.GIT/config", "a\\b", "a\x00b"])
@pytest.mark.parametrize("action", ["mkdir_workspace_file", "write_workspace_file", "delete_workspace_file"])
def test_paths_rejected_before_directory_io(case, monkeypatch, value, action):
    called = []
    monkeypatch.setattr(files, "_directory", lambda *a, **k: called.append(a))
    with pytest.raises(HostAPIError) as error:
        getattr(files, action)(case, {"path": value, "content_b64": encoded(b"x"), "expected_sha256": "0" * 64})
    assert error.value.code == "PATH_OUTSIDE_WORKSPACE"
    assert called == []


@pytest.mark.parametrize("kind", ["parent", "leaf", "hardlink"])
@pytest.mark.parametrize("action", ["mkdir_workspace_file", "write_workspace_file", "delete_workspace_file"])
def test_link_escapes_preserve_external_file(case, tmp_path, monkeypatch, action, kind):
    root = case.owner_home.workspace_dir
    external = tmp_path / "outside"
    external.mkdir()
    target = external / "keep"
    target.write_bytes(b"outside-unchanged")
    if kind == "parent":
        (root / "link").symlink_to(external, target_is_directory=True)
        path = "link/keep"
    elif kind == "leaf":
        (root / "link").symlink_to(target)
        path = "link"
    else:
        (root / "link").write_bytes(target.read_bytes())
        original = files._entry_stat
        def multi_link(parent, name):
            result = original(parent, name)
            return SimpleNamespace(**{field: getattr(result, field) for field in
                ("st_mode", "st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")}, st_nlink=2)
        monkeypatch.setattr(files, "_entry_stat", multi_link)
        path = "link"
    with pytest.raises(HostAPIError) as error:
        getattr(files, action)(case, {"path": path, "content_b64": encoded(b"x"), "expected_sha256": sha(target.read_bytes())})
    assert error.value.code == "PATH_OUTSIDE_WORKSPACE"
    assert target.read_bytes() == b"outside-unchanged"


def test_upload_honors_declared_revision(case):
    root = case.owner_home.workspace_dir
    payload = {"workspace_dir": str(root), "path": "a.txt", "data_base64": encoded(b"one"), "expected_revision": 0}
    uploaded = files.upload_workspace_file(case, payload)
    target = root / uploaded["path"]
    assert uploaded["revision"] == target.stat().st_mtime_ns
    with pytest.raises(HostAPIError) as conflict:
        files.upload_workspace_file(case, {**payload, "data_base64": encoded(b"two")})
    assert conflict.value.code == "REVISION_CONFLICT"
    assert conflict.value.to_payload()["details"]["current_revision"] == uploaded["revision"]
    assert target.read_bytes() == b"one"
    accepted = files.upload_workspace_file(case, {**payload, "expected_revision": uploaded["revision"], "data_base64": encoded(b"two")})
    assert accepted["revision"] == target.stat().st_mtime_ns and target.read_bytes() == b"two"


def test_competing_cas_writes_have_only_one_winner(case):
    payload = {"path": "shared", "content_b64": encoded(b"initial"), "create_only": True}
    initial = files.write_workspace_file(case, payload)
    def write(value):
        try:
            return files.write_workspace_file(case, {"path": "shared", "content_b64": encoded(value),
                                                       "expected_sha256": initial["sha256"]})
        except HostAPIError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, (b"left", b"right")))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count("REVISION_CONFLICT") == 1
    assert (case.owner_home.workspace_dir / "shared").read_bytes() in (b"left", b"right")


def test_schema_is_exact_and_no_owner_override():
    valid = {"workspace.files.mkdir": {"path": "folder", "parents": True},
             "workspace.files.write": {"path": "a", "content_b64": "eA==", "create_only": True},
             "workspace.files.delete": {"path": "a", "expected_sha256": "a" * 64}}
    for action, params in valid.items():
        assert action_spec(action).validate_payload(params) == params
        for extra in ({"owner_id": "other"}, {"workspace_dir": "/outside"}):
            with pytest.raises(HostAPIError):
                action_spec(action).validate_payload({**params, **extra})


def test_read_hash_matches_returned_content(case):
    root = case.owner_home.workspace_dir
    (root / "text").write_text("一\n二\n三\n", encoding="utf-8")
    read = files.read_workspace_file(case, {"workspace_dir": str(root), "path": "text", "start_line": 2, "end_line": 2})
    assert read["sha256"] == sha(read["content"].encode())
    (root / "binary").write_bytes(b"\x00\xffcontent")
    read = files.read_workspace_file(case, {"workspace_dir": str(root), "path": "binary"})
    assert read["sha256"] == sha(base64.b64decode(read["content"]))


def test_write_decoded_limit_and_required_cas(case, monkeypatch):
    monkeypatch.setattr(files, "_MAX", 4)
    with pytest.raises(HostAPIError) as limit:
        files.write_workspace_file(case, {"path": "big", "content_b64": encoded(b"large"), "create_only": True})
    assert limit.value.code == "LIMIT_EXCEEDED"
    with pytest.raises(HostAPIError) as missing:
        files.write_workspace_file(case, {"path": "unchecked", "content_b64": encoded(b"ok")})
    assert missing.value.code == "INVALID_SCHEMA"
    assert not (case.owner_home.workspace_dir / "big").exists()


def test_portal_production_wire_roundtrip(production_host):
    from agent_py_agent.tests.test_plugin_host_gateway_composition import production_call
    host = production_host
    result = production_call(host, "workspace.files.mkdir", {"path": "docs"}, key="w7_mkdir_wire_0001")
    assert result == {"path": "docs", "created": True}
    written = production_call(host, "workspace.files.write", {"path": "docs/a", "content_b64": "eA==", "create_only": True}, key="w7_write_wire_0001")
    assert written["sha256"] == sha(b"x")
    root = host[2].workspace_dir
    assert (root / "docs/a").read_bytes() == b"x"
    read = production_call(host, "workspace.files.read", {"workspace_dir": str(root), "path": "docs/a"})
    assert read["sha256"] == written["sha256"]
    deleted = production_call(host, "workspace.files.delete", {"path": "docs/a", "expected_sha256": written["sha256"]}, key="w7_delete_wire_0001")
    assert deleted == {"deleted": True} and not (root / "docs/a").exists()


# 复用真实安装和 HTTP 夹具；不复制一套自造的服务端协议。
@pytest.fixture
def production_host(tmp_path, request):
    from agent_py_agent.tests.test_plugin_host_gateway_composition import (
        production_host as real_fixture,
    )
    yield from real_fixture.__wrapped__(tmp_path, request)


@pytest.mark.parametrize("operation", ["write", "upload"])
@pytest.mark.parametrize("change", ["parent", "leaf"])
def test_cas_rechecks_held_parent_and_leaf_before_publish(case, monkeypatch, operation, change):
    from threading import Event

    root = case.owner_home.workspace_dir
    folder = root / ("uploads" if operation == "upload" else "docs")
    folder.mkdir()
    target = folder / "item"
    target.write_bytes(b"checked-A")
    revision = target.stat().st_mtime_ns
    entered, resume = Event(), Event()
    original = files.write_bytes_atomic_beneath

    def pause_publish(*args, **kwargs):
        entered.set()
        assert resume.wait(5), "父目录交错未释放"
        return original(*args, **kwargs)

    monkeypatch.setattr(files, "write_bytes_atomic_beneath", pause_publish)
    if operation == "write":
        def invoke():
            return files.write_workspace_file(case, {"path": "docs/item", "content_b64": encoded(b"new"),
                                                                   "expected_sha256": sha(b"checked-A")})
    else:
        def invoke():
            return files.upload_workspace_file(case, {"workspace_dir": str(root), "path": "item",
                                                                   "data_base64": encoded(b"new"), "expected_revision": revision})
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(invoke)
        try:
            assert entered.wait(5), "未进入真实 CAS 后的发表入口"
            if change == "parent":
                folder.rename(root / "detached")
                folder.mkdir()
            target.write_bytes(b"unchecked-B")
        finally:
            resume.set()
        with pytest.raises(HostAPIError) as refused:
            future.result(timeout=5)
    assert refused.value.code in {"REVISION_CONFLICT", "PATH_OUTSIDE_WORKSPACE"}
    assert target.read_bytes() == b"unchecked-B"
    if change == "parent":
        assert (root / "detached/item").read_bytes() == b"checked-A"
    assert not list(folder.glob("*.tmp-*"))
