"""管理员口令只用合成输入与临时根验证，不接触真实身份或终端秘密。"""
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from agent_py_agent.agent.user_space import admin_password as passwords
from agent_py_agent.cli import admin_identity_commands as commands

OLD = "Synthetic-Old-42"
NEW = "Synthetic-New-73"
OTHER = "Synthetic-Other-81"


@pytest.fixture
def local_admin(tmp_path, monkeypatch):
    monkeypatch.setattr(commands, "_local_admin_home", lambda _args: tmp_path)
    return tmp_path, Namespace()


def answers(*values):
    pending, prompts = iter(values), []

    def prompt(message):
        prompts.append(message)
        return next(pending)

    return prompt, prompts


def test_formal_set_cannot_overwrite_existing_password_without_old_secret(tmp_path):
    passwords.set_admin_password(tmp_path, OLD)
    before = passwords.admin_password_path(tmp_path).read_bytes()
    with pytest.raises(passwords.AdminPasswordError):
        passwords.set_admin_password(tmp_path, NEW)
    assert passwords.admin_password_path(tmp_path).read_bytes() == before


def test_formal_clear_cannot_delete_existing_password_without_old_secret(tmp_path):
    passwords.set_admin_password(tmp_path, OLD)
    before = passwords.admin_password_path(tmp_path).read_bytes()
    with pytest.raises(passwords.AdminPasswordError):
        passwords.clear_admin_password(tmp_path)
    assert passwords.admin_password_path(tmp_path).read_bytes() == before


def test_create_change_and_clear_use_actual_store(local_admin, capsys):
    home, args = local_admin
    prompt, seen = answers(OLD, OLD)
    assert commands.cmd_admin_password_set(args, prompt=prompt) == 0
    assert len(seen) == 2 and passwords.admin_password_status(home)["configured"]
    prompt, seen = answers(OLD, NEW, NEW)
    assert commands.cmd_admin_password_set(args, prompt=prompt) == 0
    assert len(seen) == 3 and "原" in seen[0]
    assert passwords.verify_admin_password(home, NEW, attempt_key="test:changed").ok
    prompt, seen = answers(NEW)
    assert commands.cmd_admin_password_clear(args, prompt=prompt) == 0
    assert len(seen) == 1 and passwords.admin_password_status(home)["state"] == "missing"
    output = capsys.readouterr()
    assert all(secret not in output.out + output.err for secret in (OLD, NEW))


@pytest.mark.parametrize("operation", ["set", "clear"])
def test_wrong_old_password_preserves_record_and_stops_prompting(local_admin, operation, capsys):
    home, args = local_admin
    passwords.set_admin_password(home, OLD)
    before = passwords.admin_password_path(home).read_bytes()
    prompt, seen = answers("Synthetic-Wrong-55", NEW, NEW)
    command = getattr(commands, f"cmd_admin_password_{operation}")
    assert command(args, prompt=prompt) != 0
    assert len(seen) == 1
    assert passwords.admin_password_path(home).read_bytes() == before
    output = capsys.readouterr()
    assert "Synthetic-Wrong-55" not in output.out + output.err


@pytest.mark.parametrize("raw", [b"null", b"[]", b"not-json", b"\xff", b'{"schema":"admin_password.v1"}'])
@pytest.mark.parametrize("operation", ["set", "clear"])
def test_invalid_records_are_not_treated_as_missing(local_admin, raw, operation):
    home, args = local_admin
    path = passwords.admin_password_path(home)
    path.parent.mkdir()
    path.write_bytes(raw)
    prompt, seen = answers(OLD, NEW, NEW)
    assert getattr(commands, f"cmd_admin_password_{operation}")(args, prompt=prompt) != 0
    assert seen == [] and path.read_bytes() == raw


@pytest.mark.parametrize("operation", ["set", "clear"])
def test_unreadable_records_fail_closed(local_admin, monkeypatch, operation):
    home, args = local_admin
    passwords.set_admin_password(home, OLD)
    path = passwords.admin_password_path(home)
    before = path.read_bytes()

    def denied(*_args, **_kwargs):
        raise PermissionError("synthetic private record cannot be read")

    monkeypatch.setattr(passwords, "read_text_beneath", denied)
    prompt, seen = answers(OLD, NEW, NEW)
    assert getattr(commands, f"cmd_admin_password_{operation}")(args, prompt=prompt) != 0
    assert not seen and path.read_bytes() == before


@pytest.mark.parametrize("operation", ["set", "clear"])
@pytest.mark.parametrize("cancellation", [EOFError, KeyboardInterrupt])
def test_cancelled_prompt_is_read_only(local_admin, operation, cancellation):
    home, args = local_admin
    passwords.set_admin_password(home, OLD)
    before = passwords.admin_password_path(home).read_bytes()

    def cancel(_message):
        raise cancellation()

    assert getattr(commands, f"cmd_admin_password_{operation}")(args, prompt=cancel) != 0
    assert passwords.admin_password_path(home).read_bytes() == before


def test_new_password_confirmation_mismatch_preserves_old_record(local_admin):
    home, args = local_admin
    passwords.set_admin_password(home, OLD)
    before = passwords.admin_password_path(home).read_bytes()
    prompt, _ = answers(OLD, NEW, OTHER)
    assert commands.cmd_admin_password_set(args, prompt=prompt) != 0
    assert passwords.admin_password_path(home).read_bytes() == before


@pytest.mark.parametrize("existing", [False, True])
def test_concurrent_service_calls_have_one_winner(tmp_path, existing):
    if existing:
        passwords.set_admin_password(tmp_path, OLD)
    barrier = Barrier(2)

    def change(secret):
        barrier.wait(timeout=5)
        try:
            kwargs = {"old_password": OLD} if existing else {}
            passwords.set_admin_password(tmp_path, secret, **kwargs)
            return secret
        except passwords.AdminPasswordError:
            return None

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(change, [NEW, OTHER]))
    winners = [secret for secret in results if secret is not None]
    assert len(winners) == 1
    assert passwords.verify_admin_password(tmp_path, winners[0], attempt_key="test:winner").ok


@pytest.mark.parametrize("existing", [False, True])
def test_cli_rechecks_version_after_lock_free_prompts(local_admin, existing):
    home, args = local_admin
    if existing:
        passwords.set_admin_password(home, OLD)
    pending = iter([OLD, NEW, NEW] if existing else [NEW, NEW])
    observed = []

    def prompt(message):
        # 提示期间另一线程必须能走正式服务完成写入，不能持口令锁等待真人输入。
        if "新管理员密码" in message:
            with ThreadPoolExecutor(max_workers=1) as workers:
                kwargs = {"old_password": OLD} if existing else {}
                workers.submit(passwords.set_admin_password, home, OTHER, **kwargs).result(timeout=5)
            observed.append(passwords.admin_password_path(home).read_bytes())
        return next(pending)

    assert commands.cmd_admin_password_set(args, prompt=prompt) != 0
    assert len(observed) == 1 and passwords.admin_password_path(home).read_bytes() == observed[0]


def test_local_changes_share_existing_failure_throttle(tmp_path):
    passwords.set_admin_password(tmp_path, OLD)
    for offset in range(5):
        with pytest.raises(passwords.AdminPasswordError):
            passwords.set_admin_password(tmp_path, NEW, old_password="wrong-old", now=100 + offset)
    with pytest.raises(passwords.AdminPasswordError):
        passwords.clear_admin_password(tmp_path, old_password=OLD, now=105)
    assert passwords.admin_password_status(tmp_path)["configured"]
    assert passwords.clear_admin_password(tmp_path, old_password=OLD, now=800)


@pytest.mark.parametrize("argv", [["admin-password", "set"], ["admin", "password", "set"]])
def test_legacy_and_canonical_command_routes_share_password_check(local_admin, argv, monkeypatch):
    from agent_py_agent.cli.parser import build_parser

    home, _ = local_admin
    passwords.set_admin_password(home, OLD)
    before = passwords.admin_password_path(home).read_bytes()
    args = build_parser().parse_args(argv)
    prompt, seen = answers("wrong-old", NEW, NEW)
    assert args.func is commands.cmd_admin_password_set
    assert args.func(args, prompt=prompt) != 0
    assert len(seen) == 1 and passwords.admin_password_path(home).read_bytes() == before
