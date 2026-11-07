"""Deterministic loopback OpenAI fixture; never contacts an external provider."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class FakeProvider:
    # LLM: Own one loopback listener and its scripted responses; callers own the temporary root.
    # 函数用途: Configure a deterministic provider without starting network activity.
    def __init__(self, root: Path, *, script=None, https=False, first_token_delay=0.0,
                 inter_token_delay=0.0, length=1, handshake_delay=0.0, auto_tool=None, long_stream_length=None,
                 teardown_seconds=5):
        self.root, self.script, self.https = Path(root), list(script or []), https
        self.first_token_delay, self.inter_token_delay = first_token_delay, inter_token_delay
        self.length, self.handshake_delay = length, handshake_delay
        self.auto_tool, self.long_stream_length = auto_tool, long_stream_length
        self.teardown_seconds = teardown_seconds
        self.active_requests = set()
        self.records, self.tokens, self.errors = [], [], []
        self.replacements = {}
        self.accepts = self.handshakes = 0
        self.condition = threading.Condition()
        self.release = threading.Event()
        self.phase_releases = {}
        self.closed = threading.Event()
        self.server = self.thread = None

    # LLM: Certificate files are generated exclusively below the test root; no trust store changes.
    # 函数用途: Create a short-lived loopback certificate and server TLS context.
    def _tls_context(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        self.root.mkdir(parents=True, exist_ok=True)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
                .not_valid_after(now + timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
                .sign(key, hashes.SHA256()))
        cert_path, key_path = self.root / "cert.pem", self.root / "key.pem"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        self.cert_path = cert_path
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_path, key_path)
        return context

    # LLM: Counts actual TCP accepts and completed TLS handshakes, not model-call guesses.
    # 函数用途: Start the loopback HTTP server and expose its base URL.
    def start(self):
        provider = self
        context = self._tls_context() if self.https else None

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            # LLM: Register before thread startup so teardown cannot miss a not-yet-running handler.
            # 函数用途: Track every owned client connection across its entire handler lifetime.
            def process_request(self, request, client_address):
                with provider.condition:
                    if provider.closed.is_set():
                        self.shutdown_request(request)
                        return
                    provider.active_requests.add(request)
                try:
                    super().process_request(request, client_address)
                except BaseException as error:
                    with provider.condition:
                        provider.active_requests.discard(request)
                        provider.errors.append(error)
                        provider.condition.notify_all()
                    raise

            # LLM: Error publication precedes removal; an empty active set makes the final error snapshot complete.
            # 函数用途: Notify teardown only after a request handler and its error path have finished.
            def process_request_thread(self, request, client_address):
                try:
                    super().process_request_thread(request, client_address)
                finally:
                    with provider.condition:
                        provider.active_requests.discard(request)
                        provider.condition.notify_all()

            # LLM: Accept accounting precedes optional TLS; handshake failures are not successes.
            # 函数用途: Count transport establishment for connection-reuse measurements.
            def get_request(self):
                sock, address = super().get_request()
                sock.settimeout(provider.teardown_seconds)
                provider.accepts += 1
                if context is not None:
                    provider.closed.wait(provider.handshake_delay)
                    try:
                        sock = context.wrap_socket(sock, server_side=True)
                    except BaseException:
                        sock.close()
                        raise
                    provider.handshakes += 1
                return sock, address

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            disable_nagle_algorithm = True
            # LLM: Fixture requests must not leak prompts or authentication values into stderr.
            # 函数用途: Suppress default HTTP access logging.
            def log_message(self, *_args):
                return

            # LLM: Handler errors return to the test thread at teardown instead of masquerading as measured disconnects.
            # 函数用途: Retain producer-side protocol and fixture failures for the foreground test.
            def do_POST(self):
                try:
                    self.serve_post()
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True
                except BaseException as error:
                    with provider.condition:
                        provider.errors.append(error)
                    raise

            # LLM: Requests use a deterministic script index; blocked calls expose a condition to clients.
            # 函数用途: Serve JSON or SSE chat completions and record the complete request evidence.
            def serve_post(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                body = json.loads(raw)
                probe = any(tool.get("function", {}).get("name") == "my_agent_capability_probe" for tool in body.get("tools", []))
                with provider.condition:
                    index = len(provider.records)
                    script_index = sum(not row["is_probe"] for row in provider.records)
                    row = {"method": "POST", "path": self.path,
                           "body_hash": hashlib.sha256(raw).hexdigest(), "body": body,
                           "connection_id": str(self.client_address), "turn": index, "is_probe": probe}
                    provider.records.append(row)
                    provider.condition.notify_all()
                if probe:
                    nonce = re.search(r"nonce ([0-9a-f]+)", str(body["messages"][-1]["content"])).group(1)
                    entry = {"tool_calls": [{"id": f"probe-{index}", "type": "function", "function": {
                        "name": "my_agent_capability_probe", "arguments": json.dumps({"nonce": nonce})}}]}
                else:
                    entry = provider.script[script_index] if script_index < len(provider.script) else {}
                    if not entry:
                        matches = [(i, str(message.get("content", ""))) for i, message in enumerate(body.get("messages", []))
                                   if message.get("role") == "user" and "fixture-kind=" in str(message.get("content", ""))]
                        if matches:
                            position, prompt = matches[-1]
                            owner = re.search(r"fixture-owner=([^;]+)", prompt)
                            kind = re.search(r"fixture-kind=(qa|tool|stream)", prompt)
                            tag = re.search(r"fixture-tag=([^;]+)", prompt)
                            row["fixture_owner"] = owner.group(1) if owner else ""
                            row["fixture_kind"] = kind.group(1) if kind else ""
                            if kind and kind.group(1) == "tool" and provider.auto_tool and not any(
                                    item.get("role") == "tool" for item in body["messages"][position + 1:]):
                                entry = {"tool_calls": [{"id": f"fixture-tool-{script_index}", "type": "function", "function": {
                                    "name": provider.auto_tool["name"], "arguments": json.dumps(provider.auto_tool["arguments"])}}]}
                            elif kind and kind.group(1) == "stream" and provider.long_stream_length:
                                entry = {"tokens": [f"<fixture-stream:{tag.group(1) if tag else row['fixture_owner']}:{index}:{i}>" for i in range(provider.long_stream_length)]}
                if entry.get("wait_for_release"):
                    name = "default" if entry["wait_for_release"] is True else entry["wait_for_release"]
                    with provider.condition:
                        gate = provider.phase_gate(name)
                        row["gate_name"], row["waiting"] = name, not gate.is_set()
                        provider.condition.notify_all()
                    released = gate.wait(float(entry.get("deadline_seconds", 10)))
                    with provider.condition:
                        row["waiting"] = False
                    if provider.closed.is_set():
                        return
                    if not released:
                        raise TimeoutError(f"provider phase gate timed out: {name}")
                provider.closed.wait(float(entry.get("first_token_delay", provider.first_token_delay)))
                calls = json.loads(json.dumps(entry.get("tool_calls", [])))
                for call in calls:
                    arguments = call["function"]["arguments"]
                    for key, value in provider.replacements.items():
                        arguments = arguments.replace("{{" + key + "}}", str(value))
                    call["function"]["arguments"] = arguments
                content = entry.get("content", "fixture-answer")
                tokens = entry.get("tokens", [content] * int(entry.get("length", provider.length)))
                message = {"role": "assistant", "content": content}
                if calls:
                    message["content"], message["tool_calls"] = None, calls
                if not body.get("stream"):
                    payload = json.dumps({"id": f"completion-{index}", "object": "chat.completion",
                        "model": body.get("model", "fixture"), "choices": [{"index": 0,
                        "message": message, "finish_reason": "tool_calls" if calls else "stop"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                try:
                    chunks = [{"content": token} for token in tokens] if not calls else [
                        {"tool_calls": [{"index": i, **call} for i, call in enumerate(calls)]}]
                    for offset, delta in enumerate(chunks):
                        if offset == 0:
                            delta = {"role": "assistant", **delta}
                        with provider.condition:
                            provider.tokens.append({"turn": index, "offset": offset, "at": time.monotonic(),
                                "text": delta.get("content", ""), "owner": row.get("fixture_owner", ""), "kind": row.get("fixture_kind", "")})
                            provider.condition.notify_all()
                        self._frame({"id": f"completion-{index}", "object": "chat.completion.chunk",
                            "choices": [{"index": 0, "delta": delta, "finish_reason": None}]})
                        provider.closed.wait(float(entry.get("inter_token_delay", provider.inter_token_delay)))
                    self._frame({"id": f"completion-{index}", "choices": [{"index": 0, "delta": {},
                        "finish_reason": "tool_calls" if calls else "stop"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
                    self._frame("[DONE]")
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True

            # LLM: Frame boundaries use HTTP chunking, preserving SSE token ordering.
            # 函数用途: Encode and flush one SSE event.
            def _frame(self, value):
                text = value if isinstance(value, str) else json.dumps(value)
                data = ("data: " + text + "\n\n").encode()
                self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                self.wfile.flush()

        self.server = Server(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.base_url = f"{'https' if self.https else 'http'}://127.0.0.1:{self.server.server_port}/v1"
        return self

    # LLM: Observable request counts, not sleeps, synchronize scenario control operations.
    # 函数用途: Wait with a bounded deadline until the provider received the requested number of calls.
    def wait_requests(self, count, timeout):
        with self.condition:
            return self.condition.wait_for(lambda: len(self.records) >= count, timeout)

    # LLM: A release belongs to one scenario phase; a previous phase cannot release a future wait.
    # 函数用途: Return the default or named release event without conflating separate blocked calls.
    def phase_gate(self, name="default"):
        with self.condition:
            if name == "default":
                return self.release
            return self.phase_releases.setdefault(name, threading.Event())

    # LLM: Scenario actions release only their named phase; teardown separately releases all phases.
    # 函数用途: Unblock the intended model call after its observable control boundary.
    def release_phase(self, name="default"):
        self.phase_gate(name).set()

    # LLM: Teardown releases scripted waits before closing this fixture's listener.
    # 函数用途: Stop the provider without leaving a listener thread behind.
    def close(self):
        self.closed.set()
        self.release.set()
        with self.condition:
            for gate in self.phase_releases.values():
                gate.set()
            connections = list(self.active_requests)
            self.condition.notify_all()
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # The owned connection may already have exited normally.
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=self.teardown_seconds)
            if self.thread.is_alive():
                raise TimeoutError("provider listener did not terminate")
        with self.condition:
            if not self.condition.wait_for(lambda: not self.active_requests, self.teardown_seconds):
                raise TimeoutError("provider handlers did not terminate")
            records, errors = list(self.records), list(self.errors)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "requests.json").write_text(json.dumps(records, indent=2) + "\n")
        if errors:
            raise errors[0]

    # LLM: Context ownership is local to the invoking test.
    # 函数用途: Start the provider on context entry.
    def __enter__(self):
        return self.start()

    # LLM: Exceptions never bypass provider teardown.
    # 函数用途: Close the provider on context exit.
    def __exit__(self, *_args):
        self.close()
