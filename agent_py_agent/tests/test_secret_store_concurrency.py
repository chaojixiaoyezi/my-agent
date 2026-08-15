"""审计 #12 修复真测:SecretStore 读改写加锁(并发不丢密钥)+ 损坏文件拒绝写入(不覆盖销毁其余密钥)。

真起 20 线程并发 register,断言 20 条全保留(原无锁读改写会丢更新);真损坏库文件,断言 register 抛错且
不把损坏文件覆盖成单条新密钥(原 _read 把损坏静默当空 → 一条 register 即销毁其余全部密钥)。
"""

from __future__ import annotations

import threading

import pytest

from agent_py_agent.agent.secret_store import SecretStore, SecretStoreError


def test_concurrent_register_no_lost_secrets(tmp_path) -> None:
    store = SecretStore(tmp_path / "s")
    errors: list[Exception] = []

    def reg(i: int) -> None:
        try:
            store.register(f"k{i}", f"v{i}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=reg, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(store.list_summaries()) == 20  # 20 并发 register 全保留,无丢更新


def test_corrupt_file_refuses_write_not_destroy(tmp_path) -> None:
    store = SecretStore(tmp_path / "s")
    store.register("k1", "v1")
    store_path = tmp_path / "s" / "store.json"
    store_path.write_text("{corrupt not json", encoding="utf-8")  # 损坏库文件
    with pytest.raises(SecretStoreError):
        store.register("k2", "v2")  # 损坏库 → 拒绝写入,不覆盖
    # 损坏内容原样保留(没被一条新密钥覆盖销毁其余),可人工抢救
    assert store_path.read_text(encoding="utf-8") == "{corrupt not json"


def test_corrupt_file_remove_refuses(tmp_path) -> None:
    store = SecretStore(tmp_path / "s")
    ref = store.register("k1", "v1")
    (tmp_path / "s" / "store.json").write_text("not json", encoding="utf-8")
    with pytest.raises(SecretStoreError):
        store.remove(ref)  # remove 也走 _read_for_mutation,损坏拒绝


def test_register_remove_roundtrip_unchanged(tmp_path) -> None:
    store = SecretStore(tmp_path / "s")
    ref = store.register("k", "v")
    assert store.exists(ref)
    assert store.remove(ref) is True
    assert not store.exists(ref)
    assert store.remove(ref) is False
