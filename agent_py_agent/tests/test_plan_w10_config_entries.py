"""W10：只用合成凭据和临时文件验证连接配置事务及唯一来源切换。"""

from __future__ import annotations

import json
from argparse import Namespace
from contextlib import contextmanager
from dataclasses import fields, replace
from hashlib import sha256

import pytest

from agent_py_agent.agent.settings import config_io
from agent_py_agent.agent.settings import parameter_changes as changes
from agent_py_agent.agent.settings.config import AgentConfig, load_config
from agent_py_agent.agent.settings.secret_ref import (
    freeze_managed_secret_environment,
    managed_secret_environment_names,
    resolve_secret_ref,
)
from agent_py_agent.cli import feishu_cmd


@pytest.fixture
def config_files(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_bytes(b'\xef\xbb\xbf# keep\r\nagent_name: "fixture"\r\n'
                     b'feishu_app_id: "old-app"\r\nfeishu_app_secret: "old-marker"\r\n'
                     b'feishu_connection_mode: "webhook"\r\n')
    path.chmod(0o600)
    ledger = changes.ledger_path(path)
    ledger.write_bytes(b'{"id": "before", "key": "agent_name", "value": "fixture"}\r\n')
    return path, ledger


def _connect(path, **overrides):
    args = dict(config=str(path), scan=False, app_id="new-app", app_secret="new-marker",
                domain="feishu", secret_file=None, restart=False)
    args.update(overrides)
    return feishu_cmd.cmd_feishu_connect(Namespace(**args))


def _before(files):
    return tuple(path.read_bytes() for path in files)


def test_connect_commits_one_candidate_and_receipts_use_formal_reader(config_files, monkeypatch, capsys):
    path, ledger = config_files
    writer = changes.write_config_bytes_atomic_unlocked
    publications = []

    def observe(target, content):
        if target == path.resolve():
            publications.append(content)
        return writer(target, content)

    monkeypatch.setattr(changes, "write_config_bytes_atomic_unlocked", observe)
    assert _connect(path, app_id="123456") == 0
    loaded = load_config(path)
    assert (loaded.feishu_app_id, loaded.feishu_app_secret, loaded.feishu_connection_mode) == (
        "123456", "new-marker", "long_connection")
    assert len(publications) == 1
    assert path.read_bytes().startswith(b'\xef\xbb\xbf# keep\r\nagent_name: "fixture"\r\n')
    assert path.stat().st_mode & 0o777 == 0o600
    entries = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert [row["key"] for row in entries[1:]] == [
        "feishu_app_id", "feishu_app_secret", "feishu_connection_mode"]
    output = capsys.readouterr().out
    assert loaded.feishu_app_id in output and "new-marker" not in output
    assert "new-marker" not in ledger.read_text()


@pytest.mark.parametrize("key", ["feishu_app_id", "feishu_app_secret", "feishu_connection_mode"])
def test_connect_duplicate_target_is_rejected_without_partial_writes(config_files, capsys, key):
    path, _ = config_files
    with path.open("ab") as handle:
        handle.write(f'{key}: "duplicate"\r\n'.encode())
    before = _before(config_files)
    assert _connect(path) == 1
    assert "CONFIG" in capsys.readouterr().out
    assert _before(config_files) == before


@pytest.mark.parametrize("bad", ["bad\x00marker", "bad\x01marker", "bad\x1fmarker", "bad\x7fmarker",
                                 "bad\x85marker", "bad\u2028marker", "bad\u2029marker", "bad\ud800marker",
                                 "\tmarker", "marker\r", "bad\nmarker", 'bad"marker'])
def test_connect_rejects_unsafe_characters_before_any_write(config_files, bad, capsys):
    before = _before(config_files)
    assert _connect(config_files[0], app_secret=bad) == 1
    output = capsys.readouterr().out
    assert "PARAMETER_INVALID" in output
    assert "marker" not in output
    assert _before(config_files) == before


@pytest.mark.parametrize("bad", ["bad\x00value", "bad\u2028value", "bad\udfffvalue"])
def test_scalar_parameter_entry_uses_same_character_validation(config_files, bad):
    before = _before(config_files)
    result = changes.set_parameter("agent_name", bad, paths=changes.WritePaths(config_files[0]),
                                   origin=changes.ChangeOrigin("test"))
    assert result["ok"] is False and result["code"] == "PARAMETER_INVALID"
    assert "value" not in result["error"]
    assert _before(config_files) == before


@pytest.mark.parametrize("failure_at", [1, 2, 3])
def test_connect_single_ledger_failure_restores_both_exact_images(config_files, monkeypatch, capsys, failure_at):
    before = _before(config_files)
    record, written = changes._record, []

    def fail_once(*args):
        entry = record(*args)
        written.append(entry)
        if len(written) == failure_at:
            raise OSError("synthetic-private-marker ledger failure")
        return entry

    monkeypatch.setattr(changes, "_record", fail_once)
    assert _connect(config_files[0]) == 1
    assert len(written) == failure_at
    assert _before(config_files) == before
    output = capsys.readouterr().out
    assert "USER_CONFIG_WRITE_FAILED" in output and "synthetic-private-marker" not in output


def test_connect_stale_revision_preserves_config_and_ledger(config_files, capsys):
    before = _before(config_files)
    revision = sha256(before[0]).hexdigest()
    assert _connect(config_files[0], expected_revision=revision + "stale") == 1
    assert "CONFIG_REVISION_CONFLICT" in capsys.readouterr().out
    assert _before(config_files) == before


def test_connect_uses_config_then_ledger_lock_order(config_files, monkeypatch):
    original, acquired = changes.locked_json_path, []

    @contextmanager
    def observe(path):
        with original(path):
            acquired.append(path)
            yield

    monkeypatch.setattr(changes, "locked_json_path", observe)
    assert _connect(config_files[0]) == 0
    assert acquired == [path.resolve() for path in config_files]


def test_connect_failed_formal_readback_restores_both_files(config_files, monkeypatch, capsys):
    path, _ = config_files
    before, effective = _before(config_files), changes._effective

    def inconsistent(target, key):
        actual = effective(target, key)
        return "mismatch" if target == path.resolve() and key == "feishu_app_secret" else actual

    monkeypatch.setattr(changes, "_effective", inconsistent)
    assert _connect(path) == 1
    assert "PARAMETER_NOT_EFFECTIVE" in capsys.readouterr().out
    assert _before(config_files) == before


@pytest.mark.parametrize("source", ["env", "file"])
def test_source_is_prepared_then_only_reference_is_committed(config_files, tmp_path, monkeypatch, capsys, source):
    if source == "env":
        monkeypatch.setenv("W10_NEW_SOURCE", "private-new-marker")
        reference = "env:W10_NEW_SOURCE"
    else:
        target = tmp_path / "new-source"
        target.write_text("private-new-marker\n")
        reference = f"file:{target}"
    assert _connect(config_files[0], app_secret=reference) == 0
    loaded = load_config(config_files[0])
    assert loaded.feishu_app_secret == reference
    assert resolve_secret_ref(loaded.feishu_app_secret) == "private-new-marker"
    assert "private-new-marker" not in config_files[0].read_text()
    assert "private-new-marker" not in config_files[1].read_text()
    assert "private-new-marker" not in capsys.readouterr().out
    if source == "env":
        assert "W10_NEW_SOURCE" in managed_secret_environment_names(loaded)


@pytest.mark.parametrize("source", ["env", "missing_file", "bad_utf8_file", "empty_file"])
def test_unavailable_new_source_is_explicit_noop_without_old_source_fallback(
        config_files, tmp_path, monkeypatch, capsys, source):
    monkeypatch.delenv("W10_MISSING_SOURCE", raising=False)
    target = tmp_path / "unavailable"
    if source == "bad_utf8_file":
        target.write_bytes(b"\xffprivate-marker")
    elif source == "empty_file":
        target.write_text(" \n")
    reference = "env:W10_MISSING_SOURCE" if source == "env" else f"file:{target}"
    before = _before(config_files)
    assert _connect(config_files[0], app_secret=reference) == 1
    output = capsys.readouterr().out
    assert "CONFIG_SOURCE_UNAVAILABLE" in output and "private-marker" not in output
    assert _before(config_files) == before
    assert load_config(config_files[0]).feishu_app_secret == "old-marker"


def test_prepared_reference_transaction_failure_keeps_old_source(config_files, tmp_path, monkeypatch, capsys):
    path, _ = config_files
    old = tmp_path / "old-source"
    old.write_text("private-old-marker")
    config_io.set_simple_yaml_value(path, "feishu_app_secret", f"file:{old}")
    monkeypatch.setenv("W10_NEW_SOURCE", "private-new-marker")
    before = _before(config_files)
    record = changes._record

    def fail_after_record(*args):
        record(*args)
        raise OSError("private-new-marker")

    monkeypatch.setattr(changes, "_record", fail_after_record)
    assert _connect(path, app_secret="env:W10_NEW_SOURCE") == 1
    assert _before(config_files) == before
    assert resolve_secret_ref(load_config(path).feishu_app_secret) == "private-old-marker"
    output = capsys.readouterr().out
    assert "private-new-marker" not in output and "private-old-marker" not in output


def test_parameter_source_switch_also_checks_new_target(config_files, monkeypatch):
    monkeypatch.delenv("W10_MISSING_SOURCE", raising=False)
    before = _before(config_files)
    with changes.cli_deployment_write_scope():
        result = changes.set_parameter("feishu_app_secret", "env:W10_MISSING_SOURCE",
                                       paths=changes.WritePaths(config_files[0]), origin=changes.ChangeOrigin("cli"))
    assert result["ok"] is False and result["code"] == "CONFIG_SOURCE_UNAVAILABLE"
    assert _before(config_files) == before


def test_all_declared_secret_sources_share_one_environment_projection(monkeypatch):
    secret_fields = [item.name for item in fields(AgentConfig) if item.metadata.get("secret_ref")]
    references = {key: f" env: W10_SOURCE_{index} " for index, key in enumerate(secret_fields)}
    expected = {f"W10_SOURCE_{index}" for index in range(len(secret_fields))}
    for name in expected:
        monkeypatch.setenv(name, "private-marker")
    config = AgentConfig(**references, api_key_env="W10_MODEL_SOURCE",
                         config_sources={"api_key": {"source": "env:W10_RESOLVED_SOURCE"}},
                         host_credential_env_names=("W10_OLDER_SOURCE",), system_prompt="env:NOT_A_SOURCE")
    expected |= {"W10_MODEL_SOURCE", "W10_RESOLVED_SOURCE", "W10_OLDER_SOURCE"}
    assert managed_secret_environment_names(config) == frozenset(expected)
    for ref in references.values():
        assert resolve_secret_ref(ref) == "private-marker"
    frozen = freeze_managed_secret_environment(config)
    stripped = replace(frozen, **dict.fromkeys(secret_fields, ""), api_key_env="", config_sources={})
    assert managed_secret_environment_names(stripped) == frozenset(expected)
    assert "private-marker" not in repr(frozen.host_credential_env_names)


@pytest.mark.parametrize("value", ["bad\x00value", "bad\x85value", "bad\u2028value", "bad\ud800value"])
def test_legacy_scalar_writer_refuses_unsafe_text(config_files, value):
    before = _before(config_files)
    with pytest.raises(ValueError):
        config_io.set_simple_yaml_value(config_files[0], "feishu_app_id", value)
    assert _before(config_files) == before


def test_legacy_scalar_writer_rejects_duplicate_target(config_files):
    path, _ = config_files
    with path.open("ab") as handle:
        handle.write(b'feishu_app_id: "duplicate"\r\n')
    before = _before(config_files)
    with pytest.raises(ValueError):
        config_io.set_simple_yaml_value(path, "feishu_app_id", "new-app")
    assert _before(config_files) == before


def test_connect_rechecks_cas_after_waiting_for_ledger(config_files, monkeypatch, capsys):
    path, ledger = config_files
    original_lock = changes.locked_json_path
    ledger_before = ledger.read_bytes()
    newer = b'agent_name: "concurrent-writer"\n'

    @contextmanager
    def change_while_waiting(target):
        with original_lock(target):
            if target == ledger.resolve():
                path.write_bytes(newer)
            yield

    monkeypatch.setattr(changes, "locked_json_path", change_while_waiting)
    assert _connect(path) == 1
    assert "CONFIG_REVISION_CONFLICT" in capsys.readouterr().out
    assert path.read_bytes() == newer and ledger.read_bytes() == ledger_before


def test_source_disappearing_after_precheck_restores_exact_images(config_files, monkeypatch, capsys):
    path, _ = config_files
    monkeypatch.setenv("W10_TEMPORARY_SOURCE", "private-marker")
    before, writer = _before(config_files), changes.write_config_bytes_atomic_unlocked

    def disappear(target, content):
        result = writer(target, content)
        if target == path.resolve():
            monkeypatch.delenv("W10_TEMPORARY_SOURCE", raising=False)
        return result

    monkeypatch.setattr(changes, "write_config_bytes_atomic_unlocked", disappear)
    assert _connect(path, app_secret="env:W10_TEMPORARY_SOURCE") == 1
    assert "CONFIG_SOURCE_UNAVAILABLE" in capsys.readouterr().out
    assert _before(config_files) == before


def test_new_ledger_is_removed_if_first_append_fails(config_files, monkeypatch):
    path, ledger = config_files
    ledger.unlink()
    before, record = path.read_bytes(), changes._record

    def fail_after_append(*args):
        record(*args)
        raise OSError("synthetic append acknowledgement lost")

    monkeypatch.setattr(changes, "_record", fail_after_append)
    assert _connect(path) == 1
    assert path.read_bytes() == before and not ledger.exists()


def test_batch_service_does_not_accept_actor_as_cli_authority(config_files):
    before = _before(config_files)
    result = changes.set_feishu_connection("new-app", "private-marker", paths=changes.WritePaths(config_files[0]),
                                           origin=changes.ChangeOrigin("cli"))
    assert result["ok"] is False and result["code"] == "PARAMETER_SOURCE_READ_ONLY"
    assert _before(config_files) == before


def test_optional_export_uses_loaded_values_not_raw_refs(config_files, tmp_path, monkeypatch):
    monkeypatch.setenv("W10_EXPORT_SOURCE", "private-export-marker")
    exported = tmp_path / "feishu.env"
    assert _connect(config_files[0], app_id="  new-app  ", app_secret="env:W10_EXPORT_SOURCE",
                    secret_file=str(exported)) == 0
    assert exported.read_text() == 'FEISHU_APP_ID="new-app"\nFEISHU_APP_SECRET="private-export-marker"\n'
    assert exported.stat().st_mode & 0o777 == 0o600


def test_optional_export_failure_is_explicit_and_does_not_leak_values(config_files, monkeypatch, capsys):
    def fail(*args):
        raise OSError("private-export-marker")

    monkeypatch.setattr(feishu_cmd, "_write_secret_env", fail)
    assert _connect(config_files[0], secret_file="synthetic.env") == 1
    output = capsys.readouterr().out
    assert "SECRET_EXPORT_FAILED" in output and "private-export-marker" not in output
    assert "配置已提交" in output


def test_registration_error_does_not_print_sensitive_exception(config_files, monkeypatch, capsys):
    before = _before(config_files)

    def fail(**kwargs):
        raise RuntimeError("private-registration-marker")

    monkeypatch.setattr(feishu_cmd, "register_feishu_app_by_scan", fail)
    assert _connect(config_files[0], scan=True) == 1
    output = capsys.readouterr().out
    assert "FEISHU_REGISTRATION_FAILED" in output and "private-registration-marker" not in output
    assert _before(config_files) == before


@pytest.mark.parametrize("target_index", [0, 1])
@pytest.mark.parametrize("alias_kind", ["direct", "symlink", "hardlink"])
def test_optional_export_cannot_overwrite_transaction_files(config_files, tmp_path, monkeypatch, capsys, target_index, alias_kind):
    target = config_files[target_index]
    if alias_kind != "direct":
        alias = tmp_path / "export-alias"
        if alias_kind == "symlink":
            alias.symlink_to(target)
        else:
            # 当前测试执行环境明确拒绝 os.link；固定同 inode 的只读身份输入，断言仍要求事务前拒绝。
            from pathlib import Path

            alias.write_text("fixture alias")
            samefile, protected = Path.samefile, target
            monkeypatch.setattr(Path, "samefile", lambda self, other: (
                True if self == alias and other == protected else samefile(self, other)))
        target = alias
    before = _before(config_files)
    assert _connect(config_files[0], secret_file=str(target)) == 1
    assert "CONFIG_PATH_INVALID" in capsys.readouterr().out
    assert _before(config_files) == before


def test_config_replaced_with_symlink_before_resolve_is_rejected(config_files, tmp_path, monkeypatch, capsys):
    from pathlib import Path

    path, ledger = config_files
    victim = tmp_path / "other-config.yaml"
    victim.write_bytes(path.read_bytes())
    victim_before, ledger_before = victim.read_bytes(), ledger.read_bytes()
    is_symlink, swapped = Path.is_symlink, []

    def replace_after_check(target):
        answer = is_symlink(target)
        if target == path and not swapped:
            swapped.append(True)
            path.unlink()
            path.symlink_to(victim)
        return answer

    monkeypatch.setattr(Path, "is_symlink", replace_after_check)
    assert _connect(path) == 1
    assert "CONFIG_PATH_INVALID" in capsys.readouterr().out
    assert victim.read_bytes() == victim_before and ledger.read_bytes() == ledger_before


def test_export_is_private_before_atomic_publication(tmp_path, monkeypatch):
    from pathlib import Path

    target = tmp_path / "export.env"
    target.write_text("old")
    target.chmod(0o644)
    replace_file, modes = feishu_cmd.os.replace, []

    def observe(source, destination):
        if Path(destination) == target:
            modes.append(Path(source).stat().st_mode & 0o777)
        return replace_file(source, destination)

    monkeypatch.setattr(feishu_cmd.os, "replace", observe)
    feishu_cmd._write_secret_env(str(target), "new-app", "private-marker")
    assert modes == [0o600] and target.stat().st_mode & 0o777 == 0o600


def test_restricted_writer_is_rejected_before_scan(config_files, monkeypatch, capsys):
    from agent_py_agent.agent.common import global_write_guard as guard

    before, called = _before(config_files), []
    monkeypatch.setattr(guard, "_restricted_writer", lambda: object())
    monkeypatch.setattr(feishu_cmd, "register_feishu_app_by_scan", lambda **kwargs: called.append(True))
    assert _connect(config_files[0], scan=True) == 1
    assert not called and _before(config_files) == before
    assert "GLOBAL_WRITE_IN_WORKER" in capsys.readouterr().out


def test_structured_json_whitespace_keeps_existing_writer_contract(config_files):
    result = changes.set_parameter("tool_catalog_categories", '[\n  "core",\n  "plugins"\n]',
                                   paths=changes.WritePaths(config_files[0]), origin=changes.ChangeOrigin("test"))
    assert result["ok"] is True
    assert load_config(config_files[0]).tool_catalog_categories == ["core", "plugins"]


def test_legacy_scalar_writer_preserves_old_text_return_value(tmp_path):
    path = tmp_path / "legacy.yaml"
    path.write_text('feishu_app_id: "old-app" # previous comment\n')
    old, new_line = config_io.set_simple_yaml_value(path, "feishu_app_id", "new-app")
    assert old == '"old-app" # previous comment'
    assert new_line == 'feishu_app_id: "new-app"'
