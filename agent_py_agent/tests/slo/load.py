"""Open-loop arrivals and isolated foreground Gateway process ownership."""
from __future__ import annotations

import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[3]


# LLM: One sample retains every offered arrival, including transport and receipt failures.
@dataclass
class Sample:
    offered_at: float
    timeout_ms: float
    sent_at: float | None = None
    accepted_at: float | None = None
    completed: bool = False
    rejected: bool = False
    timed_out: bool = False
    unconfirmed: bool = False
    status: int = 0
    code: str = ""
    request_id: str = ""
    receipt: str = ""
    kind: str = "qa"
    owner: str = ""
    phase: str = "warm"

    # LLM: Failures use their configured timeout, never a fast rejection latency.
    # 函数用途: Return the denominator-preserving latency value used in percentiles.
    def latency_ms(self):
        if self.accepted_at is None or self.sent_at is None:
            return self.timeout_ms
        return (self.accepted_at - self.sent_at) * 1000

    # LLM: Serialize evidence without reconstructing a sample from aggregates.
    # 函数用途: Export the sample's states and timestamps.
    def as_dict(self):
        return {**asdict(self), "offered": True, "sent": self.sent_at is not None,
                "accepted": self.accepted_at is not None, "latency_ms": self.latency_ms()}


# LLM: Only a queued disposition or an exact durable request file proves acceptance.
# 函数用途: Correlate a response with its durable receipt, excluding arbitrary HTTP success.
def durable_receipt(body, paths=None):
    rid = str(body.get("request_id") or "")
    if not rid:
        return ""
    if (body.get("delivery_status") == "accepted" and body.get("input_state") == "queued"
            and body.get("disposition") == "queued"):
        return "accepted_queued_disposition"
    if paths is not None:
        for directory in (paths.inbox, paths.processing):
            candidate = directory / f"{rid}.json"
            if candidate.is_file():
                row = json.loads(candidate.read_text())
                if str(row.get("id") or row.get("request_id")) == rid:
                    return f"file:{directory.name}"
    return ""


# LLM: Each request owns a short-lived client; identity is explicit and authentication remains real.
# 函数用途: Perform one loopback JSON HTTP request without retries or status masking.
def json_request(base_url, path, body=None, *, headers=None, timeout=10.0, timings=None):
    parsed = urlsplit(base_url)
    connection_type = http.client.HTTPConnection
    if timings is not None:
        started = time.monotonic()
        timings["clock"] = "client_monotonic"

        class TimedConnection(http.client.HTTPConnection):
            def connect(self):
                before = time.monotonic()
                try:
                    super().connect()
                finally:
                    timings["connect_ms"] = (time.monotonic() - before) * 1000

        connection_type = TimedConnection
    connection = connection_type(parsed.hostname, parsed.port, timeout=timeout)
    try:
        wire = json.dumps(body).encode() if body is not None else None
        connection.request("POST" if body is not None else "GET", path, wire,
                           {"Content-Type": "application/json", **(headers or {})})
        if timings is not None:
            sent = time.monotonic()
            timings["request_sent_ms"] = (sent - started) * 1000
        response = connection.getresponse()
        if timings is not None:
            received = time.monotonic()
            timings["response_headers_ms"] = (received - started) * 1000
            timings["sent_to_headers_ms"] = (received - sent) * 1000
        payload = response.read()
        if timings is not None:
            read = time.monotonic()
            timings["body_read_ms"] = (read - received) * 1000
        decoded = json.loads(payload) if payload else {}
        if timings is not None:
            timings["json_parse_ms"] = (time.monotonic() - read) * 1000
        return response.status, decoded
    finally:
        connection.close()


# LLM: The scheduler never waits for a preceding response before releasing a new arrival.
# 函数用途: Offer the fixed arrival profile and retain failures in the returned sample list.
def open_loop(profile, submit):
    start = time.monotonic()
    samples, threads, errors = [], [], []
    timer = threading.Event()

    # LLM: Timestamp receipt observation on this same process's monotonic clock.
    # 函数用途: Send one arrival and record confirmed, rejected or unknown disposition.
    def dispatch(sample, arrival):
        sample.sent_at = time.monotonic()
        try:
            status, body, receipt = submit(arrival)
            sample.status, sample.request_id = status, str(body.get("request_id") or "")
            sample.code = str(body.get("error_code") or body.get("error", {}).get("code", "")
                              if isinstance(body.get("error"), dict) else body.get("error_code") or "")
            sample.completed = body.get("status") in {"done", "completed"}
            if 200 <= status < 300 and receipt:
                sample.accepted_at, sample.receipt = time.monotonic(), receipt
            elif status >= 400:
                sample.rejected = True
                sample.code = sample.code or f"HTTP_{status}"
            else:
                sample.unconfirmed, sample.code = True, "RECEIPT_UNCONFIRMED"
        except TimeoutError:
            sample.timed_out, sample.code = True, "TIMEOUT"
        except (OSError, http.client.IncompleteRead) as exc:
            sample.unconfirmed, sample.code = True, type(exc).__name__
        except BaseException as error:
            errors.append(error)

    for arrival in profile["arrivals"]:
        offset = float(arrival["offset_ms"]) / 1000
        timer.wait(max(0, start + offset - time.monotonic()))
        sample = Sample(start + offset, float(profile["timeout_ms"]), kind=arrival.get("kind", "qa"),
                        owner=arrival.get("owner", ""), phase=arrival.get("phase", "warm"))
        samples.append(sample)
        thread = threading.Thread(target=dispatch, args=(sample, arrival), daemon=True)
        threads.append(thread)
        thread.start()
    for thread in threads:
        thread.join(float(profile["timeout_ms"]) / 1000 + 1)
        assert not thread.is_alive(), "arrival outlived its bounded transport deadline"
    if errors:
        raise errors[0]
    return samples


# LLM: Polling waits are deadline-bound and predicates refer to observable facts.
# 函数用途: Wait for a state or file without encoding a sleep-based scenario step.
def wait_until(predicate, timeout, poll):
    deadline = time.monotonic() + timeout
    event = threading.Event()
    while True:
        result = predicate()
        if result:
            return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError("observable condition deadline expired")
        event.wait(min(poll, remaining))


class GatewayProcess:
    # LLM: All subprocess files, configuration and working directory stay below root.
    # 函数用途: Prepare an isolated Gateway configuration with explicit scenario overrides.
    def __init__(self, root, provider, parameters, overrides=None):
        self.root = Path(root)
        self.home = self.root / "home"
        self.parameters = parameters
        self.provider = provider
        self.home.mkdir(parents=True, exist_ok=True)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"
        workspace = self.home / "owners" / "local" / "main" / "tasks" / "fixture"
        workspace.mkdir(parents=True, exist_ok=True)
        config = {"my_agent_home": str(self.home), "workspace_root": str(workspace),
                  "gateway_port": port, "gateway_bind_host": "127.0.0.1", "gateway_worker_process_count": 0,
                  "model_backend": "openai_compatible", "api_base": provider.base_url, "model_name": "fixture",
                  "api_key": "", "home_context_enabled": False, "memory_curator_enabled": False,
                  "enable_decision_model": False, "business_spaces_enabled": True, **(overrides or {})}
        self.config = self.root / "agent_config.yaml"
        self.config.write_text("\n".join(f"{key}: {json.dumps(value)}" for key, value in config.items()) + "\n")
        self._seed_model_catalog()
        self.proc = self.log = None

    # LLM: Managed turns resolve the real owner catalog, never the legacy flat YAML model projection.
    # 函数用途: Seed and select a loopback-only synthetic model through the public catalog operations.
    def _seed_model_catalog(self):
        from types import SimpleNamespace

        from agent_py_agent.agent.settings import AgentConfig
        from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation
        from agent_py_agent.agent.user_space.home_layout import home_paths
        from agent_py_agent.agent.user_space.owner_resolver import (
            OwnerIdentity,
            home_paths_with_owner,
            resolve_owner_home,
        )
        owner = resolve_owner_home(self.home, OwnerIdentity.local_main())
        host = SimpleNamespace(home_paths=home_paths_with_owner(home_paths(self.home), owner), config=AgentConfig())
        profile_id = "00000000-0000-4000-8000-000000000001"
        execute_model_profile_operation(host, "add", {"profile_id": profile_id, "profile": {
            "model_backend": "openai_compatible", "model_name": "fixture", "api_base": self.provider.base_url,
            "api_key": "fixture-not-a-secret", "model_context_window_tokens": self.parameters["model_context_window_tokens"]}})
        execute_model_profile_operation(host, "set_default", {"profile_id": profile_id})
        execute_model_profile_operation(host, "set_shared", {"profile_id": profile_id, "enabled": True})
        execute_model_profile_operation(host, "set_initial", {"profile_id": profile_id})

    def _command(self):
        """默认仍走原 CLI；回放夹具可只替换启动引导，不复制进程寿命与清理逻辑。"""
        return [sys.executable, "-m", "agent_py_agent", "--config", str(self.config), "gateway", "run"]

    # LLM: Startup uses the current interpreter and checkout, not an installed editable tree.
    # 函数用途: Start a foreground Gateway child and wait for its real status endpoint.
    def start(self):
        if self.proc is not None and self.proc.poll() is None:
            raise RuntimeError("the fixture Gateway is already running")
        if self.log is not None:
            self.log.close()
        env = {**os.environ, "HOME": str(self.home), "MY_AGENT_HOME": str(self.home),
               "PYTHONPATH": os.pathsep.join([str(REPO.parent), str(REPO)]), "PYTHONDONTWRITEBYTECODE": "1"}
        env.pop("MY_AGENT_HOSTING_GATEWAY_PID", None)
        self.log = (self.root / "gateway-process.log").open("ab")
        self.proc = subprocess.Popen(self._command(),
                                     cwd=self.root, env=env, stdout=self.log, stderr=self.log, start_new_session=True)
        observation = {"probes": 0, "response_status": None, "transport_error": None}
        # LLM: A live PID alone is not readiness; only a successful status response ends startup.
        # 函数用途: Probe readiness while preserving early subprocess failure evidence.
        def ready():
            if self.proc.poll() is not None:
                raise AssertionError((self.root / "gateway-process.log").read_text())
            observation["probes"] += 1
            try:
                status, _ = json_request(self.base_url, "/status", timeout=self.parameters["probe_seconds"])
                observation.update(response_status=status, transport_error=None)
                return status == 200
            except (OSError, http.client.HTTPException) as error:
                observation.update(response_status=None, transport_error=(type(error).__name__, getattr(error, "errno", None)))
                return False
        try:
            wait_until(ready, self.parameters["startup_seconds"], self.parameters["poll_seconds"])
        except AssertionError as error:
            if self.proc.poll() is not None:
                raise
            # 只读夹具自己的最后 16 KiB，输出启动阶段和栈帧，不回显模型正文、配置或请求头。
            with (self.root / "gateway-process.log").open("rb") as log:
                log.seek(max(0, log.seek(0, os.SEEK_END) - 16 * 1024))
                tail = log.read().decode(errors="replace").splitlines()
            stages = [line.strip() for line in tail if line.lstrip().startswith(("[gateway-run]", "File \"", "Thread 0x"))]
            raise AssertionError(f"Gateway readiness deadline: {observation}; pid={self.proc.pid}; "
                                 f"stages={stages[-72:]}") from error
        return self

    # LLM: Credential values remain in memory and the isolated host's own credential file.
    # 函数用途: Load the real Gateway credential for an explicit synthetic identity.
    def headers(self, user="admin", channel="chat"):
        from agent_py_agent.tests.support.ci_authenticated_gateway import authenticated_headers
        return authenticated_headers(self.home, user=user, channel=channel)

    # LLM: Queue location is discovered only below the isolated root.
    # 函数用途: Return the Gateway's authoritative path contract after startup.
    def paths(self):
        from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
        states = list(self.home.rglob("gateway_state.json"))
        assert len(states) == 1, states
        return gateway_paths_from_root(states[0].parent)

    # LLM: Only this child's process group is signaled; wait is the exit proof.
    # 函数用途: Terminate the complete fixture process tree and reap the Gateway child.
    def close(self):
        if self.proc is not None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.proc.wait(timeout=self.parameters["teardown_seconds"])
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait(timeout=self.parameters["teardown_seconds"])
        if self.log:
            self.log.close()
