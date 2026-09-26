"""管理员密码、失败节流、IM 身份绑定与本机 CLI。

钉住：
1. 密码文件只存 scrypt 参数/盐/派生值，0600、目录 0700，任何文件里都没有明文。
2. 同一渠道身份 10 分钟内错 5 次锁 10 分钟；锁定期内正确密码也拒绝、不加计数；拒绝文案不区分原因。
3. 绑定只按 (channel, user_id) 精确匹配；损坏文件查询 fail-closed、写入不覆盖。
4. CLI 只在 local/main 配置下运行，密码两次输入、不回显、不打印散列。
"""

from __future__ import annotations

import json
import stat
from argparse import Namespace
from pathlib import Path

import pytest

from agent_py_agent.agent.user_space.admin_channel_identity import (
    AdminIdentityStoreError,
    admin_channel_identities_path,
    bind_admin_channel_identity,
    find_admin_channel_identity,
    list_admin_channel_identities,
    parse_admin_channel_identity_key,
    remove_admin_channel_identity,
)
from agent_py_agent.agent.user_space.admin_password import (
    ADMIN_PASSWORD_LOCK_SECONDS,
    AdminPasswordCheck,
    AdminPasswordError,
    admin_password_path,
    admin_password_refusal_message,
    admin_password_status,
    clear_admin_password,
    set_admin_password,
    verify_admin_password,
)
from agent_py_agent.cli import admin_identity_commands

_SECRET = "Correct-Horse-42"
_KEY = "feishu:ou_admin"


def _files_containing(root: Path, needle: str) -> list[Path]:
    hits = []
    for path in root.rglob("*"):
        if path.is_file() and needle.encode("utf-8") in path.read_bytes():
            hits.append(path)
    return hits


def test_password_record_is_private_scrypt_without_plaintext(tmp_path):
    status = set_admin_password(tmp_path, _SECRET, now=1000.0)

    assert status == {"configured": True, "state": "configured", "algorithm": "scrypt", "updated_at": 1000.0}
    path = admin_password_path(tmp_path)
    record = json.loads(path.read_text(encoding="utf-8"))
    assert set(record) == {"schema", "algorithm", "n", "r", "p", "salt", "hash", "updated_at"}
    assert record["schema"] == "admin_password.v1" and record["algorithm"] == "scrypt"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert _files_containing(tmp_path, _SECRET) == []
    assert verify_admin_password(tmp_path, _SECRET, attempt_key=_KEY).ok is True
    assert verify_admin_password(tmp_path, _SECRET + "x", attempt_key=_KEY).ok is False
    # 同一密码再设置会换新盐，散列不同。
    set_admin_password(tmp_path, _SECRET)
    assert json.loads(path.read_text(encoding="utf-8"))["salt"] != record["salt"]


@pytest.mark.parametrize("candidate", ["short7!", " leading-space", "trailing-space ", "has\nnewline1", "tab\tinside1"])
def test_invalid_passwords_are_rejected_without_writing(tmp_path, candidate):
    with pytest.raises(AdminPasswordError):
        set_admin_password(tmp_path, candidate)
    assert not admin_password_path(tmp_path).exists()


def test_five_failures_in_window_lock_identity_and_hide_reason(tmp_path):
    set_admin_password(tmp_path, _SECRET)
    for offset in range(4):
        check = verify_admin_password(tmp_path, "wrong-password", attempt_key=_KEY, now=100.0 + offset)
        assert check == AdminPasswordCheck(False, 0.0)
        assert admin_password_refusal_message(check) == "管理员身份验证未通过。"
    fifth = verify_admin_password(tmp_path, "wrong-password", attempt_key=_KEY, now=104.0)
    assert fifth.ok is False and fifth.retry_after_seconds == pytest.approx(ADMIN_PASSWORD_LOCK_SECONDS)
    assert admin_password_refusal_message(fifth) == "管理员身份验证未通过。请约 10 分钟后再试。"

    locked = verify_admin_password(tmp_path, _SECRET, attempt_key=_KEY, now=400.0)
    assert locked.ok is False and locked.retry_after_seconds == pytest.approx(304.0)
    other = verify_admin_password(tmp_path, _SECRET, attempt_key="feishu:ou_other", now=400.0)
    assert other.ok is True, "锁定只作用于同一渠道身份"
    assert verify_admin_password(tmp_path, _SECRET, attempt_key=_KEY, now=104.0 + ADMIN_PASSWORD_LOCK_SECONDS + 1).ok

    attempts = json.loads((tmp_path / "config" / "admin-password-attempts.json").read_text(encoding="utf-8"))
    assert attempts["schema"] == "admin_password_attempts.v1"
    assert _KEY not in attempts["identities"], "成功后清零"
    assert _files_containing(tmp_path, _SECRET) == [] and _files_containing(tmp_path, "wrong-password") == []


def test_failures_outside_window_do_not_accumulate(tmp_path):
    set_admin_password(tmp_path, _SECRET)
    for moment in (0.0, 200.0, 400.0, 600.5, 800.0):
        assert verify_admin_password(tmp_path, "nope-nope", attempt_key=_KEY, now=moment).retry_after_seconds == 0.0
    assert verify_admin_password(tmp_path, _SECRET, attempt_key=_KEY, now=801.0).ok is True


def test_missing_or_corrupt_password_never_verifies(tmp_path):
    assert admin_password_status(tmp_path) == {"configured": False, "state": "missing"}
    assert verify_admin_password(tmp_path, _SECRET, attempt_key=_KEY).ok is False
    admin_password_path(tmp_path).write_text('{"schema": "admin_password.v1", "algorithm": "none"}', encoding="utf-8")
    assert admin_password_status(tmp_path) == {"configured": False, "state": "invalid"}
    assert verify_admin_password(tmp_path, _SECRET, attempt_key=_KEY).ok is False
    assert clear_admin_password(tmp_path) is True
    assert not admin_password_path(tmp_path).exists() and clear_admin_password(tmp_path) is False


def test_corrupt_attempts_file_fails_closed(tmp_path):
    set_admin_password(tmp_path, _SECRET)
    (tmp_path / "config" / "admin-password-attempts.json").write_text("[]", encoding="utf-8")
    with pytest.raises(AdminPasswordError):
        verify_admin_password(tmp_path, _SECRET, attempt_key=_KEY)


def test_binding_is_exact_private_and_logout_removes_it(tmp_path):
    bound = bind_admin_channel_identity(tmp_path, "Feishu", "ou_admin", now=50.0)
    assert (bound.channel, bound.user_id, bound.key) == ("feishu", "ou_admin", "feishu:ou_admin")
    path = admin_channel_identities_path(tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert find_admin_channel_identity(tmp_path, "feishu", "ou_admin") == bound
    assert find_admin_channel_identity(tmp_path, "feishu", "OU_ADMIN") is None, "用户 ID 大小写敏感"
    assert find_admin_channel_identity(tmp_path, "qq", "ou_admin") is None
    again = bind_admin_channel_identity(tmp_path, "feishu", "ou_admin", now=60.0)
    assert list_admin_channel_identities(tmp_path) == [again]
    assert remove_admin_channel_identity(tmp_path, "feishu", "ou_admin") is True
    assert remove_admin_channel_identity(tmp_path, "feishu", "ou_admin") is False
    assert find_admin_channel_identity(tmp_path, "feishu", "ou_admin") is None
    assert parse_admin_channel_identity_key(" Feishu:ou_x ") == ("feishu", "ou_x")
    with pytest.raises(AdminIdentityStoreError):
        parse_admin_channel_identity_key("ou_without_channel")


def test_corrupt_binding_file_fails_closed_and_is_not_overwritten(tmp_path):
    path = admin_channel_identities_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("not json", encoding="utf-8")
    assert find_admin_channel_identity(tmp_path, "feishu", "ou_admin") is None
    with pytest.raises(AdminIdentityStoreError):
        bind_admin_channel_identity(tmp_path, "feishu", "ou_admin")
    assert path.read_text(encoding="utf-8") == "not json"


def _config(tmp_path: Path, *, owner_provider: str = "local", owner_kind: str = "main") -> Namespace:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    config = tmp_path / "agent_config.yaml"
    config.write_text(
        f"my_agent_home: {home}\nmy_agent_owner_provider: {owner_provider}\nmy_agent_owner_kind: {owner_kind}\n",
        encoding="utf-8",
    )
    return Namespace(config=str(config), json=False, identity="")


def _prompts(*answers: str):
    queue = list(answers)
    seen: list[str] = []

    def prompt(message: str) -> str:
        seen.append(message)
        return queue.pop(0)

    return prompt, seen


def test_cli_sets_password_twice_without_echo_and_status_hides_hash(tmp_path, capsys):
    args = _config(tmp_path)
    home = tmp_path / "home"
    prompt, seen = _prompts(_SECRET, _SECRET + "-typo")
    assert admin_identity_commands.cmd_admin_password_set(args, prompt=prompt) == 2
    assert len(seen) == 2 and not admin_password_path(home).exists()
    prompt, _seen = _prompts("short")
    assert admin_identity_commands.cmd_admin_password_set(args, prompt=prompt) == 2
    prompt, _seen = _prompts(_SECRET, _SECRET)
    assert admin_identity_commands.cmd_admin_password_set(args, prompt=prompt) == 0
    assert verify_admin_password(home, _SECRET, attempt_key=_KEY).ok is True

    bind_admin_channel_identity(home, "feishu", "ou_admin")
    assert admin_identity_commands.cmd_admin_password_status(args) == 0
    assert admin_identity_commands.cmd_admin_identities_list(args) == 0
    out = capsys.readouterr().out
    record = json.loads(admin_password_path(home).read_text(encoding="utf-8"))
    assert "已设置" in out and "feishu:ou_admin" in out
    assert _SECRET not in out and record["hash"] not in out and record["salt"] not in out

    assert admin_identity_commands.cmd_admin_identities_remove(Namespace(**{**vars(args), "identity": "feishu:ou_admin"})) == 0
    assert find_admin_channel_identity(home, "feishu", "ou_admin") is None
    assert admin_identity_commands.cmd_admin_password_clear(args) == 0
    assert not admin_password_path(home).exists()


def test_cli_refuses_non_local_main_configuration(tmp_path, capsys):
    args = _config(tmp_path, owner_provider="feishu", owner_kind="user")
    prompt, seen = _prompts(_SECRET, _SECRET)
    assert admin_identity_commands.cmd_admin_password_set(args, prompt=prompt) == 2
    assert seen == [] and "local/main" in capsys.readouterr().err
    assert not list((tmp_path / "home").rglob("admin-password.json"))


def test_cli_group_without_subcommand_prints_help_instead_of_chat(tmp_path, capsys):
    from agent_py_agent.cli.parser import build_parser

    parser = build_parser()
    args = parser.parse_args(["--config", _config(tmp_path).config, "admin-password"])
    assert args.func(args) == 2
    assert "set" in capsys.readouterr().out
    args = parser.parse_args(["--config", _config(tmp_path).config, "admin-identities", "remove", "feishu:ou_x"])
    assert args.func is admin_identity_commands.cmd_admin_identities_remove
