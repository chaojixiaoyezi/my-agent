"""Phase 1 加密密钥库测试:加密往返、脱敏、0o600、错误主密钥拒绝、明文不外泄。"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from agent_py_agent.agent.secret_store import SecretStore


def test_register_returns_ref_and_resolves_plaintext(tmp_path: Path) -> None:
    store = SecretStore(tmp_path / "secrets")
    ref = store.register("minimax_api_key", "sk-secret-value-123")

    assert ref.startswith("sec_")
    assert store.resolve_plaintext_for_gateway(ref) == "sk-secret-value-123"


def test_summary_and_list_never_contain_plaintext(tmp_path: Path) -> None:
    store = SecretStore(tmp_path / "secrets")
    ref = store.register("feishu_app_secret", "PLAINTEXT-SHOULD-NOT-LEAK")

    summary = store.summary(ref)
    assert summary.name == "feishu_app_secret"
    assert "PLAINTEXT-SHOULD-NOT-LEAK" not in json.dumps(summary.to_dict())
    listed = store.list_summaries()
    assert all("PLAINTEXT-SHOULD-NOT-LEAK" not in json.dumps(s.to_dict()) for s in listed)
    # 落盘文件里也只能是密文,不能有明文
    raw = (tmp_path / "secrets" / "store.json").read_text(encoding="utf-8")
    assert "PLAINTEXT-SHOULD-NOT-LEAK" not in raw


def test_at_rest_file_is_encrypted_and_0600(tmp_path: Path) -> None:
    store = SecretStore(tmp_path / "secrets")
    store.register("k", "topsecret")
    master = tmp_path / "secrets" / "master.key"
    body = tmp_path / "secrets" / "store.json"
    # 主密钥 0o600(仅属主读写)
    assert stat.S_IMODE(os.stat(master).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(body).st_mode) == 0o600


def test_persists_across_instances(tmp_path: Path) -> None:
    ref = SecretStore(tmp_path / "secrets").register("k", "persisted-value")
    # 新实例(重新加载持久化的 master.key)仍能解
    assert SecretStore(tmp_path / "secrets").resolve_plaintext_for_gateway(ref) == "persisted-value"


def test_wrong_master_key_is_rejected(tmp_path: Path) -> None:
    ref = SecretStore(tmp_path / "store_a").register("k", "v")
    # 把 A 的密文搬到 B(B 有不同 master.key)→ 解不开,报 PermissionError 而非泄漏/崩溃
    store_b_dir = tmp_path / "store_b"
    SecretStore(store_b_dir)  # 触发生成 B 的 master.key
    data = json.loads((tmp_path / "store_a" / "store.json").read_text(encoding="utf-8"))
    (store_b_dir / "store.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(PermissionError):
        SecretStore(store_b_dir).resolve_plaintext_for_gateway(ref)


def test_invalid_ref_rejected(tmp_path: Path) -> None:
    store = SecretStore(tmp_path / "secrets")
    with pytest.raises(PermissionError):
        store.resolve_plaintext_for_gateway("/etc/passwd")
    with pytest.raises(PermissionError):
        store.resolve_plaintext_for_gateway("not-a-sec-ref")


def test_remove_and_missing(tmp_path: Path) -> None:
    store = SecretStore(tmp_path / "secrets")
    ref = store.register("k", "v")
    assert store.exists(ref) is True
    assert store.remove(ref) is True
    assert store.exists(ref) is False
    assert store.remove(ref) is False
    with pytest.raises(KeyError):
        store.resolve_plaintext_for_gateway(ref)


def test_register_rejects_empty(tmp_path: Path) -> None:
    store = SecretStore(tmp_path / "secrets")
    with pytest.raises(ValueError):
        store.register("", "v")
    with pytest.raises(ValueError):
        store.register("k", "")


def test_redact_is_irreversible(tmp_path: Path) -> None:
    assert SecretStore.redact("sk-1234567890") == "sk***90"
    assert SecretStore.redact("abcd") == "****"
    assert SecretStore.redact("") == ""


def test_resolve_source_handles_ref_and_env(tmp_path: Path, monkeypatch) -> None:
    store = SecretStore(tmp_path / "secrets")
    ref = store.register("k", "from-encrypted-store")
    # 加密引用源(claw 式)
    assert store.resolve_source(ref) == "from-encrypted-store"
    # env 源(长期助手 式,兼容现有 AGENT_API_KEY 等)
    monkeypatch.setenv("MY_TEST_KEY", "from-env")
    assert store.resolve_source("env:MY_TEST_KEY") == "from-env"
    # env 未设 → 明确报错
    with pytest.raises(KeyError):
        store.resolve_source("env:NOT_SET_VAR_XYZ")
    # 内联明文/未知源一律拒绝(逼走引用/env)
    with pytest.raises(PermissionError):
        store.resolve_source("just-a-plaintext-value")


def test_encryption_active_when_cryptography_present(tmp_path: Path) -> None:
    store = SecretStore(tmp_path / "secrets")
    ref = store.register("k", "v")
    assert store.encryption_active is True          # cryptography 已装 → 真加密
    assert store.summary(ref).encrypted is True


def test_graceful_fallback_without_cryptography(tmp_path: Path, monkeypatch) -> None:
    """没装 cryptography 时:核心自建部分仍可用,encrypted=False 诚实标注,0o600 保护,仍能往返。"""
    import agent_py_agent.agent.secret_store as ss

    monkeypatch.setattr(ss, "_HAS_CRYPTO", False)
    store = ss.SecretStore(tmp_path / "secrets")
    assert store.encryption_active is False
    ref = store.register("k", "fallback-value")
    assert store.summary(ref).encrypted is False                          # 诚实:未加密
    assert store.resolve_plaintext_for_gateway(ref) == "fallback-value"   # 仍能取回
    assert not (tmp_path / "secrets" / "master.key").exists()             # base64 不需密钥
    assert stat.S_IMODE(os.stat(tmp_path / "secrets" / "store.json").st_mode) == 0o600
