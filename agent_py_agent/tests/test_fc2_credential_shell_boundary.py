"""FC2 (C4/C5/C6) 携卡命令/固化脚本隔离边界、全切点跨块净化与出站扣留测试（零原始秘密进测试输出）。"""
from __future__ import annotations

import base64
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import threading
import time
import uuid
from dataclasses import replace  # noqa: F401
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, quote_plus

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams  # noqa: F401
from agent_py_agent.agent.agent_core.tool_runtime_ledger import write_boundary_with_runtime_ledger
from agent_py_agent.agent.artifacts.registry import ArtifactRegistration, register_artifact
from agent_py_agent.agent.capability.channel_message_tool import SendMessageTool
from agent_py_agent.agent.common.json_io import read_json_object_report
from agent_py_agent.agent.common.log_redaction import install_log_redaction, redact_sensitive_value
from agent_py_agent.agent.conversation.background_delivery import (
    BackgroundDeliveryDependencies,
    cached_owner_delivery,
    frozen_owner_delivery,
    record_background_response,
)
from agent_py_agent.agent.delivery import (
    ChannelAdapterRegistry,
    ChannelCapabilities,
    DeliveryContext,
    DeliveryService,
)
from agent_py_agent.agent.gateway_parts.http_handlers import _read_public_progress_events
from agent_py_agent.agent.gateway_parts.private_interaction_service import (
    resolve_private_interaction,
)
from agent_py_agent.agent.gateway_parts.stream_writer import BufferedChunkStreamWriter
from agent_py_agent.agent.tooling.host_output_redaction import (
    HostOutputRedaction,
    HostOutputStreamRedactor,
    credential_echo_variants,
    turn_credential_redaction,
)
from agent_py_agent.agent.tooling.sandbox import (
    SandboxSpec,
    SandboxUnavailable,
    _reset_bwrap_binary_probe_state,
    build_bwrap_argv,
)
from agent_py_agent.agent.tooling.scripts_tool import ScriptsTool
from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions
from agent_py_agent.agent.user_space import task_credential_material as cred_mat_mod
from agent_py_agent.agent.user_space import task_credentials as task_cred_mod
from agent_py_agent.agent.user_space.task_credential_material import (
    ChildCredentialFile,  # noqa: F401
    credential_file_for_child,
)
from agent_py_agent.agent.user_space.task_credentials import (
    CredentialCardOptions,
    CredentialMaterial,
    credential_path,
    request_credential_card,
)
from agent_py_agent.agent.workflows.run_artifacts import bind_run_out_target
from agent_py_agent.agent.workflows.script_runner import ScriptRunner
from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call
from agent_py_agent.tests.golden.replayer import _shell_python
from agent_py_agent.tests.test_im_progress_draft import Adapter
from agent_py_agent.tests.test_ingestion_task_credential_scope import _active, _agent


def _open_fd_count() -> int:
    try:
        return len(os.listdir("/dev/fd"))
    except OSError:
        return sum(1 for fd in range(256) if _fd_alive(fd))


def _fd_alive(fd: int) -> bool:
    try:
        os.fstat(fd)
        return True
    except OSError:
        return False


def _child_pids_of_self() -> set[int]:
    proc = subprocess.Popen(
        ["ps", "-axo", "pid=,ppid="],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout_text, _ = proc.communicate(timeout=5)
    me = os.getpid()
    pids: set[int] = set()
    for line in (stdout_text or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit() and int(parts[1]) == me:
            pid = int(parts[0])
            if pid != proc.pid:
                pids.add(pid)
    return pids


def _assert_zero_secret_hits(text_or_obj: object, raw_values: tuple[str, ...]) -> None:
    rendered = text_or_obj if isinstance(text_or_obj, str) else json.dumps(text_or_obj, ensure_ascii=False, default=str)
    all_forms = credential_echo_variants(raw_values)
    hit_count = sum(1 for variant in all_forms if variant and variant in rendered)
    assert hit_count == 0, f"expected 0 credential variant hits, observed hit_count={hit_count}"


def _extract_json_report(text: str) -> dict[str, object]:
    matched = re.search(r"\{[^\n]*\}", text)
    assert matched is not None, f"missing JSON report in output: {text!r}"
    return json.loads(matched.group(0))


def _submit_custom_card(
    agent,
    url: str,
    *,
    fields_spec: list[dict[str, str]],
    submitted_values: dict[str, str],
    authentication: str = "collected",
    collection_mode: str = "username_password",
) -> str:
    agent.config.feishu_session_lock_enabled = False
    agent.channel_registry.register_adapter(
        "feishu",
        SimpleNamespace(
            send_control_card=lambda *_a, **_k: {
                "message_id": "om_" + uuid.uuid4().hex,
                "chat_id": agent.config.my_agent_owner_id if agent.home_paths.owner_kind == "group" else "oc_dm_fc2",
            }
        ),
        capabilities=ChannelCapabilities(control_cards=True, proactive=True),
    )
    ref, _ = request_credential_card(
        agent,
        url=url,
        authentication=authentication,
        options=CredentialCardOptions(
            fields=fields_spec,
            collection_mode=collection_mode,
            access_scope="task",
        ),
    )
    row = read_json_object_report(credential_path(agent.home_paths.owner_home_dir, ref), context="fc2.card").payload
    submit_body = {
        "reference": row["reference"],
        "decision": "submit",
        "message_id": row["message_id"],
        "chat_id": row["chat_id"],
        "fields": dict(submitted_values),
    }
    if row.get("presentation", {}).get("kind") == "group":
        submit_body["submitter"] = {
            "actor_id": row["actor_id"],
            "chat_id": row["chat_id"],
            "current_group_member": True,
        }
    res = resolve_private_interaction(agent, submit_body, channel="feishu", actor_id=row["actor_id"])
    assert res["ok"] is True
    return ref


def _run_formal_tool(agent, tool_name: str, arguments: dict[str, object]):
    current = agent._current_run_params
    boundary = write_boundary_with_runtime_ledger(agent, current)
    return execute_registry_test_call(
        agent.tools,
        tool_name,
        arguments,
        run_id=str(current.run_id),
        call_id="call-" + uuid.uuid4().hex[:12],
        attempt_id=str(getattr(current, "attempt_id", "") or "test-attempt"),
        write_boundary=boundary,
        register_with=agent,
    )


def test_real_seatbelt_subprocess_reads_verbatim_unicode_custom_fields_and_enforces_readonly(tmp_path):
    install_log_redaction()
    agent = _agent(tmp_path, "user", full=False)
    _active(agent)
    field_values = {
        "username": "  张 三 · Café User  ",
        "secret": "S3cr3t!\"\\/\t@#%&+==✨_FC2",
        "tenant_code": " 租户_A/99+01 ",
    }
    ref = _submit_custom_card(
        agent,
        "https://portal.example.test:8443/account/signin",
        fields_spec=[
            {"name": "username", "label": "用户名"},
            {"name": "secret", "label": "密码"},
            {"name": "tenant_code", "label": "租户编码"},
        ],
        submitted_values=field_values,
    )
    expected_hashes = {
        k: base64.b16encode(v.encode("utf-8")).decode("ascii")
        for k, v in field_values.items()
    }
    ws_dir = agent.tools.workspace_root
    ws_dir.mkdir(parents=True, exist_ok=True)
    probe_script = ws_dir / "verify_cred_fd.py"
    probe_script.write_text(
        "import base64, json, os, stat, sys\n"
        "from pathlib import Path\n"
        f"expected = {json.dumps(expected_hashes)}\n"
        "cred_path = os.environ.get('MY_AGENT_CREDENTIAL_FILE', '')\n"
        "with open(cred_path, encoding='utf-8') as fh:\n"
        "    st = os.fstat(fh.fileno())\n"
        "    loaded = json.load(fh)\n"
        "vals = loaded.get('values', {})\n"
        "all_equal = set(vals) == set(expected) and all(\n"
        "    base64.b16encode(vals[k].encode('utf-8')).decode('ascii') == expected[k] for k in expected\n"
        ")\n"
        "write_blocked = False\n"
        "try:\n"
        "    open(cred_path, 'w', encoding='utf-8')\n"
        "except OSError:\n"
        "    write_blocked = True\n"
        "ws_ro = False\n"
        "try:\n"
        "    (Path.cwd() / 'cookie_jar_should_fail.txt').write_text('x', encoding='utf-8')\n"
        "except OSError:\n"
        "    ws_ro = True\n"
        "scratch_ok = False\n"
        "scratch_file = Path(os.environ['TMPDIR']) / 'ephemeral_session.txt'\n"
        "scratch_file.write_text('ephemeral', encoding='utf-8')\n"
        "scratch_ok = scratch_file.read_text(encoding='utf-8') == 'ephemeral'\n"
        "env_split_absent = not any(k in os.environ for k in ('USERNAME', 'SECRET', 'TENANT_CODE', 'MY_AGENT_SECRET'))\n"
        "print(json.dumps({\n"
        "    'all_fields_equal': all_equal,\n"
        "    'origin_ok': loaded.get('origin') == 'https://portal.example.test:8443',\n"
        "    'target_url_ok': loaded.get('target_url') == 'https://portal.example.test:8443/account/signin',\n"
        "    'ref_ok': loaded.get('credential_ref') == " + repr(ref) + ",\n"
        "    'mode_0600': stat.S_IMODE(st.st_mode) == 0o600,\n"
        "    'nlink_zero': st.st_nlink == 0,\n"
        "    'write_blocked': write_blocked,\n"
        "    'workspace_ro': ws_ro,\n"
        "    'scratch_writable': scratch_ok,\n"
        "    'env_split_absent': env_split_absent,\n"
        "}))\n",
        encoding="utf-8",
    )

    tmpdir_path = Path(os.environ.get("TMPDIR") or "/tmp")
    tmpdir_before = set(os.listdir(tmpdir_path))
    ws_before = set(os.listdir(ws_dir))
    fd_before = _open_fd_count()
    children_before = _child_pids_of_self()

    outcome = _run_formal_tool(
        agent,
        "run_command",
        {
            "command": f"{shlex.quote(_shell_python())} {shlex.quote(str(probe_script))}",
            "credential_ref": ref,
        },
    )
    fd_after = _open_fd_count()
    children_after = _child_pids_of_self()
    tmpdir_after = set(os.listdir(tmpdir_path))
    ws_after = set(os.listdir(ws_dir))

    assert outcome.ok is True, outcome.output
    report = _extract_json_report(outcome.output)
    assert report == {
        "all_fields_equal": True,
        "origin_ok": True,
        "target_url_ok": True,
        "ref_ok": True,
        "mode_0600": True,
        "nlink_zero": True,
        "write_blocked": True,
        "workspace_ro": True,
        "scratch_writable": True,
        "env_split_absent": True,
    }
    envelope_dict = outcome.metadata.get("handler_details") or outcome.metadata
    telemetry = envelope_dict["credential_execution"]
    assert telemetry["cleanup_confirmed"] is True
    assert telemetry["fd_closed"] is True
    assert telemetry["scratch_cleaned"] is True
    assert telemetry["process_tree_reaped"] is True
    assert fd_after - fd_before == 0
    assert children_after - children_before == set()
    assert tmpdir_after == tmpdir_before
    assert ws_after == ws_before
    raw_tuple = (*field_values.values(), f"{field_values['username']}:{field_values['secret']}")
    _assert_zero_secret_hits(outcome.output, raw_tuple)
    _assert_zero_secret_hits(outcome.metadata, raw_tuple)
    turn_credential_redaction(agent, agent._current_run_params).close()


def test_prestart_validation_and_mock_sandbox_reject_before_decryption_or_file_creation(tmp_path, monkeypatch):
    agent = _agent(tmp_path, "user", full=False)
    _active(agent)
    valid_ref = _submit_custom_card(
        agent,
        "https://api.example.test/v1",
        fields_spec=[{"name": "secret", "label": "Token"}],
        submitted_values={"secret": "SYNTHETIC_PRESTART_TOKEN_8821"},
        authentication="bearer",
        collection_mode="password",
    )
    decrypt_calls = 0
    file_calls = 0
    orig_cipher = task_cred_mod._cipher
    orig_cf = cred_mat_mod.credential_file_for_child

    def spy_cipher(*args, **kwargs):
        nonlocal decrypt_calls
        decrypt_calls += 1
        return orig_cipher(*args, **kwargs)

    def spy_cf(*args, **kwargs):
        nonlocal file_calls
        file_calls += 1
        return orig_cf(*args, **kwargs)

    monkeypatch.setattr(task_cred_mod, "_cipher", spy_cipher)
    monkeypatch.setattr(cred_mat_mod, "credential_file_for_child", spy_cf)

    # 1. Empty / malformed credential_ref -> TOOL_INVALID_ARGUMENTS / not_started
    for bad_ref in ("", "not-a-cred", "cred_UPPERCASE0123456789abcdef0123"):
        bad_out = ShellTool(tmp_path).execute({"command": "echo ok", "credential_ref": bad_ref})
        assert not bad_out.ok
        assert bad_out.error_code == "TOOL_INVALID_ARGUMENTS"
        assert bad_out.failure_stage == "validation"
        assert bad_out.effect_outcome == "not_started"
        assert bad_out.next_action

    # 2. Background run with credential_ref -> TOOL_INVALID_ARGUMENTS / not_started
    bg_out = _run_formal_tool(
        agent,
        "run_command",
        {"command": "python3 -c 'print(1)'", "run_in_background": True, "credential_ref": valid_ref},
    )
    assert not bg_out.ok
    assert bg_out.error_code == "TOOL_INVALID_ARGUMENTS"
    assert bg_out.effect_outcome == "not_started"

    # 3. Missing host credential_use context -> CREDENTIAL_NOT_AUTHORIZED / not_started
    no_ctx_out = ShellTool(tmp_path).execute({"command": "echo ok", "credential_ref": valid_ref})
    assert not no_ctx_out.ok
    assert no_ctx_out.error_code == "CREDENTIAL_NOT_AUTHORIZED"
    assert no_ctx_out.failure_stage == "authorization"
    assert no_ctx_out.effect_outcome == "not_started"

    # 4. Custom executor or mock sandbox_factory without OS FD isolation proof -> SANDBOX_UNAVAILABLE / not_started
    agent.tools.workspace_root.mkdir(parents=True, exist_ok=True)
    custom_exec_tool = ShellTool(
        agent.tools.workspace_root,
        options=ShellToolOptions(owner_scope_root=str(agent.home_paths.owner_home_dir)),
        executor=SimpleNamespace(run=lambda _req: None),
    )
    ctx = SimpleNamespace(
        credential_use=write_boundary_with_runtime_ledger(agent, agent._current_run_params)["_host_output_redaction"]._credential_use.begin_invocation(HostOutputRedaction()),
        execution_authority_check=None,
        cancellation_token=None,
    )
    exec_out = custom_exec_tool.execute({"command": "echo ok", "credential_ref": valid_ref}, context=ctx)
    assert not exec_out.ok and exec_out.error_code == "SANDBOX_UNAVAILABLE" and exec_out.effect_outcome == "not_started"

    class _MockSandbox:
        def probe(self, *, binary_only: bool = False):
            _ = binary_only
            return SimpleNamespace(ready=True, code="OK", detail="mock")

        def require_ready(self):
            return self.probe()

        def build_argv(self, cmd):
            return cmd

    mock_sbx_tool = ShellTool(
        agent.tools.workspace_root,
        options=ShellToolOptions(owner_scope_root=str(agent.home_paths.owner_home_dir)),
        sandbox_factory=lambda _spec: _MockSandbox(),
    )
    mock_out = mock_sbx_tool.execute({"command": "echo ok", "credential_ref": valid_ref}, context=ctx)
    assert not mock_out.ok and mock_out.error_code == "SANDBOX_UNAVAILABLE" and mock_out.effect_outcome == "not_started"
    ctx.credential_use.disarm()

    assert decrypt_calls == 0
    assert file_calls == 0


def test_linux_bwrap_credential_mount_argv_and_pass_fds_contract(tmp_path):
    _reset_bwrap_binary_probe_state()
    good_bwrap = tmp_path / "bwrap-good"
    good_bwrap.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'bubblewrap 0.8.0'; exit 0; fi\n"
        "if [ \"$1\" = \"--help\" ]; then echo 'usage: --perms OCTAL --ro-bind-data FD DEST'; exit 0; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    good_bwrap.chmod(0o755)

    old_bwrap = tmp_path / "bwrap-old"
    old_bwrap.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'bubblewrap 0.1.0'; exit 0; fi\n"
        "if [ \"$1\" = \"--help\" ]; then echo 'usage: --ro-bind SRC DEST'; exit 0; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    old_bwrap.chmod(0o755)

    owner_dir = tmp_path / "owner"
    ws_dir = owner_dir / "work"
    sys_bin = tmp_path / "sys_bin"
    ws_dir.mkdir(parents=True)
    sys_bin.mkdir(parents=True)

    mat = CredentialMaterial(
        credential_ref="cred_" + "b" * 32,
        generation=2,
        origin="https://linux.example.test",
        target_url="https://linux.example.test/api",
        values={"secret": "SYNTHETIC_LINUX_FD_VAL"},
    )
    with credential_file_for_child(mat, sandbox_backend="linux") as cf:
        assert cf.env_path == "/dev/shm/.my-agent-credential/credential.json"
        assert cf.child_mount == "/dev/shm/.my-agent-credential/credential.json"
        assert os.fstat(cf.read_fd).st_nlink == 0
        assert stat.S_IMODE(os.fstat(cf.read_fd).st_mode) == 0o600

        for mode_kwargs in (
            {"write_roots": (ws_dir,)},
            {"read_only_root": True, "write_roots": (ws_dir,)},
            {"read_only_root": True, "read_mode": "allowlist", "system_read_roots": (sys_bin,), "write_roots": (ws_dir,)},
            {"full_access": True},
        ):
            argv = build_bwrap_argv(
                SandboxSpec(
                    owner_home=owner_dir,
                    workspace=ws_dir,
                    bwrap_path=str(good_bwrap),
                    credential_file=cf,
                    **mode_kwargs,
                )
            )
            idx = argv.index("--ro-bind-data")
            assert argv[idx - 2 : idx] == ["--perms", "0600"]
            assert argv[idx + 1 : idx + 5] == [
                str(cf.read_fd),
                "/dev/shm/.my-agent-credential/credential.json",
                "--remount-ro",
                "/dev/shm/.my-agent-credential",
            ]
            assert not any(".my-agent-cred-stage" in token for token in argv)

        with pytest.raises(SandboxUnavailable, match="BWRAP_CREDENTIAL_BIND_UNSUPPORTED"):
            build_bwrap_argv(
                SandboxSpec(
                    owner_home=owner_dir,
                    workspace=ws_dir,
                    bwrap_path=str(old_bwrap),
                    credential_file=cf,
                )
            )
    assert cf.fd_closed is True


def test_stdout_and_stderr_all_byte_split_points_invalid_utf8_overflow_and_truncation():
    user_val = "张伟_user+01"
    sec_val = "K9#mP!2x$vQ_秘密值"
    custom_val = "TENANT/αβγ=42"
    basic_combo = f"{user_val}:{sec_val}"
    raw_secrets = (user_val, sec_val, custom_val, basic_combo)
    redaction = HostOutputRedaction().with_credential_values(raw_secrets)
    secret_tuple = redaction.snapshot.secret_values

    # Verify all required deterministic encoding variants are generated sorted longest-first
    variants = credential_echo_variants(raw_secrets)
    assert list(variants) == sorted(variants, key=len, reverse=True)
    for item in (
        sec_val,
        json.dumps(sec_val, ensure_ascii=True)[1:-1],
        quote(sec_val, safe=""),
        quote_plus("a b+秘密", safe="") if False else quote_plus(sec_val, safe=""),
        base64.b64encode(sec_val.encode("utf-8")).decode("ascii"),
        base64.b64encode(sec_val.encode("utf-8")).decode("ascii").rstrip("="),
        base64.urlsafe_b64encode(sec_val.encode("utf-8")).decode("ascii").rstrip("="),
        base64.b64encode(basic_combo.encode("utf-8")).decode("ascii"),
    ):
        assert item in variants

    # Exhaustive single-split & 3-byte chunking across stdout and stderr with invalid UTF-8 bytes
    sample_stream = (
        b"hdr:\xff\xfe "
        + sec_val.encode("utf-8")
        + b" | b64="
        + base64.b64encode(sec_val.encode("utf-8")).rstrip(b"=")
        + b" | pct="
        + quote(user_val, safe="").lower().encode("ascii")
        + b" | basic=Basic "
        + base64.b64encode(basic_combo.encode("utf-8"))
        + b" \x80\n"
    )
    for split_at in range(len(sample_stream) + 1):
        for _stream_name in ("stdout", "stderr"):
            redactor = HostOutputStreamRedactor(secret_values=secret_tuple)
            out_bytes = redactor.feed(sample_stream[:split_at]) + redactor.feed(sample_stream[split_at:]) + redactor.finish()
            decoded = out_bytes.decode("utf-8", errors="replace")
            _assert_zero_secret_hits(decoded, raw_secrets)
            assert "[REDACTED]" in decoded

    # Overlong > 65536 byte line overflow cannot expose embedded secret
    overlong = b"X" * 40000 + sec_val.encode("utf-8") + b"Y" * 40000 + b"\n"
    ov_redactor = HostOutputStreamRedactor(secret_values=secret_tuple)
    ov_out = (ov_redactor.feed(overlong[:40010]) + ov_redactor.feed(overlong[40010:]) + ov_redactor.finish()).decode("utf-8", "replace")
    _assert_zero_secret_hits(ov_out, raw_secrets)

    # JSON dynamic keys redacted while protocol control keys remain intact
    with cred_mat_mod.contextlib.ExitStack() as stack:
        from agent_py_agent.agent.common.log_redaction import configured_secret_redaction

        stack.enter_context(configured_secret_redaction(secret_tuple))
        cleaned_obj = redact_sensitive_value(
            {
                "status": "ok",
                "error_code": "NONE",
                f"dyn_{sec_val}": f"val_{custom_val}",
                "nested": {user_val: base64.b64encode(sec_val.encode("utf-8")).decode("ascii")},
            }
        )
    assert cleaned_obj["status"] == "ok"
    assert cleaned_obj["error_code"] == "NONE"
    _assert_zero_secret_hits(cleaned_obj, raw_secrets)


def test_stream_writer_sse_reset_reconnect_and_concurrent_owner_isolation(tmp_path):
    agent_a = _agent(tmp_path / "a", "user", full=False)
    agent_b = _agent(tmp_path / "b", "user", full=False)
    _active(agent_a)
    _active(agent_b)

    secret_a = "SYNTHETIC_STREAM_ALPHA_Secret_7741"
    secret_b = "SYNTHETIC_STREAM_BETA_Secret_9923"

    turn_a = turn_credential_redaction(agent_a, agent_a._current_run_params)
    turn_b = turn_credential_redaction(agent_b, agent_b._current_run_params)
    turn_a.register_values((secret_a,))
    turn_b.register_values((secret_b,))

    live_events_a: list[dict[str, object]] = []
    writer_a = BufferedChunkStreamWriter(
        tmp_path / "a_chunks.jsonl",
        delivery_channel="feishu",
        rich_transcript=True,
        transcript_sink=live_events_a.append,
    )
    writer_a.bind_credential_redaction(turn_a)
    writer_a.set_verbose_level("stream")

    # Feed partial prefix of secret_a into model stream & thinking delta, then reset on retry
    bound_call_1 = writer_a.bind_model_call("mc-1")
    bound_call_1("准备连接接口：前缀 ")
    bound_call_1(secret_a[:14])
    writer_a.write_thinking_delta("思考片段 " + secret_a[:12])
    writer_a.write_provider_retry(scope="turn", attempt=1, total=3, delay_seconds=0.1, error_type="TimeoutError")

    # Second model call completes with split secret_a across chunks
    bound_call_2 = writer_a.bind_model_call("mc-2")
    bound_call_2("查询完成，回显测试：" + secret_a[:10])
    bound_call_2(secret_a[10:] + " 正常记录。")
    writer_a.write_thinking_delta(secret_a[12:] + " 思考完成。\n")
    writer_a.commit_model_response(
        {
            "schema_version": "committed_model_response.v1",
            "model_call_id": "mc-2",
            "boundary": "final",
            "final_text": f"最终结论：{secret_a} 已净化，业务单号 ORD-100。",
        }
    )
    writer_a.close()

    chunk_raw_a = writer_a.chunk_path.read_text(encoding="utf-8")
    sse_replay_a, _ = _read_public_progress_events(writer_a.chunk_path, 0, channel="feishu")
    _assert_zero_secret_hits(chunk_raw_a, (secret_a,))
    _assert_zero_secret_hits(live_events_a, (secret_a,))
    _assert_zero_secret_hits(sse_replay_a, (secret_a,))
    assert secret_a[:14] not in chunk_raw_a

    # Owner B's secret never contaminates Owner A, and vice versa
    assert turn_a.public_text("普通文本") == "普通文本"
    assert "[REDACTED]" in turn_b.public_text(f"B:{secret_b}")
    turn_a.close()
    turn_b.close()


def test_store_messages_explicit_send_background_delivery_forbidden_attachments_and_restart_replay(tmp_path):
    agent = _agent(tmp_path, "user", full=False)
    thread, actor, _claim = _active(agent)
    secret_tok = "SYNTHETIC_FEISHU_OUTBOUND_TOK_5531"

    adapter = Adapter()
    adapter.supports_frozen_attachment_content = True
    adapter.send_attachment_content = lambda target, item, **_k: (adapter.finals.append((target, item.attachment.name)), True)[1]
    registry = ChannelAdapterRegistry()
    registry.register_adapter(
        "feishu",
        adapter,
        capabilities=ChannelCapabilities(
            proactive=True,
            edit_progress=True,
            files=True,
            frozen_attachment_content=True,
        ),
    )
    service = DeliveryService(registry)
    agent.delivery_service = service
    agent.channel_registry = registry

    turn_red = turn_credential_redaction(agent, agent._current_run_params)
    turn_red.register_values((secret_tok,))

    # 1. MessageStore append & append_once scrub content & metadata before dedupe check
    dedupe_req = {
        "thread_id": thread.thread_id,
        "role": "assistant",
        "content": f"记录摘要（含敏感回显 {secret_tok}）",
        "channel": "feishu",
        "metadata": {
            "conversation_request_id": agent._current_run_params.request_id,
            "attachment_label": f"report_{secret_tok}.txt",
        },
    }
    first_entry = agent.conversation_store.messages.append_once(dedupe_req, dedupe_key="fc2-dedupe-1")
    second_entry = agent.conversation_store.messages.append_once(dedupe_req, dedupe_key="fc2-dedupe-1")
    assert first_entry.message_id == second_entry.message_id
    _assert_zero_secret_hits(first_entry.content, (secret_tok,))
    _assert_zero_secret_hits(first_entry.metadata, (secret_tok,))
    msg_disk = agent.conversation_store.storage.message_path(thread.thread_id).read_text(encoding="utf-8")
    _assert_zero_secret_hits(msg_disk, (secret_tok,))

    # 2. Explicit SendMessageTool: blocks cookie/debug/echo attachments & scrubs clean message
    owner_root = Path(agent.home_paths.owner_home_dir)
    task_root = owner_root / "tasks" / "fc2_send"
    work_dir = task_root / "output"
    work_dir.mkdir(parents=True, exist_ok=True)

    cookie_file = work_dir / "session_cookie_dump.txt"
    cookie_file.write_text("SID=123", encoding="utf-8")
    cookie_rec = register_artifact(ArtifactRegistration(workspace_root=task_root, path=cookie_file, kind="text"))

    echo_file = work_dir / "summary_echo.txt"
    echo_file.write_text(f"leaked={base64.b64encode(secret_tok.encode()).decode()}", encoding="utf-8")
    echo_rec = register_artifact(ArtifactRegistration(workspace_root=task_root, path=echo_file, kind="text"))

    clean_file = work_dir / "clean_business.txt"
    clean_file.write_text("ORDER_STATUS=PAID\n", encoding="utf-8")
    clean_rec = register_artifact(ArtifactRegistration(workspace_root=task_root, path=clean_file, kind="text"))

    send_tool = SendMessageTool(agent)
    run_scope = {
        "request_id": agent._current_run_params.request_id,
        "run_id": agent._current_run_params.run_id,
        "task_id": agent._current_run_params.task_id,
        "session_id": thread.thread_id,
    }
    for blocked_id in (cookie_rec.artifact_id, echo_rec.artifact_id):
        blocked_res = send_tool.execute(
            {
                "notify_channel": "feishu",
                "message": "请查收调试文件",
                "attachments": [blocked_id],
                "__run_scope": run_scope,
                "__artifact_task_root": str(task_root),
                "__artifact_registry_root": str(task_root),
            }
        )
        assert not blocked_res.ok
        assert blocked_res.error_code == "ARTIFACT_VALIDATION_FAILED"
        assert blocked_res.reported_error_code == "CREDENTIAL_ATTACHMENT_WITHHELD"
        assert blocked_res.effect_outcome == "not_started"
    assert not adapter.finals

    ok_send = send_tool.execute(
        {
            "notify_channel": "feishu",
            "message": f"业务核验完成，令牌 {secret_tok} 已隐藏",
            "attachments": [clean_rec.artifact_id],
            "__run_scope": run_scope,
            "__artifact_task_root": str(task_root),
            "__artifact_registry_root": str(task_root),
        }
    )
    assert ok_send.ok, ok_send.output
    assert len(adapter.finals) >= 1
    _assert_zero_secret_hits(adapter.finals, (secret_tok,))
    _assert_zero_secret_hits(ok_send.output, (secret_tok,))

    # 3. Background delivery + outbox freeze + restart redelivery + unverified legacy withholding
    fail_once = {"fail": True}

    class _FlakyFeishuAdapter:
        def finalize_response(self, target, _handle, message):
            return self.send_message(target, message)

        def send_message(self, target, message):
            if fail_once["fail"]:
                return False
            adapter.finals.append((target, message.content))
            return True

    flaky_reg = ChannelAdapterRegistry()
    flaky_reg.register_adapter("feishu", _FlakyFeishuAdapter(), capabilities=ChannelCapabilities(proactive=True))
    flaky_service = DeliveryService(flaky_reg)

    wake = agent.conversation_store.wakes.raise_signal(
        {
            "thread_id": thread.thread_id,
            "root_task_id": agent._current_run_params.task_id,
            "reason": "task_input_resolved",
            "metadata": {"input_provider": "credential", "credential_ref": "cred_" + "c" * 32},
        }
    )
    bg_req = SimpleNamespace(
        thread_id=thread.thread_id,
        task_id=agent._current_run_params.task_id,
        reason="task_input_resolved",
        conversation_turn_id=agent._current_run_params.request_id,
        wake_signal=wake.to_dict(),
    )
    deps = BackgroundDeliveryDependencies(
        agent=agent,
        store=agent.conversation_store,
        channels=flaky_service,
        task_status=lambda _r: "active",
    )
    deliv_ctx = DeliveryContext(
        channel="feishu",
        target=actor.actor_id,
        thread_id=thread.thread_id,
        task_id=agent._current_run_params.task_id,
        mode="reply",
        idempotency_key="bg-cred-1",
    )
    first_commit = record_background_response(
        deps,
        bg_req,
        deliv_ctx,
        f"自动汇报：{secret_tok} 对应的订单已结清。",
        delivery_artifacts=(
            {"artifact_id": "art_cookie", "path": str(cookie_file), "name": cookie_file.name, "kind": "text", "sha256": cookie_rec.sha256, "size_bytes": cookie_rec.size_bytes},
        ),
        assistant_commentaries=(f"中间过程：{secret_tok}",),
        deliver=True,
        delivery_reason="task_input_resolved",
    )
    assert first_commit.outbox_frozen is True

    # Inspect persisted outbox wake on disk: MUST already be sanitized & marked verified!
    persisted_wake = agent.conversation_store.wakes.pending_one(wake.wake_signal_id)
    cached_delivery = cached_owner_delivery(persisted_wake)
    assert cached_delivery is not None
    assert cached_delivery["credential_redaction_verified"] is True
    _assert_zero_secret_hits(cached_delivery, (secret_tok,))

    # Simulate process restart: close in-memory TurnCredentialRedaction, then replay frozen delivery
    turn_red.close()
    fail_once["fail"] = False
    frozen_obj = frozen_owner_delivery(cached_delivery)
    replay_commit = record_background_response(
        deps,
        bg_req,
        deliv_ctx,
        frozen_obj.content,
        delivery_artifacts=frozen_obj.delivery_artifacts,
        assistant_commentaries=frozen_obj.assistant_commentaries,
        deliver=True,
        delivery_reason="task_input_resolved",
        frozen_retry=frozen_obj,
    )
    assert replay_commit.delivery_status == "sent"
    _assert_zero_secret_hits(replay_commit.content, (secret_tok,))

    # Legacy unverified frozen record for credentialed wake is withheld with diagnostic
    legacy_wake = agent.conversation_store.wakes.raise_signal(
        {
            "thread_id": thread.thread_id,
            "root_task_id": agent._current_run_params.task_id,
            "reason": "task_input_resolved",
            "metadata": {"input_provider": "credential", "credential_ref": "cred_" + "d" * 32},
        }
    )
    legacy_bg_req = SimpleNamespace(
        thread_id=thread.thread_id,
        task_id=agent._current_run_params.task_id,
        reason="task_input_resolved",
        conversation_turn_id="gwreq-legacy-" + uuid.uuid4().hex[:8],
        wake_signal=legacy_wake.to_dict(),
    )
    unverified_legacy = frozen_owner_delivery(
        {
            "schema_version": "wake-owner-delivery.v1",
            "reason": "task_input_resolved",
            "task_id": agent._current_run_params.task_id,
            "content": "legacy unverified text",
        }
    )
    assert unverified_legacy.credential_redaction_verified is False
    withheld_commit = record_background_response(
        deps,
        legacy_bg_req,
        DeliveryContext(
            channel="feishu",
            target=actor.actor_id,
            thread_id=thread.thread_id,
            task_id=agent._current_run_params.task_id,
            mode="reply",
            idempotency_key="bg-cred-legacy",
        ),
        unverified_legacy.content,
        deliver=True,
        delivery_reason="task_input_resolved",
        frozen_retry=unverified_legacy,
    )
    assert "已安全扣留" in withheld_commit.content
    assert "legacy unverified text" not in withheld_commit.content


def test_two_concurrent_barrier_cards_and_unauthorized_actor_creates_zero_files(tmp_path, monkeypatch):
    agent_1 = _agent(tmp_path / "u1", "user", full=False)
    agent_2 = _agent(tmp_path / "u2", "user", full=False)
    _active(agent_1, actor_id="ou_user_one")
    _active(agent_2, actor_id="ou_user_two")

    sec_1 = "SYNTHETIC_CONCURRENT_CARD_ONE_1111"
    sec_2 = "SYNTHETIC_CONCURRENT_CARD_TWO_2222"
    ref_1 = _submit_custom_card(
        agent_1,
        "https://one.example.test/api",
        fields_spec=[{"name": "secret", "label": "Token"}],
        submitted_values={"secret": sec_1},
        authentication="bearer",
        collection_mode="password",
    )
    ref_2 = _submit_custom_card(
        agent_2,
        "https://two.example.test/api",
        fields_spec=[{"name": "secret", "label": "Token"}],
        submitted_values={"secret": sec_2},
        authentication="bearer",
        collection_mode="password",
    )

    sync_dir = tmp_path / "sync"
    sync_dir.mkdir(parents=True, exist_ok=True)
    for agent_obj in (agent_1, agent_2):
        agent_obj.config.additional_write_roots = [str(sync_dir)]

    host_scratch_parent = cred_mat_mod._ensure_private_host_dir(cred_mat_mod.credential_scratch_parent())
    neighbor_dir = host_scratch_parent / f"neighbor-{uuid.uuid4().hex[:8]}"
    neighbor_dir.mkdir(mode=0o700)
    neighbor_sentinel = neighbor_dir / "sentinel.txt"
    neighbor_sentinel.write_text("HOST_NEIGHBOR_SENTINEL", encoding="utf-8")

    worker_code = (
        "import hashlib, json, os, sys, time\n"
        "from pathlib import Path\n"
        "p = os.environ['MY_AGENT_CREDENTIAL_FILE']\n"
        "val = json.load(open(p, encoding='utf-8'))['values']['secret']\n"
        "digest = hashlib.sha256(val.encode('utf-8')).hexdigest()\n"
        "own_scratch = Path(os.environ['TMPDIR'])\n"
        "scratch_parent = own_scratch.parent\n"
        "parent_list_blocked = False\n"
        "parent_entries = []\n"
        "try:\n"
        "    parent_entries = sorted(os.listdir(scratch_parent))\n"
        "except OSError:\n"
        "    parent_list_blocked = True\n"
        "neighbor_read_blocked = False\n"
        "try:\n"
        "    Path(sys.argv[1]).read_text(encoding='utf-8')\n"
        "except OSError:\n"
        "    neighbor_read_blocked = True\n"
        "time.sleep(0.15)\n"
        "print(json.dumps({\n"
        "    'sha256': digest,\n"
        "    'own_scratch_name': own_scratch.name,\n"
        "    'parent_list_blocked': parent_list_blocked,\n"
        "    'parent_entries': parent_entries,\n"
        "    'neighbor_read_blocked': neighbor_read_blocked,\n"
        "}))\n"
    )
    for idx, ag in enumerate((agent_1, agent_2), start=1):
        ag.tools.workspace_root.mkdir(parents=True, exist_ok=True)
        (ag.tools.workspace_root / f"worker_{idx}.py").write_text(worker_code, encoding="utf-8")

    params_1 = agent_1._current_run_params
    params_2 = agent_2._current_run_params
    barrier = threading.Barrier(2)
    results: dict[int, object] = {}
    shell_py = _shell_python()

    def _run_worker(slot: int, ag, run_params, ref: str) -> None:
        ag._current_run_params = run_params
        script_path = ag.tools.workspace_root / f"worker_{slot}.py"
        barrier.wait(timeout=10)
        results[slot] = _run_formal_tool(
            ag,
            "run_command",
            {
                "command": f"{shlex.quote(shell_py)} {shlex.quote(str(script_path))} {shlex.quote(str(neighbor_sentinel))}",
                "credential_ref": ref,
            },
        )

    try:
        t1 = threading.Thread(target=_run_worker, args=(1, agent_1, params_1, ref_1))
        t2 = threading.Thread(target=_run_worker, args=(2, agent_2, params_2, ref_2))
        t1.start()
        t2.start()
        t1.join(timeout=20)
        t2.join(timeout=20)
    finally:
        shutil.rmtree(neighbor_dir, ignore_errors=True)
        cred_mat_mod._try_prune_empty_dir(host_scratch_parent)

    out_1, out_2 = results[1], results[2]
    assert out_1.ok and out_2.ok, (out_1.output, out_2.output)
    import hashlib

    rep_1 = _extract_json_report(out_1.output)
    rep_2 = _extract_json_report(out_2.output)
    assert rep_1["sha256"] == hashlib.sha256(sec_1.encode()).hexdigest()
    assert rep_2["sha256"] == hashlib.sha256(sec_2.encode()).hexdigest()
    for rep in (rep_1, rep_2):
        assert rep["parent_list_blocked"] is True or rep["parent_entries"] == [rep["own_scratch_name"]]
        assert rep["neighbor_read_blocked"] is True
    _assert_zero_secret_hits(out_1.output, (sec_1, sec_2))
    _assert_zero_secret_hits(out_2.output, (sec_1, sec_2))

    # Wrong actor attempting to use ref_1: 0 decryptions and 0 temp credential files created!
    file_created_count = 0
    decrypt_count = 0
    orig_cf = cred_mat_mod.credential_file_for_child
    orig_cipher = task_cred_mod._cipher

    def _count_cf(*a, **k):
        nonlocal file_created_count
        file_created_count += 1
        return orig_cf(*a, **k)

    def _count_cipher(*a, **k):
        nonlocal decrypt_count
        decrypt_count += 1
        return orig_cipher(*a, **k)

    monkeypatch.setattr(cred_mat_mod, "credential_file_for_child", _count_cf)
    monkeypatch.setattr(task_cred_mod, "_cipher", _count_cipher)

    # Switch agent_1 to a different actor in a new task
    _active(agent_1, actor_id="ou_unauthorized_other")
    denied = _run_formal_tool(
        agent_1,
        "run_command",
        {"command": "python3 -c 'print(1)'", "credential_ref": ref_1},
    )
    assert not denied.ok
    assert denied.error_code in {"CREDENTIAL_NOT_AUTHORIZED", "CREDENTIAL_BINDING_INVALID"}
    assert denied.effect_outcome == "not_started"
    assert file_created_count == 0
    assert decrypt_count == 0

    turn_credential_redaction(agent_1, agent_1._current_run_params).close()
    turn_credential_redaction(agent_2, agent_2._current_run_params).close()


def test_registered_scripts_action_run_with_credential_ref_and_artifact_gate(tmp_path):
    agent = _agent(tmp_path, "user", full=False)
    _active(agent)
    card_secret = "SYNTHETIC_REGISTERED_SCRIPT_SECRET_6619"
    ref = _submit_custom_card(
        agent,
        "https://scripts.example.test/api",
        fields_spec=[{"name": "secret", "label": "Token"}],
        submitted_values={"secret": card_secret},
        authentication="bearer",
        collection_mode="password",
    )

    owner_home = Path(agent.home_paths.owner_home_dir)
    scripts_tool = ScriptsTool(
        owner_home,
        workspace_root=agent.tools.workspace_root,
        runner=ScriptRunner(
            owner_home,
            workspace_root=agent.tools.workspace_root,
            read_scope_mode="exact",
            owner_scope_root=owner_home,
            agent_home_root=agent.home_paths.root,
            run_out_quota=agent.owner_quota,
        ),
    )

    # Non-run actions reject credential_ref at validation with not_started
    bad_get = scripts_tool.execute({"action": "list", "credential_ref": ref})
    assert not bad_get.ok
    assert bad_get.error_code == "TOOL_INVALID_ARGUMENTS"
    assert bad_get.effect_outcome == "not_started"

    reg = scripts_tool.execute(
        {
            "action": "register",
            "script_id": "cred_reader",
            "code": (
                "import hashlib, json, os, sys\n"
                "from pathlib import Path\n"
                "inp = json.loads(os.environ.get('MY_AGENT_SCRIPT_INPUT_JSON', '{}'))\n"
                "cred_path = os.environ.get('MY_AGENT_CREDENTIAL_FILE', '')\n"
                "loaded = json.load(open(cred_path, encoding='utf-8')) if cred_path else {}\n"
                "sec = loaded.get('values', {}).get('secret', '')\n"
                "run_out = os.environ.get('MY_AGENT_RUN_OUT', '')\n"
                "if run_out and inp.get('write_cookie_dump'):\n"
                "    (Path(run_out) / 'debug_cookie.bin').write_bytes(b'\\x00RAW')\n"
                "elif run_out and inp.get('write_text_report'):\n"
                "    (Path(run_out) / 'report.txt').write_text(f'summary echo={sec}', encoding='utf-8')\n"
                "sys.stderr.write(f'diag={sec}\\n')\n"
                "print(json.dumps({\n"
                "    'digest': hashlib.sha256(sec.encode('utf-8')).hexdigest() if sec else '',\n"
                "    'echo_check': f'seen:{sec}',\n"
                "}))\n"
            ),
            "descriptor": {
                "name": "凭据校验固化脚本",
                "access_targets": [],
                "data_sources": [],
                "credential_refs": [],
                "io_contract": {"inputs": [], "outputs": ["digest", "echo_check"]},
            },
        }
    )
    assert reg.ok, reg.output

    boundary = write_boundary_with_runtime_ledger(agent, agent._current_run_params)
    cred_use = boundary["_host_output_redaction"]._credential_use.begin_invocation(boundary["_host_output_redaction"])
    ctx = SimpleNamespace(credential_use=cred_use, execution_authority_check=None)
    try:
        # 1. Authorized run reads MY_AGENT_CREDENTIAL_FILE under exact read scope & scrubs output/stderr/text run_out
        with bind_run_out_target(owner_home, "wfrun_ok", "step_1", "a" * 32):
            run_ok = scripts_tool.execute_scoped(
                {
                    "action": "run",
                    "script_id": "cred_reader",
                    "inputs": {"write_text_report": True},
                    "credential_ref": ref,
                },
                ctx,
            )
        assert run_ok.ok, run_ok.output
        import hashlib

        env_data = run_ok.result_envelope
        assert env_data["output_data"]["digest"] == hashlib.sha256(card_secret.encode()).hexdigest()
        assert "[REDACTED]" in env_data["output_data"]["echo_check"]
        assert env_data["credential_execution"]["cleanup_confirmed"] is True
        _assert_zero_secret_hits(run_ok.output, (card_secret,))
        _assert_zero_secret_hits(env_data, (card_secret,))
        published_dir = Path(env_data["output_dir"])
        _assert_zero_secret_hits((published_dir / "report.txt").read_text(encoding="utf-8"), (card_secret,))

        # 2. Binary / cookie debug file in MY_AGENT_RUN_OUT is hard-blocked before staging
        with bind_run_out_target(owner_home, "wfrun_block", "step_2", "b" * 32):
            run_blocked = scripts_tool.execute_scoped(
                {
                    "action": "run",
                    "script_id": "cred_reader",
                    "inputs": {"write_cookie_dump": True},
                    "credential_ref": ref,
                },
                ctx,
            )
        assert not run_blocked.ok
        assert run_blocked.result_envelope["error_code"] == "SCRIPT_CREDENTIAL_ARTIFACT_BLOCKED"
        _assert_zero_secret_hits(run_blocked.output, (card_secret,))
    finally:
        cred_use.disarm()
        turn_credential_redaction(agent, agent._current_run_params).close()


def test_session_scope_cli_non_task_graceful_and_turn_eviction_clears_log_secrets(tmp_path):
    from agent_py_agent.agent.common import log_redaction as log_red_mod
    from agent_py_agent.agent.tooling.host_output_redaction import (
        _MAX_ACTIVE_TURNS_PER_SCOPE_COUNT,
        TurnCredentialRedaction,
    )

    agent = _agent(tmp_path, "user", full=False)
    # Local CLI / non-task RunParams without registered task link must not raise in session_scope()
    cli_params = RunParams(request_id="cli-req-1", task_id="unregistered-cli-task", run_id="unregistered-cli-run")
    boundary = write_boundary_with_runtime_ledger(agent, cli_params)
    cred_use = boundary["_host_output_redaction"]._credential_use.begin_invocation(HostOutputRedaction())
    try:
        scope_info = cred_use.session_scope()
        assert scope_info["active"] is False
        empty_params = RunParams(request_id="cli-empty", task_id="", run_id="")
        empty_use = write_boundary_with_runtime_ledger(agent, empty_params)["_host_output_redaction"]._credential_use
        assert empty_use.session_scope()["active"] is False
    finally:
        cred_use.disarm()

    # Evicting oldest turn from scope bucket also unregisters its _ACTIVE_TURN_LOG_SECRETS entry
    turns: list[TurnCredentialRedaction] = []
    evicted_secret = "SYNTHETIC_EVICTED_LOG_SECRET_0001"
    for idx in range(_MAX_ACTIVE_TURNS_PER_SCOPE_COUNT + 2):
        tr = TurnCredentialRedaction(owner_key="evict-owner", thread_id="thr-evict", turn_key=f"turn-{idx}")
        tr.register_values((evicted_secret if idx == 0 else f"SYNTHETIC_TURN_SECRET_{idx:04d}",))
        turns.append(tr)
    try:
        assert "evict-owner::turn-0" not in log_red_mod._ACTIVE_TURN_LOG_SECRETS
        assert evicted_secret not in log_red_mod._ACTIVE_TURN_LOG_COUNTS
    finally:
        for tr in turns:
            tr.close()
    assert not any(k.startswith("evict-owner::") for k in log_red_mod._ACTIVE_TURN_LOG_SECRETS)
