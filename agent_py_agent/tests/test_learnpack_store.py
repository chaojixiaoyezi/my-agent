"""learnpack 宿主存储（<owner home>/data/learnpack/）。

锁定：打包产物按 sha256 内容寻址、权限 0600、读回重算摘要（被改过就当不存在）；同一字节只存一份；
待确认安装单号格式固定、只能执行一次（第二次抢不到）、结果可读回；安装记录可追加和读出；"她装过的包名"可重复记、
不合规的包名一律当没装过；存储位置在 owner 根 data/ 下（H3 宿主运行状态，模型工具只读）。
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

from agent_py_agent.agent.capability.learnpack_store import (
    ORDER_ID_PATTERN,
    STORE_PARTS,
    BuildProvenance,
    LearnpackStore,
)
from agent_py_agent.agent.capability.package_build import (
    BuiltPackage,
    build_capability_pack,
    read_declared_files,
)
from agent_py_agent.agent.path_access_policy import HOST_STATE_OWNER_DIRS

_PROVENANCE = BuildProvenance("github.com/example/drama-agent@abc123", "MIT", "run-1")


def _built(tmp_path: Path, version: str = "0.1.0") -> BuiltPackage:
    root = tmp_path / f"src-{version}"
    root.mkdir(exist_ok=True)
    (root / "CAPABILITY.md").write_text(f"# 短剧分场 {version}\n", encoding="utf-8")
    declaration = {"plugin_id": "drama-scenes", "version": version, "summary": "短剧分场方法",
                   "capability": {"description": "把短剧故事拆成场次", "keywords": ["短剧"], "entry_document": "CAPABILITY.md"},
                   "files": [{"path": "CAPABILITY.md"}], "settings_schema": {"type": "object", "properties": {}}}
    return build_capability_pack(declaration, read_declared_files(root, ["CAPABILITY.md"]))


def test_store_lives_under_owner_host_state_data_dir():
    assert STORE_PARTS[0] == "data" and ("data",) in HOST_STATE_OWNER_DIRS


def test_build_is_content_addressed_private_and_verified_on_read(tmp_path):
    store = LearnpackStore(tmp_path / "owner")
    built = _built(tmp_path)
    record = store.save_build(built, _PROVENANCE)
    assert (record.origin, record.license, record.run_id, record.file_count) == (
        "github.com/example/drama-agent@abc123", "MIT", "run-1", 1)
    path = store.build_path(built.sha256)
    assert path.read_bytes() == built.payload and stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert store.build(built.sha256) == record
    store.save_build(built, _PROVENANCE)
    assert len(list(path.parent.glob("*.zip"))) == 1
    path.write_bytes(b"tampered")
    assert store.build(built.sha256) is None
    store.save_build(built, _PROVENANCE)  # 被改坏的内容寻址文件会被原子替换回正确字节
    assert store.build(built.sha256) is not None


def test_unknown_or_malformed_sha_is_not_a_build(tmp_path):
    store = LearnpackStore(tmp_path)
    assert store.build("0" * 64) is None and store.build("../x") is None and store.build(42) is None


def test_order_executes_only_once_and_keeps_its_outcome(tmp_path):
    store = LearnpackStore(tmp_path / "owner")
    record = store.save_build(_built(tmp_path), _PROVENANCE)
    order = store.create_order(record)
    assert ORDER_ID_PATTERN.fullmatch(order.order_id)
    assert store.order(order.order_id) == order and store.order("lp-zzzz") is None
    assert store.order_outcome(order.order_id) is None
    assert store.claim_order(order.order_id) is True
    assert store.claim_order(order.order_id) is False
    assert store.order_outcome(order.order_id) == {"state": "executing", "steps": []}
    store.note_order_step(order.order_id, "lp-1", "/plugins install x")
    store.finish_order(order.order_id, {"install_state": "installed_enabled"})
    assert store.order_outcome(order.order_id) == {
        "state": "finished", "install_state": "installed_enabled",
        "steps": [{"request_id": "lp-1", "command": "/plugins install x"}]}
    # 换一个存储对象（相当于 Gateway 重启后）照样读得到单子，也照样只能执行一次。
    reopened = LearnpackStore(tmp_path / "owner")
    assert reopened.order(order.order_id) == order and reopened.claim_order(order.order_id) is False


def test_install_records_append_and_read_back(tmp_path):
    store = LearnpackStore(tmp_path)
    store.record_install({"package_id": "drama-scenes", "sha256": "a" * 64, "via": "self"})
    store.record_install({"package_id": "drama-scenes", "sha256": "b" * 64, "previous_sha256": "a" * 64})
    rows = store.installs()
    assert [row["sha256"] for row in rows] == ["a" * 64, "b" * 64] and all("at" in row for row in rows)
    assert Path(tmp_path, *STORE_PARTS, "installs.jsonl").stat().st_mode & 0o077 == 0


def test_record_must_match_the_package_manifest(tmp_path):
    import json

    store = LearnpackStore(tmp_path / "owner")
    built = _built(tmp_path)
    store.save_build(built, _PROVENANCE)
    record_path = store.root / "builds" / f"{built.sha256}.json"
    raw = json.loads(record_path.read_text(encoding="utf-8"))
    record_path.write_text(json.dumps({**raw, "package_id": "other-pack"}), encoding="utf-8")
    assert store.build(built.sha256) is None, "记录里的包名和包清单对不上就当不存在"


def test_order_ids_never_overwrite_each_other(tmp_path, monkeypatch):
    import agent_py_agent.agent.capability.learnpack_store as module

    store = LearnpackStore(tmp_path / "owner")
    record = store.save_build(_built(tmp_path), _PROVENANCE)
    tokens = iter(["aaaaaaaa", "aaaaaaaa", "bbbbbbbb"])
    monkeypatch.setattr(module.secrets, "token_hex", lambda _n: next(tokens))
    first, second = store.create_order(record), store.create_order(record)
    assert (first.order_id, second.order_id) == ("lp-aaaaaaaa", "lp-bbbbbbbb")


def test_owned_ids_are_idempotent_and_reject_odd_names(tmp_path):
    store = LearnpackStore(tmp_path / "owner")
    assert not store.owns_id("drama-scenes")
    store.remember_owned_id("drama-scenes")
    store.remember_owned_id("drama-scenes")  # 每条宿主命令前都会记一次，不能报错
    assert store.owns_id("drama-scenes") and not store.owns_id("novel-outline")
    assert stat.S_IMODE(os.stat(store.root / "owned" / "drama-scenes.json").st_mode) == 0o600
    assert not any(store.owns_id(name) for name in ("", "../drama-scenes", ".hidden", "a/b", 42))


def test_events_share_one_increasing_sequence_that_survives_a_broken_counter(tmp_path):
    store = LearnpackStore(tmp_path / "owner")
    record = store.save_build(_built(tmp_path), _PROVENANCE)
    first = store.create_order(record)
    store.record_install({"package_id": "drama-scenes", "sha256": record.sha256})
    second = store.create_order(record)
    assert first.seq < store.installs()[-1]["seq"] < second.seq
    (store.root / "seq.json").write_text("{坏了", encoding="utf-8")
    third = store.create_order(record)
    assert third.seq > second.seq, "计数文件坏了就从已有最大序号接着往上，不倒退"
    (store.root / "seq.json").unlink()
    assert store.create_order(record).seq > third.seq, "计数文件丢了（被删、备份漏掉）也接着往上"
