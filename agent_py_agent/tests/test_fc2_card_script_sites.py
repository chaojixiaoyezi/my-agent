"""FC2 端到端双临时回环站（标准 Header API + SM4-CBC/PKCS7/Base64/循环移位表单会话站）与全表面零泄漏测试。"""
from __future__ import annotations

import base64
import contextlib
import io
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from agent_py_agent.agent.agent_core.tool_runtime_ledger import write_boundary_with_runtime_ledger
from agent_py_agent.agent.common.json_io import read_json_object_report
from agent_py_agent.agent.common.log_redaction import RedactingFormatter, install_log_redaction
from agent_py_agent.agent.delivery import (
    ChannelAdapterRegistry,
    ChannelCapabilities,
    DeliveryContext,
    DeliveryService,
    ReplyEnvelope,
)
from agent_py_agent.agent.gateway_parts.http_handlers import _read_public_progress_events
from agent_py_agent.agent.gateway_parts.private_interaction_service import (
    resolve_private_interaction,
)
from agent_py_agent.agent.gateway_parts.stream_writer import BufferedChunkStreamWriter
from agent_py_agent.agent.tooling.host_output_redaction import (
    credential_echo_variants,
    turn_credential_redaction,
)
from agent_py_agent.agent.user_space.task_credentials import (
    CredentialCardOptions,
    credential_path,
    request_credential_card,
)
from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call
from agent_py_agent.tests.test_im_progress_draft import Adapter
from agent_py_agent.tests.test_ingestion_task_credential_scope import _active, _agent

_SM4_TEST_KEY_HEX = "0123456789abcdeffedcba9876543210"
_SM4_TEST_IV_HEX = "fedcba98765432100123456789abcdef"


def _rotate_left_4(text: str) -> str:
    if len(text) <= 4:
        return text
    return text[4:] + text[:4]


def _rotate_right_4(text: str) -> str:
    if len(text) <= 4:
        return text
    return text[-4:] + text[:-4]


def _sm4_cbc_encode_shifted(plain_text: str, key_bytes: bytes, iv_bytes: bytes) -> str:
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plain_text.encode("utf-8")) + padder.finalize()
    encryptor = Cipher(algorithms.SM4(key_bytes), modes.CBC(iv_bytes)).encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    b64_text = base64.b64encode(encrypted).decode("ascii")
    return _rotate_left_4(b64_text)


def _sm4_cbc_decode_shifted(shifted_text: str, key_bytes: bytes, iv_bytes: bytes) -> str:
    restored_b64 = _rotate_right_4(shifted_text)
    cipher_bytes = base64.b64decode(restored_b64.encode("ascii"), validate=True)
    decryptor = Cipher(algorithms.SM4(key_bytes), modes.CBC(iv_bytes)).decryptor()
    padded = decryptor.update(cipher_bytes) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    raw = unpadder.update(padded) + unpadder.finalize()
    return raw.decode("utf-8")


def _open_fd_count() -> int:
    try:
        return len(os.listdir("/dev/fd"))
    except OSError:
        count = 0
        for fd in range(256):
            try:
                os.fstat(fd)
                count += 1
            except OSError:
                pass
        return count


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


def _extract_json_line(text: str) -> dict[str, object]:
    matched = re.search(r"\{[^\n]*\}", text)
    assert matched is not None, f"missing JSON object in tool output: {text!r}"
    return json.loads(matched.group(0))


def _assert_zero_secret_hits(surface_name: str, obj: object, secret_items: tuple[str, ...]) -> None:
    rendered = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, default=str)
    variants = credential_echo_variants(secret_items)
    hits = sum(1 for v in variants if v and v in rendered)
    assert hits == 0, f"surface {surface_name} contained {hits} credential echo variant hit(s)"


@contextlib.contextmanager
def _loopback_standard_token_site(*, header_name: str, expected_token: str, private_marker: str):
    audit_events: list[dict[str, object]] = []
    lock = threading.Lock()

    class _StdHandler(BaseHTTPRequestHandler):
        def log_message(self, _fmt: str, *_args: object) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            provided = self.headers.get(header_name, "")
            authorized = self.path == "/v1/records/protected" and provided == expected_token
            status = 200 if authorized else 401
            with lock:
                audit_events.append(
                    {
                        "method": "GET",
                        "path": self.path,
                        "status": status,
                        "has_session": False,
                        "format_ok": bool(provided),
                        "raw_secret_in_body": False,
                    }
                )
            body_obj = (
                {"ok": True, "marker": private_marker, "record": "STD-REC-01"}
                if authorized
                else {"ok": False, "error": "unauthorized"}
            )
            encoded = json.dumps(body_obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _StdHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}", audit_events
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextlib.contextmanager
def _loopback_sm4_shifted_form_site(
    *,
    expected_user: str,
    expected_password: str,
    key_bytes: bytes,
    private_marker: str,
):
    audit_events: list[dict[str, object]] = []
    valid_sessions: set[str] = set()
    lock = threading.Lock()

    class _Sm4Handler(BaseHTTPRequestHandler):
        def log_message(self, _fmt: str, *_args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0") or "0")
            wire_bytes = self.rfile.read(length) if length > 0 else b""
            wire_text = wire_bytes.decode("utf-8", errors="replace")
            raw_hit = (
                expected_password in wire_text
                or urllib.parse.quote(expected_password, safe="") in wire_text
                or urllib.parse.quote_plus(expected_password, safe="") in wire_text
                or base64.b64encode(expected_password.encode("utf-8")).decode("ascii") in wire_text
            )
            parsed = urllib.parse.parse_qs(wire_text, keep_blank_values=True)
            account = (parsed.get("account") or [""])[0]
            cipher_form = (parsed.get("cipher_form") or [""])[0]
            nonce_hex = (parsed.get("nonce") or [""])[0]
            format_ok = False
            auth_ok = False
            if self.path == "/portal/signin" and account and cipher_form and len(nonce_hex) == 32 and not raw_hit:
                try:
                    iv_bytes = bytes.fromhex(nonce_hex)
                    decoded_pw = _sm4_cbc_decode_shifted(cipher_form, key_bytes, iv_bytes)
                    format_ok = True
                    auth_ok = account == expected_user and decoded_pw == expected_password
                except Exception:
                    format_ok = False
                    auth_ok = False

            status = 200 if auth_ok else 401
            issued_sid = ""
            if auth_ok:
                issued_sid = "sid_" + uuid.uuid4().hex
                with lock:
                    valid_sessions.add(issued_sid)
            with lock:
                audit_events.append(
                    {
                        "method": "POST",
                        "path": self.path,
                        "status": status,
                        "has_session": bool(issued_sid),
                        "format_ok": format_ok,
                        "raw_secret_in_body": raw_hit,
                    }
                )
            resp_bytes = json.dumps({"ok": auth_ok}).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            if issued_sid:
                self.send_header("Set-Cookie", f"portal_sid={issued_sid}; Path=/; HttpOnly")
            self.send_header("Content-Length", str(len(resp_bytes)))
            self.end_headers()
            self.wfile.write(resp_bytes)

        def do_GET(self) -> None:  # noqa: N802
            cookie_hdr = self.headers.get("Cookie", "")
            jar = SimpleCookie()
            if cookie_hdr:
                jar.load(cookie_hdr)
            sid_morsel = jar.get("portal_sid")
            sid_val = sid_morsel.value if sid_morsel is not None else ""
            with lock:
                session_ok = bool(sid_val and sid_val in valid_sessions)
            authorized = self.path == "/portal/records/private" and session_ok
            status = 200 if authorized else 401
            with lock:
                audit_events.append(
                    {
                        "method": "GET",
                        "path": self.path,
                        "status": status,
                        "has_session": session_ok,
                        "format_ok": True,
                        "raw_secret_in_body": False,
                    }
                )
            resp_obj = (
                {"ok": True, "marker": private_marker, "record": "SM4-REC-02"}
                if authorized
                else {"ok": False, "error": "session_required"}
            )
            resp_bytes = json.dumps(resp_obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp_bytes)))
            self.end_headers()
            self.wfile.write(resp_bytes)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Sm4Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}", audit_events
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _submit_card(
    agent,
    url: str,
    *,
    authentication: str,
    collection_mode: str,
    fields_spec: list[dict[str, str]] | None,
    submitted_fields: dict[str, str],
    header_name: str | None = None,
) -> str:
    agent.config.feishu_session_lock_enabled = False
    agent.channel_registry.register_adapter(
        "feishu",
        SimpleNamespace(
            send_control_card=lambda *_a, **_k: {
                "message_id": "om_" + uuid.uuid4().hex,
                "chat_id": "oc_dm_sites_fc2",
            }
        ),
        capabilities=ChannelCapabilities(control_cards=True, proactive=True),
    )
    ref, _ = request_credential_card(
        agent,
        url=url,
        authentication=authentication,
        **({"header_name": header_name} if header_name else {}),
        options=CredentialCardOptions(
            fields=fields_spec,
            collection_mode=collection_mode,
            access_scope="task",
        ),
    )
    card_row = read_json_object_report(
        credential_path(agent.home_paths.owner_home_dir, ref),
        context="fc2.sites.card",
    ).payload
    res = resolve_private_interaction(
        agent,
        {
            "reference": card_row["reference"],
            "decision": "submit",
            "message_id": card_row["message_id"],
            "chat_id": card_row["chat_id"],
            "fields": dict(submitted_fields),
        },
        channel="feishu",
        actor_id=card_row["actor_id"],
    )
    assert res["ok"] is True
    return ref


def _invoke_run_command(agent, command: str, *, credential_ref: str):
    current = agent._current_run_params
    boundary = write_boundary_with_runtime_ledger(agent, current)
    return execute_registry_test_call(
        agent.tools,
        "run_command",
        {"command": command, "credential_ref": credential_ref},
        run_id=str(current.run_id),
        call_id="call-" + uuid.uuid4().hex[:12],
        attempt_id=str(getattr(current, "attempt_id", "") or "test-attempt"),
        write_boundary=boundary,
        register_with=agent,
    )


def _http_status(url: str, *, headers: dict[str, str] | None = None) -> tuple[int, dict[str, object]]:
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_sm4_gmt_0002_2012_vector_and_end_to_end_dual_loopback_sites_zero_leak(tmp_path):
    # 0. Verify cryptography SM4 implementation against GM/T 0002-2012 standard public vector
    vec_key = bytes.fromhex(_SM4_TEST_KEY_HEX)
    vec_block = bytes.fromhex("0123456789abcdeffedcba9876543210")
    ecb_enc = Cipher(algorithms.SM4(vec_key), modes.ECB()).encryptor()
    assert (ecb_enc.update(vec_block) + ecb_enc.finalize()).hex() == "681edf34d206965e86b3e94f536e4246"

    install_log_redaction()
    log_buffer = io.StringIO()
    log_handler = logging.StreamHandler(log_buffer)
    log_handler.setFormatter(RedactingFormatter("%(levelname)s %(name)s %(message)s"))
    gw_logger = logging.getLogger("agent_py_agent.tests.fc2_gateway_probe")
    gw_logger.setLevel(logging.INFO)
    gw_logger.addHandler(log_handler)

    std_token = "SYNTH_STD_HEADER_TOKEN_FC2_4091"
    sm4_user = "corp_operator_fc2"
    sm4_pass = "Sm4#Pass!秘密口令+2026=="
    std_marker = "PRIVATE_MARKER_STD_API_8812"
    sm4_marker = "PRIVATE_MARKER_SM4_PORTAL_9934"
    sm4_key = bytes.fromhex(_SM4_TEST_KEY_HEX)
    sm4_iv = bytes.fromhex(_SM4_TEST_IV_HEX)

    agent = _agent(tmp_path, "user", full=False)
    thread, actor, _claim = _active(agent)
    ws_dir = agent.tools.workspace_root
    ws_dir.mkdir(parents=True, exist_ok=True)

    # Write deterministic client scripts inside workspace BEFORE credentialed runs (workspace is read-only during runs)
    std_client_script = ws_dir / "query_std_site.py"
    std_client_script.write_text(
        "import json, os, sys, urllib.request\n"
        "base_url, header_name = sys.argv[1], sys.argv[2]\n"
        "with open(os.environ['MY_AGENT_CREDENTIAL_FILE'], encoding='utf-8') as fh:\n"
        "    cred = json.load(fh)\n"
        "tok = cred['values']['secret']\n"
        "req = urllib.request.Request(base_url + '/v1/records/protected', headers={header_name: tok})\n"
        "with urllib.request.urlopen(req, timeout=5) as resp:\n"
        "    data = json.loads(resp.read().decode('utf-8'))\n"
        "print(json.dumps({'status': resp.status, 'marker': data['marker']}))\n",
        encoding="utf-8",
    )

    sm4_client_script = ws_dir / "query_sm4_site.py"
    sm4_client_script.write_text(
        "import base64, http.cookiejar, json, os, sys, urllib.parse, urllib.request\n"
        "from pathlib import Path\n"
        "from cryptography.hazmat.primitives import padding\n"
        "from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes\n"
        "base_url, key_hex, iv_hex, emit_canary = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] == '1'\n"
        "with open(os.environ['MY_AGENT_CREDENTIAL_FILE'], encoding='utf-8') as fh:\n"
        "    cred = json.load(fh)\n"
        "vals = cred['values']\n"
        "user_val, pw_val = vals['username'], vals['secret']\n"
        "padder = padding.PKCS7(128).padder()\n"
        "padded = padder.update(pw_val.encode('utf-8')) + padder.finalize()\n"
        "enc = Cipher(algorithms.SM4(bytes.fromhex(key_hex)), modes.CBC(bytes.fromhex(iv_hex))).encryptor()\n"
        "b64_ct = base64.b64encode(enc.update(padded) + enc.finalize()).decode('ascii')\n"
        "shifted = b64_ct[4:] + b64_ct[:4]\n"
        "cookie_file = Path(os.environ['TMPDIR']) / 'portal_cookies.txt'\n"
        "jar = http.cookiejar.MozillaCookieJar(str(cookie_file))\n"
        "opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))\n"
        "form_bytes = urllib.parse.urlencode({'account': user_val, 'cipher_form': shifted, 'nonce': iv_hex}).encode('ascii')\n"
        "login_req = urllib.request.Request(\n"
        "    base_url + '/portal/signin',\n"
        "    data=form_bytes,\n"
        "    headers={'Content-Type': 'application/x-www-form-urlencoded'},\n"
        "    method='POST',\n"
        ")\n"
        "with opener.open(login_req, timeout=5) as login_resp:\n"
        "    login_ok = login_resp.status == 200\n"
        "jar.save(ignore_discard=True, ignore_expires=True)\n"
        "with opener.open(base_url + '/portal/records/private', timeout=5) as rec_resp:\n"
        "    rec_data = json.loads(rec_resp.read().decode('utf-8'))\n"
        "if emit_canary:\n"
        "    combo_b64 = base64.b64encode(f'{user_val}:{pw_val}'.encode('utf-8')).decode('ascii')\n"
        "    pct_pw = urllib.parse.quote(pw_val, safe='')\n"
        "    sys.stderr.write(f'CANARY_STDERR raw={pw_val} pct={pct_pw} basic={combo_b64}\\n')\n"
        "    print(f'CANARY_STDOUT user={user_val} pw={pw_val}')\n"
        "print(json.dumps({'login_ok': login_ok, 'marker': rec_data['marker'], 'cookie_in_scratch': cookie_file.is_file()}))\n",
        encoding="utf-8",
    )

    tmpdir_path = Path(os.environ.get("TMPDIR") or "/tmp")
    tmpdir_before = set(os.listdir(tmpdir_path))
    ws_before = set(os.listdir(ws_dir))
    children_before = _child_pids_of_self()

    try:
        with (
            _loopback_standard_token_site(
                header_name="X-Service-Token",
                expected_token=std_token,
                private_marker=std_marker,
            ) as (std_url, std_logs),
            _loopback_sm4_shifted_form_site(
                expected_user=sm4_user,
                expected_password=sm4_pass,
                key_bytes=sm4_key,
                private_marker=sm4_marker,
            ) as (sm4_url, sm4_logs),
        ):
            # 1. Prove unauthenticated requests return 401 on both sites
            unauth_std_code, _ = _http_status(f"{std_url}/v1/records/protected")
            unauth_sm4_code, _ = _http_status(f"{sm4_url}/portal/records/private")
            assert unauth_std_code == 401
            assert unauth_sm4_code == 401

            # 2. Direct control queries (without credential cards) to establish reference markers
            ctrl_std_code, ctrl_std_body = _http_status(
                f"{std_url}/v1/records/protected",
                headers={"X-Service-Token": std_token},
            )
            assert ctrl_std_code == 200
            direct_std_marker = ctrl_std_body["marker"]

            ctrl_jar = urllib.request.HTTPCookieProcessor()
            ctrl_opener = urllib.request.build_opener(ctrl_jar)
            ctrl_form = urllib.parse.urlencode(
                {
                    "account": sm4_user,
                    "cipher_form": _sm4_cbc_encode_shifted(sm4_pass, sm4_key, sm4_iv),
                    "nonce": _SM4_TEST_IV_HEX,
                }
            ).encode("ascii")
            with ctrl_opener.open(
                urllib.request.Request(
                    f"{sm4_url}/portal/signin",
                    data=ctrl_form,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    method="POST",
                ),
                timeout=5,
            ) as ctrl_login:
                assert ctrl_login.status == 200
            with ctrl_opener.open(f"{sm4_url}/portal/records/private", timeout=5) as ctrl_rec:
                direct_sm4_marker = json.loads(ctrl_rec.read().decode("utf-8"))["marker"]

            # 3. Real Feishu card submission + ToolExecutor -> run_command(credential_ref) -> OS subprocess
            std_ref = _submit_card(
                agent,
                f"{std_url}/v1/records/protected",
                authentication="header",
                header_name="X-Service-Token",
                collection_mode="password",
                fields_spec=None,
                submitted_fields={"secret": std_token},
            )
            sm4_ref = _submit_card(
                agent,
                f"{sm4_url}/portal/signin",
                authentication="collected",
                collection_mode="username_password",
                fields_spec=[
                    {"name": "username", "label": "企业账号"},
                    {"name": "secret", "label": "登录密码"},
                ],
                submitted_fields={"username": sm4_user, "secret": sm4_pass},
            )

            # Warm runtime.db authority chain once, then verify 0 FD leak across all 3 credentialed runs
            warmup_res = _invoke_run_command(
                agent,
                f"{shlex.quote(sys.executable)} {shlex.quote(str(std_client_script))} {shlex.quote(std_url)} X-Service-Token",
                credential_ref=std_ref,
            )
            assert warmup_res.ok, warmup_res.output
            fd_before_runs = _open_fd_count()

            std_res = _invoke_run_command(
                agent,
                f"{shlex.quote(sys.executable)} {shlex.quote(str(std_client_script))} {shlex.quote(std_url)} X-Service-Token",
                credential_ref=std_ref,
            )
            assert std_res.ok, std_res.output
            std_report = _extract_json_line(std_res.output)
            assert std_report == {"status": 200, "marker": direct_std_marker}

            sm4_res = _invoke_run_command(
                agent,
                (
                    f"{shlex.quote(sys.executable)} {shlex.quote(str(sm4_client_script))} "
                    f"{shlex.quote(sm4_url)} {_SM4_TEST_KEY_HEX} {_SM4_TEST_IV_HEX} 0"
                ),
                credential_ref=sm4_ref,
            )
            assert sm4_res.ok, sm4_res.output
            sm4_report = _extract_json_line(sm4_res.output)
            assert sm4_report == {"login_ok": True, "marker": direct_sm4_marker, "cookie_in_scratch": True}

            # Verify server audit logs: zero raw secrets in SM4 wire body, session cookie honored
            signin_events = [e for e in sm4_logs if e["path"] == "/portal/signin"]
            private_events = [e for e in sm4_logs if e["path"] == "/portal/records/private" and e["status"] == 200]
            assert len(signin_events) >= 2
            assert all(e["format_ok"] is True and e["raw_secret_in_body"] is False for e in signin_events)
            assert len(private_events) >= 2
            assert all(e["has_session"] is True for e in private_events)

            # 4. Synthetic canary echo on stdout/stderr + full public surface scan
            turn_red = turn_credential_redaction(agent, agent._current_run_params)
            sse_sink_events: list[dict[str, object]] = []
            stream_writer = BufferedChunkStreamWriter(
                tmp_path / "gateway_chunks.jsonl",
                delivery_channel="feishu",
                rich_transcript=True,
                transcript_sink=sse_sink_events.append,
            )
            stream_writer.bind_credential_redaction(turn_red)
            stream_writer.set_verbose_level("stream")

            canary_res = _invoke_run_command(
                agent,
                (
                    f"{shlex.quote(sys.executable)} {shlex.quote(str(sm4_client_script))} "
                    f"{shlex.quote(sm4_url)} {_SM4_TEST_KEY_HEX} {_SM4_TEST_IV_HEX} 1"
                ),
                credential_ref=sm4_ref,
            )
            assert canary_res.ok, canary_res.output
            fd_after_runs = _open_fd_count()
            assert fd_after_runs - fd_before_runs == 0

            # Feed synthetic echoes into stream writer, gateway logger, conversation store, and Feishu delivery
            bound_mc = stream_writer.bind_model_call("mc-sites-final")
            bound_mc(f"已获取记录 {direct_std_marker} / {direct_sm4_marker}，回显检查：{sm4_pass[:8]}")
            bound_mc(f"{sm4_pass[8:]} 与 {std_token}")
            stream_writer.commit_model_response(
                {
                    "schema_version": "committed_model_response.v1",
                    "model_call_id": "mc-sites-final",
                    "boundary": "final",
                    "final_text": f"核验通过：{direct_std_marker} & {direct_sm4_marker}（canary={sm4_pass} / {std_token}）",
                }
            )
            stream_writer.close()

            gw_logger.info("gateway audit trace std=%s sm4_pw=%s user=%s", std_token, sm4_pass, sm4_user)
            log_handler.flush()

            msg_entry = agent.conversation_store.messages.append(
                {
                    "thread_id": thread.thread_id,
                    "role": "assistant",
                    "channel": "feishu",
                    "content": f"查询完成：{direct_std_marker}, {direct_sm4_marker}, canary={sm4_pass}, tok={std_token}",
                    "metadata": {
                        "conversation_request_id": agent._current_run_params.request_id,
                        "tool_summary": canary_res.output,
                    },
                }
            )

            feishu_adapter = Adapter()
            ch_reg = ChannelAdapterRegistry()
            ch_reg.register_adapter("feishu", feishu_adapter, capabilities=ChannelCapabilities(proactive=True, reply=True))
            deliv_svc = DeliveryService(ch_reg)
            outbound_text = turn_red.public_text(
                f"站点结果：{direct_std_marker} / {direct_sm4_marker}（canary={sm4_pass} / {std_token}）"
            )
            deliv_rcpt = deliv_svc.deliver(
                DeliveryContext(
                    channel="feishu",
                    target=actor.actor_id,
                    thread_id=thread.thread_id,
                    task_id=agent._current_run_params.task_id,
                    mode="proactive",
                    request_id=agent._current_run_params.request_id,
                ),
                ReplyEnvelope(content=outbound_text),
            )
            assert deliv_rcpt.delivery_status == "sent"

            all_secrets = (
                std_token,
                sm4_user,
                sm4_pass,
                f"{sm4_user}:{sm4_pass}",
            )

            # Scan EVERY public & persisted surface for zero raw/encoded hits
            _assert_zero_secret_hits("std_res.output", std_res.output, all_secrets)
            _assert_zero_secret_hits("sm4_res.output", sm4_res.output, all_secrets)
            _assert_zero_secret_hits("canary_res.output", canary_res.output, all_secrets)
            _assert_zero_secret_hits("canary_res.metadata", canary_res.metadata, all_secrets)
            _assert_zero_secret_hits("msg_entry", msg_entry.to_dict(), all_secrets)
            _assert_zero_secret_hits(
                "messages.jsonl",
                agent.conversation_store.storage.message_path(thread.thread_id).read_text(encoding="utf-8"),
                all_secrets,
            )
            _assert_zero_secret_hits(
                "gateway_chunks.jsonl",
                stream_writer.chunk_path.read_text(encoding="utf-8"),
                all_secrets,
            )
            sse_public_events, _ = _read_public_progress_events(stream_writer.chunk_path, 0, channel="feishu")
            _assert_zero_secret_hits("sse_public_events", sse_public_events, all_secrets)
            _assert_zero_secret_hits("sse_sink_events", sse_sink_events, all_secrets)
            _assert_zero_secret_hits("gateway_logs", log_buffer.getvalue(), all_secrets)
            _assert_zero_secret_hits("feishu_deliveries", feishu_adapter.finals, all_secrets)
            _assert_zero_secret_hits("std_server_logs", std_logs, all_secrets)
            _assert_zero_secret_hits("sm4_server_logs", sm4_logs, all_secrets)

            # Scan any archived tool outputs & workspace files on disk
            for p in Path(agent.home_paths.owner_home_dir).rglob("*"):
                if p.is_file() and p.suffix in {".txt", ".json", ".jsonl", ".log"} and "credentials" not in p.parts:
                    _assert_zero_secret_hits(f"owner_disk:{p.name}", p.read_text(encoding="utf-8", errors="replace"), all_secrets)
            for p in ws_dir.rglob("*"):
                if p.is_file():
                    _assert_zero_secret_hits(f"workspace:{p.name}", p.read_text(encoding="utf-8", errors="replace"), all_secrets)

            turn_red.close()

        # 5. Verify zero lingering child processes, zero temp files/cookie jars
        children_after = _child_pids_of_self()
        tmpdir_after = set(os.listdir(tmpdir_path))
        ws_after = set(os.listdir(ws_dir))

        assert children_after - children_before == set()
        assert tmpdir_after == tmpdir_before
        assert ws_after - ws_before == set()
        assert "portal_cookies.txt" not in ws_after
    finally:
        gw_logger.removeHandler(log_handler)
