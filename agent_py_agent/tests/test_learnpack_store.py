"""learnpack 宿主存储（<owner home>/data/learnpack/）。

锁定：打包产物按 sha256 内容寻址、权限 0600、读回重算摘要（被改过就当不存在）；同一字节只存一份；
待确认安装单号格式固定、只能执行一次（第二次抢不到）、结果可读回；安装记录可追加和读出；存储位置在 owner 根 data/ 下
（H3 宿主运行状态，模型工具只读）。
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
from agent_py_agent.agent.capability.package_build import BuiltPackage
from agent_py_agent.agent.path_access_policy import HOST_STATE_OWNER_DIRS

_PROVENANCE = BuildProvenance("github.com/example/drama-agent@abc123", "MIT", "run-1")


def _built(payload: bytes = b"zip-bytes") -> BuiltPackage:
    import hashlib

    return BuiltPackage("capability_pack", "drama-scenes", "0.1.0", payload, hashlib.sha256(payload).hexdigest(),
                        ("CAPABILITY.md",))


def test_store_lives_under_owner_host_state_data_dir():
    assert STORE_PARTS[0] == "data" and ("data",) in HOST_STATE_OWNER_DIRS


def test_build_is_content_addressed_private_and_verified_on_read(tmp_path):
    store = LearnpackStore(tmp_path)
    built = _built()
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
    store = LearnpackStore(tmp_path)
    record = store.save_build(_built(), _PROVENANCE)
    order = store.create_order(record)
    assert ORDER_ID_PATTERN.fullmatch(order.order_id)
    assert store.order(order.order_id) == order and store.order("lp-zzzz") is None
    assert store.order_outcome(order.order_id) is None
    assert store.claim_order(order.order_id) is True
    assert store.claim_order(order.order_id) is False
    assert store.order_outcome(order.order_id) == {"state": "executing"}
    store.finish_order(order.order_id, {"install_state": "installed_enabled"})
    assert store.order_outcome(order.order_id) == {"state": "finished", "install_state": "installed_enabled"}
    # 换一个存储对象（相当于 Gateway 重启后）照样读得到单子，也照样只能执行一次。
    reopened = LearnpackStore(tmp_path)
    assert reopened.order(order.order_id) == order and reopened.claim_order(order.order_id) is False


def test_install_records_append_and_read_back(tmp_path):
    store = LearnpackStore(tmp_path)
    store.record_install({"package_id": "drama-scenes", "sha256": "a" * 64, "via": "self"})
    store.record_install({"package_id": "drama-scenes", "sha256": "b" * 64, "previous_sha256": "a" * 64})
    rows = store.installs()
    assert [row["sha256"] for row in rows] == ["a" * 64, "b" * 64] and all("at" in row for row in rows)
    assert Path(tmp_path, *STORE_PARTS, "installs.jsonl").stat().st_mode & 0o077 == 0
